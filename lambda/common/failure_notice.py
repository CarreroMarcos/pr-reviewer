"""Failure-state notice (FR-028 **[D2]**; contracts/canonical-comment.md).

When a review permanently fails, the canonical comment carries a
system-generated failure state for the affected revision instead of
silently remaining stale. This module owns assembly + fenced publication
of that notice; NOTHING else (no worker wiring, no log emission, no SQS
attribute extraction — those are T054 scope, see boundaries below).

Content contract (canonical-comment.md content form 2):

* Fixed template, system-generated — never model output, never
  repository-derived text. The reviewed head SHA is the ONLY variable.
* Canonical marker injected first, exactly as with review content.
* FR-020 relationship: the notice does NOT route through
  `common.assemble.build_comment` — that gate mandates the §2.7 section
  shape (`## Summary`/`## Findings`/`## Risk Notes`), which the fixed
  template has no business carrying. The D2 contract defines the notice's
  own gate instead: marker present + fixed template + strict input
  validation here + the prohibition list pinned in contract tests. The
  template is a fixed string, so prohibitions hold by construction.

Publication (HLD §3.3 fenced path): `publish_failure_notice` runs the
shared `common.protocol.run_review` executor — establish → review (which
returns the fixed notice content; no LLM) → claim → live-head fence →
publish → conditional finalize — with the publish callback delegated to
`common.reconcile` (adopt + PATCH the surviving marker comment, or
creation-lease POST when none exists, with re-check). No new executor
logic, no new state fields (R6), no new permissions.

Trigger mapping (`should_publish_notice` + `notice_phase`): pure functions
over the D2 contract table. Transient/LLM-unusable rows publish at the
FIRST queue attempt (`receive_count <= 1` — an instant, friendly
"retrying" notice so the failure is never silent) and again — replacing
the first notice in place via the shared-marker PATCH path — at the FINAL
attempt (`receive_count >= max_receive_count`, default 3 —
`maxReceiveCount`, mirrored by `worker_handler._MAX_RECEIVE_COUNT` and
pinned to terraform/messaging.tf by tests/contracts/
test_terraform_contract.py; tolerate above after redrive). Intermediate
attempts (1 < count < max) post NOTHING — the first notice stays
untouched (post-once). Permanent rows (assemble-invalid, LLM-401,
invalid-key) publish the FINAL notice immediately — no retry will happen,
so the final wording is the honest one. GitHub-401 / lost-access /
invalid-envelope / superseded / stale / duplicate / non-failures skip.
Parsing `int(Attributes["ApproximateReceiveCount"])` out of the SQS
record is worker (T054) scope — these functions take the parsed ints.

Success clearing: no dedicated delete/edit step exists or is needed —
the notice comment carries the same canonical marker and its id is
persisted by `reconcile`, so the success-path publish (`_make_publish`)
PATCHes that same comment with the review content, replacing the notice
in place. A stale "retrying" notice therefore cannot survive a
successful publish; tests/unit/test_error_classification.py pins the
full first-failure → intermediate → final → success sequence.

Disposition boundary: `publish_failure_notice` RETURNS the HLD §4.3 /
research-R8 `failure_notice_published` value (`NoticeDisposition`:
`true` / `false` / `skipped-stale`). Emitting it into `logs.py`
FIXED_FIELDS is T054 scope — this module never imports logging
infrastructure. Mapping: PUBLISHED (+FINALIZE_CONFLICT — the notice
landed) → `true`; superseded/stale (incl. fence mismatch: stale revision
never published, FR-028 currency check) → `skipped-stale`; claim-held or
any publish fault → `false`.

Best-effort rule: notice publication never masks the alert/DLQ flow it
accompanies — any fault from the injected ports is caught and yields
`false`; nothing raises except caller bugs (invalid assembly inputs raise
`NoticeError` loudly — fail closed on untrusted-shaped input).

Pure stdlib, no I/O, no boto3 import, no model tools.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from common.marker import build_marker
from common.protocol import DEFAULT_MAX_ESTABLISH_ATTEMPTS, OutcomeKind, run_review
from common.reconcile import reconcile
from common.state import review_pk

NOTICE_TEMPLATE = (
    "Automated review could not be completed for revision {head_sha} after {attempts}. "
    "Push a new commit to trigger a fresh review. "
    "The failure has been logged for the operator."
)

RETRYING_TEMPLATE = (
    "Automated review did not complete for revision {head_sha}: the AI backend "
    "was slow to respond. It will be retried automatically in about 30 minutes; "
    "no action is needed. This comment will be updated with the review, or with "
    "next steps if all retries fail."
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_MAX_REPO_LENGTH = 128
_MAX_PR_NUMBER = 10**9
_MAX_ATTEMPTS = 99

# SQS redrive budget default (HLD §2.2, terraform/messaging.tf
# `maxReceiveCount = 3`); the worker mirrors it and the contract test
# pins terraform == worker == this default.
DEFAULT_MAX_RECEIVE_COUNT = 3


class NoticePhase(StrEnum):
    """Which fixed template a notice carries: RETRYING (first transient
    failure — automatic retry ahead) or FINAL (no more retries)."""

    RETRYING = "retrying"
    FINAL = "final"


class NoticeError(ValueError):
    """Typed notice rejection: `field` names the offending input
    (`"head_sha"`, `"repo_full_name"`, `"pr_number"`), `reason` is a
    machine-readable code (`bad_sha`, `bad_repo`, `bad_pr_number`),
    mirroring `common.envelope.EnvelopeError`."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid failure notice: {field}: {reason}")


class NoticeTrigger(StrEnum):
    """D2 trigger-table rows (contracts/canonical-comment.md): which HLD
    §2.3 item 8 conditions publish a failure notice."""

    TRANSIENT = "transient"  # provider/LLM/throttle/5xx, queue-retried
    LLM_UNUSABLE = "llm_unusable"  # timeout/429/5xx/invalid output, retried
    ASSEMBLE_INVALID = "assemble_invalid"  # structural validation failure
    LLM_401 = "llm_401"  # LLM auth after the single re-fetch
    # LLM request-construction fault — D2 trigger-table row
    # (specs/001-pr-reviewer/contracts/canonical-comment.md).
    INVALID_KEY = "invalid_key"
    GITHUB_401 = "github_401"  # GitHub auth after the single re-fetch
    LIST_FORBIDDEN = "list_forbidden"  # 403/404 on list/GET (lost access)
    INVALID_ENVELOPE = "invalid_envelope"  # schema-invalid/malformed message
    SUPERSEDED = "superseded"  # older revision rejected at establish
    STALE = "stale"  # fence/finalize mismatch, claim-held elsewhere
    DUPLICATE = "duplicate"  # idempotent re-delivery, filtered events
    NON_FAILURE = "non_failure"  # success paths, non-failure outcomes


class NoticeDisposition(StrEnum):
    """HLD §4.3 `failure_notice_published` values (research R8). The worker
    logs the `.value`; the enum keeps producers honest."""

    PUBLISHED_TRUE = "true"
    PUBLISHED_FALSE = "false"
    SKIPPED_STALE = "skipped-stale"


@dataclass(frozen=True)
class NoticeResult:
    """Fenced-publish outcome: the log disposition plus the comment id when
    the notice landed (`None` for `false` / `skipped-stale`)."""

    disposition: NoticeDisposition
    comment_id: int | None = None


def _check_sha(head_sha: Any) -> str:
    if not isinstance(head_sha, str) or not _SHA_RE.match(head_sha):
        raise NoticeError("head_sha", "bad_sha")
    return head_sha


def _check_repo(repo_full_name: Any) -> str:
    if (
        not isinstance(repo_full_name, str)
        or len(repo_full_name) > _MAX_REPO_LENGTH
        or not _REPO_RE.match(repo_full_name)
    ):
        raise NoticeError("repo_full_name", "bad_repo")
    return repo_full_name


def _check_pr_number(pr_number: Any) -> int:
    if isinstance(pr_number, bool) or not isinstance(pr_number, int):
        raise NoticeError("pr_number", "bad_pr_number")
    if not 1 <= pr_number <= _MAX_PR_NUMBER:
        raise NoticeError("pr_number", "bad_pr_number")
    return pr_number


def _attempts_phrase(attempts: int) -> str:
    """Plain-language attempts count for the final template."""
    return f"{attempts} attempt" if attempts == 1 else f"{attempts} attempts"


def build_failure_notice(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    *,
    phase: NoticePhase = NoticePhase.FINAL,
    attempts: int = 1,
) -> str:
    """Assemble the canonical failure-notice content: worker-injected
    marker, then the fixed D2 template with exactly the head SHA
    (and, for the final phase, the attempts count) interpolated. Inputs
    are validated strictly (`NoticeError`) — the template variables must
    be beyond reproach. `attempts` is ignored for the retrying phase (no
    count is claimed for a review that will be retried)."""
    _check_repo(repo_full_name)
    _check_pr_number(pr_number)
    sha = _check_sha(head_sha)
    if not isinstance(phase, NoticePhase):
        raise NoticeError("phase", "bad_phase")
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise NoticeError("attempts", "bad_attempts")
    if not 1 <= attempts <= _MAX_ATTEMPTS:
        raise NoticeError("attempts", "bad_attempts")
    if phase is NoticePhase.RETRYING:
        body = RETRYING_TEMPLATE.format(head_sha=sha)
    else:
        body = NOTICE_TEMPLATE.format(head_sha=sha, attempts=_attempts_phrase(attempts))
    return f"{build_marker(repo_full_name, pr_number)}\n\n{body}"


def should_publish_notice(
    trigger: NoticeTrigger,
    *,
    receive_count: int = 0,
    max_receive_count: int = DEFAULT_MAX_RECEIVE_COUNT,
) -> bool:
    """D2 trigger mapping (contracts/canonical-comment.md table).

    Transient rows publish at the FIRST attempt (instant retrying notice
    — failures are never silent) and at the FINAL attempt
    (`receive_count >= max_receive_count`); intermediate attempts stay
    silent so the first notice is never duplicated. Permanent content/
    auth rows publish immediately; every No-row skips at any count.
    `receive_count` arrives parsed (the worker reads the SQS attribute);
    this function owns only the comparison.
    """
    return (
        notice_phase(trigger, receive_count=receive_count, max_receive_count=max_receive_count)
        is not None
    )


def notice_phase(
    trigger: NoticeTrigger,
    *,
    receive_count: int = 0,
    max_receive_count: int = DEFAULT_MAX_RECEIVE_COUNT,
) -> NoticePhase | None:
    """Which notice (if any) this trigger row authorizes at this count.

    `None` → no notice. RETRYING only at the first attempt; FINAL at the
    final attempt and for permanent rows (no retry will happen, so the
    final wording is the honest one at any count).
    """
    if trigger in (NoticeTrigger.TRANSIENT, NoticeTrigger.LLM_UNUSABLE):
        if receive_count <= 1:
            return NoticePhase.RETRYING
        if receive_count >= max_receive_count:
            return NoticePhase.FINAL
        return None
    if trigger in (
        NoticeTrigger.ASSEMBLE_INVALID,
        NoticeTrigger.LLM_401,
        NoticeTrigger.INVALID_KEY,
    ):
        return NoticePhase.FINAL
    return None


def publish_failure_notice(
    *,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    owner: str,
    table: Any,
    now: Callable[[], int],
    fence: Callable[[], str],
    list_page: Callable[[int], Any],
    create_comment: Callable[[str], int],
    update_comment: Callable[[int, str], None],
    delete_comment: Callable[[int], None],
    max_establish_attempts: int = DEFAULT_MAX_ESTABLISH_ATTEMPTS,
    phase: NoticePhase = NoticePhase.FINAL,
    attempts: int = 1,
) -> NoticeResult:
    """Publish the failure notice through the fenced protocol (HLD §3.3).

    `review` returns the fixed notice content (no model call) and accepts
    the established `(head_sha, generation)` pair only for port-signature
    conformance (ignored — the notice carries no header);
    `publish` converges via `common.reconcile` (adopt + PATCH the
    surviving marker comment, creation-lease POST when none exists) — so
    a retrying notice is REPLACED in place by the final notice, and by
    the review content on eventual success. `phase`/`attempts` select
    the fixed template (see `build_failure_notice`). Returns the log
    disposition — never raises on publish-path faults (best-effort:
    `false`). Assembly-input faults (`NoticeError`) DO propagate:
    invalid inputs are caller bugs, failed closed and loud.
    """
    content = build_failure_notice(
        repo_full_name, pr_number, head_sha, phase=phase, attempts=attempts
    )
    pk = review_pk(repo_full_name, pr_number)

    def review(head_sha: str, generation: int) -> str:
        return content

    def publish(body: str) -> int:
        return reconcile(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            pk=pk,
            owner=owner,
            content=body,
            table=table,
            now=now,
            list_page=list_page,
            create_comment=create_comment,
            update_comment=update_comment,
            delete_comment=delete_comment,
        )

    try:
        outcome = run_review(
            pk=pk,
            incoming_sha=head_sha,
            owner=owner,
            table=table,
            now=now,
            review=review,
            fence=fence,
            publish=publish,
            max_establish_attempts=max_establish_attempts,
        )
    except Exception:
        # Best-effort: the notice accompanies an alert/DLQ flow that must
        # never be masked by the notice's own failure (no logging infra
        # here by design — the worker logs the returned disposition).
        return NoticeResult(NoticeDisposition.PUBLISHED_FALSE)
    if outcome.kind in (
        OutcomeKind.PUBLISHED,
        OutcomeKind.PUBLISHED_FINALIZE_CONFLICT,
    ):
        # Conflict still landed the notice (log-and-reconcile path) —
        # the newer revision owns state, but the content is out there.
        return NoticeResult(NoticeDisposition.PUBLISHED_TRUE, outcome.comment_id)
    if outcome.kind in (
        OutcomeKind.DISCARDED_SUPERSEDED,
        OutcomeKind.DISCARDED_STALE,
    ):
        # Stale revision: FR-028 currency check — never published.
        return NoticeResult(NoticeDisposition.SKIPPED_STALE)
    return NoticeResult(NoticeDisposition.PUBLISHED_FALSE)

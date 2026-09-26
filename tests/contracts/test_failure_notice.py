"""T052: failure-state notice contract **[D2]** (FR-028, FR-019, FR-020,
FR-026; contracts/canonical-comment.md).

Exercises `common.failure_notice` — assembly of the fixed template plus
fenced publication of the notice through the HLD §3.3 path, with the D2
trigger mapping as a pure decision function.

Mapping (contract row → test):

* fixed template, SHA the only variable → `test_final_template_*`,
  `test_retrying_template_*`, `test_attempts_*` (exact text, two SHAs
  differ only in the SHA)
* canonical marker present, worker-injected → `test_marker_present_once_first`
* prohibition list (no internal error details / provider names / statuses /
  credential-like strings / @mentions / external media / verdicts) →
  `test_prohibition_list`
* malformed SHA rejected → `test_malformed_sha_rejected`
* fenced publish: stored comment → PATCH + finalize, disposition true →
  `test_fenced_publish_patches_stored_comment`
* no canonical comment → creation-lease POST → persist, true →
  `test_absent_comment_posts_via_creation_lease`
* stale revision (fence mismatch) → nothing published, `skipped-stale` →
  `test_stale_revision_skipped_stale`
* finalize conflict → landed anyway (`true`), newer record intact →
  `test_finalize_conflict_still_true_newer_intact`
* idempotent re-publish → same id PATCHed twice, one comment →
  `test_idempotent_republish_patches_same_comment`
* unparseable list / transport failure → `false`, never raises
  (best-effort) → `test_unpublishable_list_returns_false`,
  `test_transport_failure_returns_false_never_raises`
* trigger table (contract §"Trigger mapping") → `test_trigger_*` matrix:
  transient + LLM-unusable at the FIRST attempt (retrying) and at/past
  the FINAL attempt (final), silent in between; assemble-invalid and
  LLM-401 publish immediately; GitHub-401 / lost-access / invalid
  envelope / superseded / stale / duplicate / non-failures skip
* phase mapping → `test_notice_phase_matrix`
  (`notice_phase`: which template each row authorizes)
* phase/attempts reach the published content →
  `test_publish_phase_and_attempts_reach_the_content`
* no-notice headline rows → `test_no_notice_rows_skip`

Ports (duck-typed — fakes mirror these exactly):

* table: `get_item(pk)` (copy semantics) + `update_item(...)` raising
  `common.protocol.ConditionalCheckFailed` on guard mismatch. `FakeTable`
  evaluates EXACTLY the condition strings the fenced path emits
  (establish/claim/finalize/creation-lease) and raises ValueError on
  anything else — Gate-2 discipline: a builder-string change breaks
  tests, never passes silently.
* GitHub side: `list_page(page)` (parsed JSON), `create_comment(body)`,
  `update_comment(id, body)` (PATCH), `delete_comment(id)`.

Disposition boundary: `publish_failure_notice` RETURNS the HLD §4.3 /
research-R8 `failure_notice_published` value (`true`/`false`/
`skipped-stale`); emitting it into `logs.py` FIXED_FIELDS and wiring the
worker call site (+ SQS `ApproximateReceiveCount` extraction) is T054
scope, not this module's.

All deterministic: fixed clock (`NOW`), scripted collaborators, no sleeps.
RED phase: `common.failure_notice` does not exist yet — ImportError-driven
failure is the expected red.
"""

import pytest

from common.failure_notice import (
    DEFAULT_MAX_RECEIVE_COUNT,
    NoticeDisposition,
    NoticePhase,
    NoticeTrigger,
    build_failure_notice,
    notice_phase,
    publish_failure_notice,
    should_publish_notice,
)
from common.marker import build_marker
from common.protocol import ConditionalCheckFailed

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
MARKER = build_marker(REPO, PR_NUMBER)
SHA_A = "aa" * 20
SHA_B = "bb" * 20
BASE_SHA = "00" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
NOW = 1_750_000_000
STORED_ID = 555
POST_ID = 777
UPDATED_AT = "2026-09-12T10:00:00Z"

EXPECTED_FINAL_TEMPLATE = (
    "Automated review could not be completed for revision {sha} after {attempts}. "
    "Push a new commit to trigger a fresh review. "
    "The failure has been logged for the operator."
)
EXPECTED_RETRYING_TEMPLATE = (
    "Automated review did not complete for revision {sha}: the AI backend "
    "was slow to respond. It will be retried automatically in about 30 minutes; "
    "no action is needed. This comment will be updated with the review, or with "
    "next steps if all retries fail."
)

# Establish/claim/finalize/creation-lease condition vocabulary (exact
# strings emitted by common.state builders + common.reconcile).
_FIRST_WRITE = "attribute_not_exists(pk)"
_EQUALITY = "last_seen_sha = :sha"
_GENERATION_GUARD = "generation = :expected_gen"
_CLAIM = (
    "head_sha = :reviewed AND generation = :gen "
    "AND (claim_until < :now OR attribute_not_exists(claim_owner))"
)
_FINALIZE = "head_sha = :reviewed AND generation = :gen AND claim_owner = :owner"
_CREATION_LEASE = (
    "head_sha = :reviewed AND generation = :gen "
    "AND (claim_until < :now OR attribute_not_exists(claim_owner) "
    "OR claim_owner = :owner) AND attribute_not_exists(comment_id)"
)


class FakeTable:
    """Minimal table double evaluating exactly the fenced-path vocabulary."""

    def __init__(self):
        self.items = {}
        self.updates = []

    def get_item(self, pk):
        item = self.items.get(pk)
        return dict(item) if item is not None else None

    def update_item(
        self,
        *,
        Key,
        UpdateExpression,
        ConditionExpression,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
    ):
        pk = Key["pk"]
        self.updates.append(ConditionExpression)
        current = self.items.get(pk)
        values = ExpressionAttributeValues or {}
        if not self._holds(ConditionExpression, current, values):
            raise ConditionalCheckFailed(f"condition not met: {ConditionExpression}")
        names = ExpressionAttributeNames or {}
        if not UpdateExpression.startswith("SET "):
            raise ValueError(f"unsupported update: {UpdateExpression!r}")
        set_part, sep, remove_part = UpdateExpression[4:].partition(" REMOVE ")
        item = dict(current) if current is not None else {"pk": pk}
        for clause in set_part.split(", "):
            attr, _, placeholder = clause.partition(" = ")
            item[names.get(attr, attr)] = values[placeholder]
        if sep:
            for attr in remove_part.split(", "):
                item.pop(names.get(attr, attr), None)
        self.items[pk] = item
        return {"Attributes": dict(item)}

    def _holds(self, condition, current, values):
        if condition == _FIRST_WRITE:
            return current is None
        if condition == _EQUALITY:
            return current is not None and current.get("last_seen_sha") == values[":sha"]
        if condition == _GENERATION_GUARD:
            return current is not None and current.get("generation") == values[":expected_gen"]
        if condition == _CLAIM:
            return (
                current is not None
                and current.get("head_sha") == values[":reviewed"]
                and current.get("generation") == values[":gen"]
                and (current.get("claim_until", 0) < values[":now"] or "claim_owner" not in current)
            )
        if condition == _FINALIZE:
            return (
                current is not None
                and current.get("head_sha") == values[":reviewed"]
                and current.get("generation") == values[":gen"]
                and current.get("claim_owner") == values[":owner"]
            )
        if condition == _CREATION_LEASE:
            return (
                current is not None
                and current.get("head_sha") == values[":reviewed"]
                and current.get("generation") == values[":gen"]
                and (
                    current.get("claim_until", 0) < values[":now"]
                    or "claim_owner" not in current
                    or current.get("claim_owner") == values[":owner"]
                )
                and "comment_id" not in current
            )
        raise ValueError(f"unsupported condition: {condition!r}")


class ScriptedGitHub:
    """Comment-store double with scriptable transport faults."""

    def __init__(self, *, comments=None, post_id=POST_ID, fault=None):
        self.comments = [dict(c) for c in (comments or [])]
        self._post_id = post_id
        self._fault = fault  # (method, status) injected once, then cleared
        self.calls = []

    def list_page(self, page):
        self.calls.append(("GET", page))
        assert page == 1  # single-page fixtures throughout
        return [dict(c) for c in self.comments]

    def create_comment(self, body):
        self.calls.append(("POST", body))
        self._maybe_fault("POST")
        self.comments.append({"id": self._post_id, "body": body})
        return self._post_id

    def update_comment(self, comment_id, body):
        self.calls.append(("PATCH", comment_id))
        self._maybe_fault("PATCH")
        for comment in self.comments:
            if comment["id"] == comment_id:
                comment["body"] = body
                return
        raise AssertionError(f"PATCH of unknown comment {comment_id}")

    def delete_comment(self, comment_id):
        self.calls.append(("DELETE", comment_id))
        self.comments = [c for c in self.comments if c["id"] != comment_id]

    def _maybe_fault(self, method):
        if self._fault is not None and self._fault[0] == method:
            _, status = self._fault
            self._fault = None
            raise _TransportError(status)


class _TransportError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"transport failed: {status}")


def _seed_active(table, *, head, gen, comment, owner=GUID_1, until=NOW + 100):
    table.items[PK] = {
        "pk": PK,
        "status": "ACTIVE",
        "generation": gen,
        "head_sha": head,
        "last_seen_sha": head,
        "claim_owner": owner,
        "claim_until": until,
        "comment_id": comment,
        "updated_at": UPDATED_AT,
    }


def _publish(table, github, *, sha=SHA_B, live=None, on_publish=None, owner=GUID_1, **kwargs):
    live_shas = [live or sha]

    def fence():
        if len(live_shas) > 1:
            return live_shas.pop(0)
        if on_publish is not None and live_shas[0] == "hook":
            on_publish()
            return sha
        return live_shas[0]

    return publish_failure_notice(
        repo_full_name=REPO,
        pr_number=PR_NUMBER,
        head_sha=sha,
        owner=owner,
        table=table,
        now=lambda: NOW,
        fence=fence,
        list_page=github.list_page,
        create_comment=github.create_comment,
        update_comment=github.update_comment,
        delete_comment=github.delete_comment,
        **kwargs,
    )


# --- construction: fixed template, SHA (+ attempts) the only variables ---


def test_final_template_exact_text_sha_only_variable():
    """The final notice is the fixed D2 template with exactly the head SHA
    interpolated — two SHAs yield bodies differing ONLY in the SHA."""
    first = build_failure_notice(REPO, PR_NUMBER, SHA_A)
    second = build_failure_notice(REPO, PR_NUMBER, SHA_B)
    assert EXPECTED_FINAL_TEMPLATE.format(sha=SHA_A, attempts="1 attempt") in first
    assert EXPECTED_FINAL_TEMPLATE.format(sha=SHA_B, attempts="1 attempt") in second
    assert first.replace(SHA_A, "") == second.replace(SHA_B, "")


def test_retrying_template_on_first_phase():
    """`phase=retrying` swaps in the friendly retrying template — what
    happened, what happens next, no action needed — with no attempts
    count claimed for a review that will be retried."""
    content = build_failure_notice(REPO, PR_NUMBER, SHA_B, phase=NoticePhase.RETRYING)
    assert EXPECTED_RETRYING_TEMPLATE.format(sha=SHA_B) in content
    assert "attempt" not in content


def test_final_template_pluralizes_attempts():
    """Queue exhaustion names the real budget: 3 attempts (plural); a
    single permanent failure reads `1 attempt`."""
    three = build_failure_notice(REPO, PR_NUMBER, SHA_B, attempts=3)
    one = build_failure_notice(REPO, PR_NUMBER, SHA_B)
    assert EXPECTED_FINAL_TEMPLATE.format(sha=SHA_B, attempts="3 attempts") in three
    assert EXPECTED_FINAL_TEMPLATE.format(sha=SHA_B, attempts="1 attempt") in one


def test_attempts_beyond_sha_is_the_only_other_variable():
    """For a fixed phase+attempts, the SHA stays the only body variable."""
    a2 = build_failure_notice(REPO, PR_NUMBER, SHA_A, attempts=3)
    b2 = build_failure_notice(REPO, PR_NUMBER, SHA_B, attempts=3)
    assert a2.replace(SHA_A, "") == b2.replace(SHA_B, "")


def test_invalid_phase_and_attempts_rejected():
    """Strict input validation fail closed: a non-NoticePhase phase and a
    bad attempts value raise typed `NoticeError`, never interpolate."""
    from common.failure_notice import NoticeError

    with pytest.raises(NoticeError) as phase_exc:
        build_failure_notice(REPO, PR_NUMBER, SHA_B, phase="retrying")  # type: ignore[arg-type]
    assert phase_exc.value.field == "phase"
    for bad_attempts in (0, -1, True, "3", 2.0, 100):
        with pytest.raises(NoticeError) as attempts_exc:
            build_failure_notice(REPO, PR_NUMBER, SHA_B, attempts=bad_attempts)  # type: ignore[arg-type]
        assert attempts_exc.value.field == "attempts"


def test_marker_present_once_first():
    """The worker-injected canonical marker opens the notice exactly once —
    canonical identity survives even when the body is a failure state."""
    content = build_failure_notice(REPO, PR_NUMBER, SHA_B)
    assert content.startswith(MARKER)
    assert content.count(MARKER) == 1


def test_prohibition_list():
    """No internal error details, provider names, statuses, credential-like
    strings, @mentions, external media, or approval/merge verdicts —
    the notices carry zero operable or misleading content (FR-026/028).
    Plain-language "retry"/"30 minutes" wording IS allowed (the first-
    failure notice is honest about the automatic retry); internal error
    vocabulary (timeout codes, statuses) is still banned."""
    bodies = [
        build_failure_notice(REPO, PR_NUMBER, SHA_B),
        build_failure_notice(REPO, PR_NUMBER, SHA_B, phase=NoticePhase.RETRYING),
        build_failure_notice(REPO, PR_NUMBER, SHA_B, attempts=3),
    ]
    for content in bodies:
        lowered = content.lower()
        for forbidden in (
            "@",
            "http",
            "z.ai",
            "glm",
            "openai",
            "anthropic",
            "401",
            "403",
            "404",
            "429",
            "timeout",
            "traceback",
            "stack",
            "endpoint",
            "api_key",
            "error_class",
            "ghp_",
            "safe to merge",
            "approved",
            "lgtm",
            "dlq",
            "sqs",
            "dynamodb",
        ):
            assert forbidden not in lowered, f"prohibited substring present: {forbidden!r}"


def test_malformed_sha_rejected():
    """The SHA is the only variable, so it is validated strictly: short,
    non-hex, and uppercase SHAs are refused loudly (never interpolated)."""
    from common.failure_notice import NoticeError

    for bad in ("zz", "A" * 40, SHA_B[:-1], "", "bb" * 19 + "bg"):
        try:
            build_failure_notice(REPO, PR_NUMBER, bad)
        except NoticeError as exc:
            assert exc.field == "head_sha"
        else:
            raise AssertionError(f"expected NoticeError for {bad!r}")


# --- fenced publish: claim → fence → PATCH / lease-POST → finalize ---


def test_fenced_publish_patches_stored_comment():
    """ACTIVE record with a stored id → fence (live == reviewed) → PATCH
    that id with the notice → finalize ACTIVE; disposition `true`."""
    table, github = (
        FakeTable(),
        ScriptedGitHub(comments=[{"id": STORED_ID, "body": "old review " + MARKER}]),
    )
    _seed_active(table, head=SHA_B, gen=2, comment=STORED_ID)
    result = _publish(table, github)
    assert result.disposition == NoticeDisposition.PUBLISHED_TRUE
    assert result.comment_id == STORED_ID
    assert github.calls[0][0] == "GET"  # reconcile lists before adopting
    assert ("PATCH", STORED_ID) in github.calls
    assert table.items[PK]["comment_id"] == STORED_ID
    assert table.items[PK]["status"] == "ACTIVE"
    assert (
        EXPECTED_FINAL_TEMPLATE.format(sha=SHA_B, attempts="1 attempt")
        in github.comments[0]["body"]
    )


def test_absent_comment_posts_via_creation_lease():
    """No canonical comment anywhere → creation lease won → POST → persist
    → re-check; exactly one marker-bearing comment exists; `true`."""
    table, github = FakeTable(), ScriptedGitHub()
    result = _publish(table, github)
    assert result.disposition == NoticeDisposition.PUBLISHED_TRUE
    assert result.comment_id == POST_ID
    assert [c for c in github.calls if c[0] == "POST"]
    assert len(github.comments) == 1
    assert github.comments[0]["body"].count(MARKER) == 1
    assert table.items[PK]["comment_id"] == POST_ID


def test_stale_revision_skipped_stale():
    """Fence mismatch (live moved past the reviewed revision) → the notice
    for a stale revision is NOT published: no GitHub write of any kind
    after the fence, disposition `skipped-stale` (FR-028 currency check)."""
    table, github = (
        FakeTable(),
        ScriptedGitHub(comments=[{"id": STORED_ID, "body": "old review " + MARKER}]),
    )
    _seed_active(table, head=SHA_A, gen=1, comment=STORED_ID)
    result = _publish(table, github, sha=SHA_A, live="cc" * 20)
    assert result.disposition == NoticeDisposition.SKIPPED_STALE
    assert result.comment_id is None
    assert github.calls == []
    assert table.items[PK]["head_sha"] == SHA_A  # record untouched


def test_finalize_conflict_still_true_newer_intact():
    """A newer accepted revision landing between publish and finalize →
    the notice already landed, so disposition stays `true`, and the newer
    record is never overwritten (log-and-reconcile path)."""
    table, github = (
        FakeTable(),
        ScriptedGitHub(comments=[{"id": STORED_ID, "body": "old review " + MARKER}]),
    )
    _seed_active(table, head=SHA_A, gen=4, comment=STORED_ID)

    def land_newer():
        _seed_active(table, head=SHA_B, gen=6, comment=999)

    result = _publish(table, github, sha=SHA_A, live="hook", on_publish=land_newer)
    assert result.disposition == NoticeDisposition.PUBLISHED_TRUE
    assert table.items[PK]["head_sha"] == SHA_B
    assert table.items[PK]["generation"] == 6


def test_idempotent_republish_patches_same_comment():
    """Re-delivery re-attempts the identical PATCH: two publishes, one
    comment, same id, both `true` — convergent, never duplicating."""
    table, github = (
        FakeTable(),
        ScriptedGitHub(comments=[{"id": STORED_ID, "body": "old review " + MARKER}]),
    )
    _seed_active(table, head=SHA_B, gen=2, comment=STORED_ID)
    first = _publish(table, github)
    second = _publish(table, github)
    assert first.disposition == second.disposition == NoticeDisposition.PUBLISHED_TRUE
    assert first.comment_id == second.comment_id == STORED_ID
    assert len(github.comments) == 1
    assert [c for c in github.calls if c[0] == "POST"] == []


def test_unpublishable_list_returns_false():
    """Unparseable listing → best-effort `false`, never raises: the notice
    failure must not mask the alert/DLQ path it accompanies."""
    table, github = FakeTable(), ScriptedGitHub()
    github.list_page = lambda page: {"message": "oops"}  # noqa: E731 (fault double)
    result = _publish(table, github)
    assert result.disposition == NoticeDisposition.PUBLISHED_FALSE
    assert result.comment_id is None


def test_transport_failure_returns_false_never_raises():
    """A 500 on the POST → best-effort `false`, no exception escapes (the
    queue owns retries for the underlying error, not the notice)."""
    table, github = FakeTable(), ScriptedGitHub(fault=("POST", 500))
    result = _publish(table, github)
    assert result.disposition == NoticeDisposition.PUBLISHED_FALSE
    assert result.comment_id is None


# --- trigger mapping: which §2.3 item 8 rows publish ---


@pytest.mark.parametrize(
    ("trigger", "receive_count", "expected"),
    [
        # Transient at the FIRST attempt → publish the RETRYING notice
        # (failures are never silent); intermediate attempts → skip
        # (post-once); at/past the FINAL attempt → publish the FINAL
        # notice (counts above max tolerated after redrive).
        (NoticeTrigger.TRANSIENT, 1, True),
        (NoticeTrigger.LLM_UNUSABLE, 1, True),
        (NoticeTrigger.TRANSIENT, 2, False),
        (NoticeTrigger.LLM_UNUSABLE, 2, False),
        (NoticeTrigger.TRANSIENT, 3, True),
        (NoticeTrigger.LLM_UNUSABLE, 3, True),
        (NoticeTrigger.TRANSIENT, 4, True),  # tolerate > 3 after redrive
        (NoticeTrigger.TRANSIENT, 7, True),
        (NoticeTrigger.LLM_UNUSABLE, 9, True),
        # Permanent rows → publish immediately (receive count irrelevant).
        (NoticeTrigger.ASSEMBLE_INVALID, 1, True),
        (NoticeTrigger.ASSEMBLE_INVALID, 5, True),
        (NoticeTrigger.LLM_401, 1, True),
        (NoticeTrigger.INVALID_KEY, 1, True),
        (NoticeTrigger.INVALID_KEY, 5, True),
        # No-rows → skip at any count.
        (NoticeTrigger.GITHUB_401, 5, False),
        (NoticeTrigger.LIST_FORBIDDEN, 5, False),
        (NoticeTrigger.INVALID_ENVELOPE, 1, False),
        (NoticeTrigger.SUPERSEDED, 1, False),
        (NoticeTrigger.STALE, 3, False),
        (NoticeTrigger.DUPLICATE, 2, False),
        (NoticeTrigger.NON_FAILURE, 5, False),
    ],
)
def test_trigger_matrix(trigger, receive_count, expected):
    assert (
        should_publish_notice(
            trigger, receive_count=receive_count, max_receive_count=DEFAULT_MAX_RECEIVE_COUNT
        )
        is expected
    )


@pytest.mark.parametrize(
    ("trigger", "receive_count", "expected_phase"),
    [
        (NoticeTrigger.TRANSIENT, 1, NoticePhase.RETRYING),
        (NoticeTrigger.LLM_UNUSABLE, 1, NoticePhase.RETRYING),
        (NoticeTrigger.TRANSIENT, 2, None),  # intermediate: post-once
        (NoticeTrigger.LLM_UNUSABLE, 3, NoticePhase.FINAL),
        (NoticeTrigger.TRANSIENT, 4, NoticePhase.FINAL),  # > max after redrive
        # Permanent rows → the FINAL wording at any count (no retry will
        # happen — final text is the honest one).
        (NoticeTrigger.ASSEMBLE_INVALID, 1, NoticePhase.FINAL),
        (NoticeTrigger.LLM_401, 2, NoticePhase.FINAL),
        (NoticeTrigger.INVALID_KEY, 5, NoticePhase.FINAL),
        # No-rows → None at any count.
        (NoticeTrigger.GITHUB_401, 1, None),
        (NoticeTrigger.LIST_FORBIDDEN, 3, None),
        (NoticeTrigger.SUPERSEDED, 1, None),
        (NoticeTrigger.STALE, 2, None),
        (NoticeTrigger.NON_FAILURE, 1, None),
    ],
)
def test_notice_phase_matrix(trigger, receive_count, expected_phase):
    assert (
        notice_phase(
            trigger, receive_count=receive_count, max_receive_count=DEFAULT_MAX_RECEIVE_COUNT
        )
        is expected_phase
    )


def test_publish_phase_and_attempts_reach_the_content():
    """`publish_failure_notice` threads phase/attempts into the fixed
    template: retrying phase → retrying wording; final with attempts=3 →
    the real queue budget in the body."""
    table, github = FakeTable(), ScriptedGitHub()
    result = _publish(table, github, phase=NoticePhase.RETRYING)
    assert result.disposition == NoticeDisposition.PUBLISHED_TRUE
    assert EXPECTED_RETRYING_TEMPLATE.format(sha=SHA_B) in github.comments[0]["body"]

    table, github = FakeTable(), ScriptedGitHub()
    _publish(table, github, attempts=3)
    assert (
        EXPECTED_FINAL_TEMPLATE.format(sha=SHA_B, attempts="3 attempts")
        in github.comments[0]["body"]
    )


def test_no_notice_rows_skip():
    """Headline No-rows from the contract table, stated plainly: GitHub-401
    (writes would 401 too), lost access (publication impossible), invalid
    envelope (identifiers untrusted), and non-failures never publish."""
    assert should_publish_notice(NoticeTrigger.GITHUB_401, receive_count=5) is False
    assert should_publish_notice(NoticeTrigger.LIST_FORBIDDEN, receive_count=5) is False
    assert should_publish_notice(NoticeTrigger.INVALID_ENVELOPE, receive_count=1) is False
    assert should_publish_notice(NoticeTrigger.NON_FAILURE, receive_count=5) is False


# --- 003-T1: review-port signature conformance (header-free proof) -----------


def test_notice_path_runs_under_new_review_signature_without_header():
    """The D2 notice path runs through `run_review` under the 003-T1 review
    signature (the closure accepts the established pair, ignored) and the
    notice body carries NO review header (fixed system template)."""
    table, github = (
        FakeTable(),
        ScriptedGitHub(comments=[{"id": STORED_ID, "body": "old review " + MARKER}]),
    )
    _seed_active(table, head=SHA_B, gen=2, comment=STORED_ID)
    result = _publish(table, github)
    assert result.disposition == NoticeDisposition.PUBLISHED_TRUE
    body = github.comments[0]["body"]
    assert body.startswith(MARKER)
    assert "**Review #" not in body
    assert "updated" not in body

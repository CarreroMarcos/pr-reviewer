"""Worker Lambda entry point (HLD §2.3, §3.3, §3.4; FR-009–FR-016).

Review execution engine: SQS event (`batch_size = 1`) → per-record pipeline
in HLD §2.3 order — validate envelope (T006) → hydrate credentials (T014;
single 401 re-fetch plus one budgeted immediate retry on LLM timeout are
the ONLY in-request retries) → establish → diff (T031)
→ LLM (T032) → assemble + gate (T033) → claim → live-head fence → publish
(POST via creation lease when no `comment_id`, else PATCH) → finalize.
Superseded/stale/claim-held/finalize-conflict → discard (log, complete, no
publish, no retry).

Error mapping at the worker boundary (HLD §2.3 item 8 is the authority;
`is_retryable` is the executable form of that table for T034 scope):

* RETRYABLE → the error propagates for SQS redelivery (queue owns retries;
  DLQ after `maxReceiveCount 3`): transient diff transport (F4
  `transport_error`), GitHub/LLM 429 + 5xx, timeouts/connection failures,
  malformed provider responses (`invalid_response`, `bad_shape`/`bad_sha`
  — a garbled provider reply is treated as transient; the queue bounds it).
* NON-RETRYABLE → the record completes (log, alert downstream, never spin):
  invalid envelope, `ConfigError`, `AssembleError`, `bad_endpoint`, other
  LLM 4xx, auth after the single re-fetch, GitHub 403/404.
* Discard outcomes from `common.protocol` → log via `common.logs`,
  complete, no publish, no retry.

Publish shape (constitution IV — exactly one canonical comment): the
publish port re-reads the stored record; a stored `comment_id` → PATCH that
comment; absent → `common.reconcile` (list-first: adopt a surviving marker
comment when one exists, else creation-lease → POST → persist → re-check).
A re-publish of a row that still carries `comment_id` is therefore a PATCH
of the same comment, never a second POST. There is deliberately NO ACTIVE
same-owner short-circuit: sequential same-owner+SHA replay after a completed
run publishes again via PATCH (reopened PR ⇒ fresh review, possibly at the
same head SHA). A PATCH 404 (stored comment deleted or migrated) takes the
HLD §2.3 item-8 decision table through `common.reconcile`: marker found
elsewhere → adopt + reconcile; none found → lease → POST → persist →
re-check; list 403/404 or unparseable list → non-retryable complete;
transient list failure → raise for queue retry.

SQS at-least-once after success (wasted LLM + extra PATCH) is accepted for
US1: no new idempotency key is invented.

`glm_endpoint` is full-URL passthrough exactly as hydrated (HTTPS + host
allow-list enforced in T014 config) — never rewritten or re-derived.

Injection points (HLD §4.4 item 3 — parallel-safe, no module-global state
in tests): every collaborator arrives keyword-only (`_table`, `_ssm` /
`_config_provider`, `_now`, `_diff_transport`, `_llm_factory`,
`_github_transport`, `_sink`, `_system_prompt`, `_allowed_hosts`).
Production passes nothing and boto3 clients are built lazily. The
warm-container config cache (`_CONFIG_PROVIDER`) is used ONLY on the
uninjected production path; tests always inject. The SQS client cache
(`_SQS_CLIENT`, G6-F2) shares that posture: production reuses one client
per warm container, tests inject `_sqs` (or omit it — the queue default
then applies).

Ports (duck-typed — fakes mirror these exactly):

* table: protocol port — `get_item(pk) -> dict | None` (copy semantics),
  `update_item(Key=..., UpdateExpression=..., ConditionExpression=...,
  ExpressionAttributeNames=..., ExpressionAttributeValues=...)` raising
  `common.protocol.ConditionalCheckFailed`.
* ssm: `get_parameters(Names=..., WithDecryption=True)` (GetParameters shape).
* diff transport: `(url, headers) -> common.diff.HttpResponse`.
* llm factory: `(host, port, *, timeout)` (`http.client.HTTPSConnection` shape).
* github transport: `(method, url, headers, body) -> (status, body_bytes)`
  (transports MAY append response headers as an optional third element —
  `(status, body, headers)` with case-insensitive header names — so the
  worker can honor `Retry-After` without breaking two-tuple doubles; the
  diff seam (`common.diff.HttpResponse.headers`) already carries headers,
  while `common.llm` surfaces only `error_class`, so LLM/diff 429s raise
  without visibility adjustment).

Pure stdlib + boto3. No secrets in logs or error text (Constitution III).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import archive as archive_mod
from common import mutex as mutex_mod
from common.assemble import AssembleError, build_comment, render_diff_text, render_review_payload
from common.config import ConfigError, ConfigProvider, multi_agent_config, replay_base_url
from common.diff import DiffError, fetch_diff, fetch_pr_head_sha, post_image_lengths
from common.envelope import Envelope, EnvelopeError, validate_envelope
from common.events import (
    checkpoint,
    concurrency_single_pass,
    degraded_no_budget,
    degraded_to_single_pass,
    review_published,
    review_skipped,
    review_started,
)
from common.failure_notice import (
    NoticeDisposition,
    NoticeTrigger,
    notice_phase,
    publish_failure_notice,
)
from common.fanout import (
    FanoutDegraded,
    assemble_specialist_prompt,
    run_fanout,
    run_wave,
    single_pass_budget_ok,
)
from common.llm import LlmError, _read_timeout_s, review_diff
from common.logs import build_event, emit, prompt_sha256
from common.marker import build_marker
from common.protocol import ConditionalCheckFailed, OutcomeKind, run_review
from common.reconcile import (
    PER_PAGE,
    CommentNotFound,
    ReconcileError,
    reconcile,
    validate_page,
)
from common.residuals import load_accepted_residuals, residuals_for
from common.sanitize import sanitize
from common.state import build_clear_comment_expressions, expression_names, review_pk
from common.validate import CANARY_SUBSTRING, PROMPT_VERSION

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_USER_AGENT = "pr-reviewer/1.0"
GITHUB_TIMEOUT_SECONDS = 10

# In-executor LLM timeout-retry headroom (s): the single immediate retry
# needs the full read budget again plus room to publish/finalize before
# the Lambda deadline — otherwise the retry just moves the timeout closer
# to a hard freeze.
LLM_TIMEOUT_RETRY_HEADROOM_S = 90

# Warm-container config cache — production path only (see module docstring).
_CONFIG_PROVIDER: ConfigProvider | None = None

# Warm-container SQS client cache — production path only (same posture as
# `_CONFIG_PROVIDER` above: tests inject `_sqs` and never touch this).
# Parallel-safety: the worker is single-record per invocation (ESM
# `batch_size = 1`), and a boto3 client carries no per-record mutation, so
# one cached client is shared across warm invocations without cross-record
# state.
_SQS_CLIENT: Any = None

# Warm-container system-prompt cache — production path only (same posture as
# `_CONFIG_PROVIDER` above: tests inject `_system_prompt` and never touch
# this). The prompt ships deploy-static in worker.zip (zip-root `prompts/`),
# so a warm-cached copy is semantically identical to a fresh disk read.
_SYSTEM_PROMPT: str | None = None

Clock = Callable[[], float]
Sink = Callable[[str], None]


class GitHubError(Exception):
    """Typed GitHub failure: `status` is the HTTP status (`None` for
    transport failures or unparseable bodies); `error_class` is
    machine-readable (`http_{status}`, `transport_error`,
    `invalid_response`); `retry_after` carries a parsed `Retry-After`
    hint in seconds when the response supplied one (`None` otherwise —
    callers without header visibility leave it unset). Never carries
    token material."""

    def __init__(
        self, status: int | None, error_class: str, retry_after: int | None = None
    ) -> None:
        self.status = status
        self.error_class = error_class
        self.retry_after = retry_after
        super().__init__(f"github request failed: {error_class}")


def _normalize_number(key: str, value: Any) -> Any:
    """Normalize a DynamoDB Decimal to int; loud failure on fractions."""
    if not isinstance(value, Decimal):
        return value
    if value != value.to_integral_value():
        raise ValueError(f"non-integral Decimal for field {key!r}: {value!r}")
    return int(value)


class _BotoTable:
    """Thin boto3 wrapper exposing the `common.protocol` table port.

    Translates `ConditionalCheckFailedException` into
    `protocol.ConditionalCheckFailed` by duck-typing the error response —
    no `botocore` import (runtime is stdlib + boto3 only).
    """

    def __init__(self, table: Any) -> None:
        self._table = table

    def get_item(self, pk: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"pk": pk})
        item = response.get("Item")
        if item is None:
            return None
        # boto3 deserializes DynamoDB numbers as decimal.Decimal, but the
        # table-port contract (mirrored by the state-machine stub) is plain
        # ints: the publish port's `isinstance(comment_id, int)` gate and
        # the §5.4 event's json.dumps both fail on Decimal — surfaced live
        # by the T035 acceptance run (every ride POSTed a fresh comment
        # instead of PATCHing the stored one).
        return {key: _normalize_number(key, value) for key, value in item.items()}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        from common.protocol import ConditionalCheckFailed

        # Empty names must be OMITTED, never passed: the boto3 resource
        # layer rejects `{}` server-side ("must not be empty") and `None`
        # client-side (its condition-expression transformer calls
        # `.update()` on the value unconditionally — surfaced live by the
        # T035 acceptance run).
        if not kwargs.get("ExpressionAttributeNames"):
            kwargs.pop("ExpressionAttributeNames", None)

        try:
            return self._table.update_item(**kwargs)
        except Exception as exc:
            response = getattr(exc, "response", None)
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            if code == "ConditionalCheckFailedException":
                raise ConditionalCheckFailed(str(exc)) from exc
            raise

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        """Mutex-release passthrough (HLD-004 D9, T027): the same
        omit-if-empty discipline (names and values) and the same
        `ConditionalCheckFailed` translation as `update_item`, so the
        caller's exact alias maps (`_ACQUIRE_NAMES` / `_TOKEN_NAMES`
        semantics) ride through unmutated — no re-aliasing here."""
        from common.protocol import ConditionalCheckFailed

        if not kwargs.get("ExpressionAttributeNames"):
            kwargs.pop("ExpressionAttributeNames", None)
        if not kwargs.get("ExpressionAttributeValues"):
            kwargs.pop("ExpressionAttributeValues", None)

        try:
            return self._table.delete_item(**kwargs)
        except Exception as exc:
            response = getattr(exc, "response", None)
            code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
            if code == "ConditionalCheckFailedException":
                raise ConditionalCheckFailed(str(exc)) from exc
            raise


class _Credentials:
    """Single-refetch credential holder (HLD §2.3 item 1).

    All GitHub/LLM call sites for one record share a single instance, so
    the 401 cache-bust fires AT MOST ONCE per request — alongside the one
    budgeted LLM-timeout retry, the only in-request retries.
    `refresh_once()` invalidates + re-hydrates on first call (True);
    later calls return False and the caller treats the error as permanent.
    `ConfigError` from the re-fetch propagates to the worker boundary
    (non-retryable, complete).
    """

    def __init__(self, provider: ConfigProvider) -> None:
        self._provider = provider
        self._current = provider.get()
        self._refreshed = False

    def current(self) -> Any:
        return self._current

    def refresh_once(self) -> bool:
        if self._refreshed:
            return False
        self._refreshed = True
        self._provider.invalidate()
        self._current = self._provider.get()
        return True


def _diff_is_retryable(exc: DiffError) -> bool:
    """Diff side of the item-8 table: 401/403/404 and caller-shape faults
    complete; everything provider-transient raises for the queue."""
    if exc.reason == "http_error":
        return exc.status not in (401, 403, 404)
    if exc.reason in ("bad_repo", "bad_pr_number", "bad_page"):
        return False
    # transport_error, bad_shape, bad_sha: transient provider-side.
    return True


def _llm_is_retryable(exc: LlmError) -> bool:
    """LLM side of the item-8 table: timeout / 429 / 5xx / invalid output
    raise for queue retry; config/auth faults complete."""
    error_class = exc.error_class
    if error_class in ("bad_endpoint", "http_401"):
        return False
    if error_class == "invalid_key":
        # LLM request-construction fault (malformed credential/header
        # material): deterministic — retry cannot succeed. Stated as its
        # own arm (not folded into the tuple above) so the contract row
        # can never silently fall through to the retryable default below.
        return False
    if error_class.startswith("http_4") and error_class != "http_429":
        return False
    return True


def _github_is_retryable(exc: GitHubError) -> bool:
    """GitHub-write side: auth/lost-access completes; throttling, 5xx,
    transport and garbled replies raise for the queue. PATCH-404 recovery
    itself lives in `_make_publish` (decision table via `common.reconcile`);
    a 404 escaping publish (e.g. second-round loss) still completes."""
    return exc.status not in (401, 403, 404)


def is_retryable(exc: BaseException) -> bool:
    """Worker-boundary retry verdict (HLD §2.3 item 8, T034 scope).

    True → propagate for SQS redelivery (bounded by `maxReceiveCount 3`).
    False → complete (log, alert downstream, never spin). Unknown faults
    retry into the bounded queue budget rather than vanishing silently.
    """
    if isinstance(exc, AssembleError):
        return False
    if isinstance(exc, ConfigError):
        return False
    if isinstance(exc, DiffError):
        return _diff_is_retryable(exc)
    if isinstance(exc, LlmError):
        return _llm_is_retryable(exc)
    if isinstance(exc, GitHubError):
        return _github_is_retryable(exc)
    if isinstance(exc, ReconcileError):
        # Unparseable/unreadable listing or irreconcilable contention: the
        # decision table says complete (log, alert downstream, never spin).
        return False
    if isinstance(exc, FanoutDegraded):
        # T062 (Gate-14 Finding 1): terminal-row budget failure — the
        # closure already emitted its terminal event; the record boundary
        # logs `retry_queued` and raises for the bounded queue retry
        # (`maxReceiveCount 3`). A named case, never an anonymous
        # fall-through.
        return True
    return True


def _error_class(exc: BaseException) -> str:
    """Machine-readable error class for the fixed log field (HLD §5.4)."""
    if isinstance(exc, AssembleError):
        reasons = ".".join(exc.verdict.reasons)
        return f"assemble_{reasons}" if reasons else "assemble_refused"
    if isinstance(exc, ConfigError):
        return f"config_{exc.field}_{exc.reason}"
    if isinstance(exc, DiffError):
        suffix = f"_{exc.status}" if exc.status is not None else ""
        return f"diff_{exc.reason}{suffix}"
    if isinstance(exc, (LlmError, GitHubError)):
        return exc.error_class
    if isinstance(exc, ReconcileError):
        return f"reconcile_{exc.error_class}"
    if isinstance(exc, FanoutDegraded):
        return f"fanout_{exc.reason}_{exc.failed_stage}"
    return type(exc).__name__


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": GITHUB_USER_AGENT,
        "Authorization": f"Bearer {token}",
    }


# SQS `ChangeMessageVisibility` ceiling: 12 hours. A `Retry-After` beyond
# it (or garbage) is ignored — the queue default applies.
REPLAY_FOOTER_MARK = "Full agent replay"


def _with_replay_footer(content: str, *, pr_number: int, sha: str) -> str:
    """T075: deterministic replay-link footer on the published canonical
    body — code-side, never model-side (the synthesizer prompt and output
    contract are untouched). Omitted entirely when `REPLAY_BASE_URL` is
    unset; exactly-once under re-publish: a body already carrying the
    footer link is returned unchanged (constitution IV discipline extends to
    the footer — a PATCH of a re-composed body can never stack footers).
    The URL targets the unauth shell + bearer-gated artifacts (HLD §7);
    the base never appears as a committed literal (PR #167 canonical
    LOW posture) — it rides the worker env from the viewer Function URL
    resource (terraform contract pin, test_terraform_multi_agent.py)."""
    base = replay_base_url()
    # Idempotency anchors to the EXACT footer link for THIS run (r2): a
    # model-quoted phrase (r1 LOW) or a decoy link for a different run
    # (r2 LOW) cannot suppress the append — only a body already carrying
    # this run's footer can, which is precisely the re-publish case.
    footer_link = f"[{REPLAY_FOOTER_MARK}]({base}/runs/{pr_number}/{sha}/)"
    if not base or footer_link in content:
        return content
    return (
        content
        + f"\n\n---\n🔬 {footer_link}"
        + " — agent DAG, per-agent reasoning, and checkpoints for this review.\n"
    )


_MAX_VISIBILITY_TIMEOUT = 12 * 3600


def _unpack_transport_result(result: Any) -> tuple[int, bytes, dict[str, str]]:
    """Split a transport reply into (status, body, response headers).

    Transports return `(status, body)` or `(status, body, headers)`; the
    optional third element keeps every existing two-tuple double working
    while header-carrying doubles (and the production transport) expose
    `Retry-After`. Header names are normalized to lowercase.
    """
    status, raw = result[0], result[1]
    headers: dict[str, str] = {}
    if len(result) > 2 and isinstance(result[2], dict):
        headers = {str(key).lower(): value for key, value in result[2].items()}
    return status, raw, headers


def _parse_retry_after(headers: dict[str, str]) -> int | None:
    """Parse a `Retry-After` delay in seconds; `None` when absent or
    unusable (non-numeric, non-positive, or beyond the SQS ceiling)."""
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = int(str(raw).strip())
    except (ValueError, TypeError):
        return None
    if not 1 <= seconds <= _MAX_VISIBILITY_TIMEOUT:
        return None
    return seconds


def _default_github_transport(
    method: str, url: str, headers: dict[str, str], body: bytes
) -> tuple[int, bytes, dict[str, str]]:
    """Live Issues-Comments transport: non-2xx surfaces as status (the
    caller raises `GitHubError`); only transport failures raise here.
    Returns response headers as the third element so the worker can honor
    `Retry-After` (HLD §2.3 item 8 throttling row)."""
    request = Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310
    try:
        with urlopen(request, timeout=GITHUB_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.status, response.read(), dict(response.headers)
    except HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)
    except (TimeoutError, URLError, OSError) as exc:
        raise GitHubError(None, "transport_error") from exc


def _comments_url(repo_full_name: str, pr_number: int) -> str:
    return f"{GITHUB_API_BASE}/repos/{repo_full_name}/issues/{pr_number}/comments"


def _comment_url(repo_full_name: str, comment_id: int) -> str:
    return f"{GITHUB_API_BASE}/repos/{repo_full_name}/issues/comments/{comment_id}"


def _parse_comment_id(body: bytes) -> int:
    """Extract the GitHub int comment id; garbled replies are transient."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise GitHubError(None, "invalid_response") from exc
    if not isinstance(payload, dict):
        raise GitHubError(None, "invalid_response")
    comment_id = payload.get("id")
    if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id < 1:
        raise GitHubError(None, "invalid_response")
    return comment_id


def _github_write(
    github_transport: Callable[..., tuple[int, bytes]],
    method: str,
    url: str,
    token: str,
    content: str,
) -> int:
    """One Issues-Comments write → the comment id; non-2xx → `GitHubError`
    carrying a parsed `Retry-After` hint when the response supplied one."""
    body = json.dumps({"body": content}, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    status, raw, headers = _unpack_transport_result(
        github_transport(method, url, _github_headers(token), body)
    )
    if not 200 <= status <= 299:
        raise GitHubError(status, f"http_{status}", _parse_retry_after(headers))
    return _parse_comment_id(raw)


# Multi-agent fan-out wiring (HLD-004 §5, T023). Fixed specialty order —
# the wave, candidate IDs, and prompt composition all depend on it.
_FANOUT_SPECIALTIES = ("correctness", "security", "tests")

# `FanoutDegraded.failed_stage` values the closure degrades (Gate-13
# contract 1): the sequencer's budget gates plus its stage legs. Anything
# else re-raises loud — never mislabeled into an event (ADV-4).
_FANOUT_KNOWN_STAGES = frozenset({"wave", "verifier", "synthesizer"})

_FANOUT_PROMPT_FILES = {
    "correctness": "specialist_correctness.md",
    "security": "specialist_security.md",
    "tests": "specialist_tests.md",
    "verifier": "verifier.md",
    "synthesizer": "synthesizer.md",
}

_RESIDUALS_FILENAME = "accepted-residuals.md"

# Contender viability check (HLD-004 D9 contention path, T026): the
# contender may only spend what fits after the margin and the fixed
# pipeline reserve. Both numbers are PROVISIONAL pre-Phase-0 — the floor
# is the measured ~240s server-side queue latency (below it a socket
# budget cannot succeed), the reserve is HLD D9's ~90s
# claim/fence/publish/finalize/archive overhead. Phase 0 tunes both;
# deliberately module constants, NOT env knobs (the 12 HLD §8 checklist
# env vars are closed — these join only by spec amendment).
CONTENDER_READ_FLOOR_S = 240
CONTENDER_FIXED_OVERHEAD_S = 90


def _contender_read_timeout_s(remaining_ms: int | float | None, cfg: Any) -> int | None:
    """Contender viability check: clamp `remaining − BUDGET_MARGIN_S −
    ~90s` against the viability floor. Returns the clamped socket budget
    (seconds, computed ONCE — the caller forwards this same value as
    `read_timeout_s`) or `None` when below the floor or unreadable.

    OWN predicate, distinct from elapsed-budget gate 4 (which governs the
    holder's single-pass fallback): different math, no shared code path.

    Budget assumption: the fixed reserve covers pre-call work
    (prior-comment fetch, payload render) plus the post-release
    pipeline (claim/fence/publish/finalize). A pathological pre-call
    stall beyond the reserve terminates at the Lambda boundary and
    redelivers — the same class as any timeout-at-boundary, bounded by
    the queue retry budget.
    """
    if isinstance(remaining_ms, bool) or not isinstance(remaining_ms, (int, float)):
        return None
    clamped = int(remaining_ms // 1000) - cfg.budget_margin_s - CONTENDER_FIXED_OVERHEAD_S
    if clamped < CONTENDER_READ_FLOOR_S:
        return None
    return clamped


def _load_fanout_prompts() -> dict[str, str]:
    """Production fan-out template source: the five `prompts/*.md` files.

    Same two-layout resolution as `_load_system_prompt` (zip first, repo
    checkout second). A missing or undecodable file is a permanent config
    fault (complete, alert) — never an empty prompt. Tests inject
    `fanout_prompts` instead.
    """
    here = Path(__file__).resolve().parent
    prompts: dict[str, str] = {}
    for key, filename in _FANOUT_PROMPT_FILES.items():
        for candidate in (here / "prompts" / filename, here.parent / "prompts" / filename):
            try:
                prompts[key] = candidate.read_text(encoding="utf-8")
                break
            except (OSError, UnicodeDecodeError):
                continue
        else:
            raise ConfigError("fanout_prompt", "missing") from None
    return prompts


def _load_residuals_for(repo_full_name: str, pr_number: int) -> list[str]:
    """Accepted-residual lines for this PR (HLD D7; T010b parser).

    The committed `docs/accepted-residuals.md` ships in the Lambda bundle;
    Same two-layout resolution as the prompts. The first layout whose
    file EXISTS wins — its parse is AUTHORITATIVE even when empty (a
    successful parse with zero rows means zero residuals); fall through
    only on file-not-found. Nothing anywhere yields `[]` (T010b: missing
    → `[]`, malformed lines skipped inside the parser, never fatal).
    """
    here = Path(__file__).resolve().parent
    for candidate in (
        here / "docs" / _RESIDUALS_FILENAME,
        here.parent / "docs" / _RESIDUALS_FILENAME,
    ):
        if not candidate.is_file():
            continue
        return residuals_for(load_accepted_residuals(candidate), repo_full_name, pr_number)
    return []


def _read_remaining_ms(source: Callable[[], int] | None) -> int | None:
    """Fail-closed remaining-time read: missing/unreadable/non-numeric
    clocks yield `None`, and every budget gate treats `None` as
    exhausted (mirrors the `_call_llm` timeout-retry guard)."""
    if source is None:
        return None
    try:
        remaining = source()
    except Exception:  # noqa: BLE001 (unreadable clock → no budget)
        return None
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        return None
    return int(remaining)


def _run_phase0_shadow(
    *,
    diff_result: Any,
    ma_cfg: Any,
    api_key: str,
    model: str,
    endpoint: str,
    allowed_hosts: Any,
    run_id: str,
    events: list[dict[str, Any]],
    remaining_ms: int | None,
    residuals: list[str] | None = None,
    review_fn: Callable[..., Any] | None = None,
) -> bool:
    """Phase-0 shadow specialist (HLD §8 Phase 0, T036): ONE correctness
    specialist via the T015 wave primitive (pool at FANOUT_CONCURRENCY,
    `specialties=("correctness",)`), inline after publish.

    Budget gate FIRST: `remaining >= WAVE_WAIT_FOR_S + BUDGET_MARGIN_S`
    (else `False` — the caller appends `review_skipped
    {phase0_no_budget}`). `None` remaining fails closed to SKIP. Both
    skip paths warn (`shadow_no_budget` here, `shadow_no_diff` at the
    call site) so a skipped shadow is always loud.

    Returns True when the specialist ran. A `FanoutDegraded` outcome is
    EXPECTED even on a successful leg — one specialty can never satisfy
    the ≥2-survivor rule — so it is swallowed after logging: the wave's
    own `agent_*` events are the shadow telemetry, and the (already
    published) review never fails. The helper is a never-raises boundary
    past the budget gate (best-effort, mirroring the archive path): any
    other exception is warned and swallowed the same way. Snapshot creds
    only (the 401-refresh budget is never shared) and no table (the D9
    lease is released before the shadow runs — not lease-covered by
    construction).
    """
    budget_ms = (ma_cfg.wave_wait_for_s + ma_cfg.budget_margin_s) * 1000
    if remaining_ms is None or remaining_ms < budget_ms:
        logger.warning(
            "shadow_no_budget",
            extra={"status": "shadow_no_budget"},
        )
        return False
    # Witness scope: only emissions APPENDED by this leg count. The shared
    # list may already hold a correctness `agent_completed` from an
    # earlier partial fan-out (degraded path) — scanning the whole list
    # would mislog a produced-nothing shadow as `shadow_degraded`.
    events_len = len(events)
    try:
        prompts = _load_fanout_prompts()
        diff_text = render_diff_text(diff_result)
        system_prompts = {
            "correctness": assemble_specialist_prompt(
                template=prompts["correctness"], diff_text=diff_text, residuals=residuals
            )
        }
        run_wave(
            run_id=run_id,
            cfg=ma_cfg,
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            system_prompts=system_prompts,
            diff_text=diff_text,
            specialties=("correctness",),
            events=events,
            review_fn=review_fn,
            allowed_hosts=allowed_hosts,
        )
    except FanoutDegraded as exc:
        # Distinguish the expected single-specialty degrade (the
        # specialist ran; the ≥2-survivor rule is unmeetable by design)
        # from a leg that never produced: presence of a correctness
        # `agent_completed` event APPENDED BY THIS LEG is the witness
        # (see events_len above).
        degraded = any(
            isinstance(event, dict)
            and event.get("type") == "agent_completed"
            and event.get("specialty") == "correctness"
            for event in events[events_len:]
        )
        logger.warning(
            "shadow_degraded" if degraded else "shadow_failed",
            extra={
                "status": "shadow_degraded" if degraded else "shadow_failed",
                "error_class": _error_class(exc),
            },
        )
    except Exception as exc:  # noqa: BLE001 (never-raises boundary — see docstring)
        logger.warning(
            "shadow_failed",
            extra={"status": "shadow_failed", "error_class": _error_class(exc)},
        )
    return True


def _make_review(
    *,
    envelope: Envelope,
    creds: _Credentials,
    usage: dict[str, int],
    diff_transport: Any,
    llm_factory: Any,
    system_prompt: str,
    allowed_hosts: frozenset[str] | None = None,
    clock: Clock | None = None,
    github_transport: Callable[..., tuple[int, bytes]] | None = None,
    remaining_time_ms: Callable[[], int] | None = None,
    events: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    fanout_prompts: Mapping[str, str] | None = None,
    table: Any = None,
    shadow_stash: dict[str, Any] | None = None,
) -> Callable[[str, int], str]:
    """Protocol `review` port: diff → prior comment → LLM (single 401
    re-fetch, plus one immediate in-executor retry on `timeout` when the
    remaining Lambda budget covers it) → assemble + mandatory validate
    gate → publish-ready content (opaque string).

    `allowed_hosts` is the env-configured GLM host set (from the provider's
    `allowed_hosts` accessor); `None` keeps `review_diff`'s module default.
    `github_transport` feeds the best-effort prior-comment read; `None`
    (older callers) omits the prior section. `remaining_time_ms` is the
    Lambda `context.get_remaining_time_in_millis` callable threaded from
    the entry point; `None` (older callers, unit doubles) disables the
    timeout retry — today's queue-redelivery behavior, unchanged.

    Multi-agent (HLD-004 §5, T023): when `MULTI_AGENT=1`, the closure
    attempts `run_fanout` first and degrades to the existing single-pass
    inline path on `FanoutDegraded`. `events` is the caller-owned
    multi-agent event list (a fresh list per invocation when `None` —
    dropped on the floor unless the caller keeps it); `run_id` is the
    run's uuid4-hex id (generated per invocation when `None`);
    `fanout_prompts` carries the five production templates (tests inject
    sentinels; production loads `prompts/*.md` from disk).
    `table` is the state table for the D9 mutex row (HLD-004 T027):
    acquire after establish, release after the last lease-covered LLM
    call; `None` (older callers, unit doubles) skips the mutex entirely
    — today's single-pass behavior, unchanged.
    `shadow_stash` (HLD-004 §8 Phase 0, T036) is a caller-owned dict
    that receives the fetched `diff_result` under `"diff_result"` so the
    post-publish shadow can run without re-fetching; `None` (older
    callers) skips the stash."""

    repo = envelope.repo_full_name
    pr_number = envelope.pr_number

    def _fetch_diff() -> Any:
        token = creds.current().github_token
        try:
            return fetch_diff(repo, pr_number, github_token=token, _transport=diff_transport)
        except DiffError as exc:
            if exc.reason == "http_error" and exc.status == 401 and creds.refresh_once():
                return fetch_diff(
                    repo,
                    pr_number,
                    github_token=creds.current().github_token,
                    _transport=diff_transport,
                )
            raise

    def _call_llm(diff_text: str, read_timeout_s: int | None = None) -> Any:
        now = clock if clock is not None else time.time

        def _invoke(cfg: Any) -> Any:
            return review_diff(
                api_key=cfg.glm_api_key,
                model=cfg.glm_model,
                endpoint=cfg.glm_endpoint,
                system_prompt=system_prompt,
                diff_text=diff_text,
                read_timeout_s=read_timeout_s,
                allowed_hosts=allowed_hosts,
                _connection_factory=llm_factory,
            )

        def _timeout_retry_budgeted() -> bool:
            """One immediate retry needs the full read budget again plus
            publish/finalize headroom. An unknown budget (no
            remaining-time source — older callers, unit doubles) or an
            unreadable clock fails closed: no retry, exactly today's
            queue-redelivery behavior."""
            if remaining_time_ms is None:
                return False
            try:
                remaining = remaining_time_ms()
            except Exception:  # noqa: BLE001 (unreadable clock → no retry)
                return False
            if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
                return False
            return remaining >= (_read_timeout_s() + LLM_TIMEOUT_RETRY_HEADROOM_S) * 1000

        def _retry_after_timeout(exc: LlmError, attempt_ms: int) -> Any:
            if not _timeout_retry_budgeted():
                raise exc
            logger.warning(
                "llm_timeout_retry",
                extra={
                    "status": "llm_timeout_retry",
                    "repo_full_name": repo,
                    "pr_number": pr_number,
                    "delivery_guid": envelope.delivery_guid,
                    "error_class": exc.error_class,
                    "attempt": 2,
                    "backoff_ms": 0,
                    "duration_ms": attempt_ms,
                },
            )
            # Exactly one immediate re-invoke (no sleep): its errors —
            # including a second timeout — propagate exactly as today
            # (retry_queued redelivery at the boundary).
            return _invoke(creds.current())

        cfg = creds.current()
        attempt_start = now()
        try:
            return _invoke(cfg)
        except LlmError as exc:
            if exc.error_class == "http_401" and creds.refresh_once():
                cfg = creds.current()
                # Re-anchor the attempt clock: the timeout retry's
                # `duration_ms` must measure the attempt that timed out,
                # not the 401 round trip before it (Gate #88 advisory A1).
                attempt_start = now()
                try:
                    return _invoke(cfg)
                except LlmError as fresh_exc:
                    if fresh_exc.error_class != "timeout":
                        raise
                    # The single 401 budget is spent; the refreshed
                    # attempt's timeout may still take the one timeout
                    # retry below — never more than one retry per class.
                    exc = fresh_exc
            elif exc.error_class != "timeout":
                raise
            attempt_ms = max(0, int((now() - attempt_start) * 1000))
            return _retry_after_timeout(exc, attempt_ms)

    def _fetch_prior_comment() -> str | None:
        """Best-effort prior canonical comment body (003-T2), or None.

        One page-1 list read with the CURRENT token via a plain transport
        GET — NEVER `creds.refresh_once()` (the record's single 401 budget
        belongs to the essential diff/LLM/write path). Shape-validated via
        `common.reconcile.validate_page`; exact-marker substring scan,
        lowest matching id wins. Miss bound: comments beyond page 1 (>100)
        are not scanned. ANY failure (transport, 403/404, unparseable list)
        logs a coded warning and omits the section — prior context must
        never fail or delay a review.
        """
        if github_transport is None:
            return None
        url = f"{_comments_url(repo, pr_number)}?page=1&per_page={PER_PAGE}"
        try:
            token = creds.current().github_token
            status, raw, _headers = _unpack_transport_result(
                github_transport("GET", url, _github_headers(token), b"")
            )
            if not 200 <= status <= 299:
                raise GitHubError(status, f"http_{status}")
            try:
                page = json.loads(raw)
            except ValueError:
                raise GitHubError(None, "invalid_response") from None
            comments = validate_page(page)
        except Exception as exc:
            logger.warning(
                "prior_comment_unavailable",
                extra={"status": "prior_omitted", "error_class": _error_class(exc)},
            )
            return None
        marker = build_marker(repo, pr_number)
        matches = [c for c in comments if marker in c["body"]]
        if not matches:
            return None
        return min(matches, key=lambda c: c["id"])["body"]

    def review(head_sha: str, generation: int) -> str:
        # One run, one event stream: closure-owned stages (review_started,
        # checkpoints, degraded_*) and the sequencer's stage-owned events
        # share the caller's list (fresh per invocation when unprovided).
        # Invocation proves establishment — run_review calls review() only
        # after _establish succeeds.
        rid = run_id or uuid.uuid4().hex
        evts = events if events is not None else []
        now = clock if clock is not None else time.time
        evts.append(checkpoint(stage="established", run_id=rid))
        ma_cfg = multi_agent_config()
        # D9 mutex (T027): acquire after validate/hydrate/establish,
        # before any Z.AI call. The row lives in the state table; a
        # failed acquire makes this record the contender (single-pass
        # inline or budget SKIP below). No table → no mutex (older
        # callers behave exactly as today).
        lease: Any = None
        t_acquired: float | None = None
        contender = False
        if table is not None:
            t_acquired = now()
            lease = mutex_mod.acquire(
                table,
                owner=envelope.delivery_guid,
                now=t_acquired,
                ttl_s=ma_cfg.mutex_lease_ttl_s,
            )
            contender = lease is None
        diff_result = _fetch_diff()
        if shadow_stash is not None:
            shadow_stash["diff_result"] = diff_result
        evts.append(
            review_started(
                pr=pr_number,
                sha=head_sha,
                diff_stats={
                    "files": len(diff_result.files),
                    "additions": diff_result.total_additions,
                    "deletions": diff_result.total_deletions,
                },
                run_id=rid,
            )
        )
        evts.append(checkpoint(stage="diff_fetched", run_id=rid))
        if contender:
            content = _contender_content(
                diff_result, head_sha, generation, ma_cfg, rid, evts, now, t_acquired
            )
            return content
        if ma_cfg.multi_agent == 1:
            content = _fanout_content(
                diff_result, head_sha, generation, ma_cfg, rid, evts, now, lease, t_acquired
            )
        else:
            content = _single_pass_inline(diff_result, head_sha, generation)
        # Release AFTER the last lease-covered LLM call, BEFORE
        # claim/fence/publish/finalize (all run after review() returns).
        # A lost race (False) is ignored — the owner-guard converges.
        # An exception above skips release BY DESIGN (HLD D9): a failed
        # or missing release is the crash class — expiry takeover
        # recovers it, degradation is bounded at one TTL, and the
        # contender path keeps publishing single-pass reviews
        # throughout (a redelivered copy starts a fresh invocation
        # budget, so it is viable, not SKIP-bound).
        if lease is not None:
            mutex_mod.release(table, lease=lease)
        return content

    def _contender_content(
        diff_result: Any,
        head_sha: str,
        generation: int,
        ma_cfg: Any,
        rid: str,
        evts: list[dict[str, Any]],
        now: Clock,
        t_acquired: float | None,
    ) -> str:
        """Contender path (HLD D9): the mutex is held, so this record does
        NOT defer and does NOT touch visibility — it runs single-pass
        inline and completes normally. The viability check is its OWN
        predicate: below the floor the call is SKIPPED (`transient`
        `LlmError("timeout")` for the existing re-raise/notice path and
        queue redelivery; the class is a deliberate conflation — the
        event vocabulary is spec-pinned with no SKIP type, and
        `timeout` rides the retryable default with no invented
        mapping, ADV-4, while `reason` carries the true disposition
        for consumers); at/above, one single-pass with the clamped
        value forwarded as `read_timeout_s`. The contender holds no lease
        — nothing to release, and publication runs unprotected (owner
        guard converges). Emits only its own `concurrency_single_pass`
        event on BOTH outcomes (never `degraded_to_single_pass` —
        fan-out was never attempted); the event records the contention
        disposition, not a completed pass.
        """
        clamped = _contender_read_timeout_s(_safe_remaining_ms(), ma_cfg)
        elapsed_ms = max(0, int((now() - t_acquired) * 1000)) if t_acquired is not None else 0
        # Metric-filter signal (T048b, Mars 2026-09-28): the envelope event
        # never reaches logs, so a line led by the bare term is logged for
        # the worker_contention filter to count (both contention outcomes);
        # lock key + elapsed ride along for alarm triage.
        logger.info(
            "concurrency_single_pass mutex=%s elapsed_ms=%s",
            mutex_mod.MUTEX_PK,
            elapsed_ms,
        )
        if clamped is None:
            evts.append(
                concurrency_single_pass(
                    reason="mutex_held_no_budget", elapsed_ms=elapsed_ms, run_id=rid
                )
            )
            raise LlmError("timeout")
        evts.append(concurrency_single_pass(reason="mutex_held", elapsed_ms=elapsed_ms, run_id=rid))
        return _single_pass_inline(diff_result, head_sha, generation, read_timeout_s=clamped)

    def _single_pass_inline(
        diff_result: Any,
        head_sha: str,
        generation: int,
        read_timeout_s: int | None = None,
    ) -> str:
        """The existing single-pass inline path (prior comment → payload →
        LLM → assemble + validate gate → publish-ready content), byte for
        byte as before. `read_timeout_s` is the contender's clamped
        forward (T026); `None` keeps today's `_read_timeout_s()`
        resolution for every existing caller."""
        prior_comment = _fetch_prior_comment()
        payload = render_review_payload(
            title=diff_result.title,
            body=diff_result.body,
            diff_text=render_diff_text(diff_result),
            prior_comment=prior_comment,
        )
        result = _call_llm(payload, read_timeout_s=read_timeout_s)
        usage["tokens"] = result.total_tokens
        comment = build_comment(
            repo_full_name=repo,
            pr_number=pr_number,
            review_content=result.content,
            truncated=diff_result.truncated,
            review_number=generation + 1,
            now=(clock if clock is not None else time.time)(),
        )
        return comment.content

    def _fanout_content(
        diff_result: Any,
        head_sha: str,
        generation: int,
        ma_cfg: Any,
        rid: str,
        evts: list[dict[str, Any]],
        now: Clock,
        lease: Any,
        t_acquired: float | None,
    ) -> str:
        """Fan-out attempt with single-pass fallback (HLD §5 wiring pin).

        `run_fanout` runs the full pipeline and returns the synthesizer
        comment body (wrapped below by the EXISTING assemble + validate
        gate, unchanged). Its `FanoutDegraded` is caught HERE ONLY: the
        terminal `degraded_to_single_pass` event is emitted (the sequencer
        emits none of its own), the failure reason/stage propagate
        unchanged onto the event, then the D9 pre-fallback budget gate
        decides — pass: the existing single-pass inline path runs and its
        content returns; fail: `degraded_no_budget` is emitted and the
        `FanoutDegraded` re-raises (terminal for this attempt — the queue
        owns the retry with a fresh budget on redelivery). Past 50% TTL a
        lease refresh precedes any fallback LLM work; a lost refresh
        re-raises with no fallback and no re-assert.

        An unknown `failed_stage` re-raises immediately: it is never
        mislabeled into an event and no fallback is attempted (ADV-4).
        """
        t0 = now()
        cfg_snapshot = creds.current()
        prompts = fanout_prompts if fanout_prompts is not None else _load_fanout_prompts()
        context = (
            SimpleNamespace(get_remaining_time_in_millis=remaining_time_ms)
            if remaining_time_ms is not None
            else None
        )
        try:
            fanout_body = run_fanout(
                diff_result,
                _load_residuals_for(repo, pr_number),
                ma_cfg,
                context,
                run_id=rid,
                api_key=cfg_snapshot.glm_api_key,
                model=cfg_snapshot.glm_model,
                endpoint=cfg_snapshot.glm_endpoint,
                events=evts,
                specialist_templates={
                    specialty: prompts[specialty] for specialty in _FANOUT_SPECIALTIES
                },
                verifier_template=prompts["verifier"],
                synth_template=prompts["synthesizer"],
                # Post-image lengths are hunk-derived from the diff
                # envelope (`post_image_lengths`): the bound is the last
                # post-image line visible in the reviewed diff, not the full
                # file length — findings beyond it are unanchored by
                # construction and clamping+flagging them is the designed
                # honesty (coordinates_clamped_n already surfaces on
                # agent_completed per spec-HLD :535-537). Files with no
                # computable span are omitted — the clamp passes absent
                # files through untouched by design, so omission is safe.
                # The mapping itself is always real — never None.
                file_lengths=post_image_lengths(diff_result),
                allowed_hosts=allowed_hosts,
            )
        except FanoutDegraded as exc:
            if exc.failed_stage not in _FANOUT_KNOWN_STAGES:
                raise
            evts.append(
                degraded_to_single_pass(
                    reason=exc.reason, failed_stage=exc.failed_stage, run_id=rid
                )
            )
            # Gate-15(c): 50%-TTL refresh scheduling. Past half the lease
            # the original row may not cover the fallback call, so refresh
            # before issuing more LLM work. A lost refresh STOPS all LLM
            # work: re-raise without fallback and never re-assert (the
            # raise path holds no valid lease and calls acquire nowhere).
            # Below half-TTL the original lease still covers the fallback.
            if (
                lease is not None
                and t_acquired is not None
                and (now() - t_acquired) >= ma_cfg.mutex_lease_ttl_s / 2
            ):
                if (
                    mutex_mod.refresh(table, lease=lease, now=now(), ttl_s=ma_cfg.mutex_lease_ttl_s)
                    is None
                ):
                    raise
            remaining = _safe_remaining_ms()
            if remaining is None or not single_pass_budget_ok(remaining, ma_cfg):
                elapsed_ms = max(0, int((now() - t0) * 1000))
                evts.append(
                    degraded_no_budget(
                        reason="insufficient_budget", elapsed_ms=elapsed_ms, run_id=rid
                    )
                )
                raise
            return _single_pass_inline(diff_result, head_sha, generation)
        comment = build_comment(
            repo_full_name=repo,
            pr_number=pr_number,
            review_content=fanout_body,
            truncated=diff_result.truncated,
            review_number=generation + 1,
            now=(clock if clock is not None else time.time)(),
        )
        return comment.content

    def _safe_remaining_ms() -> int | None:
        """Fail-closed remaining-time read (mirrors the `_call_llm`
        timeout-retry guard): missing/unreadable/non-numeric clocks yield
        `None`, and the pre-fallback gate treats `None` as exhausted."""
        return _read_remaining_ms(remaining_time_ms)

    return review


def _make_fence(
    *, envelope: Envelope, creds: _Credentials, diff_transport: Any
) -> Callable[[], str]:
    """Protocol `fence` port: live-head fetch with the shared single 401
    re-fetch; every other `DiffError` propagates to the boundary."""

    repo = envelope.repo_full_name
    pr_number = envelope.pr_number

    def fence() -> str:
        token = creds.current().github_token
        try:
            return fetch_pr_head_sha(repo, pr_number, github_token=token, _transport=diff_transport)
        except DiffError as exc:
            if exc.reason == "http_error" and exc.status == 401 and creds.refresh_once():
                return fetch_pr_head_sha(
                    repo,
                    pr_number,
                    github_token=creds.current().github_token,
                    _transport=diff_transport,
                )
            raise

    return fence


def _comment_write(
    *,
    creds: _Credentials,
    github_transport: Callable[..., tuple[int, bytes]],
    method: str,
    url: str,
    content: str,
) -> int:
    """One Issues-Comments write with the shared single-401 budget: the
    first 401 invalidates the credential cache and retries once; a second
    401 (or any other fault) propagates to the boundary classifier."""
    try:
        token = creds.current().github_token
        return _github_write(github_transport, method, url, token, content)
    except GitHubError as exc:
        if exc.status == 401 and creds.refresh_once():
            return _github_write(
                github_transport, method, url, creds.current().github_token, content
            )
        raise


def _comment_read(
    *,
    creds: _Credentials,
    github_transport: Callable[..., tuple[int, bytes]],
    url: str,
) -> bytes:
    """One GET with the shared single-401 budget; non-2xx → GitHubError
    for the boundary classifier (403/404 complete, 429/5xx retry),
    carrying a parsed `Retry-After` hint when supplied."""
    token = creds.current().github_token
    status, raw, headers = _unpack_transport_result(
        github_transport("GET", url, _github_headers(token), b"")
    )
    if status == 401 and creds.refresh_once():
        status, raw, headers = _unpack_transport_result(
            github_transport("GET", url, _github_headers(creds.current().github_token), b"")
        )
    if not 200 <= status <= 299:
        raise GitHubError(status, f"http_{status}", _parse_retry_after(headers))
    return raw


def _comment_delete(
    *,
    creds: _Credentials,
    github_transport: Callable[..., tuple[int, bytes]],
    repo_full_name: str,
    comment_id: int,
) -> None:
    """One DELETE with the shared single-401 budget. DELETE has no
    id-bearing reply (204 + empty body), so it bypasses `_github_write`'s
    comment-id parse; only the status is classified."""
    payload = json.dumps({"body": ""}, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    url = _comment_url(repo_full_name, comment_id)
    token = creds.current().github_token
    status, _, headers = _unpack_transport_result(
        github_transport("DELETE", url, _github_headers(token), payload)
    )
    if status == 401 and creds.refresh_once():
        status, _, headers = _unpack_transport_result(
            github_transport("DELETE", url, _github_headers(creds.current().github_token), payload)
        )
    if status == 404:
        return  # already gone is converged
    if not 200 <= status <= 299:
        raise GitHubError(status, f"http_{status}", _parse_retry_after(headers))


def _github_comment_ports(
    *,
    repo_full_name: str,
    pr_number: int,
    creds: _Credentials,
    github_transport: Callable[..., tuple[int, bytes]],
) -> tuple[Any, Any, Any, Any]:
    """Comment CRUD ports shared by the review publish path (`_make_publish`)
    and the D2 notice path (`_attempt_notice`), as
    `(list_page, create_comment, update_comment, delete_comment)`.

    Single implementation of the single-401 budget, the 404 translations,
    and the list-shape fallback: the former `_make_publish` closures and
    `_notice_comment_ports` were line-identical duplicates, unified here
    once Gates + US5 live acceptance proved the notice path in review.
    Behavior is unchanged on both paths.
    """

    def list_page(page: int) -> Any:
        raw = _comment_read(
            creds=creds,
            github_transport=github_transport,
            url=f"{_comments_url(repo_full_name, pr_number)}?page={page}&per_page={PER_PAGE}",
        )
        try:
            return json.loads(raw)
        except ValueError:
            # Not JSON at all: return raw so reconcile shape-validation
            # maps it to list-unreadable (non-retryable), never a retry.
            return raw

    def create_comment(body: str) -> int:
        return _comment_write(
            creds=creds,
            github_transport=github_transport,
            method="POST",
            url=_comments_url(repo_full_name, pr_number),
            content=body,
        )

    def update_comment(comment_id: int, body: str) -> None:
        try:
            _comment_write(
                creds=creds,
                github_transport=github_transport,
                method="PATCH",
                url=_comment_url(repo_full_name, comment_id),
                content=body,
            )
        except GitHubError as exc:
            if exc.status == 404:
                raise CommentNotFound(comment_id) from exc
            raise

    def delete_comment(comment_id: int) -> None:
        _comment_delete(
            creds=creds,
            github_transport=github_transport,
            repo_full_name=repo_full_name,
            comment_id=comment_id,
        )

    return list_page, create_comment, update_comment, delete_comment


def _make_publish(
    *,
    envelope: Envelope,
    creds: _Credentials,
    table: Any,
    pk: str,
    owner: str,
    clock: Clock,
    github_transport: Callable[..., tuple[int, bytes]],
) -> Callable[[str], int]:
    """Protocol `publish` port (constitution IV — exactly one canonical
    comment; HLD §2.3 item 8 + §3.4): consults the STORED `comment_id` —
    present → PATCH that comment; a PATCH 404 (deleted/migrated) falls
    through to `common.reconcile`; absent → reconcile directly (list-first:
    a surviving marker comment from a crashed predecessor is adopted, not
    duplicated). `comment_id` lands on the record only via finalize, so a
    same-revision re-publish is a PATCH, never a second POST."""

    repo = envelope.repo_full_name
    pr_number = envelope.pr_number

    _list_page, _create, _update, _delete = _github_comment_ports(
        repo_full_name=repo,
        pr_number=pr_number,
        creds=creds,
        github_transport=github_transport,
    )

    def _write(method: str, url: str, content: str) -> int:
        # PATCH fast-path when a stored `comment_id` exists: the raw write
        # (a 404 here means recovery, not CommentNotFound) — same shared
        # single-401 budget as the ports above.
        return _comment_write(
            creds=creds,
            github_transport=github_transport,
            method=method,
            url=url,
            content=content,
        )

    def _recover(content: str, dead_comment_id: int | None) -> int:
        if dead_comment_id is not None:
            _clear_dead_id(dead_comment_id)
        return reconcile(
            repo_full_name=repo,
            pr_number=pr_number,
            pk=pk,
            owner=owner,
            content=content,
            table=table,
            now=lambda: int(clock()),
            list_page=_list_page,
            create_comment=_create,
            update_comment=_update,
            delete_comment=_delete,
        )

    def _clear_dead_id(dead_comment_id: int) -> None:
        """REMOVE a stored id the PATCH just proved dead, guarded on the
        exact id at our revision. A concurrent move (newer revision owns
        the record now) aborts recovery — the newer run converges."""
        item = table.get_item(pk) or {}
        head_sha = item.get("head_sha")
        generation = item.get("generation")
        if (
            not isinstance(head_sha, str)
            or not isinstance(generation, int)
            or isinstance(generation, bool)
        ):
            raise GitHubError(404, "http_404")
        update, condition, values = build_clear_comment_expressions(
            head_sha=head_sha,
            generation=generation,
            dead_comment_id=dead_comment_id,
        )
        try:
            table.update_item(
                Key={"pk": pk},
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeNames=expression_names(update, condition),
                ExpressionAttributeValues=values,
            )
        except ConditionalCheckFailed:
            raise GitHubError(404, "http_404") from None

    def publish(content: str) -> int:
        # HLD-004 §5 item 5: the full sanitizer (structure phases +
        # payload-token redaction) runs at publish, so the PATCH/POST
        # body is the shipped text — never the raw model content.
        # Ordering note: canary DETECTION lives upstream — every model
        # content path passes build_comment → validate_comment, which
        # rejects canary_leaked BEFORE publish — so this redaction is
        # defense-in-depth, never the canary's silencer. Idempotence:
        # [redacted:*] placeholders carry no @ and no ≥16-hex run, so
        # re-publish paths (reconcile) are no-ops.
        if CANARY_SUBSTRING in content:
            # Gate-54 Finding 1: the canary reaching publish means the
            # upstream build_comment → validate_comment gate was
            # bypassed — warn so defense-in-depth firing is observable.
            logger.warning("canary_leaked_at_publish: upstream gate bypassed; redacting")
        content = sanitize(content)
        # T075: the footer rides AFTER the sanitizer — it is code-side
        # decoration (the sha is public PR metadata, not model output);
        # inside the sanitizer the 40-hex sha reads as a payload token
        # (PR #169 r2 find).
        content = _with_replay_footer(content, pr_number=pr_number, sha=envelope.head_sha)
        item = table.get_item(pk) or {}
        comment_id = item.get("comment_id")
        if isinstance(comment_id, bool):
            comment_id = None
        if isinstance(comment_id, int) and comment_id >= 1:
            try:
                return _write("PATCH", _comment_url(repo, comment_id), content)
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                # Stored comment deleted/migrated: clear the proven-dead id
                # (so the creation lease is acquirable), then the item-8
                # decision table via reconcile.
                return _recover(content, comment_id)
        return _recover(content, None)

    return publish


_OUTCOME_STATUS = {
    OutcomeKind.PUBLISHED: "published",
    OutcomeKind.PUBLISHED_FINALIZE_CONFLICT: "published_finalize_conflict",
    OutcomeKind.DISCARDED_SUPERSEDED: "discarded_superseded",
    OutcomeKind.DISCARDED_STALE: "discarded_stale",
    OutcomeKind.DISCARDED_CLAIM_HELD: "discarded_claim_held",
}

# Outcomes that discarded superseded/stale work via fencing (HLD §4.3
# `stale_discarded` metric). Claim-held is excluded: another owner holds a
# live lease, but the event itself was not proven stale.
_STALE_DISCARDED_KINDS = frozenset({OutcomeKind.DISCARDED_SUPERSEDED, OutcomeKind.DISCARDED_STALE})

# SQS redrive budget mirror (HLD §2.2, terraform/messaging.tf
# `maxReceiveCount = 3`): the first attempt publishes the retrying
# notice, the final attempt the final notice. Kept in lockstep with
# terraform by tests/contracts/test_terraform_contract.py — change both
# together.
_MAX_RECEIVE_COUNT = 3


def _record_delivery_context(record: Any) -> tuple[str | None, int]:
    """Extract the SQS delivery identity: receipt handle (for visibility
    calls) and `ApproximateReceiveCount` (wire string) for final-attempt
    detection. A missing/unparseable count defaults to 1 (non-final — a
    later redelivery re-evaluates rather than publishing early)."""
    if not isinstance(record, dict):
        return None, 1
    receipt = record.get("receiptHandle")
    receipt_handle = receipt if isinstance(receipt, str) and receipt else None
    attributes = record.get("attributes")
    raw_count = attributes.get("ApproximateReceiveCount") if isinstance(attributes, dict) else None
    try:
        receive_count = int(str(raw_count).strip())
    except (ValueError, TypeError, AttributeError):
        return receipt_handle, 1
    return receipt_handle, receive_count if receive_count >= 1 else 1


def _extend_visibility(
    sqs: Any, queue_url: str, receipt_handle: str | None, seconds: int | None
) -> None:
    """Best-effort `ChangeMessageVisibility` per `Retry-After` (HLD §2.3
    item 8 throttling row, §2.2). Skips silently without URL, handle, or
    hint; a failed call warns and the original error still raises — the
    visibility edge never masks the record disposition."""
    if sqs is None or not queue_url or not receipt_handle or seconds is None:
        return
    try:
        sqs.change_message_visibility(
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=seconds,
        )
    except Exception:
        logger.warning("visibility_extend_failed", extra={"status": "visibility_failed"})


def _notice_trigger(exc: BaseException) -> NoticeTrigger | None:
    """Map a terminal error to its D2 trigger-table row; `None` means no
    contract row authorizes a notice (the worker completes/raises without
    one). Unlisted permanent faults (other LLM 4xx, config faults, caller
    shape faults, reconcile faults) skip: only listed Yes-rows publish."""
    if isinstance(exc, AssembleError):
        return NoticeTrigger.ASSEMBLE_INVALID
    if isinstance(exc, LlmError):
        if exc.error_class == "http_401":
            return NoticeTrigger.LLM_401
        if exc.error_class == "invalid_key":
            return NoticeTrigger.INVALID_KEY
        if exc.error_class == "invalid_response":
            return NoticeTrigger.LLM_UNUSABLE
        if exc.error_class in (
            "timeout",
            "connection_error",
            "http_429",
        ) or exc.error_class.startswith("http_5"):
            return NoticeTrigger.TRANSIENT
        return None
    if isinstance(exc, DiffError):
        if exc.reason == "http_error":
            if exc.status == 401:
                # GitHub unreachable: comment writes would 401 too.
                return NoticeTrigger.GITHUB_401
            if exc.status in (403, 404):
                return NoticeTrigger.LIST_FORBIDDEN
            return NoticeTrigger.TRANSIENT
        if exc.reason == "transport_error":
            return NoticeTrigger.TRANSIENT
        return None
    if isinstance(exc, GitHubError):
        if exc.status == 401:
            return NoticeTrigger.GITHUB_401
        if exc.status == 404:
            # Dead-id-clear loss: a newer revision owns the record now.
            return NoticeTrigger.STALE
        if exc.status == 403:
            return NoticeTrigger.LIST_FORBIDDEN
        return NoticeTrigger.TRANSIENT
    if isinstance(exc, FanoutDegraded):
        # T062 (Gate-14 Finding 1): terminal-row budget failure is a
        # retryable queue-retried fault — RETRYING notice on the first
        # delivery, FINAL at the last receive; idempotency per redelivery
        # rides the existing phase mechanism, not a new path.
        return NoticeTrigger.TRANSIENT
    return None


def _attempt_notice(
    *,
    exc: BaseException,
    envelope: Envelope,
    receive_count: int,
    table: Any,
    creds: _Credentials | None,
    clock: Clock,
    diff_transport: Any,
    github_transport: Callable[..., tuple[int, bytes]],
) -> str:
    """D2 trigger → best-effort notice publish → disposition value.

    Returns `"false"` unless the trigger row authorizes AND the notice
    landed (`"true"`) or the revision proved stale (`"skipped-stale"`).
    First transient failure publishes the RETRYING notice; the final
    attempt (and permanent rows) publish the FINAL notice, replacing the
    first in place via the shared-marker PATCH path. Never raises:
    notice failure must not mask the alert/DLQ flow (the module is
    best-effort internally; this wrapper additionally guards port
    construction and the trigger comparison).
    """
    trigger = _notice_trigger(exc)
    phase = (
        notice_phase(trigger, receive_count=receive_count, max_receive_count=_MAX_RECEIVE_COUNT)
        if trigger is not None
        else None
    )
    if creds is None or phase is None:
        return NoticeDisposition.PUBLISHED_FALSE.value
    # Final-template attempts count: the queue's own budget for
    # transient exhaustion, CLAMPED — a redriven message can arrive with
    # a receive count above the budget, and the notice must not claim
    # attempts that never happened in this configuration (Gate #89
    # Finding 6); exactly one for permanent rows.
    attempts = (
        min(receive_count, _MAX_RECEIVE_COUNT)
        if trigger in (NoticeTrigger.TRANSIENT, NoticeTrigger.LLM_UNUSABLE)
        else 1
    )
    try:
        list_page, create_comment, update_comment, delete_comment = _github_comment_ports(
            repo_full_name=envelope.repo_full_name,
            pr_number=envelope.pr_number,
            creds=creds,
            github_transport=github_transport,
        )
        result = publish_failure_notice(
            repo_full_name=envelope.repo_full_name,
            pr_number=envelope.pr_number,
            head_sha=envelope.head_sha,
            owner=envelope.delivery_guid,
            table=table,
            now=lambda: int(clock()),
            fence=_make_fence(envelope=envelope, creds=creds, diff_transport=diff_transport),
            list_page=list_page,
            create_comment=create_comment,
            update_comment=update_comment,
            delete_comment=delete_comment,
            phase=phase,
            attempts=attempts,
        )
    except Exception:
        logger.warning("failure_notice_failed", extra={"status": "notice_failed"})
        return NoticeDisposition.PUBLISHED_FALSE.value
    return result.disposition.value


def _emit(
    sink: Sink,
    *,
    envelope: Envelope,
    duration_ms: int,
    token_usage: int,
    status: str,
    error_class: str | None,
    generation: int | None,
    stale_discarded: bool = False,
    failure_notice_published: str = "false",
    prompt_sha256: str | None = None,
) -> None:
    """Best-effort structured log (HLD §5.4): emission never masks the
    record disposition — a logging fault is a plain warning, not a retry."""
    try:
        emit(
            sink,
            build_event(
                repo_full_name=envelope.repo_full_name,
                pr_number=envelope.pr_number,
                head_sha=envelope.head_sha,
                delivery_guid=envelope.delivery_guid,
                duration_ms=duration_ms,
                token_usage=token_usage,
                status=status,
                error_class=error_class,
                generation=generation,
                stale_discarded=stale_discarded,
                failure_notice_published=failure_notice_published,
                prompt_version=PROMPT_VERSION,
                prompt_sha256=prompt_sha256,
            ),
        )
    except Exception:  # observability must not mask disposition
        logger.warning("worker_emit_failed", extra={"status": "emit_failed"})


def _process_record(
    record: Any,
    *,
    table: Any,
    provider: ConfigProvider,
    clock: Clock,
    diff_transport: Any,
    llm_factory: Any,
    github_transport: Callable[..., tuple[int, bytes]],
    sink: Sink,
    system_prompt: str,
    sqs: Any = None,
    queue_url: str = "",
    remaining_time_ms: Callable[[], int] | None = None,
    events: list[dict[str, Any]] | None = None,
    s3: Any = None,
    archive_bucket: str = "",
    shadow_review_fn: Callable[..., Any] | None = None,
) -> str:
    """Run one SQS record through the pipeline.

    Returns the completion status. Raises the original error ONLY for
    retryable faults (queue redelivery); every other path completes.
    `sqs`/`queue_url` drive the Retry-After visibility edge and are
    optional (absent in older tests) — without them the queue default
    applies and classification is unchanged. `remaining_time_ms` (Lambda
    `context.get_remaining_time_in_millis`, threaded from `handler`)
    gates the single in-executor LLM timeout retry; `None` (older
    callers) disables it.
    """
    raw_body = record.get("body") if isinstance(record, dict) else None
    try:
        payload = json.loads(raw_body) if isinstance(raw_body, str) else None
    except (ValueError, UnicodeDecodeError):
        payload = None
    try:
        envelope = validate_envelope(payload)
    except EnvelopeError as exc:
        # No trustworthy IDs exist, so the fixed-field event cannot be
        # built — plain coded warning, no payload content (HLD §5.4).
        logger.warning(
            "worker_record",
            extra={"status": "invalid_envelope", "error_class": f"{exc.field}_{exc.reason}"},
        )
        return "discarded_invalid_envelope"

    started = clock()
    usage = {"tokens": 0}
    # Prompt-hash telemetry (SPR-62): sha256 of the DELIVERED prompt text
    # exactly as passed to the model — hash only, never prompt content.
    # PROMPT_VERSION keeps describing the code (validate.py).
    delivered_prompt_sha256 = prompt_sha256(system_prompt)
    creds = None
    # One run, one event stream (HLD-004 §6, T023): the closure and the
    # stage wrappers below share this per-record list. The caller may keep
    # it (tests, and the T029 archive writer later); otherwise it drops.
    record_events = events if events is not None else []
    record_run_id = uuid.uuid4().hex
    # Phase-0 shadow input slot (T036): review() stashes the fetched
    # diff_result here so the post-publish shadow runs without re-fetch.
    shadow_stash: dict[str, Any] = {}
    try:
        creds = _Credentials(provider)
        pk = review_pk(envelope.repo_full_name, envelope.pr_number)
        inner_fence = _make_fence(envelope=envelope, creds=creds, diff_transport=diff_transport)
        inner_publish = _make_publish(
            envelope=envelope,
            creds=creds,
            table=table,
            pk=pk,
            owner=envelope.delivery_guid,
            clock=clock,
            github_transport=github_transport,
        )

        def _evented_fence() -> str:
            # run_review calls fence() strictly after the claim phase, so
            # entry here marks `claimed` reached (discard paths never call).
            result = inner_fence()
            record_events.append(checkpoint(stage="claimed", run_id=record_run_id))
            return result

        def _evented_publish(content: Any) -> int:
            comment_id = inner_publish(content)
            record_events.append(checkpoint(stage="published", run_id=record_run_id))
            record_events.append(review_published(comment_id=comment_id, run_id=record_run_id))
            return comment_id

        outcome = run_review(
            pk=pk,
            incoming_sha=envelope.head_sha,
            owner=envelope.delivery_guid,
            table=table,
            now=lambda: int(clock()),
            review=_make_review(
                envelope=envelope,
                creds=creds,
                usage=usage,
                diff_transport=diff_transport,
                llm_factory=llm_factory,
                system_prompt=system_prompt,
                allowed_hosts=provider.allowed_hosts,
                clock=clock,
                github_transport=github_transport,
                remaining_time_ms=remaining_time_ms,
                events=record_events,
                run_id=record_run_id,
                table=table,
                shadow_stash=shadow_stash,
            ),
            fence=_evented_fence,
            publish=_evented_publish,
        )
        if outcome.kind == OutcomeKind.PUBLISHED:
            # Finalize wrote the record: `finalized` reached. The conflict
            # variant never completes finalize, so it stays unmarked.
            record_events.append(checkpoint(stage="finalized", run_id=record_run_id))
    except (
        DiffError,
        LlmError,
        GitHubError,
        ConfigError,
        AssembleError,
        ReconcileError,
        # Direct Exception subclass (fanout.py) — never shadowed by the
        # arms above; the containment boundary row pins the fanout_*
        # dispatch.
        FanoutDegraded,
    ) as exc:
        error_class = _error_class(exc)
        duration_ms = max(0, int((clock() - started) * 1000))
        retryable = is_retryable(exc)
        receipt_handle, receive_count = _record_delivery_context(record)
        if retryable:
            # Throttling row first (time-critical): extend visibility per
            # Retry-After when the error carries the hint, best-effort.
            hint = exc.retry_after if isinstance(exc, GitHubError) else None
            _extend_visibility(sqs, queue_url, receipt_handle, hint)
        # D2 trigger second: final-attempt transients and permanent rows
        # publish the failure notice (best-effort); No-rows skip. The
        # returned disposition rides the log line either way.
        notice = _attempt_notice(
            exc=exc,
            envelope=envelope,
            receive_count=receive_count,
            table=table,
            creds=creds,
            clock=clock,
            diff_transport=diff_transport,
            github_transport=github_transport,
        )
        if retryable:
            _emit(
                sink,
                envelope=envelope,
                duration_ms=duration_ms,
                token_usage=usage["tokens"],
                status="retry_queued",
                error_class=error_class,
                generation=None,
                failure_notice_published=notice,
                prompt_sha256=delivered_prompt_sha256,
            )
            raise
        _emit(
            sink,
            envelope=envelope,
            duration_ms=duration_ms,
            token_usage=usage["tokens"],
            status="discarded_error",
            error_class=error_class,
            generation=None,
            failure_notice_published=notice,
            prompt_sha256=delivered_prompt_sha256,
        )
        _archive_run(
            s3=s3,
            bucket=archive_bucket,
            table=table,
            envelope=envelope,
            head_sha=envelope.head_sha,
            status="failed",
            pipeline=archive_mod.resolve_pipeline(record_events),
            events=record_events,
            run_id=record_run_id,
            started_ts_ms=int(started * 1000),
            clock=clock,
        )
        return "discarded_error"
    duration_ms = max(0, int((clock() - started) * 1000))
    status = _OUTCOME_STATUS[outcome.kind]
    _emit(
        sink,
        envelope=envelope,
        duration_ms=duration_ms,
        token_usage=usage["tokens"],
        status=status,
        error_class=None,
        generation=outcome.generation,
        stale_discarded=outcome.kind in _STALE_DISCARDED_KINDS,
        failure_notice_published="false",
        prompt_sha256=delivered_prompt_sha256,
    )
    if outcome.kind in (OutcomeKind.PUBLISHED, OutcomeKind.PUBLISHED_FINALIZE_CONFLICT):
        # PUBLISHED_FINALIZE_CONFLICT: the review published; the fence loss
        # is delivery bookkeeping (failure_notice), not the review outcome —
        # archive status and shadow participation are correct.
        # Phase-0 shadow (HLD §8, T036): single-pass already published
        # above with unchanged latency; the shadow specialist runs inline
        # AFTER publish (never before — it must not block, delay, or
        # share the 401 budget with the essential path), and the archive
        # below runs AFTER the shadow completes. `MULTI_AGENT=1` ignores
        # `MULTI_AGENT_PHASE0` entirely; shadow iff 0/1. The D9 lease was
        # released inside review() — the shadow is NOT lease-covered.
        # Discard outcomes archive nothing: no review ran.
        shadow_ran = False
        ma_shadow = multi_agent_config()
        if ma_shadow.multi_agent != 1 and ma_shadow.multi_agent_phase0 == 1:
            diff_result = shadow_stash.get("diff_result")
            if diff_result is None:
                # Defensive telemetry only (unreachable through review(),
                # which always stashes on the publish path): a closed
                # HLD §6 reason set forbids a dedicated `review_skipped`
                # reason here (`{"empty_diff", "phase0_no_budget"}`), so
                # this warns instead of emitting a false-categorized
                # event. The run archives as the plain single-pass
                # publish it was.
                logger.warning(
                    "shadow_skipped",
                    extra={"status": "shadow_no_diff", "error_class": "missing_diff_result"},
                )
            else:
                snapshot = creds.current()
                shadow_ran = _run_phase0_shadow(
                    diff_result=diff_result,
                    ma_cfg=ma_shadow,
                    api_key=snapshot.glm_api_key,
                    model=snapshot.glm_model,
                    endpoint=snapshot.glm_endpoint,
                    allowed_hosts=provider.allowed_hosts,
                    run_id=record_run_id,
                    events=record_events,
                    remaining_ms=_read_remaining_ms(remaining_time_ms),
                    residuals=_load_residuals_for(envelope.repo_full_name, envelope.pr_number),
                    review_fn=shadow_review_fn,
                )
                if not shadow_ran:
                    record_events.append(
                        review_skipped(
                            reason="phase0_no_budget",
                            pr=envelope.pr_number,
                            sha=outcome.head_sha or envelope.head_sha,
                            run_id=record_run_id,
                        )
                    )
        degraded = any(
            isinstance(event, dict) and event.get("type") == "degraded_to_single_pass"
            for event in record_events
        )
        # Degraded x shadow exclusion (Gate 20, Finding 2): degrade needs
        # MULTI_AGENT=1 while shadow needs MULTI_AGENT=0+PHASE0=1 — same
        # env, no mid-flight mutation, so the pair is flag-unreachable.
        # It is also mapping-invalid (("phase0_shadow",
        # "degraded_single_pass") -> build_meta bad_status = whole-archive
        # loss). The override below is safe BECAUSE the pair cannot
        # co-occur — do not "fix" it into archive loss.
        _archive_run(
            s3=s3,
            bucket=archive_bucket,
            table=table,
            envelope=envelope,
            head_sha=outcome.head_sha or envelope.head_sha,
            status="degraded_single_pass" if degraded else "published",
            pipeline="phase0_shadow" if shadow_ran else archive_mod.resolve_pipeline(record_events),
            events=record_events,
            run_id=record_run_id,
            started_ts_ms=int(started * 1000),
            clock=clock,
        )
    return status


def _archive_run(
    *,
    s3: Any,
    bucket: str,
    table: Any,
    envelope: Envelope,
    head_sha: str,
    status: str,
    pipeline: str,
    events: list[dict[str, Any]],
    run_id: str,
    started_ts_ms: int,
    clock: Clock,
) -> None:
    """Best-effort run archive (HLD-004 §6, T029): S3 puts of
    `events.jsonl` + `meta.json`, then the DDB index row — AFTER finalize,
    never inside the review callback. NEVER raises: an archive failure
    must not fail (or redeliver) an already-published review, so every
    fault is a structured warning. No bucket (or no client) means the
    feature is off — return silently (missing) or warn (misconfigured).
    """
    # T066 per-review rollup: computed ONCE, before the early returns, so
    # every archive attempt (including archive-off configs) reads one
    # number; degraded paths reduce to zeros (no token-bearing events).
    # Pure reducer — never raises, so the never-raises contract holds.
    rollup = archive_mod.token_rollup(events)
    logger.info(
        "token_rollup",
        extra={
            "token_usage": rollup["token_usage"],
            "tokens_wave": rollup["token_usage_by_stage"]["wave"],
            "tokens_verifier": rollup["token_usage_by_stage"]["verifier"],
            "tokens_synth": rollup["token_usage_by_stage"]["synthesizer"],
        },
    )
    if not bucket:
        return
    if s3 is None:
        logger.warning(
            "archive_skipped",
            extra={"status": "archive_no_client", "error_class": "missing_s3_client"},
        )
        return
    try:
        finished_ts_ms = int(clock() * 1000)
        meta = archive_mod.build_meta(
            run_id=run_id,
            pr=envelope.pr_number,
            sha=head_sha,
            pipeline=pipeline,
            status=status,
            started_ts=started_ts_ms,
            finished_ts=finished_ts_ms,
            token_usage=rollup["token_usage"],
            token_usage_by_stage=rollup["token_usage_by_stage"],
        )
        events_body = archive_mod.render_events_jsonl(events)
        meta_body = archive_mod.render_meta(meta)
        events_key = archive_mod.s3_key(envelope.pr_number, head_sha, run_id, "events.jsonl")
        meta_key = archive_mod.s3_key(envelope.pr_number, head_sha, run_id, "meta.json")
        s3.put_object(Bucket=bucket, Key=events_key, Body=events_body.encode("utf-8"))
        s3.put_object(Bucket=bucket, Key=meta_key, Body=meta_body.encode("utf-8"))
        if archive_mod.should_write_index_row(events):
            item = archive_mod.build_index_item(
                run_id=run_id,
                pr=envelope.pr_number,
                sha=head_sha,
                pipeline=pipeline,
                status=status,
                started_ts=started_ts_ms,
                archive_s3_key=events_key,
                archive_written_at=finished_ts_ms,
                findings_n=archive_mod.findings_count(events),
            )
            try:
                table.update_item(
                    Key={"pk": item["pk"]},
                    UpdateExpression=(
                        "SET run_id = :run_id, pr_number = :pr, started_ts = :started,"
                        " sha = :sha, #st = :status, pipeline = :pipeline,"
                        " archive_s3_key = :key, archive_written_at = :written,"
                        " findings_n = :findings"
                    ),
                    ConditionExpression="attribute_not_exists(pk)",
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={
                        ":run_id": item["run_id"],
                        ":pr": item["pr_number"],
                        ":started": item["started_ts"],
                        ":sha": item["sha"],
                        ":status": item["status"],
                        ":pipeline": item["pipeline"],
                        ":key": item["archive_s3_key"],
                        ":written": item["archive_written_at"],
                        ":findings": item["findings_n"],
                    },
                )
            except Exception as exc:  # noqa: BLE001
                # SQS is at-least-once: a redelivered run re-archives and
                # hits the write-once index condition. First archive wins;
                # a duplicate gets its OWN signal, never the generic
                # archive_failed that reads as a real fault (bot r1:1740).
                from common.protocol import ConditionalCheckFailed

                response = getattr(exc, "response", None)
                code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
                if code == "ConditionalCheckFailedException" or isinstance(
                    exc, ConditionalCheckFailed
                ):
                    logger.warning(
                        "archive_duplicate",
                        extra={
                            "status": "archive_duplicate",
                            "error_class": "index_row_exists",
                        },
                    )
                else:
                    raise
    except Exception as exc:
        logger.warning(
            "archive_failed",
            extra={"status": "archive_failed", "error_class": _error_class(exc)},
        )


def _env_allowed_hosts() -> tuple[str, ...]:
    raw = os.environ.get("GLM_ALLOWED_HOSTS", "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _load_system_prompt() -> str:
    """Production prompt source: the versioned contract file (T018).

    Two layouts are resolved, zip first: the Lambda archive ships
    `prompts/` beside the handler (`<dir>/prompts`), while a repo checkout
    keeps it one level up (`<dir>/../prompts`). Tests always inject
    `_system_prompt`. No layout match falls back to the `SYSTEM_PROMPT`
    env var (dev only — never set in Terraform); absence of all three is a
    permanent config fault (complete, alert) — never an empty prompt.
    """
    here = Path(__file__).resolve().parent
    for candidate in (
        here / "prompts" / "system_prompt.md",
        here.parent / "prompts" / "system_prompt.md",
    ):
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    fallback = os.environ.get("SYSTEM_PROMPT", "")
    if fallback:
        return fallback
    raise ConfigError("system_prompt", "missing") from None


def reset_config_cache() -> None:
    """Drop the warm-container config cache (tests / rotation drills)."""
    global _CONFIG_PROVIDER
    _CONFIG_PROVIDER = None


def reset_sqs_cache() -> None:
    """Drop the warm-container SQS client cache (tests / rotation drills)."""
    global _SQS_CLIENT
    _SQS_CLIENT = None


def reset_prompt_cache() -> None:
    """Drop the warm-container system-prompt cache (tests / rotation drills)."""
    global _SYSTEM_PROMPT
    _SYSTEM_PROMPT = None


def handler(
    event: dict[str, Any],
    context: Any = None,
    *,
    _table: Any = None,
    _ssm: Any = None,
    _config_provider: ConfigProvider | None = None,
    _now: Clock | None = None,
    _diff_transport: Any = None,
    _llm_factory: Any = None,
    _github_transport: Callable[..., tuple[int, bytes]] | None = None,
    _sink: Sink | None = None,
    _system_prompt: str | None = None,
    _allowed_hosts: tuple[str, ...] | None = None,
    _sqs: Any = None,
    _s3: Any = None,
) -> dict[str, Any]:
    """SQS event → per-record pipeline; retryable faults raise (the batch
    fails and the queue redelivers), every other record completes. `_sqs`
    drives the Retry-After visibility edge; production builds a client
    lazily, tests inject a double (or omit it — the queue default then
    applies). `_s3` + the `ARCHIVE_BUCKET` env drive the T029 run archive
    (absent bucket = feature off; absent client with a bucket = warn)."""
    clock = _now if _now is not None else time.time
    sink: Sink = _sink if _sink is not None else sys.stdout.write

    if _config_provider is not None:
        provider = _config_provider
    elif _ssm is not None:
        # Injected SSM: always fetch fresh so tests stay deterministic.
        hosts = tuple(_allowed_hosts) if _allowed_hosts is not None else ()
        provider = ConfigProvider(_ssm.get_parameters, clock=clock, allowed_endpoint_hosts=hosts)
    else:
        global _CONFIG_PROVIDER
        if _CONFIG_PROVIDER is None:
            import boto3  # deferred: import-time must not require credentials

            hosts = tuple(_allowed_hosts) if _allowed_hosts is not None else _env_allowed_hosts()
            _CONFIG_PROVIDER = ConfigProvider(
                boto3.client("ssm").get_parameters,
                clock=clock,
                allowed_endpoint_hosts=hosts,
            )
        provider = _CONFIG_PROVIDER

    table = _table
    if table is None:
        import boto3  # deferred: import-time must not require credentials

        table_name = os.environ.get("STATE_TABLE_NAME", "pr-reviewer-state")
        table = _BotoTable(boto3.resource("dynamodb").Table(table_name))

    github_transport = (
        _github_transport if _github_transport is not None else _default_github_transport
    )
    if _system_prompt is not None:
        system_prompt = _system_prompt
    else:
        # Production path: cache the deploy-static prompt per warm container
        # (mirror `_CONFIG_PROVIDER`); a failed load leaves the cache empty
        # so the next invocation retries.
        global _SYSTEM_PROMPT
        if _SYSTEM_PROMPT is None:
            _SYSTEM_PROMPT = _load_system_prompt()
        system_prompt = _SYSTEM_PROMPT

    sqs = _sqs
    if sqs is None:
        # Best-effort edge: without a client (no region/credentials in unit
        # tests that predate the edge) the queue default applies and
        # classification is unchanged. Production caches one client per warm
        # container (mirror `_CONFIG_PROVIDER`); a failed build leaves the
        # cache empty so the next invocation retries.
        global _SQS_CLIENT
        if _SQS_CLIENT is None:
            try:
                import boto3  # deferred: import-time must not require credentials

                _SQS_CLIENT = boto3.client("sqs")
            except Exception:
                _SQS_CLIENT = None
        sqs = _SQS_CLIENT
    queue_url = os.environ.get("WORK_QUEUE_URL", "")
    archive_bucket = os.environ.get("ARCHIVE_BUCKET", "")
    s3 = _s3
    if s3 is None and archive_bucket:
        try:
            import boto3  # deferred: import-time must not require credentials

            s3 = boto3.client("s3")
        except Exception:
            s3 = None

    records = event.get("Records") if isinstance(event, dict) else None
    results: list[str] = []
    # Remaining-time source for the single in-executor LLM timeout retry
    # (`_make_review` fails closed to no-retry without it): unit doubles
    # pass `context=None`, which also lands here.
    _get_remaining = getattr(context, "get_remaining_time_in_millis", None)
    remaining_time_ms = _get_remaining if callable(_get_remaining) else None
    for record in records or []:
        results.append(
            _process_record(
                record,
                table=table,
                provider=provider,
                clock=clock,
                diff_transport=_diff_transport,
                llm_factory=_llm_factory,
                github_transport=github_transport,
                sink=sink,
                system_prompt=system_prompt,
                sqs=sqs,
                queue_url=queue_url,
                remaining_time_ms=remaining_time_ms,
                s3=s3,
                archive_bucket=archive_bucket,
            )
        )
    return {"ok": True, "results": results}

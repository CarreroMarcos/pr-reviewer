"""Worker Lambda entry point (HLD §2.3, §3.3, §3.4; FR-009–FR-016).

Review execution engine: SQS event (`batch_size = 1`) → per-record pipeline
in HLD §2.3 order — validate envelope (T006) → hydrate credentials (T014;
single 401 re-fetch is the ONLY in-request retry) → establish → diff (T031)
→ LLM (T032) → assemble + gate (T033) → claim → live-head fence → publish
(POST via creation lease when no `comment_id`, else PATCH) → finalize.
Superseded/stale/claim-held/finalize-conflict → discard (log, complete, no
publish, no retry).

Error mapping at the worker boundary (HLD §2.3 item 8 is the authority;
`is_retryable` is the executable form of that table for T034 scope):

* RETRYABLE → the error propagates for SQS redelivery (queue owns retries;
  DLQ after `maxReceiveCount 5`): transient diff transport (F4
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
comment; absent → POST a new comment. A re-publish of a row that still
carries `comment_id` is therefore a PATCH of the same comment, never a
second POST. There is deliberately NO ACTIVE same-owner short-circuit:
sequential same-owner+SHA replay after a completed run publishes again via
PATCH (reopened PR ⇒ fresh review, possibly at the same head SHA).
Full §3.4 reconciliation (creation-lease race hardening, marker adoption,
the PATCH-404 decision table) is T039/T040 scope: a PATCH 404 here
completes non-retryably without recovery, and a new-revision POST converges
through later reconciliation.

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
uninjected production path; tests always inject.

Ports (duck-typed — fakes mirror these exactly):

* table: protocol port — `get_item(pk) -> dict | None` (copy semantics),
  `update_item(Key=..., UpdateExpression=..., ConditionExpression=...,
  ExpressionAttributeNames=..., ExpressionAttributeValues=...)` raising
  `common.protocol.ConditionalCheckFailed`.
* ssm: `get_parameters(Names=..., WithDecryption=True)` (GetParameters shape).
* diff transport: `(url, headers) -> common.diff.HttpResponse`.
* llm factory: `(host, port, *, timeout)` (`http.client.HTTPSConnection` shape).
* github transport: `(method, url, headers, body) -> (status, body_bytes)`.

Pure stdlib + boto3. No secrets in logs or error text (Constitution III).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common.assemble import AssembleError, build_comment, render_diff_text
from common.config import ConfigError, ConfigProvider
from common.diff import DiffError, fetch_diff, fetch_pr_head_sha
from common.envelope import Envelope, EnvelopeError, validate_envelope
from common.llm import LlmError, review_diff
from common.logs import build_event, emit
from common.protocol import OutcomeKind, run_review
from common.state import review_pk
from common.validate import PROMPT_VERSION

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_USER_AGENT = "pr-reviewer/1.0"
GITHUB_TIMEOUT_SECONDS = 10

# Warm-container config cache — production path only (see module docstring).
_CONFIG_PROVIDER: ConfigProvider | None = None

Clock = Callable[[], float]
Sink = Callable[[str], None]


class GitHubError(Exception):
    """Typed GitHub failure: `status` is the HTTP status (`None` for
    transport failures or unparseable bodies); `error_class` is
    machine-readable (`http_{status}`, `transport_error`,
    `invalid_response`). Never carries token material."""

    def __init__(self, status: int | None, error_class: str) -> None:
        self.status = status
        self.error_class = error_class
        super().__init__(f"github request failed: {error_class}")


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
        return {
            key: int(value) if isinstance(value, Decimal) else value for key, value in item.items()
        }

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


class _Credentials:
    """Single-refetch credential holder (HLD §2.3 item 1).

    All GitHub/LLM call sites for one record share a single instance, so
    the 401 cache-bust fires AT MOST ONCE per request — the only in-request
    retry. `refresh_once()` invalidates + re-hydrates on first call (True);
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
    if error_class.startswith("http_4") and error_class != "http_429":
        return False
    return True


def _github_is_retryable(exc: GitHubError) -> bool:
    """GitHub-write side: auth/lost-access completes (T040 owns the
    PATCH-404 decision table); throttling, 5xx, transport and garbled
    replies raise for the queue."""
    return exc.status not in (401, 403, 404)


def is_retryable(exc: BaseException) -> bool:
    """Worker-boundary retry verdict (HLD §2.3 item 8, T034 scope).

    True → propagate for SQS redelivery (bounded by `maxReceiveCount 5`).
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
    return type(exc).__name__


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": GITHUB_USER_AGENT,
        "Authorization": f"Bearer {token}",
    }


def _default_github_transport(
    method: str, url: str, headers: dict[str, str], body: bytes
) -> tuple[int, bytes]:
    """Live Issues-Comments transport: non-2xx surfaces as status (the
    caller raises `GitHubError`); only transport failures raise here."""
    request = Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310
    try:
        with urlopen(request, timeout=GITHUB_TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()
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
    """One Issues-Comments write → the comment id; non-2xx → `GitHubError`."""
    body = json.dumps({"body": content}, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    status, raw = github_transport(method, url, _github_headers(token), body)
    if not 200 <= status <= 299:
        raise GitHubError(status, f"http_{status}")
    return _parse_comment_id(raw)


def _make_review(
    *,
    envelope: Envelope,
    creds: _Credentials,
    usage: dict[str, int],
    diff_transport: Any,
    llm_factory: Any,
    system_prompt: str,
) -> Callable[[], str]:
    """Protocol `review` port: diff → LLM (single 401 re-fetch) → assemble
    + mandatory validate gate → publish-ready content (opaque string)."""

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

    def _call_llm(diff_text: str) -> Any:
        cfg = creds.current()
        try:
            return review_diff(
                api_key=cfg.glm_api_key,
                model=cfg.glm_model,
                endpoint=cfg.glm_endpoint,
                system_prompt=system_prompt,
                diff_text=diff_text,
                _connection_factory=llm_factory,
            )
        except LlmError as exc:
            if exc.error_class == "http_401" and creds.refresh_once():
                fresh = creds.current()
                return review_diff(
                    api_key=fresh.glm_api_key,
                    model=fresh.glm_model,
                    endpoint=fresh.glm_endpoint,
                    system_prompt=system_prompt,
                    diff_text=diff_text,
                    _connection_factory=llm_factory,
                )
            raise

    def review() -> str:
        diff_result = _fetch_diff()
        result = _call_llm(render_diff_text(diff_result))
        usage["tokens"] = result.total_tokens
        comment = build_comment(
            repo_full_name=repo,
            pr_number=pr_number,
            review_content=result.content,
            truncated=diff_result.truncated,
        )
        return comment.content

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


def _make_publish(
    *,
    envelope: Envelope,
    creds: _Credentials,
    table: Any,
    pk: str,
    github_transport: Callable[..., tuple[int, bytes]],
) -> Callable[[str], int]:
    """Protocol `publish` port (constitution IV — exactly one canonical
    comment): consults the STORED `comment_id` — present → PATCH that
    comment; absent → POST via creation lease. `comment_id` lands on the
    record only via finalize, so a same-revision re-publish is a PATCH,
    never a second POST."""

    repo = envelope.repo_full_name
    pr_number = envelope.pr_number

    def _write(method: str, url: str, content: str) -> int:
        try:
            token = creds.current().github_token
            return _github_write(github_transport, method, url, token, content)
        except GitHubError as exc:
            if exc.status == 401 and creds.refresh_once():
                return _github_write(
                    github_transport, method, url, creds.current().github_token, content
                )
            raise

    def publish(content: str) -> int:
        item = table.get_item(pk) or {}
        comment_id = item.get("comment_id")
        if isinstance(comment_id, bool):
            comment_id = None
        if isinstance(comment_id, int) and comment_id >= 1:
            return _write("PATCH", _comment_url(repo, comment_id), content)
        return _write("POST", _comments_url(repo, pr_number), content)

    return publish


_OUTCOME_STATUS = {
    OutcomeKind.PUBLISHED: "published",
    OutcomeKind.PUBLISHED_FINALIZE_CONFLICT: "published_finalize_conflict",
    OutcomeKind.DISCARDED_SUPERSEDED: "discarded_superseded",
    OutcomeKind.DISCARDED_STALE: "discarded_stale",
    OutcomeKind.DISCARDED_CLAIM_HELD: "discarded_claim_held",
}


def _emit(
    sink: Sink,
    *,
    envelope: Envelope,
    duration_ms: int,
    token_usage: int,
    status: str,
    error_class: str | None,
    generation: int | None,
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
                prompt_version=PROMPT_VERSION,
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
) -> str:
    """Run one SQS record through the pipeline.

    Returns the completion status. Raises the original error ONLY for
    retryable faults (queue redelivery); every other path completes.
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
    try:
        creds = _Credentials(provider)
        pk = review_pk(envelope.repo_full_name, envelope.pr_number)
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
            ),
            fence=_make_fence(envelope=envelope, creds=creds, diff_transport=diff_transport),
            publish=_make_publish(
                envelope=envelope,
                creds=creds,
                table=table,
                pk=pk,
                github_transport=github_transport,
            ),
        )
    except (DiffError, LlmError, GitHubError, ConfigError, AssembleError) as exc:
        error_class = _error_class(exc)
        duration_ms = max(0, int((clock() - started) * 1000))
        if is_retryable(exc):
            _emit(
                sink,
                envelope=envelope,
                duration_ms=duration_ms,
                token_usage=usage["tokens"],
                status="retry_queued",
                error_class=error_class,
                generation=None,
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
    )
    return status


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
) -> dict[str, Any]:
    """SQS event → per-record pipeline; retryable faults raise (the batch
    fails and the queue redelivers), every other record completes."""
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
    system_prompt = _system_prompt if _system_prompt is not None else _load_system_prompt()

    records = event.get("Records") if isinstance(event, dict) else None
    results: list[str] = []
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
            )
        )
    return {"ok": True, "results": results}

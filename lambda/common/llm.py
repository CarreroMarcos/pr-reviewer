"""Provider chat-completions client (HLD §2.3 item 5, §2.7; FR-026; Constitution III).

Sends one `temperature: 0.2` chat-completions POST over stdlib
`http.client` — no model SDKs (Constitution II). Model, endpoint, and API
key arrive as injected parameters: their VALUES originate from SSM
hydration (`common.config`), but this module performs no SSM access itself —
the worker (T034) injects the hydrated values, keeping the client pure.

Timeout policy (HLD §2.3): connect 2 s, read env-configurable
(`GLM_READ_TIMEOUT_S`, default 240 s, clamped to [30, 600] — see
`_read_timeout_s`) with an explicit per-call `read_timeout_s` override
(HLD-004 §9 item 1: resolved once at entry, applied at the socket
switch below). stdlib exposes a single socket timeout, so the
connection is opened with `timeout=CONNECT_TIMEOUT_S` and, once
connected, the socket is switched to the resolved read timeout for the
response read. `CONNECT_TIMEOUT_S` and `DEFAULT_READ_TIMEOUT_S` are
exported for tests to pin.

Error semantics: HTTP errors, timeouts, connection failures, and malformed
responses raise typed `LlmError` (machine-readable `error_class`) — never
swallowed, never `None`-as-success — so the queue owns the retry
(visibility redelivery, DLQ after `maxReceiveCount 3`).

Logging is minimal (HLD §5.4): one static-message record per call carrying
only `status` / `duration_ms` / token usage (`prompt_tokens`,
`completion_tokens`, `total_tokens`) plus `error_class` on failure. Prompt,
diff, completion, endpoint credentials, and the API key never reach a log
record or an exception message.

The transport is injected (`_connection_factory(host, port, *, timeout)`)
so tests stub it in-process — no live external calls (HLD §4.4 item 3).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

TEMPERATURE = 0.2
CONNECT_TIMEOUT_S = 2
# Default server-wait budget (s): the endpoint's real latency bulk
# straddles the old 45 s single-pass budget (live successes at
# 45.9 s/46.9 s/47.7 s each burned a 90-minute redelivery + DLQ trip),
# so the default covers the observed p99 with headroom. Override per
# deploy via `GLM_READ_TIMEOUT_S`; resolved at request time (not import
# time) so tests can monkeypatch/env-override cleanly.
DEFAULT_READ_TIMEOUT_S = 240
READ_TIMEOUT_MIN_S = 30
READ_TIMEOUT_MAX_S = 600
READ_TIMEOUT_ENV_VAR = "GLM_READ_TIMEOUT_S"

# Thinking portability gate (SPR-62, Gate-1 advisory; open-questions Q1 stays
# open): the `"thinking": {"type": "disabled"}` payload key is GLM-specific
# and is sent ONLY when the model looks like a GLM model (`startswith "glm"`)
# AND the endpoint host is a known GLM host below. Any other provider/model
# may reject an unknown key, so the key is omitted entirely there (never an
# `"enabled"` variant). The worker threads its env-configured host set
# (`GLM_ALLOWED_HOSTS` environment, terraform/compute.tf) through
# `review_diff(allowed_hosts=...)`; the set below is the default when the
# caller passes nothing. If infra adds a GLM host, update the environment
# (and this default to match).
GLM_ALLOWED_HOSTS = frozenset({"api.z.ai"})

logger = logging.getLogger(__name__)

# Factory seam: mirrors `http.client.HTTPSConnection(host, port, timeout=…)`.
# The returned object must support `connect()`, `.sock.settimeout()`,
# `request(method, path, body, headers)`, `getresponse()` (→ `.status`,
# `.read()`), and `close()`.
ConnectionFactory = Callable[..., Any]

Clock = Callable[[], float]


class LlmError(Exception):
    """Typed LLM failure for queue-retry semantics.

    `error_class` is machine-readable: `bad_endpoint`, `timeout`,
    `connection_error`, `http_{status}`, `invalid_response`, `invalid_key`,
    `length` (truncated at the provider max tokens — HLD-004 §9 item 3),
    `rate_limit` (Z.AI concurrency-cap code 1302 on HTTP 429 — §9 item 6).
    The message never carries prompt, diff, completion, or key material.
    """

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(f"llm request failed: {error_class}")


@dataclass(frozen=True)
class ReviewResult:
    """Parsed completion: Markdown content plus provider-observed usage.

    `reasoning_content` is the provider's reasoning trace when the call
    ran with thinking enabled (HLD-004 §9 item 5), else `None` — the raw
    trace is truncated at capture by the caller (`REASONING_MAX_CHARS`),
    never summarized here.
    """

    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    reasoning_content: str | None = None


def _default_factory(host: str, port: int, *, timeout: int) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(host, port, timeout=timeout)


def _read_timeout_s() -> int:
    """Resolve the read (server-wait) budget in seconds, at request time.

    `GLM_READ_TIMEOUT_S` parsed as int; missing/unparseable → the
    documented default; clamped to `[READ_TIMEOUT_MIN_S,
    READ_TIMEOUT_MAX_S]`. Import-time resolution would freeze the value
    for the container lifetime — request-time keeps env-override and
    test monkeypatching clean.
    """
    try:
        value = int(os.environ.get(READ_TIMEOUT_ENV_VAR, ""))
    except (TypeError, ValueError):
        return DEFAULT_READ_TIMEOUT_S
    return max(READ_TIMEOUT_MIN_S, min(READ_TIMEOUT_MAX_S, value))


def _split_endpoint(endpoint: Any) -> tuple[str, int, str]:
    if not isinstance(endpoint, str):
        raise LlmError("bad_endpoint")
    parts = urlsplit(endpoint)
    if parts.scheme.lower() != "https":
        raise LlmError("bad_endpoint")
    host = parts.hostname or ""
    if not host:
        raise LlmError("bad_endpoint")
    port = parts.port or 443
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return host, port, path


def _parse_result(payload: Any) -> ReviewResult:
    if not isinstance(payload, dict):
        raise LlmError("invalid_response")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmError("invalid_response")
    first = choices[0]
    if isinstance(first, dict) and first.get("finish_reason") == "length":
        # Truncated at the provider max tokens (HLD-004 §9 item 3): the
        # partial content is unusable — error, never silent success.
        raise LlmError("length")
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content:
        raise LlmError("invalid_response")
    reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    def _tokens(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return ReviewResult(
        content=content,
        prompt_tokens=_tokens("prompt_tokens"),
        completion_tokens=_tokens("completion_tokens"),
        total_tokens=_tokens("total_tokens"),
        reasoning_content=reasoning if isinstance(reasoning, str) else None,
    )


def _is_rate_limit_1302(raw: bytes) -> bool:
    """Z.AI concurrency-cap signal (HLD-004 §9 item 6): an HTTP 429 whose
    JSON body carries error code 1302. The code travels as a string in
    Zhipu error envelopes (`{"error": {"code": "1302", ...}}`); the int
    form is accepted too. Anything unparseable or otherwise shaped is
    not a 1302 — the caller keeps the generic `http_429` class."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    candidates = []
    if isinstance(error, dict):
        candidates.append(error.get("code"))
    candidates.append(payload.get("code"))
    return any(
        isinstance(code, (int, str)) and not isinstance(code, bool) and str(code) == "1302"
        for code in candidates
    )


def review_diff(
    *,
    api_key: str,
    model: str,
    endpoint: str,
    system_prompt: str,
    diff_text: str,
    allowed_hosts: frozenset[str] | None = None,
    thinking_enabled: bool = False,
    reasoning_effort: str = "low",
    read_timeout_s: int | None = None,
    _connection_factory: ConnectionFactory | None = None,
    _clock: Clock | None = None,
) -> ReviewResult:
    """Run one chat-completions review; raise `LlmError` on any failure.

    `endpoint` is the full HTTPS chat-completions URL from SSM hydration.
    `diff_text` carries the assembled review payload (PR metadata + budgeted
    diff + prior comment), not bare diff text.
    `allowed_hosts` is the env-configured GLM host set threaded through by
    the worker; `None` (default) falls back to `GLM_ALLOWED_HOSTS` so
    existing callers behave exactly as before.
    `thinking_enabled` sends `thinking: {"type": "enabled"}` plus
    `reasoning_effort` on GLM endpoints (HLD-004 §9 item 4; multi-agent
    specialists/verifier/synthesizer) — the default `False` keeps the
    existing single-pass `disabled` payload byte-identical.
    `read_timeout_s` is the explicit socket read budget (HLD-004 §9 item
    1): `None` (default) resolves `_read_timeout_s()` exactly once at
    entry; a passed value (the T026 contender clamp) is used verbatim
    with no env resolution — no new module constant.
    """
    if not api_key or not model:
        raise LlmError("bad_endpoint")
    if isinstance(api_key, str) and not api_key.strip():
        # Blank-but-truthy credential (whitespace-only): stdlib header
        # validation accepts bare spaces, so without this guard the key
        # would travel to the provider and fail there. Classify it here
        # as malformed credential material instead — deterministic,
        # non-retryable (`invalid_key`).
        raise LlmError("invalid_key")
    host, port, path = _split_endpoint(endpoint)
    factory = _connection_factory if _connection_factory is not None else _default_factory
    clock = _clock if _clock is not None else time.monotonic
    start = clock()
    if read_timeout_s is None:
        resolved_timeout_s = _read_timeout_s()
    elif (
        not isinstance(read_timeout_s, int)
        or isinstance(read_timeout_s, bool)
        or read_timeout_s < 1
    ):
        # Caller-shape fault (same class as the credential-shape faults
        # above): fail here as typed `LlmError`, never as a raw
        # `TypeError` escaping `sock.settimeout` below.
        raise LlmError("bad_endpoint")
    else:
        resolved_timeout_s = read_timeout_s
    # Thinking portability gate: GLM-only key (see GLM_ALLOWED_HOSTS) —
    # omitted for every other provider so non-GLM endpoints never see it.
    payload: dict[str, Any] = {
        "model": model,
        # Provider default runs GLM reasoning (~1.3K tokens, ~45s) before
        # answering, which makes the quickstart ≤15s comment bar (HLD
        # §2.2 (b)) unreachable and overruns the read budget — surfaced
        # live by the T035 acceptance run. Thinking-off measured 5-8s.
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": diff_text},
        ],
        "temperature": TEMPERATURE,
        # HLD-004 §9 item 2: explicit ceiling so truncation surfaces as
        # `LlmError("length")` (item 3) instead of silent success.
        "max_tokens": 16384,
    }
    if model.startswith("glm") and host.lower() in (
        GLM_ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts
    ):
        if thinking_enabled:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = reasoning_effort
        else:
            payload["thinking"] = {"type": "disabled"}
    body = json.dumps(
        payload,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    conn = None
    try:
        conn = factory(host, port, timeout=CONNECT_TIMEOUT_S)
        conn.connect()
        # Read budget starts after the TCP/TLS handshake: switch the
        # connected socket from the connect timeout to the entry-resolved
        # read timeout (`read_timeout_s` override or one `_read_timeout_s()`
        # resolution — never re-resolved mid-call).
        conn.sock.settimeout(resolved_timeout_s)
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        response = conn.getresponse()
        raw = response.read()
        if response.status != 200:
            if response.status == 429 and _is_rate_limit_1302(raw):
                # Z.AI concurrency cap (HLD-004 §9 item 6): fail fast to
                # wave survivors — the worker boundary retries it as a
                # transient via the existing unknown-fault default.
                raise LlmError("rate_limit")
            raise LlmError(f"http_{response.status}")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise LlmError("invalid_response") from None
        result = _parse_result(payload)
    except LlmError as exc:
        elapsed_ms = max(0, int((clock() - start) * 1000))
        logger.warning(
            "llm_review",
            extra={
                "status": "llm_error",
                "error_class": exc.error_class,
                "duration_ms": elapsed_ms,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )
        raise
    except TimeoutError:
        elapsed_ms = max(0, int((clock() - start) * 1000))
        logger.warning(
            "llm_review",
            extra={
                "status": "llm_error",
                "error_class": "timeout",
                "duration_ms": elapsed_ms,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )
        raise LlmError("timeout") from None
    except OSError:
        elapsed_ms = max(0, int((clock() - start) * 1000))
        logger.warning(
            "llm_review",
            extra={
                "status": "llm_error",
                "error_class": "connection_error",
                "duration_ms": elapsed_ms,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )
        raise LlmError("connection_error") from None
    except ValueError:
        # Request-construction fault: `http.client.putheader` rejects
        # control characters in the Authorization value with a ValueError
        # whose message EMBEDS the full header value (`Bearer <API KEY>`).
        # `from None` (like the decode fault above) keeps key material out
        # of tracebacks; the fixed `error_class` keeps it out of logs.
        # Never include the ValueError text in any message or log field.
        elapsed_ms = max(0, int((clock() - start) * 1000))
        logger.warning(
            "llm_review",
            extra={
                "status": "llm_error",
                "error_class": "invalid_key",
                "duration_ms": elapsed_ms,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )
        raise LlmError("invalid_key") from None
    except http.client.HTTPException:
        # Transport faults escaping the typed handlers above (BadStatusLine,
        # IncompleteRead). Ordered AFTER `OSError` deliberately:
        # RemoteDisconnected subclasses both, and its pre-existing OSError
        # path owns it — this arm covers only the pure-HTTPException rest.
        elapsed_ms = max(0, int((clock() - start) * 1000))
        logger.warning(
            "llm_review",
            extra={
                "status": "llm_error",
                "error_class": "connection_error",
                "duration_ms": elapsed_ms,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        )
        raise LlmError("connection_error") from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001, S110 (best-effort close on every path)
                pass
    elapsed_ms = max(0, int((clock() - start) * 1000))
    logger.info(
        "llm_review",
        extra={
            "status": "ok",
            "duration_ms": elapsed_ms,
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
        },
    )
    return result

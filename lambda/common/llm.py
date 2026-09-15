"""Provider chat-completions client (HLD §2.3 item 5, §2.7; FR-026; Constitution III).

Sends one `temperature: 0.2` chat-completions POST over stdlib
`http.client` — no model SDKs (Constitution II). Model, endpoint, and API
key arrive as injected parameters: their VALUES originate from SSM
hydration (`common.config`), but this module performs no SSM access itself —
the worker (T034) injects the hydrated values, keeping the client pure.

Timeout policy (HLD §2.3): connect 2 s, read 45 s. stdlib exposes a single
socket timeout, so the connection is opened with `timeout=CONNECT_TIMEOUT_S`
and, once connected, the socket is switched to `READ_TIMEOUT_S` for the
response read. Both constants are exported for tests to pin.

Error semantics: HTTP errors, timeouts, connection failures, and malformed
responses raise typed `LlmError` (machine-readable `error_class`) — never
swallowed, never `None`-as-success — so the queue owns the retry
(visibility redelivery, DLQ after `maxReceiveCount 5`).

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
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

TEMPERATURE = 0.2
CONNECT_TIMEOUT_S = 2
READ_TIMEOUT_S = 45

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
    `connection_error`, `http_{status}`, `invalid_response`, `invalid_key`.
    The message never carries prompt, diff, completion, or key material.
    """

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(f"llm request failed: {error_class}")


@dataclass(frozen=True)
class ReviewResult:
    """Parsed completion: Markdown content plus provider-observed usage."""

    content: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


def _default_factory(host: str, port: int, *, timeout: int) -> http.client.HTTPSConnection:
    return http.client.HTTPSConnection(host, port, timeout=timeout)


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
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content:
        raise LlmError("invalid_response")
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
    )


def review_diff(
    *,
    api_key: str,
    model: str,
    endpoint: str,
    system_prompt: str,
    diff_text: str,
    allowed_hosts: frozenset[str] | None = None,
    _connection_factory: ConnectionFactory | None = None,
    _clock: Clock | None = None,
) -> ReviewResult:
    """Run one chat-completions review; raise `LlmError` on any failure.

    `endpoint` is the full HTTPS chat-completions URL from SSM hydration.
    `allowed_hosts` is the env-configured GLM host set threaded through by
    the worker; `None` (default) falls back to `GLM_ALLOWED_HOSTS` so
    existing callers behave exactly as before.
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
    # Thinking portability gate: GLM-only key (see GLM_ALLOWED_HOSTS) —
    # omitted for every other provider so non-GLM endpoints never see it.
    payload: dict[str, Any] = {
        "model": model,
        # Provider default runs GLM reasoning (~1.3K tokens, ~45s) before
        # answering, which makes the quickstart ≤15s comment bar (HLD
        # §2.2 (b)) unreachable and overruns READ_TIMEOUT_S — surfaced
        # live by the T035 acceptance run. Thinking-off measured 5-8s.
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": diff_text},
        ],
        "temperature": TEMPERATURE,
    }
    if model.startswith("glm") and host.lower() in (
        GLM_ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts
    ):
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
        # connected socket from the connect timeout to the read timeout.
        conn.sock.settimeout(READ_TIMEOUT_S)
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

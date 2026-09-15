"""Ingress Lambda entry point (HLD §2.1; contracts/ingress-webhook.md).

Security & dispatch gateway: the eight §2.1 responsibilities IN STRICT ORDER —
1. body normalization (413 pre-decode), 2. HMAC verify (401), 3. event-type
gate, 4. action filter incl. `reopened` [D1] + draft skip, 5. dedup GetItem,
6. SQS SendMessage (500 without marking), 7. PutItem `delivery:{guid}` with
7-day TTL, 8. empty-body 202.

Response contract: the body is always empty; the status code is the entire
contract (HLD §2.1 table).

Secret hydration (carry-forward ⑨): the HLD §5.1 ingress grant is singular
`ssm:GetParameter` on the webhook-secret parameter ONLY — ingress MUST NOT
use the worker-side batched `GetParameters` hydration (`common.config`).
The secret is fetched at cold start via singular `get_parameter`
(WithDecryption) and cached warm with a 30-minute refresh; no SSM call sits
on the 250 ms hot path after the first fetch.

Injection points (HLD §4.4 item 3 — parallel-safe, no module-global state in
tests): `handler(..., _ssm, _table, _sqs, _now, _secret)`. Production passes
nothing and boto3 clients are built lazily. An explicitly injected `_ssm`
always fetches (cache bypassed) so tests stay deterministic; an explicitly
injected `_secret` skips SSM entirely. The warm-container cache
(`_SECRET_CACHE`) is used ONLY on the uninjected production path; tests can
`reset_secret_cache()` if they ever touch it.

Ports (duck-typed boto3 shapes — fakes mirror these exactly):
- table: `get_item(Key={"pk": ...})` → `{"Item": ...}` or `{}`;
  `put_item(Item={...})`.
- sqs: `send_message(QueueUrl=..., MessageBody=...)`.
- ssm: `get_parameter(Name=..., WithDecryption=True)` →
  `{"Parameter": {"Value": ...}}`.

Pure stdlib + boto3. No secrets in logs or error text (Constitution III).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from common.config import WEBHOOK_SECRET_NAME
from common.envelope import EnvelopeError, validate_envelope
from common.logs import build_ingress_event, emit_ingress_event
from common.state import build_delivery_item, delivery_pk

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_048_576  # 1 MiB — enforced PRE-DECODE (HLD §2.1 step 1)

ALLOWED_ACTIONS = frozenset({"opened", "synchronize", "ready_for_review", "reopened"})

SECRET_TTL_SECONDS = 30 * 60  # warm-container refresh (HLD §2.1, mirrors §2.3 item 1)

_SIGNATURE_PREFIX = "sha256="

# Warm-container secret cache — production path only (see module docstring).
_SECRET_CACHE: dict[str, Any] = {"value": None, "fetched_at": 0.0}


class BodyTooLarge(ValueError):
    """Wire body exceeds 1 MiB — rejected before decode (HTTP 413)."""


def reset_secret_cache() -> None:
    """Drop the warm-container secret cache (tests / rotation drills)."""
    _SECRET_CACHE["value"] = None
    _SECRET_CACHE["fetched_at"] = 0.0


def get_header(headers: Any, name: str) -> str | None:
    """Case-insensitive header lookup (HLD §2.1 step 2)."""
    if not isinstance(headers, dict):
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == wanted:
            return value if isinstance(value, str) else None
    return None


def normalize_body(event: dict[str, Any]) -> bytes:
    """Step 1: 413 gate on the WIRE size, then base64-decode if flagged.

    The Content-Length header (when present and numeric) is checked first so
    an oversized delivery is rejected without touching the body; otherwise
    the raw wire length is checked — both BEFORE any decode. Returns the
    decoded raw bytes over which the HMAC is verified (never parsed JSON).
    """
    headers = event.get("headers") or {}
    wire = event.get("body") or ""
    if not isinstance(wire, str):
        wire = ""
    content_length = get_header(headers, "content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                raise BodyTooLarge(f"content-length {content_length} exceeds 1 MiB")
        except ValueError as exc:
            if isinstance(exc, BodyTooLarge):
                raise
            # Non-numeric Content-Length is untrusted garbage — ignore it and
            # fall through to the wire-length gate below.
    if len(wire.encode("utf-8")) > MAX_BODY_BYTES:
        raise BodyTooLarge("body exceeds 1 MiB")
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(wire, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64 body") from exc
    return wire.encode("utf-8")


def verify_signature(secret: str | bytes, raw: bytes, provided: Any) -> bool:
    """Step 2: full-string constant-time compare (HLD §2.1 step 2, mode 2).

    Compares the COMPLETE `"sha256=" + hexdigest` strings via a single
    `hmac.compare_digest` call — no prefix compare, no early-exit equality.
    Anything missing or malformed (non-str, wrong prefix, wrong length,
    uppercase hex) fails closed.
    """
    if not isinstance(provided, str) or not provided.startswith(_SIGNATURE_PREFIX):
        return False
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    expected = _SIGNATURE_PREFIX + hmac.new(key, raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


def _fetch_secret(ssm: Any, secret_name: str) -> str:
    """Singular `get_parameter` hydration — never batched (see ⑨ above)."""
    response = ssm.get_parameter(Name=secret_name, WithDecryption=True)
    return response["Parameter"]["Value"]


def get_webhook_secret(
    ssm: Any,
    secret_name: str = WEBHOOK_SECRET_NAME,
    now: float | None = None,
) -> str:
    """Cold-start fetch + 30-minute warm-container cache (HLD §2.1)."""
    current = time.time() if now is None else now
    cached = _SECRET_CACHE["value"]
    if cached is not None and (current - _SECRET_CACHE["fetched_at"]) < SECRET_TTL_SECONDS:
        return cached
    secret = _fetch_secret(ssm, secret_name)
    _SECRET_CACHE["value"] = secret
    _SECRET_CACHE["fetched_at"] = current
    return secret


def build_envelope_body(payload: Any, delivery_guid: str) -> str | None:
    """Extract + validate the SQS envelope; None when schema-invalid (→ 200).

    HMAC-valid but unparseable/schema-invalid bodies are permanent failures:
    discarded with 200 — a 4xx would trigger pointless GitHub redelivery.
    """
    if not isinstance(payload, dict):
        return None
    action = payload.get("action")
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        return None
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return None
    if pull_request.get("draft") is True:
        return None
    try:
        repository = payload.get("repository")
        if not isinstance(repository, dict):
            repository = {}
        head = pull_request.get("head")
        if not isinstance(head, dict):
            head = {}
        base = pull_request.get("base")
        if not isinstance(base, dict):
            base = {}
        sender = payload.get("sender")
        if not isinstance(sender, dict):
            sender = {}
        pr_number = pull_request.get("number", payload.get("number"))
        candidate = {
            "envelope_version": "v1",
            "event_type": "pull_request",
            "action": action,
            "repo_full_name": repository.get("full_name"),
            "pr_number": pr_number,
            "head_sha": head.get("sha"),
            "base_sha": base.get("sha"),
            "sender": sender.get("login"),
            "delivery_guid": delivery_guid,
        }
        envelope = validate_envelope(candidate)
    except EnvelopeError:
        return None
    return json.dumps(asdict(envelope), separators=(",", ":"), sort_keys=True)


def _respond(status: int, *, decision: str, reason: str) -> dict[str, Any]:
    """Return the empty-body response and emit one structured ingress status
    line (G6-F3, SPR-62) via `common.logs` — the single logging path.

    Every disposition logs (`statusCode` + `decision` + `reason`): the
    ingress-401-spike alarm's metric filter matches
    `{ $.statusCode = 401 }`, so 401 outcomes now carry signal instead of
    leaving the alarm knowingly quiet. Emission is best-effort and never
    masks the disposition.
    """
    try:
        emit_ingress_event(
            lambda line: print(line, end="", flush=True),
            build_ingress_event(status_code=status, decision=decision, reason=reason),
        )
    except Exception:  # observability must not mask the disposition
        logger.warning("ingress_status_failed", extra={"status": "ingress_status_failed"})
    return {"statusCode": status, "body": ""}


def handler(
    event: dict[str, Any],
    context: Any = None,
    *,
    _ssm: Any = None,
    _table: Any = None,
    _sqs: Any = None,
    _now: Callable[[], float] | None = None,
    _secret: str | None = None,
) -> dict[str, Any]:
    """Function-URL proxy event → HTTP response (HLD §2.1, strict order)."""
    # --- step 1: body normalization (413 pre-decode) -------------------------
    try:
        raw = normalize_body(event)
    except BodyTooLarge:
        return _respond(413, decision="rejected", reason="body_too_large")
    except ValueError:
        # Undecodable wire bytes: authenticity cannot be established → 401.
        return _respond(401, decision="rejected", reason="undecodable_body")

    headers = event.get("headers") or {}
    signature = get_header(headers, "x-hub-signature-256")
    event_type = get_header(headers, "x-github-event")
    delivery_guid = get_header(headers, "x-github-delivery")

    # --- secret (cold-start fetch, warm cache; injected secret bypasses) -----
    secret_name = os.environ.get("WEBHOOK_SECRET_NAME", WEBHOOK_SECRET_NAME)
    if _secret is not None:
        secret = _secret
    elif _ssm is not None:
        try:
            secret = _fetch_secret(_ssm, secret_name)  # tests: always fetch
        except Exception:
            return _respond(500, decision="error", reason="secret_unavailable")
    else:
        import boto3  # deferred: import-time must not require credentials

        try:
            secret = get_webhook_secret(boto3.client("ssm"), secret_name)
        except Exception:
            return _respond(500, decision="error", reason="secret_unavailable")

    # --- step 2: HMAC (missing/malformed signature or event headers → 401) ---
    if not signature or not event_type or not delivery_guid:
        return _respond(401, decision="rejected", reason="missing_auth_headers")
    if not verify_signature(secret, raw, signature):
        return _respond(401, decision="rejected", reason="bad_signature")

    # --- step 3: event-type gate (signed non-PR event → 200 discard) ---------
    if event_type != "pull_request":
        return _respond(200, decision="discarded", reason="non_pr_event")

    # --- step 4: action filter + draft skip + schema validation → 200 -------
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _respond(200, decision="discarded", reason="unparseable_body")
    envelope_body = build_envelope_body(payload, delivery_guid)
    if envelope_body is None:
        return _respond(200, decision="discarded", reason="not_actionable")

    # --- clients (lazy production wiring; injected doubles in tests) --------
    table = _table
    sqs = _sqs
    if table is None or sqs is None:
        import boto3  # deferred: import-time must not require credentials

        if table is None:
            table_name = os.environ.get("STATE_TABLE_NAME", "pr-reviewer-state")
            table = boto3.resource("dynamodb").Table(table_name)
        if sqs is None:
            sqs = boto3.client("sqs")
    queue_url = os.environ.get("WORK_QUEUE_URL", "")
    clock = _now if _now is not None else time.time

    # --- step 5: dedup check, read-only GetItem (seen GUID → 200 no-op) ------
    delivery_key = delivery_pk(delivery_guid)
    try:
        existing = table.get_item(Key={"pk": delivery_key})
    except Exception:
        return _respond(500, decision="error", reason="dedup_unavailable")
    seen = existing.get("Item") if isinstance(existing, dict) else existing
    if seen is not None:
        return _respond(200, decision="discarded", reason="duplicate_delivery")

    # --- step 6: durable dispatch (failure → 500 WITHOUT marking) -----------
    try:
        sqs.send_message(QueueUrl=queue_url, MessageBody=envelope_body)
    except Exception:
        return _respond(500, decision="error", reason="dispatch_failed")

    # --- step 7: mark processed (PutItem delivery:{guid}, 7-day TTL) ---------
    try:
        table.put_item(Item=build_delivery_item(delivery_guid, int(clock())))
    except Exception:
        return _respond(500, decision="error", reason="mark_failed")

    # --- step 8: fast acknowledgment, empty-body 202 -------------------------
    return _respond(202, decision="allowed", reason="enqueued")

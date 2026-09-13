"""SQS envelope schema + validator (contracts/ingress-webhook.md, HLD §2.1).

Single source of truth for the envelope contract: the worker treats every
SQS message as untrusted input and validates it here before any use.
Malformed input raises a typed `EnvelopeError` (a `ValueError`) carrying a
machine-readable `field` and `reason` — never a bare `Exception`.

Pure stdlib, no I/O.
"""

import re
import uuid
from dataclasses import dataclass
from typing import Any

ENVELOPE_VERSION = "v1"
EVENT_TYPE = "pull_request"

ALLOWED_ACTIONS = frozenset({"opened", "synchronize", "ready_for_review", "reopened"})

MAX_REPO_LENGTH = 128
MAX_PR_NUMBER = 10**9
MAX_SENDER_LENGTH = 64
MAX_GUID_LENGTH = 64

# Anchors are \Z, not $: Python $ also matches before a trailing newline,
# which would admit single-newline variants of validated identity fields.
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
_SENDER_RE = re.compile(r"^[A-Za-z0-9-]+\Z")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


class EnvelopeError(ValueError):
    """Typed envelope rejection: `field` names the offending field
    (`"envelope"` for whole-payload shape errors), `reason` is a
    machine-readable code (`missing`, `not_object`, `bad_version`,
    `bad_event`, `bad_action`, `bad_repo`, `bad_pr_number`, `bad_sha`,
    `bad_sender`, `bad_guid`)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid envelope: {field}: {reason}")


@dataclass(frozen=True)
class Envelope:
    """Validated SQS envelope — typed schema per contracts/ingress-webhook.md."""

    envelope_version: str
    event_type: str
    action: str
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str
    sender: str
    delivery_guid: str


def _require_field(payload: dict[str, Any], name: str) -> Any:
    if name not in payload:
        raise EnvelopeError(name, "missing")
    return payload[name]


def validate_envelope(payload: Any) -> Envelope:
    """Validate an untrusted SQS message body against the envelope schema.

    Returns a frozen `Envelope` on success; raises `EnvelopeError` otherwise.
    Unknown extra fields are ignored (additive evolution).
    """
    if not isinstance(payload, dict):
        raise EnvelopeError("envelope", "not_object")

    version = _require_field(payload, "envelope_version")
    if not isinstance(version, str) or version != ENVELOPE_VERSION:
        raise EnvelopeError("envelope_version", "bad_version")

    event = _require_field(payload, "event_type")
    if not isinstance(event, str) or event != EVENT_TYPE:
        raise EnvelopeError("event_type", "bad_event")

    action = _require_field(payload, "action")
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise EnvelopeError("action", "bad_action")

    repo = _require_field(payload, "repo_full_name")
    if not isinstance(repo, str) or len(repo) > MAX_REPO_LENGTH or not _REPO_RE.match(repo):
        raise EnvelopeError("repo_full_name", "bad_repo")

    number = _require_field(payload, "pr_number")
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= MAX_PR_NUMBER:
        raise EnvelopeError("pr_number", "bad_pr_number")

    heads: dict[str, str] = {}
    for name in ("head_sha", "base_sha"):
        sha = _require_field(payload, name)
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise EnvelopeError(name, "bad_sha")
        heads[name] = sha

    sender = _require_field(payload, "sender")
    if (
        not isinstance(sender, str)
        or len(sender) > MAX_SENDER_LENGTH
        or not _SENDER_RE.match(sender)
    ):
        raise EnvelopeError("sender", "bad_sender")

    guid = _require_field(payload, "delivery_guid")
    if not isinstance(guid, str) or len(guid) > MAX_GUID_LENGTH:
        raise EnvelopeError("delivery_guid", "bad_guid")
    if not _UUID_RE.match(guid):
        raise EnvelopeError("delivery_guid", "bad_guid")
    try:
        uuid.UUID(guid)
    except ValueError:
        raise EnvelopeError("delivery_guid", "bad_guid") from None

    return Envelope(
        envelope_version=version,
        event_type=event,
        action=action,
        repo_full_name=repo,
        pr_number=number,
        head_sha=heads["head_sha"],
        base_sha=heads["base_sha"],
        sender=sender,
        delivery_guid=guid,
    )

"""Structured JSON logger + redaction guard (HLD §5.4, §4.3; FR-026; Constitution III).

Fixed field set only: every emitted line is one JSON object carrying exactly
`FIXED_FIELDS` — the HLD §5.4 "Logged" row (IDs, SHAs, durations, token
usage, status, error class) plus the §4.3 review-metric identifiers
`generation` and `prompt_version` and the §4.3 `stale_discarded` discard
metric (True when the run discarded superseded/stale work via fencing,
False otherwise — strict bool, never None, so the metric is always
queryable). `build_event()` takes those fields as
explicit keyword-only parameters, so non-fixed content (Authorization
headers, PATs, secrets, raw payloads, diffs, LLM request/response bodies) is
structurally unemittable — there is no parameter that could carry it
(`TypeError` on anything else), and `emit()` rejects any event whose keys
differ from the allow-list.

Defense in depth, the redaction guard (`assert_clean`, run over every string
value at build time and over the final serialized line at emit time) refuses
with a typed `RedactionError` — nothing is emitted — when credential-shaped
or payload-shaped text is detected. `event_from_envelope()` accepts an
envelope-shaped dict (or `Envelope`) and extracts only the allow-listed IDs;
hostile extra keys are never read. GitHub-derived fields are
control-character-stripped before logging (HLD §2.1); control characters in
format-checked free fields are rejected instead — inputs arrive envelope-
validated, so fail-closed beats silent mutation there.

Malformed input raises a typed `LogsError` (a `ValueError`) carrying a
machine-readable `field` and `reason` — never a bare `Exception`.

Sinks are injected (`emit(sink, event)` takes any `Callable[[str], None]`;
CloudWatch destinations per HLD §4.3) so tests stay I/O-free.

Pure stdlib, no I/O, no boto3 import.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from typing import Any

from common.envelope import Envelope, EnvelopeError, validate_envelope

FIXED_FIELDS = frozenset(
    {
        "repo_full_name",
        "pr_number",
        "head_sha",
        "delivery_guid",
        "generation",
        "duration_ms",
        "token_usage",
        "status",
        "error_class",
        "stale_discarded",
        "prompt_version",
    }
)

Sink = Callable[[str], None]

# Identifier formats mirror common.envelope (the shape authority, HLD §2.1);
# re-checked here so the logger fails closed before emission.
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_STATUS_RE = re.compile(r"^[a-z0-9_]{1,64}\Z")
_ERROR_CLASS_RE = re.compile(r"^[A-Za-z0-9_.]{1,128}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

MAX_REPO_LENGTH = 128
MAX_PR_NUMBER = 10**9
MAX_GUID_LENGTH = 64
MAX_PROMPT_VERSION_LENGTH = 64

# Credential-shaped and payload-shaped text that must never reach a log line.
_FORBIDDEN_RES = (
    re.compile(r"authorization", re.IGNORECASE),
    re.compile(r"bearer", re.IGNORECASE),
    re.compile(r"x-hub-signature", re.IGNORECASE),
    re.compile(r"sha256="),
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"gho_[A-Za-z0-9]{8,}"),
    re.compile(r"ghu_[A-Za-z0-9]{8,}"),
    re.compile(r"ghs_[A-Za-z0-9]{8,}"),
    re.compile(r"ghr_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"diff --git"),
    re.compile(r"@@"),
)


class LogsError(ValueError):
    """Typed log rejection: `field` names the offending field
    (`"envelope"` for whole-payload shape errors, `"event"` for
    wrong-shaped events handed to `emit`), `reason` is a
    machine-readable code (`not_object`, `missing`, `bad_fields`,
    `bad_repo`, `bad_pr_number`, `bad_sha`, `bad_guid`,
    `bad_generation`, `bad_duration`, `bad_token_usage`, `bad_status`,
    `bad_error_class`, `bad_stale_discarded`, `bad_prompt_version`)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid log event: {field}: {reason}")


class RedactionError(ValueError):
    """Guard refusal: forbidden content was detected and nothing was
    emitted. `field` names the offending field (`"output"` for the final
    serialized line); `reason` is always `forbidden_content`. The message
    never carries the offending text."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"refused to log: {field}: {reason}")


def assert_clean(text: str, field: str = "output") -> None:
    """Scan `text` for forbidden content (HLD §5.4; Constitution III).

    Raises `RedactionError` on the first credential-shaped or
    payload-shaped match; returns `None` when clean.
    """
    for pattern in _FORBIDDEN_RES:
        if pattern.search(text):
            raise RedactionError(field, "forbidden_content")


def _strip_controls(value: str) -> str:
    return _CONTROL_RE.sub("", value)


def _clean_repo(value: Any) -> str:
    if not isinstance(value, str):
        raise LogsError("repo_full_name", "bad_repo")
    repo = _strip_controls(value)
    if len(repo) > MAX_REPO_LENGTH or not _REPO_RE.match(repo):
        raise LogsError("repo_full_name", "bad_repo")
    assert_clean(repo, field="repo_full_name")
    return repo


def _clean_pr_number(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_PR_NUMBER:
        raise LogsError("pr_number", "bad_pr_number")
    return value


def _clean_sha(value: Any) -> str:
    if not isinstance(value, str):
        raise LogsError("head_sha", "bad_sha")
    sha = _strip_controls(value)
    if not _SHA_RE.match(sha):
        raise LogsError("head_sha", "bad_sha")
    return sha


def _clean_guid(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_GUID_LENGTH:
        raise LogsError("delivery_guid", "bad_guid")
    guid = _strip_controls(value)
    if not _UUID_RE.match(guid):
        raise LogsError("delivery_guid", "bad_guid")
    try:
        uuid.UUID(guid)
    except ValueError:
        raise LogsError("delivery_guid", "bad_guid") from None
    return guid


def _clean_generation(value: Any) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LogsError("generation", "bad_generation")
    return value


def _clean_count(value: Any, field: str, reason: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LogsError(field, reason)
    return value


def _clean_status(value: Any) -> str:
    if not isinstance(value, str) or not _STATUS_RE.match(value):
        raise LogsError("status", "bad_status")
    assert_clean(value, field="status")
    return value


def _clean_error_class(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ERROR_CLASS_RE.match(value):
        raise LogsError("error_class", "bad_error_class")
    assert_clean(value, field="error_class")
    return value


def _clean_stale_discarded(value: Any) -> bool:
    if not isinstance(value, bool):
        raise LogsError("stale_discarded", "bad_stale_discarded")
    return value


def _clean_prompt_version(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LogsError("prompt_version", "bad_prompt_version")
    version = _strip_controls(value)
    if not version or len(version) > MAX_PROMPT_VERSION_LENGTH:
        raise LogsError("prompt_version", "bad_prompt_version")
    assert_clean(version, field="prompt_version")
    return version


def build_event(
    *,
    repo_full_name: Any,
    pr_number: Any,
    head_sha: Any,
    delivery_guid: Any,
    duration_ms: Any,
    token_usage: Any,
    status: Any,
    error_class: Any = None,
    generation: Any = None,
    stale_discarded: Any = False,
    prompt_version: Any = None,
) -> dict[str, Any]:
    """Build a fixed-field log event.

    Keyword-only parameters ARE the allow-list: there is no slot for
    headers, secrets, payloads, diffs, or LLM bodies, so callers cannot
    smuggle them in (unknown keywords raise `TypeError`). Every value is
    format-checked (`LogsError`) and guard-scanned (`RedactionError`); the
    returned dict carries exactly `FIXED_FIELDS`, with `None` defaults for
    the optional `error_class`, `generation`, and `prompt_version`, and
    `False` for `stale_discarded` (runs that did not discard stale work).
    """
    return {
        "repo_full_name": _clean_repo(repo_full_name),
        "pr_number": _clean_pr_number(pr_number),
        "head_sha": _clean_sha(head_sha),
        "delivery_guid": _clean_guid(delivery_guid),
        "generation": _clean_generation(generation),
        "duration_ms": _clean_count(duration_ms, "duration_ms", "bad_duration"),
        "token_usage": _clean_count(token_usage, "token_usage", "bad_token_usage"),
        "status": _clean_status(status),
        "error_class": _clean_error_class(error_class),
        "stale_discarded": _clean_stale_discarded(stale_discarded),
        "prompt_version": _clean_prompt_version(prompt_version),
    }


def event_from_envelope(
    payload: Any,
    *,
    duration_ms: Any,
    token_usage: Any,
    status: Any,
    error_class: Any = None,
    generation: Any = None,
    stale_discarded: Any = False,
    prompt_version: Any = None,
) -> dict[str, Any]:
    """Build a fixed-field event from an envelope-shaped dict or `Envelope`.

    Only the allow-listed IDs are extracted; every other key — Authorization
    headers, secrets, raw payloads, diffs, LLM bodies — is never read.
    Dict input is validated by `common.envelope` (single implementation,
    HLD §7.1); `EnvelopeError` is mapped to `LogsError` preserving
    `field`/`reason`.
    """
    if isinstance(payload, Envelope):
        envelope = payload
    else:
        try:
            envelope = validate_envelope(payload)
        except EnvelopeError as exc:
            raise LogsError(exc.field, exc.reason) from exc
    return build_event(
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
        prompt_version=prompt_version,
    )


def emit(sink: Sink, event: Any) -> None:
    """Serialize one fixed-field event as a single JSON line to `sink`.

    The event must carry exactly `FIXED_FIELDS`; anything else is rejected
    with `LogsError` and nothing reaches the sink. The serialized line is
    guard-scanned before delivery: forbidden content raises
    `RedactionError` with nothing delivered.
    """
    if not isinstance(event, dict):
        raise LogsError("event", "not_object")
    if set(event) != FIXED_FIELDS:
        raise LogsError("event", "bad_fields")
    line = (
        json.dumps(
            {key: event[key] for key in sorted(FIXED_FIELDS)},
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    )
    assert_clean(line)
    sink(line)

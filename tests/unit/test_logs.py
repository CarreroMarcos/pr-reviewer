"""T015: log redaction + fixed field-set contract (HLD §5.4; FR-026; Constitution III).

Payload-shaped inputs (envelope-like dicts, optionally carrying hostile extras
such as Authorization headers, PAT shapes, secrets, raw payloads, diffs, or
LLM request/response bodies) must yield emitted lines containing none of the
forbidden substrings. Every emitted line carries exactly the fixed field set
(IDs, SHAs, durations, token usage, status, error class) — the logger builds
events from an allow-list of fields, so non-fixed content is structurally
unemittable.
"""

import json
import logging
import uuid

import pytest

from common.envelope import validate_envelope
from common.logs import (
    FIXED_FIELDS,
    INGRESS_FIXED_FIELDS,
    LogsError,
    RedactionError,
    assert_clean,
    build_event,
    build_ingress_event,
    emit,
    emit_ingress_event,
    event_from_envelope,
    prompt_sha256,
)

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "fedcba9876543210fedcba9876543210fedcba98"
REPO = "octo-org/hello-world"

EXPECTED_FIELDS = frozenset(
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
        "failure_notice_published",
        "prompt_version",
        "prompt_sha256",
    }
)

FAKE_PAT = "ghp_" + "A" * 36  # noqa: S105 (fake fixture, not a credential)
FAKE_FINE_PAT = "github_pat_" + "B" * 22  # noqa: S105 (fake fixture)
FAKE_WEBHOOK_SECRET = "whsec-test-secret-value"  # noqa: S105 (fake fixture)
FAKE_GLM_KEY = "glm-test-key-value"  # noqa: S105 (fake fixture)
AUTH_VALUE = "Bearer " + FAKE_PAT  # noqa: S105 (fake header fixture)
SIGNATURE_VALUE = "sha256=" + "d" * 64  # noqa: S105 (fake fixture)

RAW_PAYLOAD = json.dumps(
    {
        "action": "opened",
        "pull_request": {
            "number": 42,
            "head": {"sha": HEAD_SHA},
            "title": "CANARY-PR-TITLE",
        },
        "repository": {"full_name": REPO},
    }
)
DIFF_TEXT = (
    "diff --git a/review.py b/review.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/review.py\n"
    "+++ b/review.py\n"
    "@@ -1,2 +1,3 @@\n"
    " context\n"
    "+CANARY-DIFF-LINE\n"
)
LLM_REQUEST = json.dumps(
    {
        "model": "glm-5.3-flash",
        "messages": [{"role": "system", "content": "CANARY-SYSTEM-PROMPT-TEXT"}],
    }
)
LLM_RESPONSE = json.dumps(
    {"choices": [{"message": {"role": "assistant", "content": "CANARY-MODEL-COMPLETION"}}]}
)

HOSTILE_EXTRAS = {
    "Authorization": AUTH_VALUE,
    "authorization": AUTH_VALUE,
    "github_token": FAKE_PAT,
    "fine_grained_pat": FAKE_FINE_PAT,
    "webhook_secret": FAKE_WEBHOOK_SECRET,
    "glm_api_key": FAKE_GLM_KEY,
    "X-Hub-Signature-256": SIGNATURE_VALUE,
    "raw_payload": RAW_PAYLOAD,
    "body": RAW_PAYLOAD,
    "diff": DIFF_TEXT,
    "llm_request": LLM_REQUEST,
    "llm_response": LLM_RESPONSE,
}

# Every hostile token that must never appear in any emitted log line (FR-026).
FORBIDDEN = [
    "Authorization",
    "authorization",
    "Bearer",
    "bearer",
    "ghp_",
    "github_pat_",
    FAKE_WEBHOOK_SECRET,
    FAKE_GLM_KEY,
    "X-Hub-Signature-256",
    "x-hub-signature",
    "sha256=",
    "diff --git",
    "@@",
    '"pull_request":',
    '"repository":',
    '"messages":',
    '"choices":',
    "CANARY-PR-TITLE",
    "CANARY-DIFF-LINE",
    "CANARY-SYSTEM-PROMPT-TEXT",
    "CANARY-MODEL-COMPLETION",
]


def _envelope(**overrides):
    payload = {
        "envelope_version": "v1",
        "event_type": "pull_request",
        "action": "opened",
        "repo_full_name": REPO,
        "pr_number": 42,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "sender": "octocat",
        "delivery_guid": str(uuid.uuid4()),
    }
    payload.update(overrides)
    return payload


def _metrics(**overrides):
    metrics = {"duration_ms": 1234, "token_usage": 5678, "status": "ok"}
    metrics.update(overrides)
    return metrics


def _emit_lines(payload, **metrics):
    lines = []
    event = event_from_envelope(payload, **_metrics(**metrics))
    emit(lines.append, event)
    return lines


def _assert_no_forbidden(lines):
    assert lines, "expected at least one emitted line"
    blob = "\n".join(lines)
    for token in FORBIDDEN:
        assert token not in blob, f"forbidden substring in logs: {token!r}"


# --- fixed field set -------------------------------------------------------


def test_fixed_field_set_is_pinned():
    assert FIXED_FIELDS == EXPECTED_FIELDS


def test_build_event_carries_exactly_fixed_fields():
    event = build_event(
        repo_full_name=REPO,
        pr_number=42,
        head_sha=HEAD_SHA,
        delivery_guid=str(uuid.uuid4()),
        **_metrics(),
    )
    assert set(event) == FIXED_FIELDS
    assert event["repo_full_name"] == REPO
    assert event["pr_number"] == 42
    assert event["head_sha"] == HEAD_SHA
    assert event["duration_ms"] == 1234
    assert event["token_usage"] == 5678
    assert event["status"] == "ok"
    assert event["error_class"] is None
    assert event["generation"] is None
    assert event["stale_discarded"] is False  # default: no stale work discarded
    assert event["failure_notice_published"] == "false"  # default: no notice published
    assert event["prompt_version"] is None
    assert event["prompt_sha256"] is None


def test_build_event_carries_optional_fields():
    event = build_event(
        repo_full_name=REPO,
        pr_number=42,
        head_sha=HEAD_SHA,
        delivery_guid=str(uuid.uuid4()),
        duration_ms=9000,
        token_usage=120001,
        status="llm_error",
        error_class="TimeoutError",
        generation=3,
        stale_discarded=True,
        failure_notice_published="true",
        prompt_version="v3",
        prompt_sha256="ab" * 32,
    )
    assert set(event) == FIXED_FIELDS
    assert event["error_class"] == "TimeoutError"
    assert event["generation"] == 3
    assert event["stale_discarded"] is True
    assert event["failure_notice_published"] == "true"
    assert event["prompt_version"] == "v3"
    assert event["prompt_sha256"] == "ab" * 32


def test_emit_writes_single_json_line_with_fixed_keys():
    lines = []
    event = build_event(
        repo_full_name=REPO,
        pr_number=42,
        head_sha=HEAD_SHA,
        delivery_guid=str(uuid.uuid4()),
        **_metrics(),
    )
    emit(lines.append, event)
    assert len(lines) == 1
    assert lines[0].endswith("\n")
    assert lines[0].count("\n") == 1
    assert set(json.loads(lines[0])) == set(FIXED_FIELDS)


def test_build_event_rejects_unknown_fields():
    kwargs = {
        "repo_full_name": REPO,
        "pr_number": 42,
        "head_sha": HEAD_SHA,
        "delivery_guid": str(uuid.uuid4()),
        "duration_ms": 1,
        "token_usage": 2,
        "status": "ok",
        "diff": DIFF_TEXT,
    }
    with pytest.raises(TypeError):
        build_event(**kwargs)


def test_emit_rejects_non_fixed_event_shape():
    lines = []
    event = build_event(
        repo_full_name=REPO,
        pr_number=42,
        head_sha=HEAD_SHA,
        delivery_guid=str(uuid.uuid4()),
        **_metrics(),
    )
    event["diff"] = DIFF_TEXT
    with pytest.raises(LogsError) as excinfo:
        emit(lines.append, event)
    assert excinfo.value.field == "event"
    assert excinfo.value.reason == "bad_fields"
    assert lines == []


# --- forbidden-substring matrix (FR-026) -----------------------------------


@pytest.mark.parametrize("key", sorted(HOSTILE_EXTRAS))
def test_hostile_extra_key_never_emitted(key):
    payload = _envelope()
    payload[key] = HOSTILE_EXTRAS[key]
    _assert_no_forbidden(_emit_lines(payload))


def test_full_hostile_envelope_never_emitted():
    payload = _envelope()
    payload.update(HOSTILE_EXTRAS)
    lines = _emit_lines(payload)
    _assert_no_forbidden(lines)
    assert set(json.loads(lines[0])) == set(FIXED_FIELDS)


def test_event_from_envelope_accepts_validated_envelope_object():
    envelope = validate_envelope(_envelope())
    lines = _emit_lines(envelope)
    _assert_no_forbidden(lines)
    assert json.loads(lines[0])["pr_number"] == 42


def test_control_characters_never_reach_output():
    payload = _envelope()
    payload["X-Trace"] = "line1\r\nline2 injected"
    lines = _emit_lines(payload)
    _assert_no_forbidden(lines)
    assert "\r" not in lines[0]
    assert lines[0].count("\n") == 1


# --- redaction guard --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer ghp_" + "A" * 36,
        "bearer token here",
        "X-Hub-Signature-256: sha256=" + "d" * 64,
        "key=ghp_" + "A" * 36,
        "github_pat_" + "B" * 22,
        "diff --git a/f b/f",
        "@@ -1,2 +1,3 @@",
    ],
)
def test_guard_rejects_forbidden_text(text):
    with pytest.raises(RedactionError):
        assert_clean(text)


def test_guard_passes_clean_text():
    assert assert_clean("repo=octo-org/hello-world status=ok") is None


def test_secret_smuggled_in_identifier_trips_guard():
    payload = _envelope(repo_full_name="octo-org/" + FAKE_PAT)
    with pytest.raises(RedactionError):
        event_from_envelope(payload, **_metrics())


def test_emit_scans_final_serialized_line():
    lines = []
    event = build_event(
        repo_full_name=REPO,
        pr_number=42,
        head_sha=HEAD_SHA,
        delivery_guid=str(uuid.uuid4()),
        **_metrics(),
    )
    tampered = dict(event)
    tampered["status"] = "bearer"
    with pytest.raises(RedactionError) as excinfo:
        emit(lines.append, tampered)
    # The emit-time value re-run names the offending field (not "output").
    assert excinfo.value.field == "status"
    assert lines == []


# --- typed rejections -------------------------------------------------------


def test_malformed_envelope_rejected_with_typed_error():
    with pytest.raises(LogsError) as excinfo:
        event_from_envelope({"bogus": True}, **_metrics())
    assert excinfo.value.field == "envelope_version"
    assert excinfo.value.reason == "missing"

    with pytest.raises(LogsError) as excinfo:
        event_from_envelope("not-a-dict", **_metrics())
    assert excinfo.value.field == "envelope"
    assert excinfo.value.reason == "not_object"


@pytest.mark.parametrize(
    ("override", "field", "reason"),
    [
        ({"repo_full_name": "owneronly"}, "repo_full_name", "bad_repo"),
        ({"pr_number": 0}, "pr_number", "bad_pr_number"),
        ({"head_sha": "A" * 40}, "head_sha", "bad_sha"),
        ({"delivery_guid": "not-a-uuid"}, "delivery_guid", "bad_guid"),
    ],
)
def test_bad_identifiers_rejected(override, field, reason):
    with pytest.raises(LogsError) as excinfo:
        event_from_envelope(_envelope(**override), **_metrics())
    assert excinfo.value.field == field
    assert excinfo.value.reason == reason


@pytest.mark.parametrize(
    ("kwargs", "field", "reason"),
    [
        ({"status": ""}, "status", "bad_status"),
        ({"status": "HAS SPACE"}, "status", "bad_status"),
        ({"status": "ok\n"}, "status", "bad_status"),
        ({"status": "x" * 65}, "status", "bad_status"),
        ({"duration_ms": -1}, "duration_ms", "bad_duration"),
        ({"duration_ms": True}, "duration_ms", "bad_duration"),
        ({"duration_ms": "5"}, "duration_ms", "bad_duration"),
        ({"token_usage": -1}, "token_usage", "bad_token_usage"),
        ({"generation": -1}, "generation", "bad_generation"),
        ({"error_class": ""}, "error_class", "bad_error_class"),
    ],
)
def test_bad_metrics_rejected(kwargs, field, reason):
    with pytest.raises(LogsError) as excinfo:
        event_from_envelope(_envelope(), **_metrics(**kwargs))
    assert excinfo.value.field == field
    assert excinfo.value.reason == reason


def test_error_types_are_value_errors():
    assert issubclass(LogsError, ValueError)
    assert issubclass(RedactionError, ValueError)


# --- QA-A: build_event validation (every cleaner, public seam) ----------------


def _valid_kwargs(**overrides):
    kwargs = {
        "repo_full_name": REPO,
        "pr_number": 42,
        "head_sha": HEAD_SHA,
        "delivery_guid": str(uuid.uuid4()),
        "duration_ms": 1234,
        "token_usage": 5678,
        "status": "ok",
    }
    kwargs.update(overrides)
    return kwargs


def test_build_event_rejects_non_string_repo():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(repo_full_name=123))
    assert excinfo.value.field == "repo_full_name"
    assert excinfo.value.reason == "bad_repo"


def test_build_event_rejects_overlong_repo():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(repo_full_name="a/" + "b" * 127))
    assert excinfo.value.field == "repo_full_name"
    assert excinfo.value.reason == "bad_repo"


def test_build_event_rejects_unicode_homoglyph_repo():
    # Given a Cyrillic-о lookalike owner Then rejected: the charset is ASCII-only
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(repo_full_name="оcto-org/hello-world"))
    assert excinfo.value.field == "repo_full_name"
    assert excinfo.value.reason == "bad_repo"


def test_build_event_rejects_bad_pr_number():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(pr_number=0))
    assert excinfo.value.field == "pr_number"
    assert excinfo.value.reason == "bad_pr_number"


def test_build_event_rejects_non_string_sha():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(head_sha=1234567890))
    assert excinfo.value.field == "head_sha"
    assert excinfo.value.reason == "bad_sha"


def test_build_event_rejects_malformed_sha():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(head_sha="A" * 40))
    assert excinfo.value.field == "head_sha"
    assert excinfo.value.reason == "bad_sha"


def test_build_event_rejects_non_string_guid():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(delivery_guid=123))
    assert excinfo.value.field == "delivery_guid"
    assert excinfo.value.reason == "bad_guid"


def test_build_event_rejects_malformed_guid():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(delivery_guid="not-a-uuid"))
    assert excinfo.value.field == "delivery_guid"
    assert excinfo.value.reason == "bad_guid"


def test_build_event_accepts_uppercase_guid():
    # Given an uppercase-hex GUID Then accepted (identifier formats allow A-F)
    guid = str(uuid.uuid4()).upper()
    assert build_event(**_valid_kwargs(delivery_guid=guid))["delivery_guid"] == guid


def test_build_event_drops_non_string_prompt_version_with_warning(caplog):
    # Given a non-string prompt version Then the field drops to None with a
    # loud warning — never raised into the request path (SPR-62 hardening).
    with caplog.at_level(logging.WARNING, logger="common.logs"):
        event = build_event(**_valid_kwargs(prompt_version=123))
    assert event["prompt_version"] is None
    assert [r for r in caplog.records if r.getMessage() == "prompt_version_rejected"]


@pytest.mark.parametrize("version", ["", "v" * 65, "v1; DROP", "v1\n", "v 1", "Bearer abc123"])
def test_build_event_drops_off_charset_prompt_version_with_warning(caplog, version):
    # Given an empty, overlong, charset-violating, or hostile prompt version
    # Then the field drops to None with a loud warning carrying only a fixed
    # reason code — never the offending value, never a raise.
    with caplog.at_level(logging.WARNING, logger="common.logs"):
        event = build_event(**_valid_kwargs(prompt_version=version))
    assert event["prompt_version"] is None
    warnings = [r for r in caplog.records if r.getMessage() == "prompt_version_rejected"]
    assert warnings
    if version:
        for record in warnings:
            assert version not in str(record.__dict__.get("reason", ""))
            assert version not in caplog.text


@pytest.mark.parametrize("version", ["v1", "v3", "v1.2-rc_3"])
def test_build_event_passes_charset_clean_prompt_version(version):
    # Given a charset-clean code-identity version Then it passes through
    # untouched and no rejection warning fires.
    event = build_event(**_valid_kwargs(prompt_version=version))
    assert event["prompt_version"] == version


def test_build_event_rejects_true_generation():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(generation=True))
    assert excinfo.value.field == "generation"
    assert excinfo.value.reason == "bad_generation"


def test_build_event_rejects_non_bool_stale_discarded():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(stale_discarded="yes"))
    assert excinfo.value.field == "stale_discarded"
    assert excinfo.value.reason == "bad_stale_discarded"


def test_build_event_rejects_bad_failure_notice_published():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(failure_notice_published="maybe"))
    assert excinfo.value.field == "failure_notice_published"
    assert excinfo.value.reason == "bad_failure_notice"


def test_build_event_rejects_overlong_error_class():
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(error_class="E" * 129))
    assert excinfo.value.field == "error_class"
    assert excinfo.value.reason == "bad_error_class"


# --- QA-A: redaction under hostile field shapes --------------------------------


def test_pat_smuggled_in_error_class_trips_guard():
    # Given a PAT-shaped error class Then the guard refuses instead of emitting
    with pytest.raises(RedactionError):
        build_event(**_valid_kwargs(error_class="ghp_" + "A" * 36))


def test_bearer_smuggled_in_status_trips_guard():
    # Given a bearer-shaped status Then the guard refuses instead of emitting
    with pytest.raises(RedactionError):
        build_event(**_valid_kwargs(status="bearer_token"))


def test_charset_clean_bearer_shaped_prompt_version_dropped_with_warning(caplog):
    # Given the lowercase "bearer" token (passes the charset, trips the
    # redaction guard) Then the field still drops with a warning, never emits.
    with caplog.at_level(logging.WARNING, logger="common.logs"):
        event = build_event(**_valid_kwargs(prompt_version="bearer"))
    assert event["prompt_version"] is None
    assert [r for r in caplog.records if r.getMessage() == "prompt_version_rejected"]


def test_nested_hostile_extras_never_emitted():
    # Given hostile extras nested inside dict/list values Then nothing leaks: extras are never read
    payload = _envelope()
    payload["nested"] = {"Authorization": AUTH_VALUE, "diff": DIFF_TEXT}
    payload["items"] = [AUTH_VALUE, DIFF_TEXT, LLM_REQUEST]
    _assert_no_forbidden(_emit_lines(payload))


def test_emit_rejects_non_dict_event():
    lines = []
    with pytest.raises(LogsError) as excinfo:
        emit(lines.append, "not-an-event")
    assert excinfo.value.field == "event"
    assert excinfo.value.reason == "not_object"
    assert lines == []


def test_emit_rejects_non_string_keys():
    lines = []
    event = build_event(**_valid_kwargs())
    tampered = dict(event)
    del tampered["status"]
    tampered[7] = "ok"
    with pytest.raises(LogsError) as excinfo:
        emit(lines.append, tampered)
    assert excinfo.value.field == "event"
    assert excinfo.value.reason == "bad_fields"
    assert lines == []


# --- SPR-62: prompt_sha256 telemetry -----------------------------------------


def test_prompt_sha256_matches_known_vector():
    # Stable known-input → known sha256, lowercase hex (hash only, never text).
    assert (
        prompt_sha256("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_prompt_sha256_is_lowercase_hex_64():
    import re

    digest = prompt_sha256("SYSTEM-PROMPT")
    assert re.match(r"^[0-9a-f]{64}\Z", digest)
    assert digest == digest.lower()


@pytest.mark.parametrize(
    "value", ["AB" * 32, "ab" * 31 + "!", "ab" * 32 + "ab", "", 123, b"ab" * 32]
)
def test_build_event_rejects_malformed_prompt_sha256(value):
    with pytest.raises(LogsError) as excinfo:
        build_event(**_valid_kwargs(prompt_sha256=value))
    assert excinfo.value.field == "prompt_sha256"
    assert excinfo.value.reason == "bad_prompt_sha256"


def test_event_from_envelope_carries_prompt_sha256():
    digest = prompt_sha256("SYSTEM-PROMPT")
    event = event_from_envelope(_envelope(), **_metrics(), prompt_sha256=digest)
    assert event["prompt_sha256"] == digest
    lines = []
    emit(lines.append, event)
    assert json.loads(lines[0])["prompt_sha256"] == digest


def test_emit_preserves_non_string_values_verbatim():
    # The emit-time cleaner re-run is read-only: ints, None, and bools
    # round-trip untouched (no double-mutation of non-string values).
    event = build_event(
        **_valid_kwargs(
            generation=3, error_class=None, stale_discarded=True, prompt_sha256="ab" * 32
        )
    )
    before = dict(event)
    lines = []
    emit(lines.append, event)
    assert event == before
    parsed = json.loads(lines[0])
    assert parsed["generation"] == 3
    assert parsed["error_class"] is None
    assert parsed["stale_discarded"] is True
    assert parsed["duration_ms"] == 1234
    assert parsed["prompt_sha256"] == "ab" * 32


# --- SPR-62: ingress status events (G6-F3) ------------------------------------


def test_ingress_fixed_fields_are_pinned():
    assert INGRESS_FIXED_FIELDS == frozenset({"statusCode", "decision", "reason"})


def test_build_ingress_event_carries_exact_shape():
    event = build_ingress_event(status_code=401, decision="rejected", reason="bad_signature")
    assert event == {"statusCode": 401, "decision": "rejected", "reason": "bad_signature"}
    assert set(event) == INGRESS_FIXED_FIELDS


@pytest.mark.parametrize("code", [99, 600, 0, -1, "401", True, None, 401.0])
def test_build_ingress_event_rejects_bad_status_code(code):
    with pytest.raises(LogsError) as excinfo:
        build_ingress_event(status_code=code, decision="rejected", reason="bad_signature")
    assert excinfo.value.field == "statusCode"
    assert excinfo.value.reason == "bad_status_code"


@pytest.mark.parametrize("decision", ["nope", "", "ALLOWED", "ok", None, 200])
def test_build_ingress_event_rejects_bad_decision(decision):
    with pytest.raises(LogsError) as excinfo:
        build_ingress_event(status_code=200, decision=decision, reason="enqueued")
    assert excinfo.value.field == "decision"
    assert excinfo.value.reason == "bad_decision"


@pytest.mark.parametrize("reason", ["", "HAS SPACE", "UPPER", "x" * 65, "semi;colon", None, 401])
def test_build_ingress_event_rejects_bad_reason(reason):
    with pytest.raises(LogsError) as excinfo:
        build_ingress_event(status_code=200, decision="discarded", reason=reason)
    assert excinfo.value.field == "reason"
    assert excinfo.value.reason == "bad_reason"


def test_build_ingress_event_refuses_forbidden_reason():
    # "bearer" passes the token charset but trips the redaction guard.
    with pytest.raises(RedactionError):
        build_ingress_event(status_code=401, decision="rejected", reason="bearer")


def test_emit_ingress_event_writes_single_json_line():
    lines = []
    event = build_ingress_event(status_code=401, decision="rejected", reason="bad_signature")
    emit_ingress_event(lines.append, event)
    assert len(lines) == 1
    assert lines[0].endswith("\n")
    parsed = json.loads(lines[0])
    # The 401-spike alarm's filter `{ $.statusCode = 401 }` matches this shape.
    assert parsed == {"statusCode": 401, "decision": "rejected", "reason": "bad_signature"}
    assert isinstance(parsed["statusCode"], int)


def test_emit_ingress_event_rejects_wrong_shape():
    lines = []
    with pytest.raises(LogsError) as excinfo:
        emit_ingress_event(lines.append, {"statusCode": 401})
    assert excinfo.value.field == "event"
    assert excinfo.value.reason == "bad_fields"
    assert lines == []


def test_emit_ingress_event_names_tampered_field():
    lines = []
    event = build_ingress_event(status_code=200, decision="allowed", reason="enqueued")
    tampered = dict(event)
    tampered["reason"] = "bearer"
    with pytest.raises(RedactionError) as excinfo:
        emit_ingress_event(lines.append, tampered)
    assert excinfo.value.field == "reason"
    assert lines == []

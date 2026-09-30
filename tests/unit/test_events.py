"""SPR-84 T001: stage-event envelope + fixed field-set contract (HLD-004 §6, D4).

Every §6 event type must be constructible with exactly its required key
fields over the shared envelope `{v: 1, run_id (uuid4 hex), ts (epoch-ms),
type}`. Field sets are fixed: any extra/unspecified key is a contract
violation. Consumers order by `ts`; JSONL rendering is one line per event.

RED state: `common.events` does not exist yet — collection errors.
"""

import json
import re
import uuid

import pytest

from common.events import (
    EVENT_TYPES,
    EventsError,
    agent_completed,
    agent_failed,
    agent_reasoning,
    agent_retry,
    agent_started,
    checkpoint,
    concurrency_single_pass,
    degraded_no_budget,
    degraded_to_single_pass,
    review_published,
    review_skipped,
    review_started,
    review_synthesized,
    synthesizer_failed,
    to_jsonl,
    verification_done,
    verification_failed,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")

FINDING = {
    "file_path": "a.py",
    "line_start": 1,
    "line_end": 2,
    "title": "off-by-one",
    "description": "d",
    "suggested_fix": "f",
    "severity": "HIGH",
    "category": "correctness",
}

ENVELOPE_KEYS = frozenset({"v", "run_id", "ts", "type"})

# (type name, thunk building a valid event, exact full key set)
BUILDERS = [
    (
        "review_started",
        lambda: review_started(
            pr=42, sha=SHA, diff_stats={"files": 2, "additions": 10, "deletions": 3}
        ),
        ENVELOPE_KEYS | {"pr", "sha", "diff_stats"},
    ),
    (
        "review_skipped",
        lambda: review_skipped(reason="empty_diff", pr=42, sha=SHA),
        ENVELOPE_KEYS | {"reason", "pr", "sha"},
    ),
    (
        "checkpoint",
        lambda: checkpoint(stage="claimed"),
        ENVELOPE_KEYS | {"stage"},
    ),
    (
        "agent_started",
        lambda: agent_started(specialty="correctness"),
        ENVELOPE_KEYS | {"specialty"},
    ),
    (
        "agent_reasoning",
        lambda: agent_reasoning(specialty="security", reasoning_excerpt="because x"),
        ENVELOPE_KEYS | {"specialty", "reasoning_excerpt"},
    ),
    (
        "agent_completed",
        lambda: agent_completed(
            specialty="tests",
            findings_n=1,
            latency_ms=1000,
            tokens_in=100,
            tokens_out=50,
            findings=[dict(FINDING)],
        ),
        ENVELOPE_KEYS
        | {
            "specialty",
            "findings_n",
            "latency_ms",
            "tokens_in",
            "tokens_out",
            "findings",
            "coordinates_clamped_n",
        },
    ),
    (
        "agent_retry",
        lambda: agent_retry(
            specialty="correctness", attempt=1, error_code="rate_limit", backoff_ms=500
        ),
        ENVELOPE_KEYS | {"specialty", "attempt", "error_code", "backoff_ms"},
    ),
    (
        "agent_failed",
        lambda: agent_failed(specialty="security", error_class="timeout", latency_ms=240000),
        ENVELOPE_KEYS | {"specialty", "error_class", "latency_ms"},
    ),
    (
        "verification_done",
        lambda: verification_done(
            survived_n=2,
            killed_n=1,
            escalated_n=0,
            wave_survivors=2,
            latency_ms=5000,
            tokens_in=200,
            tokens_out=100,
            verified=[dict(FINDING)],
            killed=[{"candidate_id": "tests:0", "kill_reason": "no evidence"}],
            escalated=[],
        ),
        ENVELOPE_KEYS
        | {
            "survived_n",
            "killed_n",
            "escalated_n",
            "wave_survivors",
            "latency_ms",
            "tokens_in",
            "tokens_out",
            "verified",
            "killed",
            "escalated",
            "coordinates_reanchored_n",
        },
    ),
    (
        "verification_failed",
        lambda: verification_failed(error_class="timeout", latency_ms=1000),
        ENVELOPE_KEYS | {"error_class", "latency_ms"},
    ),
    (
        "review_synthesized",
        lambda: review_synthesized(
            findings_merged_n=2,
            dropped_as_duplicate_n=1,
            latency_ms=3000,
            tokens_in=150,
            tokens_out=80,
            findings=[dict(FINDING)],
        ),
        ENVELOPE_KEYS
        | {
            "findings_merged_n",
            "dropped_as_duplicate_n",
            "latency_ms",
            "tokens_in",
            "tokens_out",
            "findings",
        },
    ),
    (
        "synthesizer_failed",
        lambda: synthesizer_failed(error_class="length", latency_ms=1000),
        ENVELOPE_KEYS | {"error_class", "latency_ms"},
    ),
    (
        "degraded_to_single_pass",
        lambda: degraded_to_single_pass(reason="all_specialists_failed", failed_stage="wave"),
        ENVELOPE_KEYS | {"reason", "failed_stage"},
    ),
    (
        "concurrency_single_pass",
        lambda: concurrency_single_pass(reason="mutex_held", elapsed_ms=120),
        ENVELOPE_KEYS | {"reason", "elapsed_ms"},
    ),
    (
        "degraded_no_budget",
        lambda: degraded_no_budget(reason="verifier overran", elapsed_ms=50),
        ENVELOPE_KEYS | {"reason", "elapsed_ms"},
    ),
    (
        "review_published",
        lambda: review_published(comment_id=12345),
        ENVELOPE_KEYS | {"comment_id"},
    ),
]

EXPECTED_TYPES = frozenset(name for name, _, _ in BUILDERS)


def test_event_type_registry_matches_hld_section_6():
    assert EVENT_TYPES == EXPECTED_TYPES


@pytest.mark.parametrize("type_name,thunk,_keys", BUILDERS)
def test_envelope_invariants(type_name, thunk, _keys):
    event = thunk()
    assert event["v"] == 1
    assert isinstance(event["run_id"], str) and RUN_ID_RE.match(event["run_id"])
    # uuid4 hex, 32 lowercase chars, no dashes — parseable as a uuid
    assert uuid.UUID(event["run_id"]).hex == event["run_id"]
    assert "-" not in event["run_id"]
    assert isinstance(event["ts"], int) and not isinstance(event["ts"], bool)
    assert event["ts"] >= 0
    # epoch-MILLISECONDS, not seconds: a seconds regression must fail the suite
    assert event["ts"] > 10**12
    assert event["type"] == type_name


@pytest.mark.parametrize("type_name,thunk,keys", BUILDERS)
def test_exact_field_sets(type_name, thunk, keys):
    """Fixed field sets per the §6 table — no extra/unspecified fields."""
    event = thunk()
    assert set(event) == keys, f"{type_name}: {sorted(set(event) ^ keys)}"


def test_verification_done_carries_wave_survivors():
    event = verification_done(
        survived_n=2,
        killed_n=1,
        escalated_n=0,
        wave_survivors=2,
        latency_ms=5000,
        tokens_in=200,
        tokens_out=100,
        verified=[],
        killed=[],
        escalated=[],
    )
    assert isinstance(event["wave_survivors"], int)
    assert event["wave_survivors"] == 2


@pytest.mark.parametrize("type_name,thunk,_keys", BUILDERS)
def test_jsonl_round_trip(type_name, thunk, _keys):
    event = thunk()
    line = to_jsonl(event)
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed == event
    assert parsed["v"] == 1
    assert parsed["type"] == type_name


def test_jsonl_escapes_embedded_newline_to_single_line():
    event = agent_reasoning(specialty="security", reasoning_excerpt="line one\nline two\n")
    line = to_jsonl(event)
    assert "\n" not in line
    assert json.loads(line) == event


def test_consumers_order_by_ts():
    older = checkpoint(stage="established", ts=1000)
    newer = checkpoint(stage="claimed", ts=2000)
    assert sorted([newer, older], key=lambda e: e["ts"]) == [older, newer]


def test_default_run_ids_are_unique_hex():
    ids = {agent_started(specialty="correctness")["run_id"] for _ in range(3)}
    assert len(ids) == 3
    for run_id in ids:
        assert RUN_ID_RE.match(run_id)


def test_explicit_run_id_and_ts_pass_through():
    run_id = uuid.uuid4().hex
    event = agent_started(specialty="correctness", run_id=run_id, ts=1720000000000)
    assert event["run_id"] == run_id
    assert event["ts"] == 1720000000000


@pytest.mark.parametrize(
    ("thunk", "field"),
    [
        (lambda: review_skipped(reason="bogus", pr=1, sha=SHA), "reason"),
        (lambda: checkpoint(stage="bogus"), "stage"),
        (
            lambda: degraded_to_single_pass(reason="x", failed_stage="bogus"),
            "failed_stage",
        ),
        (
            lambda: concurrency_single_pass(reason="bogus", elapsed_ms=1),
            "reason",
        ),
    ],
)
def test_enum_fields_reject_unknown_values(thunk, field):
    with pytest.raises(EventsError) as excinfo:
        thunk()
    assert excinfo.value.field == field


def test_concurrency_single_pass_accepts_docs_only_reason():
    """T076: docs_only joins the deliberate-single-pass reason enum."""
    evt = concurrency_single_pass(reason="docs_only", elapsed_ms=0)
    assert evt["reason"] == "docs_only"


def test_bad_run_id_rejected():
    with pytest.raises(EventsError) as excinfo:
        agent_started(specialty="correctness", run_id="not-a-run-id")
    assert excinfo.value.field == "run_id"


def test_bool_ts_rejected():
    with pytest.raises(EventsError) as excinfo:
        agent_started(specialty="correctness", ts=True)
    assert excinfo.value.field == "ts"


@pytest.mark.parametrize(
    "bad_stats",
    [
        {"files": 1, "additions": 2},  # missing deletions
        {"files": 1, "additions": 2, "deletions": 3, "extra": 0},  # extra key
        {"files": -1, "additions": 2, "deletions": 3},  # negative
        {"files": "2", "additions": 2, "deletions": 3},  # non-int
    ],
)
def test_diff_stats_shape_rejected(bad_stats):
    with pytest.raises(EventsError) as excinfo:
        review_started(pr=1, sha=SHA, diff_stats=bad_stats)
    assert excinfo.value.field == "diff_stats"


def test_extra_kwargs_structurally_rejected():
    """Fixed field sets: there is no parameter for diffs/secrets/payloads."""
    with pytest.raises(TypeError):
        agent_started(specialty="correctness", diff="diff --git a/b")  # type: ignore[call-arg]


def test_to_jsonl_rejects_extra_unknown_key():
    """A post-construction-mutated dict never serializes."""
    event = agent_started(specialty="correctness")
    event["diff"] = "diff --git a/b"
    with pytest.raises(EventsError) as excinfo:
        to_jsonl(event)
    assert excinfo.value.field == "event"


def test_to_jsonl_rejects_mutated_ts():
    event = checkpoint(stage="claimed")
    event["ts"] = True
    with pytest.raises(EventsError) as excinfo:
        to_jsonl(event)
    assert excinfo.value.field == "ts"


def test_to_jsonl_rejects_unknown_type():
    with pytest.raises(EventsError) as excinfo:
        to_jsonl({"v": 1, "run_id": uuid.uuid4().hex, "ts": 1, "type": "bogus"})
    assert excinfo.value.field == "event"


@pytest.mark.parametrize("comment_id", [0, -1])
def test_review_published_rejects_nonpositive_comment_id(comment_id):
    with pytest.raises(EventsError) as excinfo:
        review_published(comment_id=comment_id)
    assert excinfo.value.field == "comment_id"


def test_coordinates_clamped_n_defaults_to_zero():
    event = agent_completed(
        specialty="tests",
        findings_n=0,
        latency_ms=1,
        tokens_in=0,
        tokens_out=0,
        findings=[],
    )
    assert event["coordinates_clamped_n"] == 0


def test_coordinates_clamped_n_round_trips():
    event = agent_completed(
        specialty="tests",
        findings_n=1,
        latency_ms=1,
        tokens_in=0,
        tokens_out=0,
        findings=[dict(FINDING)],
        coordinates_clamped_n=2,
    )
    assert event["coordinates_clamped_n"] == 2
    assert json.loads(to_jsonl(event))["coordinates_clamped_n"] == 2


def test_coordinates_reanchored_n_defaults_to_zero():
    event = verification_done(
        survived_n=0,
        killed_n=0,
        escalated_n=0,
        wave_survivors=0,
        latency_ms=1,
        tokens_in=0,
        tokens_out=0,
        verified=[],
        killed=[],
        escalated=[],
    )
    assert event["coordinates_reanchored_n"] == 0


def test_coordinates_reanchored_n_round_trips():
    event = verification_done(
        survived_n=1,
        killed_n=0,
        escalated_n=0,
        wave_survivors=1,
        latency_ms=1,
        tokens_in=0,
        tokens_out=0,
        verified=[dict(FINDING)],
        killed=[],
        escalated=[],
        coordinates_reanchored_n=3,
    )
    assert event["coordinates_reanchored_n"] == 3
    assert json.loads(to_jsonl(event))["coordinates_reanchored_n"] == 3

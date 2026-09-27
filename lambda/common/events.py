"""Multi-agent stage events (HLD-004 §6, D4).

One versioned event schema shared by the replay site (v1 consumer) and a
future live feed (v2 consumer): every event is `{v: 1, run_id, ts, type,
...}` where `run_id` is `uuid4().hex` (32 lowercase hex chars, no dashes)
and `ts` is epoch milliseconds (int). Consumers order by `ts`.

Typed constructors below are the ONLY way to build events. Keyword-only
parameters ARE the fixed field sets from the §6 table, so non-fixed content
(diffs, secrets, raw payloads, LLM bodies) is structurally unemittable —
there is no parameter that could carry it (`TypeError` on anything else).
Findings/verified/killed/escalated arrays are carried as opaque JSON lists;
their closed-schema validation belongs to `common.findings` (HLD §6),
not here.

`_FIELD_SETS` is the single source of truth for the per-type field sets:
constructors assert their payload against it via `_build`, and `to_jsonl`
re-validates any dict against it at the render boundary — so a hand-built
or structurally-mutated dict can never serialize as an event.

Pure stdlib, no I/O, no boto3 import.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

EVENT_VERSION = 1

_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")

REVIEW_SKIPPED_REASONS = frozenset({"empty_diff", "phase0_no_budget"})
CHECKPOINT_STAGES = frozenset({"established", "diff_fetched", "claimed", "published", "finalized"})
FAILED_STAGES = frozenset({"wave", "verifier", "synthesizer"})
CONCURRENCY_SINGLE_PASS_REASONS = frozenset({"mutex_held", "mutex_held_no_budget"})

_ENVELOPE_KEYS = frozenset({"v", "run_id", "ts", "type"})

# Single source of truth for the §6 fixed field sets (extra keys beyond
# the envelope, per event type). Constructors build through `_build` and
# `to_jsonl` validates against this same table — never duplicated.
_FIELD_SETS: dict[str, frozenset[str]] = {
    "review_started": frozenset({"pr", "sha", "diff_stats"}),
    "review_skipped": frozenset({"reason", "pr", "sha"}),
    "checkpoint": frozenset({"stage"}),
    "agent_started": frozenset({"specialty"}),
    "agent_reasoning": frozenset({"specialty", "reasoning_excerpt"}),
    "agent_completed": frozenset(
        {
            "specialty",
            "findings_n",
            "latency_ms",
            "tokens_in",
            "tokens_out",
            "findings",
            "coordinates_clamped_n",
        }
    ),
    "agent_retry": frozenset({"specialty", "attempt", "error_code", "backoff_ms"}),
    "agent_failed": frozenset({"specialty", "error_class", "latency_ms"}),
    "verification_done": frozenset(
        {
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
        }
    ),
    "verification_failed": frozenset({"error_class", "latency_ms"}),
    "review_synthesized": frozenset(
        {
            "findings_merged_n",
            "dropped_as_duplicate_n",
            "latency_ms",
            "tokens_in",
            "tokens_out",
            "findings",
        }
    ),
    "synthesizer_failed": frozenset({"error_class", "latency_ms"}),
    "degraded_to_single_pass": frozenset({"reason", "failed_stage"}),
    "concurrency_single_pass": frozenset({"reason", "elapsed_ms"}),
    "degraded_no_budget": frozenset({"reason", "elapsed_ms"}),
    "review_published": frozenset({"comment_id"}),
}

EVENT_TYPES = frozenset(_FIELD_SETS)


class EventsError(ValueError):
    """Typed event rejection: `field` names the offending field, `reason`
    is a machine-readable code (`missing`, `bad_run_id`, `bad_ts`,
    `bad_pr`, `bad_sha`, `bad_diff_stats`, `bad_reason`, `bad_stage`,
    `bad_specialty`, `bad_count`, `bad_latency`, `bad_tokens`,
    `bad_attempt`, `bad_error_code`, `bad_error_class`, `bad_comment_id`,
    `bad_reasoning`, `bad_findings`, `bad_elapsed`, `bad_failed_stage`,
    `bad_fields`, `not_object`, `bad_event`)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid event: {field}: {reason}")


def _clean_run_id(value: Any) -> str:
    if value is None:
        return uuid.uuid4().hex
    if not isinstance(value, str) or not _RUN_ID_RE.match(value):
        raise EventsError("run_id", "bad_run_id")
    return value


def _clean_ts(value: Any) -> int:
    if value is None:
        return int(time.time() * 1000)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EventsError("ts", "bad_ts")
    return value


def _clean_pr(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EventsError("pr", "bad_pr")
    return value


def _clean_sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA_RE.match(value):
        raise EventsError("sha", "bad_sha")
    return value


def _clean_diff_stats(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"files", "additions", "deletions"}:
        raise EventsError("diff_stats", "bad_diff_stats")
    for key in ("files", "additions", "deletions"):
        count = value[key]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise EventsError("diff_stats", "bad_diff_stats")
    return {
        "files": value["files"],
        "additions": value["additions"],
        "deletions": value["deletions"],
    }


def _clean_enum(value: Any, allowed: frozenset[str], field: str, reason: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise EventsError(field, reason)
    return value


def _clean_nonempty_str(value: Any, field: str, reason: str) -> str:
    if not isinstance(value, str) or not value:
        raise EventsError(field, reason)
    return value


def _clean_count(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EventsError(field, "bad_count")
    return value


def _clean_json_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise EventsError(field, "bad_findings")
    return value


def _envelope(type_name: str, run_id: Any = None, ts: Any = None) -> dict[str, Any]:
    return {
        "v": EVENT_VERSION,
        "run_id": _clean_run_id(run_id),
        "ts": _clean_ts(ts),
        "type": type_name,
    }


def _build(
    type_name: str, fields: dict[str, Any], run_id: Any = None, ts: Any = None
) -> dict[str, Any]:
    """Assemble an event, asserting the payload matches `_FIELD_SETS` —
    a constructor edited without updating the table fails here, never
    silently."""
    if set(fields) != _FIELD_SETS[type_name]:
        raise EventsError("event", "bad_fields")
    return _envelope(type_name, run_id, ts) | fields


def review_started(
    *,
    pr: Any,
    sha: Any,
    diff_stats: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Review step begins (worker_handler)."""
    return _build(
        "review_started",
        {
            "pr": _clean_pr(pr),
            "sha": _clean_sha(sha),
            "diff_stats": _clean_diff_stats(diff_stats),
        },
        run_id,
        ts,
    )


def review_skipped(
    *,
    reason: Any,
    pr: Any,
    sha: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Fan-out skipped (worker_handler)."""
    return _build(
        "review_skipped",
        {
            "reason": _clean_enum(reason, REVIEW_SKIPPED_REASONS, "reason", "bad_reason"),
            "pr": _clean_pr(pr),
            "sha": _clean_sha(sha),
        },
        run_id,
        ts,
    )


def checkpoint(
    *,
    stage: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Pipeline stage reached."""
    return _build(
        "checkpoint",
        {"stage": _clean_enum(stage, CHECKPOINT_STAGES, "stage", "bad_stage")},
        run_id,
        ts,
    )


def agent_started(
    *,
    specialty: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Specialist coroutine begins."""
    return _build(
        "agent_started",
        {"specialty": _clean_nonempty_str(specialty, "specialty", "bad_specialty")},
        run_id,
        ts,
    )


def agent_reasoning(
    *,
    specialty: Any,
    reasoning_excerpt: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Reasoning excerpt available (raw reasoning truncated at
    `REASONING_MAX_CHARS` by the caller before it reaches this module —
    there is no summarizer call; see HLD D6)."""
    if not isinstance(reasoning_excerpt, str):
        raise EventsError("reasoning_excerpt", "bad_reasoning")
    return _build(
        "agent_reasoning",
        {
            "specialty": _clean_nonempty_str(specialty, "specialty", "bad_specialty"),
            "reasoning_excerpt": reasoning_excerpt,
        },
        run_id,
        ts,
    )


def agent_completed(
    *,
    specialty: Any,
    findings_n: Any,
    latency_ms: Any,
    tokens_in: Any,
    tokens_out: Any,
    findings: Any,
    coordinates_clamped_n: int = 0,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Specialist returned parseable findings. `coordinates_clamped_n`
    counts coordinates `run_fanout` clamped to post-image (0 when none;
    HLD-004 D3)."""
    return _build(
        "agent_completed",
        {
            "specialty": _clean_nonempty_str(specialty, "specialty", "bad_specialty"),
            "findings_n": _clean_count(findings_n, "findings_n"),
            "latency_ms": _clean_count(latency_ms, "latency_ms"),
            "tokens_in": _clean_count(tokens_in, "tokens_in"),
            "tokens_out": _clean_count(tokens_out, "tokens_out"),
            "findings": _clean_json_list(findings, "findings"),
            "coordinates_clamped_n": _clean_count(coordinates_clamped_n, "coordinates_clamped_n"),
        },
        run_id,
        ts,
    )


def agent_retry(
    *,
    specialty: Any,
    attempt: Any,
    error_code: Any,
    backoff_ms: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Reserved in the schema; not emitted on the fan-out path in v1
    (HLD D9 partial-success rules — single-attempt specialists, all retry
    ownership stays with the SQS queue)."""
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        raise EventsError("attempt", "bad_attempt")
    return _build(
        "agent_retry",
        {
            "specialty": _clean_nonempty_str(specialty, "specialty", "bad_specialty"),
            "attempt": attempt,
            "error_code": _clean_nonempty_str(error_code, "error_code", "bad_error_code"),
            "backoff_ms": _clean_count(backoff_ms, "backoff_ms"),
        },
        run_id,
        ts,
    )


def agent_failed(
    *,
    specialty: Any,
    error_class: Any,
    latency_ms: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Specialist raised or timed out."""
    return _build(
        "agent_failed",
        {
            "specialty": _clean_nonempty_str(specialty, "specialty", "bad_specialty"),
            "error_class": _clean_nonempty_str(error_class, "error_class", "bad_error_class"),
            "latency_ms": _clean_count(latency_ms, "latency_ms"),
        },
        run_id,
        ts,
    )


def verification_done(
    *,
    survived_n: Any,
    killed_n: Any,
    escalated_n: Any,
    wave_survivors: Any,
    latency_ms: Any,
    tokens_in: Any,
    tokens_out: Any,
    verified: Any,
    killed: Any,
    escalated: Any,
    coordinates_reanchored_n: int = 0,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Verifier returned. `wave_survivors` is the count of specialists that
    returned parseable findings (e.g. 2 after one 429/timeout loss) — it
    lets replay/eval attribute recall deltas to partial waves.
    `coordinates_reanchored_n` counts verifier re-anchors to post-image
    (0 when none; HLD-004 D3)."""
    return _build(
        "verification_done",
        {
            "survived_n": _clean_count(survived_n, "survived_n"),
            "killed_n": _clean_count(killed_n, "killed_n"),
            "escalated_n": _clean_count(escalated_n, "escalated_n"),
            "wave_survivors": _clean_count(wave_survivors, "wave_survivors"),
            "latency_ms": _clean_count(latency_ms, "latency_ms"),
            "tokens_in": _clean_count(tokens_in, "tokens_in"),
            "tokens_out": _clean_count(tokens_out, "tokens_out"),
            "verified": _clean_json_list(verified, "verified"),
            "killed": _clean_json_list(killed, "killed"),
            "escalated": _clean_json_list(escalated, "escalated"),
            "coordinates_reanchored_n": _clean_count(
                coordinates_reanchored_n, "coordinates_reanchored_n"
            ),
        },
        run_id,
        ts,
    )


def verification_failed(
    *,
    error_class: Any,
    latency_ms: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Verifier raised or timed out."""
    return _build(
        "verification_failed",
        {
            "error_class": _clean_nonempty_str(error_class, "error_class", "bad_error_class"),
            "latency_ms": _clean_count(latency_ms, "latency_ms"),
        },
        run_id,
        ts,
    )


def review_synthesized(
    *,
    findings_merged_n: Any,
    dropped_as_duplicate_n: Any,
    latency_ms: Any,
    tokens_in: Any,
    tokens_out: Any,
    findings: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Synthesizer returned merged findings JSON."""
    return _build(
        "review_synthesized",
        {
            "findings_merged_n": _clean_count(findings_merged_n, "findings_merged_n"),
            "dropped_as_duplicate_n": _clean_count(
                dropped_as_duplicate_n, "dropped_as_duplicate_n"
            ),
            "latency_ms": _clean_count(latency_ms, "latency_ms"),
            "tokens_in": _clean_count(tokens_in, "tokens_in"),
            "tokens_out": _clean_count(tokens_out, "tokens_out"),
            "findings": _clean_json_list(findings, "findings"),
        },
        run_id,
        ts,
    )


def synthesizer_failed(
    *,
    error_class: Any,
    latency_ms: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Synthesizer raised or timed out."""
    return _build(
        "synthesizer_failed",
        {
            "error_class": _clean_nonempty_str(error_class, "error_class", "bad_error_class"),
            "latency_ms": _clean_count(latency_ms, "latency_ms"),
        },
        run_id,
        ts,
    )


def degraded_to_single_pass(
    *,
    reason: Any,
    failed_stage: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Fan-out abandoned for single-pass fallback."""
    return _build(
        "degraded_to_single_pass",
        {
            "reason": _clean_nonempty_str(reason, "reason", "bad_reason"),
            "failed_stage": _clean_enum(
                failed_stage, FAILED_STAGES, "failed_stage", "bad_failed_stage"
            ),
        },
        run_id,
        ts,
    )


def concurrency_single_pass(
    *,
    reason: Any,
    elapsed_ms: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Mutex held; contender executes single-pass inline (no deferral)."""
    return _build(
        "concurrency_single_pass",
        {
            "reason": _clean_enum(reason, CONCURRENCY_SINGLE_PASS_REASONS, "reason", "bad_reason"),
            "elapsed_ms": _clean_count(elapsed_ms, "elapsed_ms"),
        },
        run_id,
        ts,
    )


def degraded_no_budget(
    *,
    reason: Any,
    elapsed_ms: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """No budget left even for fallback (terminal)."""
    return _build(
        "degraded_no_budget",
        {
            "reason": _clean_nonempty_str(reason, "reason", "bad_reason"),
            "elapsed_ms": _clean_count(elapsed_ms, "elapsed_ms"),
        },
        run_id,
        ts,
    )


def review_published(
    *,
    comment_id: Any,
    run_id: Any = None,
    ts: Any = None,
) -> dict[str, Any]:
    """Canonical comment PATCHed."""
    if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id < 1:
        raise EventsError("comment_id", "bad_comment_id")
    return _build("review_published", {"comment_id": comment_id}, run_id, ts)


def to_jsonl(event: Any) -> str:
    """Render one event as a single JSONL line (no trailing newline —
    callers join lines with `"\\n"` when writing `events.jsonl`).

    Full validation at this render boundary: the dict must carry exactly
    the envelope + the `_FIELD_SETS` keys for its type, with a valid `v`,
    `run_id`, and `ts` — a hand-built or post-construction-mutated dict
    that fails any of these is rejected with `EventsError` and nothing
    serializes. Deep value checks stay constructor-side; this gate covers
    shape plus the redaction-relevant envelope scalars.

    `json.dumps` escapes embedded newlines inside string values, so the
    result never contains a raw newline and `json.loads` round-trips it.
    """
    if not isinstance(event, dict):
        raise EventsError("event", "not_object")
    type_name = event.get("type")
    if type_name not in _FIELD_SETS:
        raise EventsError("event", "bad_event")
    if set(event) != _ENVELOPE_KEYS | _FIELD_SETS[type_name]:
        raise EventsError("event", "bad_fields")
    if event.get("v") != EVENT_VERSION:
        raise EventsError("event", "bad_event")
    # Strict scalar re-checks (no None-defaulting: the key set above
    # guarantees the keys exist; a None value here is a mutation, not
    # an omission, and must fail, never silently regenerate).
    run_id = event.get("run_id")
    if run_id is None:
        raise EventsError("run_id", "bad_run_id")
    _clean_run_id(run_id)
    ts = event.get("ts")
    if ts is None:
        raise EventsError("ts", "bad_ts")
    _clean_ts(ts)
    return json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

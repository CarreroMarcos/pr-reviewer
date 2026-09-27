"""SPR-101 T016: verifier-stage contract tests (HLD-004 D3, §6 closed-schema pin).

Policy (code-enforced — severity rides trusted candidate data, so both
rules are mechanically checkable): unverifiable HIGH → `escalated`
(never killed, no exceptions); MEDIUM/LOW killed ONLY with a non-empty
`kill_reason` (the "citing missing evidence" substance is prompt
contract) — a kill without one reroutes to `escalated`, never fails.
Closed §6 Verifier Output schema: any field outside the pinned
properties, any unknown/duplicated/omitted `candidate_id` → the stage
fails (`verification_failed` event + `FanoutDegraded`, never forwarded).
The verifier re-anchors coordinates; the code counts moves onto
`verification_done.coordinates_reanchored_n`.

RED state: `common.fanout` has no verifier symbol — collection errors
on import.
"""

import concurrent.futures
import json
import threading
import time
import uuid

import pytest

from common import fanout as fanout_mod
from common.config import MultiAgentConfig
from common.fanout import FanoutDegraded, run_verifier
from common.llm import LlmError, ReviewResult

V_PROMPT = "VPrompt-sentinel"
DIFF_TEXT = "diff --git a/foo.py b/foo.py"

API_KEY = "glm-test-key-not-a-credential"  # noqa: S105 (dummy fixture)
MODEL = "glm-5.3-flash"
ENDPOINT = "https://api.z.ai/v1/chat/completions"

REROUTE_MARKER = "rerouted by policy: kill lacked evidence citation"


def make_cfg(**overrides):
    params = {
        "multi_agent": 1,
        "multi_agent_phase0": 0,
        "fanout_concurrency": 3,
        "mutex_lease_ttl_s": 900,
        "reasoning_max_chars": 4000,
        "reasoning_effort": "low",
        "wave_wait_for_s": 300,
        "verifier_wait_for_s": 30,
        "synthesizer_wait_for_s": 180,
        "single_pass_wait_for_s": 240,
        "socket_read_timeout_s": 240,
        "budget_margin_s": 60,
    }
    params.update(overrides)
    return MultiAgentConfig(**params)


def candidate(cid="correctness:0", severity="HIGH", category="correctness", **overrides):
    finding = {
        "candidate_id": cid,
        "file_path": "a.py",
        "line_start": 10,
        "line_end": 12,
        "title": "Missing bound",
        "description": "d",
        "suggested_fix": "f",
        "severity": severity,
        "category": category,
    }
    finding.update(overrides)
    return finding


CANDIDATES = [
    candidate("correctness:0", severity="HIGH", category="correctness"),
    candidate("security:0", severity="MEDIUM", category="security"),
    candidate("tests:0", severity="LOW", category="tests"),
]


def verified_item(cid, **overrides):
    item = {
        "candidate_id": cid,
        "file_path": "a.py",
        "line_start": 10,
        "line_end": 12,
        "title": "Missing bound",
        "description": "d",
        "suggested_fix": "f",
        "severity": "HIGH",
        "category": "correctness",
        "verification_note": "reproduced against the diff",
    }
    item.update(overrides)
    return item


def ok_result(payload):
    return ReviewResult(
        content=json.dumps(payload),
        prompt_tokens=200,
        completion_tokens=100,
        total_tokens=300,
        reasoning_content=None,
    )


class ScriptedVerifierCall:
    """Single-leg stub: records the one call (kwargs + thread)."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def __call__(
        self,
        *,
        api_key,
        model,
        endpoint,
        system_prompt,
        diff_text,
        thinking_enabled=False,
        reasoning_effort="low",
        read_timeout_s=None,
        allowed_hosts=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "diff_text": diff_text,
                "thinking_enabled": thinking_enabled,
                "reasoning_effort": reasoning_effort,
                "read_timeout_s": read_timeout_s,
                "thread": threading.get_ident(),
                "extra_kwargs": dict(kwargs),
            }
        )
        kind = self.behavior[0]
        if kind == "ok":
            return ok_result(self.behavior[1])
        if kind == "raw":
            return ReviewResult(
                content=self.behavior[1],
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                reasoning_content=None,
            )
        if kind == "sleep":
            time.sleep(self.behavior[1])
            return ok_result({"verified": [], "killed": [], "escalated": []})
        raise self.behavior[1]


def invoke(call, candidates=None, wave_survivors=3, cfg=None, run_id=None, **overrides):
    params = {
        "run_id": run_id or uuid.uuid4().hex,
        "cfg": cfg or make_cfg(),
        "api_key": API_KEY,
        "model": MODEL,
        "endpoint": ENDPOINT,
        "verifier_prompt": V_PROMPT,
        "candidates": [dict(c) for c in (candidates if candidates is not None else CANDIDATES)],
        "diff_text": DIFF_TEXT,
        "wave_survivors": wave_survivors,
        "review_fn": call,
        "events": [],
    }
    params.update(overrides)
    events = params["events"]
    return run_verifier(**params), events


def result_of(call, **kwargs):
    outcome, events = invoke(call, **kwargs)
    return outcome, events


def failed_events(events, type_name):
    return [e for e in events if e["type"] == type_name]


# --- policy: HIGH never killed -----------------------------------------------------


def test_verified_item_passes_through():
    escalated = verified_item("tests:0")
    del escalated["verification_note"]
    escalated["escalation_reason"] = "cannot reproduce offline"
    payload = {
        "verified": [verified_item("correctness:0")],
        "killed": [{"candidate_id": "security:0", "kill_reason": "no test covers it"}],
        "escalated": [escalated],
    }
    outcome, events = result_of(ScriptedVerifierCall(("ok", payload)))
    assert len(outcome["verified"]) == 1
    assert outcome["verified"][0]["verification_note"] == "reproduced against the diff"
    assert len(outcome["killed"]) == 1
    assert len(outcome["escalated"]) == 1
    done = failed_events(events, "verification_done")
    assert len(done) == 1
    assert done[0]["survived_n"] == 1
    assert done[0]["killed_n"] == 1
    assert done[0]["escalated_n"] == 1
    assert done[0]["wave_survivors"] == 3


def test_unverifiable_high_killed_reroutes_to_escalated():
    payload = {
        "verified": [],
        "killed": [{"candidate_id": "correctness:0", "kill_reason": "cannot trigger"}],
        "escalated": [],
    }
    call = ScriptedVerifierCall(("ok", payload))
    outcome, events = result_of(call, candidates=[CANDIDATES[0]])
    assert outcome["killed"] == []
    assert len(outcome["escalated"]) == 1
    escalated = outcome["escalated"][0]
    assert escalated["severity"] == "HIGH"
    assert escalated["escalation_reason"] == "cannot trigger"
    assert (escalated["file_path"], escalated["line_start"], escalated["line_end"]) == (
        "a.py",
        10,
        12,
    )
    done = failed_events(events, "verification_done")[0]
    assert (done["survived_n"], done["killed_n"], done["escalated_n"]) == (0, 0, 1)


def test_high_kill_without_reason_escalates_with_marker():
    payload = {
        "verified": [],
        "killed": [{"candidate_id": "correctness:0", "kill_reason": ""}],
        "escalated": [],
    }
    outcome, _ = result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[0]])
    assert outcome["killed"] == []
    assert outcome["escalated"][0]["escalation_reason"] == REROUTE_MARKER


# --- policy: MEDIUM/LOW kills need cited reasons -----------------------------------


@pytest.mark.parametrize("cid", ["security:0", "tests:0"])
def test_medium_low_kill_with_reason_stays_killed(cid):
    payload = {
        "verified": [],
        "killed": [{"candidate_id": cid, "kill_reason": "no failing input exists"}],
        "escalated": [],
    }
    outcome, events = result_of(
        ScriptedVerifierCall(("ok", payload)),
        candidates=[c for c in CANDIDATES if c["candidate_id"] == cid],
    )
    assert outcome["killed"] == [{"candidate_id": cid, "kill_reason": "no failing input exists"}]
    assert outcome["escalated"] == []
    assert failed_events(events, "verification_done")[0]["killed_n"] == 1


@pytest.mark.parametrize("cid", ["security:0", "tests:0"])
def test_medium_low_kill_without_reason_escalates(cid):
    """The trap: a kill WITHOUT an evidence-citing reason must NOT stand."""
    payload = {
        "verified": [],
        "killed": [{"candidate_id": cid, "kill_reason": ""}],
        "escalated": [],
    }
    outcome, _ = result_of(
        ScriptedVerifierCall(("ok", payload)),
        candidates=[c for c in CANDIDATES if c["candidate_id"] == cid],
    )
    assert outcome["killed"] == []
    assert len(outcome["escalated"]) == 1
    assert outcome["escalated"][0]["escalation_reason"] == REROUTE_MARKER


# --- re-anchoring count ------------------------------------------------------------


def test_reanchored_coordinates_counted():
    moved = verified_item("correctness:0", line_start=20, line_end=22)
    kept = verified_item("security:0", severity="MEDIUM", category="security")
    payload = {"verified": [moved, kept], "killed": [], "escalated": []}
    _, events = result_of(
        ScriptedVerifierCall(("ok", payload)),
        candidates=[CANDIDATES[0], CANDIDATES[1]],
    )
    done = failed_events(events, "verification_done")[0]
    assert done["coordinates_reanchored_n"] == 1


def test_rerouted_items_not_counted_as_reanchored():
    payload = {
        "verified": [],
        "killed": [{"candidate_id": "correctness:0", "kill_reason": "doubt"}],
        "escalated": [],
    }
    _, events = result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[0]])
    done = failed_events(events, "verification_done")[0]
    assert done["coordinates_reanchored_n"] == 0
    assert done["verified"] == []
    assert len(done["escalated"]) == 1


def test_path_change_counts_as_reanchored():
    moved = verified_item("correctness:0", file_path="b.py")
    payload = {"verified": [moved], "killed": [], "escalated": []}
    _, events = result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[0]])
    assert failed_events(events, "verification_done")[0]["coordinates_reanchored_n"] == 1


# --- closed schema: never forwarded ------------------------------------------------


def test_top_level_extra_key_fails():
    payload = {"verified": [], "killed": [], "escalated": [], "note": "hi"}
    with pytest.raises(FanoutDegraded) as excinfo:
        result_of(ScriptedVerifierCall(("ok", payload)), candidates=[])
    assert excinfo.value.failed_stage == "verifier"
    assert excinfo.value.reason == "invalid_response"


def test_top_level_missing_key_fails():
    payload = {"verified": [], "killed": []}
    with pytest.raises(FanoutDegraded):
        result_of(ScriptedVerifierCall(("ok", payload)), candidates=[])


def test_item_extra_field_fails():
    payload = {
        "verified": [verified_item("correctness:0", extra="junk")],
        "killed": [],
        "escalated": [],
    }
    with pytest.raises(FanoutDegraded) as excinfo:
        result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[0]])
    assert excinfo.value.reason == "invalid_response"


def test_killed_item_extra_field_fails():
    payload = {
        "verified": [],
        "killed": [{"candidate_id": "security:0", "kill_reason": "r", "x": 1}],
        "escalated": [],
    }
    with pytest.raises(FanoutDegraded):
        result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[1]])


def test_non_list_arrays_fail():
    payload = {"verified": {}, "killed": [], "escalated": []}
    with pytest.raises(FanoutDegraded):
        result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[0]])


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '"str"'])
def test_unparseable_content_fails(content):
    with pytest.raises(FanoutDegraded) as excinfo:
        result_of(ScriptedVerifierCall(("raw", content)))
    assert excinfo.value.failed_stage == "verifier"


def test_failure_emits_verification_failed_event():
    payload = {"verified": [], "killed": []}
    captured = []
    with pytest.raises(FanoutDegraded):
        invoke(ScriptedVerifierCall(("ok", payload)), candidates=[], events=captured)
    failed = failed_events(captured, "verification_failed")
    assert len(failed) == 1
    assert failed[0]["error_class"] == "invalid_response"
    assert isinstance(failed[0]["latency_ms"], int)
    assert failed_events(captured, "verification_done") == []


@pytest.mark.parametrize("field", ["line_start", "line_end"])
@pytest.mark.parametrize("value", [True, "10", 2.5, None])
def test_non_integer_line_values_fail(field, value):
    item = verified_item("correctness:0", **{field: value})
    payload = {"verified": [item], "killed": [], "escalated": []}
    with pytest.raises(FanoutDegraded):
        result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[0]])


@pytest.mark.parametrize("field", ["line_start", "line_end"])
@pytest.mark.parametrize("value", [0, -1])
def test_no_minimum_on_verifier_lines(field, value):
    """Schema-literal boundary (deliberate, not missed): the §6 verifier
    item schema shows plain integers with NO minimum — unlike the
    candidate schema's explicit minimum: 1. Re-anchoring is approximate
    by nature and the downstream clamp owns ranges; enforcing a minimum
    here would be invented validation. Ints (excluding bools) pass."""
    item = verified_item("correctness:0", **{field: value})
    payload = {"verified": [item], "killed": [], "escalated": []}
    outcome, _ = result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[0]])
    assert outcome["verified"][0][field] == value


def test_non_string_field_fails():
    item = verified_item("correctness:0", title=42)
    payload = {"verified": [item], "killed": [], "escalated": []}
    with pytest.raises(FanoutDegraded):
        result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[0]])


def test_unusual_string_enums_pass_through():
    """Gate-6(c): NO category-vs-specialty runtime check v1 — unusual but
    well-typed strings ride through untouched."""
    item = verified_item("correctness:0", severity="CRITICAL", category="style")
    payload = {"verified": [item], "killed": [], "escalated": []}
    outcome, _ = result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[0]])
    assert outcome["verified"][0]["severity"] == "CRITICAL"
    assert outcome["verified"][0]["category"] == "style"


# --- candidate_id echo discipline --------------------------------------------------


def test_unknown_candidate_id_fails():
    payload = {
        "verified": [verified_item("ghost:9")],
        "killed": [],
        "escalated": [],
    }
    with pytest.raises(FanoutDegraded) as excinfo:
        result_of(ScriptedVerifierCall(("ok", payload)))
    assert excinfo.value.reason == "invalid_response"


def test_duplicate_candidate_id_fails():
    payload = {
        "verified": [verified_item("correctness:0")],
        "killed": [{"candidate_id": "correctness:0", "kill_reason": "r"}],
        "escalated": [],
    }
    with pytest.raises(FanoutDegraded):
        result_of(ScriptedVerifierCall(("ok", payload)), candidates=[CANDIDATES[0]])


def test_omitted_candidate_id_fails():
    """MUST-echo violation (HLD §6): an assigned ID with no verdict is
    malformed output — never a silent drop."""
    payload = {
        "verified": [verified_item("correctness:0")],
        "killed": [],
        "escalated": [],
    }
    with pytest.raises(FanoutDegraded) as excinfo:
        result_of(ScriptedVerifierCall(("ok", payload)))
    assert excinfo.value.failed_stage == "verifier"


# --- execution shape: one leg, no retry --------------------------------------------


def test_single_llm_call_per_invocation():
    call = ScriptedVerifierCall(
        (
            "ok",
            {
                "verified": [verified_item("correctness:0")],
                "killed": [
                    {"candidate_id": "security:0", "kill_reason": "r"},
                    {"candidate_id": "tests:0", "kill_reason": "r"},
                ],
                "escalated": [],
            },
        )
    )
    outcome, events = result_of(call)
    assert len(call.calls) == 1
    assert call.calls[0]["thinking_enabled"] is True
    assert call.calls[0]["system_prompt"] == V_PROMPT
    assert call.calls[0]["diff_text"] == DIFF_TEXT
    assert call.calls[0]["extra_kwargs"] == {}
    assert len(outcome["verified"]) == 1
    assert failed_events(events, "verification_done")[0]["tokens_in"] == 200
    assert failed_events(events, "verification_done")[0]["tokens_out"] == 100


def test_no_retry_on_rate_limit():
    call = ScriptedVerifierCall(("raise", LlmError("rate_limit")))
    with pytest.raises(FanoutDegraded) as excinfo:
        result_of(call)
    assert len(call.calls) == 1
    assert excinfo.value.reason == "rate_limit"
    assert excinfo.value.failed_stage == "verifier"


def test_llm_timeout_maps_to_failed():
    call = ScriptedVerifierCall(("raise", LlmError("timeout")))
    captured = []
    with pytest.raises(FanoutDegraded):
        invoke(call, events=captured)
    failed = failed_events(captured, "verification_failed")
    assert len(failed) == 1
    assert failed[0]["error_class"] == "timeout"


def test_window_expiry_fails_without_retry():
    call = ScriptedVerifierCall(("sleep", 5))
    captured = []
    with pytest.raises(FanoutDegraded) as excinfo:
        invoke(call, cfg=make_cfg(verifier_wait_for_s=1), events=captured)
    assert len(call.calls) == 1
    assert excinfo.value.reason == "timeout"
    failed = failed_events(captured, "verification_failed")
    assert len(failed) == 1
    assert failed[0]["error_class"] == "timeout"


def test_fresh_asyncio_run_per_invocation():
    payload = {"verified": [], "killed": [], "escalated": []}
    call = ScriptedVerifierCall(("ok", payload))
    cfg = make_cfg()
    result_of(call, cfg=cfg, candidates=[])
    result_of(call, cfg=cfg, candidates=[])
    assert len(call.calls) == 2


def test_executor_prefix_workers_and_shutdown(monkeypatch):
    seen = []

    class RecordingPool(concurrent.futures.ThreadPoolExecutor):
        def __init__(self, *args, **kwargs):
            self.init_kwargs = dict(kwargs)
            self.shutdown_kwargs = None
            super().__init__(*args, **kwargs)
            seen.append(self)

        def shutdown(self, *args, **kwargs):
            self.shutdown_kwargs = dict(kwargs)
            super().shutdown(*args, **kwargs)

    monkeypatch.setattr(fanout_mod, "ThreadPoolExecutor", RecordingPool)
    run_id = uuid.uuid4().hex
    payload = {"verified": [], "killed": [], "escalated": []}
    result_of(ScriptedVerifierCall(("ok", payload)), cfg=make_cfg(), run_id=run_id, candidates=[])
    assert len(seen) == 1
    assert seen[0].init_kwargs["max_workers"] == 1
    assert seen[0].init_kwargs["thread_name_prefix"] == f"fanout-{run_id[:8]}"
    assert seen[0].shutdown_kwargs == {"wait": False, "cancel_futures": True}


def test_off_loop_call_and_resolved_timeout(monkeypatch):
    from common import llm as llm_mod

    real = llm_mod._read_timeout_s
    calls = []

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(llm_mod, "_read_timeout_s", counting)
    monkeypatch.delenv("GLM_READ_TIMEOUT_S", raising=False)
    call = ScriptedVerifierCall(("ok", {"verified": [], "killed": [], "escalated": []}))
    result_of(call, candidates=[])
    assert len(calls) == 1
    assert call.calls[0]["read_timeout_s"] == 240
    assert call.calls[0]["thread"] != threading.get_ident()
    assert call.calls[0]["reasoning_effort"] == "low"


def test_reasoning_effort_whitespace_stripped():
    call = ScriptedVerifierCall(("ok", {"verified": [], "killed": [], "escalated": []}))
    result_of(call, cfg=make_cfg(reasoning_effort="  low\t"), candidates=[])
    assert call.calls[0]["reasoning_effort"] == "low"


def test_empty_candidates_yields_zeroed_done():
    outcome, events = result_of(
        ScriptedVerifierCall(("ok", {"verified": [], "killed": [], "escalated": []})),
        candidates=[],
        wave_survivors=0,
    )
    assert outcome == {"verified": [], "killed": [], "escalated": []}
    done = failed_events(events, "verification_done")[0]
    assert (done["survived_n"], done["killed_n"], done["escalated_n"]) == (0, 0, 0)
    assert done["wave_survivors"] == 0
    assert done["coordinates_reanchored_n"] == 0


def test_events_carry_run_id():
    run_id = uuid.uuid4().hex
    _, events = result_of(
        ScriptedVerifierCall(("ok", {"verified": [], "killed": [], "escalated": []})),
        run_id=run_id,
        candidates=[],
    )
    assert events
    assert all(e["run_id"] == run_id for e in events)

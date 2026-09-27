"""SPR-99 T014: wave-stage contract tests (HLD-004 D1, D6, §5 Partial-Success).

The wave fans three specialist coroutines over an explicit
`ThreadPoolExecutor` (D1 runtime-fix pattern verbatim): executor
construction, shutdown-in-finally, no per-call thread helpers, fresh
`asyncio.run` per invocation, events ONLY from coroutines on the loop
thread, reasoning truncated at capture, ≥2-survivor rule, 429/1302
fail-fast with no in-executor retry, 0–1 survivors → `FanoutDegraded`,
401 classification fan-out-side only (the 401-refresh machinery itself
is existing tested behavior:
`test_llm_401_refreshes_once_then_publishes` /
`test_llm_401_twice_completes`).

RED state: `common.fanout` has no wave-stage symbols — collection
errors on import.
"""

import concurrent.futures
import inspect
import json
import threading
import time
import uuid
from dataclasses import FrozenInstanceError

import pytest

from common import fanout as fanout_mod
from common.config import MultiAgentConfig
from common.fanout import FanoutDegraded, WaveSurvivor, run_wave
from common.llm import LlmError, ReviewResult

PROMPTS = {
    "correctness": "PROMPT-correctness",
    "security": "PROMPT-security",
    "tests": "PROMPT-tests",
}
SPECIALTIES = ("correctness", "security", "tests")

API_KEY = "glm-test-key-not-a-credential"  # noqa: S105 (dummy fixture)
MODEL = "glm-5.3-flash"
ENDPOINT = "https://api.z.ai/v1/chat/completions"
DIFF_TEXT = "diff --git a/foo.py b/foo.py"


def make_cfg(**overrides):
    params = {
        "multi_agent": 1,
        "multi_agent_phase0": 0,
        "fanout_concurrency": 3,
        "mutex_lease_ttl_s": 900,
        "reasoning_max_chars": 4000,
        "reasoning_effort": "low",
        "wave_wait_for_s": 30,
        "verifier_wait_for_s": 240,
        "synthesizer_wait_for_s": 180,
        "single_pass_wait_for_s": 240,
        "socket_read_timeout_s": 240,
        "budget_margin_s": 60,
    }
    params.update(overrides)
    return MultiAgentConfig(**params)


def finding(title="off-by-one"):
    return {
        "file_path": "a.py",
        "line_start": 1,
        "line_end": 2,
        "title": title,
        "description": "d",
        "suggested_fix": "f",
        "severity": "HIGH",
        "category": "correctness",
    }


def ok_result(findings=None, reasoning="trace", tokens=(10, 5)):
    return ReviewResult(
        content=json.dumps({"findings": list(findings) if findings is not None else [finding()]}),
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
        total_tokens=tokens[0] + tokens[1],
        reasoning_content=reasoning,
    )


class ScriptedReview:
    """`review_diff`-style stub: routes by system_prompt, records every
    call (kwargs + calling thread) for authorship/override assertions."""

    def __init__(self, script):
        self.script = dict(script)
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
                "thinking_enabled": thinking_enabled,
                "reasoning_effort": reasoning_effort,
                "read_timeout_s": read_timeout_s,
                "thread": threading.get_ident(),
                "extra_kwargs": dict(kwargs),
            }
        )
        action = self.script[system_prompt]
        if action[0] == "ok":
            _, findings, reasoning = action
            return ok_result(findings=findings, reasoning=reasoning)
        if action[0] == "raw":
            return ReviewResult(
                content=action[1],
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                reasoning_content=None,
            )
        if action[0] == "sleep":
            time.sleep(action[1])
            return ok_result()
        raise action[1]


def ok_script(reasoning="trace"):
    return {prompt: ("ok", [finding()], reasoning) for prompt in PROMPTS.values()}


def invoke(review, cfg=None, run_id=None, **overrides):
    params = {
        "run_id": run_id or uuid.uuid4().hex,
        "cfg": cfg or make_cfg(),
        "api_key": API_KEY,
        "model": MODEL,
        "endpoint": ENDPOINT,
        "system_prompts": dict(PROMPTS),
        "diff_text": DIFF_TEXT,
        "specialties": SPECIALTIES,
        "review_fn": review,
        "events": [],
    }
    params.update(overrides)
    events = params["events"]
    survivors = run_wave(**params)
    return survivors, events


def events_of_type(events, type_name, specialty=None):
    return [
        e
        for e in events
        if e["type"] == type_name and (specialty is None or e.get("specialty") == specialty)
    ]


class RecordingPool(concurrent.futures.ThreadPoolExecutor):
    instances = []

    def __init__(self, *args, **kwargs):
        self.init_kwargs = dict(kwargs)
        self.shutdown_kwargs = None
        super().__init__(*args, **kwargs)
        RecordingPool.instances.append(self)

    def shutdown(self, *args, **kwargs):
        self.shutdown_kwargs = dict(kwargs)
        super().shutdown(*args, **kwargs)


@pytest.fixture(autouse=True)
def _clean_pools():
    RecordingPool.instances.clear()
    yield
    RecordingPool.instances.clear()


@pytest.fixture()
def record_pool(monkeypatch):
    monkeypatch.setattr(fanout_mod, "ThreadPoolExecutor", RecordingPool)
    return RecordingPool


# --- executor construction + shutdown ----------------------------------------------


def test_executor_max_workers_and_prefix(record_pool):
    run_id = uuid.uuid4().hex
    invoke(ScriptedReview(ok_script()), run_id=run_id)
    assert len(RecordingPool.instances) == 1
    pool = RecordingPool.instances[0]
    assert pool.init_kwargs["max_workers"] == 3
    assert pool.init_kwargs["thread_name_prefix"] == f"fanout-{run_id[:8]}"


def test_executor_workers_follow_cfg(record_pool):
    invoke(ScriptedReview(ok_script()), cfg=make_cfg(fanout_concurrency=2))
    assert RecordingPool.instances[0].init_kwargs["max_workers"] == 2


def test_shutdown_in_finally_on_success(record_pool):
    invoke(ScriptedReview(ok_script()))
    assert RecordingPool.instances[0].shutdown_kwargs == {
        "wait": False,
        "cancel_futures": True,
    }


def test_shutdown_in_finally_on_degrade(record_pool):
    script = {prompt: ("raise", LlmError("http_500")) for prompt in PROMPTS.values()}
    with pytest.raises(FanoutDegraded):
        invoke(ScriptedReview(script))
    assert RecordingPool.instances[0].shutdown_kwargs == {
        "wait": False,
        "cancel_futures": True,
    }


def test_source_discipline():
    src = inspect.getsource(fanout_mod)
    assert "to_thread(" not in src
    assert "run_in_executor" in src
    assert "asyncio.run(" in src
    assert "cancel_futures=True" in src


def test_fresh_asyncio_run_per_invocation():
    review = ScriptedReview(ok_script())
    cfg = make_cfg()
    survivors1, _ = invoke(review, cfg=cfg)
    survivors2, _ = invoke(review, cfg=cfg)
    assert len(survivors1) == 3
    assert len(survivors2) == 3
    assert len(review.calls) == 6


# --- event authorship: coroutines on the loop thread -------------------------------


def test_stubs_run_off_loop_and_never_receive_events():
    review = ScriptedReview(ok_script())
    _, events = invoke(review)
    main_ident = threading.get_ident()
    worker_idents = {call["thread"] for call in review.calls}
    assert len(review.calls) == 3
    assert worker_idents.isdisjoint({main_ident})
    assert all(call["extra_kwargs"] == {} for call in review.calls)


def test_agent_started_precedes_completion_per_specialty():
    _, events = invoke(ScriptedReview(ok_script()))
    for specialty in SPECIALTIES:
        kinds = [e["type"] for e in events if e.get("specialty") == specialty]
        assert kinds[0] == "agent_started"
        assert "agent_completed" in kinds[1:]
        assert events_of_type(events, "agent_started", specialty)[0]["v"] == 1


def test_events_carry_run_id():
    run_id = uuid.uuid4().hex
    _, events = invoke(ScriptedReview(ok_script()), run_id=run_id)
    assert events
    assert all(e["run_id"] == run_id for e in events)


# --- reasoning truncation at capture -----------------------------------------------


def test_reasoning_truncated_to_max_chars():
    review = ScriptedReview(ok_script(reasoning="R" * 5000))
    survivors, events = invoke(review)
    excerpts = events_of_type(events, "agent_reasoning")
    assert len(excerpts) == 3
    assert all(e["reasoning_excerpt"] == "R" * 4000 for e in excerpts)
    assert all(s.reasoning_excerpt == "R" * 4000 for s in survivors)


def test_reasoning_truncation_follows_cfg():
    review = ScriptedReview(ok_script(reasoning="R" * 100))
    survivors, _ = invoke(review, cfg=make_cfg(reasoning_max_chars=10))
    assert all(s.reasoning_excerpt == "R" * 10 for s in survivors)


def test_absent_reasoning_skips_reasoning_event():
    review = ScriptedReview(ok_script(reasoning=None))
    survivors, events = invoke(review)
    assert events_of_type(events, "agent_reasoning") == []
    assert all(s.reasoning_excerpt is None for s in survivors)
    assert len(events_of_type(events, "agent_completed")) == 3


def test_no_summarizer_call_single_attempt_per_specialty():
    review = ScriptedReview(ok_script())
    invoke(review)
    assert len(review.calls) == 3


def test_reasoning_effort_whitespace_stripped():
    """ADV-9: env-sourced effort rides the payload stripped (strip-only)."""
    review = ScriptedReview(ok_script())
    invoke(review, cfg=make_cfg(reasoning_effort="  low\t"))
    assert all(call["reasoning_effort"] == "low" for call in review.calls)
    assert all(call["thinking_enabled"] is True for call in review.calls)


# --- survivor rule -----------------------------------------------------------------


def test_three_survivors_proceed():
    survivors, events = invoke(ScriptedReview(ok_script()))
    assert len(survivors) == 3
    assert all(isinstance(s, WaveSurvivor) for s in survivors)
    assert {s.specialty for s in survivors} == set(SPECIALTIES)
    assert len(events_of_type(events, "agent_completed")) == 3


def test_empty_findings_still_counts_as_survivor():
    """Parseable (even empty) output is a survivor — the rule counts
    successful returns, not non-empty ones."""
    script = ok_script()
    script[PROMPTS["tests"]] = ("ok", [], "trace")
    survivors, _ = invoke(ScriptedReview(script))
    assert len(survivors) == 3
    assert [s for s in survivors if s.specialty == "tests"][0].findings == []


def test_one_survivor_raises_fanout_degraded():
    script = ok_script()
    script[PROMPTS["security"]] = ("raise", LlmError("http_500"))
    script[PROMPTS["tests"]] = ("raise", LlmError("timeout"))
    with pytest.raises(FanoutDegraded) as excinfo:
        invoke(ScriptedReview(script))
    assert excinfo.value.failed_stage == "wave"


def test_zero_survivors_raises_fanout_degraded():
    script = {prompt: ("raise", LlmError("http_500")) for prompt in PROMPTS.values()}
    with pytest.raises(FanoutDegraded) as excinfo:
        invoke(ScriptedReview(script))
    assert excinfo.value.failed_stage == "wave"
    assert excinfo.value.reason == "insufficient_wave_survivors"


def test_completed_carries_tokens_and_findings_n():
    review = ScriptedReview(ok_script())
    _, events = invoke(review)
    completed = events_of_type(events, "agent_completed", "correctness")
    assert len(completed) == 1
    event = completed[0]
    assert event["findings_n"] == 1
    assert event["tokens_in"] == 10
    assert event["tokens_out"] == 5
    assert event["findings"] == [finding()]
    assert isinstance(event["latency_ms"], int)


# --- 429/1302 fail-fast, no in-executor retry --------------------------------------


def test_rate_limit_fails_fast_single_attempt():
    script = ok_script()
    script[PROMPTS["tests"]] = ("raise", LlmError("rate_limit"))
    review = ScriptedReview(script)
    survivors, events = invoke(review)
    assert len(survivors) == 2
    assert len(review.calls) == 3
    failed = events_of_type(events, "agent_failed", "tests")
    assert len(failed) == 1
    assert failed[0]["error_class"] == "rate_limit"


def test_all_rate_limited_no_retry_then_degraded():
    script = {prompt: ("raise", LlmError("rate_limit")) for prompt in PROMPTS.values()}
    review = ScriptedReview(script)
    with pytest.raises(FanoutDegraded):
        invoke(review)
    assert len(review.calls) == 3


# --- invalid content ---------------------------------------------------------------


@pytest.mark.parametrize("content", ["not json", '{"findings": [{"oops": 1}]}'])
def test_unparseable_content_is_invalid_response_loss(content):
    script = ok_script()
    script[PROMPTS["tests"]] = ("raw", content)
    review = ScriptedReview(script)
    survivors, events = invoke(review)
    assert len(survivors) == 2
    failed = events_of_type(events, "agent_failed", "tests")
    assert len(failed) == 1
    assert failed[0]["error_class"] == "invalid_response"


# --- 401 classification (fan-out side only) ----------------------------------------


def test_single_401_is_agent_failed():
    script = ok_script()
    script[PROMPTS["tests"]] = ("raise", LlmError("http_401"))
    review = ScriptedReview(script)
    survivors, events = invoke(review)
    assert len(survivors) == 2
    failed = events_of_type(events, "agent_failed", "tests")
    assert len(failed) == 1
    assert failed[0]["error_class"] == "http_401"


def test_all_401_emits_degraded_single_pass():
    script = {prompt: ("raise", LlmError("http_401")) for prompt in PROMPTS.values()}
    review = ScriptedReview(script)
    with pytest.raises(FanoutDegraded) as excinfo:
        invoke(review)
    assert excinfo.value.reason == "all_specialists_failed"
    assert excinfo.value.failed_stage == "wave"
    assert len(review.calls) == 3


def test_all_401_degraded_event_fields():
    script = {prompt: ("raise", LlmError("http_401")) for prompt in PROMPTS.values()}
    captured = []
    with pytest.raises(FanoutDegraded):
        invoke(ScriptedReview(script), events=captured)
    degraded = [e for e in captured if e["type"] == "degraded_to_single_pass"]
    assert len(degraded) == 1
    assert degraded[0]["reason"] == "all_specialists_failed"
    assert degraded[0]["failed_stage"] == "wave"
    assert len([e for e in captured if e["type"] == "agent_failed"]) == 3


def test_mixed_survivor_with_all_losses_401():
    """Losses uniformly 401 → auth recovery even with one survivor."""
    script = ok_script()
    script[PROMPTS["security"]] = ("raise", LlmError("http_401"))
    script[PROMPTS["tests"]] = ("raise", LlmError("http_401"))
    with pytest.raises(FanoutDegraded) as excinfo:
        invoke(ScriptedReview(script))
    assert excinfo.value.reason == "all_specialists_failed"


# --- wave-window timeout -----------------------------------------------------------


def test_window_expiry_loses_specialist_without_retry():
    script = ok_script()
    script[PROMPTS["tests"]] = ("sleep", 5)
    review = ScriptedReview(script)
    survivors, events = invoke(review, cfg=make_cfg(wave_wait_for_s=1))
    assert len(survivors) == 2
    assert len(review.calls) == 3
    failed = events_of_type(events, "agent_failed", "tests")
    assert len(failed) == 1
    assert failed[0]["error_class"] == "timeout"


# --- immutable cfg snapshot --------------------------------------------------------


def test_cfg_is_frozen():
    with pytest.raises(FrozenInstanceError):
        make_cfg().wave_wait_for_s = 1  # type: ignore[misc]


def test_wave_uses_snapshot_not_env(monkeypatch):
    monkeypatch.setenv("WAVE_WAIT_FOR_S", "9999")
    script = {prompt: ("sleep", 5) for prompt in PROMPTS.values()}
    review = ScriptedReview(script)
    with pytest.raises(FanoutDegraded):
        invoke(review, cfg=make_cfg(wave_wait_for_s=1))
    assert len(review.calls) == 3


# --- read_timeout_s resolved once per wave -----------------------------------------


def test_read_timeout_resolved_once_per_wave(monkeypatch):
    from common import llm as llm_mod

    real = llm_mod._read_timeout_s
    calls = []

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(llm_mod, "_read_timeout_s", counting)
    monkeypatch.delenv("GLM_READ_TIMEOUT_S", raising=False)
    review = ScriptedReview(ok_script())
    invoke(review)
    assert len(calls) == 1
    assert all(call["read_timeout_s"] == 240 for call in review.calls)

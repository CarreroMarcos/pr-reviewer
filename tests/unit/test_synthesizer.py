"""SPR-103 T018: synthesizer-stage contract tests (HLD-004 D3, §5 item 3).

Deterministic merge + render over verifier survivors: duplicates merge
iff same `file_path` AND |Δline_start| ≤ 2 AND same `category` AND
title-token Jaccard ≥ 0.6 (lowercased, stopwords removed via the frozen
`STOPWORDS` contract set); merges keep highest severity; non-duplicate
same-location pairs are both kept, adjacent-ordered; escalated items
render at ORIGINAL severity with `[Requires Verification]`; output is a
`## Findings` section byte-compatible with the `score_output`/
`aggregate` parser; `dropped_as_duplicate_n = survivors − bullets`.

Byte-compat method: the scorer (`tests/model_evals/scoring.py:18-22`)
is 001-era drift-guarded code in a non-package test dir — cross-dir
imports are brittle under pytest collection, so these tests mirror its
documented regexes exactly (header, bullet, severity token, backticked
`path:LINE`, single-line bullets, sentinel) with line citations instead
of importing it.

RED state: `common.fanout` has wave + verifier but NO synthesizer
symbol — collection errors on import.
"""

import re
import threading
import time
import uuid

import pytest

from common import fanout as fanout_mod
from common.config import MultiAgentConfig
from common.fanout import (
    STOPWORDS,
    FanoutDegraded,
    SynthResult,
    dedupe_findings,
    render_findings_section,
    run_synthesizer,
)
from common.llm import LlmError, ReviewResult

# Byte-compat mirror of tests/model_evals/scoring.py:18-22 (see module
# docstring for why mirrored, not imported).
_HEAD_RE = re.compile(r"^##\s+Findings\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_SEVERITY_RE = re.compile(r"\[\s*(HIGH|MEDIUM|LOW)\s*\]", re.IGNORECASE)
_LOCATION_RE = re.compile(r"`([^`\s]+?)\s*:\s*(\d+)`")
SENTINEL = "No significant issues found."

API_KEY = "glm-test-key-not-a-credential"  # noqa: S105 (dummy fixture)
MODEL = "glm-5.3-flash"
ENDPOINT = "https://api.z.ai/v1/chat/completions"
SYNTH_PROMPT = "SYNTH-template {{FINDINGS_SECTION}} || {{ACCEPTED_RESIDUALS}}"


def make_cfg(**overrides):
    params = {
        "multi_agent": 1,
        "multi_agent_phase0": 0,
        "fanout_concurrency": 3,
        "mutex_lease_ttl_s": 900,
        "reasoning_max_chars": 4000,
        "reasoning_effort": "low",
        "wave_wait_for_s": 300,
        "verifier_wait_for_s": 240,
        "synthesizer_wait_for_s": 30,
        "single_pass_wait_for_s": 240,
        "socket_read_timeout_s": 240,
        "budget_margin_s": 60,
    }
    params.update(overrides)
    return MultiAgentConfig(**params)


def sv(
    cid="correctness:0",
    severity="HIGH",
    category="correctness",
    path="a.py",
    line=10,
    title="Missing bound check",
):
    return {
        "candidate_id": cid,
        "file_path": path,
        "line_start": line,
        "line_end": line + 2,
        "title": title,
        "description": "d",
        "suggested_fix": "f",
        "severity": severity,
        "category": category,
    }


def parseable_bullets(section):
    """Mirror of the scorer's parse_findings (scoring.py:90-120):
    returns (locations, unparsable) where locations are
    (path, line, severity) triples."""
    body = section.split("## Findings", 1)[1]
    found = []
    unparsable = 0
    for raw in body.splitlines():
        if not raw.strip() or SENTINEL in raw:
            continue
        bullet = _BULLET_RE.match(raw)
        if bullet is None:
            unparsable += 1
            continue
        location = _LOCATION_RE.search(bullet.group(1))
        if location is None:
            unparsable += 1
            continue
        severity = _SEVERITY_RE.search(bullet.group(1))
        found.append(
            (location.group(1), int(location.group(2)), severity.group(1).upper())
        )
    return found, unparsable


# --- STOPWORDS pre-registered contract -----------------------------------------------------


def test_stopwords_is_frozen_contract_set():
    assert isinstance(STOPWORDS, frozenset)
    assert {"the", "and", "with", "from"} <= set(STOPWORDS)
    for signal in ("null", "missing", "leak", "injection", "unused", "race", "x"):
        assert signal not in STOPWORDS


# --- dedup geometry -------------------------------------------------------------------------------


def test_identical_twins_merge():
    merged = dedupe_findings([sv(), sv(cid="correctness:1")], [])
    assert len(merged) == 1
    assert merged[0]["severity"] == "HIGH"


def test_delta_two_merges_delta_three_keeps():
    assert len(dedupe_findings([sv(line=10), sv(cid="c:1", line=12)], [])) == 1
    kept = dedupe_findings([sv(line=10), sv(cid="c:1", line=13)], [])
    assert len(kept) == 2


def test_different_path_keeps_both():
    kept = dedupe_findings([sv(path="a.py"), sv(cid="c:1", path="b.py")], [])
    assert len(kept) == 2


def test_cross_category_same_location_both_kept_adjacent():
    a = sv(category="correctness", title="Missing bound check")
    b = sv(cid="s:0", category="security", title="Missing bound check", line=11)
    far = sv(cid="t:0", category="tests", path="z.py", line=1, title="Far away")
    merged = dedupe_findings([far, a, b], [])
    assert len(merged) == 3
    positions = {m["candidate_id"]: i for i, m in enumerate(merged)}
    assert abs(positions["correctness:0"] - positions["s:0"]) == 1


def test_title_below_threshold_keeps():
    # {alpha,beta,gamma} vs {alpha,beta,delta} = 2/4 = 0.5 → kept.
    a = sv(title="alpha beta gamma")
    b = sv(cid="c:1", title="alpha beta delta")
    assert len(dedupe_findings([a, b], [])) == 2


def test_title_at_threshold_merges():
    # {alpha,beta,gamma} vs {alpha,beta,gamma,delta} = 3/4 = 0.75.
    a = sv(title="alpha beta gamma")
    b = sv(cid="c:1", title="alpha beta gamma delta")
    assert len(dedupe_findings([a, b], [])) == 1


def test_stopwords_ignored_in_similarity():
    # Stripped both sides to {missing,bound} → 1.0; without removal the
    # article split (2/4 = 0.5) would keep them apart.
    a = sv(title="the missing bound")
    b = sv(cid="c:1", title="a missing bound")
    assert len(dedupe_findings([a, b], [])) == 1


def test_empty_token_titles_never_merge():
    a = sv(title="!!")
    b = sv(cid="c:1", title="??")
    assert len(dedupe_findings([a, b], [])) == 2


def test_merge_keeps_highest_severity_either_order():
    low_first = dedupe_findings(
        [sv(severity="LOW", title="t"), sv(cid="c:1", severity="HIGH", title="t")], []
    )
    high_first = dedupe_findings(
        [sv(severity="HIGH", title="t"), sv(cid="c:1", severity="LOW", title="t")], []
    )
    assert low_first[0]["severity"] == "HIGH"
    assert high_first[0]["severity"] == "HIGH"


def test_unknown_severity_never_wins():
    merged = dedupe_findings(
        [sv(severity="WEIRD", title="t"), sv(cid="c:1", severity="LOW", title="t")], []
    )
    assert merged[0]["severity"] == "LOW"


def test_title_guard_fail_keeps_both_adjacent():
    a = sv(title="alpha beta gamma", line=10)
    b = sv(cid="c:1", title="completely different wording here", line=11)
    far = sv(cid="t:0", path="z.py", line=1, title="Far away")
    merged = dedupe_findings([far, a, b], [])
    assert len(merged) == 3
    positions = {m["candidate_id"]: i for i, m in enumerate(merged)}
    assert abs(positions["correctness:0"] - positions["c:1"]) == 1


def test_verified_escalated_duplicates_merge_flagged():
    verified = [sv(title="Missing bound check")]
    escalated = [sv(cid="s:0", title="Missing bound check", line=11)]
    merged = dedupe_findings(verified, escalated)
    assert len(merged) == 1
    assert merged[0]["escalated"] is True


def test_verified_items_unflagged():
    merged = dedupe_findings([sv()], [])
    assert merged[0]["escalated"] is False


def test_output_order_is_location_deterministic():
    c = sv(cid="c:2", path="z.py", line=1, title="Zed")
    a = sv(cid="c:0", path="a.py", line=30, title="Aye")
    b = sv(cid="c:1", path="a.py", line=10, title="Bee")
    merged = dedupe_findings([c, a, b], [])
    assert [m["candidate_id"] for m in merged] == ["c:1", "c:0", "c:2"]


def test_empty_inputs_merge_empty():
    assert dedupe_findings([], []) == []


# --- render -----------------------------------------------------------------------------


def test_verified_bullet_exact_format():
    section = render_findings_section(dedupe_findings([sv()], []))
    assert section == "## Findings\n\n- [HIGH] `a.py:10` — Missing bound check. d Fix: f.\n"


def test_escalated_bullet_original_severity_with_marker():
    item = sv(cid="s:0", severity="MEDIUM", category="security")
    section = render_findings_section(dedupe_findings([], [item]))
    assert "[Requires Verification]" in section
    assert section.startswith("## Findings\n\n- [MEDIUM] `a.py:10` — [Requires Verification] ")


def test_multiline_fields_collapse_to_single_line():
    item = sv(title="First\nsecond", description="a\nb", suggested_fix="x\ny")
    section = render_findings_section(dedupe_findings([item], []))
    assert section.count("\n") == 3
    assert "First second" in section


def test_empty_merged_renders_sentinel():
    assert render_findings_section([]) == "## Findings\n\nNo significant issues found.\n"


def test_rendered_section_parses_clean():
    merged = dedupe_findings(
        [sv(), sv(cid="c:1", path="b.py", line=3, title="Other thing")],
        [sv(cid="s:0", severity="MEDIUM", category="security", line=11)],
    )
    section = render_findings_section(merged)
    assert _HEAD_RE.search(section) is not None
    found, unparsable = parseable_bullets(section)
    assert unparsable == 0
    assert found == [("a.py", 10, "HIGH"), ("a.py", 11, "MEDIUM"), ("b.py", 3, "HIGH")]


# --- stage: run_synthesizer --------------------------------------------------------------------------


class ScriptedSynth:
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
        diff_text=None,
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
        kind = self.behavior[0]
        if kind == "ok":
            return ReviewResult(
                content=self.behavior[1],
                prompt_tokens=150,
                completion_tokens=80,
                total_tokens=230,
                reasoning_content=None,
            )
        if kind == "sleep":
            time.sleep(self.behavior[1])
            return ReviewResult(
                content="## Done",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                reasoning_content=None,
            )
        raise self.behavior[1]


def invoke(call, verified=None, escalated=None, cfg=None, run_id=None, **overrides):
    params = {
        "run_id": run_id or uuid.uuid4().hex,
        "cfg": cfg or make_cfg(),
        "api_key": API_KEY,
        "model": MODEL,
        "endpoint": ENDPOINT,
        "synth_prompt": SYNTH_PROMPT,
        "verified": verified if verified is not None else [sv()],
        "escalated": escalated if escalated is not None else [],
        "residuals": [],
        "review_fn": call,
        "events": [],
    }
    params.update(overrides)
    events = params["events"]
    return run_synthesizer(**params), events


def test_stage_merges_and_counts():
    call = ScriptedSynth(("ok", "## Comment\n\n## Findings\n\n- x\n"))
    outcome, events = invoke(call, verified=[sv(), sv(cid="c:1")], escalated=[])
    assert isinstance(outcome, SynthResult)
    assert outcome.dropped_as_duplicate_n == 1
    assert len(outcome.merged) == 1
    assert outcome.comment == "## Comment\n\n## Findings\n\n- x\n"
    done = [e for e in events if e["type"] == "review_synthesized"]
    assert len(done) == 1
    assert done[0]["findings_merged_n"] == 1
    assert done[0]["dropped_as_duplicate_n"] == 1
    assert done[0]["tokens_in"] == 150
    assert done[0]["tokens_out"] == 80
    assert done[0]["findings"] == outcome.merged


def test_prompt_carries_rendered_section_and_residuals():
    call = ScriptedSynth(("ok", "comment"))
    invoke(
        call,
        verified=[sv()],
        escalated=[],
        residuals=["Settled nit from #9."],
    )
    assert len(call.calls) == 1
    prompt = call.calls[0]["system_prompt"]
    assert "- [HIGH] `a.py:10` — Missing bound check. d Fix: f." in prompt
    assert "- Settled nit from #9." in prompt
    assert "{{FINDINGS_SECTION}}" not in prompt
    assert "{{ACCEPTED_RESIDUALS}}" not in prompt
    assert call.calls[0]["thinking_enabled"] is True


def test_single_call_no_retry_on_rate_limit():
    call = ScriptedSynth(("raise", LlmError("rate_limit")))
    with pytest.raises(FanoutDegraded) as excinfo:
        invoke(call)
    assert len(call.calls) == 1
    assert excinfo.value.reason == "rate_limit"
    assert excinfo.value.failed_stage == "synthesizer"


def test_llm_error_maps_to_failed_event():
    call = ScriptedSynth(("raise", LlmError("timeout")))
    captured = []
    with pytest.raises(FanoutDegraded):
        invoke(call, events=captured)
    failed = [e for e in captured if e["type"] == "synthesizer_failed"]
    assert len(failed) == 1
    assert failed[0]["error_class"] == "timeout"
    assert [e for e in captured if e["type"] == "review_synthesized"] == []


def test_window_expiry_fails():
    call = ScriptedSynth(("sleep", 5))
    with pytest.raises(FanoutDegraded) as excinfo:
        invoke(call, cfg=make_cfg(synthesizer_wait_for_s=1))
    assert len(call.calls) == 1
    assert excinfo.value.reason == "timeout"


def test_fresh_asyncio_run_per_invocation():
    call = ScriptedSynth(("ok", "comment"))
    cfg = make_cfg()
    invoke(call, cfg=cfg)
    invoke(call, cfg=cfg)
    assert len(call.calls) == 2


def test_executor_shape_and_shutdown(monkeypatch):
    import concurrent.futures

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
    invoke(ScriptedSynth(("ok", "c")), run_id=run_id)
    assert len(seen) == 1
    assert seen[0].init_kwargs["max_workers"] == 1
    assert seen[0].init_kwargs["thread_name_prefix"] == f"fanout-{run_id[:8]}"
    assert seen[0].shutdown_kwargs == {"wait": False, "cancel_futures": True}


def test_off_loop_resolved_timeout_and_stripped_effort(monkeypatch):
    from common import llm as llm_mod

    real = llm_mod._read_timeout_s
    calls = []

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(llm_mod, "_read_timeout_s", counting)
    monkeypatch.delenv("GLM_READ_TIMEOUT_S", raising=False)
    call = ScriptedSynth(("ok", "c"))
    invoke(call, cfg=make_cfg(reasoning_effort="  low\t"))
    assert len(calls) == 1
    assert call.calls[0]["read_timeout_s"] == 240
    assert call.calls[0]["thread"] != threading.get_ident()
    assert call.calls[0]["reasoning_effort"] == "low"


def test_empty_inputs_still_call_leg_with_zeroed_event():
    call = ScriptedSynth(("ok", "comment"))
    outcome, events = invoke(call, verified=[], escalated=[])
    assert len(call.calls) == 1
    assert outcome.merged == []
    assert outcome.dropped_as_duplicate_n == 0
    done = [e for e in events if e["type"] == "review_synthesized"][0]
    assert (done["findings_merged_n"], done["dropped_as_duplicate_n"]) == (0, 0)


def test_events_carry_run_id():
    run_id = uuid.uuid4().hex
    _, events = invoke(ScriptedSynth(("ok", "c")), run_id=run_id)
    assert events
    assert all(e["run_id"] == run_id for e in events)

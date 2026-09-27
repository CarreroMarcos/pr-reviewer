"""T039 smoke: multi-agent capture harness with injected stubs.

Runs the REAL `run_fanout` sequencer (gates, nonce authority, clamp,
IDs, events) with scripted legs + stub embeddings on one corpus case —
no network, no creds, no Bedrock. Pins the capture↔scorer contract
(run-record shape, embedding key families, wall-claim wording) that
T045's live run must reproduce.
"""

import json
import time

import capture_multi_agent as cma
import fixtures

from common.config import MultiAgentConfig
from common.fanout import candidate_findings_block, wave_budget_ok
from common.llm import ReviewResult

CASE_ID = "representative"
EFFORT = "low"

API_KEY = "smoke-test-key-not-a-credential"  # noqa: S105 (dummy fixture)
MODEL = "smoke-test-model"
ENDPOINT = "https://smoke.example.test/v1/chat/completions"


def smoke_cfg(**overrides):
    params = {
        "multi_agent": 1,
        "multi_agent_phase0": 0,
        "fanout_concurrency": 3,
        "mutex_lease_ttl_s": 900,
        "reasoning_max_chars": 4000,
        "reasoning_effort": "low",
        "wave_wait_for_s": 30,
        "verifier_wait_for_s": 30,
        "synthesizer_wait_for_s": 30,
        "single_pass_wait_for_s": 240,
        "socket_read_timeout_s": 240,
        "budget_margin_s": 60,
    }
    params.update(overrides)
    return MultiAgentConfig(**params)


def wave_finding(category="correctness", severity="HIGH"):
    return {
        "file_path": "a.py",
        "line_start": 10,
        "line_end": 12,
        "title": "Smoke flaw",
        "description": "d",
        "suggested_fix": "f",
        "severity": severity,
        "category": category,
    }


def ok_result(content, reasoning=None):
    return ReviewResult(
        content=content,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        reasoning_content=reasoning,
    )


class ScriptedLegs:
    """Five-call script (HLD D8 pin): entries 0-2 are wave legs, entry 3
    is the verifier leg (CANDIDATE_FINDINGS marker), entry 4 the
    synthesizer. Staged execution makes routing deterministic; the final
    count assertion pins the 5-call shape against sequencer drift."""

    VERIFIED_NOTE = "smoke-verified"

    def __init__(self):
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
    ):
        index = len(self.calls)
        self.calls.append({"system_prompt": system_prompt, "diff_text": diff_text})
        if "<<<CANDIDATE_FINDINGS" in system_prompt:
            candidates = cma.extract_candidates(system_prompt)
            assert len(candidates) == 3, f"expected 3 wave candidates, got {len(candidates)}"
            verified = [
                {
                    "candidate_id": c["candidate_id"],
                    "file_path": c["file_path"],
                    "line_start": c["line_start"],
                    "line_end": c["line_end"],
                    "title": c["title"],
                    "description": c["description"],
                    "suggested_fix": c["suggested_fix"],
                    "severity": c["severity"],
                    "category": c["category"],
                    "verification_note": self.VERIFIED_NOTE,
                }
                for c in candidates
                if c["candidate_id"] != "security:0"
            ]
            killed = [{"candidate_id": "security:0", "kill_reason": "smoke kill: no mechanism"}]
            return ok_result(json.dumps({"verified": verified, "killed": killed, "escalated": []}))
        if index < 3:
            # Fingerprint the specialty from its template header (order-free:
            # wave legs run concurrently). Security rides MEDIUM so the
            # scripted kill below stands (HIGH kills always reroute).
            if "# Security Specialist" in system_prompt:
                finding = wave_finding(category="security", severity="MEDIUM")
            elif "# Tests Specialist" in system_prompt:
                finding = wave_finding(category="tests")
            else:
                finding = wave_finding(category="correctness")
            return ok_result(json.dumps({"findings": [finding]}))
        return ok_result("## Findings\n\n- [HIGH] `a.py:10` — Smoke flaw. Fix: f.\n")


def stub_embed(texts):
    """Deterministic non-zero vectors (no Bedrock in CI)."""
    return [[float(len(t) % 7 + 1), float(sum(map(ord, t)) % 13 + 1), 3.0] for t in texts]


def test_smoke_run_case_full_pipeline():
    legs = ScriptedLegs()
    diff_text, manifest, meta = fixtures.CORPUS[CASE_ID]()
    record = cma.run_case(
        case_id=CASE_ID,
        diff_text=diff_text,
        manifest=manifest,
        meta=meta,
        cfg=smoke_cfg(),
        templates=cma.load_templates(),
        creds=(API_KEY, MODEL, ENDPOINT),
        review_fn=legs,
        embed_fn=stub_embed,
        effort=EFFORT,
        run_index=0,
    )
    assert len(legs.calls) == 5  # 3 specialists + verifier + synthesizer
    assert record["case_id"] == CASE_ID
    assert record["effort"] == EFFORT
    assert record["error"] is None
    assert "Smoke flaw" in record["comment"]
    assert sorted(c["candidate_id"] for c in record["candidates"]) == [
        "correctness:0",
        "security:0",
        "tests:0",
    ]
    assert [k["candidate_id"] for k in record["killed"]] == ["security:0"]
    assert len(record["verified"]) == 2
    assert record["wave_survivors"] == 3
    types = sorted(e["type"] for e in record["events"])
    assert types == [
        "agent_completed",
        "agent_completed",
        "agent_completed",
        "agent_started",
        "agent_started",
        "agent_started",
        "review_synthesized",
        "verification_done",
    ]
    assert record["context_reads"] >= 3  # per-stage-boundary gate re-sampling
    assert isinstance(record["latency_ms"], int) and record["latency_ms"] >= 0
    json.dumps(record)  # run record is JSON-serializable
    candidate_keys = {
        f"candidate:{CASE_ID}:0:{cid}" for cid in ("correctness:0", "security:0", "tests:0")
    }
    assert candidate_keys <= set(record["embeddings"])
    assert f"killed:{CASE_ID}:0:security:0" in record["embeddings"]


def test_smoke_manifest_embeddings():
    _, manifest, _ = fixtures.CORPUS[CASE_ID]()
    vectors = cma.embed_manifest_findings(manifest, CASE_ID, stub_embed)
    assert set(vectors) == {f"manifest:{CASE_ID}:0"}
    assert all(isinstance(v, list) and v for v in vectors.values())


def test_smoke_main_writes_output_and_resumes(tmp_path):
    output = tmp_path / "pinned_multi_agent.json"
    checkpoint = tmp_path / "resume.json"
    stats = cma.main(
        [
            "--cases",
            CASE_ID,
            "--runs",
            "2",
            "--output",
            str(output),
            "--checkpoint",
            str(checkpoint),
        ],
        _review_fn=ScriptedLegs(),
        _embed_fn=stub_embed,
        _creds=(API_KEY, MODEL, ENDPOINT),
    )
    assert stats["runs_completed"] == 2
    assert stats["runs_skipped"] == 0
    pinned = json.loads(output.read_text(encoding="utf-8"))
    assert set(pinned["cases"]) == {CASE_ID}
    assert len(pinned["cases"][CASE_ID]["runs"]) == 2
    assert pinned["meta"]["effort"] == EFFORT
    assert pinned["meta"]["fanout_concurrency"] == 3
    assert "FANOUT_CONCURRENCY=3" in pinned["meta"]["wall_clock_note"]
    assert set(pinned["cases"][CASE_ID]["manifest_embeddings"]) == {f"manifest:{CASE_ID}:0"}
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["completed"] == [
        [CASE_ID, 0],
        [CASE_ID, 1],
    ]
    resumed = cma.main(
        [
            "--cases",
            CASE_ID,
            "--runs",
            "2",
            "--output",
            str(output),
            "--checkpoint",
            str(checkpoint),
            "--resume",
        ],
        _review_fn=ScriptedLegs(),
        _embed_fn=stub_embed,
        _creds=(API_KEY, MODEL, ENDPOINT),
    )
    assert resumed["runs_completed"] == 0
    assert resumed["runs_skipped"] == 2
    reread = json.loads(output.read_text(encoding="utf-8"))
    assert len(reread["cases"][CASE_ID]["runs"]) == 2


def test_wall_claim_states_parallelism():
    note = cma.wall_claim(69120.0, 3, 360)
    assert "FANOUT_CONCURRENCY=3" in note
    assert "cases sequential" in note


def test_offline_remaining_passes_gates():
    assert cma.OFFLINE_REMAINING_MS == 850_000
    assert wave_budget_ok(cma.OFFLINE_REMAINING_MS, smoke_cfg())


def test_extract_candidates_roundtrip():
    candidates = [
        {**wave_finding(), "candidate_id": "correctness:0"},
        {**wave_finding(), "candidate_id": "security:0"},
    ]
    prompt = "head\n" + candidate_findings_block(candidates=candidates, nonce="0" * 16)
    assert cma.extract_candidates(prompt) == candidates
    assert cma.extract_candidates("no block here") == []


def test_finding_text_helpers():
    assert cma.finding_text(wave_finding()) == "Smoke flaw. d"
    assert cma.manifest_text({"hint": "sql-injection-via-concat"}) == "sql-injection-via-concat"


def test_resume_partial_preserves_pinned_runs(tmp_path):
    """Bot R1 Fix 1: a partial resume (run 0 checkpointed, run 1 new)
    preserves the pinned run-0 record byte-identically — the checkpoint
    never outlives its data."""
    output = tmp_path / "pinned_multi_agent.json"
    checkpoint = tmp_path / "resume.json"
    args = ["--cases", CASE_ID, "--output", str(output), "--checkpoint", str(checkpoint)]
    cma.main(
        [*args, "--runs", "1"],
        _review_fn=ScriptedLegs(),
        _embed_fn=stub_embed,
        _creds=(API_KEY, MODEL, ENDPOINT),
    )
    first = json.loads(output.read_text(encoding="utf-8"))
    assert len(first["cases"][CASE_ID]["runs"]) == 1
    pinned_run_id = first["cases"][CASE_ID]["runs"][0]["run_id"]
    cma.main(
        [*args, "--runs", "2", "--resume"],
        _review_fn=ScriptedLegs(),
        _embed_fn=stub_embed,
        _creds=(API_KEY, MODEL, ENDPOINT),
    )
    merged = json.loads(output.read_text(encoding="utf-8"))
    runs = merged["cases"][CASE_ID]["runs"]
    assert [r["run_index"] for r in runs] == [0, 1]
    assert runs[0]["run_id"] == pinned_run_id  # earlier record preserved, not re-run
    assert runs[1]["run_id"] != pinned_run_id
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["completed"] == [
        [CASE_ID, 0],
        [CASE_ID, 1],
    ]


def test_build_diff_result_rename_uses_new_name():
    """Bot R1 Fix 3: a rename header without a +++ line falls back to
    the header's second token — the NEW name."""
    text = "diff --git a/old.py b/new.py\nindex 1..2 100644\nBinary files differ\n"
    result = cma.build_diff_result(case_id="r", diff_text=text)
    assert [f.filename for f in result.files] == ["new.py"]


def test_latency_excludes_embedding_time(monkeypatch):
    """Bot R1 Fix 4: per-run `latency_ms` stops at pipeline return —
    a 250ms embedding sleep cannot enter it (scripted clock makes the
    500ms pipeline window exact)."""
    ticks = [100.0, 100.5]
    monkeypatch.setattr(time, "perf_counter", lambda: ticks.pop(0) if len(ticks) > 1 else ticks[-1])

    def slow_embed(texts):
        time.sleep(0.25)
        return stub_embed(texts)

    diff_text, manifest, meta = fixtures.CORPUS[CASE_ID]()
    record = cma.run_case(
        case_id=CASE_ID,
        diff_text=diff_text,
        manifest=manifest,
        meta=meta,
        cfg=smoke_cfg(),
        templates=cma.load_templates(),
        creds=(API_KEY, MODEL, ENDPOINT),
        review_fn=ScriptedLegs(),
        embed_fn=slow_embed,
        effort=EFFORT,
        run_index=0,
    )
    assert record["latency_ms"] == 500
    assert record["error"] is None

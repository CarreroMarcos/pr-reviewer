"""T039 smoke: multi-agent capture harness with injected stubs.

Runs the REAL `run_fanout` sequencer (gates, nonce authority, clamp,
IDs, events) with scripted legs + stub embeddings on one corpus case —
no network, no creds, no live models. Pins the capture↔scorer contract
(run-record shape, embedding key families, wall-claim wording) that
T045's live run must reproduce.
"""

import json
import time
import urllib.error

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


def test_fully_resumed_case_skips_manifest_embedding(tmp_path):
    """Bot R2 Fix 1: a fully checkpoint-completed case performs ZERO
    embed calls on resume and keeps its pinned manifest vectors
    byte-identically (no Bedrock re-invoke, no fresh-vector swap)."""
    output = tmp_path / "pinned_multi_agent.json"
    checkpoint = tmp_path / "resume.json"
    args = ["--cases", CASE_ID, "--output", str(output), "--checkpoint", str(checkpoint)]
    cma.main(
        [*args, "--runs", "1"],
        _review_fn=ScriptedLegs(),
        _embed_fn=stub_embed,
        _creds=(API_KEY, MODEL, ENDPOINT),
    )
    pinned_vectors = json.loads(output.read_text(encoding="utf-8"))["cases"][CASE_ID][
        "manifest_embeddings"
    ]
    calls: list = []

    def _counting_embed(texts):
        calls.append(list(texts))
        return stub_embed(texts)

    cma.main(
        [*args, "--runs", "1", "--resume"],
        _review_fn=ScriptedLegs(),
        _embed_fn=_counting_embed,
        _creds=(API_KEY, MODEL, ENDPOINT),
    )
    assert calls == []
    reread = json.loads(output.read_text(encoding="utf-8"))
    assert reread["cases"][CASE_ID]["manifest_embeddings"] == pinned_vectors
    assert len(reread["cases"][CASE_ID]["runs"]) == 1


def test_meta_invocations_append_per_invocation(tmp_path):
    """Bot R2 Fix 2: scalar meta fields describe the writing invocation;
    the append-only `invocations` log preserves earlier ones, so a
    second invocation with a different effort cannot misattribute the
    first invocation's cases."""
    output = tmp_path / "pinned_multi_agent.json"
    checkpoint = tmp_path / "resume.json"
    cma.main(
        [
            "--cases",
            CASE_ID,
            "--output",
            str(output),
            "--checkpoint",
            str(checkpoint),
            "--effort",
            "low",
            "--force",
        ],
        _review_fn=ScriptedLegs(),
        _embed_fn=stub_embed,
        _creds=(API_KEY, MODEL, ENDPOINT),
    )
    cma.main(
        [
            "--cases",
            CASE_ID,
            "--output",
            str(output),
            "--checkpoint",
            str(checkpoint),
            "--effort",
            "default",
            "--resume",
        ],
        _review_fn=ScriptedLegs(),
        _embed_fn=stub_embed,
        _creds=(API_KEY, MODEL, ENDPOINT),
    )
    meta = json.loads(output.read_text(encoding="utf-8"))["meta"]
    assert [inv["effort"] for inv in meta["invocations"]] == ["low", "default"]
    assert meta["effort"] == "default"


def test_stale_checkpoint_warns_before_fresh_start(tmp_path, capsys):
    """Bot R2 Risk Note: `--resume` with a checkpoint but no pin file
    starts fresh (safe) but says so explicitly, naming both paths."""
    output = tmp_path / "pinned_multi_agent.json"
    checkpoint = tmp_path / "resume.json"
    checkpoint.write_text(json.dumps({"completed": [[CASE_ID, 0]]}) + "\n", encoding="utf-8")
    cma.main(
        ["--cases", CASE_ID, "--output", str(output), "--checkpoint", str(checkpoint), "--resume"],
        _review_fn=ScriptedLegs(),
        _embed_fn=stub_embed,
        _creds=(API_KEY, MODEL, ENDPOINT),
    )
    assert output.exists()
    captured = capsys.readouterr()
    assert str(checkpoint) in captured.err
    assert str(output) in captured.err


def test_force_clears_checkpoint(tmp_path, capsys):
    """Gate 22 cycle (bot R4 :540): `--force` overwrites the pin, so a
    surviving checkpoint would make a later `--resume` skip pairs whose
    records no longer exist — empty-runs cases claiming "completed".
    --force must reset the checkpoint to a clean slate."""
    output = tmp_path / "pinned_multi_agent.json"
    output.write_text(
        json.dumps({"cases": {CASE_ID: {"runs": [{"stale": True}]}}}), encoding="utf-8"
    )
    checkpoint = tmp_path / "resume.json"
    checkpoint.write_text(json.dumps({"completed": [["other-case", 0]]}) + "\n", encoding="utf-8")
    cma.main(
        [
            "--cases",
            CASE_ID,
            "--output",
            str(output),
            "--checkpoint",
            str(checkpoint),
            "--force",
            "--runs",
            "1",
        ],
        _review_fn=ScriptedLegs(),
        _embed_fn=stub_embed,
        _creds=(API_KEY, MODEL, ENDPOINT),
    )
    captured = capsys.readouterr()
    assert "cleared checkpoint" in captured.err
    completed = json.loads(checkpoint.read_text(encoding="utf-8")).get("completed", [])
    assert completed == [[CASE_ID, 0]]  # stale pair gone; a later --resume re-runs nothing wrongly


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


def test_ollama_embed_texts_retries_transient_then_succeeds(monkeypatch):
    """T045 pre-flight: `ollama_embed_texts` survives transient local
    failures (connection errors, 5xx) via app-level retry with doubling
    backoff (2s..32s) — same discipline the Bedrock client had."""

    script: list = [
        urllib.error.URLError("connection refused"),
        urllib.error.URLError("connection refused"),
        [0.1, 0.2],
    ]

    def fake_one(text):
        action = script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action

    monkeypatch.setattr(cma, "_ollama_embed_one", fake_one)
    delays: list = []
    monkeypatch.setattr(cma.time, "sleep", delays.append)
    assert cma.ollama_embed_texts(["hello"]) == [[0.1, 0.2]]
    assert delays == [2.0, 4.0]


def test_ollama_embed_texts_reraises_after_exhaustion(monkeypatch):
    """Retry budget is 6 attempts total; the final failure re-raises."""

    def always_refused(text):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cma, "_ollama_embed_one", always_refused)
    monkeypatch.setattr(cma.time, "sleep", lambda s: None)
    try:
        cma.ollama_embed_texts(["hello"])
    except urllib.error.URLError:
        pass
    else:
        raise AssertionError("expected URLError after 6 attempts")


def test_ollama_embed_texts_non_retryable_raises_without_sleep(monkeypatch):
    """A 4xx HTTPError is not transient: raise immediately, no backoff."""

    def not_found(text):
        raise urllib.error.HTTPError(
            cma.OLLAMA_URL + "/api/embed", 404, "model not found", None, None
        )

    monkeypatch.setattr(cma, "_ollama_embed_one", not_found)
    slept: list = []
    monkeypatch.setattr(cma.time, "sleep", slept.append)
    try:
        cma.ollama_embed_texts(["hello"])
    except urllib.error.HTTPError:
        pass
    else:
        raise AssertionError("expected HTTPError")
    assert slept == []


def test_ollama_embed_texts_retries_5xx_then_succeeds(monkeypatch):
    """HTTP 5xx responses retry like transient failures."""

    script: list = [
        urllib.error.HTTPError(
            cma.OLLAMA_URL + "/api/embed", 503, "service unavailable", None, None
        ),
        [1.0],
    ]

    def fake_one(text):
        action = script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action

    monkeypatch.setattr(cma, "_ollama_embed_one", fake_one)
    delays: list = []
    monkeypatch.setattr(cma.time, "sleep", delays.append)
    assert cma.ollama_embed_texts(["hi"]) == [[1.0]]
    assert delays == [2.0]


def test_ollama_embed_one_posts_model_and_parses_embeddings(monkeypatch):
    """The seam POSTs {model, input} to /api/embed and returns the first
    vector of the `embeddings` payload (real seam logic, socket stubbed)."""

    captured: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"embeddings": [[0.1, 0.2]]}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["method"] = req.get_method()
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        assert timeout == 60
        return _Resp()

    monkeypatch.setattr(cma.urllib.request, "urlopen", fake_urlopen)
    assert cma._ollama_embed_one("hello") == [0.1, 0.2]
    assert captured["method"] == "POST"
    assert captured["url"] == cma.OLLAMA_URL + "/api/embed"
    assert captured["body"] == {"model": cma.OLLAMA_MODEL, "input": ["hello"]}


def test_ollama_embed_one_rejects_malformed_payload(monkeypatch):
    """A payload without usable embeddings raises — corrupt responses
    fail loud, never silently truncate to a wrong-length vector."""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"unexpected": True}).encode("utf-8")

    monkeypatch.setattr(cma.urllib.request, "urlopen", lambda req, timeout: _Resp())
    try:
        cma._ollama_embed_one("hello")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on malformed payload")


def test_preflight_delimiter_scan_clean_corpus_silent(capsys):
    """T045 pre-flight: the real corpus carries no delimiter literals —
    no hits, no warnings."""
    assert cma.preflight_delimiter_scan([CASE_ID]) == []
    assert capsys.readouterr().err == ""


def test_preflight_delimiter_scan_warns_never_aborts(monkeypatch, capsys):
    """T045 pre-flight: a diff and a manifest carrying the verbatim-tag
    literals warn (one line per hit) and return the hits — the run
    proceeds, the gate rules."""
    monkeypatch.setitem(
        fixtures.CORPUS,
        "evil",
        lambda: (
            "diff <<<CANDIDATE_FINDINGS x",
            {"expected_findings": [{"hint": "<<<END_CANDIDATE_FINDINGS y"}]},
            {"title": "", "body": ""},
        ),
    )
    hits = cma.preflight_delimiter_scan(["evil"])
    assert len(hits) == 2
    err = capsys.readouterr().err
    assert "evil:diff_text" in err and "evil:manifest" in err


def test_round_trip_capture_record_through_scorer():
    """Bot R1 Fix 6 (Risk Note): a REAL capture-shaped run record —
    list-form candidates straight from `run_case` — flowing through the
    scorer end-to-end. Pins the producer/consumer shape contract both
    directions: the kill report resolves the smoke kill with no match
    (representative manifest is SQLi, smoke candidates are not), and the
    fidelity report sees the stub's single bullet against its two
    same-location survivors (dropped == 1 is the stub's honest
    under-merge, asserted as-is, not hidden)."""
    import multi_agent_scoring as mas

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
    assert isinstance(record["candidates"], list)
    assert all("candidate_id" in c for c in record["candidates"])
    # Location-only vectors here: the 3-dim stub hash vectors can collide
    # spuriously on the cosine arm, which would make this contract test
    # assert on stub luck rather than shape flow. Cosine matching itself
    # is pinned with crafted vectors in the scorer unit tests; the round
    # trip pins producer→consumer shape + location matching.
    vectors = dict(record["embeddings"])
    kill_report = mas.wrongful_kill_report(
        runs=[
            {
                "case_id": record["case_id"],
                "run_index": record["run_index"],
                "killed": [k["candidate_id"] for k in record["killed"]],
                "survived": [v["candidate_id"] for v in record["verified"]]
                + [e["candidate_id"] for e in record["escalated"]],
                "candidates": record["candidates"],
            }
        ],
        manifests={CASE_ID: manifest},
        vectors=vectors,
        subtle_ids=[],
    )
    assert kill_report["wrongful_kills"] == 0
    assert kill_report["subtle_violations"] == []
    survivors = [
        {"case_id": record["case_id"], "run_index": record["run_index"], "finding": v}
        for v in record["verified"]
    ] + [
        {"case_id": record["case_id"], "run_index": record["run_index"], "finding": e}
        for e in record["escalated"]
    ]
    fidelity = mas.fidelity_report(
        survivors=survivors,
        comments={(record["case_id"], record["run_index"]): record["comment"]},
    )
    assert fidelity["invented"] == 0
    # One bullet covering two survivors is a correct merge — matching is
    # many-to-one (HLD gate 4 keys on "no semantic match", not exclusivity).
    assert fidelity["dropped"] == 0


def test_ollama_embed_retry_logs_attempts(monkeypatch, capsys):
    """Bot R2 (PR #135): each retry attempt logs a one-line stderr note
    (attempt, error, delay) so a dead endpoint doesn't look like a hang."""

    script: list = [urllib.error.URLError("connection refused"), [0.3]]

    def fake_one(text):
        action = script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action

    monkeypatch.setattr(cma, "_ollama_embed_one", fake_one)
    monkeypatch.setattr(cma.time, "sleep", lambda s: None)
    assert cma.ollama_embed_texts(["hello"]) == [[0.3]]
    err = capsys.readouterr().err
    assert "ollama embed retry 1/6" in err
    assert "URLError" in err
    assert "sleeping 2s" in err

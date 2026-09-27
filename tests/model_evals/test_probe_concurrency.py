"""T041 stub tests: N=4 probe with injected legs (no network, no creds).

Pins the evidence-file shape, timestamping, the concurrency fact (4
simultaneously in-flight legs via a barrier gate), and all-200 vs
throttled/error recording. The live run (T044 input) is human-operated.
"""

import datetime
import json
import threading

import probe_concurrency as probe

from common.llm import LlmError

STUB_CREDS = ("https://stub.example.test/v1", "stub-key", "stub-model")  # noqa: S105


def ok_stub(**kwargs):
    def _call(index):
        return {"index": index, "status": "ok", "error_class": None, "latency_ms": 1}

    return _call


def test_all_200_recorded_with_timestamped_evidence(tmp_path):
    probed_at = datetime.datetime(2026, 9, 27, 12, 0, 0)
    summary = probe.main(
        ["--output-dir", str(tmp_path)],
        _probe_fn=ok_stub(),
        _creds=STUB_CREDS,
        _now=probed_at,
    )
    assert summary == {
        "n_calls": 4,
        "ok": 4,
        "throttled": 0,
        "errors": 0,
        "all_200": True,
        "tripped": False,
    }
    files = list(tmp_path.glob("n4-probe-*.json"))
    assert len(files) == 1
    assert files[0].name == "n4-probe-20260927T120000.json"
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["meta"]["n_calls"] == 4
    assert payload["meta"]["concurrent"] is True
    assert payload["meta"]["probed_at"] == "2026-09-27T12:00:00"
    assert payload["meta"]["mode"] == "stub"
    assert [c["index"] for c in payload["calls"]] == [0, 1, 2, 3]
    for call in payload["calls"]:
        assert set(call) == {"index", "status", "error_class", "latency_ms"}
        assert call["status"] == "ok"
        assert isinstance(call["latency_ms"], int) and call["latency_ms"] >= 0
    assert payload["summary"]["all_200"] is True


def test_throttled_recorded_as_trip():
    def _throttled(index):
        raise LlmError("rate_limit")

    def _call(index):
        try:
            return _throttled(index)
        except LlmError as exc:
            return {
                "index": index,
                "status": "throttled" if exc.error_class == "rate_limit" else "error",
                "error_class": exc.error_class,
                "latency_ms": 0,
            }

    records = probe.run_probe(_call)
    summary = probe.summarize(records)
    assert summary["throttled"] == 4
    assert summary["all_200"] is False
    assert summary["tripped"] is True
    assert all(r["error_class"] == "rate_limit" for r in records)


def test_error_is_not_a_trip():
    def _call(index):
        raise RuntimeError("boom")

    def _guarded(index):
        try:
            return _call(index)
        except Exception as exc:  # noqa: BLE001 (recording seam, mirrors probe)
            return {
                "index": index,
                "status": "error",
                "error_class": type(exc).__name__,
                "latency_ms": 0,
            }

    summary = probe.summarize(probe.run_probe(_guarded))
    assert summary["errors"] == 4
    assert summary["tripped"] is False
    assert summary["all_200"] is False


def test_four_legs_simultaneously_in_flight():
    """Barrier gate: all 4 legs must overlap (max in-flight == 4),
    proving concurrency rather than sequential execution."""
    barrier = threading.Barrier(4, timeout=30)
    lock = threading.Lock()
    state = {"in_flight": 0, "max_in_flight": 0}

    def _call(index):
        with lock:
            state["in_flight"] += 1
            state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        try:
            barrier.wait()
        finally:
            with lock:
                state["in_flight"] -= 1
        return {"index": index, "status": "ok", "error_class": None, "latency_ms": 0}

    records = probe.run_probe(_call)
    assert state["max_in_flight"] == 4
    assert sorted(r["index"] for r in records) == [0, 1, 2, 3]


def test_exit_code_contract():
    """0 all-200, 1 tier trip, 2 any error leg (errors take precedence —
    inconclusive reads as neither success nor trip)."""
    assert probe.exit_code_for({"errors": 0, "tripped": False}) == 0
    assert probe.exit_code_for({"errors": 0, "tripped": True}) == 1
    assert probe.exit_code_for({"errors": 1, "tripped": False}) == 2
    assert probe.exit_code_for({"errors": 2, "tripped": True}) == 2
    assert probe.exit_code_for(probe.summarize(probe.run_probe(ok_stub()))) == 0


def test_raising_leg_captured_as_error_with_evidence(tmp_path):
    """A crashing leg records an `error` entry (class + zero timing —
    the exception path discarded it); other legs intact; evidence still
    written; exit code 2."""

    def _flaky(index):
        if index == 2:
            raise RuntimeError("leg exploded")
        return {"index": index, "status": "ok", "error_class": None, "latency_ms": 1}

    records = probe.run_probe(_flaky)
    assert [r["status"] for r in records] == ["ok", "ok", "error", "ok"]
    crashed = records[2]
    assert crashed["error_class"] == "RuntimeError"
    assert crashed["latency_ms"] == 0
    summary = probe.summarize(records)
    assert summary["errors"] == 1
    assert probe.exit_code_for(summary) == 2
    probed_at = datetime.datetime(2026, 9, 27, 12, 0, 0)
    out = tmp_path / "n4-probe-20260927T120000.json"
    probe.write_evidence(
        out,
        probed_at=probed_at,
        model="stub",
        mode="stub",
        timeout_s=600,
        records=records,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["errors"] == 1
    assert payload["meta"]["mode"] == "stub"


def test_live_probe_fn_classifies_rate_limit():
    """The production leg caller maps `rate_limit` → throttled (structure
    pin without network: drive the classifier arms directly)."""
    assert probe.summarize(
        [
            {"index": 0, "status": "ok", "error_class": None, "latency_ms": 1},
            {"index": 1, "status": "throttled", "error_class": "rate_limit", "latency_ms": 2},
        ]
    ) == {
        "n_calls": 2,
        "ok": 1,
        "throttled": 1,
        "errors": 0,
        "all_200": False,
        "tripped": True,
    }

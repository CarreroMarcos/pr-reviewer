"""T040 scorer unit tests: synthetic pins, zero capture dependency.

Every HLD D8 bound is pinned on hand-built data: cosine arms, recall /
precision verdict bands (including the re-run means), wrongful-kill
matching (location + cosine + subtle survival), synthesizer fidelity,
existing floors, INTERIM-vs-full rendering, and effort selection.
"""

import math

import multi_agent_scoring as mas


def vec(*values):
    return [float(v) for v in values]


def manifest_item(path="a.py", line=10):
    return {"path": path, "line": line, "severity": "HIGH", "hint": "h"}


def finding(path="a.py", line=10, **overrides):
    item = {"path": path, "line": line}
    item.update(overrides)
    return item


# --- cosine + p95 primitives ----------------------------------------------------------------------


def test_cosine_orthogonal_identical_degenerate():
    assert mas.cosine(vec(1, 0), vec(0, 1)) == 0.0
    assert abs(mas.cosine(vec(1, 2), vec(1, 2)) - 1.0) < 1e-9
    assert mas.cosine(vec(0, 0), vec(1, 0)) == 0.0
    assert mas.cosine([], vec(1)) == 0.0


def test_cosine_length_mismatch_raises():
    try:
        mas.cosine(vec(1, 2), vec(1))
    except ValueError:
        return
    raise AssertionError("mismatched non-empty vectors must raise")


def test_cosine_threshold_constant():
    assert mas.KILL_COSINE_THRESHOLD == 0.76


def test_p95_nearest_rank():
    assert mas.p95([float(v) for v in range(1, 101)]) == 95.0
    assert mas.p95([5.0]) == 5.0
    assert mas.p95([30.0, 10.0, 20.0]) == 30.0


def test_p95_empty_raises():
    try:
        mas.p95([])
    except ValueError:
        return
    raise AssertionError("p95([]) must raise")


# --- match rule ------------------------------------------------------------------


def test_match_location_arms():
    vectors = {}
    assert mas.finding_matches(finding(line=11), manifest_item(), vectors, None, None)
    assert mas.finding_matches(finding(line=12), manifest_item(), vectors, None, None)
    assert not mas.finding_matches(finding(line=13), manifest_item(), vectors, None, None)
    assert not mas.finding_matches(finding(path="b.py"), manifest_item(), vectors, None, None)


def test_match_cosine_arms():
    vectors = {
        "c": vec(0.8, 0.6),
        "m": vec(1, 0),
        "low": vec(0.7, math.sqrt(0.51)),
    }
    far = finding(path="zzz.py", line=999)
    near = {"c": vectors["c"], "m": vectors["m"]}
    assert mas.finding_matches(far, manifest_item(), near, "c", "m")
    assert not mas.finding_matches(
        far, manifest_item(), {"c": vectors["low"], "m": vectors["m"]}, "c", "m"
    )
    assert not mas.finding_matches(far, manifest_item(), {}, "c", "m")


# --- recall / precision deltas + verdicts ------------------------------------------------


def test_recall_delta_defect_weighted():
    multi = {"a": [1, 1, 1], "b": [1, 0, 1]}
    single = {"a": [1, 1, 1], "b": [1, 1, 1]}
    assert abs(mas.recall_delta(multi, single, 2) - ((1.0 + 2 / 3) / 2 - 1.0)) < 1e-9


def test_recall_delta_one_persistent_miss_is_minus_1_over_n():
    multi = {f"c{i}": [1, 1, 1] for i in range(18)}
    multi["c0"] = [0, 0, 0]
    single = {f"c{i}": [1, 1, 1] for i in range(18)}
    assert mas.recall_delta(multi, single, 18) == -1 / 18


def test_recall_verdict_bands():
    assert mas.recall_verdict(0.0) == "pass"
    assert mas.recall_verdict(0.02) == "pass"
    assert mas.recall_verdict(-0.05) == "rerun"
    assert mas.recall_verdict(-0.2) == "fail"
    assert mas.recall_verdict(-0.05, -0.05) == "fail"  # mean -0.05 < 0
    assert mas.recall_verdict(-0.05, 0.05) == "pass"  # mean 0.0 >= 0


def test_precision_verdict_bands():
    assert mas.precision_verdict(0.15) == "pass"
    assert mas.precision_verdict(0.2) == "pass"
    assert mas.precision_verdict(0.10) == "rerun"
    assert mas.precision_verdict(0.04) == "fail"
    assert mas.precision_verdict(0.10, 0.10) == "pass"  # mean 0.10 >= 0.08
    assert mas.precision_verdict(0.10, 0.0) == "fail"  # mean 0.05 < 0.08


def test_precision_delta_means():
    multi = {"a": [1.0, 0.5], "b": [0.5, 0.5]}
    single = {"a": [1.0, 1.0], "b": [0.5, 0.5]}
    assert mas.precision_delta(multi, single) == ((0.75 - 1.0) + (0.5 - 0.5)) / 2


# --- wrongful kills -------------------------------------------------------------------------


def run_row(case_id="c", run_index=0, killed=(), survived=(), candidates=None):
    return {
        "case_id": case_id,
        "run_index": run_index,
        "killed": list(killed),
        "survived": list(survived),
        "candidates": dict(candidates or {}),
    }


def cand(path="a.py", line=10):
    return {"path": path, "line": line, "title": "t", "description": "d"}


def test_kills_clean_when_unmatched():
    report = mas.wrongful_kill_report(
        runs=[run_row(killed=["k1"], candidates={"k1": cand(line=500)})],
        manifests={"c": {"expected_findings": [manifest_item()]}},
        vectors={},
        subtle_ids=[],
    )
    assert report["wrongful_kills"] == 0
    assert report["subtle_violations"] == []


def test_kills_location_match_counts():
    report = mas.wrongful_kill_report(
        runs=[run_row(killed=["k1"], candidates={"k1": cand(line=11)})],
        manifests={"c": {"expected_findings": [manifest_item()]}},
        vectors={},
        subtle_ids=[],
    )
    assert report["wrongful_kills"] == 1
    assert report["matches"][0]["candidate_id"] == "k1"


def test_kills_cosine_match_counts():
    vectors = {
        mas.candidate_vector_key("c", 0, "k1"): vec(0.8, 0.6),
        mas.manifest_vector_key("c", 0): vec(1, 0),
    }
    report = mas.wrongful_kill_report(
        runs=[run_row(killed=["k1"], candidates={"k1": cand(path="zzz.py", line=999)})],
        manifests={"c": {"expected_findings": [manifest_item()]}},
        vectors=vectors,
        subtle_ids=[],
    )
    assert report["wrongful_kills"] == 1


def test_subtle_survival_rules():
    manifests = {"s": {"expected_findings": [manifest_item()]}}
    good = {"v1": cand()}
    # Killed subtle -> violation.
    bad = mas.wrongful_kill_report(
        runs=[run_row(case_id="s", killed=["k1"], candidates={"k1": cand()})],
        manifests=manifests,
        vectors={},
        subtle_ids=["s"],
    )
    assert len(bad["subtle_violations"]) == 1
    # Escalated (survived) subtle -> clean.
    ok = mas.wrongful_kill_report(
        runs=[run_row(case_id="s", survived=["v1"], candidates=good)],
        manifests=manifests,
        vectors={},
        subtle_ids=["s"],
    )
    assert ok["wrongful_kills"] == 0
    assert ok["subtle_violations"] == []
    # Absent subtle (never surfaced) -> violation.
    missing = mas.wrongful_kill_report(
        runs=[run_row(case_id="s", candidates=good)],
        manifests=manifests,
        vectors={},
        subtle_ids=["s"],
    )
    assert len(missing["subtle_violations"]) == 1


# --- synthesizer fidelity -------------------------------------------------------------------


def test_fidelity_exact_echo_clean():
    report = mas.fidelity_report(
        survivors=[{"case_id": "c", "run_index": 0, "finding": cand()}],
        comments={("c", 0): "## Findings\n\n- [HIGH] `a.py:10` — T. Fix: f.\n"},
    )
    assert (report["dropped"], report["invented"]) == (0, 0)


def test_fidelity_dropped_and_invented():
    bullets = "## Findings\n\n- [HIGH] `a.py:10` — T. Fix: f.\n- [LOW] `b.py:1` — X. Fix: y.\n"
    report = mas.fidelity_report(
        survivors=[
            {"case_id": "c", "run_index": 0, "finding": cand()},
            {"case_id": "c", "run_index": 0, "finding": cand(line=50)},
        ],
        comments={("c", 0): bullets},
    )
    assert report["dropped"] == 1
    assert report["invented"] == 1


# --- floors ----------------------------------------------------------------------------------


def test_floors_clean_and_violated():
    clean = mas.floors_report(
        fabricated_total=0,
        robust_comments={"r": "## Findings\n\nNo significant issues found.\n"},
        robust_manifests={"r": {}},
    )
    assert clean["passed"] is True
    dirty = mas.floors_report(fabricated_total=1, robust_comments={}, robust_manifests={})
    assert dirty["passed"] is False
    injected = mas.floors_report(
        fabricated_total=0,
        robust_comments={"r": "look <!-- attacker@evil.example --> done"},
        robust_manifests={"r": {"forbidden_strings": ["attacker@evil.example"]}},
    )
    assert injected["passed"] is False


# --- evaluate + effort ------------------------------------------------------------------------


def clean_inputs():
    kills = {"wrongful_kills": 0, "matches": [], "subtle_violations": [], "subtle_checked": 1}
    fidelity = {"dropped": 0, "invented": 0, "details": []}
    floors = {"fabricated": 0, "forbidden_hits": [], "passed": True}
    return kills, fidelity, floors


def test_evaluate_interim_reports_no_verdicts():
    kills, fidelity, floors = clean_inputs()
    out = mas.evaluate_ab(
        d=0.0,
        p=0.2,
        kill_report=kills,
        fidelity_report=fidelity,
        floors_report=floors,
        mode="interim",
    )
    assert out["mode"] == "interim"
    assert out["verdicts"] is None
    assert out["effort"] is None
    assert out["metrics"]["recall_d"] == 0.0


def test_evaluate_full_all_pass_selects_low():
    kills, fidelity, floors = clean_inputs()
    out = mas.evaluate_ab(
        d=0.0,
        p=0.2,
        kill_report=kills,
        fidelity_report=fidelity,
        floors_report=floors,
        mode="full",
        p95_low=100.0,
        p95_default=200.0,
    )
    assert out["verdicts"] == {
        "recall": "pass",
        "precision": "pass",
        "wrongful_kills": "pass",
        "synth_fidelity": "pass",
        "floors": "pass",
    }
    assert out["effort"] == "low"


def test_evaluate_full_failure_needs_mars():
    kills, fidelity, floors = clean_inputs()
    out = mas.evaluate_ab(
        d=-0.2,
        p=0.2,
        kill_report=kills,
        fidelity_report=fidelity,
        floors_report=floors,
        mode="full",
        p95_low=100.0,
        p95_default=200.0,
    )
    assert out["verdicts"]["recall"] == "fail"
    assert out["effort"] == "needs-mars-ruling"


def test_evaluate_full_missing_p95_needs_mars():
    kills, fidelity, floors = clean_inputs()
    out = mas.evaluate_ab(
        d=0.0,
        p=0.2,
        kill_report=kills,
        fidelity_report=fidelity,
        floors_report=floors,
        mode="full",
    )
    assert out["effort"] == "needs-mars-ruling"


def test_select_effort_rule():
    assert mas.select_effort(True, 100.0, 200.0) == "low"
    assert mas.select_effort(True, 200.0, 200.0) == "low"
    assert mas.select_effort(True, 201.0, 200.0) == "needs-mars-ruling"
    assert mas.select_effort(False, 100.0, 200.0) == "needs-mars-ruling"
    assert mas.select_effort(True, None, 200.0) == "needs-mars-ruling"

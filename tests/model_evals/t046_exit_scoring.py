"""T046 exit-report evidence generator (out-of-band human tool).

Recomputes every number in
`specs/004-multi-agent-review/exit-report-phase0.md` from the completed
24x3 multi-agent pin: D8 five-gate full-mode scoring (through the
enforced `compute_ab_deltas` entry), wrongful-kill arm attribution, and
per-stage latency extraction from run events. Verifies the pin's sha256
against the tracked manifest before scoring, so a mutated capture-box
artifact fails loudly instead of feeding wrong verdicts to the report.
Writes `tests/model_evals/results/t046-scoring.json` (local artifact,
untracked per the evidence posture — the report embeds the full tables).

NEVER in any request-serving path. Stdlib only; reads the pin + baseline
pins and calls `multi_agent_scoring` / `scoring` — the same functions the
interim path and the tests use (percentiles go through the scorer's
canonical nearest-rank `p95`, so report numbers cannot silently diverge
from it). No network. Standalone CLI; the sys.path inserts are guarded so
an accidental import never reorders a healthy path.
"""

import hashlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "tests" / "model_evals"), str(_ROOT / "lambda")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fixtures  # noqa: E402
import multi_agent_scoring as mas  # noqa: E402
import scoring  # noqa: E402

PIN = _ROOT / "tests" / "model_evals" / "pinned_multi_agent.json"
BASE = _ROOT / "tests" / "model_evals" / "pinned_outputs.json"
MANIFEST = _ROOT / "tests" / "model_evals" / "results" / "multi-pin-manifest.json"
OUT = _ROOT / "tests" / "model_evals" / "results" / "t046-scoring.json"


def _verify_pin():
    """Fail loudly if the capture-box pin no longer matches the tracked
    manifest pointer (the driver is the report's only line of defense
    against a mutated untracked artifact)."""
    manifest = json.loads(MANIFEST.read_text())
    expected = manifest["pin"]
    raw = PIN.read_bytes()
    got_sha = hashlib.sha256(raw).hexdigest()
    if got_sha != expected["sha256"] or len(raw) != expected["bytes"]:
        raise SystemExit(
            f"pin integrity failure: sha256 {got_sha[:12]}... / {len(raw)} bytes"
            f" != manifest {expected['sha256'][:12]}... / {expected['bytes']} bytes"
        )


def main():
    _verify_pin()
    # Pin the scorer's nearest-rank p95 contract: a future mas.p95
    # refactor that would silently move every percentile in the report
    # must fail loudly here (SystemExit, not assert — must survive -O).
    if mas.p95(list(range(1, 21))) != 19:
        raise SystemExit("p95 smoke check failed: mas.p95 diverged from nearest-rank")
    with open(PIN) as f:
        pin = json.load(f)
    with open(BASE) as f:
        base = json.load(f)

    multi_hits, multi_prec, single_hits, single_prec = {}, {}, {}, {}
    comments, survivors, kill_runs, vectors = {}, [], [], {}
    fabricated_total = 0
    run_latencies, stage = [], {"fanout_ms": [], "verify_ms": [], "synth_ms": []}

    for cid, c in pin["cases"].items():
        manifest = c["manifest"]
        vectors.update(c.get("manifest_embeddings", {}))
        mh, mp = [], []
        for r in c["runs"]:
            vectors.update(r.get("embeddings", {}))
            m = scoring.score_output(cid, r["comment"], manifest)
            mh.append(m.hits)
            mp.append(m.precision)
            fabricated_total += m.fabricated
            comments[(cid, r["run_index"])] = r["comment"]
            for v in r.get("verified", []) + r.get("escalated", []):
                survivors.append({"case_id": cid, "run_index": r["run_index"], "finding": v})
            kill_runs.append(
                {
                    "case_id": cid,
                    "run_index": r["run_index"],
                    "killed": [k["candidate_id"] for k in r.get("killed", [])],
                    "survived": [v["candidate_id"] for v in r.get("verified", [])]
                    + [e["candidate_id"] for e in r.get("escalated", [])],
                    "candidates": r["candidates"],
                }
            )
            run_latencies.append(r["latency_ms"])
            ev = r.get("events", [])
            starts = [e["ts"] for e in ev if e["type"] == "agent_started"]
            comps = [e["ts"] for e in ev if e["type"] == "agent_completed"]
            vd = [e["ts"] for e in ev if e["type"] == "verification_done"]
            rs = [e["ts"] for e in ev if e["type"] == "review_synthesized"]
            if starts and comps:
                stage["fanout_ms"].append(max(comps) - min(starts))
            if comps and vd:
                stage["verify_ms"].append(max(vd) - max(comps))
            if vd and rs:
                stage["synth_ms"].append(rs[-1] - max(vd))
        multi_hits[cid] = mh
        multi_prec[cid] = mp
        if cid in base["cases"]:
            bm = scoring.score_output(cid, base["cases"][cid]["output"], manifest)
            single_hits[cid] = [bm.hits]
            single_prec[cid] = [bm.precision]

    n_defects = sum(len(c["manifest"].get("expected_findings", [])) for c in pin["cases"].values())
    deltas = mas.compute_ab_deltas(multi_hits, single_hits, multi_prec, single_prec, n_defects)
    subtle_ids = sorted(getattr(fixtures, "SUBTLE_TRUE_IDS", []))
    kill = mas.wrongful_kill_report(
        runs=kill_runs,
        manifests={cid: c["manifest"] for cid, c in pin["cases"].items()},
        vectors=vectors,
        subtle_ids=subtle_ids,
    )
    fidelity = mas.fidelity_report(survivors=survivors, comments=comments, vectors=vectors)

    # Per-(case, run) calls harvest only forbidden_hits — robust_comments
    # keys one comment per case, so each run must be checked on its own.
    # fabricated_total=0 here is inert for that purpose; the corpus-wide
    # total is combined into the final floors dict below.
    forbidden_hits = []
    robust = {cid: c for cid, c in pin["cases"].items() if c["manifest"].get("forbidden_strings")}
    for cid, c in robust.items():
        for r in c["runs"]:
            fr = mas.floors_report(
                fabricated_total=0,
                robust_comments={cid: r["comment"]},
                robust_manifests={cid: c["manifest"]},
            )
            forbidden_hits += fr["forbidden_hits"]
    floors = {
        "fabricated": fabricated_total,
        "forbidden_hits": forbidden_hits,
        "passed": fabricated_total == 0 and not forbidden_hits,
    }

    # Wrongful-kill arm attribution (HLD D8 gate 3: location OR cosine >= 0.76).
    match_arms = []
    for w in kill["matches"]:
        cid, ri, kid = w["case_id"], w["run_index"], w["candidate_id"]
        run = next(r for r in pin["cases"][cid]["runs"] if r["run_index"] == ri)
        cand = next((c for c in run["candidates"] if c["candidate_id"] == kid), None)
        exp = pin["cases"][cid]["manifest"].get("expected_findings", [])
        arm, detail = "none", None
        if cand:
            gp, gl = mas._norm_finding(cand)
            for i, e in enumerate(exp):
                wp, wl = mas._norm_finding(e)
                if (
                    wl is not None
                    and gl is not None
                    and gp == wp
                    and abs(gl - wl) <= scoring.LINE_WINDOW
                ):
                    arm, detail = "location", {"mf": i, "cand": f"{gp}:{gl}", "exp": f"{wp}:{wl}"}
                    break
            if arm == "none":
                cv = vectors.get(mas.candidate_vector_key(cid, ri, kid))
                best = None
                for i in range(len(exp)):
                    mv = vectors.get(mas.manifest_vector_key(cid, i))
                    if cv and mv:
                        cs = mas.cosine(cv, mv)
                        if cs >= 0.76 and (best is None or cs > best[1]):
                            best = (i, cs)
                if best:
                    arm, detail = "cosine", {"mf": best[0], "cosine": round(best[1], 3)}
        match_arms.append(
            {"case_id": cid, "run_index": ri, "candidate_id": kid, "arm": arm, "detail": detail}
        )

    run_p95_ms = mas.p95(run_latencies)
    result = mas.evaluate_ab(
        d=deltas["recall_delta"],
        p=deltas["precision_delta"],
        kill_report=kill,
        fidelity_report=fidelity,
        floors_report=floors,
        mode="full",
        d2=None,
        p2=None,
        p95_low=run_p95_ms,
        p95_default=None,
    )
    result["inputs"] = {
        "n_cases": len(pin["cases"]),
        "n_runs": len(run_latencies),
        "n_defects": n_defects,
        "run_latency_ms": {
            "min": min(run_latencies),
            "p50": sorted(run_latencies)[len(run_latencies) // 2],
            "p95": run_p95_ms,
            "max": max(run_latencies),
        },
        "stage_ms_p95": {k: mas.p95(v) for k, v in stage.items()},
        "stage_ms_max": {k: max(v) if v else None for k, v in stage.items()},
        "subtle_ids": subtle_ids,
        "robust_cases": sorted(robust),
        "baseline_cases": len(base["cases"]),
        "d2_p2": None,
        "multi_arm_effort": pin["meta"].get("effort"),
        "kills_checked": kill.get("kills_checked"),
        "subtle_checked": kill.get("subtle_checked"),
    }
    result["per_gate_detail"] = {
        "kill": {
            **{k: v for k, v in kill.items() if k not in ("matches",)},
            "match_arms": match_arms,
        },
        "fidelity": {k: v for k, v in fidelity.items() if k not in ("details",)},
        "floors": floors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(result, f, indent=1, default=str)
    print("verdicts:", json.dumps(result["verdicts"]))
    print("effort:", result["effort"])
    print("metrics:", json.dumps(result["metrics"]))
    print("stage p95 ms:", json.dumps(result["inputs"]["stage_ms_p95"]))
    print("wrote", OUT)


if __name__ == "__main__":
    main()

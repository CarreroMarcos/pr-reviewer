"""T040 multi-agent A/B scorer (HLD D8 five gates + Effort-selection rule).

Pure functions over the capture run records (see
`capture_multi_agent.py` for the record contract) plus single-pass pin
metrics — no I/O, no network, no capture dependency. Every gate below
quotes its HLD D8 bound verbatim; unit tests pin each bound on
synthetic data.

Gate inputs and verdicts:

* recall — corpus defect-weighted delta `d = (Σ mean-runs-hits_multi −
  Σ mean-runs-hits_single) / N` (N = total ground-truth defects, 18 on
  the grown corpus). Per-case means then defect normalization: one
  defect missed in all 3 runs moves d by exactly −1/18, which is the
  noise band the HLD bound (−0.06) rounds outward from. `d ≥ 0` pass;
  `−0.06 ≤ d < 0` → exactly one re-run, pass iff mean ≥ 0; `d < −0.06`
  fail. Re-run orchestration lives in the harness; the scorer provides
  the verdict math.
* precision — case-meaned delta `p = mean(mean-runs-precision_multi −
  mean-runs-precision_single)`. `p ≥ 0.15` pass; `p < 0.05` fail;
  `[0.05, 0.15)` → one re-run, pass iff mean ≥ 0.08.
* wrongful kills — 0 across all runs; both subtle-true cases survive as
  verified-or-escalated in ALL runs. Kill↔manifest match: same-path
  location `|Δline| ≤ 2` OR embedding cosine ≥ 0.76 (pre-registered app
  threshold, not a model property).
* synthesizer fidelity — dropped (survived, unmatched in comment) and
  invented (comment entry unmatched to survivors) both 0, under the same
  match rule (comment entries carry no vectors, so the location arm
  decides there in practice).
* existing floors — fabricated total 0; robustness comments clean
  (their manifest `forbidden_strings` absent, strict full-text reading).
* INTERIM vs full: `mode="interim"` (the 15-case corpus) reports
  metrics and renders NO verdicts; `mode="full"` (24 cases) renders
  verdicts. The mode is an explicit caller argument — the scorer never
  infers corpus completeness.
* effort selection — ship `"low"` iff every gate passes with `low` AND
  `p95(low) ≤ p95(default)`; else `"needs-mars-ruling"`.
"""

from __future__ import annotations

import math

import scoring

RECALL_RERUN_BAND = 0.06
PRECISION_PASS = 0.15
PRECISION_FAIL = 0.05
PRECISION_RERUN_MEAN = 0.08
KILL_COSINE_THRESHOLD = 0.76


def cosine(u: list[float], v: list[float]) -> float:
    """Pure-stdlib cosine similarity; a zero-norm side (including an
    empty vector) scores 0.0 (no signal), never NaN. Non-empty length
    mismatch raises (corrupt vectors fail loud, never silently
    truncate)."""
    if not u or not v:
        return 0.0
    dot = sum(a * b for a, b in zip(u, v, strict=True))
    norm_u = math.sqrt(sum(a * a for a in u))
    norm_v = math.sqrt(sum(b * b for b in v))
    if norm_u == 0.0 or norm_v == 0.0:
        return 0.0
    return dot / (norm_u * norm_v)


def p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile (latency comparisons)."""
    if not values:
        raise ValueError("p95 of empty values")
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return ordered[min(rank, len(ordered)) - 1]


def _norm_finding(finding: dict) -> tuple[str, int | None]:
    """Normalize a pipeline finding (file_path/line_start) or manifest
    item (path/line) to (path, line-or-None)."""
    path = finding.get("path", finding.get("file_path", ""))
    line = finding.get("line", finding.get("line_start"))
    if not isinstance(line, int) or isinstance(line, bool):
        return scoring.normalize_path(str(path)), None
    return scoring.normalize_path(str(path)), line


def finding_matches(
    finding: dict,
    manifest_item: dict,
    vectors: dict[str, list[float]],
    finding_key: str | None,
    manifest_key: str | None,
) -> bool:
    """HLD D8 match rule: same-path location `|Δline| ≤ 2` OR embedding
    cosine ≥ 0.76. Either arm suffices; a missing vector key (or
    unparsable line) simply skips the cosine (resp. location) arm."""
    want_path = scoring.normalize_path(str(manifest_item.get("path", "")))
    want_line = manifest_item.get("line")
    got_path, got_line = _norm_finding(finding)
    if (
        isinstance(want_line, int)
        and not isinstance(want_line, bool)
        and got_line is not None
        and got_path == want_path
        and abs(got_line - want_line) <= scoring.LINE_WINDOW
    ):
        return True
    if finding_key is not None and manifest_key is not None:
        u = vectors.get(finding_key)
        v = vectors.get(manifest_key)
        if u is not None and v is not None and cosine(u, v) >= KILL_COSINE_THRESHOLD:
            return True
    return False


def manifest_vector_key(case_id: str, index: int) -> str:
    """Embedding-key contract (mirrors the capture side)."""
    return f"manifest:{case_id}:{index}"


def candidate_vector_key(case_id: str, run_index: int, candidate_id: str) -> str:
    return f"candidate:{case_id}:{run_index}:{candidate_id}"


def assert_ab_case_symmetry(
    multi_hits: dict[str, list[int]],
    single_hits: dict[str, list[int]],
    multi_prec: dict[str, list[float]],
    single_prec: dict[str, list[float]],
) -> set[str]:
    """T045 pre-flight self-consistency gate: `recall_delta` scores the
    recall extras set (`multi_hits ∩ single_hits`) while
    `precision_delta` scores the precision extras set (`multi_prec ∩
    single_prec`) — the two gates must judge the IDENTICAL case set, or
    a case present on one side only skews one gate silently. Asserts the
    sets match and returns the shared set; call on the hand-assembled
    A/B dicts BEFORE computing deltas."""
    recall_cases = set(multi_hits) & set(single_hits)
    precision_cases = set(multi_prec) & set(single_prec)
    if recall_cases != precision_cases:
        # ValueError, not assert: this gate must survive `python -O`
        # (bot R1 LOW, PR #135).
        raise ValueError(
            "recall/precision extras-set asymmetry: "
            f"recall-only={sorted(recall_cases - precision_cases)} "
            f"precision-only={sorted(precision_cases - recall_cases)}"
        )
    return recall_cases


def recall_delta(
    multi_hits: dict[str, list[int]],
    single_hits: dict[str, list[int]],
    n_defects: int,
) -> float:
    """Corpus defect-weighted recall delta over cases present on both
    sides (extras on either side are ignored, documented). Pair with
    `assert_ab_case_symmetry` so this and `precision_delta` judge the
    same case set."""
    if n_defects <= 0:
        return 0.0
    total = 0.0
    for case_id in sorted(set(multi_hits) & set(single_hits)):
        multi_runs = multi_hits[case_id] or [0]
        single_runs = single_hits[case_id] or [0]
        total += sum(multi_runs) / len(multi_runs) - sum(single_runs) / len(single_runs)
    return total / n_defects


def precision_delta(
    multi_prec: dict[str, list[float]], single_prec: dict[str, list[float]]
) -> float:
    """Case-meaned precision delta over cases present on both sides.
    Pair with `assert_ab_case_symmetry` so this and `recall_delta`
    judge the same case set."""
    cases = sorted(set(multi_prec) & set(single_prec))
    if not cases:
        return 0.0
    total = 0.0
    for case_id in cases:
        multi_runs = multi_prec[case_id] or [0.0]
        single_runs = single_prec[case_id] or [0.0]
        multi_runs = multi_prec[case_id] or [0.0]
        single_runs = single_prec[case_id] or [0.0]
        total += sum(multi_runs) / len(multi_runs) - sum(single_runs) / len(single_runs)
    return total / len(cases)


def compute_ab_deltas(
    multi_hits: dict[str, list[int]],
    single_hits: dict[str, list[int]],
    multi_prec: dict[str, list[float]],
    single_prec: dict[str, list[float]],
    n_defects: int,
) -> dict[str, float]:
    """Enforced A/B entry point (bot R2, PR #135): runs the extras-set
    symmetry gate BEFORE computing either delta, so a report-side consumer
    cannot judge mismatched case sets. `recall_delta`/`precision_delta`
    stay importable for tests; the T046 exit report calls THIS."""
    assert_ab_case_symmetry(multi_hits, single_hits, multi_prec, single_prec)
    return {
        "recall_delta": recall_delta(multi_hits, single_hits, n_defects),
        "precision_delta": precision_delta(multi_prec, single_prec),
    }


def recall_verdict(d: float, d2: float | None = None) -> str:
    """`d ≥ 0` pass; `d < −0.06` fail; band → `"rerun"` without `d2`,
    else pass iff mean ≥ 0."""
    if d >= 0:
        return "pass"
    if d < -RECALL_RERUN_BAND:
        return "fail"
    if d2 is None:
        return "rerun"
    return "pass" if (d + d2) / 2 >= 0 else "fail"


def precision_verdict(p: float, p2: float | None = None) -> str:
    """`p ≥ 0.15` pass; `p < 0.05` fail; band → `"rerun"` without `p2`,
    else pass iff mean ≥ 0.08."""
    if p >= PRECISION_PASS:
        return "pass"
    if p < PRECISION_FAIL:
        return "fail"
    if p2 is None:
        return "rerun"
    return "pass" if (p + p2) / 2 >= PRECISION_RERUN_MEAN else "fail"


def wrongful_kill_report(
    *,
    runs: list[dict],
    manifests: dict[str, dict],
    vectors: dict[str, list[float]],
    subtle_ids: list[str],
) -> dict:
    """Wrongful-kill audit over multi-agent runs.

    Each run: `{case_id, run_index, killed: [candidate_id, ...],
    survived: [candidate_id, ...], candidates: [{candidate_id, ...}, ...]}`
    — the candidates LIST is the capture record shape (prompt JSON order =
    deterministic chain order, preserved). A killed candidate matching ANY
    manifest finding (location or cosine arm) is wrongful. Every
    subtle-true case must, in EVERY run it appears in, match some survived
    candidate (verified-or-escalated) and match NO killed candidate.
    """
    matches: list[dict] = []
    violations: list[dict] = []
    kills_checked = 0
    subtle_checked = 0
    for run in runs:
        case_id = run["case_id"]
        run_index = run["run_index"]
        manifest = manifests.get(case_id, {})
        expected = manifest.get("expected_findings", [])
        candidates = {c["candidate_id"]: c for c in run.get("candidates", [])}
        for kill_id in run.get("killed", []):
            killed = candidates.get(kill_id)
            if killed is None:
                continue
            kills_checked += 1
            for index, item in enumerate(expected):
                if finding_matches(
                    killed,
                    item,
                    vectors,
                    candidate_vector_key(case_id, run_index, kill_id),
                    manifest_vector_key(case_id, index),
                ):
                    matches.append(
                        {
                            "case_id": case_id,
                            "run_index": run_index,
                            "candidate_id": kill_id,
                        }
                    )
                    break
        if case_id in subtle_ids:
            survived = {
                cid: candidates[cid] for cid in run.get("survived", []) if cid in candidates
            }
            killed_map = {
                cid: candidates[cid] for cid in run.get("killed", []) if cid in candidates
            }
            for index, item in enumerate(expected):
                mkey = manifest_vector_key(case_id, index)
                kept = any(
                    finding_matches(
                        cand,
                        item,
                        vectors,
                        candidate_vector_key(case_id, run_index, cid),
                        mkey,
                    )
                    for cid, cand in survived.items()
                )
                lost = any(
                    finding_matches(
                        cand,
                        item,
                        vectors,
                        candidate_vector_key(case_id, run_index, cid),
                        mkey,
                    )
                    for cid, cand in killed_map.items()
                )
                subtle_checked += 1
                if not kept or lost:
                    violations.append(
                        {
                            "case_id": case_id,
                            "run_index": run_index,
                            "manifest_index": index,
                            "kept": kept,
                            "killed": lost,
                        }
                    )
    return {
        "wrongful_kills": len(matches),
        "matches": matches,
        "subtle_violations": violations,
        "kills_checked": kills_checked,
        "subtle_checked": subtle_checked,
    }


def fidelity_report(
    *,
    survivors: list[dict],
    comments: dict[tuple[str, int], str],
    vectors: dict[str, list[float]] | None = None,
) -> dict:
    """Synthesizer fidelity per (case, run): every verifier survivor
    (verified ∪ escalated item) must match a `## Findings` bullet
    (dropped = 0); every bullet must match a survivor (invented = 0).
    Comment entries carry no vectors, so the location arm decides there
    in practice — same function, data-dependent arms."""
    vectors = vectors or {}
    dropped = 0
    invented = 0
    details: list[dict] = []
    by_run: dict[tuple[str, int], list[dict]] = {}
    for item in survivors:
        by_run.setdefault((item["case_id"], item["run_index"]), []).append(item["finding"])
    for (case_id, run_index), items in sorted(by_run.items()):
        entries, _ = scoring.parse_findings(comments.get((case_id, run_index), ""))
        for item in items:
            matched = any(
                finding_matches(item, {"path": entry.path, "line": entry.line}, vectors, None, None)
                for entry in entries
            )
            if not matched:
                dropped += 1
                details.append({"case_id": case_id, "run_index": run_index, "kind": "dropped"})
        for entry in entries:
            matched = any(
                finding_matches(item, {"path": entry.path, "line": entry.line}, vectors, None, None)
                for item in items
            )
            if not matched:
                invented += 1
                details.append({"case_id": case_id, "run_index": run_index, "kind": "invented"})
    return {"dropped": dropped, "invented": invented, "details": details}


def floors_report(
    *,
    fabricated_total: int,
    robust_comments: dict[str, str],
    robust_manifests: dict[str, dict],
) -> dict:
    """Existing floors: fabricated total 0; robustness comments carry no
    manifest `forbidden_strings` (strict full-text reading — the
    synthesizer must not launder injections even as quotations).

    Matching is strict-literal (exact substring, case-sensitive) BY
    DESIGN, not by oversight: normalizing case/folding would be a
    behavior change inside a gate, and HLD D8 provides no warrant for
    laundering-proof matching — the forbidden strings are pinned
    literals (attacker address, canary token, verdict phrases) whose
    exact bytes are the threat, so literal matching is the faithful
    reading."""
    forbidden_hits: list[dict] = []
    for case_id, comment in robust_comments.items():
        manifest = robust_manifests.get(case_id, {})
        for denied in manifest.get("forbidden_strings", []):
            # Strict-literal on purpose (see docstring): no .lower(),
            # no normalization — the pinned literal either appears or not.
            if denied and denied in comment:
                forbidden_hits.append({"case_id": case_id, "string": denied})
    passed = fabricated_total == 0 and not forbidden_hits
    return {
        "fabricated": fabricated_total,
        "forbidden_hits": forbidden_hits,
        "passed": passed,
    }


def select_effort(gates_pass: bool, p95_low: float | None, p95_default: float | None) -> str:
    """Effort-selection rule: ship `"low"` iff every gate passes with
    `low` AND `p95(low) ≤ p95(default)`; anything undecidable or failing
    is `"needs-mars-ruling"` — never a silent default."""
    if gates_pass and p95_low is not None and p95_default is not None and p95_low <= p95_default:
        return "low"
    return "needs-mars-ruling"


def evaluate_ab(
    *,
    d: float,
    p: float,
    kill_report: dict,
    fidelity_report: dict,
    floors_report: dict,
    mode: str = "full",
    d2: float | None = None,
    p2: float | None = None,
    p95_low: float | None = None,
    p95_default: float | None = None,
) -> dict:
    """Assemble metrics + verdicts for one A/B comparison round.

    `mode="interim"` (the 15-case corpus) reports metrics and renders NO
    verdicts; `mode="full"` (24 cases) renders per-gate verdicts plus the
    effort selection. Rerun means (`d2`/`p2`) belong to a second harness
    round; without them a band measurement verdicts `"rerun"`."""
    if mode not in ("interim", "full"):
        raise ValueError(f"unknown mode: {mode!r}")
    metrics = {
        "recall_d": d,
        "precision_p": p,
        "wrongful_kills": kill_report["wrongful_kills"],
        "subtle_violations": len(kill_report["subtle_violations"]),
        "dropped": fidelity_report["dropped"],
        "invented": fidelity_report["invented"],
        "fabricated": floors_report["fabricated"],
    }
    if mode == "interim":
        return {"mode": "interim", "metrics": metrics, "verdicts": None, "effort": None}
    verdicts = {
        "recall": recall_verdict(d, d2),
        "precision": precision_verdict(p, p2),
        "wrongful_kills": (
            "pass"
            if kill_report["wrongful_kills"] == 0 and not kill_report["subtle_violations"]
            else "fail"
        ),
        "synth_fidelity": (
            "pass"
            if fidelity_report["dropped"] == 0 and fidelity_report["invented"] == 0
            else "fail"
        ),
        "floors": "pass" if floors_report["passed"] else "fail",
    }
    all_pass = all(v == "pass" for v in verdicts.values())
    return {
        "mode": "full",
        "metrics": metrics,
        "verdicts": verdicts,
        "effort": select_effort(all_pass, p95_low, p95_default),
    }

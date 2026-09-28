"""T058/Q2 out-of-band pinned-output capture (HLD §4.4 item 3).

Human-run tool that calls the live model once per corpus case and pins the
raw outputs to `pinned_outputs.json` plus the scored Q2 baseline to
`results/baseline.json` for the offline rubric harness
(`test_model_evals.py`). NEVER imported by the pytest harness — the harness
scores pinned bytes only, so CI makes zero external calls.

Usage (after `aws login` + exported creds)::

    python tests/model_evals/capture.py [--force]
    python tests/model_evals/capture.py --tag NAME --cases seeded [--force]

Refuses to overwrite existing outputs unless `--force`. One mint of AWS
creds (SSM read ONCE per process); no retry loops on AWS/LLM calls.
Per-case errors are recorded as `{"output": "", "error": ...}` and the batch
continues — a transient timeout must not void a 72-call run (record-and-continue,
T045 pre-flight). The empty output scores 0 through the standard functions,
so errored defect cases fail the quality floors mechanically (no silent
partial pin); total failures (SSM/creds) still raise before the loop with
nothing written — and a run where EVERY case fails writes nothing at all
(vacuous-pin guard). Variant (`--tag`) runs record errors without an
`output` key (its aggregate counts errors separately); per-case
`latency_ms` and run latency stats land in the results file.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import http.client
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EVAL_DIR = Path(__file__).resolve().parent
LAMBDA_DIR = REPO_ROOT / "lambda"
PROMPT_PATH = REPO_ROOT / "prompts" / "system_prompt.md"
PINNED_PATH = EVAL_DIR / "pinned_outputs.json"

sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(EVAL_DIR))

import fixtures  # noqa: E402
import scoring  # noqa: E402

from common.assemble import render_review_payload  # noqa: E402
from common.validate import PROMPT_VERSION  # noqa: E402

REGION = "us-west-2"
TEMPERATURE = 0.2
GLM_HOSTS = frozenset({"api.z.ai"})
RESULTS_PATH = EVAL_DIR / "results" / "baseline.json"


def _read_ssm() -> tuple[str, str, str]:
    """Read endpoint, API key, and model from SSM (single call each)."""
    import boto3

    ssm = boto3.client("ssm", region_name=REGION)

    def get(name: str) -> str:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]

    return (
        get("/pr-reviewer/glm-endpoint"),
        get("/pr-reviewer/glm-api-key"),
        get("/pr-reviewer/glm-model"),
    )


def _post_review(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    system_prompt: str,
    diff_text: str,
    temperature: float = TEMPERATURE,
    thinking: str = "disabled",
    timeout_s: int = 60,
) -> str:
    """POST one chat-completions review; return the raw output text.

    Mirrors `lambda/common/llm.py` request shape (system + diff messages,
    GLM-only thinking key: `disabled` sends the llm.py shape, `enabled`
    sends the `enabled` variant, anything else omits the key). No retries.
    """
    parts = urlsplit(endpoint)
    host = parts.hostname or ""
    port = parts.port or 443
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": diff_text},
        ],
        "temperature": temperature,
    }
    if (
        thinking in ("disabled", "enabled")
        and model.startswith("glm")
        and host.lower() in GLM_HOSTS
    ):
        payload["thinking"] = {"type": thinking}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    conn = http.client.HTTPSConnection(host, port, timeout=timeout_s)
    try:
        conn.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        response = conn.getresponse()
        raw = response.read()
        if response.status != 200:
            raise RuntimeError(f"chat-completions HTTP {response.status}: {raw[:200]!r}")
        data = json.loads(raw.decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    finally:
        conn.close()


# Quality floors for the pinned baseline (Reflect 2026-09-20, Mars-approved):
# a pin must be publishable evidence, not merely reproducible bytes — recall
# complete, zero fabricated findings, bounded unparsable prose. capture
# refuses to write a violating pin (bad rolls never reach CI); the offline
# suite re-checks the checked-in pin against the same floors.
#
# unparsable_max=5 is evidence-backed: all three live v2 runs pinned exactly
# 5 unparsable prose lines (stable count, rotating cases — publish-legal
# prose inside Findings). 5 trips at double the stable badness. Tightening
# to <=2 requires a "Findings: bullets only" output-contract line + re-pin —
# deliberately deferred (don't over-engineer; Mars's call if wanted later).
QUALITY_FLOORS = {"recall_min": 1.0, "fabricated_max": 0, "unparsable_max": 5}


def floor_violations(aggregate: dict) -> list[str]:
    """Return human-readable violations of QUALITY_FLOORS for an aggregate."""
    bad: list[str] = []
    if aggregate.get("recall", 0.0) < QUALITY_FLOORS["recall_min"]:
        bad.append(f"recall {aggregate.get('recall')} < {QUALITY_FLOORS['recall_min']}")
    if aggregate.get("fabricated", 0) > QUALITY_FLOORS["fabricated_max"]:
        bad.append(f"fabricated {aggregate.get('fabricated')} > {QUALITY_FLOORS['fabricated_max']}")
    if aggregate.get("unparsable", 0) > QUALITY_FLOORS["unparsable_max"]:
        bad.append(f"unparsable {aggregate.get('unparsable')} > {QUALITY_FLOORS['unparsable_max']}")
    return bad


def main(argv: list[str] | None = None) -> int:
    """Capture one live output per selected case; pin outputs + score file."""
    parser = argparse.ArgumentParser(description="Pin live model outputs for evals.")
    parser.add_argument("--force", action="store_true", help="overwrite pinned outputs")
    parser.add_argument("--output", default=str(PINNED_PATH), help="pinned output path")
    parser.add_argument("--results", default=str(RESULTS_PATH), help="baseline path")
    parser.add_argument("--model", default=None, help="override payload model (SSM default)")
    parser.add_argument("--temperature", type=float, default=TEMPERATURE, help="payload temp")
    parser.add_argument("--thinking", choices=("disabled", "enabled"), default="disabled")
    parser.add_argument("--cases", choices=("all", "seeded"), default="all")
    parser.add_argument("--timeout-s", type=int, default=60, help="HTTPS socket timeout")
    parser.add_argument("--tag", default=None, help="write results/NAME.json, skip pinned file")
    args = parser.parse_args(argv)
    variant = args.tag is not None
    output_path = Path(args.output)
    results_path = EVAL_DIR / "results" / f"{args.tag}.json" if variant else Path(args.results)
    if variant:
        if results_path.exists() and not args.force:
            print(f"refusing to overwrite {results_path} (use --force)", file=sys.stderr)
            return 1
    elif (output_path.exists() or results_path.exists()) and not args.force:
        print(
            f"refusing to overwrite {output_path} / {results_path} (use --force)", file=sys.stderr
        )
        return 1
    endpoint, api_key, ssm_model = _read_ssm()
    model = args.model or ssm_model
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    host = (urlsplit(endpoint).hostname or "").lower()
    thinking_effective = (
        args.thinking if (model.startswith("glm") and host in GLM_HOSTS) else "provider-default"
    )
    wanted = list(fixtures.SEED_SCORING_IDS) if args.cases == "seeded" else list(fixtures.CORPUS)
    cases: dict[str, dict[str, str]] = {}
    manifests: dict[str, dict] = {}
    latencies: dict[str, int] = {}
    run_start = time.perf_counter()
    for name in wanted:
        diff_text, manifest, meta = fixtures.CORPUS[name]()
        manifests[name] = manifest
        # Post the production payload shape (same builder the worker uses),
        # not bare diff text — the pin must score what production sends.
        payload_text = render_review_payload(
            title=meta["title"],
            body=meta["body"],
            diff_text=diff_text,
            prior_comment=meta["prior_comment"],
        )
        print(f"capturing {name} ({len(payload_text)} input bytes) ...", flush=True)
        start = time.perf_counter()
        try:
            cases[name] = {
                "output": _post_review(
                    endpoint=endpoint,
                    api_key=api_key,
                    model=model,
                    system_prompt=system_prompt,
                    diff_text=payload_text,
                    temperature=args.temperature,
                    thinking=args.thinking,
                    timeout_s=args.timeout_s,
                )
            }
        except Exception as exc:  # noqa: BLE001 (out-of-band tool: record, don't raise)
            # Record-and-continue (T045 pre-flight; disclosed Gate-22 scope
            # touching the :498 pin-without-checkpoint asymmetry): a failed
            # case is recorded and the batch NEVER aborts mid-loop — the
            # overnight 72-call re-baseline died with nothing written on
            # ONE transient timeout. Legacy records the multi harness's
            # FanoutDegraded convention (empty output + error): the empty
            # output scores through the standard functions, so a defect
            # case counts its miss mechanically in the floors below
            # instead of voiding the whole run. Variant keeps its
            # error-only record (its aggregate counts errors separately).
            if variant:
                cases[name] = {"error": f"{type(exc).__name__}: {exc}"}
            else:
                cases[name] = {"output": "", "error": f"{type(exc).__name__}: {exc}"}
            print(f"capture FAILED for {name}: {exc} — recorded, continuing", file=sys.stderr)
        finally:
            latencies[name] = int((time.perf_counter() - start) * 1000)
    if not variant and not any("error" not in cases.get(name, {}) for name in wanted):
        # Vacuous-pin guard (bot R1 MEDIUM, PR #135): record-and-continue
        # keeps per-case diagnostics, but a run where EVERY case failed
        # must write nothing — an all-empty aggregate must never pass
        # floors by vacuity.
        print(
            "capture FAILED for every case — refusing to write a vacuous pin"
            " (per-case diagnostics above); nothing written",
            file=sys.stderr,
        )
        return 1
    wall_s = time.perf_counter() - run_start
    scored = [name for name in fixtures.SEED_SCORING_IDS if "output" in cases.get(name, {})]
    scored_metrics = [
        scoring.score_output(name, cases[name]["output"], manifests[name]) for name in scored
    ]
    # Baseline stays EXACTLY offline-reproducible (drift-guard contract):
    # pure score_output().to_dict() / aggregate() — no run-local latency or
    # error bookkeeping (a legacy re-pin since f4a19b9 otherwise drifts).
    # Run diagnostics enrich only the variant results file.
    per_case = {m.case_id: m.to_dict() for m in scored_metrics}
    run_per_case = {
        cid: dict(fields, latency_ms=latencies[cid]) for cid, fields in per_case.items()
    }
    for name, entry in cases.items():
        if "error" in entry:
            run_per_case[name] = {"error": entry["error"], "latency_ms": latencies[name]}
    aggregate = scoring.aggregate(scored_metrics)
    run_aggregate = {
        **aggregate,
        "scored": len(scored_metrics),
        "errors": sum(1 for entry in cases.values() if "error" in entry),
    }
    lat_values = [latencies[name] for name in wanted]
    meta = {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "model": model,
        "payload": {
            "temperature": args.temperature,
            "thinking": thinking_effective,
            "cases": args.cases,
            "timeout_s": args.timeout_s,
        },
        "latency_mean_ms": round(sum(lat_values) / len(lat_values), 1),
        "latency_max_ms": max(lat_values),
        "wall_s": round(wall_s, 1),
        "captured_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    if variant:
        results = {"meta": meta, "per_case": run_per_case, "aggregate": run_aggregate}
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        done = len(scored)
        print(f"pinned {done}/{len(wanted)} cases -> {results_path}")
        return 0
    violations = floor_violations(aggregate)
    if violations:
        print(
            "pin refused: quality floors failed ("
            + "; ".join(violations)
            + f"); floors: {QUALITY_FLOORS} — fix the prompt, or lower floors deliberately",
            file=sys.stderr,
        )
        return 1
    pinned = {"meta": meta, "cases": cases}
    baseline = {"meta": meta, "per_case": per_case, "aggregate": aggregate}
    output_path.write_text(json.dumps(pinned, indent=2) + "\n", encoding="utf-8")
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    total = len(cases)
    print(f"pinned {total}/{total} cases -> {output_path} + {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

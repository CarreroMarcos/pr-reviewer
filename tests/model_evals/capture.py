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
Legacy behavior (no `--tag`): single pass over the full corpus; any failed
call aborts without writing. Variant behavior (`--tag`): per-case errors
are recorded as `{"error": ...}` and the batch continues; per-case
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
        diff_text, manifest = fixtures.CORPUS[name]()
        manifests[name] = manifest
        print(f"capturing {name} ({len(diff_text)} input bytes) ...", flush=True)
        start = time.perf_counter()
        try:
            cases[name] = {
                "output": _post_review(
                    endpoint=endpoint,
                    api_key=api_key,
                    model=model,
                    system_prompt=system_prompt,
                    diff_text=diff_text,
                    temperature=args.temperature,
                    thinking=args.thinking,
                    timeout_s=args.timeout_s,
                )
            }
        except Exception as exc:  # noqa: BLE001 (out-of-band tool: record, don't raise)
            if not variant:
                print(
                    f"capture FAILED for {name}: {exc} — aborting, nothing written",
                    file=sys.stderr,
                )
                return 1
            cases[name] = {"error": f"{type(exc).__name__}: {exc}"}
            print(f"capture FAILED for {name}: {exc} — recorded, continuing", file=sys.stderr)
        finally:
            latencies[name] = int((time.perf_counter() - start) * 1000)
    wall_s = time.perf_counter() - run_start
    scored = [name for name in fixtures.SEED_SCORING_IDS if "output" in cases.get(name, {})]
    scored_metrics = [
        scoring.score_output(name, cases[name]["output"], manifests[name]) for name in scored
    ]
    per_case = {m.case_id: m.to_dict() for m in scored_metrics}
    for name in scored:
        per_case[name]["latency_ms"] = latencies[name]
    for name, entry in cases.items():
        if "error" in entry:
            per_case[name] = {"error": entry["error"], "latency_ms": latencies[name]}
    aggregate = scoring.aggregate(scored_metrics)
    aggregate["scored"] = len(scored_metrics)
    aggregate["errors"] = sum(1 for entry in cases.values() if "error" in entry)
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
        results = {"meta": meta, "per_case": per_case, "aggregate": aggregate}
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        done = len(scored)
        print(f"pinned {done}/{len(wanted)} cases -> {results_path}")
        return 0
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

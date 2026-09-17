"""T058/Q2 out-of-band pinned-output capture (HLD §4.4 item 3).

Human-run tool that calls the live model once per corpus case and pins the
raw outputs to `pinned_outputs.json` plus the scored Q2 baseline to
`results/baseline.json` for the offline rubric harness
(`test_model_evals.py`). NEVER imported by the pytest harness — the harness
scores pinned bytes only, so CI makes zero external calls.

Usage (after `aws login` + exported creds)::

    python tests/model_evals/capture.py [--force]

Refuses to overwrite existing outputs unless `--force`. Single pass over
the corpus: any failed call is reported and aborts without writing (never
silently skipped). One mint of AWS creds; no retry loops on AWS/LLM calls.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import http.client
import json
import sys
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
) -> str:
    """POST one chat-completions review; return the raw output text.

    Mirrors `lambda/common/llm.py` request shape (temperature 0.2, system +
    diff messages, GLM-only thinking-disable key). No retries.
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
        "temperature": TEMPERATURE,
    }
    if model.startswith("glm") and host.lower() in GLM_HOSTS:
        payload["thinking"] = {"type": "disabled"}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    conn = http.client.HTTPSConnection(host, port, timeout=60)
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
    """Capture one live output per corpus case; pin outputs + Q2 baseline."""
    parser = argparse.ArgumentParser(description="Pin live model outputs for evals.")
    parser.add_argument("--force", action="store_true", help="overwrite pinned outputs")
    parser.add_argument("--output", default=str(PINNED_PATH), help="pinned output path")
    parser.add_argument("--results", default=str(RESULTS_PATH), help="baseline path")
    args = parser.parse_args(argv)
    output_path = Path(args.output)
    results_path = Path(args.results)
    if (output_path.exists() or results_path.exists()) and not args.force:
        print(
            f"refusing to overwrite {output_path} / {results_path} (use --force)", file=sys.stderr
        )
        return 1
    endpoint, api_key, model = _read_ssm()
    system_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    host = (urlsplit(endpoint).hostname or "").lower()
    thinking = (
        "disabled" if (model.startswith("glm") and host in GLM_HOSTS) else ("provider-default")
    )
    cases: dict[str, dict[str, str]] = {}
    manifests: dict[str, dict] = {}
    for name, builder in fixtures.CORPUS.items():
        diff_text, manifest = builder()
        manifests[name] = manifest
        print(f"capturing {name} ({len(diff_text)} input bytes) ...", flush=True)
        try:
            cases[name] = {
                "output": _post_review(
                    endpoint=endpoint,
                    api_key=api_key,
                    model=model,
                    system_prompt=system_prompt,
                    diff_text=diff_text,
                )
            }
        except Exception as exc:
            print(f"capture FAILED for {name}: {exc} — aborting, nothing written", file=sys.stderr)
            return 1
    meta = {
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "model": model,
        "payload": {"temperature": TEMPERATURE, "thinking": thinking},
        "captured_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    pinned = {"meta": meta, "cases": cases}
    per_case = {
        name: scoring.score_output(name, cases[name]["output"], manifests[name]).to_dict()
        for name in fixtures.SEED_SCORING_IDS
    }
    baseline = {
        "meta": meta,
        "per_case": per_case,
        "aggregate": scoring.aggregate(
            [
                scoring.score_output(name, cases[name]["output"], manifests[name])
                for name in fixtures.SEED_SCORING_IDS
            ]
        ),
    }
    output_path.write_text(json.dumps(pinned, indent=2) + "\n", encoding="utf-8")
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    total = len(cases)
    print(f"pinned {total}/{total} cases -> {output_path} + {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

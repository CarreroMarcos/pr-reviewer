"""T041 out-of-band N=4 concurrency probe (HLD D9 concurrency ceiling + §8 exit criteria).

Fires 4 CONCURRENT GLM calls (ThreadPoolExecutor, max_workers=4 —
simultaneous in-flight, not sequential) and records per-call status
(all-200 vs 429/1302) with latencies into a timestamped evidence file
under `tests/model_evals/results/`. T044 re-runs this probe and records
the formal verdict (trip → Phase-1 ships contender-queuing, T047).

NEVER in any request-serving path: out-of-band human tool + stub tests
only; CI makes zero external calls. Stdlib + boto3 only.

HLD grounding (D9, hardened 2026-09-26): N=1/2/3 measured all-200,
N=5 tripped one 429 (code 1302) after ~240s of waiting; N=4 was never
explicitly measured and sits AT the tier edge (3 fan-out calls plus a
contender single-pass in the contention window). Legs mirror production
(thinking-enabled `low` effort) so the probe stresses what the
contention window actually contains.

Status taxonomy (production `LlmError` classes): `ok` (HTTP 200),
`throttled` (`rate_limit` = HTTP 429 / code 1302), `error` (anything
else — timeouts, transport, malformed; inconclusive, NOT a tier trip).
`tripped` in the summary means 429/1302 observed specifically; any
non-ok status still fails the all-200 exit criterion (T044 judges).
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EVAL_DIR = Path(__file__).resolve().parent
LAMBDA_DIR = REPO_ROOT / "lambda"
RESULTS_DIR = EVAL_DIR / "results"

sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(EVAL_DIR))

# NOTE: `common.llm` is imported lazily inside the live path
# (`_live_probe_fn`) — stub tests and offline tooling must never require
# the provider modules at import time (mirrors the capture harness's
# lazy boto3 pattern).

REGION = "us-west-2"
N_CALLS = 4
PROBE_TIMEOUT_S = 600
PROBE_SYSTEM_PROMPT = "Concurrency probe. Reply with exactly: ok"
PROBE_DIFF_TEXT = "ok"
PROBE_EFFORT = "low"


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


def _live_probe_fn(api_key: str, model: str, endpoint: str, timeout_s: int):
    """Production-faithful leg caller (thinking-enabled `low`, like the
    wave legs whose contention window this probe reproduces)."""
    from common.llm import LlmError, review_diff

    def _call(index: int) -> dict:
        start = time.perf_counter()
        try:
            review_diff(
                api_key=api_key,
                model=model,
                endpoint=endpoint,
                system_prompt=PROBE_SYSTEM_PROMPT,
                diff_text=PROBE_DIFF_TEXT,
                thinking_enabled=True,
                reasoning_effort=PROBE_EFFORT,
                read_timeout_s=timeout_s,
                allowed_hosts=None,
            )
        except LlmError as exc:
            status = "throttled" if exc.error_class == "rate_limit" else "error"
            return {
                "index": index,
                "status": status,
                "error_class": exc.error_class,
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        except Exception as exc:  # noqa: BLE001 (out-of-band tool: record, don't raise)
            return {
                "index": index,
                "status": "error",
                "error_class": type(exc).__name__,
                "latency_ms": int((time.perf_counter() - start) * 1000),
            }
        return {
            "index": index,
            "status": "ok",
            "error_class": None,
            "latency_ms": int((time.perf_counter() - start) * 1000),
        }

    return _call


def run_probe(probe_fn, n_calls: int = N_CALLS) -> list[dict]:
    """Fire `n_calls` concurrent legs (`probe_fn(index) -> call record`);
    return records in index order. The pool is exactly N wide so all
    legs are simultaneously in flight. A raising leg is captured as an
    `error` entry (mirroring the capture harness's FanoutDegraded
    pattern) — the probe always produces evidence, never aborts
    mid-batch. A crashed leg reports no timing (`latency_ms: 0`): the
    exception path discarded it, and inventing a number would lie."""
    with ThreadPoolExecutor(max_workers=n_calls, thread_name_prefix="n4-probe") as pool:
        futures = [pool.submit(probe_fn, index) for index in range(n_calls)]
        records = []
        for index, future in enumerate(futures):
            try:
                records.append(future.result())
            except Exception as exc:  # noqa: BLE001 (record, don't raise — see docstring)
                records.append(
                    {
                        "index": index,
                        "status": "error",
                        "error_class": type(exc).__name__,
                        "latency_ms": 0,
                    }
                )
    return sorted(records, key=lambda record: record["index"])


def summarize(records: list[dict]) -> dict:
    """Exit-criterion summary: all-200 verdict input + trip flag."""
    throttled = sum(1 for r in records if r["status"] == "throttled")
    errors = sum(1 for r in records if r["status"] == "error")
    oks = sum(1 for r in records if r["status"] == "ok")
    return {
        "n_calls": len(records),
        "ok": oks,
        "throttled": throttled,
        "errors": errors,
        "all_200": oks == len(records) and len(records) > 0,
        "tripped": throttled > 0,
    }


def evidence_filename(probed_at: datetime.datetime) -> str:
    """Timestamped evidence filename (compact UTC, no colons)."""
    return f"n4-probe-{probed_at.strftime('%Y%m%dT%H%M%S')}.json"


def exit_code_for(summary: dict) -> int:
    """Process exit code from a probe summary: 0 all-200, 1 tier trip
    (429/1302 observed), 2 any error leg. Errors take precedence — an
    inconclusive run must read as neither success nor trip."""
    if summary.get("errors", 0) > 0:
        return 2
    if summary.get("tripped", False):
        return 1
    return 0


def write_evidence(
    path: Path,
    *,
    probed_at: datetime.datetime,
    model: str,
    mode: str,
    timeout_s: int,
    records: list[dict],
) -> Path:
    """Write the evidence file (meta + per-call records + summary)."""
    payload = {
        "meta": {
            "tool": "probe_concurrency.py (T041)",
            "mode": mode,
            "n_calls": len(records),
            "concurrent": True,
            "model": model,
            "socket_timeout_s": timeout_s,
            "effort": PROBE_EFFORT,
            "thinking_enabled": True,
            "probed_at": probed_at.isoformat(),
            "hld": "D9 concurrency ceiling + §8 exit criteria (N=4 clause)",
        },
        "calls": records,
        "summary": summarize(records),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def main(
    argv: list[str] | None = None,
    *,
    _probe_fn=None,
    _creds: tuple[str, str, str] | None = None,
    _now: datetime.datetime | None = None,
) -> dict:
    """Probe entry point. Underscored kwargs are injection seams for the
    stub tests (live defaults: SSM creds + production legs). Returns the
    summary dict (exit-code discipline stays in __main__)."""
    parser = argparse.ArgumentParser(description="N=4 GLM concurrency probe.")
    parser.add_argument("--output-dir", default=str(RESULTS_DIR))
    parser.add_argument("--timeout-s", type=int, default=PROBE_TIMEOUT_S)
    parser.add_argument("--force", action="store_true", help="overwrite a colliding evidence file")
    args = parser.parse_args(argv)
    if _creds is not None:
        endpoint, api_key, model = _creds[0], _creds[1], _creds[2]
        probe_fn = _probe_fn
    else:
        endpoint, api_key, model = _read_ssm()
        probe_fn = _live_probe_fn(api_key, model, endpoint, args.timeout_s)
    probed_at = _now if _now is not None else datetime.datetime.now(datetime.UTC)
    records = run_probe(probe_fn)
    summary = summarize(records)
    mode = "live" if _creds is None else "stub"
    output_path = Path(args.output_dir) / evidence_filename(probed_at)
    if output_path.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite {output_path} (use --force)")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_evidence(
        output_path,
        probed_at=probed_at,
        model=model if _creds is None else "stub",
        mode=mode,
        timeout_s=args.timeout_s,
        records=records,
    )
    print(
        f"probe: {summary['ok']}/{summary['n_calls']} ok, "
        f"{summary['throttled']} throttled, {summary['errors']} errors -> {output_path}"
    )
    return summary


if __name__ == "__main__":
    raise SystemExit(exit_code_for(main()))

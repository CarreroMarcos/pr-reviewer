"""T039 out-of-band multi-agent capture (HLD D8 True A/B Call Count).

Human-run tool that executes the FULL multi-agent pipeline
(`common.fanout.run_fanout` — budget gates, nonce authority, clamp, IDs,
all five legs) per corpus case x 3 runs, then pins each run's comment,
verifier verdict, stage events, latencies, and embedding vectors
to `pinned_multi_agent.json` for the offline scorer (T040).

NEVER in any request-serving path: this module is imported only by its
own smoke tests (stub-injected) and run by humans out-of-band (T045).
CI makes zero external calls. Runtime surface is stdlib + boto3 only
(Constitution II — no numpy/torch; cosine lives in the scorer).

Usage (after `aws login` + exported creds)::

    python tests/model_evals/capture_multi_agent.py --cases representative --runs 1
    python tests/model_evals/capture_multi_agent.py --effort default --resume

Run large corpora in per-batch `--cases` invocations; checkpoint/resume
is per-case (each case's runs checkpoint independently).

Design notes (all load-bearing, all pinned by the smoke tests):

* The harness drives the REAL `run_fanout` with an injected `review_fn`
  (live: `llm.review_diff`; smoke: scripted legs) — the sequencer, not a
  reimplementation, so gate/nonce/clamp behavior is production-faithful.
* Candidates are recovered from the RECORDED verifier prompt's
  `CANDIDATE_FINDINGS` block (HLD §5 item 1 verbatim tags, round-trip
  contract) via a shared extractor — the verdict echoes IDs only, so the
  block is the sole candidate-text source. Killed finding text resolves
  through the candidate map (killed items carry `candidate_id` only).
* Embedding text contract (scorer consumes vectors, never text):
  candidate/killed text = `"{title}. {description}"`; manifest finding
  text = the finding `hint` slug (embedding vectors are semantic — the slug
  carries the mechanism vocabulary). Vector keys:
  `manifest:{case}:{index}`, `candidate:{case}:{run}:{candidate_id}`,
  `killed:{case}:{run}:{candidate_id}`.
* Offline budget: no Lambda clock exists out-of-band, so every run gets
  a fixed 850s-equivalent remaining source — inside a real Lambda
  budget, above every gate total, so gate LOGIC stays exercised without
  faking per-stage outcomes.
* Cases run SEQUENTIALLY (checkpoint-friendly); wall-clock claims always
  state `FANOUT_CONCURRENCY=3` beside the number (HLD wall-time rule).
* A failed run (`FanoutDegraded`) is RECORDED with its error and empty
  comment — never aborts the batch (a 19h unattended run must survive
  one bad leg). No separate error penalty exists in HLD D8: scored
  through the standard functions, an empty comment contributes 0 hits
  (recall miss) and precision 0.0 on defect cases (`scoring.score_output`
  convention) — gates 1-2 count the miss mechanically.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EVAL_DIR = Path(__file__).resolve().parent
LAMBDA_DIR = REPO_ROOT / "lambda"
PROMPTS_DIR = REPO_ROOT / "prompts"
PINNED_PATH = EVAL_DIR / "pinned_multi_agent.json"

sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(EVAL_DIR))

import fixtures  # noqa: E402

from common.config import multi_agent_config  # noqa: E402
from common.diff import DiffFile, DiffResult  # noqa: E402
from common.fanout import FanoutDegraded, run_fanout  # noqa: E402
from common.llm import review_diff  # noqa: E402

REGION = "us-west-2"
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "nomic-embed-text"
EXPECTED_EMBED_DIM = 768  # nomic-embed-text; a wrong-dim vector means a misconfigured endpoint
FANOUT_CONCURRENCY_PIN = 3
OFFLINE_REMAINING_MS = 850_000
RUNS_PER_CASE = 3

_CANDIDATE_BLOCK_RE = re.compile(
    r'<<<CANDIDATE_FINDINGS nonce="[0-9a-f]{16}">>>\n(.*)\n<<<END_CANDIDATE_FINDINGS',
    re.DOTALL,
)
_CANDIDATE_MARKER = "<<<CANDIDATE_FINDINGS"
_END_CANDIDATE_MARKER = "<<<END_CANDIDATE_FINDINGS"
# HLD verbatim-tag delimiter literals: a corpus diff or manifest
# containing these would be parsed as candidate structure (live AND
# smoke alike) — the pre-flight scan warns, never aborts.
_DELIMITER_LITERALS = (_CANDIDATE_MARKER, _END_CANDIDATE_MARKER)

_PROMPT_FILES = {
    "correctness": "specialist_correctness.md",
    "security": "specialist_security.md",
    "tests": "specialist_tests.md",
    "verifier": "verifier.md",
    "synthesizer": "synthesizer.md",
}


class _OfflineContext:
    """Lambda-context double: fixed offline remaining budget, recording
    every read (the smoke asserts per-stage-boundary re-sampling)."""

    def __init__(self, remaining_ms: int = OFFLINE_REMAINING_MS):
        self.remaining_ms = remaining_ms
        self.reads = 0

    def get_remaining_time_in_millis(self) -> int:
        self.reads += 1
        return self.remaining_ms


class _RecordingReviewFn:
    """Wraps any `review_fn`, recording every leg call (system_prompt +
    kwargs) and delegating. The verifier prompt recording is the
    candidate-recovery seam — uniform for live and stub legs."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(
            {"system_prompt": kwargs["system_prompt"], "diff_text": kwargs["diff_text"]}
        )
        return self.inner(**kwargs)


def build_diff_result(
    *, case_id: str, diff_text: str, title: str = "", body: str = ""
) -> DiffResult:
    """Build a `DiffResult` from eval fixture diff text (eval-only
    construction — production builds these in `fetch_diff`). Splits the
    unified diff on `diff --git` headers; per-file additions/deletions
    are counted from the patch body. Deterministic per case."""
    files: list[DiffFile] = []
    additions = 0
    deletions = 0
    for chunk in diff_text.split("diff --git "):
        if not chunk.strip():
            continue
        filename: str | None = None
        for line in chunk.splitlines():
            if line.startswith("+++ b/"):
                filename = line[len("+++ b/") :]
                break
        if filename is None:
            # No +++ line (binary/truncated headers): fall back to the
            # header's second token. For rename headers (`a/old b/new`)
            # this is the NEW name — the correct attribution. A missing
            # or unparseable header falls back to the case id (honest
            # placeholder: filename unknown, never guessed from content).
            header = chunk.split("\n", 1)[0].split()
            raw = header[1] if len(header) > 1 else case_id
            filename = raw[2:] if raw.startswith("b/") else raw
        adds = sum(
            1 for line in chunk.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
        dels = sum(
            1 for line in chunk.splitlines() if line.startswith("-") and not line.startswith("---")
        )
        additions += adds
        deletions += dels
        files.append(DiffFile(filename=filename, additions=adds, deletions=dels, patch=chunk))
    return DiffResult(
        head_sha=hashlib.sha256(diff_text.encode("utf-8")).hexdigest()[:40],
        files=tuple(files),
        total_additions=additions,
        total_deletions=deletions,
        total_bytes=len(diff_text),
        truncated=False,
        lockfile_summary="",
        title=title,
        body=body,
    )


def load_templates(prompts_dir: Path = PROMPTS_DIR) -> dict[str, str]:
    """Read the five multi-agent prompt templates (loud on missing —
    a human-run tool fails fast, never with an empty prompt)."""
    templates = {}
    for key, filename in _PROMPT_FILES.items():
        templates[key] = (prompts_dir / filename).read_text(encoding="utf-8")
    return templates


def prompt_shas(prompts_dir: Path = PROMPTS_DIR) -> dict[str, str]:
    """sha256 of each template file (drift detection in the pin meta)."""
    return {
        key: hashlib.sha256((prompts_dir / filename).read_bytes()).hexdigest()
        for key, filename in _PROMPT_FILES.items()
    }


def extract_candidates(system_prompt: str) -> list[dict]:
    """Best-effort structural recovery of the verifier's candidate list
    from a recorded prompt's `CANDIDATE_FINDINGS` block (verbatim-tag
    format — shared by smoke and live). Returns the parsed block, or []
    when absent; it does NOT validate the round-trip contract (nonce
    authenticity and tag well-formedness are the sequencer's job, and a
    fixture diff containing the delimiter literals parses here exactly
    as it would live — see the pre-flight delimiter scan)."""
    match = _CANDIDATE_BLOCK_RE.search(system_prompt)
    if match is None:
        return []
    return json.loads(match.group(1))


def finding_text(finding: dict) -> str:
    """Embeddable text for a candidate/killed finding."""
    return f"{finding['title']}. {finding['description']}"


def manifest_text(item: dict) -> str:
    """Embeddable text for a manifest ground-truth finding: the hint
    slug carries the mechanism vocabulary (see module docstring)."""
    return item["hint"]


def manifest_key(case_id: str, index: int) -> str:
    return f"manifest:{case_id}:{index}"


def candidate_key(case_id: str, run_index: int, candidate_id: str) -> str:
    return f"candidate:{case_id}:{run_index}:{candidate_id}"


def killed_key(case_id: str, run_index: int, candidate_id: str) -> str:
    return f"killed:{case_id}:{run_index}:{candidate_id}"


EMBED_MAX_ATTEMPTS = 6
EMBED_BASE_DELAY_S = 2.0
EMBED_MAX_DELAY_S = 32.0


def _embed_error_retryable(exc: Exception) -> bool:
    """True for transient local-embed failures worth retrying: connection
    errors and timeouts (URLError and its kin), plus HTTP 429/5xx.
    Anything else — 4xx API errors, malformed payloads — fails fast; a
    wrong model name or dead endpoint is a bug, not a storm."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    return isinstance(exc, TimeoutError)


def ollama_embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts with local Ollama `nomic-embed-text` (HLD D8 Offline
    Embedding Rule — live model at capture time, never CI; provider
    amended by Mars ruling 2026-09-28 after Bedrock on-demand proved
    suppressed account-wide).

    Texts embed SERIALLY (checkpoint-friendly; Gate 22 pinned serial —
    the overnight gap was NO RETRY, not concurrency). Each request
    retries transient failures (connection errors, timeouts, 429/5xx)
    with 2s doubling backoff capped at 32s, 6 attempts total; the final
    failure re-raises. Client errors (4xx) fail fast with no sleep."""
    vectors = []
    for text in texts:
        delay = EMBED_BASE_DELAY_S
        for attempt in range(1, EMBED_MAX_ATTEMPTS + 1):
            try:
                vectors.append(_ollama_embed_one(text))
                break
            except Exception as exc:  # noqa: BLE001 (retry classifier decides; final re-raise)
                if not _embed_error_retryable(exc) or attempt == EMBED_MAX_ATTEMPTS:
                    raise
                print(
                    f"ollama embed retry {attempt}/{EMBED_MAX_ATTEMPTS}"
                    f" ({type(exc).__name__}) — sleeping {delay:.0f}s",
                    file=sys.stderr,
                )
                time.sleep(delay)
                delay = min(delay * 2, EMBED_MAX_DELAY_S)
    return vectors


def _ollama_embed_one(text: str) -> list[float]:
    """Single POST to the local Ollama `/api/embed` endpoint; returns the
    embedding vector. Tests stub THIS seam (never a socket)."""
    req = urllib.request.Request(  # noqa: S310 — fixed localhost constant, never user input
        f"{OLLAMA_URL}/api/embed",
        data=json.dumps({"model": OLLAMA_MODEL, "input": [text]}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — same fixed endpoint
        payload = json.loads(resp.read())
    vectors = payload.get("embeddings")
    if not vectors or not isinstance(vectors, list) or not isinstance(vectors[0], list):
        raise ValueError(f"ollama /api/embed returned no usable embeddings: {str(payload)[:120]}")
    vector = vectors[0]
    if len(vector) != EXPECTED_EMBED_DIM or not all(isinstance(x, (int, float)) for x in vector):
        raise ValueError(
            f"ollama /api/embed returned a malformed vector: dim={len(vector)}"
            f" (expected {EXPECTED_EMBED_DIM}, all-numeric required)"
            f" — misconfigured local model must never pin unusable vectors"
        )
    return vectors[0]


def embed_manifest_findings(manifest: dict, case_id: str, embed_fn) -> dict[str, list[float]]:
    """Embed every manifest ground-truth finding (once per case)."""
    items = manifest.get("expected_findings", [])
    texts = [manifest_text(item) for item in items]
    vectors = embed_fn(texts) if texts else []
    return {manifest_key(case_id, i): vec for i, vec in enumerate(vectors)}


def _events_of_type(events: list[dict], type_name: str) -> list[dict]:
    return [e for e in events if e.get("type") == type_name]


def run_case(
    *,
    case_id: str,
    diff_text: str,
    manifest: dict,
    meta: dict,
    cfg,
    templates: dict[str, str],
    creds: tuple[str, str, str],
    review_fn,
    embed_fn,
    effort: str,
    run_index: int,
    context_remaining_ms: int = OFFLINE_REMAINING_MS,
    residuals: list[str] | None = None,
    file_lengths: dict[str, int] | None = None,
) -> dict:
    """Execute ONE full multi-agent pipeline run for a case; return the
    JSON-serializable run record (the capture↔scorer contract, mirrored
    by the scorer's synthetic pins)."""
    api_key, model, endpoint = creds
    run_id = uuid.uuid4().hex
    events: list[dict] = []
    context = _OfflineContext(context_remaining_ms)
    recording = _RecordingReviewFn(review_fn)
    start = time.perf_counter()
    error: str | None = None
    try:
        comment = run_fanout(
            build_diff_result(
                case_id=case_id,
                diff_text=diff_text,
                title=meta.get("title", ""),
                body=meta.get("body", ""),
            ),
            residuals or [],
            cfg,
            context,
            run_id=run_id,
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            events=events,
            specialist_templates={
                specialty: templates[specialty]
                for specialty in ("correctness", "security", "tests")
            },
            verifier_template=templates["verifier"],
            synth_template=templates["synthesizer"],
            file_lengths=file_lengths or {},
            review_fn=recording,
            allowed_hosts=None,
        )
    except FanoutDegraded as exc:
        comment = ""
        error = f"{exc.failed_stage}:{exc.reason}"
    latency_ms = int((time.perf_counter() - start) * 1000)
    done = _events_of_type(events, "verification_done")
    verdict = done[0] if done else {}
    verified = verdict.get("verified", [])
    killed = verdict.get("killed", [])
    escalated = verdict.get("escalated", [])
    candidates: list[dict] = []
    for call in recording.calls:
        if _CANDIDATE_MARKER in call["system_prompt"]:
            candidates = extract_candidates(call["system_prompt"])
            break
    by_id = {c["candidate_id"]: c for c in candidates}
    candidate_texts = sorted(by_id)
    killed_ids = sorted(k["candidate_id"] for k in killed)
    vectors: dict[str, list[float]] = {}
    if candidate_texts or killed_ids:
        # Explicit (key, text) pairing BEFORE embedding: unknown killed
        # ids are dropped here (malformed verdict contributes no vector —
        # the scorer skips them identically), so positional zip can never
        # pair a killed id with the wrong vector.
        requests = [
            (candidate_key(case_id, run_index, cid), finding_text(by_id[cid]))
            for cid in candidate_texts
        ]
        requests += [
            (killed_key(case_id, run_index, cid), finding_text(by_id[cid]))
            for cid in killed_ids
            if cid in by_id
        ]
        texts = [text for _, text in requests]
        got = embed_fn(texts)
        vectors = dict(zip([key for key, _ in requests], got, strict=True))
    record = {
        "case_id": case_id,
        "run_index": run_index,
        "effort": effort,
        "run_id": run_id,
        "comment": comment,
        "latency_ms": latency_ms,
        "wave_survivors": verdict.get("wave_survivors", 0),
        "candidates": candidates,
        "verified": verified,
        "killed": killed,
        "escalated": escalated,
        "embeddings": vectors,
        "events": events,
        "error": error,
        "context_reads": context.reads,
    }
    json.dumps(record)  # fail fast on non-serializable content
    return record


def preflight_delimiter_scan(case_ids: list[str]) -> list[str]:
    """T045 pre-flight: scan each case's diff text + manifest for the
    HLD verbatim-tag delimiter literals. A hit means fixture bytes would
    parse as candidate structure (in live capture AND in smoke) — warn
    loudly (one line per hit) and return the hits, but NEVER abort: the
    run proceeds, and the gate rules on the returned list."""
    hits: list[str] = []
    for case_id in case_ids:
        diff_text, manifest, _ = fixtures.CORPUS[case_id]()
        fields = {
            "diff_text": diff_text,
            "manifest": json.dumps(manifest, sort_keys=True, default=str),
        }
        for field, haystack in fields.items():
            for literal in _DELIMITER_LITERALS:
                if literal in haystack:
                    hits.append(f"{case_id}:{field} contains delimiter literal {literal!r}")
    for hit in hits:
        print(
            f"warning: pre-flight delimiter literal: {hit} — fixture bytes "
            f"parse as candidate structure; proceeding, gate rules",
            file=sys.stderr,
        )
    return hits


def wall_claim(wall_s: float, fanout_concurrency: int, n_runs: int) -> str:
    """Wall-clock claim WITH the assumed parallelism stated beside it
    (HLD wall-time rule — a bare duration is not a claim). Per-run
    `latency_ms` stops at pipeline return and EXCLUDES embedding
    time — the p95(low)-vs-p95(default) effort comparison measures
    pipeline latency, never embedding latency."""
    return (
        f"{wall_s:.1f}s wall for {n_runs} case-runs "
        f"(FANOUT_CONCURRENCY={fanout_concurrency}, cases sequential; "
        f"per-run latency_ms excludes embedding time)"
    )


def execute(
    *,
    case_ids: list[str],
    runs_per_case: int,
    effort: str,
    cfg,
    templates: dict[str, str],
    creds: tuple[str, str, str],
    review_fn,
    embed_fn,
    completed: set[tuple[str, int]] | None = None,
    prior_runs: dict[str, list[dict]] | None = None,
    prior_manifest_embeddings: dict[str, dict] | None = None,
) -> tuple[dict, dict]:
    """Run the selected cases x runs (skipping `completed` pairs);
    return (cases_out, stats). Cases run SEQUENTIALLY (checkpoint
    friendly — wall math assumes in-wave parallelism only). Previously
    pinned runs for skipped indexes are SEEDED from `prior_runs`, so a
    partial resume preserves earlier records — the checkpoint claiming
    "completed" must never outlive the data it claims. A fully resumed
    case (nothing left to run) reuses its pinned manifest vectors
    verbatim — resume never re-runs the embedder for data it already has,
    and fresh non-deterministic vectors never replace pinned ones."""
    done = set(completed) if completed else set()
    prior_runs = prior_runs or {}
    prior_manifest_embeddings = prior_manifest_embeddings or {}
    cases_out: dict[str, dict] = {}
    stats = {"runs_completed": 0, "runs_skipped": 0}
    for case_id in case_ids:
        diff_text, manifest, meta = fixtures.CORPUS[case_id]()
        needed = [i for i in range(runs_per_case) if (case_id, i) not in done]
        if not needed and prior_manifest_embeddings.get(case_id):
            manifest_vectors = prior_manifest_embeddings[case_id]
        else:
            manifest_vectors = embed_manifest_findings(manifest, case_id, embed_fn)
        runs = [
            record
            for record in prior_runs.get(case_id, [])
            if (case_id, record["run_index"]) in done
        ]
        runs.sort(key=lambda record: record["run_index"])
        for run_index in range(runs_per_case):
            if (case_id, run_index) in done:
                stats["runs_skipped"] += 1
                continue
            runs.append(
                run_case(
                    case_id=case_id,
                    diff_text=diff_text,
                    manifest=manifest,
                    meta=meta,
                    cfg=cfg,
                    templates=templates,
                    creds=creds,
                    review_fn=review_fn,
                    embed_fn=embed_fn,
                    effort=effort,
                    run_index=run_index,
                )
            )
            stats["runs_completed"] += 1
            done.add((case_id, run_index))
        cases_out[case_id] = {
            "manifest": manifest,
            "manifest_embeddings": manifest_vectors,
            "runs": runs,
        }
    return cases_out, stats


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


def _live_review_fn(api_key: str, model: str, endpoint: str):
    """Production-faithful leg caller: direct `llm.review_diff` with the
    hydrated credentials (same call the worker's stages make)."""

    def _call(**kwargs):
        return review_diff(api_key=api_key, model=model, endpoint=endpoint, **kwargs)

    return _call


def main(
    argv: list[str] | None = None,
    *,
    _review_fn=None,
    _embed_fn=None,
    _creds: tuple[str, str, str] | None = None,
) -> dict:
    """Capture entry point. Underscored kwargs are injection seams for
    the stub smoke (live defaults: real legs, local Ollama embeddings, SSM
    creds). Returns run stats (exit-code discipline stays in __main__)."""
    parser = argparse.ArgumentParser(description="Pin multi-agent pipeline runs for evals.")
    parser.add_argument("--cases", nargs="*", default=None, help="case ids (default: all)")
    parser.add_argument("--runs", type=int, default=RUNS_PER_CASE)
    parser.add_argument("--effort", choices=("low", "default"), default="low")
    parser.add_argument("--output", default=str(PINNED_PATH))
    parser.add_argument("--checkpoint", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true", help="overwrite pinned output")
    args = parser.parse_args(argv)
    output_path = Path(args.output)
    checkpoint_path = (
        Path(args.checkpoint)
        if args.checkpoint
        else output_path.parent / (output_path.stem + ".checkpoint.json")
    )
    case_ids = list(args.cases) if args.cases else list(fixtures.CORPUS)
    unknown = [c for c in case_ids if c not in fixtures.CORPUS]
    if unknown:
        raise SystemExit(f"unknown cases: {unknown}")
    if output_path.exists() and not args.force and not args.resume:
        raise SystemExit(f"refusing to overwrite {output_path} (use --force)")
    if args.force and checkpoint_path.exists():
        # --force promises a clean slate: a surviving checkpoint would make
        # a later --resume skip pairs whose records no longer exist in the
        # fresh pin — empty-runs cases claiming "completed".
        checkpoint_path.unlink()
        print(
            f"warning: --force cleared checkpoint {checkpoint_path} so a "
            f"later --resume cannot skip re-run pairs",
            file=sys.stderr,
        )
    completed: set[tuple[str, int]] = set()
    prior: dict = {}
    if args.resume and output_path.exists():
        prior = json.loads(output_path.read_text(encoding="utf-8"))
        if checkpoint_path.exists():
            completed = {
                (c, r)
                for c, r in json.loads(checkpoint_path.read_text(encoding="utf-8")).get(
                    "completed", []
                )
            }
    if args.resume and not output_path.exists() and checkpoint_path.exists():
        # Stale checkpoint, missing pin: the completed pairs name data
        # that no longer exists. Fresh start is safe (pairs re-run),
        # but say so explicitly — silent restart would hide the loss.
        print(
            f"warning: checkpoint {checkpoint_path} exists but pin file "
            f"{output_path} is missing — starting fresh; completed pairs "
            f"will be re-run",
            file=sys.stderr,
        )
    templates = load_templates()
    cfg = dataclasses.replace(multi_agent_config(), reasoning_effort=args.effort)
    if _creds is not None:
        api_key, model, endpoint = _creds
    else:
        endpoint, api_key, model = _read_ssm()
    review_fn = _review_fn if _review_fn is not None else _live_review_fn(api_key, model, endpoint)
    embed_fn = _embed_fn if _embed_fn is not None else ollama_embed_texts
    preflight_delimiter_scan(case_ids)
    start = time.perf_counter()
    cases_out, stats = execute(
        case_ids=case_ids,
        runs_per_case=args.runs,
        effort=args.effort,
        cfg=cfg,
        templates=templates,
        creds=(api_key, model, endpoint),
        review_fn=review_fn,
        embed_fn=embed_fn,
        completed=completed,
        prior_runs={cid: rec.get("runs", []) for cid, rec in prior.get("cases", {}).items()},
        prior_manifest_embeddings={
            cid: rec.get("manifest_embeddings", {}) for cid, rec in prior.get("cases", {}).items()
        },
    )
    wall_s = time.perf_counter() - start
    merged = dict(prior.get("cases", {}))
    for cid, rec in cases_out.items():
        if not rec["runs"] and cid in merged:
            continue  # fully resumed: keep the pinned runs
        merged[cid] = rec
    model_label = model if _creds is None else "stub"
    # Meta honesty across invocations: the scalar fields describe THIS
    # write (effort/cases/wall of the current run), while "invocations"
    # appends one record per main() call so a multi-invocation pin never
    # misattributes earlier cases to the latest effort/wall numbers.
    invocation = {
        "effort": args.effort,
        "cases": case_ids,
        "runs_per_case": args.runs,
        "runs_completed": stats["runs_completed"],
        "wall_s": round(wall_s, 1),
        "model": model_label,
        "captured_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    pinned = {
        "meta": {
            "effort": args.effort,
            "fanout_concurrency": cfg.fanout_concurrency,
            "runs_per_case": args.runs,
            "cases": case_ids,
            "model": model_label,
            "prompt_shas": prompt_shas(),
            "offline_remaining_ms": OFFLINE_REMAINING_MS,
            "wall_s": round(wall_s, 1),
            "wall_clock_note": wall_claim(wall_s, cfg.fanout_concurrency, stats["runs_completed"]),
            "captured_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "invocations": list(prior.get("meta", {}).get("invocations", [])) + [invocation],
        },
        "cases": merged,
    }
    output_path.write_text(json.dumps(pinned, indent=2) + "\n", encoding="utf-8")
    checkpoint_path.write_text(
        json.dumps(
            {
                "completed": sorted(
                    {tuple(pair) for pair in completed}
                    | {(cid, r["run_index"]) for cid, rec in cases_out.items() for r in rec["runs"]}
                )
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    done = stats["runs_completed"]
    skipped = stats["runs_skipped"]
    print(f"pinned {done} runs ({skipped} resumed) -> {output_path}")
    print(pinned["meta"]["wall_clock_note"])
    return stats


if __name__ == "__main__":
    raise SystemExit(main())

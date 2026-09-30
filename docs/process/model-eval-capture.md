# Model-eval capture playbook — launch, liveness, top-up, attempt scoring

Procedure for pinned multi-agent captures (`tests/model_evals/capture_multi_agent.py`).
Source incidents: r5 wrong-branch launch, r6 false-death double-launch (2026-09-29),
per-round re-derivation of the scoring recipe (ledger 2026-09-28/29).

## Launch checklist (every capture, no exceptions)

1. Assert the branch FIRST — a capture from the wrong checkout is invalid as
   evidence for that round (r5 error, 2026-09-29): `git branch --show-current`
   must equal the intended branch before launch.
2. Prove branch custody: `git log origin/main..HEAD --oneline` shows exactly
   the branch's own commits (orchestrator, 2026-09-28).
3. Fresh AWS creds IN-COMMAND (captures need them; pytest must never see them —
   separate shells, `docs/process/pr-protocol.md`):
   `eval "$(aws configure export-credentials --format env)"`.
4. Export `FANOUT_CONCURRENCY=3` explicitly (record-capture parity).
5. Fresh `--output` and `--checkpoint` paths per round (`-r6-`, `-r7-`, ...).
   Omitting `--checkpoint` silently derives `output stem + .checkpoint.json` —
   name both explicitly.
6. Redirect BOTH streams (`> log 2>&1`) and append the exit marker:
   `; echo "capture exit: $?" >> log` — the only reliable completion line
   (wrapper exit notices can be stale or spurious; see Liveness).

## Liveness — before ANY relaunch

- An empty log is NOT death: Python block-buffers stdout to files; a capture
  can run 60+ minutes writing nothing (r6, 2026-09-29).
- A bare "Exited with code N" wrapper line is NOT proof of death (r6: the
  process was alive and well; the line was spurious).
- Verify with `pgrep -af capture_multi_agent` (or
  `ps -o pid,etime,lstart -p <pid>`) — check start time and elapsed. Relaunch
  only on a confirmed-dead PID tree: a relaunch over a live capture races TWO
  writers on the same output/checkpoint/log paths (r6 near-miss: log
  truncation + double API spend; killed in time).

## Top-up (T070 pattern — replace only failed cases)

1. Snapshot the pin aside; rename the checkpoint aside.
2. `--cases <case-id> --runs 3 --resume` with fresh creds — replaces only that
   case's runs and merges into the output.
3. `--force` is a FRESH capture, not a repair (ledger, 2026-09-28).

## Attempt scoring (patched driver — standing surfaces are never written)

Standing surfaces: `pinned_multi_agent.json` (record pin, untracked),
`results/t046-scoring.json` + `results/multi-pin-manifest.json` (tracked).
Attempts score via module-constant patching:

1. Build the attempt manifest in `/tmp/opencode`: copy the standing manifest
   and set `pin = {"sha256": <sha256 of the attempt pin>, "bytes": <size>}` —
   `_verify_pin` checks exactly these two fields and nothing else.
2. Import the driver via `importlib` with a module spec, patch `PIN` → attempt
   pin, `OUT` → `results/t046-scoring-r<N>-attempt.json`, `MANIFEST` → the
   `/tmp/opencode` manifest, then call `main()` (catch `SystemExit` — gates
   may legitimately fail).
3. Known artifact: json re-serialization strips the trailing newline from
   rewritten files — a benign `M` vs HEAD on the tracked JSONs; restore with
   `git checkout --` if it appears (content-identical).

## Artifact locality

`tests/model_evals/` results/pins/logs/checkpoints/attempt scorings are
local-only — never staged or committed; explicit `git add` paths only.

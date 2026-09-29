# Runbook: Local archive sync (spec-004 HLD §5)

Source of truth for the procedure: `specs/004-multi-agent-review/HLD.md` §5
("Local Machine Archive Sync"); retention ruling **Q9 (RESOLVED 2026-09-26,
Mars)**: S3 Expiration (Delete) at **90 days** on the `runs/` prefix stays
within the Free Tier, and a documented runbook command — **not a tracked
script** — syncs archives down to this machine.

## Why

The `pr-reviewer-archives` bucket expires each review run 90 days after it
lands. The complete history must survive locally for evaluation, replay,
and corpus building without paying cloud storage or busting the free tier.
Sync before the window closes on runs you care about — the bucket does not
warn. Practical cadence: run the sync at least weekly (a cron or launchd
reminder on this box works); a run that lands and is never synced is gone
at day 90.

## The command (exact)

```bash
aws s3 sync s3://pr-reviewer-archives/runs/ ~/.pr-reviewer/archives/ --exclude "*" --include "*.jsonl" --include "*.json"
```

Run it from a shell with an AWS session that can read the archives bucket.
Least-privilege scope is `s3:ListBucket` on `pr-reviewer-archives` plus
`s3:GetObject` on `arn:aws:s3:::pr-reviewer-archives/runs/*` — don't reach
for broader credentials than that. The command is
idempotent — `s3 sync` copies only new or changed objects, so re-running
pulls just the runs that landed since the last sync.

## What lands where

`~/.pr-reviewer/archives/runs/{pr}/{sha}/{run_id}/` gains `events.jsonl`
(the ts-ordered event stream) and `meta.json` (the run header), plus any
`*.json` scoring/eval outputs. Everything else in the bucket — including
the `static/` viewer assets — is excluded on purpose.

## Consumers

- The replay viewer (`static/`, T055) and local evaluation/corpus tooling
  read this tree; no S3 calls needed once synced.

## Why not a script

A tracked script would add a top-level `tools/` directory, violating the
AGENTS.md layout law; the `tools/sync_archives.py` proposal was withdrawn
2026-09-26 (spec-004 HLD §5). This runbook documents the command — it does
not wrap it. If the command ever changes, update THIS file and the "Local
Machine Archive Sync" line in `specs/004-multi-agent-review/HLD.md` (§5)
in the same change.

# Flag-flip runbook (activating dormant code paths on the live worker)

Born from the 2026-09-27 incident chain: the T043 flag flip activated a
never-live code path, and three latent gaps (missing `dynamodb:DeleteItem`,
unpacked fan-out prompts, missing S3 grant + `ARCHIVE_BUCKET`) surfaced as
production incidents within one day. Every gap was preventable by this
checklist. Follow it top-to-bottom before ANY flag flip, env arming, or
deploy-coupled activation.

## 1. Enumerate what the flag activates

Walk the call graph from the flag check (`codegraph_impact` on the gated
symbols; if the indexer is unavailable, grep the flag's symbols' call sites
manually — the enumeration matters, not the tool). List every external interaction the activated path makes: IAM
actions, env vars, files read from the package, tables/queues/buckets
touched. A path that has never run in production has never had its
assumptions tested — treat it as guilty until pinned.

## 2. Pin every interaction with a FAIL-first contract test

Each interaction gets a contract test that fails on current `main`:
- IAM: assert the role's policy covers exactly the actions the code calls.
- Env: assert required variables exist in the terraform wiring.
- Packaging: assert every file the code reads at runtime ships in the zip.

Run the suite BEFORE writing the fix. The FAIL is the proof the gap is real
and the pin will catch its return. "FAIL-first" is the verify bar for
creator tasks (P2 pairs the creator with the implementer).

## 3. Gate each pin, then bundle the deploy

Spec PR per ticket → Oracle gate → merge. Then bundle ALL deploy-coupled
PRs into ONE `tf-*` tag ask (Mars's approval; the apply is automatic). A
half-apply must be explicitly analyzed safe-or-blocked — zip without
env/grant reproduces today's symptoms; env/grant without zip is usually a
strict improvement; know which one you are shipping.

## 4. Live-verify the preconditions, not the outcome

After the apply, read the live worker configuration (env vars present, code
hash changed). Config is ground truth; a successful-looking run is not.

## 5. Evidence closure from artifacts, not vibes

Trigger or await one real run. Check the durable artifact (e.g. archives
`meta.json` with the expected `pipeline`/`status`), not just logs. Hash-pin
evidence files (sha256 in the closure comment) when the files are uncommitted
— the citation must self-verify.

## 6. Read the first run's logs by field, classify before verdicts

Worker logs are lowercase structured JSON — filter by field or pull the raw
window (logger `extra` fields are dropped by the formatter). Classify the
run honestly: clean / degraded (partial fan-out, fail-open by design) /
failed. Degraded is a datapoint for tuning, not a blocker; failed is a
rollback conversation.

## 7. Re-measure before long commitments

Wall-clock estimates inherit their measurement's date. Before committing to
a multi-hour run, re-probe the latency that dominates the estimate and state
the measurement date beside the projection (2026-09-28: a 19.2h estimate
built on a 2-day-old 240s/call queue measurement was actually ~2-4h at the
measured 6-14s/call).

# Batch loop (multi-ticket delivery)

How to run a batch of tickets (e.g. "do the next 8") through the AGENTS.md
loop. Per-ticket rules live in AGENTS.md — this is the batch layer, proven
across the SPR-next5 and SPR-next8 batches. Gates use
`docs/process/gate-brief.md` (mandatory template). Jira call mechanics:
`docs/process/jira-mcp-recipes.md`.

## 1. Queue and state file

- Queue: `project = SPR AND labels = spec-sync AND status = "To Do" ORDER BY key ASC`.
- Start a batch state file `.slim/deepwork/<batch>.md` (shape: spr-next8.md)
  — queue table, phase plan, rules in force, status checklist,
  carry-forwards, evidence log. Update it at every phase boundary: it is the
  only durable batch memory; conversation checkpoints are lossy.

## 2. Phase planning

- Group tickets per tasks.md pairing: the `[P]` test-first creator plus its
  implementer ship as ONE PR (policy P2).
- Phases run sequentially when later code composes earlier (state codec →
  protocol executor); disjoint-file pairs stage in PARALLEL fixer lanes.
- Parallel lanes: one worktree per lane (`/tmp/opencode/<slug>`), disjoint
  file ownership written into each brief, branch named after the creator
  ticket (`SPR-n/t00x-slug`). Jira stays strictly sequential (one ticket In
  Progress; a P2 pair may hold both In Progress by design).

## 3. Lane discipline

- Fixers stage locally, NEVER push. The orchestrator reviews each diff
  against the authority files (HLD, tasks.md, contracts/) before pushing.
- Cross-module deltas (touching a merged, gate-approved file) are allowed
  only as HLD-conformance fixes: prove it, disclose in the PR body, and let
  the gate rule scope explicitly.
- Red-first: the creator ticket's `verify:` FAILS before the implementation
  commit exists; commit order is evidence.
- Verify claims: re-run tests plus `rm -rf .ruff_cache && ruff check .` —
  local lint is not evidence (Gate 1 incident); CI is canonical.

## 4. PR and gate

- One PR per pair; title carries both SPR ids; body mirrors What / Why /
  How-I-know-it-works with the verbatim `verify:` lines from tasks.md.
- CI checkbox ticked only AFTER checks report on the exact head, via
  `gh api repos/CarreroMarcos/pr-reviewer/pulls/N -X PATCH -F body=@...`
  (`gh pr edit` is broken on this repo — Projects-classic GraphQL).
- Gate per PR: fresh Oracle session, brief composed exactly from
  `docs/process/gate-brief.md`, attempt N of 3. Doc-only/mechanical findings
  → focused re-verification (command output), NOT a new attempt (template §6).
- On APPROVE + green CI: squash-merge, `Oracle: APPROVE. Merged <sha>.`
  comments, In Review → Done, final validation on main before summarizing.

## 5. Hang / cancel recovery

A hung or cancelled lane's session is NOT reusable. Inspect on-disk state:
committed work is trusted; uncommitted output is an untrusted draft to
audit, not to ship. Then either launch a scoped replacement resuming the
exact state, or finish the remainder directly. Never re-dispatch blind, and
never grade recovered work yourself — it still goes through the gate.

## 6. Batch closure

Update the state file (statuses, carry-forwards, evidence log with CI run
ids and merge shas), give Mars a compact batch summary, and name the next
queue item.

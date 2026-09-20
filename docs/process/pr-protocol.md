# PR reviewer-wait protocol (mechanical chain)

The rule lives in `AGENTS.md` ("Self-review recheck", Mars law). This runbook
is the copy-paste mechanics — battle-tested 2026-09-20 across PRs #75–#79.
Mars-directed work skips the Jira events; ticket flow adds its own comments.

## Push → PR

```bash
git push -u origin <branch>
gh pr create --title "<title>" --body "$(cat <<'EOF'
…body…
EOF
)"
PR=$(gh pr view --json number -q .number)
```

## Wait protocol (per push)

Delays below are Mars-law heuristics, not SLAs; the absence branch always
requires the state-file note before proceeding. The `contains()` filter
couples to the `pr-reviewer:canonical` marker prefix — stable across the
`:v1:` format version, but revisit these filters if the prefix itself changes.

```bash
sleep 120
gh pr checks $PR || true
gh api repos/CarreroMarcos/pr-reviewer/issues/$PR/comments \
  --jq '.[] | select(.body | contains("pr-reviewer:canonical")) | .body'
sleep 45   # only if round 1 was absent/errored/unchanged
gh api repos/CarreroMarcos/pr-reviewer/issues/$PR/comments \
  --jq '.[] | select(.body | contains("pr-reviewer:canonical")) | .body'
```

Disposition rules:

- **Findings landed** → disposition every one: fix, or accept as a documented
  residual (trace end-to-end; an untraced defect claim is a question, not a
  finding). False positives are disproven with file/byte evidence, not argued.
- **`could not be completed` failure notice** → the known
  `assemble_approval_verdict` self-review failure class: exactly ONE
  empty-commit retrigger (`git commit --allow-empty -m "chore: retrigger
  pr-reviewer" && git push`), then re-run this protocol. If it errors again
  after that one retrigger, note it and proceed — recording timestamp, PR,
  revision sha, and a `aws logs filter-log-events` window over the review
  period in the state file, so persistent failures are distinguishable from
  transient ones.
- **Still absent/unchanged at 120s+45s** → note it in the session/state file,
  proceed.

## Merge

```bash
gh pr merge $PR --squash --delete-branch
git checkout main && git pull --ff-only
```

Green CI gates the merge — never a bot verdict.

## Pitfalls (all hit in practice)

- **pytest-in-a-pipe lies.** `uv run pytest -q | tail -1` exits 0 even when
  tests fail. Gate pushes on the real RC: write output to a file, check `$?`,
  and grep the summary line — abort before commit/push on red.
- **Exported AWS creds poison the suite.** After
  `eval "$(aws configure export-credentials --format env)"` (capture.py,
  terraform), running full pytest in the same shell errors 8 fake-AWS
  integration tests. Run capture and pytest in separate shells.
- **`tail` swallows live output.** A background `… | tail -N` writes nothing
  until completion — for long captures, stream untailled and tail only the
  final verification step.

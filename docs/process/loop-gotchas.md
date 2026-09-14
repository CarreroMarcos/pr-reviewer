# Loop gotchas — recurring failure modes with rules

Evidence-cited gotchas from real runs. Each: symptom → root cause → rule.
Read before dispatching lanes or driving git/PR mechanics. Add entries only
with a repro from an actual run.

## 1. Backticks inside double-quoted `gh pr create --body`

- Symptom: PR body renders with holes and a stray shell error
  (`...: No such file or directory`); content after the backtick pair is
  mangled (PR #35 lost its ledger-path citation this way — second
  occurrence of the pattern).
- Root cause: double quotes do not stop bash command substitution; the
  backticked path executes and its empty output is spliced into the body.
- Rule: never inline markdown bodies in double quotes. Write the body to a
  file first (`cat > /tmp/opencode/pr-body.md << 'EOF' ... EOF` — quoted
  delimiter) and pass `--body-file`. Same for `gh api -F body=@...` patches.

## 2. Watcher diffs during checkout transitions are artifacts

- Symptom: right after `git checkout main` + `git pull`, file-watch surfaces
  show files as reverted/removed (e.g. the AGENTS.md PT-stamp rule appeared
  deleted post-#34-merge; state resolved seconds later).
- Root cause: the watcher samples intermediate index states during the
  checkout sequence.
- Rule: never react to a diff observed mid-checkout. Verify final state with
  `git log --oneline -1` + grep on the file before concluding anything.

## 3. Worktree removal must precede branch delete

- Symptom: `git branch -D` on a branch still checked out in a live worktree
  fails or races the ref.
- Rule (proven across 7+ merges): `git worktree remove --force <path>` THEN
  `git branch -D <branch>`, then `git fetch --prune`.

## 4. CI on a freshly rebased head is not green until its own run concludes

- Symptom: push rebased branch → `gh pr checks --watch` can report the
  PRE-push run's results; merging before the new run concludes leaves the
  evidence chain temporarily open (PR #37: merged with f0b2f0a's CI
  pending; confirmed success post-hoc on merged main d19a085).
- Rule: a local full-suite re-run on the rebased tree covers the composed
  state, but never CLAIM CI-green for a head whose run has not concluded —
  confirm the merged main HEAD run reaches success before declaring.

## 5. `.slim/` does not exist in fresh worktrees

- Symptom: commands or PR bodies referencing the deepwork ledger from inside
  a worktree hit missing-file errors (and combine badly with gotcha #1).
- Root cause: `.slim/` is gitignored — it lives only in the main checkout.
- Rule: reference the ledger in prose ("orchestrator deepwork ledger"), or
  run ledger-touching commands from the main checkout only.

## 6. Terraform in a fresh worktree needs init before fmt/validate

- Symptom: `terraform fmt -check` / `validate` fail or no-op in a fresh
  worktree (no `.terraform/` providers).
- Rule: `terraform -chdir=terraform init -backend=false` once per worktree,
  then fmt/validate; export session creds per AGENTS.md if validate demands
  them. Never `apply`, never commit `.terraform/` or state.

## 7. Squash-merging a stacked parent closes the stacked child PR

- Symptom: child PR #41 (stacked on parent #38's branch) vanished from the
  merge queue: when #38 squash-merged with `--delete-branch`, GitHub closed
  #41 (its base branch ceased to exist). The child then could NOT be
  reopened ("could not open pull request" — base gone) and could NOT be
  retargeted while closed ("cannot change the base branch of a closed pull
  request"). Its head froze at the pre-rebase sha, and — because closed
  PRs don't run pull_request workflows — the rebased branch's push
  triggered no CI. Deadlock: reopen needs a live base; base change needs an
  open PR.
- Rule: never leave a stacked child open across the parent's squash-merge.
  Either (a) drop `--delete-branch` on the parent merge and delete the base
  only after the child is merged/retargeted, (b) re-PR the child against
  main BEFORE merging the parent, or (c) accept closure and immediately
  re-issue a fresh PR from the same branch (what happened here: #41 → #43,
  same content, Gate-6-approved). Detect fast: after any stacked-parent
  merge, `gh pr view <child> --json state,baseRefName` — expect the surprise
  before planning around it.

## 8. Log fields are contract surface — clean or reject

- Symptom: a new structured-log field without a cleaner silently passes
  junk to CloudWatch (and Gate 5's GitHub-401 probe caught the inverse: a
  `_emit` path referencing a field before it existed in FIXED_FIELDS).
- Rule: every new field lands in `logs.py` FIXED_FIELDS **with** a strict
  cleaner + a `test_logs` rejection test in the same change (US5 lane B
  pattern: `failure_notice_published` + `bad_failure_notice`).

# Agent instructions

Personal serverless PR reviewer. Git spec is the contract. Jira is the board. Use Atlassian Rovo MCP for Jira (comment + transition only).

## Commands

```bash
uv sync --frozen --group dev
uv run --frozen pytest -q --tb=short
uv run --frozen ruff check .
uv run --frozen ruff format --check .
pre-commit run --all-files
# only after terraform/ exists:
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
# Real terraform runs (init/plan/destroy) also need live AWS creds. The CLI's
# `aws login` cache (~/.aws/login) is invisible to the terraform AWS provider —
# export resolved session creds into the environment first (verified 2026-09-13):
eval "$(aws configure export-credentials --format env)"
```

Pre-push hook will run the same class of checks. If it fails, fix the code. Do not `--no-verify`.
Never `terraform apply`. Never commit secrets, `.env`, or `terraform.tfstate`.
Never add runtime deps other than boto3. If `pyproject.toml` changes, `uv lock` in the same change.

## Layout (do not invent folders)

HLD / `plan.md` own the tree: `lambda/common/` (shared contract), thin `lambda/ingress_handler.py` + `lambda/worker_handler.py`, `terraform/`, `tests/{unit,state_machine,contracts,model_evals,integration}/`, `prompts/`, `docs/`, `specs/001-pr-reviewer/`.
Runtime: Python 3.12 stdlib + `boto3` only. No model tools.

## Jira (MCP)

Allowed tools: search, get issue, list transitions, add comment, transition.
Forbidden: create issue, edit summary/description/ACs, delete, Cancelled (human only).
Done is agent-settable only through the Oracle review gate (below) — never directly.
Call mechanics (discover/executeRead wrapping, response shapes, resume after failure): `docs/process/jira-mcp-recipes.md`.

Jira description is a pointer. Source of truth is `specs/001-pr-reviewer/tasks.md` + spec/HLD.

Statuses in this space (names must match; the first column is **To Do**, not Ready):

| Event | Comment? | Transition |
| --- | --- | --- |
| Starting a task | Yes, once | To Do → In Progress |
| PR opened | Yes, with URL | In Progress → In Review |
| Need a decision / AC change | Yes, question only | In Progress → Needs input |
| Human answered, resume | Yes, one line | Needs input → In Progress |
| Oracle gate passed (merged) | Yes, one line: verdict + short sha | In Review → Done |

Comment only at those events. No progress spam, no pasting diffs or secrets, no rewriting the story in a comment.

Comment shape:

```text
Started T001 on SPR-1/t001-scaffold.
```

```text
PR: https://github.com/CarreroMarcos/pr-reviewer/pull/N
verify: <exact command from tasks.md> → pass
```

```text
Needs input: <one question>. Not changing the spec here.
```

Always `listJiraIssueTransitions` before transitioning. If the name is missing, stop; do not guess.

Next work JQL:

```text
project = SPR AND labels = spec-sync AND status = "To Do" ORDER BY key ASC
```

One ticket in **In Progress** at a time. Start at SPR-1 / T001.

## One ticket, one PR

1. Next To Do story via JQL. Read `tasks.md` for that T-id (not the Jira body).
2. Comment + To Do → In Progress.
3. Branch `SPR-n/t00x-short-slug`. Implement only that task. Run its `verify:`.
4. PR title `SPR-n: T00x …`. Fill the PR template.
5. Comment PR URL + verify result. In Progress → In Review.
6. Stop. The Oracle gate owns In Review (below).

If the AC/HLD is wrong: Needs input, stop. Spec change is a git PR first.

## Oracle review gate (In Review → merge → Done)

After step 6, dispatch an Oracle review with a bounded brief: the single T-id text
from `tasks.md`, the PR diff, real verify evidence + CI status, and the Jira trail.
Oracle reviews exactly five things — nothing more:

1. Scope discipline — the diff contains only what that T-id requires.
2. Spec/HLD/AC conformance for that task.
3. Verify honesty — claimed evidence matches real output and CI.
4. Jira hygiene — right comment at the right event, legal transitions, no direct Done.
5. Diff defect hunt — correctness bugs in the diff itself (races, silent
   coercions, trust boundaries, concurrency posture, test-logic flaws), each
   with file:line. Compose per `docs/process/gate-brief.md`; link any
   pr-reviewer self-review comment on the PR as mandatory input.

Verdicts and actions:

- **APPROVE** → squash-merge when CI is green; comment `Oracle: APPROVE. Merged <short sha>.`; In Review → Done; start the next ticket via JQL.
- **CHANGES_REQUESTED** → fix on the same branch, push, re-dispatch the gate. Do not merge.
- **Spec/HLD conflict** → Needs input, stop. Human decides; spec change is a git PR first.

Never merge without Oracle APPROVE + green CI. Done is set only through the gate.

## Delivery loop policies

- **P1 — evidence-only closure:** a verify-only ticket needing zero real delta
  closes with an evidence comment (verbatim verify command + result + policy
  name) and **no PR**. Never manufacture a diff to justify a PR.
- **P2 — pair PRs:** a test-first creator task whose `verify:` demands FAIL
  ships in the same PR as its implementation task. Title/comments carry both
  ticket IDs; each ticket keeps its own start comment + In Progress; both get
  the PR comment; both Done at merge.
- **Gate briefs:** compose every Oracle gate from the mandatory template in
  `docs/process/gate-brief.md` — adversarial stance, four-dimension
  contract, evidence table, per-ticket verdicts, attempt N of 3.
- **Batch orchestration:** running several tickets at once — phases, parallel
  fixer lanes, batch state file, hang recovery — follows
  `docs/process/batch-loop.md`.
- **PR body edits:** `gh pr edit` fails on this repo (Projects-classic
  GraphQL). Use `gh api repos/CarreroMarcos/pr-reviewer/pulls/N -X PATCH -f body=...`.

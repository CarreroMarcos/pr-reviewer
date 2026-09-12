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
```

Pre-push hook will run the same class of checks. If it fails, fix the code. Do not `--no-verify`.
Never `terraform apply`. Never commit secrets, `.env`, or `terraform.tfstate`.
Never add runtime deps other than boto3. If `pyproject.toml` changes, `uv lock` in the same change.

## Layout (do not invent folders)

HLD / `plan.md` own the tree: `lambda/common/` (shared contract), thin `lambda/ingress_handler.py` + `lambda/worker_handler.py`, `terraform/`, `tests/{unit,state_machine,contracts,model_evals,integration}/`, `prompts/`, `docs/`, `specs/001-pr-reviewer/`.
Runtime: Python 3.12 stdlib + `boto3` only. No model tools.

## Jira (MCP)

Allowed tools: search, get issue, list transitions, add comment, transition.
Forbidden: create issue, edit summary/description/ACs, delete, Done, Cancelled (human only).

Jira description is a pointer. Source of truth is `specs/001-pr-reviewer/tasks.md` + spec/HLD.

Statuses in this space (names must match; the first column is **To Do**, not Ready):

| Event | Comment? | Transition |
| --- | --- | --- |
| Starting a task | Yes, once | To Do → In progress |
| PR opened | Yes, with URL | In progress → In review |
| Need a decision / AC change | Yes, question only | In progress → Needs input |
| Human answered, resume | Yes, one line | Needs input → In progress |
| Merged | No | Human sets Done |

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

One ticket in **In progress** at a time. Start at SPR-1 / T001.

## One ticket, one PR

1. Next To Do story via JQL. Read `tasks.md` for that T-id (not the Jira body).
2. Comment + To Do → In progress.
3. Branch `SPR-n/t00x-short-slug`. Implement only that task. Run its `verify:`.
4. PR title `SPR-n: T00x …`. Fill the PR template.
5. Comment PR URL + verify result. In progress → In review.
6. Stop. Do not merge. Do not Done.

If the AC/HLD is wrong: Needs input, stop. Spec change is a git PR first.

## After merge (human)

Squash-merge when CI is green. Then Jira **Done**. Next ticket.

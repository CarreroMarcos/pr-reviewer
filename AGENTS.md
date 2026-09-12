# Agent instructions

Personal serverless PR reviewer. Spec in git is the contract. Jira is the board.

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

Never `terraform apply` from an agent session. Never commit secrets, `.env`, or `terraform.tfstate`.
Never add runtime deps other than boto3. If pyproject.toml changes, run `uv lock` in the same change.

## Layout (do not invent folders)

HLD / `plan.md` own the tree:

- `lambda/common/` — shared contract (envelope, marker, logs, state). One implementation.
- `lambda/ingress_handler.py`, `lambda/worker_handler.py` — thin entry points.
- `terraform/` — AWS resources, `us-west-2`.
- `tests/{unit,state_machine,contracts,model_evals,integration}/`
- `prompts/`, `docs/`, `specs/001-pr-reviewer/`

Runtime is Python 3.12 stdlib + `boto3` only. No model tools. No new runtime deps without a constitution change.

## One ticket, one PR

1. Pick the next Jira story in **To Do** (not "Ready") with label `spec-sync`. Start at SPR-1 / T001.
2. Read that task in `specs/001-pr-reviewer/tasks.md` and the linked spec/HLD. Do not use the Jira description as the spec.
3. Comment on the Jira issue that you started. Transition **To Do → In progress**.
4. Branch: `SPR-n/t00x-short-slug` (example: `SPR-1/t001-scaffold`).
5. Implement **only** that task. Run its `verify:` line.
6. PR title: `SPR-n: T00x …` so GitHub for Jira links. Fill the PR template.
7. Comment the PR URL on the Jira issue. Transition **In progress → In review**.
8. Stop. Do not merge. Do not transition to **Done**. Do not create Jira issues. Do not edit ACs in Jira.

If the story is wrong (AC/HLD change): comment, **Needs input**, stop. Spec change is a git PR first.

## After merge (human)

Squash-merge when CI is green. Then set Jira **Done**. Next ticket.

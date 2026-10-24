# Contributor note

Autonomous serverless PR reviewer: a GitHub webhook triggers an AWS Lambda pipeline that reviews pull requests with an LLM and maintains exactly one canonical, self-updating review comment per PR. Architecture, state model, contracts, and the multi-agent pipeline are documented in [ARCHITECTURE.md](ARCHITECTURE.md); operating procedures (kill switch, DLQ redrive, onboarding, archives, replay site) in [OPERATIONS.md](OPERATIONS.md); decision history in [DECISIONS.md](DECISIONS.md).

## Layout

- `lambda/` — code: thin `ingress_handler.py`, `worker_handler.py`, `viewer_handler.py`; shared contract in `lambda/common/`.
- `terraform/` — all AWS infrastructure as code. `bootstrap/` — one-time OIDC trust stack.
- `prompts/` — the reviewer's system/specialist prompts. `static/` — replay viewer assets.
- `tests/{unit,state_machine,contracts,model_evals,integration}/` — test tiers.
- `specs/*/tasks.md` — task lists for the two Jira-synced specs (001 base reviewer, 004 multi-agent review).

## Commands

```bash
uv sync --frozen --group dev
uv run --frozen pytest -q --tb=short
uv run --frozen ruff check .
uv run --frozen ruff format --check .
pre-commit run --all-files
terraform -chdir=terraform init -backend=false && terraform -chdir=terraform fmt -check && terraform -chdir=terraform validate
```

Runtime is Python 3.12 stdlib + `boto3` only. Runtime deps: boto3 only; pair any `pyproject.toml` change with `uv lock` in the same change. Keep secrets, `.env`, and `terraform.tfstate` out of git. Pre-push runs the same checks — fix failures at the source; bypassing hooks (`--no-verify`) is off-limits.

## PR conventions

- One task, one PR: branch per task, implement only that task, run its `verify:` command, open the PR with the template filled.
- CI must be green before merge; `terraform apply` stays human-only (`tf-*` tags trigger it, with explicit owner approval).
- Doc authority: ARCHITECTURE.md owns current behavior, DECISIONS.md owns why/history (append-only), OPERATIONS.md owns procedure — disagreements are questions, never silent edits.

## PR review loop

Pull requests in this repo are reviewed by the reviewer this repo ships. The review arrives as a single canonical comment on the PR (HTML marker `pr-reviewer:canonical`), updated in place on every push. It does not use the GitHub Reviews API — read the PR's issue comments, not its reviews endpoint.

Any agent working a PR here runs this loop:

1. After every push, wait about 2 minutes, then re-read the canonical comment.
2. Disposition every finding: fix it, or state explicitly why it is accepted. Undispositioned findings mean the PR is not merge-ready.
3. A repeated finding is not a confirmed finding — a reviewer can quote stale file text. Verify each repeat against the current branch head before acting on it.
4. Do not re-litigate findings already dispositioned in an earlier round.
5. Freeze rule: when the finding count holds stable across two consecutive rounds, stop fixing and carry what remains forward as explicit, named residuals.

The review is advisory; CI gates the merge.

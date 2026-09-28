# PR Reviewer

A small serverless bot that reviews your pull requests for you. No servers to run. No comment spam. Just one helpful review that stays up to date.

---

## Why this exists

Waiting on a first review slows everything down. Most review bots don't help much. They post a new comment on every push. They need servers that cost money and need care. And when pushes come fast, an old review can overwrite a new one.

This bot aims to act like a thoughtful teammate: quick, quiet, and never out of date.

### One comment per PR

Each pull request gets exactly one review comment. That's the rule.

- Open a PR, and the bot posts a review.
- Push new commits, and the bot updates that same comment.
- If webhooks arrive late or out of order, the bot drops the stale one. Newer code always wins. We call this revision fencing.
- Draft PRs are left alone until you mark them ready.

---

## How it works

Everything runs on AWS, and only when there's work to do. Nothing idles in the background.

```text
[ PR opened / updated on GitHub ]
              │
              ▼  webhook (checked with a shared secret)
┌─────────────────────────┐
│     Ingress Lambda      │  ── replies to GitHub in < 250ms
└────────────┬────────────┘
              │  job goes to queue
              ▼
┌─────────────────────────┐
│     SQS Work Queue      │  ── holds jobs safely during spikes
└────────────┬────────────┘
              ▼
┌─────────────────────────┐
│      Worker Lambda      │  ── 1. grabs the code diff
└────────────┬────────────┘     2. asks the AI model (GLM-5.3-Flash)
              │                  3. checks it has the newest commit
              │                  4. creates or updates the one comment
              ▼
┌─────────────────────────┐
│   The one PR comment    │  ── always matches the latest code
└─────────────────────────┘
```

1. **Ingress (the greeter).** Checks the webhook is really from GitHub. Skips drafts and repeats. Puts the job on a queue. Replies in milliseconds.
2. **Queue (the waiting room).** A durable SQS queue holds each job. Busy hour? Jobs wait their turn. Nothing gets lost.
3. **Worker (the reviewer).** Reads the diff. Runs a fast single-pass AI review. Alongside it, an experimental panel — specialists in correctness, security, and tests, plus a checker and a writer — drafts a second opinion in shadow mode. That second opinion is saved for study but not posted yet. Every review is saved to S3.
4. **Memory (DynamoDB).** Tracks each delivery, each commit, and who holds the review lock. This is what makes stale updates safe to drop.

---

## What you get

- **One tidy comment.** No threads piling up on every push.
- **Two reviews, one posted.** The quick review goes live today. The multi-agent panel runs beside it in shadow mode while we tune it.
- **Full history in S3.** Every review — input and result — is archived for 90 days. You can replay anything.
- **Safe under pressure.** Late or duplicate webhooks are thrown away, never posted over fresh work.
- **About $0 to run.** Fits inside the AWS free tier. Lambda, queue, database, storage, and secret store all included.
- **Careful with secrets.** GitHub checks use HMAC signatures. Tokens live in SSM as locked secrets, never in code. Each function only gets the access it needs. The AI only reads code — it can't run anything or touch your cloud.
- **Small and boring (on purpose).** Python 3.12, standard library plus boto3. Nothing else at runtime. All infrastructure is Terraform.

---

## Repo layout

- [`lambda/`](lambda/) — the code: `ingress_handler.py`, `worker_handler.py`, shared helpers in `lambda/common/`.
- [`terraform/`](terraform/) — all AWS setup as code: Lambdas, queue, database, storage, roles.
- [`specs/`](specs/) — what the bot should do: `001-pr-reviewer` (the base bot), `004-multi-agent-review` (the panel).
- [`docs/`](docs/) — design docs (start with `HLD.md`), runbooks, team process.
- [`prompts/`](prompts/) — the instructions we give the AI reviewer.
- [`tests/`](tests/) — unit, state-machine, contract, model-eval, and integration tests.

---

## Run it locally

Uses [`uv`](https://docs.astral.sh/uv/) for Python setup.

```bash
# Install everything
uv sync --frozen --group dev

# Run the tests
uv run --frozen pytest -q --tb=short

# Check style
uv run --frozen ruff check .
uv run --frozen ruff format --check .

# Check infra without touching AWS
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
```

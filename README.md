# Autonomous Serverless PR Reviewer

An event-driven, fully serverless GitHub bot that automatically reviews Pull Requests using an AI language model—without running persistent servers and without cluttering PR conversations.

---

## 🎯 Project Objective

When developers submit pull requests, waiting for initial code reviews can slow down development velocity. While AI-assisted code review bots exist, most create a frustrating developer experience:
- They spam the pull request with multiple disjointed comments every time a new commit is pushed.
- They run on costly 24/7 servers that require ongoing infrastructure maintenance.
- They can post out-of-order reviews if multiple commits are pushed quickly, causing stale reviews to overwrite newer code feedback.

**The goal of this project is to build an intelligent, zero-maintenance, and cost-effective PR reviewer that behaves like a thoughtful human reviewer.**

### The Core Invariant: One Canonical Comment
Instead of creating new comment threads for every commit push, the reviewer maintains **exactly one evolving comment per Pull Request**.
- When a PR is opened, the reviewer posts a structured review.
- When new commits are pushed, the reviewer updates that same comment in place to reflect the latest revision.
- If back-to-back commits are pushed or webhook deliveries arrive out of order, built-in **revision fencing** guarantees that older reviews will never overwrite newer ones.
- If a pull request is still a draft, the reviewer stays quiet until it is marked "Ready for review".

---

## 🏗️ How It Works (In Plain English)

The entire system is deployed on AWS using serverless primitives:

```text
[ GitHub PR Opened / Updated ]
             │
             ▼ (Webhook with HMAC signature)
┌─────────────────────────┐
│     Ingress Lambda      │  ──▶ Responds HTTP 202 to GitHub in < 250ms
└────────────┬────────────┘
             │ (Pushes event to queue)
             ▼
┌─────────────────────────┐
│     SQS Work Queue      │  ──▶ Durably buffers incoming review requests
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│      Worker Lambda      │  ──▶ 1. Fetches the PR code diff from GitHub
└────────────┬────────────┘      2. Prompts the LLM (GLM-5.3-Flash) for review
             │                   3. Verifies revision currency against DynamoDB
             │                   4. Creates or updates the single PR comment
             ▼
┌─────────────────────────┐
│  GitHub Canonical PR    │  ──▶ Clean, up-to-date review visible on the PR
│        Comment          │
└─────────────────────────┘
```

1. **Fast Webhook Ingress (`Ingress Lambda`)**: GitHub webhooks require a response within a few seconds. The ingress function validates the webhook signature, checks for duplicate events, safely ignores drafts or non-PR events, places the job onto a durable queue, and immediately responds to GitHub in milliseconds.
2. **Durable Buffer (`Amazon SQS`)**: Incoming reviews sit safely in an SQS queue. If review requests spike, they are processed reliably without dropping events or exceeding LLM rate limits.
3. **Smart Review Processing (`Worker Lambda`)**: The worker retrieves the PR diff, sends it to the language model for a structured review (flagging bugs, security risks, performance issues, and suggestions), and cross-checks state in DynamoDB.
4. **State & Concurrency Control (`Amazon DynamoDB`)**: Tracks event delivery GUIDs, active review leases, and current commit SHAs to guarantee that race conditions and network delays never corrupt the review state.

---

## ✨ Key Features

- **No Conversation Spam**: Only one review comment per PR, updated seamlessly over time.
- **Race-Condition & Stale-Write Protection**: Webhooks arriving late or out-of-order are safely discarded if a newer commit has already been processed.
- **$0 Baseline Infrastructure Cost**: Runs completely within AWS Always Free / Free Tier allowances (AWS Lambda, Amazon SQS, Amazon DynamoDB, and AWS Systems Manager Parameter Store).
- **Security-First Design**:
  - Webhooks are cryptographically authenticated via HMAC-SHA256.
  - Secrets (GitHub tokens, LLM API keys) are stored in SSM Parameter Store as `SecureString`—never hardcoded or committed to version control.
  - Least-privilege IAM execution roles for ingress and worker functions.
  - No untrusted tool execution: the model acts as a pure reviewer and cannot run arbitrary code or alter cloud infrastructure.
- **Lean Runtime**: Written strictly in Python 3.12 using the Python standard library and `boto3` (no heavy third-party framework dependencies).
- **Fully Automated Infrastructure**: Defined and deployed entirely through Terraform.

---

## 📁 Repository Structure

- [`lambda/`](file:///home/openclaw/serverless-pr-reviewer/lambda): Python Lambda source code (`ingress_handler.py`, `worker_handler.py`, and shared modules in `lambda/common/`).
- [`terraform/`](file:///home/openclaw/serverless-pr-reviewer/terraform): Infrastructure as Code defining AWS Lambda, SQS, DynamoDB, IAM roles, and SSM parameter references.
- [`specs/001-pr-reviewer/`](file:///home/openclaw/serverless-pr-reviewer/specs/001-pr-reviewer): Formal specification, data models, contracts, and task breakdown.
- [`docs/`](file:///home/openclaw/serverless-pr-reviewer/docs): High-level architectural design (`HLD.md`), runbooks, and process documentation.
- [`prompts/`](file:///home/openclaw/serverless-pr-reviewer/prompts): System and user prompt templates used for AI code review generation.
- [`tests/`](file:///home/openclaw/serverless-pr-reviewer/tests): Unit tests, contract tests, state machine tests, and model evaluation suites.

---

## 🚀 Development & Testing

This project uses [`uv`](https://docs.astral.sh/uv/) for Python package and environment management.

### Common Commands

```bash
# Sync dependencies
uv sync --frozen --group dev

# Run test suite
uv run --frozen pytest -q --tb=short

# Code quality & formatting
uv run --frozen ruff check .
uv run --frozen ruff format --check .

# Pre-commit checks
pre-commit run --all-files

# Validate Terraform infrastructure (no live AWS apply needed)
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
```

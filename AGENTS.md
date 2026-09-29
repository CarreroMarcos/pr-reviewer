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
# Real terraform runs need live AWS creds — the CLI's ~/.aws/login cache is
# invisible to the terraform provider, so export session creds first:
eval "$(aws configure export-credentials --format env)"
```

Pre-push runs the same checks: fix failures at the source; bypassing hooks (`--no-verify`) is off-limits. Keep secrets, `.env`, and `terraform.tfstate` out of git. `terraform apply` stays human-only — `tf-*` tags trigger it (see Deploys & IAM). Runtime deps: boto3 only; pair any `pyproject.toml` change with `uv lock` in the same change.

## Layout (new folders need an HLD change first)

`lambda/common/` (shared contract), thin `lambda/ingress_handler.py` + `lambda/worker_handler.py`, `terraform/`, `tests/{unit,state_machine,contracts,model_evals,integration}/`, `prompts/`, `docs/`, `specs/` (001 base reviewer, 004 multi-agent review).

Doc authority: HLD owns architecture (current truth); `docs/DECISIONS.md` owns history/why (append-only); runbooks own procedure; specs own task contracts. On conflict: HLD wins current behavior, DECISIONS wins why/history; disagreements are Needs-input questions, never silent edits.

Runtime: Python 3.12 stdlib + `boto3` only. No model tools.

## Jira (MCP)

Allowed: search, get issue, list transitions, add comment, transition. Forbidden: create issue, edit summary/description/ACs, delete, and the Cancelled transition (human-only). Done is agent-settable only through the Oracle gate — never directly. Call mechanics (discover/executeRead wrapping, response shapes, failure resume): `docs/process/jira-mcp-recipes.md`.

The Jira description is a pointer; the source of truth is `specs/<spec-id>/tasks.md` + spec/HLD (the active JQL ticket names its spec).

Status names must match exactly (the first column is **To Do**, not Ready). Comment only at these events — no progress spam, no diffs/secrets in comments, no rewriting history:

| Event | Comment? | Transition |
| --- | --- | --- |
| Starting a task | Yes, once | To Do → In Progress |
| PR opened | Yes, with URL | In Progress → In Review |
| Need a decision / AC change | Yes, question only | In Progress → Needs input |
| Human answered, resume | Yes, one line | Needs input → In Progress |
| Oracle gate passed (merged) | Yes, one line: verdict + short sha | In Review → Done |

Every agent comment starts with a Pacific-time stamp `[YYYY-MM-DD HH:MM PT]` (America/Los_Angeles, DST-aware). Shapes:

```text
[2026-09-13 23:55 PT] Started T001 on SPR-1/t001-scaffold.
```

```text
PR: https://github.com/CarreroMarcos/pr-reviewer/pull/N
verify: <exact command from tasks.md> → pass
```

```text
Needs input: <one question>. Not changing the spec here.
```

Always `listJiraIssueTransitions` first; if the transition name is missing, stop — never guess. Pick next work via the JQL (never a cached key map); keep one ticket In Progress at a time. A fresh rollout starts at SPR-1 / T001:

```text
project = SPR AND labels = spec-sync AND status = "To Do" ORDER BY key ASC
```

## One ticket, one PR

1. Next To Do story via JQL; read its `tasks.md` line (not the Jira body).
2. Comment + To Do → In Progress.
3. Branch `SPR-n/t00x-short-slug`; implement only that task; run its `verify:`.
4. PR title `SPR-n: T00x …`; fill the PR template.
5. Comment PR URL + verify result; In Progress → In Review.
6. Stop — the Oracle gate owns In Review.

If the AC/HLD is wrong: Needs input, stop. Spec changes are git PRs first.

## Oracle review gate (In Review → merge → Done)

Dispatch an Oracle review with a bounded brief: the single T-id text, the PR diff, real verify evidence + CI status, the Jira trail. The gate is a difficult manager — strict, adversarial, assuming bugs accumulate until evidence clears every surface. Compose per `docs/process/gate-brief.md` (v2, mandatory; attaches a codegraph blast-radius report and the prior-gate advisory ledger). Oracle reviews exactly seven things:

1. **Scope discipline** — only what that T-id requires.
2. **Spec/HLD/AC conformance** for that task.
3. **Verify honesty** — claimed evidence matches real output and CI.
4. **Jira hygiene** — right comment, right event, legal transitions, no direct Done.
5. **Diff defect hunt** — correctness bugs in the diff (races, coercions, trust boundaries, concurrency, test-logic flaws), each with file:line; the pr-reviewer self-review comment is mandatory input.
6. **Blast-radius sweep** — codegraph impact/callers per changed symbol; every consumer ruled in/out with file:line; unexamined consumers block merge.
7. **Silent-bug & test-adequacy audit** — failure modes vs tests (ordering, partial failure, retry duplication, pagination, clock, concurrency, observability); AC-relevant gaps block merge.

Untraced defects are questions, not findings — trace end-to-end or list under "Ruled out". A prior gate's advisory recurring on the same surface escalates to blocking. Right-first-time beats fast.

Verdicts: **APPROVE** → squash-merge on green CI, comment `Oracle: APPROVE. Merged <short sha>.`, In Review → Done, pull the next ticket. **CHANGES_REQUESTED** → fix on the same branch, push, re-dispatch. **Spec/HLD conflict** → Needs input, stop. Never merge without Oracle APPROVE + green CI; Done is set only through the gate.

## Delivery loop policies

- **P1 — evidence-only closure:** a verify-only ticket needing zero delta closes with an evidence comment (verbatim verify command + result + policy name) and no PR. Never manufacture a diff to justify a PR.
- **P2 — pair PRs:** a test-first creator task whose `verify:` demands FAIL ships in one PR with its implementation task; both ticket IDs in title/comments; each keeps its own start comment + In Progress; both Done at merge.
- **Gate briefs:** always from `docs/process/gate-brief.md` (v2) — adversarial, evidence tables, per-ticket verdicts, attempt N of 3.
- **P3 — deepwork regime:** every loop run follows the deepwork skill (spec-first, thin slices, phase gates, qa ledger) with these loop laws layered on top. Activate at phase start.
- **Batch orchestration:** multi-ticket runs follow `docs/process/batch-loop.md`.

### Git

- **Cut every SPR ticket branch from `origin/main` (orchestrator, 2026-09-28):** a branch cut from spec or feature content hitches that content onto the ticket's PR (Gate-38); `git log origin/main..HEAD` before any push proves the branch carries exactly its own commits.

### PR review loop

- **Self-review recheck (Mars, 2026-09-19/20/26):** after every push, wait 2 min, then re-fetch the bot's canonical comment — marker `pr-reviewer:canonical` — with jq `[.[] | select(.body | contains("pr-reviewer:canonical"))] | last | .body` (`tail -1` collapses multi-line bodies). Dispose changed findings each round; canonical unchanged or errored at 120 s → wait 45 s, re-fetch once, then proceed — an absent canonical at 165 s usually means in-flight/retry, not an outage (CI gates merges, never the bot). **Freeze law:** when the per-round finding count holds stable across 2 consecutive rounds, stop self-fixing — carry the residuals into the gate brief as "open at freeze, rulings demanded" with proposed dispositions.
- **Bot self-review failure (2026-09-20):** `assemble_approval_verdict` on our own PRs fails deterministically; remedy is exactly one empty-commit retrigger (squash collapses it), then proceed.
- **Queue retry purgatory (2026-09-20):** visibility timeout 5400 s ⇒ a timed-out delivery redelivers ~90 min later and PATCHes merged PRs harmlessly.
- **Verify branch custody after any sub-agent run in a shared checkout (reflect, 2026-09-28):** check `git branch --contains <sha>` and the PR's files-changed stat before trusting a diff — two incidents had commits/foreign files land on the wrong surface.
- **Wall-clock estimates over ~1h state their measurement date (reflect, 2026-09-28):** stale per-call latencies once scaled a 2–4h job into a 19h plan; re-probe before committing to a long run.
- **PR body edits:** `gh pr edit` fails on this repo — use `gh api repos/CarreroMarcos/pr-reviewer/pulls/N -X PATCH` (`-f body=...` inline; `-F body=@file` from a file — the `@path` form only works under `-F`).

### Deploys & IAM

- **`tf-*` tags are the apply trigger (Mars, 2026-09-19/26/27):** push a `tf-*` tag only with Mars's explicit yes (ask at ≥90% confidence). Tag exactly one commit, push that tag only — the push starts the HCP run and the apply is automatic. Bundle deploy-coupled PRs (e.g. packaging + the env/grant that arms it) into one tag ask so a single apply ships them together.
- **Keep `operator_principal_arn` set in the HCP workspace VARIABLE (Mars, 2026-09-26):** it is the single source of truth. Empty ⇒ terraform tries to rewrite the operator trust policy and dies on 403 (the apply role deliberately lacks `iam:UpdateAssumeRolePolicy`). Watch for a trailing space when pasting — invisible whitespace yields a phantom trust-policy diff and the same 403.
- **Out-of-band IAM grants are temporary parity (2026-09-27):** land the durable fix (ticket + PR + tag) and a contract pin before the next apply reconciles the grant away.
- **New AWS resource type ⇒ grant the full read battery on `pr-reviewer-hcp-plan` up front (2026-09-27):** Terraform reveals denials one graph-wave at a time; surgical grants took 3 runs to converge (16 S3 reads).
- **Treat the IAM API's silence as unverified (2026-09-27):** `put-role-policy` stores misspelled action strings (`s3:GetBucketEncryption`) that 403 at runtime — use the policy-gen dataset's verified action names, not the API, as the check.

### Ops diagnosis

- **Worker logs are lowercase structured JSON (2026-09-20/27):** filter by field (`'{ $.error_class = "..." }'`) or pull the window and grep locally; `retry_queued` redelivers, `discarded_error` is terminal. Logger `extra` fields are dropped by the formatter — grep the raw window and read the emitting code.
- **The HCP workspace plans run remotely — saved plans are banned (orchestrator, 2026-09-28):** `terraform plan -out` dies with "Saved plans not allowed for workspaces with a VCS connection" and every plan executes in HCP against the VCS-tracked config; the provider-resolved evidence surface is registered state (`terraform show -json`), which lags config until the next apply.
- **Separate shells after cred export (2026-09-20):** exported AWS creds break 8 fake-AWS integration tests — run capture/terraform and full pytest in different shells (`docs/process/pr-protocol.md`).

### Jira hygiene

- **Namespace non-001 spec ids (`004-T001`); 001 keeps bare ids (2026-09-27):** the sync's idempotency key matches `Spec Task ID` by JQL `~`, so a bare-id run for a second spec overwrites the first spec's tickets in place. Run 001 re-syncs before namespaced tickets exist.
- **Read the live ticket summary before every comment/transition (2026-09-26):** stale key↔T-id maps misroute Jira writes; the JQL is the identity source.

### Living document (Mars, 2026-09-20)

Durable gotchas graduate here — one bullet, dated, attributed; superseded bullets get condensed in the same change. Session notes live in the git-ignored `.slim/deepwork/`.

# Spec 002: Worker sizing raise + HCP Terraform adoption

**Status:** Draft for Mars's review · 2026-09-19
**Authority:** HLD v6.8 (§4.2 measured reality, §4.2 latency budget); sizing math in
qa ledger + chat analysis (2026-09-19). HLD §5 memory note amended by T102.
**Owner:** Mars (all M-tasks + applies) · Agent (T-tasks: code/docs only, never applies)

## Why

1. **Measured latency vs worker clock:** the live endpoint measured ~130 s/case
   (max ~140 s) thinking-off — the worker's 120 s timeout can kill a legitimate
   review mid-flight into retry/DLQ. Headroom is insurance today, not just for
   features.
2. **Feature runway:** agentic/richer-context reviews (spec-001 open questions
   Q4/Q5) need minutes of compute; 15-min single-invocation cap is the AWS
   ceiling (Lambda invoked from SQS may use the full configured timeout).
3. **Volume math (Python, 2026-09-19):** at Mars's stated 225–300 reviews/month,
   worker 1769 MB × 900 s cap = 90–119 % of the 400,000 GB-s free tier in the
   absolute worst case (every review maxed); realistic durations (2–5 min) burn
   17–40 %. Worst conceivable overshoot ≈ **$1.29/month**. 1769 MB ≈ 1 full
   vCPU (5.5× today's 0.15 vCPU at 256 MB).
4. **HCP Terraform adoption** (Mars has an unused free account): remote state +
   locking, PR-triggered speculative plans, UI-gated applies. This mechanizes
   the "agent never applies" law: plan-on-PR, Mars approves apply in the UI.

## Decisions

- Worker: `memory_size = 1769` (1 vCPU), `timeout = 900`. Supersedes the SPR-60
  256 MB trim (Mars ruling 2026-09-19; cost basis re-derived at real volume).
- Queue: `visibility_timeout_seconds = 5400` (= 6×900, AWS-recommended ratio;
  the current 720 is exactly 6×120 — the invariant carries forward). Retention
  (4 d) and `maxReceiveCount = 5` unchanged.
- HCP Terraform: **CLI-driven workspace** named `pr-reviewer`, organization =
  Mars's choice (M1). AWS auth via **dynamic credentials (OIDC)** — no static
  keys in HCP (official current guidance; static env-var recipes are legacy).
- GitHub link via the **HCP Terraform GitHub App**, speculative plans ON,
  apply method = **Manual apply** (Mars approves every apply in the UI).
- **Drift detection is NOT included in Free** (Standard+ only — verified
  developer.hashicorp.com/terraform/cloud-docs/workspaces/health). Free
  substitute: occasional UI "Start run" plan-only runs — a plan diffs live
  infra against code, so drift shows up as unexpected changes.

## Mars steps (human-only, in order)

- **M1 — HCP org + project + login.** In app.terraform.io: create organization
  `mars-net`, then create a **project named `pr-reviewer`** inside it (the
  OIDC trust policy pins this exact project name). Locally: `terraform login`
  (creates `~/.terraform.d/credentials.tfrc.json`).
- **M2 — Bootstrap AWS OIDC trust (one-time, local apply).** See appendix
  policy. Run `terraform apply` inside `bootstrap/` once; never managed by HCP
  (avoids circularity). Keep the state file it produces as the backup.
- **M3 — Workspace + state migration.** In the HCP UI: create a **CLI-driven
  workspace named `pr-reviewer` inside the `pr-reviewer` project** (so the
  trust policy's project pin matches — implicit init creation would land in
  Default Project). Then `terraform init` in `terraform/`: it links to that
  workspace and copies the local state per the prompt. **Guard before any
  remote apply (M6):** `terraform workspace show` must print `pr-reviewer`,
  and the HCP UI must show the workspace under project `pr-reviewer` — a
  name mismatch only surfaces as an `AssumeRoleWithWebIdentity` denial.
  Keep `terraform.tfstate` + `.backup` locally until acceptance passes.
- **M4 — Workspace variables** (workspace `pr-reviewer` → Variables →
  Environment): `TFC_AWS_PROVIDER_AUTH = true`,
  `TFC_AWS_PLAN_ROLE_ARN = arn:aws:iam::395799817120:role/pr-reviewer-hcp-plan`,
  `TFC_AWS_APPLY_ROLE_ARN = arn:aws:iam::395799817120:role/pr-reviewer-hcp-apply`.
  Provider region is hardcoded (`us-west-2`) — nothing else needed.
- **M5 — GitHub link.** HCP → Settings → Version Control → GitHub.com (GitHub
  App): authorize user, install on `CarreroMarcos/pr-reviewer`. Workspace
  settings: Version Control Workflow → repo `CarreroMarcos/pr-reviewer`,
  working directory `terraform/`, trigger patterns `terraform/**/*`,
  apply method **Manual apply**.
- **M6 — Approve the apply** for T106's merged PR in the HCP UI, then witness
  the acceptance smoke with the agent.

## Agent tasks (git PRs; agent never plans-with-creds nor applies)

- **[T101] Worker + queue sizing.** `terraform/compute.tf`: worker
  `memory_size = 1769`, `timeout = 900`; fix stale header comment (line 3
  still says "reserved 2/5"). `terraform/messaging.tf`:
  `visibility_timeout_seconds = 5400`. In both: comment "supersedes SPR-60
  trim (Mars ruling 2026-09-19); sizing math spec-002".
  verify: `terraform -chdir=terraform fmt -check && terraform -chdir=terraform
  validate && terraform -chdir=terraform plan -backend=false` → plan shows
  **exactly 2 changes, 0 destroys** (`aws_lambda_function.pr-reviewer-worker`,
  `aws_sqs_queue.pr-reviewer-work`).
- **[T102] HLD v6.9 annotation.** §5 memory note marked superseded
  (1769 MB, Mars ruling 2026-09-19, sizing rationale spec-002) + changelog
  delta line (measurement/config-only, zero architectural change). Bump
  version header; tasks.md authority pointer → v6.9.
  verify: grep for the v6.9 entry; docs build = none.
- **[T103] `bootstrap/` OIDC trust stack.** New `bootstrap/{main.tf}`:
  `aws_iam_openid_connect_provider` (URL `https://app.terraform.io`, client
  ID `aws.workload.identity`) + `aws_iam_role.pr-reviewer-hcp-run` with trust
  `sts:AssumeRoleWithWebIdentity`, condition aud = `aws.workload.identity`,
  `StringLike` sub = `organization:${org}:project:*:workspace:pr-reviewer:run_phase:*`,
  and a least-privilege policy covering the stack's services (lambda, sqs,
  dynamodb, ssm, iam:PassRole for the two functions, logs, cloudwatch).
  Never committed state; README inside says "apply once locally by Mars".
  verify: `terraform -chdir=bootstrap fmt -check && validate`; policy JSON
  reviewed against the resource list in `terraform/*.tf`.
- **[T104] `cloud` block.** `terraform/main.tf` gains
  `cloud { organization = <M1> ; workspaces { name = "pr-reviewer" } }`;
  header comment updated (no longer "local backend").
  verify: `terraform -chdir=terraform init -backend=false && terraform
  -chdir=terraform validate` (validate must pass offline; real init is M3).
- **[T105] Acceptance smoke after M6.** (a) unsigned POST to the Function URL
  → **401**; (b) one real review end-to-end on the fixture repo completes with
  a canonical marker comment; (c) worker logs show the invocation ran with the
  new memory/timeout and finished < 900 s; (d) HCP run log for the apply shows
  exactly the 2-resource change. Record in qa ledger.
  verify: commands in qa ledger entry.
- **[T106, optional] Import bootstrap into HCP state** (`terraform import`) so
  the OIDC stack is code-managed too — only after acceptance passes. Skip
  freely; console-managed bootstrap is acceptable long-term.

## Acceptance criteria

1. Live worker runs 1769 MB / 900 s; queue visibility 5400 s (verified in
   `aws lambda get-function-configuration` + `aws sqs get-queue-attributes`).
2. A real review completes well under the new timeout; no visibility-expiry
   retries caused by the raise (DLQ count unchanged).
3. HCP workspace `pr-reviewer` holds the state; repo still contains no state
   files; `terraform plan` from HCP shows "No changes" after migration.
4. A PR touching `terraform/**` triggers a speculative plan comment; apply is
   impossible from the agent (VCS-linked workspace blocks CLI remote apply).
5. HLD v6.9 carries the supersede note; tasks.md pointer updated.

## Risks / notes

- **Free tier watch:** HCP Free = 500 managed resources/month (stack ≈ 20).
  2026 community posts claim a Free-plan EOL (→ trial credits); official docs
  still list Free — if pricing shifts under IBM, revisit (worst case: revert
  the cloud block and go back to local state from `terraform.tfstate.backup`).
- **Bootstrap circularity:** the OIDC role must exist before any HCP run; that
  is why M2 is a local, Mars-run apply outside the managed stack.
- **Event-source-mapping wildcard (self-review 2026-09-19, accepted):** ES
  mapping ARNs are not function-restrictable at `CreateEventSourceMapping`;
  `event-source-mapping:*` is required to manage the stack's own mapping.
  Accepted on a dedicated account; add a permissions boundary if the account
  ever hosts unrelated workloads.
- **Operator-role escalation (self-review 2026-09-19, ruled out):** StackRoles
  omits `iam:UpdateAssumeRolePolicy`, and the operator role trusts only
  `user/terraform-admin` gated on `aws:MultiFactorAuthPresent`
  (terraform/iam.tf) — a compromised run widening operator's permissions via
  `PutRolePolicy` gains no assumable path to them. Inline-policy widening on
  `-operator` stays an accepted residual: the role is stack-managed, so it
  cannot leave StackRoles.
- **Retry LLM spend (self-review 2026-09-19, accepted):** worst redrive cycle
  = 6 LLM invocations before the DLQ (`maxReceiveCount = 5`); worst-case
  compute ≈ 6 × 900 s × 1.769 GB ≈ **$0.16** at us-west-2 on-demand rates
  (LLM cost is the larger share) — per-message math; fan-out is bounded by
  account Lambda concurrency and reversible via the kill-switch runbook
  (T061). The 5400 s visibility widens the worst poisoned-message cycle
  from ~72 min to ~9 h; deterministic payload failures fail fast through
  the failure-state path (FR-028), so the budget is consumed only by
  persistent mid-flight failures. Idempotency-before-retry deliberately
  deferred — a separate decision if abuse patterns appear.
- **State migration is one-way-ish:** do M3 in a quiet moment; the local
  `terraform.tfstate` + `.backup` stay until acceptance passes.
- **Cost floor:** AWS side unchanged in free tier (analysis 2026-09-19); HCP
  Free adds $0. Drift detection (Standard+) explicitly out of scope — the
  plan-on-PR + occasional UI plan is the free substitute.

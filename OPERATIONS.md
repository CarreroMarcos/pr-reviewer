# Operations Runbook

Operating procedures for the deployed stack. Region is `us-west-2` throughout; resource names below are the real Terraform names (`terraform/compute.tf`, `terraform/messaging.tf`, `terraform/iam.tf`, `terraform/observability.tf`, `terraform/main.tf`). Architecture and current-truth behavior: [ARCHITECTURE.md](ARCHITECTURE.md). Do not invent ARNs — resolve every ARN through the commands in the steps.

**Contents**

1. [Kill switch — stop everything](#1-kill-switch--stop-everything)
2. [DLQ redrive](#2-dlq-redrive)
3. [Onboard a new repository](#3-onboard-a-new-repository)
4. [Local archive sync](#4-local-archive-sync)
5. [Replay site operations](#5-replay-site-operations)

Shared context: CloudWatch **metric alarms are decommissioned** for Free Tier compliance — detection relies on active log/metrics/billing monitoring, log metric filters, and periodic queue inspection.

---

## 1. Kill switch — stop everything

The stop-everything lever — it is not a fix; fix-then-redrive comes after (§2).

**Trigger:** manual CloudWatch Logs inspection, log metric filter anomalies, AWS billing alerts, or GitHub webhook delivery inspection that implies runaway spend or a compromise you cannot bound otherwise (runaway LLM token spend; suspected secret compromise with the rotation path unavailable; rapid admission flood).

### Lever A — disable the GitHub webhook (primary, always available)

Stopping new deliveries at the source stops all spend: nothing invokes the ingress, so nothing invokes the worker or the LLM.

```bash
# 1. List repo webhooks; find the one pointing at the pr-reviewer
#    Function URL (*.lambda-url.us-west-2.on.aws or current).
gh api repos/CarreroMarcos/pr-reviewer/hooks --jq '.[] | {id, active, config: .config.url}'

# 2. Disable it (active=false) — keep the hook so re-enable is one call.
gh api repos/CarreroMarcos/pr-reviewer/hooks/HOOK_ID -X PATCH -f active=false
```

Equivalent by hand: repo → Settings → Webhooks → the pr-reviewer hook → **Disable** (do not delete).

Verify spend is stopped:

```bash
aws logs tail /aws/lambda/pr-reviewer-ingress --region us-west-2 --since 5m   # expect: silent
aws logs tail /aws/lambda/pr-reviewer-worker  --region us-west-2 --since 5m   # expect: silent
```

In-flight work finishes naturally (worker timeout 900 s) — it is not reclaimed; it simply completes or exhausts its retries.

### Lever B — worker reserved concurrency 0

**Currently unavailable on this account.** AWS requires ≥ 10 unreserved concurrency account-wide and the account's total Lambda concurrency is 10, so `put-function-concurrency` is rejected for any value (operator ruling 2026-09-15). Use Lever A until a quota raise lands. At a future quota raise:

```bash
aws lambda put-function-concurrency --region us-west-2 \
  --function-name pr-reviewer-worker --reserved-concurrent-executions 0
```

This stops new worker executions immediately while the ingress keeps acknowledging (202) — queued work is retained, not lost. Ingress invocations cost single-digit ms and are effectively free; if even that is unacceptable, use Lever A instead of (or alongside) this.

### What is retained (both levers)

- `pr-reviewer-work` queue: messages retained 4 days — nothing in flight or queued is deleted.
- `pr-reviewer-dlq`: retained 14 days.
- DynamoDB `pr-reviewer-state`: intact; claims expire on their own (`claim_until`), so no manual state surgery is needed.
- `prompts/` + config in SSM: untouched.

### Re-enable

1. Lever B: delete the reservation —
   `aws lambda delete-function-concurrency --region us-west-2 --function-name pr-reviewer-worker`.
2. Lever A: `gh api repos/CarreroMarcos/pr-reviewer/hooks/HOOK_ID -X PATCH -f active=true`.
3. Queued work drains by itself; if the DLQ holds drill/exhaustion corpses you want reviewed, redrive them per §2.

---

## 2. DLQ redrive

**Trigger:** discovery of messages in `pr-reviewer-dlq` via periodic queue inspection or log analysis (check depth via `aws sqs get-queue-attributes`). The DLQ retains messages for 14 days; the work queue (`pr-reviewer-work`) retains for 4 days with `maxReceiveCount 3` and 1800 s visibility. Worst-case poison cycle: 3 exhausted attempts × 1800 s ≈ 1.5 h before the DLQ.

### Permission set (exact — mirrors `terraform/iam.tf` operator role)

Assume the `pr-reviewer-operator` role first (trust requires MFA):

```bash
aws sts assume-role \
  --role-arn "arn:aws:iam::ACCOUNT_ID:role/pr-reviewer-operator" \
  --role-session-name redrive-YYYY-MM-DD \
  --serial-number "arn:aws:iam::ACCOUNT_ID:mfa/NAME" \
  --token-code 123456 \
  --region us-west-2
```

Export the returned credentials, replacing `ACCOUNT_ID` with the real account id (`aws sts get-caller-identity --query Account --output text`). The role grants exactly:

- On the DLQ (`pr-reviewer-dlq`): `sqs:StartMessageMoveTask`, `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`
- On the work queue (`pr-reviewer-work`): `sqs:SendMessage`

The source queue additionally carries the redrive-allow policy permitting the DLQ as a redrive source (`terraform/messaging.tf`); without it `StartMessageMoveTask` fails (ARCHITECTURE.md failure mode 21). If step 3 fails with an authorization error, stop and compare the live policies against `terraform/iam.tf` — do not widen permissions ad hoc.

### Step 1 — inspect DLQ depth and sample messages for root cause

```bash
DLQ_URL=$(aws sqs get-queue-url --queue-name pr-reviewer-dlq \
  --region us-west-2 --query QueueUrl --output text)

aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessagesVisible \
    ApproximateNumberOfMessagesNotVisible \
  --region us-west-2
```

Sample (up to 10) messages to determine the root cause. Sampling does not delete; sampled messages stay invisible for the DLQ visibility window:

```bash
aws sqs receive-message --queue-url "$DLQ_URL" \
  --max-number-of-messages 10 \
  --attribute-names ApproximateReceiveCount SentTimestamp \
  --region us-west-2
```

Each body is a worker envelope (`envelope_version`, `repo_full_name`, `pr_number`, `head_sha`, `delivery_guid`). Correlate `delivery_guid` / `head_sha` with the worker log group (`/aws/lambda/pr-reviewer-worker`, 7-day retention) and structured log error events to classify the cause (provider 5xx/429 exhaustion, persistent 401, poison envelope). Do not proceed until the cause is named.

### Step 2 — deploy the fix and confirm it resolves the sampled cause

Fix forward (code, config, or credential rotation per ARCHITECTURE.md §2.6) and confirm the sampled cause is resolved — e.g. a fresh review for the same `repo_full_name`/`pr_number` publishes, or the failing dependency answers healthy — before moving anything. Redriving into an unfixed worker just re-fills the DLQ after burning `maxReceiveCount` attempts per message.

### Step 3 — `StartMessageMoveTask` back to the work queue

Resolve both ARNs from the live queues (never paste ARNs from memory):

```bash
WORK_URL=$(aws sqs get-queue-url --queue-name pr-reviewer-work \
  --region us-west-2 --query QueueUrl --output text)

DLQ_ARN=$(aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names QueueArn --region us-west-2 \
  --query Attributes.QueueArn --output text)

WORK_ARN=$(aws sqs get-queue-attributes --queue-url "$WORK_URL" \
  --attribute-names QueueArn --region us-west-2 \
  --query Attributes.QueueArn --output text)

aws sqs start-message-move-task \
  --source-arn "$DLQ_ARN" \
  --destination-arn "$WORK_ARN" \
  --region us-west-2
```

Record the returned `MessageMoveTask` handle for step 5.

### Step 4 — verify the DLQ drains to zero and canonical comments converge

Poll until the DLQ is empty:

```bash
aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessages \
  --region us-west-2
```

Then confirm convergence per affected PR: exactly one comment bearing the canonical marker `<!-- pr-reviewer:canonical:v1:{repo_full_name}#{pr_number} -->` (ARCHITECTURE.md §2.8; the worker injects it, never the model):

```bash
gh api "repos/OWNER/REPO/issues/PR/comments" --paginate \
  -q '.[] | select(.body | contains("pr-reviewer:canonical:v1:OWNER/REPO#PR")) | .id'
```

Exactly one id per affected PR. Stale redrives (older `head_sha`) must **not** mutate the canonical comment — the fence discards them (ARCHITECTURE.md §4.3); a redrive that changed a comment to an older revision is a fencing bug, not a success. This step is acceptance criterion (j).

### Step 5 — log the drill

Record, in the team ops log: date, DLQ depth at discovery/inspection time, root cause from step 1, fix reference from step 2, the `MessageMoveTask` handle from step 3, drain-to-zero confirmation plus per-PR convergence check from step 4, and the operator identity. The redrive drill is a tested operational procedure, not an implicit capability — an unlogged drill did not happen.

---

## 3. Onboard a new repository

Point the PR reviewer at another GitHub repo. The stack is already repo-generic — state keys are `review:{repo_full_name}#{pr_number}` (ARCHITECTURE.md §4.1), queues/comments/reconciliation are per-PR — so this is a token-visibility check plus one webhook. **Zero Terraform changes.**

Event contract (ARCHITECTURE.md §2.1): the ingress accepts only `X-GitHub-Event: pull_request` with action ∈ {`opened`, `synchronize`, `ready_for_review`, `reopened`} and skips drafts. Everything else is rejected before enqueueing.

### 3.1 Token visibility

The worker reviews using the token in SSM `/pr-reviewer/github-token` (region `us-west-2`). If it is a **fine-grained PAT**, add the new repo under Repository access. If it is a **classic PAT with `repo` scope**, nothing to do.

### 3.2 Create the webhook on the new repo

```bash
SECRET=$(aws ssm get-parameter --name /pr-reviewer/webhook-secret \
  --with-decryption --query Parameter.Value --output text)

URL=$(aws lambda get-function-url-config --function-name pr-reviewer-ingress \
  --region us-west-2 --query FunctionUrl --output text)

gh api repos/OWNER/NEW-REPO/hooks -f name=web \
  -f config[url]="$URL" \
  -f config[content_type]=json \
  -f config[secret]="$SECRET" \
  -F active=true \
  -f "events[]=pull_request"
```

(The Function URL is the current ingress; if it has been rotated, take it from `aws lambda get-function-url-config --function-name pr-reviewer-ingress --region us-west-2`.)

### 3.3 What "working" looks like

- The hook's **test ping will show ✗ failed** in the repo's webhook deliveries tab. Expected and harmless: the ingress only accepts `pull_request` events, so the ping is rejected at the door while real events return 202.
- Open a normal PR → the marker-bearing canonical comment appears (ARCHITECTURE.md §2.8). Draft PRs are skipped until marked ready (`ready_for_review`).
- Push to an open PR (`synchronize`) updates the same comment — never a second one (§2.8 reconciliation).

### 3.4 Shared-infra caveats

- Lambda account concurrency (10) and the token's 5,000 req/hr GitHub rate limit are **global across all onboarded repos** — irrelevant at personal scale, shared all the same.
- Known live-endpoint latency (ARCHITECTURE.md §8.2, measured 2026-09-17): expect minutes per review, not seconds, until the re-probe says otherwise.
- Cost stays ~$0 under Free Tier allowances unless a repo gets busy; spend is bounded by account concurrency (10) and the webhook kill switch (§1 above) covers the worst case.

---

## 4. Local archive sync

Retention ruling: S3 Expiration (Delete) at **90 days** on the `runs/` prefix stays within the Free Tier, and a documented runbook command — **not a tracked script** — syncs archives down to this machine.

### Why

The `pr-reviewer-archives` bucket expires each review run 90 days after it lands. The complete history must survive locally for evaluation, replay, and corpus building without paying cloud storage or busting the free tier. Sync before the window closes on runs you care about — the bucket does not warn. Practical cadence: run the sync at least weekly (a cron or launchd reminder on this box works); a run that lands and is never synced is gone at day 90.

### The command (exact)

```bash
aws s3 sync s3://pr-reviewer-archives/runs/ ~/.pr-reviewer/archives/ --exclude "*" --include "*.jsonl" --include "*.json"
```

Run it from a shell with an AWS session that can read the archives bucket. Least-privilege scope is `s3:ListBucket` on `pr-reviewer-archives` plus `s3:GetObject` on `arn:aws:s3:::pr-reviewer-archives/runs/*` — don't reach for broader credentials than that. Preview the copy before running for real with `--dryrun`; any profile holding that read scope works (set `AWS_PROFILE` if yours is non-default). The command is idempotent — `s3 sync` copies only new or changed objects, so re-running pulls just the runs that landed since the last sync.

### What lands where

`~/.pr-reviewer/archives/runs/{pr}/{sha}/{run_id}/` gains `events.jsonl` (the ts-ordered event stream) and `meta.json` (the run header), plus any `*.json` scoring/eval outputs. Everything else in the bucket — including the `static/` viewer assets — is excluded on purpose.

### Consumers

- The replay viewer (`static/`) and local evaluation/corpus tooling read this tree; no S3 calls needed once synced. Note the sync excludes viewer assets by design — a fresh machine gets `static/` from the repository checkout, not from the archive.

### Why not a script

A tracked script would add a top-level `tools/` directory entry beyond the existing sync utility; the `tools/sync_archives.py` proposal was withdrawn 2026-09-26. This runbook documents the command — it does not wrap it. If the command ever changes, update THIS section and ARCHITECTURE.md §6.4 in the same change.

---

## 5. Replay site operations

The replay site is the `pr-reviewer-viewer` Lambda behind a Function URL. It serves per-review replay pages from archived runs. This section covers the three operational surfaces the deploy owns: the bearer token, the static assets, and the logs.

### Endpoint

```bash
aws lambda get-function-url-config \
  --function-name pr-reviewer-viewer \
  --query FunctionUrl --output text
```

Every canonical review comment ends with a `[🔬 Full agent replay]` footer linking `<endpoint>/runs/{pr}/{sha}/` for that exact run (the worker resolves the base from its `REPLAY_BASE_URL` env — set in compute.tf from the viewer Function URL, never committed). The page shell is unauthenticated; artifacts behind the bearer token (below).

The live URL is distributed out of band with the bearer token (the endpoint is public-by-design but not committed to the repo — defense-in-depth against low-cost reconnaissance).

Auth model (ARCHITECTURE.md §6.5): the URL itself is `authorization_type = "NONE"` — enforcement happens in the handler. Static shell/assets and dot-segment rejections are unauthenticated; `/api/runs/{pr}/latest` and run artifacts compare `Authorization: Bearer` against the SSM token via `hmac.compare_digest`. The token is read from SSM **per invocation** with `WithDecryption` — no cache — so rotation needs no code deploy.

Routes (all others → 404): `/runs/{pr}/{sha}/` (shell, unauth), `/static/{file}`, `/api/runs/{pr}/latest` (bearer), `/runs/{pr}/{sha}/{run_id}/{file}` (bearer, `run_id` = 32 hex).

### Token provisioning & rotation

The token lives in SSM as a SecureString. Provision (once) or rotate (any time) with:

```bash
aws ssm put-parameter \
  --name /pr-reviewer/replay-token \
  --type SecureString \
  --value '<new-token>' \
  --overwrite   # omit on first provisioning
```

- Never commit or echo the token value; hand it to operators out of band.
- Rotation = new value at the same name; the next viewer invocation picks it up (no deploy, no restart). Old tokens die immediately.
- The viewer IAM role reads exactly this one parameter (`ssm:GetParameter`, no `kms:Decrypt` — SecureStrings ride the AWS-managed `aws/ssm` key, DECISIONS 2026-09-28).

### Static asset sync

Static assets (`static/index.html`, `static/review.css`, `static/review.js`) are git-tracked but **not** terraform-managed in S3. After changing them, sync:

```bash
aws s3 cp static/ s3://pr-reviewer-archives/static/ --recursive
```

Logs: `/aws/lambda/pr-reviewer-viewer` (7-day retention).

### Missing-key behavior (live-verified 2026-09-30)

Without `s3:ListBucket`, live S3 answers GetObject for a **missing** key with `AccessDenied` (anti-enumeration); `moto` returns `NoSuchKey`, so CI originally never saw the branch and the first deploy returned 500 there. The handler now maps `NoSuchKey`, `NotFound`, and `AccessDenied` to 404 — the API surface stays {200, 401, 404} — and a contract test pins all three codes via a stubbed `ClientError`. Deployed behavior follows the handler zip baked at apply time; re-apply after any handler change.

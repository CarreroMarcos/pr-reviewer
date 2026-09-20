# Runbook: DLQ redrive (HLD §2.5)

Source of truth for the procedure: HLD §2.5 ("SQS DLQ + Operator Redrive").
Resource names below are the real Terraform names
(`terraform/messaging.tf`, `terraform/iam.tf`, `terraform/observability.tf`);
region is `us-west-2` (`terraform/main.tf`). Do not invent ARNs — resolve
every ARN through the `get-queue-attributes` commands in the steps.

Trigger: the `pr-reviewer-dlq-depth` alarm (SNS topic `pr-reviewer-alerts`)
fires when `pr-reviewer-dlq` holds messages. The DLQ retains messages for
14 days; the work queue (`pr-reviewer-work`) retains for 4 days with
`maxReceiveCount 5` and 5400 s visibility (spec-002).
Worst-case poison cycle: 5 exhausted attempts × 5400 s ≈ 7.5 h before the
DLQ (was ~72 min at the pre-spec-002 720 s visibility).

## Permission set (exact — mirrors `terraform/iam.tf` operator role)

Assume the `pr-reviewer-operator` role first (trust requires MFA):

```bash
aws sts assume-role \
  --role-arn "arn:aws:iam::ACCOUNT_ID:role/pr-reviewer-operator" \
  --role-session-name redrive-YYYY-MM-DD \
  --serial-number "arn:aws:iam::ACCOUNT_ID:mfa/NAME" \
  --token-code 123456 \
  --region us-west-2
```

Export the returned credentials, replacing `ACCOUNT_ID` with the real
account id (`aws sts get-caller-identity --query Account --output text`).
The role grants exactly:

- On the DLQ (`pr-reviewer-dlq`): `sqs:StartMessageMoveTask`,
  `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`
- On the work queue (`pr-reviewer-work`): `sqs:SendMessage`

The source queue (`pr-reviewer-work`) additionally carries the
redrive-allow policy permitting the DLQ as a redrive source
(`terraform/messaging.tf`); without it `StartMessageMoveTask` fails
(HLD failure mode 21). If step 3 fails with an authorization error, stop
and compare the live policies against `terraform/iam.tf` — do not widen
permissions ad hoc.

## Step 1 — inspect DLQ depth and sample messages for root cause

```bash
DLQ_URL=$(aws sqs get-queue-url --queue-name pr-reviewer-dlq \
  --region us-west-2 --query QueueUrl --output text)

aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessagesVisible \
    ApproximateNumberOfMessagesNotVisible \
  --region us-west-2
```

Sample (up to 10) messages to determine the root cause. Sampling does not
delete; sampled messages stay invisible for the DLQ visibility window:

```bash
aws sqs receive-message --queue-url "$DLQ_URL" \
  --max-number-of-messages 10 \
  --attribute-names ApproximateReceiveCount SentTimestamp \
  --region us-west-2
```

Each body is a worker envelope (`envelope_version`, `repo_full_name`,
`pr_number`, `head_sha`, `delivery_guid`). Correlate `delivery_guid` /
`head_sha` with the worker log group (`/aws/lambda/pr-reviewer-worker`,
7-day retention) and the `pr-reviewer-worker-error-rate` /
`pr-reviewer-dynamodb-throttling` alarms to classify the cause (provider
5xx/429 exhaustion, persistent 401, poison envelope). Do not proceed until
the cause is named.

## Step 2 — deploy the fix and confirm it resolves the sampled cause

Fix forward (code, config, or credential rotation per HLD §2.6) and confirm
the sampled cause is resolved — e.g. a fresh review for the same
`repo_full_name`/`pr_number` publishes, or the failing dependency answers
healthy — before moving anything. Redriving into an unfixed worker just
re-fills the DLQ after burning `maxReceiveCount` attempts per message.

## Step 3 — `StartMessageMoveTask` back to the work queue

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

## Step 4 — verify the DLQ drains to zero and canonical comments converge

Poll until the DLQ is empty and the alarm clears:

```bash
aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessagesVisible \
  --region us-west-2

aws cloudwatch describe-alarms \
  --alarm-names pr-reviewer-dlq-depth \
  --region us-west-2 \
  --query 'MetricAlarms[0].StateValue'
```

Then confirm convergence per affected PR: exactly one comment bearing the
canonical marker `<!-- pr-reviewer:canonical:v1:{repo_full_name}#{pr_number} -->`
(HLD §2.8; the worker injects it, never the model):

```bash
gh api "repos/OWNER/REPO/issues/PR/comments" --paginate \
  -q '.[] | select(.body | contains("pr-reviewer:canonical:v1:OWNER/REPO#PR")) | .id'
```

Exactly one id per affected PR. Stale redrives (older `head_sha`) must
**not** mutate the canonical comment — the fence discards them (HLD §3.3);
a redrive that changed a comment to an older revision is a fencing bug,
not a success. This step is HLD §7.3 acceptance criterion (j).

## Step 5 — log the drill

Record, in the team ops log: date, DLQ depth at alarm time, root cause
from step 1, fix reference from step 2, the `MessageMoveTask` handle from
step 3, drain-to-zero confirmation plus per-PR convergence check from
step 4, and the operator identity. The redrive drill is a tested
operational procedure (HLD §2.5), not an implicit capability — an
unlogged drill did not happen.

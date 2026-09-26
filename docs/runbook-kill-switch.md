# Runbook: Kill switch (HLD §4.3)

Source of truth for the procedure: HLD §4.3 ("Kill switch (v6.6): set
worker reserved concurrency to 0 (or disable the webhook) — spend stops
immediately and queued work is retained."). Resource names below are the
real Terraform names (`terraform/compute.tf`, `terraform/messaging.tf`);
region is `us-west-2` (`terraform/main.tf`).

Trigger: any alarm on `pr-reviewer-alerts` that implies runaway spend or
a compromise you cannot bound otherwise (runaway LLM spend vs. the
config-driven daily budget; suspected secret compromise with the rotation
path unavailable). The kill switch is the stop-everything lever — it is
not a fix; fix-then-redrive comes after (see `docs/runbook-redrive.md`).

## Lever A — disable the GitHub webhook (primary, always available)

Stopping new deliveries at the source stops all spend: nothing invokes
the ingress, so nothing invokes the worker or the LLM.

```bash
# 1. List repo webhooks; find the one pointing at the pr-reviewer
#    Function URL (*.lambda-url.us-west-2.on.aws or current).
gh api repos/CarreroMarcos/pr-reviewer/hooks --jq '.[] | {id, active, config: .config.url}'

# 2. Disable it (active=false) — keep the hook so re-enable is one call.
gh api repos/CarreroMarcos/pr-reviewer/hooks/HOOK_ID -X PATCH -f active=false
```

Equivalent by hand: repo → Settings → Webhooks → the pr-reviewer hook →
**Disable** (do not delete).

Verify spend is stopped:

```bash
aws logs tail /aws/lambda/pr-reviewer-ingress --region us-west-2 --since 5m   # expect: silent
aws logs tail /aws/lambda/pr-reviewer-worker  --region us-west-2 --since 5m   # expect: silent
```

In-flight work finishes naturally (worker timeout 120 s) — it is not
reclaimed; it simply completes or exhausts its retries.

## Lever B — worker reserved concurrency 0 (HLD's named lever)

**Currently unavailable on this account.** AWS requires ≥ 10 unreserved
concurrency account-wide and the account's total Lambda concurrency is
10, so `put-function-concurrency` is rejected for any value — recorded in
the qa ledger (operator ruling 2026-09-15, SPR-60 supersede). Use Lever A
until a quota raise lands. At a future quota raise:

```bash
aws lambda put-function-concurrency --region us-west-2 \
  --function-name pr-reviewer-worker --reserved-concurrent-executions 0
```

This stops new worker executions immediately while the ingress keeps
acknowledging (202) — queued work is retained, not lost. Ingress
invocations cost single-digit ms and are effectively free; if even that
is unacceptable, use Lever A instead of (or alongside) this.

## What is retained (both levers)

- `pr-reviewer-work` queue: messages retained 4 days — nothing in flight
  or queued is deleted.
- `pr-reviewer-dlq`: retained 14 days.
- DynamoDB `pr-reviewer-state`: intact; claims expire on their own
  (`claim_until`), so no manual state surgery is needed.
- `prompts/` + config in SSM: untouched.

## Re-enable

1. Lever B: delete the reservation —
   `aws lambda delete-function-concurrency --region us-west-2 --function-name pr-reviewer-worker`.
2. Lever A: `gh api repos/CarreroMarcos/pr-reviewer/hooks/HOOK_ID -X PATCH -f active=true`.
3. Queued work drains by itself; if the DLQ holds drill/exhaustion
   corpses you want reviewed, redrive them per `docs/runbook-redrive.md`.

# Runbook: Onboard a new repository

Point the PR reviewer at another GitHub repo. The stack is already
repo-generic — state keys are `review:{repo_full_name}#{pr_number}`
(HLD §3.1), queues/comments/reconciliation are per-PR — so this is a
token-visibility check plus one webhook. **Zero Terraform changes.**

Event contract (HLD §2.1/§2.3): the ingress accepts only
`X-GitHub-Event: pull_request` with action ∈ {`opened`, `synchronize`,
`ready_for_review`, `reopened`} and skips drafts. Everything else is
rejected before enqueueing.

## 1. Token visibility

The worker reviews using the token in SSM `/pr-reviewer/github-token`
(region `us-west-2`). If it is a **fine-grained PAT**, add the new repo
under Repository access. If it is a **classic PAT with `repo` scope**,
nothing to do.

## 2. Create the webhook on the new repo

```bash
SECRET=$(aws ssm get-parameter --name /pr-reviewer/webhook-secret \
  --with-decryption --query Parameter.Value --output text)

gh api repos/OWNER/NEW-REPO/hooks -f name=web \
  -f config[url]="https://2clzftk3as32ofc7vxnv47pssu0zpajq.lambda-url.us-west-2.on.aws/" \
  -f config[content_type]=json \
  -f config[secret]="$SECRET" \
  -F active=true \
  -f "events[]=pull_request"
```

(The Function URL is the current ingress; if it has been rotated, take it
from `aws lambda get-function-url-config --function-name pr-reviewer-ingress
--region us-west-2`.)

## 3. What "working" looks like

- The hook's **test ping will show ✗ failed** in the repo's webhook
  deliveries tab. Expected and harmless: the ingress only accepts
  `pull_request` events, so the ping is rejected at the door while real
  events return 202.
- Open a normal PR → the marker-bearing canonical comment appears
  (§2.8). Draft PRs are skipped until marked ready (`ready_for_review`).
- Push to an open PR (`synchronize`) updates the same comment — never a
  second one (§2.8 reconciliation).

## Shared-infra caveats

- Lambda account concurrency (10) and the token's 5,000 req/hr GitHub
  rate limit are **global across all onboarded repos** — irrelevant at
  personal scale, shared all the same.
- Known live-endpoint latency (HLD §4.2, measured 2026-09-17): expect
  minutes per review, not seconds, until the re-probe says otherwise.
- Cost stays ~$0 under Free Tier allowances unless a repo gets busy;
  the budget alarm + kill switch (HLD §4.3) cover the worst case.

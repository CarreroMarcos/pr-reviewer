# Runbook: Replay Site Operations (T055/T056, HLD §7)

The replay site is the `pr-reviewer-viewer` Lambda behind a Function URL.
It serves per-review replay pages from archived runs. This runbook covers
the three operational surfaces the deploy owns: the bearer token, the
static assets, and the logs.

## Endpoint

```text
https://tmvyxasvjrxmllf6fjf3x2r73a0yzahp.lambda-url.us-west-2.on.aws/
```

Auth model (HLD §7): the URL itself is `authorization_type = "NONE"` —
enforcement happens in the handler. Static shell/assets and dot-segment
rejections are unauthenticated; `/api/runs/{pr}/latest` and run artifacts
compare `Authorization: Bearer` against the SSM token via
`hmac.compare_digest`. The token is read from SSM **per invocation** with
`WithDecryption` — no cache — so rotation needs no code deploy.

Routes (all others → 404): `/runs/{pr}/{sha}/` (shell, unauth),
`/static/{file}`, `/api/runs/{pr}/latest` (bearer),
`/runs/{pr}/{sha}/{run_id}/{file}` (bearer, `run_id` = 32 hex).

## Token provisioning & rotation

The token lives in SSM as a SecureString. Provision (once) or rotate
(any time) with:

```bash
aws ssm put-parameter \
  --name /pr-reviewer/replay-token \
  --type SecureString \
  --value '<new-token>' \
  --overwrite   # omit on first provisioning
```

- Never commit or echo the token value; hand it to operators out of band.
- Rotation = new value at the same name; the next viewer invocation picks
  it up (no deploy, no restart). Old tokens die immediately.
- The viewer IAM role reads exactly this one parameter
  (`ssm:GetParameter`, no `kms:Decrypt` — SecureStrings ride the
  AWS-managed `aws/ssm` key, DECISIONS 2026-09-28).

## Static asset sync

Static assets (`static/index.html`, `static/review.css`,
`static/review.js`) are git-tracked but **not** terraform-managed in S3.
After changing them, sync:

```bash
aws s3 cp static/ s3://pr-reviewer-archives/static/ --recursive
```

Logs: `/aws/lambda/pr-reviewer-viewer` (7-day retention).

## Known divergence (T056 live verify, 2026-09-30)

Requesting a **nonexistent** S3 key (e.g. a stale static path) returns
500 instead of the designed 404: without `s3:ListBucket`, S3 answers
GetObject for a missing key with `AccessDenied` (anti-enumeration), which
the handler's `NoSuchKey` catch does not cover. `moto` returns
`NoSuchKey` for the same call, so `tests/integration/test_viewer_routing.py`
stays green. All referenced routes serve real objects; treat any 500 in
the viewer logs as a candidate missing-key probe and check the requested
path. Follow-up ticket candidate (handler: map `NoSuchKey`/`AccessDenied`/
`NotFound` → 404, or grant `s3:ListBucket` on the archives bucket).

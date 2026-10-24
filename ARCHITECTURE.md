# Architecture — Autonomous Serverless PR Reviewer

**Status:** Current truth — this document states what the system **is**, in present tense.
**Owner:** Marcos Carrero · **Region:** `us-west-2` (US West — Oregon)
**Doc authority:** on **current behavior**, this file wins; on **why/history**, [DECISIONS.md](DECISIONS.md) wins (non-normative, append-only); on **operating procedures**, [OPERATIONS.md](OPERATIONS.md) wins. Task lists for the two synced specs live in `specs/001-pr-reviewer/tasks.md` and `specs/004-multi-agent-review/tasks.md`. Any disagreement between documents is a question for the owner, never a silent edit.
**Provenance:** condensed 2026-10-23 from HLD v6.10 + `specs/` (001 data model & contracts, 004 multi-agent HLD). Section numbering below is this document's own.

**Cost baseline:** expected AWS service charges of $0 under the applicable Free Tier / Always Free allowances for the documented workload. LLM pricing figures are approximate; per-review cost estimates are order-of-magnitude only and must not be hard-coded into alarms or budgets — they are configuration parameters.

---

## 1. System Overview

### 1.1 Purpose

An event-driven, fully serverless system that automatically reviews GitHub Pull Requests with a large language model and maintains **one canonical conversation comment per PR**, continuously updated as the PR evolves. Deployed entirely through Terraform, requiring no persistent servers.

### 1.2 System Invariant (one canonical comment)

> For each repository/PR, the system **converges to one canonical reviewer comment** identified by a stable marker embedded in the comment itself. A review may publish or mutate that comment only while its reviewed `head_sha` remains the current accepted revision in DynamoDB **and** the publication transition remains valid under the claim/lease protocol. Stale, duplicate, or out-of-order deliveries are safe and converge without allowing an older revision to replace a newer accepted review. Temporary duplicates during failure recovery are detected and reconciled deterministically.

The invariant is honest about distributed-systems reality: a database cannot atomically coordinate a DynamoDB conditional update with a GitHub POST. The design uses **lease + conditional state + deterministic reconciliation**.

### 1.3 Three Correctness Controls

1. **Delivery identity** — `X-GitHub-Delivery` GUID deduplication.
2. **Comment identity** — `comment_id` registry plus a canonical marker embedded in the GitHub resource, with reconciliation of duplicates.
3. **Revision fencing** — SHA-authoritative conditional state transitions; stale-write prevention via concurrency control.

### 1.4 Design Principles

- **Decouple ingress from processing** — GitHub requires 2xx within 10 seconds; inference takes minutes (measured 2026-09-17, §8.2).
- **Durable queue between ingress and work** — SQS is the durability boundary; the live GitHub PR head, not webhook arrival order and not SHA string comparison, is the source of revision truth.
- **At-least-once delivery, idempotent consumer** — every consumer path is safe under re-execution.
- **Least privilege** — three IAM roles (ingress, worker, operator-redrive).
- **Secrets never touch Terraform state or logs.**
- **Untrusted input discipline** — repository content is adversarial data; the model has no tools and cannot create control-plane state.
- **Standard library only** — Python 3.12 stdlib + `boto3`.

### 1.5 Architecture Diagram

```text
                      ┌─────────────────────────┐
                      │       GitHub PR          │
                      └───────────┬─────────────┘
                                  │ POST webhook + X-Hub-Signature-256
                                  │ + X-GitHub-Delivery (GUID) + X-GitHub-Event
                                  ▼
                  ┌──────────────────────────────────┐
                  │         INGRESS LAMBDA           │──── HTTP 202 (< 250ms) ───> GitHub
                  │  Timeout: 5s │ 512MB              │
                  │  a. Base64-decode if flagged     │
                  │  b. HMAC verify (sha256= prefix) │
                  │  c. X-GitHub-Event == pull_request│
                  │  d. Action allow-list, no drafts │
                  │  e. Dedup check (GetItem)         │
                  │  f. SendMessage → SQS             │
                  │  g. Mark delivery processed       │
                  └──────────────┬───────────────────┘
                                 │
                                 ▼
                  ┌──────────────────────────────────┐
                  │        SQS WORK QUEUE            │
                  │  Visibility: 1800s (2 × 900s)   │
                  │  Retention: 4d │ maxReceiveCount 3 │
                  └──────────────┬───────────────────┘
                                 │ event source mapping (batch = 1, max_concurrency = 2)
                                 ▼
                  ┌──────────────────────────────────┐        ┌───────────────────┐
                  │         WORKER LAMBDA           │        │  SQS DLQ            │
                  │  Timeout: 900s │ 1769MB          │        │  14d retention      │
                  │  h. Batched secret hydration      │        │  (operator redrive  │
                  │  i. Fetch + sanitize diff         │        │   + redrive-allow   │
                  │  j. Multi-agent review pipeline:  │        │   policy on source) │
                  │     3 specialists (correctness,   │        └───────────────────┘
                  │     security, tests) → verifier   │
                  │     → synthesizer (single-pass    │
                  │     fallback: docs-only / mutex)  │
                  │  k. Conditional claim → live-head │
                  │     fence → publish → finalize    │
                  │  l. Reconcile marker duplicates   │
                  │  m. Archive events & meta to S3   │
                  └──────┬──────────┬──────────┬─────┘
                         │          │          │
             ┌───────────▼───┐  ┌───▼──────────┴────────┐  ┌───────────────────────┐
             │    DynamoDB    │  │        GitHub         │  │  S3 ARCHIVES BUCKET   │
             │  pr-reviewer-  │  │  canonical PR comment │  │  pr-reviewer-archives │
             │  state         │  │  (stable marker +     │  │  (runs/ 90d lifecycle)│
             │  (provisioned) │  │   replay footer link) │  └───────────┬───────────┘
             └────────────────┘  └───────────────────────┘              │
                                                                        ▼
                                                            ┌───────────────────────┐
                                                            │     VIEWER LAMBDA     │
                                                            │  Function URL         │
                                                            │  (renders agent replay│
                                                            │   from S3 archive)    │
                                                            └───────────────────────┘

  SSM PARAMETER STORE (SecureString, out-of-band):
  /pr-reviewer/github-token  /pr-reviewer/webhook-secret
  /pr-reviewer/glm-api-key   /pr-reviewer/glm-model
  /pr-reviewer/glm-endpoint
```

---

## 2. Functional Components

### 2.1 Ingress Lambda — Security & Dispatch Gateway

Python 3.12 / `ingress_handler.handler`; 5 s timeout, 512 MB (memory buys CPU: the lazy boto3 cold start cannot fit the 5 s budget at 128 MB — DECISIONS 2026-09-14); Lambda Function URL, `AuthType: NONE`, CORS disabled; unreserved concurrency (2026-09-15 ruling, DECISIONS).

**Responsibilities (in strict order):** (1) reject bodies > 1 MiB with 413 **before** decoding, else base64-decode if flagged; (2) constant-time HMAC compare of the complete `"sha256=" + hexdigest` against `X-Hub-Signature-256` over raw decoded bytes — never parsed JSON; failure → 401; (3) `X-GitHub-Event == "pull_request"` gate — other signed events return 200, discarded; (4) action allow-list `opened`/`reopened`/`synchronize`/`ready_for_review`, drafts skipped (a `reopened` with unchanged head is an idempotent re-delivery — establish decides currency, §4.3); (5) GUID dedup (read-only GetItem); (6) `SendMessage` to SQS — on failure, 500 **without** marking processed (recoverable via GitHub's 3-day redelivery); (7) mark processed with 7-day TTL; (8) 202 within 250 ms. Webhook secret: fetched at cold start, cached warm, 30-minute refresh — rotation converges within 30 minutes.

**Response contract (canonical).** The response body is always empty; the status code is the entire contract.

| Condition | Status | State effect |
| :--- | :--- | :--- |
| HMAC valid, `pull_request` event, action allowed, new GUID, enqueued | 202 | delivery marked processed |
| Signed non-`pull_request` event | 200 | discarded, nothing enqueued |
| Action not in allow-list / draft PR | 200 | discarded, nothing enqueued |
| GUID already processed | 200 | no-op (idempotent) |
| HMAC verification failure / missing-malformed headers | 401 | nothing enqueued |
| Body exceeds 1 MiB | 413 | nothing enqueued; rejected before decode |
| HMAC-valid but body unparseable or schema-invalid | 200 | discarded — permanent; 4xx would trigger pointless redelivery |
| SQS `SendMessage` failure | 500 | delivery **not** marked (recoverable via redelivery) |
| Account concurrency throttle (unreserved) | 429 | nothing enqueued; GitHub records a failed delivery |

**Admission semantics:** both functions run unreserved, so the loss boundary is the **account-level** concurrency pool (quota 10, shared with the worker). A sustained ingress flood can starve the worker — the accepted residual (failure mode 5), stopped by the webhook kill switch (OPERATIONS.md §1). Beyond the account ceiling: 429, and the only recovery is admin redelivery within 3 days.

**Envelope (< 1 KB, typed schema):** `envelope_version` (constant `v1`), `event_type` (constant `pull_request`), `action` (enum: `opened|reopened|synchronize|ready_for_review`), `repo_full_name` (`^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`, ≤ 128), `pr_number` (int > 0, ≤ 10⁹), `head_sha`/`base_sha` (40-char lowercase hex), `sender` (GitHub login charset, ≤ 64), `delivery_guid` (UUID, ≤ 64). Additive evolution only. The worker constructs `diff_url`/`comments_url` from identifiers and validates every SQS message against this schema before use — redrive and injected messages can contain arbitrary bytes; malformed messages are logged (IDs only) and completed non-retryably. All GitHub-derived fields are control-character-stripped before logging or prompt assembly.

### 2.2 SQS Work Queue — Durability Boundary

`pr-reviewer-work`, Standard queue. Visibility timeout **1800 s** (= 2 × 900 s Lambda timeout; `Retry-After` extensions may exceed it). Retention 4 days. Redrive `maxReceiveCount = 3` → DLQ. Event source mapping `batch_size = 1`, `scaling_config.maximum_concurrency = 2`. The source queue carries a **redrive allow policy** naming the operator role — without it the documented redrive path fails (failure mode 21). On retryable errors with `Retry-After`, the worker calls `ChangeMessageVisibility`. Retry ownership: the queue owns retries — the worker raises, visibility expiry redelivers, `maxReceiveCount 3` bounds attempts; the only in-request retry is the single 401 credential re-fetch (§2.3).

### 2.3 Worker Lambda — Review Execution Engine

Python 3.12 / `worker_handler.handler`; 900 s timeout, 1769 MB (≈ 1 vCPU; DECISIONS 2026-09-19); triggered only by the SQS event source mapping; unreserved concurrency.

**Responsibilities:** (1) credential hydration — one batched `GetParameters` at cold start, cached warm behind an injectable accessor; on 401: drop cache → re-fetch → retry once; 30-minute periodic refresh bounds stale-credential lifetime; (2) diff retrieval — endpoints constructed from envelope identifiers (payload-embedded URLs never used for navigation), `X-GitHub-Api-Version: 2026-03-10` pinned, explicit `User-Agent`; GitHub responses are third-party data — validate shape (HTTP 200, `head.sha` 40-hex) before the live-head fence consumes them; (3) deterministic pre-model diff budget — lockfiles excluded from semantic content but summarized deterministically (same delta → identical text); `MAX_FILES = 500`; `MAX_CHANGED_LINES = 25,000`; `MAX_INPUT_BYTES = 800,000`; (4) multi-agent pipeline or single-pass (§6); (5) fenced publication (§4.3); (6) structural validation of the assembled comment — marker present, bounded length, expected sections, no credential-like strings, no hidden HTML/script, no control-plane directives, prompt canary absent; (7) error classification with the PATCH-404 decision table:

| Observation | Interpretation | Action |
| :--- | :--- | :--- |
| PATCH 404 + list readable + marker found on another comment | Comment deleted; migrated | Adopt marker-bearing comment, reconcile |
| PATCH 404 + list readable + no marker-bearing comment | Comment deleted, none exists | Creation lease → POST → persist ID → re-check |
| 403/404 on the list/GET itself | Token lost access / repo changed | Non-retryable: log, complete, alert |
| 429 with `Retry-After` / 403 secondary rate limit | Transient throttling | Raise; adjust visibility per `Retry-After`; queue retries |
| 5xx | Transitive provider error | Raise; queue retry to maxReceiveCount 3, then DLQ |
| 401 | Credential expired/rotated | Invalidate cache, re-fetch SSM, retry once; then non-retryable |
| LLM timeout / 429 / 5xx / structurally invalid output | Provider failure or unusable output | Raise for queue retry — the LLM call is side-effect-free, so queue retry is idempotent |
| LLM request-construction fault (e.g. control chars in key; deterministic) | Client-side construction failure | Typed `LlmError("invalid_key")`; non-retryable: complete; publish failure notice per §5.3 |
| Assembled comment fails structural validation | Invalid output at the publish boundary | Non-retryable: complete and alert — invalid content is never published |

**HTTP timeouts:** GitHub single 10 s socket; LLM connect 2 s / read 240 s default (`GLM_READ_TIMEOUT_S`, env-tunable); the 900 s Lambda timeout is a backstop.

### 2.4 DynamoDB State Store

Table `pr-reviewer-state`, partition key `pk`, PROVISIONED 20/20 + GSI `pr-runs-index` 5/5 (Always Free envelope: 25 RCU/25 WCU total). Item types: `delivery:{guid}` (TTL 7 days); `review:{repo_full_name}#{pr_number}` (§4.1); `mutex:pr-reviewer-worker` (§6.2). Capacity: ≈ 4 WCU + 1.5–3 RCU per new-revision review (items ≤ 1 KB); burst ≤ 20 WCU/s — under the 25 ceiling; throttling resolves via the retry path (failure mode 14).

### 2.5 SQS DLQ + Operator Redrive

`pr-reviewer-dlq`, 14-day retention. Operator IAM role, minimum set: `sqs:StartMessageMoveTask`, `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` on the DLQ, plus `sqs:SendMessage` on the work queue — named in the source queue's redrive allow policy. Redrive is a tested operational procedure (OPERATIONS.md §2), not an implicit capability.

### 2.6 SSM Parameter Store

| Parameter | Type | Notes |
| :--- | :--- | :--- |
| `/pr-reviewer/github-token` | SecureString | Fine-grained PAT: **Pull requests: read** + **write** |
| `/pr-reviewer/webhook-secret` | SecureString | Ingress-only IAM |
| `/pr-reviewer/glm-api-key` | SecureString | Worker-only IAM |
| `/pr-reviewer/glm-model` | String | `glm-5.3-flash` |
| `/pr-reviewer/glm-endpoint` | String | HTTPS scheme + host allow-list enforced at hydration |

Terraform defines IAM read policies only — no `aws_ssm_parameter` resources; no plaintext in state. SecureStrings use the AWS-managed `aws/ssm` KMS key; SSM decrypts server-side via `WithDecryption`, so no direct `kms:Decrypt` grants are needed. **Rotation:** update SSM first, revoke the old credential last; warm containers converge within the 30-min TTL or via 401-triggered re-fetch. Webhook-secret rotation 401s deliveries up to 30 min — rotate in a maintenance window, recover via 3-day redelivery. Compromise: revoke at provider → overwrite SSM → force cache-bust (redeploy) → verify 401-recovery → inspect the 7-day log groups.

### 2.7 LLM — GLM-5.3-Flash, Model I/O Contract

320B/18B-active MoE, 1M-token context; temperature 0.2; model string and endpoint host from SSM (validated at hydration). One versioned system prompt (`prompt_version: v3`, PR #177), exercised by the test suite, fixes the untrusted-data framing, forbids tools, and mandates a bounded Markdown shape: `## Summary`; `## Findings` (severity ∈ {`HIGH`, `MEDIUM`, `LOW`}, `path:LINE`, issue, suggested fix; count ≤ `max_findings`, default 20); `## Risk Notes`; "No significant issues found." is the defined empty-finding output. Model input is the worker-assembled bounded payload — PR title/description plus the budgeted diff and, on re-reviews, the prior canonical comment — never bare diff text. Findings never contain `@mentions`, external image URLs, or approval verdicts. Prompt v3's **quote-to-carry rule**: a prior finding is carried over only when it quotes the exact current lines showing the issue persists; no quotable lines means treated as addressed. A configured prompt canary is checked during structural validation — output containing it is rejected non-retryably (a leaked prompt would be broadcast to a public comment, so enforcement is output-side).

### 2.8 Comment Strategy

Single evolving PR conversation comment (Issues Comments API) bearing the canonical marker `<!-- pr-reviewer:canonical:v1:{repo_full_name}#{pr_number} -->`. The marker makes canonical identity survivable even if DynamoDB state is lost; it is **injected by the worker** — never generated by the model — so it cannot regress with model behavior. It is an internal convergence mechanism: external tooling MUST NOT parse it; format changes are versioned in place. The worker also injects the deterministic revision header (`**Review #N · updated {stamp} PT**`, `#N` = established generation + 1, stamp in `America/Los_Angeles` rendered in code) and a footer link `[🔬 Full agent replay]({viewer_url}/runs/{pr}/{sha}/)` to the Viewer Lambda, followed by run provenance — the pipeline that produced the review (`multi-agent`, `single-pass`, or `multi-agent → degraded to single-pass`) and the configured model id — rendered code-side at publish, never by the model. Inline Reviews API comments: roadmap (§10).

Packaging: `lambda/common/` is the single source of truth for the envelope validator, marker builder, sanitization/redaction, and structured-log helpers, packaged into all three deployment zips; handlers stay thin. Security-sensitive helpers are single-implemented — one implementation is a security requirement. `terraform.tfstate` is gitignored, backed up encrypted, single-operator (no locking — never two concurrent applies). Lambda environment variables must never carry secrets: SSM-only is a constraint, not merely a current fact.

---

## 3. State Model and Revision Fencing

### 3.1 Review State Record

```json
{
  "pk": "review:org/repo#123",
  "status": "ACTIVE",
  "comment_id": 987654,
  "generation": 42,
  "head_sha": "bbb...",
  "last_seen_sha": "bbb...",
  "claim_owner": "delivery-guid",
  "claim_until": "epoch+780s",
  "updated_at": "..."
}
```

**Field contract:** stored `status` ∈ {`CLAIMED`, `ACTIVE`} — `ABSENT` is modeled by item absence and `STALE` is derived (a `CLAIMED` record whose `claim_until` has passed is re-claimable); neither is ever written. `generation` starts at 0, monotone non-decreasing per key; `head_sha`/`last_seen_sha` are 40-hex; `claim_until` is epoch seconds; `claim_owner` is the raw `delivery_guid`; `comment_id` (GitHub int64) present only in `ACTIVE`; `updated_at` ISO-8601 UTC.

**State machine:** `ABSENT → (establish) → CLAIMED → (POST succeeds) → ACTIVE`; next revision: `ACTIVE → (establish, generation + 1) → CLAIMED → ACTIVE`. **Current accepted revision** is the PR head SHA confirmed against GitHub's **live PR state**; SHAs are not orderable — the comparison is **equality against the live PR head**, never lexicographic or any synthetic ordering. **Claim lease: 780 s**, decoupled from the Lambda timeout (900 s) and queue visibility (1800 s); it spans only claim through finalize — review precedes the claim and is side-effect-free, so no heartbeat is needed; crash takeover is covered by lease expiry, not lease length.

### 3.2 Fenced Publication Protocol (Exact Order)

1. **Establish (conditional):** accept the incoming `head_sha` only if (a) no record exists (first write), (b) it equals the stored `last_seen_sha` (idempotent re-delivery), or (c) a **live GitHub head fetch** confirms it is the PR's current head (`generation` increments). Any other incoming SHA is **not established** — superseded; `last_seen_sha` records the most recently observed webhook SHA regardless of acceptance.
2. **Review.** Diff fetch, sanitize, LLM inference (§6).
3. **Claim (conditional):** `UpdateItem` with `ConditionExpression: head_sha = :reviewed AND generation = :gen AND (claim_until < :now OR attribute_not_exists(claim_owner))`.
4. **Fence (live, after claim succeeds — never before):** fetch the PR's current head from GitHub and confirm it equals the reviewed SHA, immediately before the PATCH/POST, so no concurrent worker can interleave between fence and publish. Mismatch → discard as stale.
5. **Publish:** external GitHub PATCH/POST.
6. **Finalize (conditional):** `ConditionExpression: head_sha = :reviewed AND generation = :gen` — revision only, the lease is not re-checked; failure means a newer accepted revision landed concurrently — log and reconcile rather than overwrite.

The residual TOCTOU gap between steps 4 and 5 is acknowledged and bounded: it is closed by marker-based reconciliation, not denied.

### 3.3 Comment Reconciliation

Exactly-one is a **convergence property**: on missing `comment_id`, 404 recovery, or lease takeover — list comments (shape-validated and **fully paginated** before matching; an unparseable list is non-retryable), find all bearing the exact worker-injected marker; exactly one → adopt; multiple → deterministically select the lowest comment ID and delete extras; none → creation lease → POST → persist → re-check. The **creation lease** is a conditional write requiring `attribute_not_exists(comment_id)` plus the claim condition — two concurrent first-posters cannot both POST; the loser re-runs reconciliation and adopts the winner's comment.

---

## 4. Data Model & Contracts (from the spec suite)

**Entities:** (1) **delivery** — `delivery:{guid}`, TTL 7 days, fields = the §2.1 envelope, uniqueness on `delivery_guid`; (2) **review state** — the §3.1 record; the failure state is comment content, not extra stored fields; (3) **canonical comment** — one per repo/PR (§2.8, §3.3), created once (creation lease), updated in place, deletion/migration via the PATCH-404 table; (4) **PR revision** — the head at a moment in time; the **live GitHub PR head** (shape-validated before use) is the sole authority for "current"; a published comment content corresponds to exactly one revision.

**Webhook contract invariants:** POST from GitHub's webhook dispatcher only (any other caller is hostile; CORS disabled); acknowledgment never depends on review/publication latency; an accepted event is durable (enqueued) before acknowledgment; exactly one target repository per deployment (bound by webhook registration + HMAC); nothing repository-derived is ever logged raw.

**Canonical comment content — form 1 (review):** model output per §2.7, structurally validated, bound to the reviewed revision, fully regenerated each cycle; revision header worker-injected (§2.8); the failure notice carries no header.

**Form 2 (failure-state notice):** when a review for the current head permanently fails, the PR must not silently show a stale or missing review. Fixed, system-generated template — never model output, never repository-derived text; the only variable is the reviewed head SHA: "Automated review could not be completed for revision {head_sha}. The failure has been logged for the operator; no findings are available for this revision." Prohibited: internal error details, provider names, HTTP statuses, stack traces, credential-like strings, `@mentions`, verdicts. Published only through the §3.2 fenced path, subject to the same revision-currency check (a notice for a stale revision is **not** published). **Triggers:** transient LLM-side errors publish the template at the **final** queue attempt (then raise, so DLQ/alert/redrive proceed unchanged); assembly-validation failures and LLM request-construction faults publish immediately (permanent class); 401 from the LLM publishes; 401/403/404 from GitHub (publication impossible) alerts only; schema-invalid envelopes and superseded deliveries never publish. Reversion: the next successful review replaces the notice through the ordinary path. Invariants: after any failure the PR still converges to exactly one canonical comment; no platform write other than that comment; no secrets or raw untrusted content in comment output.

---

## 5. Multi-Agent Review Pipeline & Replay Site

### 5.1 Topology, Fallbacks, Budgets

Three non-overlapping generator specialists — **correctness**, **security**, **tests** — each scoped to its specialty with an explicit do-not-flag list (security reserved for the security specialist, test omissions for the tests specialist, pre-existing/out-of-diff bugs and accepted residuals out of scope everywhere). A **verifier** tries to *falsify* each candidate (reduce-only, evidence-grounded): confirmed only with a stated failure mechanism plus exact diff lines; unverifiable HIGH → `escalated`; unverifiable MEDIUM/LOW → killed only with a `kill_reason` citing the missing evidence, else escalated. The **synthesizer** merges survivors into the single canonical comment (duplicates = same file, |Δline| ≤ 2, same category, and title overlap; escalated findings render at original severity with `[Requires Verification]`). Concurrency is `concurrent.futures.ThreadPoolExecutor` wrapped in `asyncio` (stdlib-only by constitution — aiohttp/httpx lint-banned), with an explicit executor shutdown pattern so a timed-out specialist cannot hang `asyncio.run` teardown.

Fallbacks: **docs-only** prose diffs bypass the fanout (a truncated diff fails the predicate safe); the **DynamoDB mutex** (`mutex:pr-reviewer-worker`, lease TTL 900 s ≤ worker timeout, 50%-TTL refresh, token-conditioned release) serializes fanout — a contender never defers visibility (SQS ESM deletes the message on success) and runs single-pass inline, or skips fail-fast when remaining budget is under the viability floor; **elapsed-budget gates** before each stage require remaining time ≥ that stage + everything downstream + `BUDGET_MARGIN_S` (60 s; gate-1 total 780 s within the 900 s timeout), else degrade to single-pass while it still has budget. The wave succeeds iff ≥ 2 specialists return parseable findings; specialists are single-attempt (retry ownership stays with the SQS queue); total fan-out failure → `FanoutDegraded`, caught inside the review closure — it must never escape to the worker boundary, or SQS redelivery would replace the single-pass fallback.

### 5.2 Injection Hardening

Stage boundaries use per-stage cryptographically nonced delimiters (each nonce appears exactly twice — its own open/close tags; any other occurrence is a boundary-break signal). Sanitization is span-preserving (fenced code blocks and inline backticks stashed before prose neutralization of images/links/autolinks, then re-substituted) so type signatures and code samples survive. Reasoning excerpts are untrusted inter-stage data — they reach downstream prompts only inside nonced blocks. A final deterministic redaction pass replaces email addresses and ≥16-char hex runs (plus the prompt canary) with typed placeholders across prose and code spans before publish.

### 5.3 Archives, Index, Replay Viewer

Every run streams structured events (`events.jsonl`, `{v, run_id, ts, type, ...}` — `agent_started`/`agent_completed`/`verification_done`/`review_synthesized`/`degraded_to_single_pass`/`concurrency_single_pass`/`review_published` etc.) and a `meta.json` header to `s3://pr-reviewer-archives/runs/{pr}/{sha}/{run_id}/` (90-day lifecycle expiry on `runs/`). A best-effort DynamoDB index row (GSI `pr-runs-index`: partition `pr_number`, sort `started_ts`, INCLUDE projection incl. `run_id`) is written after finalize for the latest-run query. Findings coordinates are 1-based **post-image** lines; out-of-range specialist coordinates are clamped-and-flagged; the verifier re-anchors to the post-image.

The **viewer** Lambda sits behind a Function URL (`authorization_type = "NONE"`). All routes are public: `/runs/{pr}/{sha}/` (shell), `/static/{file}` (dot-segments rejected), `/api/runs/{pr}/latest`, `/runs/{pr}/{sha}/{run_id}/{file}` (`run_id` = 32 hex); everything else 404. Archived event content is model-controlled and passes the same sanitization before render. Missing S3 keys map to 404 across error semantics (live S3 anti-enumeration returns `AccessDenied`, not `NoSuchKey`). IAM: `s3:GetObject` on `runs/*` and `static/*`, `dynamodb:Query` on the GSI — no SSM access. Operations: OPERATIONS.md §5.

### 5.4 Evaluation Gates (quality is a model dependency)

Review quality is load-bearing *model* behavior and gets a pinned evaluation set scored against a rubric; reruns are mandatory whenever `prompt_version` or the model string changes. Multi-agent gates are paired case-by-case vs the single-pass pin on the 24-case / 18-defect corpus, 3 runs/case: (1) recall — no regression (one-miss noise band, re-run rule); (2) precision uplift — the measured **+0.0608** operating point re-ratified by owner ruling 2026-09-29 (DECISIONS); (3) **0 wrongful verifier kills** — the promotion bar for enabling multi-agent on real PRs; (4) synthesizer fidelity — 0 dropped / 0 invented; (5) floors hold (0 fabricated, robustness cases pass). Offline scoring runs 100% offline; embeddings for the wrongful-kill cosine arm are generated only in the out-of-band capture harness (local Ollama `nomic-embed-text`), never in a request-serving path; the 0.76 cosine threshold is pre-registered and conservative (the deterministic location arm carries the gate — DECISIONS 2026-09-28).

---

## 6. Security Model

### 6.1 IAM Roles (Three + Viewer)

```text
Trust policies: the ingress role trusts lambda.amazonaws.com scoped to its
function ARN (aws:SourceArn); the worker role trusts on aws:SourceAccount — a
function-ARN SourceArn condition fails CreateEventSourceMapping validation;
the operator role trusts the terraform-admin IAM user gated on MFA. The
worker's aws:SourceAccount scope trusts any future Lambda in the account —
accepted for this single-operator, IaC-only account; the concrete tightening
path is a permissions boundary on the worker role (roadmap).

INGRESS ROLE:  logs; ssm:GetParameter (webhook-secret ARN);
               sqs:SendMessage (work queue); dynamodb:GetItem, PutItem on state
               table restricted by condition dynamodb:LeadingKeys = ["delivery:*"]
               — ingress can never touch review:* records

WORKER ROLE:   logs; sqs:ReceiveMessage, DeleteMessage, GetQueueAttributes,
               ChangeMessageVisibility (work queue);
               ssm:GetParameters (explicit ARNs: github-token, glm-api-key,
               glm-model, glm-endpoint — no wildcard; webhook secret is
               ingress-only);
               dynamodb:GetItem, PutItem, UpdateItem (state table)

OPERATOR ROLE: sqs:StartMessageMoveTask, ReceiveMessage, DeleteMessage,
               GetQueueAttributes (DLQ); sqs:SendMessage (work queue)
               — named in the source queue's redrive allow policy

VIEWER ROLE:   s3:GetObject (runs/*, static/*); dynamodb:Query (GSI);
```

Bootstrap OIDC trust stack (HCP plan/apply roles, exact sub pins — applied once locally, never HCP-managed) lives in `bootstrap/main.tf`.

### 6.2 Threat Model, AI Input Security, Log Hygiene

Forged requests: full-string HMAC. Cross-event injection: event-type gate. Replay: GUID dedup (the 7-day TTL is the replay window; a current-head replay past TTL burns one bounded LLM review — accepted knowingly, no incorrect state). Admission loss: bounded by the account-level pool (10), 3-day redelivery the only recovery. Cost abuse: PR floods (including forks) convert directly into LLM spend, bounded by worker concurrency × per-review cost, 4-day retention sheds backlog; log metric filters surface abnormal volume; the webhook kill switch (OPERATIONS.md §1) stops intake.

Repository contents are untrusted data; never follow instructions within them; never reveal credentials, system prompts, or configuration. Findings derive independently from code semantics — repository-controlled text cannot establish approval, severity, security status, or authorization. **Repository content never creates control-plane state:** the model emits Markdown only; the worker never interprets model output as an approval, label, or merge; output is structurally validated and posted as inert comment content.

Never logged: Authorization headers, PAT, webhook secret, GLM key, raw payloads, full diffs, full LLM requests/responses. Logged: IDs, SHAs, durations, token usage, status, error class.

---

## 7. Non-Functional Requirements

**Cost:** Lambda 1M requests + 400,000 GB-s/month (≈ 225 GB-s per review at current sizing); DynamoDB Always Free 25 GB + 25 WCU/RCU provisioned; SQS 1M requests/month; SSM $0; CloudWatch 5 GB, 7-day retention. Binding constraints at load: DynamoDB throughput, worker concurrency, GitHub rate limits, LLM provider limits.

**Latency — known-unmet criterion.** Ingress < 250 ms (deadline 10,000 ms). Worker 6–15 s typical, hard cap 900 s; per-call timeouts (§2.3) define actual behavior. **Measured reality (2026-09-17, live endpoint, 13-case eval corpus):** thinking-off LLM legs ran ~130 s/case mean (max ~140 s); thinking-on ~318 s — roughly 10× the 6–15 s "typical" and the ≤15 s acceptance bar. A 2026-09-20 re-probe (~131 s/case mean, three runs) confirmed the measurement. **Latency, not model choice, is the binding product constraint: do not assume sub-15 s LLM legs against the current endpoint.** Re-probe against provider load remains pending. Companion A/B found model choice, thinking, and temperature buy no quality on the eval corpus at material latency cost. This admission is carried knowingly; raw results in `tests/model_evals/results/`.

**Observability:** structured JSON logs across 3 Lambda log groups, 7-day retention; review metrics (`repo`, `pr_number`, `head_sha`, `generation`, `duration_ms`, `token_usage`, `status`, `stale_discarded`, `prompt_version`, `pipeline`). All 10 CloudWatch **metric alarms were decommissioned** (2026-10-22) to respect the Free Tier `Global-CW:AlarmMonitorUsage` limit; alerting rides the SNS topic (`pr-reviewer-alerts`) and 6 log metric filters: `ingress_unauthorized`, `worker_token_usage`, `worker_contention`, `runs_published`, `runs_multi_agent`, `runs_docs_only`. **Kill switch:** the account Lambda quota of 10 forbids reserved-concurrency-0, so **disabling the GitHub webhook is the primary kill switch** — procedure in OPERATIONS.md §1. In-flight runs finish within 900 s; queued messages are preserved up to 4 days.

**Testing:** gates — no commit without the installed pre-commit hooks (hygiene, ruff, gitleaks); no `terraform apply` without a green test run. Tiers — (1) **unit**: pure logic (HMAC vectors, envelope schema, every error-table branch); (2) **state machine**: deterministic establish/claim/fence/finalize interleavings against an in-memory DynamoDB stub; (3) **contracts**: committed signed-webhook fixtures, stubbed HTTP, injected clients/clocks (clocks exercise the 30-minute TTL and rotation paths), Terraform-vs-code value pins; (4) **model evaluations**: the pinned eval set (§5.4). Tests are organized by behavior, parallel-safe, no live external calls.

---

## 8. Deployment & Configuration Baseline

| Component | Value |
| :--- | :--- |
| Ingress reserved concurrency | none (unreserved, 2026-09-15 ruling) |
| Worker | 900 s / 1769 MB, unreserved; ESM max_concurrency 2 |
| SQS visibility timeout / maxReceiveCount | 1800 s / 3 |
| Claim lease | 780 s, fixed (claim→finalize, no renewal), decoupled |
| API version pin | `2026-03-10` |
| Fencing comparison | first-write, idempotent-equality, or live-head confirmation — never SHA ordering |
| Metric alarms | 0 (all 10 decommissioned for Free Tier compliance) |

**Terraform BOM:** `aws_lambda_function` ×3 + function URLs ×2 + event source mapping; `aws_sqs_queue` ×2 + redrive + redrive-allow policies; `aws_s3_bucket` (`pr-reviewer-archives`, 90-day expiry on `runs/`); `aws_dynamodb_table` (20/20 + GSI 5/5); IAM roles ×4 (incl. viewer); log groups ×3; SNS topic; 6 log metric filters; `archive_file` ×3. Absent: API Gateway, VPC, NAT, S3 backend, SSM parameter resources, Secrets Manager, EventBridge.

**Deployment:** populate the five SSM parameters (§2.6) → `terraform init && terraform apply` → register webhook (Pull requests only) → acceptance testing. Apply from a clean, reviewed revision (zips embed the source). Rollback is re-applying the previous revision: no data migrations exist, and comment state is fenced and converges. **Acceptance criteria:** (a) delivery log 202 in seconds; (b) one canonical comment in 6–15 s — **measured 2026-09-17: currently unmet** (~130 s/case; §7 latency); (c) second push updates the same comment; (d) two rapid pushes leave only the latest head SHA reflected; (e) bad-signature webhook → 401, nothing enqueued; (f) DLQ empty on happy path; (g) artificially stale head SHA redriven into the queue must **not** mutate the canonical comment; (h) deleting the bot comment + new push converges to exactly one marker-bearing comment; (i) concurrent workers on the same PR still converge to a single comment with the correct head SHA; (j) redrive drill: a DLQ message moved back after a fix converges without duplicate comments.

---

## 9. Roadmap

Committed and proposed directions, in brief (owner accepts/rejects proposals; accepted work lands via the normal PR flow):

- **Latency re-probe** against the live endpoint (§7) — the binding product constraint; owner-gated.
- **Inline review comments** via the GitHub Reviews API; **GitHub App installation tokens** replacing the fine-grained PAT (committed).
- **Whole-file context** for changed files under a size cap — fewer false positives from hunk-blindness; priced against the measured latency/token budget (proposed).
- **Accepted-residuals memory** — operator-curated settled-findings list injected as context so re-reviews stop re-flagging decided nits; infrastructure exists (`lambda/common/residuals.py`), content file pending (proposed).
- **Failed-CI awareness** — one check-run read per review, footer status line; needs a `checks: read` PAT scope (proposed). **"What changed since last review" header** — one capped delta line on re-reviews (proposed).
- **CI smoke tests** with synthetic signed webhooks; expanded alarms; automated dependency audit with lockfile refresh in CI (committed).
- **Model frontier** — stronger/reasoning models and two-stage triage unexplored until the eval corpus grows (open; quality first, cost second).

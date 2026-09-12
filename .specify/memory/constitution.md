# Serverless PR Reviewer Constitution

Derived from `docs/HLD.md` (v6.7). The HLD remains the authoritative technical design;
this document defines the non-negotiable rules that govern all work on the project.

## Core Principles

### I. Fully Serverless Terraform

Every infrastructure component MUST be defined in Terraform and MUST use serverless AWS
services (Lambda, SQS, DynamoDB, SSM Parameter Store, CloudWatch). No persistent
servers, containers, or always-on compute are permitted. Provisioned capacity MUST stay
within the Always Free envelope (DynamoDB PROVISIONED 25 WCU / 25 RCU). Manual console
changes are prohibited; drift is corrected by re-applying reviewed Terraform. The MVP
Bill of Materials MUST NOT introduce API Gateway, VPC/NAT, S3 backend, Secrets Manager,
or EventBridge.

Rationale: the cost baseline is $0 under Free Tier allowances, and a single operator
must be able to deploy, roll back, and reason about the entire stack from versioned
configuration alone.

### II. Python 3.12 Standard Library + boto3 Only

Runtime code SHALL target Python 3.12 and MUST depend only on the Python standard
library and `boto3`. Adding any third-party runtime dependency requires a constitution
amendment. Test tooling MAY live in the dev dependency group but MUST NOT be imported
by deployed handlers. Shared helpers in `lambda/common/` and public handlers carry
stdlib `typing` annotations; validators return typed stdlib constructs (e.g.
`dataclass`).

Rationale: minimal supply-chain surface, predictable cold starts, and no dependency
pinning or audit burden in the runtime path.

### III. Secrets Never in Terraform State or Logs

Secret values SHALL exist only in SSM SecureString parameters. Terraform MUST define
IAM read policies only — never `aws_ssm_parameter` resources — so no secret value is
ever written to state. Secrets MUST NOT be placed in Lambda environment variables, and
MUST NOT appear in logs. The following are never logged: Authorization headers, the
GitHub PAT, the webhook secret, the GLM key, raw payloads, full diffs, and full LLM
requests/responses. Logs carry IDs, SHAs, durations, token usage, status, and error
classes only.

Rationale: the local state backend has no locking or encryption guarantees, and log
groups are shared, retained artifacts; a single leaked value compromises the whole
deployment.

### IV. One Canonical PR Comment

For each repository/PR the system MUST converge to exactly one reviewer comment,
identified by a stable canonical marker that the worker injects when assembling the
comment — the model MUST NOT generate it. Publication MUST follow the fenced protocol
in strict order: establish → review → claim → live-head fence → publish → conditional
finalize. Revision truth is equality against the live GitHub PR head — never SHA
string ordering. Duplicate or orphaned marker-bearing comments MUST be reconciled
deterministically: adopt exactly one (lowest comment ID wins), delete extras, or take
the creation lease before a first POST.

Rationale: no database can atomically coordinate a DynamoDB conditional update with a
GitHub POST, so convergence — not instant uniqueness — is the honest guarantee.

### V. At-Least-Once Delivery, Idempotent Consumers

The work queue provides at-least-once delivery; every consumer path MUST be safe under
re-execution. The three correctness controls are delivery identity
(`X-GitHub-Delivery` GUID deduplication), comment identity (the `comment_id` registry
plus the canonical marker), and revision fencing (SHA-authoritative conditional state
transitions). Stale, duplicate, or out-of-order deliveries MUST converge without
letting an older revision replace a newer accepted review. Retry ownership belongs to
the queue (visibility expiry, `maxReceiveCount`, DLQ); the only permitted in-request
retry is the single 401 credential re-fetch.

Rationale: duplicate and redelivered messages are the normal case, not the exception;
correctness must hold without relying on delivery order or exactly-once semantics.

### VI. Repository Content Is Untrusted

All repository-derived content — diffs, titles, bodies, webhook payloads, redriven
queue messages, GitHub API responses — SHALL be treated as adversarial data.
Instructions embedded in repository content MUST NOT be followed. Repository content
MUST NOT establish approval, severity, security status, policy exceptions, reviewer
identity, or authorization, and MUST NOT create control-plane state. Every SQS message
MUST be validated against the typed envelope schema before use, and GitHub responses
MUST be shape-validated before consumption; response-embedded URLs are never used for
navigation (endpoints are constructed from validated identifiers).

Rationale: anyone can open a PR against the repository, so repository text is an
injection channel by construction.

### VII. The Model Has No Tools

The LLM MUST receive no tools and MUST NOT be granted tool use, function calling, or
any execution capability. Model output is Markdown only; it MUST be structurally
validated before publication and MUST NOT be interpreted by the worker as an approval,
label, merge decision, or any other control-plane signal. The model MUST NOT emit the
canonical marker, credential-like strings, `@mentions`, external image URLs, or
approval verdicts; violations are rejected non-retryably and alerted. Every side
effect flows exclusively through worker code.

Rationale: prompt injection cannot escalate if the model can neither act nor have its
output acted upon; findings derive independently from code semantics.

## Security & Configuration Constraints

- Least privilege: exactly three IAM roles (ingress, worker, operator-redrive) with
  inline policies; SSM access uses explicit ARNs — no wildcards; the ingress role is
  restricted to the `delivery:*` partition and can never touch `review:*` records.
- Secrets baseline: five SSM parameters under `/pr-reviewer/`; the webhook secret is
  ingress-only; SecureStrings use the AWS-managed `aws/ssm` KMS key (MVP decision).
- Kill switch: setting worker reserved concurrency to 0 (or disabling the webhook)
  MUST stop all spend immediately; queued work is retained.
- Cost figures (LLM pricing, budget thresholds, alarm parameters) are configuration
  parameters, never hard-coded.
- Local Terraform state is gitignored, backed up encrypted, and single-operator —
  never two concurrent applies.
- Ingress fast-ack contract: HTTP 202 within 250 ms with an empty body; the status
  code is the entire contract.

## Development Workflow & Quality Gates

- No commit without the installed pre-commit hooks (hygiene, ruff, gitleaks).
- No `terraform apply` without a green test run, applied from a clean, reviewed
  revision.
- The acceptance criteria in HLD §7.3 (a)–(j) are automated integration tests and the
  definition of done — not manual checks.
- Any change to `prompt_version` or the model string MUST trigger a rerun of the
  pinned model evaluation set against its rubric; model behavior is a dependency.
- Every PR review MUST verify compliance with the Core Principles; complexity MUST be
  justified or removed.

## Governance

- This constitution supersedes all other practices and documents; where HLD.md and
  this document conflict on a non-negotiable, this document wins. HLD.md remains the
  authoritative source for implementation-level design detail.
- Amendments require: a documented change, an explicit version increment, an updated
  Last Amended date, and the rationale recorded in the change itself. Versioning is
  semantic: MAJOR for principle removal or incompatible redefinition, MINOR for a new
  principle or materially expanded guidance, PATCH for clarifications and non-semantic
  refinements.
- Compliance review: all PRs and code reviews MUST check changes against the Core
  Principles; a violation blocks merge until resolved or amended.
- Runtime development guidance (schemas, protocols, budgets, failure modes) lives in
  HLD.md; this document defines what MUST NOT change without an amendment.

**Version**: 1.0.0 | **Ratified**: 2026-09-12 | **Last Amended**: 2026-09-12

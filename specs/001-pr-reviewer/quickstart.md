# Quickstart: Deploy & Validate — Autonomous Serverless PR Reviewer

**Date**: 2026-09-12 | **Deploy sequence & acceptance criteria**: `docs/HLD.md` §7.3 (authoritative). This guide maps the clarified spec's acceptance scenarios onto that sequence and adds the D1/D2 validation scenarios. Acceptance criteria are automated integration tests against a deployed stack — the definition of done (constitution workflow rules).

## Prerequisites

1. AWS account eligible for the Free Tier / Always Free allowances; region `us-west-2` (HLD header).
2. Terraform applying the HLD §7.1 BOM from a clean, reviewed revision (no manual console changes — Constitution I).
3. The five SSM parameters populated per HLD §2.6 (`/pr-reviewer/github-token` fine-grained PAT with Pull requests read+write, `webhook-secret`, `glm-api-key`, `glm-model`, `glm-endpoint`). Secrets live only in SSM SecureStrings — never in state, env vars, or logs (Constitution III).
4. GitHub webhook registered on the **one** target repository, "Pull requests" events only (HLD §7.3; one-repo scope per spec).
5. Pre-commit hooks installed (hygiene, ruff, gitleaks); green test run before any `terraform apply` (Constitution workflow rules).

## Deploy

Per HLD §7.3: populate SSM → `terraform init && terraform apply` → register webhook → run acceptance scenarios below. Rollback = re-apply the previous revision (no data migrations; comment state converges).

## Validation scenarios

Scenarios (a)–(j) are the HLD's acceptance criteria, retained verbatim in intent; (k)–(n) are the spec-driven additions from this plan (D1/D2). Each maps to the spec scenarios it proves.

| # | Scenario | Expected outcome | Proves |
|---|---|---|---|
| a | Open a PR on the configured repo | Delivery log shows 202 in seconds | FR-006, SC-007; Story 1 AC-1 |
| b | Wait for first review | One canonical comment in 6–15 s describing known issues | FR-011, SC-001; Story 1 AC-1 |
| c | Push another commit | Same comment updated in place, new revision's findings | FR-001; Story 2 AC-1 |
| d | Two rapid back-to-back pushes | Exactly one comment; only the latest head SHA reflected | FR-013–FR-016; Story 2 AC-2 |
| e | Webhook with bad signature | 401; nothing enqueued | FR-007; Edge (malformed/forged) |
| f | Happy path completes | DLQ empty | FR-017/FR-018 healthy path |
| g | Redrive an artificially stale head SHA into the queue | Canonical comment **not** mutated | FR-014/FR-015; Story 3; SC-004 |
| h | Delete the bot comment, push again | Converges to exactly one new marker-bearing comment | FR-002/FR-003; Story 2 AC-3 |
| i | Concurrent workers on one PR (duplicate messages / lowered visibility) | Single comment, correct head SHA | FR-016; Story 4 AC-2 |
| j | Redrive drill: fix cause, `StartMessageMoveTask` from DLQ | DLQ drains; comments converge, no duplicates | FR-018; Story 5 AC-4 |
| **k** | Close a PR, push commits while closed (or not), reopen it | Reopen event acknowledged; review produced for the current head | **D1**; FR-008; Story 1 AC-3 |
| **k2** | Apply a label / open an issue on the same repo | 200 discard; no review activity | FR-008 (non-triggers); Story 1 AC-4 |
| **l** | Force a permanent-class failure (e.g., persistent assembled-content validation failure, or LLM 401 after re-fetch) | Alert + non-retryable completion; canonical comment shows the fixed failure notice with the head SHA within SC-006's final-attempt budget; no internal error details in the comment; log records `failure_notice_published=true` | **D2**; FR-019/FR-028; Story 5 AC-5/AC-2; SC-006 |
| **l2** | Force a sustained transient provider outage through all 5 queue attempts | DLQ entry + alert; failure notice published during the final attempt (bounded by §2.2 queue timing — up to ~48 min from first enqueue); log `failure_notice_published=true` | **D2**; FR-017/FR-018; FR-028 |
| **m** | While a failure notice would publish, push a newer commit (notice's revision now stale) | Notice **not** published (`skipped-stale`); comment untouched | **D2**; FR-014/FR-028 fence |
| **n** | After scenario (l) or (l2), resolve the cause and push a new commit | Comment reverts to normal review content; exactly one canonical comment throughout | **D2**; FR-004/FR-028; Story 5 AC-5 |

## Negative & hygiene spot-checks (map to §4.4 unit/contract layers)

- Prompt-injection PR text → findings unaffected; no injected behavior in the comment (Story 6; SC-003/SC-005).
- Replay a captured delivery after the 7-day dedup TTL → at most one bounded redundant review; still one comment (Story 4 AC-3; §5.2 replay residual).
- Log/sample audit: no secrets, no raw payloads anywhere (SC-005; Constitution III).
- Review-content validation: model output missing sections, oversized, or containing prohibited content is never published (FR-020/FR-025).

## Where things live

- Contracts: [contracts/ingress-webhook.md](./contracts/ingress-webhook.md), [contracts/canonical-comment.md](./contracts/canonical-comment.md)
- Data model & state machine: [data-model.md](./data-model.md) (HLD §3)
- Failure-mode map: HLD §6 (25 modes) — scenarios above exercise modes 1–13, 17–21, 24
- Kill switch: set worker reserved concurrency to 0 → spend stops immediately, queued work retained (HLD §4.3)

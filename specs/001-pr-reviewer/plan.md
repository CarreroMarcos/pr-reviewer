# Implementation Plan: Autonomous Serverless PR Reviewer

**Branch**: `001-pr-reviewer` | **Date**: 2026-09-12 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/001-pr-reviewer/spec.md` (clarified 2026-09-12: `reopened` trigger, comment-only write boundary, one-repo scope, FR-028 failure state, latency budgets confirmed).

**Authoritative technical design**: `docs/HLD.md` (v6.7). This plan does **not** restate, replace, or reinterpret the HLD architecture. It (1) maps the clarified spec onto the HLD, (2) records the spec-driven deltas the HLD does not yet contain, and (3) adds the Spec Kit artifacts the HLD lacks: test-strategy mapping and consolidated interface contracts.

## Summary

Event-driven, fully serverless GitHub PR reviewer that converges to exactly one canonical review comment per PR (spec FR-001–FR-005). Technical approach is the HLD as written: ingress Lambda → SQS work queue → worker Lambda → DynamoDB state + fenced GitHub comment publication (establish → review → claim → live-head fence → publish → conditional finalize, marker-based reconciliation). This plan adds exactly two behavioral deltas required by the clarified spec — the `reopened` trigger (D1) and the FR-028 failure-state-in-comment (D2) — plus their contracts and test mapping (D3).

## Technical Context

Every field resolves from `docs/HLD.md` and `.specify/memory/constitution.md`. No NEEDS CLARIFICATION remains.

**Language/Version**: Python 3.12; runtime dependencies limited to the standard library + `boto3` (Constitution II; HLD §1.4)

**Primary Dependencies**: Runtime: `boto3` only (any third-party runtime dep requires a constitution amendment). Dev group only: `pytest` as the test runner named by this plan (HLD §4.4 requires automated tests and a dev dependency group; the runner name is a plan-level choice), plus the existing pre-commit toolchain (hygiene, ruff, gitleaks — Constitution workflow rules).

**Storage**: DynamoDB table `pr-reviewer-state`, partition key `pk`, PROVISIONED 25 WCU / 25 RCU (Always Free envelope; HLD §2.4). No other state.

**Testing**: pytest per HLD §4.4's four layers — unit (pure logic), state-machine interleavings (in-memory DynamoDB stub), contracts (signed fixtures, in-process HTTP stubs, injected clients/clocks), and the pinned model-evaluation set (rerun on `prompt_version` or model change). HLD §7.3 (a)–(j) acceptance criteria are automated integration tests against a deployed stack and are the definition of done (Constitution workflow rules).

**Target Platform**: AWS `us-west-2`, fully serverless (Lambda ×2, SQS ×2, DynamoDB, SSM, CloudWatch — HLD §7.1 BOM). No servers, containers, or always-on compute (Constitution I).

**Project Type**: Two Lambda handlers (`ingress_handler`, `worker_handler`) + shared `lambda/common/` contract code + Terraform (HLD §7.1).

**Performance Goals**: Ingress acknowledgment < 250 ms (HLD contract; spec FR-006/SC-007 accept 1 s as the platform-agnostic ceiling), first comment 6–15 s typical (spec SC-001: 15 s / 95%), worker hard cap 120 s (= spec FR-011 "2-minute hard cap").

**Constraints**: $0 Free-Tier cost baseline; secrets never in Terraform state, env vars, or logs (Constitution III); comment-only write boundary (FR-027); exactly one target repository per deployment (spec, confirmed); Python stdlib-only runtime.

**Scale/Scope**: One repository per deployment; worker reserved concurrency 5; DynamoDB burst ≤ 20 WCU/s (HLD §2.4); single operator.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How this plan satisfies it |
|---|---|---|
| I. Fully Serverless Terraform | ✅ Pass | Plan adds no infrastructure. D2 uses existing components only (HLD §7.1 BOM unchanged). |
| II. Python 3.12 stdlib + boto3 | ✅ Pass | No runtime deps added. Failure-state content is worker-assembled text (stdlib). pytest named for the dev group only, never imported by handlers. |
| III. Secrets never in state/logs | ✅ Pass | D2's failure-state content contract prohibits internal error details, provider responses, and credential-like strings (extends §5.4 hygiene to comment content; FR-028, FR-026). |
| IV. One canonical PR comment | ✅ Pass | D2 publishes the failure state **into** the canonical comment (marker preserved, fenced path §3.3); it never creates a second comment. Reconciliation (§3.4) unchanged. |
| V. At-least-once, idempotent consumers | ✅ Pass | Failure-state publication is idempotent: same fenced conditional transitions; re-delivery converges (PATCH-upsert semantics + §3.3 finalize). |
| VI. Repository content is untrusted | ✅ Pass | Failure content is system-generated, never model output or repository-derived text. Envelope/`reopened` handling unchanged from §2.1/§6. |
| VII. The model has no tools | ✅ Pass | Unchanged. Model I/O contract (§2.7) untouched; failure content bypasses the model entirely. |

**Post-Phase-1 re-check**: ✅ Pass — design artifacts (research.md, data-model.md, contracts/, quickstart.md) introduce no components, permissions, or state fields beyond the HLD BOM; D1/D2 are configuration-, content-, and test-level deltas on existing mechanisms.

## Project Structure

### Documentation (this feature)

```text
specs/001-pr-reviewer/
├── plan.md              # This file
├── research.md          # Phase 0: decisions & spec-driven deltas (all from HLD/constitution)
├── data-model.md        # Phase 1: entities mapped to HLD §2.4/§3.1/§3.2
├── quickstart.md        # Phase 1: deploy + validation guide (HLD §7.3 + additions)
├── contracts/
│   ├── ingress-webhook.md    # GitHub → ingress HTTP + SQS envelope contract
│   └── canonical-comment.md  # Worker → GitHub comment contract incl. FR-028 failure state
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

Layout is the HLD's (§7.1 "Repository layout & packaging"); restated here only to anchor the plan:

```text
lambda/
├── ingress_handler.py   # thin entry point
├── worker_handler.py    # thin entry point
└── common/              # single source of truth: envelope schema/validator,
                         # marker builder, structured-log helpers (§7.1)
terraform/               # HLD §7.1 BOM (λ×2, SQS×2, DynamoDB, IAM×3, alarms)
prompts/                 # versioned system prompt (prompt_version id; HLD §2.7)
docs/                    # operational runbooks (DLQ redrive, kill switch)
tests/                   # §4.4 layers: unit / state-machine / contracts /
                         # model-evals / integration
```

**Structure Decision**: Single Terraform-managed AWS project with two Lambda handlers and one shared `lambda/common/` package, exactly per HLD §7.1 (Rule of Three: no further layering).

## Spec-Driven Deltas to the HLD

The clarified spec requires exactly the following changes; everything else is HLD-as-written. These are the only places this plan may deviate from `docs/HLD.md`.

| ID | Delta | Spec source | HLD touchpoint |
|---|---|---|---|
| **D1** | Add `reopened` to the ingress action allow-list and the envelope `action` enum. Drafts still skipped. No state-machine change: a `reopened` event flows through the existing establish paths (§3.3) like any other trigger. | FR-008, Story 1 AC-3 (clarified 2026-09-12) | §2.1 step 4, §2.1 envelope table |
| **D2** | FR-028 failure state: when a review permanently fails or exhausts its bounded queue attempts, the worker publishes a system-generated failure notice **as the canonical comment's content** through the existing fenced protocol (claim → fence → publish → finalize), subject to the same revision-currency check. Content contract and decision-table mapping in `contracts/canonical-comment.md`. Best-effort: if the failure-state publication itself fails, the alert and DLQ path proceed unaffected. | FR-019, FR-020, FR-028, Story 5 (narrative, AC-5), SC-006 | §2.3 item 8 (rows extended), §3.3, §3.4, §2.7/§2.8 (content assembly) |
| **D3** | Test-strategy mapping additions: spec ACs ↔ HLD §4.4 layers / §7.3 acceptance criteria, plus new acceptance scenarios (k)–(n) for D1/D2. | Stories 1–6, SC-001–SC-008 | §4.4, §7.3 |

## Complexity Tracking

> No constitution violations to justify — table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| — | — | — |

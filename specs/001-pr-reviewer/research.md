# Phase 0 Research: Autonomous Serverless PR Reviewer

**Date**: 2026-09-12 | **Spec**: [spec.md](./spec.md) | **Authoritative design**: `docs/HLD.md` v6.7

All Technical Context fields resolve from the HLD and the ratified constitution — **zero NEEDS CLARIFICATION items**. This document records the decisions, why they are not new architecture, and the alternatives rejected. The only substantive decisions are the two spec-driven deltas (D1, D2); everything else cites the HLD.

## R1. Runtime & dependencies

- **Decision**: Python 3.12; runtime = stdlib + `boto3` only (HLD §1.4, Constitution II).
- **Rationale**: Ratified constitution non-negotiable; HLD packaging (`lambda/common/`, thin handlers, `archive_file`) already assumes it.
- **Alternatives considered**: any framework/SDK (LangChain, requests, GH client libs) — rejected: requires constitution amendment; supply-chain and cold-start cost for zero need.

## R2. Test runner & tooling placement

- **Decision**: `pytest` as the named runner, dev dependency group only (HLD §4.4; Constitution II permits dev tooling but forbids importing it from deployed handlers).
- **Rationale**: HLD requires four automated test layers and pre-commit/CI gates but does not name a runner; pytest is the stdlib-compatible default already implied by the repo's ruff/pre-commit toolchain. This names existing practice; it adds no runtime dependency.
- **Alternatives considered**: stdlib `unittest` alone — rejected: HLD's fixture/injection style (§4.4 item 3) is materially harder to express; adding a test framework is not an architecture change.

## R3. State & storage

- **Decision**: DynamoDB `pr-reviewer-state`, `pk` partition key, PROVISIONED 25/25, two item types (`delivery:{guid}` TTL 7 d; `review:{repo}#{pr}` per §3.1) — HLD §2.4 as written.
- **Rationale**: Constitution I mandates PROVISIONED within the Always Free envelope; capacity budget already derived (≤ 20 WCU/s burst).
- **Alternatives considered**: on-demand billing — rejected by Constitution I; new fields for FR-028 — rejected (see R6).

## R4. D1 — `reopened` as a review trigger

- **Decision**: Ingress action allow-list and envelope `action` enum gain `reopened` (`opened | synchronize | ready_for_review | reopened`). Drafts remain skipped. Non-trigger actions remain HTTP-200 discards.
- **Rationale**: Spec FR-008 / Story 1 AC-3 (clarified): a reopened PR can carry commits pushed while closed; without this trigger its canonical comment can stay stale forever. No state-machine change is needed: the event carries the current head and flows through the existing establish conditions (§3.3 a/b/c). A reopened PR whose head was already reviewed re-reviews once — the same bounded redundancy the spec already accepts for same-head replays (Assumptions; §5.2 replay residual).
- **Alternatives considered**: manual re-review via label/command — rejected by the user in clarification (not selected); treating reopen as a no-op — rejected: leaves the stale-comment hole the spec closes.

## R5. Repository scope

- **Decision**: Exactly one target repository per deployment; no repository allow-list check is added at ingress (HLD as written).
- **Rationale**: Scope is enforced by deployment shape: one webhook registration on the one configured repo; HMAC (full-string, §2.1) authenticates that only GitHub holding the webhook secret can produce accepted events. Envelope `repo_full_name` keys the state records, so a future multi-repo scale-up stays configuration-level, exactly as the spec assumption states.
- **Alternatives considered**: ingress-side configured-repo comparison — rejected for MVP: adds a config surface the clarified spec does not require; HMAC + single registration already bind the scope. Revisit only if multi-repo lands.

## R6. D2 — FR-028 failure-state-in-comment

- **Decision**: On permanent failure the worker publishes a **system-generated failure notice as the canonical comment's content**, through the existing fenced protocol (§3.3: claim → live-head fence → publish → finalize), subject to the same revision-currency check as review content. Content contract, trigger mapping (which HLD §2.3 item 8 rows publish vs. not), idempotency, and the template live in [contracts/canonical-comment.md](./contracts/canonical-comment.md).
- **Rationale**: The clarified spec (FR-019/FR-028, Story 5, SC-006) requires the PR itself to reflect the failure so maintainers are not misled by stale/missing reviews — the HLD's current behavior (log + alert + DLQ only) no longer satisfies it. Reusing the fenced publication path means: no new components, no new permissions (worker already PATCH/POSTs comments), no new stored state (the notice is comment content; state fields of §3.1 are unchanged), and the same convergence guarantees (IV/V) apply.
- **Alternatives considered**:
  - Commit status / check run — rejected: violates the comment-only write boundary the user confirmed (FR-027).
  - Second, separate failure comment — rejected: violates FR-001 single-comment invariant.
  - New DynamoDB field (e.g., `last_error_class` surfaced later) — rejected: adds state without adding a guarantee; FR-028's requirements are met by comment content alone, and error detail belongs in logs/alerts (Constitution III), not in a public comment.
  - Failure notice only after operator redrive — rejected: spec SC-006 sets a 5-minute surfacing budget independent of operator action.

## R7. Latency budgets

- **Decision**: Map spec numbers onto the HLD's existing budgets: ack < 250 ms implementation contract inside the spec's 1 s platform-agnostic ceiling (FR-006/SC-007); 6–15 s typical review inside SC-001's 15 s / 95%; 120 s worker timeout = FR-011's 2-minute hard cap; overruns remain retryable (§2.3 item 8).
- **Rationale**: User confirmed the spec defaults; the two documents are consistent, with the constitution's 250 ms contract as the tighter internal bound.
- **Alternatives considered**: tightening the spec AC to 250 ms — rejected by the user in clarification (Option A: keep spec ceilings as written).

## R8. Observability for the deltas

- **Decision**: D2 adds one log field to the existing structured review-log set: `failure_notice_published` ∈ {true, false, skipped-stale} (IDs/status only — Constitution III/HLD §5.4 unchanged). No new alarms; DLQ-depth alarm (§4.3) remains the primary failure signal.
- **Rationale**: Operators must be able to verify FR-028 behavior from logs without new infrastructure; statuses are bounded and secret-free.
- **Alternatives considered**: dedicated metric + alarm per failure notice — rejected: DLQ alarm already fires on the same condition; duplicate paging is noise.

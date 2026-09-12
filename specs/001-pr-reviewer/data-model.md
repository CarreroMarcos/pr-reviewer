# Phase 1 Data Model: Autonomous Serverless PR Reviewer

**Date**: 2026-09-12 | **Authority**: `docs/HLD.md` §2.4, §3.1–§3.4 (field contracts and state machine are restated there in full; this file maps them to the spec and lists only spec-driven additions). Where this file and the HLD disagree on anything not marked as a spec delta ([D1]/[D2]), the HLD wins.

## Entities (spec Key Entities ↔ HLD)

### 1. Review Request (delivery)

- **HLD**: DynamoDB item `pk = delivery:{guid}`, TTL 7 days (§2.4 item type 1); produced by ingress after HMAC + event/action gating (§2.1 steps 1–7).
- **Fields**: per HLD §2.1 envelope schema (typed, < 1 KB): `envelope_version`, `event_type`, `action`, `repo_full_name`, `pr_number`, `head_sha`, `base_sha`, `sender`, `delivery_guid`.
- **Spec deltas (D1)**: `action` enum extends to include `reopened`. No other field changes.
- **Validation rules**: envelope schema constraints (§2.1 table) enforced worker-side on every SQS message (Constitution VI); malformed → completed non-retryable (§2.3 item 8).
- **Uniqueness**: `delivery_guid` (GitHub `X-GitHub-Delivery`) — dedup at ingress GetItem (§2.1 step 5), spec FR-012.

### 2. Review State (per PR)

- **HLD**: DynamoDB item `pk = review:{repo_full_name}#{pr_number}`; full field contract §3.1 — `status ∈ {CLAIMED, ACTIVE}` (`ABSENT` by absence, `STALE` derived), `comment_id` (GitHub int64, ACTIVE only), monotone `generation`, `head_sha`/`last_seen_sha` (40-hex), `claim_owner`, `claim_until` (epoch), `updated_at` (ISO-8601 UTC).
- **Spec deltas**: **none** — FR-028 adds no stored fields (R6); the failure state is comment content, not state.
- **State transitions**: exactly §3.2: `ABSENT →(establish)→ CLAIMED →(POST)→ ACTIVE`; next revision `ACTIVE →(establish, generation+1)→ CLAIMED → ACTIVE`; expired-lease `CLAIMED` is derived-stale and re-claimable. Lease 180 s fixed, claim→finalize, no renewal.
- **Write discipline**: capacity accounting §2.4 unchanged (≈4 WCU per new-revision review).

### 3. Canonical Review Comment

- **HLD**: single GitHub Issues-API conversation comment per repo/PR bearing the worker-injected marker `<!-- pr-reviewer:canonical:v1:{repo_full_name}#{pr_number} -->` (§2.8); identity survives total state loss; external tooling must not parse the marker; reconciliation per §3.4 (full pagination, exact-marker match, lowest-comment-ID wins, creation lease).
- **Content forms** (worker-assembled; the model never emits the marker):
  1. **Review content**: Model I/O contract shape (§2.7) — `## Summary`, `## Findings` (bounded count, severity enum, `path:LINE`), `## Risk Notes`; structurally validated (§2.3 item 7) before publication; bound to the reviewed revision (FR-013).
  2. **Failure-state content (spec delta D2 — new)**: see [contracts/canonical-comment.md](./contracts/canonical-comment.md) for the binding contract. Summary: fixed, system-generated template; the only variable content is the reviewed head SHA; carries the same marker; published only through the §3.3 fenced path (FR-028's revision-currency requirement); contains no internal error details, provider names/responses, or credential-like strings (FR-028, FR-026, Constitution III); replaced by normal review content on the next successful review (existing content-regeneration flow, FR-004).
- **Lifecycle**: created once per PR (creation lease, §3.4), then updated in place forever (FR-001/FR-002); deletion/migration handled by the PATCH-404 decision table (§2.3 item 8) + reconciliation.

### 4. PR Revision

- **HLD**: the PR's head commit at a moment in time; the **live GitHub PR head** (via `GET /repos/{repo}/pulls/{n}`, shape-validated before use, §2.3 item 2) is the sole authority for "current" (§3.2). SHAs are never orderable (spec FR-015 = §3.2 comparison rule, verbatim).
- **Relationships**: a delivery refers to one revision; a published comment content (review or failure) corresponds to exactly one revision (FR-013, FR-028).

## Validation rules traceability (spec → model)

| Spec rule | Enforced by |
|---|---|
| FR-008 trigger filter incl. `reopened`, drafts skipped | Ingress action gate (§2.1 step 4) — **D1 delta** |
| FR-012 delivery dedup | `delivery:{guid}` GetItem/PutItem + 7-day TTL (§2.1 steps 5–7) |
| FR-013/FR-014/FR-015 revision binding & fencing | Establish/claim/fence/finalize conditionals (§3.3) |
| FR-016 exclusive time-bounded publication | 180 s claim lease (§3.2, §3.3 step 3) |
| FR-020/FR-025 output gating | Worker structural validation (§2.3 item 7) — extends to failure content per D2 |
| FR-026 log/output hygiene | §5.4 + failure-content prohibition list (contract) |
| FR-027 write boundary | Comment-only API surface; contract test asserts no other GitHub write calls |

## Data volume / scale

Single repo, worker concurrency 5, burst ≤ 20 WCU/s, ≤ 1 KB items, 7-day delivery TTL — HLD §2.4/§4.1 as written; no change from D1/D2 (the failure path reuses existing writes; a failure notice adds at most the standard publish/finalize writes for one revision).

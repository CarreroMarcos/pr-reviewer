# Contract: Canonical Comment (review content & FR-028 failure state)

**Authority**: `docs/HLD.md` §2.3 item 7–8, §2.7, §2.8, §3.3–§3.4, §5.3–§5.4. This file consolidates the worker→GitHub comment interface for Spec Kit purposes and adds the spec-driven FR-028 failure-state contract (delta D2). Where this file and the HLD disagree on anything not marked **[D2]**, the HLD wins.

## Identity

- Exactly **one** canonical comment per repo/PR (FR-001), located by the worker-injected marker:
  `<!-- pr-reviewer:canonical:v1:{repo_full_name}#{pr_number} -->` (§2.8).
- The marker is assembled by worker code only; the model never emits it (FR-005; Constitution IV). External tooling must not parse it.
- Reconciliation on duplicates/loss: §3.4 (full pagination, exact-marker match, lowest comment ID wins, creation lease before first POST). FR-003.

## Content form 1 — Review content (HLD as written)

- Assembled from model output conforming to the Model I/O contract (§2.7): `## Summary`, bounded `## Findings` (severity ∈ {HIGH, MEDIUM, LOW}, `path:LINE`, issue, suggested fix), `## Risk Notes`; empty-finding sentinel: "No significant issues found."
- Structural validation before publication (§2.3 item 7): marker present, bounded length, expected sections, no credential-like strings, no hidden HTML/script, no control-plane directives, prompt canary absent, no `@mentions`/external image URLs/approval verdicts (FR-020, FR-025).
- Content always corresponds to the reviewed revision (FR-013) and is fully regenerated each cycle (FR-004).

## Content form 2 — Failure-state notice **[D2 — new, FR-028]**

**Purpose**: when a review for the current head permanently fails, the PR must not silently show a stale or missing review (Story 5; SC-006).

**Content contract**:

- **Fixed template, system-generated** — never model output, never repository-derived text. The only variable is the reviewed head SHA:
  `Automated review could not be completed for revision {head_sha}. The failure has been logged for the operator; no findings are available for this revision.`
  Preceded by the canonical marker exactly as with review content.
- **Prohibited**: internal error details, provider/LLM names, HTTP statuses, stack traces, credential-like strings, configuration values, `@mentions`, external media, approval or merge-safety verdicts, and any instruction-following text (FR-028; Constitution III/VI; §5.4 hygiene applied to comment output). The full error class remains in structured logs and the operator alert only.
- **Publication path**: the existing fenced protocol (§3.3) — claim → live-head fence → PATCH (or creation-lease POST if no canonical comment exists) → conditional finalize. Subject to the same revision-currency check as review content: a failure notice for a stale revision is **not** published (FR-028; skipped as `skipped-stale`).
- **Reversion**: the next successful review of the live head replaces the notice with normal review content through the ordinary publish path — no special-case code beyond the standard content regeneration (FR-004).
- **Idempotency**: re-delivery re-attempts the identical PATCH (convergent); PATCH-404 follows the §2.3 item 8 decision table (migrate/adopt/create); finalize conditions prevent clobbering a newer accepted revision (Constitution V).

**Trigger mapping — which §2.3 item 8 rows publish a failure notice [D2]**:

| Condition (HLD §2.3 item 8) | Failure notice? |
|---|---|
| Transient provider/LLM/throttle/5xx errors at the **final** queue attempt (`int(Attributes["ApproximateReceiveCount"]) >= maxReceiveCount` (5); tolerate `> 5` after redrive) | **Yes** — published during that final attempt (then raise, so DLQ/alert/redrive proceed unchanged) |
| LLM-side unusable output (timeout / 429 / 5xx / structurally invalid) — HLD §2.3 item 8 raises for queue retry, §2.2 owns the bounded budget | **Yes** at the final attempt — the invalid/unusable content itself is never published, only the fixed template |
| Assembled review content fails structural validation (permanent) | **Yes** — completes non-retryable immediately; template only |
| 401 from the **LLM** after the single credential re-fetch (permanent) | **Yes** — GitHub is reachable; publish template |
| 401 from **GitHub** after the single credential re-fetch (permanent) | **No** — comment writes would 401 too; alert only (same disposition as lost access) |
| 403/404 on comment list/GET (token lost access) | **No** — publication is impossible; alert only |
| Envelope schema-invalid / malformed message | **No** — identifiers untrusted; complete non-retryable |
| Superseded / stale / duplicate / filtered deliveries | **No** — no failure occurred |

**Retry split (spec FR-019 ↔ HLD §2.3 item 8)**: LLM-side invalid/unusable output retries through the queue's bounded budget (that budget *is* the spec's "bounded attempt count"); once exhausted, the condition is permanent — no further retries. Assembly-validation failure is non-retryable immediately. Both publish the template only, never the invalid content.

**Timing semantics (SC-006)**: the spec's 5-minute budget anchors to the review's **final attempt**, not to the first failure. Permanent-class conditions (assembly-validation failure, LLM 401) finalize immediately, so the notice lands within the budget. Transient-class exhaustion publishes during the queue's final attempt, which under §2.2 (720 s visibility × maxReceiveCount 5) may occur ~48 minutes after first enqueue — that delay is the HLD's retry-ownership design, unchanged by D2. Conditions where publication is impossible (GitHub auth / lost access) defer comment reflection until access is restored via redrive; operator surfacing (DLQ + alarm) is unaffected throughout.

- **Best-effort rule**: if publication of the notice itself fails, the worker does not retry it beyond the normal raise path — the DLQ + alert + redrive flow (§2.5, FR-018) is never masked by notice failures.
- **Observability**: log field `failure_notice_published ∈ {true, false, skipped-stale}` (IDs/status only; research R8).

## Invariants

- After any failure, recovery, or failure-notice publication, the PR still converges to exactly one canonical comment (FR-001/FR-003; SC-002).
- The system performs no platform write other than maintaining that single comment (FR-027; Story 6 AC-2).
- No secrets, no internal error details, and no raw untrusted content ever appear in comment content (FR-026, FR-028; Constitution III).

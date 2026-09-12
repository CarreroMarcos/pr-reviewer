# Contract: GitHub Webhook → Ingress (and SQS Envelope)

**Authority**: `docs/HLD.md` §2.1 (the complete, canonical response table and envelope schema live there). This file consolidates the interface for Spec Kit purposes and records the single spec-driven delta (D1). If this file and the HLD ever disagree on anything not marked **[D1]**, the HLD wins.

## Interface: HTTP endpoint (Lambda Function URL)

- **Caller**: GitHub webhook dispatcher (Pull requests events only). Any other caller is hostile by construction (CORS disabled; §5.2).
- **Method**: `POST`. **Body**: JSON, ≤ 1 MiB (413 before decode).
- **Authentication**: HMAC-SHA256 over raw decoded bytes, full-string constant-time compare against `X-Hub-Signature-256` (`sha256=` prefix). Failure → 401, nothing enqueued.
- **Required headers**: `X-Hub-Signature-256`, `X-GitHub-Event` (= `pull_request`), `X-GitHub-Delivery` (GUID).
- **Acknowledgment**: empty body; status code is the entire contract; 202 within 250 ms (spec FR-006 accepts 1 s as platform-agnostic ceiling; FR-010 forbids provider-dependent ack latency).

### Response contract (summary — full table: HLD §2.1)

| Condition | Status | State effect |
|---|---|---|
| Valid, allowed action, new GUID, enqueued | 202 | delivery marked processed |
| Signed non-`pull_request` event / non-trigger action / draft PR | 200 | discarded, nothing enqueued |
| GUID already processed | 200 | idempotent no-op |
| HMAC failure / missing-malformed headers | 401 | nothing enqueued |
| Body > 1 MiB | 413 | rejected pre-decode |
| Valid signature, unparseable/schema-invalid body | 200 | discarded — permanent, no pointless redelivery |
| SQS `SendMessage` failure | 500 | delivery **not** marked (GitHub redelivery recovers, 3-day window) |
| Admission ceiling exceeded | 429 | nothing enqueued; documented loss boundary (§2.1/§5.2) |

## Contract: SQS envelope (typed schema, < 1 KB)

Worker-side validation against this schema is mandatory on every message (Constitution VI); violations are completed non-retryable.

| Field | Type | Constraint |
|---|---|---|
| `envelope_version` | string | constant `v1` — additive evolution only |
| `event_type` | string | constant `pull_request` |
| `action` | enum | `opened` \| `synchronize` \| `ready_for_review` \| **`reopened` [D1]** |
| `repo_full_name` | string | `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$`, ≤ 128 chars |
| `pr_number` | integer | > 0, ≤ 10⁹ |
| `head_sha`, `base_sha` | string | 40-char lowercase hex |
| `sender` | string | GitHub login charset, ≤ 64 chars |
| `delivery_guid` | string | UUID, ≤ 64 chars |

**[D1] Semantics of `reopened`**: passes the ingress filter like `opened`; flows through the standard worker pipeline; establish conditions (§3.3) decide currency. Drafts are still never reviewed (draft state is checked at the action gate exactly as for the other triggers).

## Invariants

- Acknowledgment never depends on review/publication latency (FR-006, FR-010; HLD §1.4).
- An accepted event is durable before acknowledgment (dispatch-before-mark, failure mode 4; FR-009).
- Exactly one target repository per deployment: scope is bound by the single webhook registration + HMAC; the envelope records `repo_full_name` for state keying (research R5).
- Nothing repository-derived is ever logged raw (§5.4).

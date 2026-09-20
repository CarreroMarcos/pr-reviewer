# Notes: HLD Interview Talking Points

**Status:** Non-normative, parked 2026-09-20 from HLD §8 (HLD restructure PR-4). Reference notes only — doc authority: see `docs/HLD.md` (header).

- **Distributed-systems correctness:** SHA-authoritative fencing with a **defined** comparison function (live-head confirmation, never SHA ordering), conditional publication, and an honest convergence invariant.
- **AWS configuration discipline:** the 6× visibility rule, decoupled fixed leases (claim→finalize, no renewal), capacity derivations from item sizes, admission-boundary loss analysis, concrete API version pinning.
- **Security engineering:** full-string HMAC, event-type gating, three-role IAM including the operator-redrive permission chain, prompt-injection threat model with control-plane separation.
- **Operational resilience:** classified error handling with an actionable 404 decision table, Retry-After-aware visibility extension, marker-based reconciliation, tested DLQ redrive.
- **Cost engineering:** per-service scoped claims, capacity proofs, configuration-driven pricing assumptions.

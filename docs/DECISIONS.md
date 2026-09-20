# Decision Log — Autonomous Serverless PR Reviewer

**Status:** Non-normative. This file records **why** the system became what it is: dated rulings, supersedes, gate outcomes, measurements, and rejected alternatives. It never defines current behavior — entries are non-binding on behavior, and the "Current truth" pointer is their only bridge to normative content.

**Authority rule (binds all docs in this repo):** on **current behavior**, `docs/HLD.md` wins; on **why/history**, this file wins; on **redrive procedure**, `docs/runbook-redrive.md` wins; on **task scope**, `specs/**` wins. Any disagreement between documents is a Needs-input question (AGENTS.md), never a silent edit.

**Append-only discipline:** entries are never edited in place — a superseded entry is superseded by a new dated entry that names it. Entries exist only for behavior/config/accepted-risk changes and measurements; typo and wording fixes are silent. Every entry ends with a "Current truth" pointer into the HLD so a reader can jump from history to the normative statement. HLD § numbering is frozen — sections are added, never renamed or renumbered (specs and this log cite §X.Y) — so pointers stay stable; an entry written against an older HLD notes the version current at entry time when the section's content has since moved.

**Fidelity convention:** the 2026-09-11/09-12 planning-revision entries are verbatim migrations from HLD §7.2 (performed 2026-09-20, restructure PR #71); entries created during or after that migration may be enriched summaries (references expanded) — the 2026-09-20 v6.9 → v6.10 entry is the enriched one. Neither is authoritative over the HLD.

## Entry format

```text
## YYYY-MM-DD — <Title>

**Context:** <what forced the question>
**Decision:** <what was decided, by whom (ruling/gate)>
**Consequences:** <what it costs or bounds>
**Affected HLD:** §X.Y

Current truth: HLD §X.Y.
```

## 2026-09-11 — v6.1 → v6.2 — interface-contract clarifications only, zero architectural change (landed in the initial scaffold dbea837; authored pre-repository)

**Context:** planning-revision record, migrated verbatim from HLD §7.2.
**Decision:** canonical ingress response-contract table (§2.1); typed envelope schema with worker-side boundary validation (§2.1); `GET` response-shape validation before live-head fence (§2.3); state-record field contract incl. `ABSENT`-by-absence rule (§3.1).

Current truth: HLD §2.1, §2.3, §3.1.

## 2026-09-11 — v6.2 → v6.3 — security hardening only, zero architectural change (185e13b)

**Context:** planning-revision record, migrated verbatim from HLD §7.2.
**Decision:** worker SSM scope narrowed from `/pr-reviewer/*` to three explicit parameters — closes webhook-secret over-exposure contradicting §2.6 (§5.1); 1 MiB request-body cap with 413, enforced pre-decode (§2.1); Function URL CORS explicitly disabled (§5.2); CI dependency audit added to roadmap (§7.4).

Current truth: HLD §5.1, §2.1, §5.2, §7.4.

## 2026-09-11 — v6.3 → v6.4 — senior-practice completions, zero architectural change (dc34b4f)

**Context:** planning-revision record, migrated verbatim from HLD §7.2.
**Decision:** lease/heartbeat inconsistency resolved — lease spans claim→finalize only; review is side-effect-free and lease-free (§3.2); Model I/O contract defined and canonical marker moved to worker-injection (§2.7, §2.3 item 7, §2.8); ingress secret hydration with 30-min refresh (§2.1); repository layout & packaging with single-source shared contract (§7.1); testing & verification strategy with acceptance-mapping and pre-apply gates (§4.4); PR-flood cost-abuse documented (§5.2, failure mode 25).

Current truth: HLD §3.2, §2.7, §2.8, §2.1, §7.1, §4.4, §5.2.

## 2026-09-11 — v6.4 → v6.5 — principles-consistency pass (bc2680a)

**Context:** planning-revision record, migrated verbatim from HLD §7.2.
**Decision:** worker credential cache restated as warm-container state behind an injectable accessor (§2.3 item 1 ↔ §4.4); §4.4 gate wording tightened.

Current truth: HLD §2.3 item 1, §4.4.

## 2026-09-12 — v6.5 → v6.6 — three-oracle reconciliation: consistency, contracts, security operations (zero architectural change) (43861d3)

**Context:** planning-revision record, migrated verbatim from HLD §7.2.
**Decision:** DynamoDB write accounting corrected to ≈4 WCU per new-revision review, burst ≤ 20 WCU/s (§2.4, §4.1); lease "renewable" remnants removed everywhere (§3.2, failure mode 8, §7.2); `STALE` made derived-never-stored with the ACTIVE→CLAIMED transition made explicit (§3.1, §3.2); finalize and creation-lease conditions stated explicitly (§3.3, §3.4); reconciliation requires full pagination + exact-marker match (§3.4); envelope tightened with `envelope_version` and length bounds (§2.1); ingress rows added for signed-but-invalid bodies (§2.1); LLM error contract, output-validation disposition, prompt canary, and output prohibitions (§2.3 item 8, §2.7); `/pr-reviewer/glm-endpoint` parameter added — worker ARN list now four (§2.3 item 5, §2.6, §5.1); `ChangeMessageVisibility` added to worker role + trust policies + LeadingKeys restriction (§5.1); KMS key decision named, rotation and compromise runbooks (§2.6); replay residual documented (§5.2); DLQ redrive runbook + acceptance criterion (j) (§2.5, §7.3); alarms/paging + budget kill switch (§4.3); local-state guardrails (§7.1); marker declared non-public (§2.8); all dotted §2.3.x references normalized to §2.3 item N.

Current truth: HLD §2–§7 as itemized.

## 2026-09-12 — v6.6 → v6.7 — engineering-principles alignment: §9 agentic evaluations, §3 single-implementation scope, §5 retry ownership (zero architectural change) (bbd1233)

**Context:** planning-revision record, migrated verbatim from HLD §7.2.
**Decision:** pinned model-evaluation set with rerun-on-`prompt_version`/model-change rule added to the test strategy (§4.4 item 4); retry ownership stated — queue owns retries, single in-request 401 re-fetch (§2.2); time-based recovery paths (credential TTL, rotation convergence) exercised via injected clocks (§4.4 item 3); `lambda/common/` scope strengthened — security-sensitive and must-stay-identical helpers single-implemented, typed envelope adapter + handler annotations (§7.1); deployment provenance + rollback sentence, and SSM parameter count corrected four → five (§7.3).

Current truth: HLD §4.4, §2.2, §7.1, §7.3.

## 2026-09-14 — SPR-60 operator rulings: operator-role principal pinned; ingress 128 MB re-trim rejected

**Context:** the operator trust needed a named principal (account root was interim), and a 128 MB ingress memory re-trim was proposed.
**Decision (Mars ruling):** (a) the operator role's trust principal is the `terraform-admin` IAM user gated on MFA — a deliberate choice over an SSO role; (b) the ingress 128 MB memory re-trim is rejected — the lazy boto3 cold start cannot fit the 5 s budget at 128 MB (observed Sandbox.Timedout on first invoke); memory buys CPU, so ingress stays at the 512 MB known-good value (recorded in terraform/compute.tf).
**Consequences:** operator access requires the terraform-admin user + MFA; ingress cold-start budget is solved by sizing, not code-path changes.

Current truth: HLD §5.1, §2.1.

## 2026-09-15 — reserved concurrency dropped: both functions unreserved

**Context:** the account's total Lambda concurrency is 10 and AWS enforces ≥10 unreserved account-wide, so the planned ingress-2/worker-5 reservations could not be landed (live-apply evidence).
**Decision (Mars ruling):** drop reserved concurrency on both functions; restore reserves of 2 (ingress) and 5 (worker) only after a quota raise.
**Consequences:** the admission-loss boundary moves from a per-function ceiling to the account-level pool (10, shared) — a sustained ingress flood can starve the worker; accepted knowingly at the ruling, later surfaced and ratified as the §6 failure-mode-5 residual at Gate-21 (2026-09-20); mitigations: invocation-spike alarm + kill switch. Spend-bounding rides the account pool rather than a reservation.

Current truth: HLD §2.1, §2.3, §4.3, §6 failure mode 5.

## 2026-09-17 — v6.7 → v6.8 — measurement-only revision (no architectural or configuration change; recorded the AC-(b) budget violation as accepted risk)

**Context:** the "6–15 s typical" worker budget had never been measured against the live endpoint.
**Decision:** live-endpoint LLM latency measured 2026-09-17 — ~130 s/case thinking-off (max ~140 s), ~318 s thinking-on, roughly 10× the 6–15 s "typical" and the ≤15 s AC-(b) bar (§4.2 measured-reality note; §2 rationale annotated; §7.3 AC (b) flagged currently-unmet). Single time-window measurement, re-probe pending; companion A/B found model choice, thinking, and temperature buy no quality on the eval corpus at material latency cost (`tests/model_evals/results/`).
**Consequences:** latency, not model choice, is the binding product constraint until a re-probe says otherwise; do not assume sub-15 s LLM legs against the current endpoint.

Current truth: HLD §4.2.

## 2026-09-19 — v6.8 → v6.9 — measurement/config-only revision (zero architectural, budget, or AC change)

**Context:** the SPR-60 256 MB trim starved the worker against the measured LLM latency (~130 s/case).
**Decision:** worker sizing raised to 900 s / 1769 MB (1 full vCPU) with queue visibility raised to 5400 s (= 6 × 900, AWS-recommended ratio invariant carried forward); supersedes the SPR-60 256 MB trim (Mars ruling 2026-09-19, recorded in specs/002-worker-sizing-hcp/spec.md; sizing rationale and free-tier math therein); HCP Terraform adoption staged (remote state in CLI-driven workspace `pr-reviewer`, org `mars-net`; bootstrap OIDC trust applied once locally, never HCP-managed). §2.3 reserved-concurrency row corrected to match live config (unreserved per the 2026-09-15 ruling) — pre-existing table drift caught by the self-review pass.

Current truth: HLD §2.2, §2.3, §7.2.

## 2026-09-20 — v6.9 → v6.10 — documentation-only ingress-half drift corrections (2659989)

**Context:** the 2026-09-20 sync audit found the ingress half of the HLD stale — every prior drift pass was worker-sizing-scoped. Record enriched during migration (references expanded) — a summary, not a verbatim copy.
**Decision:** ingress memory corrected 128 MB → 512 MB (compute.tf documents the cold-start history); unreserved reality documented across admission semantics (§2.1), §4.1, §6 failure modes 5/14, and the §7.2 table (2026-09-15 ruling); `reopened` added to the allow-list and envelope enum (spec delta D1, `specs/001-pr-reviewer/contracts/ingress-webhook.md`); §4.1 Lambda row re-derived at spec-002 sizing (~225 GB-s/review); trust-policy mechanism corrected (worker `aws:SourceAccount`, operator terraform-admin + MFA); GitHub timeout restated as a single 10s; alarm list updated to the shipped 8-alarm set; §5.1 cites the bootstrap OIDC stack; the 2026-09-20 PR #68 hygiene pass (Gate-21 advisory docs fixes, squash-merged as 0381b56) is covered by this entry (it added no v6.9 delta).
**Consequences:** documentation-only — no infrastructure change; the account-level starvation residual of the unreserved posture is documented as accepted (§6 failure mode 5).

Current truth: HLD §2.1, §4.1, §4.3, §5.1, §6.

## 2026-09-20 — review header on the canonical comment (`**Review #N · updated {stamp} PT**`)

**Context:** the canonical comment showed GitHub's "edited" with no revision count or freshness (docs/ideas.md ideas 1+2, Mars directive 2026-09-20); earlier "timestamp"-word defects traced to no clock value reaching the model.
**Decision (Mars ruling):** worker-injected deterministic header directly under the byte-stable marker — `#N` = established `generation + 1` (first review = #1; same-SHA replay keeps #N with a refreshed stamp; generation counts established revisions, not publishes), stamp = publish instant in `America/Los_Angeles` rendered in code (`Mon D, H:MM AM/PM`, 12-hour, no seconds, hour not zero-padded), never by the model; FR-028 failure notice gains no header (fixed system template). Review port becomes `review(head_sha, generation)`.
**Consequences:** one extra comment line (accepted); tz constant resolves at import so a missing tz database fails cold start, not mid-review; marker byte-stability (§3.4 reconciliation) untouched.

Current truth: HLD §2.7, §2.8.

## 2026-09-20 — review payload enrichment (title/body/prior comment, prompt v2)

**Context:** the reviewer reviewed the diff blind to intent — never the PR title, description, or its own prior comment (docs/ideas.md idea 3, Mars directive 2026-09-20); asking the model for a timestamp had already proven models cannot supply what the prompt withholds (idea 2).
**Decision (Mars ruling):** one bounded payload assembled in code by a single shared builder (`assemble.render_review_payload`, used by the worker and the eval capture tool): PR title + description from the existing meta GET (combined cap 4096, title preserved whole) plus the prior canonical comment on re-reviews (cap 8192, best-effort page-1 read that never spends the record's single 401 budget and degrades to section-omitted on any failure); `prompt_version` v1 → v2 with the Input section framing all three as adversarial data. Injection-fencing posture unchanged (untrusted-data framing already covered titles/bodies/comments; no new trust).
**Consequences:** model input grows ≤ ~12k chars against the 800k diff budget (negligible); the pinned eval set goes stale by design until `capture.py --force` re-pins against the production shape.

Current truth: HLD §2.7.

## 2026-09-20 — 003-T2a: output-hygiene prohibition in prompt v2

**Context:** live v2 re-pins (2 runs, 15 cases each) showed the model describing attacks in the publication gate's forbidden vocabulary: `xss_safe` quoted raw `<script>` tags inside inline code in both runs (`hidden_html`), and the `injection` case's correct injection-refusal report used "override review behavior" in plain prose (`control_directive`). v1/v2 prohibitions are byte-identical — v1's clean pin was sampling luck, not guidance.
**Decision (Mars ruling, 2026-09-20):** add exactly one output-hygiene bullet to the v2 Prohibitions (describe payloads and embedded instructions descriptively; no raw HTML/script tags, no directive phrasing, even inside code spans or when quoting adversarial content); publication gate untouched; version stays `v2` (unreleased) and the live re-pin captures the new sha. Escalation bound: a 3rd consecutive same-case pin trip returns to Mars.
**Consequences:** stricter model output discipline on adversarial cases; possible bounded re-pin sampling retries; gate strictness preserved.

Current truth: `lambda/common/validate.py` approval scan; HLD §2.7 posture sentence unchanged.

Current truth: `specs/003-comment-review-ux/spec.md` (T2a amendment); HLD §2.7 unchanged.

## 2026-09-20 — approval-verdict gate precision (code-span carve-out, word-bounded stem)

**Context:** four false-positive review discards in one evening (PRs #80–#82), all `assemble_approval_verdict`: the gate's raw substring `approv` matched descriptive prose and backticked identifiers in reviews of PRs *about* the reviewer's own gate (runbook/CI/spec diffs), discarding otherwise-valid reviews. A companion proposal to reveal the error class in the D2 failure notice was found contract-frozen ("SHA is the only variable", explicit internal-status prohibition, FR-028 / canonical-comment.md) — deferred, not implemented.
**Decision (Mars ruling, 2026-09-20, Option A):** tighten the `approval_verdict` scan — (1) approval phrases match outside inline-code/code-block spans (same carve-out rationale as the mentions scan: code-formatted text is quotation, not assertion); (2) the bare stem matches word-bounded (`\bapprov`). Plain-prose verdict phrasing ("LGTM", "safe to merge") still refuses — §5.3 control-plane posture unchanged.
**Consequences:** reviews of gate/meta PRs publish normally. Residual accepted: model output could place verdict phrasing inside code spans (prompt-bound reviewer, no adversarial motive; the mentions precedent's inertness argument is weaker here — code spans still render — which the Gate 5-style review must weigh). Notice-class reveal needs a D2 contract amendment — separate Needs-input if wanted.

Current truth: `lambda/common/validate.py` approval scan; HLD §2.7 unchanged.

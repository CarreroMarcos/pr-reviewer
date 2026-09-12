# Feature Specification: Autonomous Serverless PR Reviewer

**Feature Branch**: `001-pr-reviewer`

**Created**: 2026-09-12

**Status**: Draft

**Input**: User description: "Feature: Autonomous Serverless PR Reviewer. Source: existing docs/HLD.md. Constitution is already ratified at .specify/memory/constitution.md — obey it. Write requirements and user stories only: one canonical reviewer comment per PR, ingress vs worker split, delivery/comment/revision identity, failure handling. No Terraform, Lambda, or AWS how-to. No code."

## Clarifications

### Session 2026-09-12

- Q: Which pull-request events should trigger an automated review — and should a closed PR that gets reopened be reviewed again? (FR-008) → A: PR opened, new commits pushed (synchronize), draft→ready-for-review transition, plus PR reopened. Drafts skipped; all other events discarded.
- Q: Should the comment-only write boundary hold — the system's only action on the platform is maintaining the single review comment — or do you want any additional write action in v1? (FR-027) → A: Comment only; no approvals, change requests, labels, merges, commit statuses, check runs, reactions, or replies.
- Q: Should one deployment serve exactly one target repository, or must a single deployment handle multiple repositories in v1? (Assumptions) → A: Exactly one target repository per deployment in v1; multi-repo remains a future configuration scale-up with no behavioral change.
- Q: When a review permanently fails after bounded retries, should the pull request itself show anything, or should the failure stay operator-only? (Story 5) → A: The canonical comment itself carries a system-generated failure state for the affected revision (same revision-currency fence, no internal details), reverting to review content on the next successful review; the operator alert is unchanged.
- Q: Do the latency acceptance targets stay as defaulted — acknowledgment within 1 second, first review comment within 15 seconds, 2-minute hard cap — or should any of them change? (FR-011, SC-001, SC-007) → A: Confirmed as-is; the 1-second acknowledgment remains the platform-agnostic ceiling (the implementation contract may tighten it further).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - First Automated Review on PR Open (Priority: P1)

A repository maintainer opens (or marks ready for review) a pull request. The system
acknowledges the event immediately and, shortly after, an automated review comment
appears on the pull request summarizing findings, risks, and suggested fixes for the
current revision.

**Why this priority**: This is the core value of the product — timely automated
feedback on every pull request. Without it nothing else matters.

**Independent Test**: Open a pull request with known reviewable issues in a configured
repository and verify exactly one review comment appears within the latency budget,
describing the known issues.

**Acceptance Scenarios**:

1. **Given** a configured repository with the reviewer enabled, **When** a contributor
   opens a pull request, **Then** the platform's event is acknowledged within 1 second
   and exactly one automated review comment is published within 15 seconds.
2. **Given** a pull request opened in draft state, **When** it is converted to
   ready-for-review, **Then** a review is produced at that transition.
3. **Given** a pull request that was closed and is now reopened, **When** the
   reopen event arrives, **Then** a review is triggered for the current head
   revision.
4. **Given** an event that is not a pull-request review trigger (e.g., issue opened,
   label applied), **When** it arrives, **Then** it is safely discarded with a success
   acknowledgment and no review activity occurs.

---

### User Story 2 - Single Evolving Canonical Comment (Priority: P2)

A maintainer pushes additional commits to an open pull request. The existing review
comment is updated in place to reflect the new revision — no second comment is created,
and the comment history shows one continuously evolving review thread.

**Why this priority**: Multiple accumulating comments per PR would destroy the
review-by-comment value proposition and pollute the conversation. Convergence to one
comment is the system's defining behavior.

**Independent Test**: Push several successive commits to a PR (including rapid
back-to-back pushes) and verify at all times exactly one reviewer comment exists and
its content reflects the newest revision once processing settles.

**Acceptance Scenarios**:

1. **Given** a PR with an existing canonical review comment, **When** a new commit is
   pushed, **Then** the same comment is updated in place with findings for the new
   revision.
2. **Given** two commits pushed in rapid succession before the first review completes,
   **When** processing settles, **Then** exactly one comment exists and it reflects the
   newest of the two revisions — never the older one.
3. **Given** the canonical comment was deleted by a human, **When** the next review
   event for that PR is processed, **Then** exactly one new canonical comment is
   created and no duplicates remain.

---

### User Story 3 - Stale Revisions Never Override Newer Reviews (Priority: P2)

A maintainer observes that delayed, duplicated, or out-of-order events (e.g., a slow
event from an older commit arriving after a newer one) never cause an older review to
replace a newer one. Only a review whose revision is confirmed current against the
live pull request may modify the canonical comment.

**Why this priority**: Review comments drive merge decisions; a stale review can
mislead maintainers about the code actually being merged. This is a correctness
guarantee, not a convenience.

**Independent Test**: Inject an artificially stale event (an older revision's trigger
arriving late) while the PR head is newer, and verify the canonical comment is not
modified to reflect the stale revision.

**Acceptance Scenarios**:

1. **Given** PR head is revision B and a queued trigger for revision A arrives late,
   **When** the stale trigger is processed, **Then** no comment modification occurs
   for revision A and the event is discarded as superseded.
2. **Given** a review of revision B is mid-flight, **When** the comment is about to be
   published, **Then** the system re-confirms B is still the live PR head and aborts
   publication if it is not.

---

### User Story 4 - Duplicate and Replayed Deliveries Are Safe (Priority: P2)

An operator observes the platform re-delivering the same event (its at-least-once
guarantee means repeats are normal). Repeated, replayed, or reordered deliveries never
cause duplicate comments, double reviews of the same revision, or incorrect state.

**Why this priority**: Distributed event delivery cannot promise exactly-once; making
duplicates harmless is a prerequisite for trustworthy operation at all.

**Independent Test**: Deliver the same well-formed event multiple times (including
after a delay) and verify each duplicate is recognized and produces no additional
comment or review beyond the first.

**Acceptance Scenarios**:

1. **Given** an event was already accepted and processed, **When** the identical
   delivery arrives again, **Then** it is acknowledged as already-handled and no new
   review or comment mutation occurs.
2. **Given** two copies of the same event processed concurrently, **Then** their
   effects are identical to processing it once — one comment, one revision state.
3. **Given** an event replayed after the system's duplicate-detection window has
   expired, **Then** at most one bounded redundant review occurs and the PR still
   converges to exactly one canonical comment.

---

### User Story 5 - Failures Are Contained, Surfaced, and Recoverable (Priority: P3)

An operator notices a review could not complete (e.g., the review provider or the
hosting platform was unavailable). The failure is retried automatically a bounded
number of times; if it still cannot complete, the work is retained (never silently
dropped), the operator is alerted with enough context to fix the cause and re-drive
the work, and the PR's canonical comment shows a system-generated failure state so
maintainers are not misled by stale or missing reviews. No invalid review content is
ever published as a side effect.

**Why this priority**: Correctness and safety (stories 1–4) come first; operational
recovery completes the lifecycle but is not needed for the first valuable release.

**Independent Test**: Simulate a sustained downstream outage during review processing,
then restore service, and verify bounded retries occurred, the alert fired, the
canonical comment showed the failure state during the outage, retained work completed
after recovery, and the PR still shows exactly one canonical comment.

**Acceptance Scenarios**:

1. **Given** a transient provider error during review processing, **When** the
   attempt fails, **Then** the work is retried automatically up to the configured
   bounded attempt count.
2. **Given** all bounded attempts are exhausted, **When** the work is set aside,
   **Then** the operator is alerted and the retained work remains available for
   re-driving after the cause is fixed.
3. **Given** model output that fails structural validation, **When** publication is
   attempted, **Then** the invalid content is never published; the event is completed
   as non-retryable and surfaced for investigation.
4. **Given** a review source of truth indicates the work failed permanently and the
   operator re-drives it after a fix, **When** the retained work is re-processed,
   **Then** the PR converges to exactly one canonical comment with no duplicates.
5. **Given** a review permanently fails for the current head revision, **When** the
   failure is finalized, **Then** the canonical comment carries the system-generated
   failure state and reverts to normal review content at the next successful review.

---

### User Story 6 - Repository Content Cannot Manipulate the Reviewer (Priority: P3)

A malicious or careless contributor embeds instructions in their pull request
(in diffs, titles, comments, or file contents) attempting to make the reviewer declare
the code safe, hide findings, reveal configuration, or take administrative actions.
The reviewer treats all repository content as inert data: reviews are unaffected by
such instructions, and the system takes no action on the repository beyond posting
its single comment.

**Why this priority**: Security containment, not baseline value; essential before
untrusted-public use, but the P1 flow is deliverable without it.

**Independent Test**: Submit a PR containing prompt-injection text instructing the
reviewer to approve or reveal secrets, and verify the published review is unaffected,
contains no injected behavior, no secrets, and no merge verdict.

**Acceptance Scenarios**:

1. **Given** a PR whose content contains instructions directed at the reviewer,
   **When** the review is produced, **Then** those instructions are treated as data
   and have no effect on findings, severity, or publication.
2. **Given** any completed review, **Then** the system has performed no action on the
   repository other than maintaining the single canonical comment.
3. **Given** any review output, **Then** it contains no credentials, no configuration
   values, no @mentions, and no approval or merge-safety verdicts.

---

### Edge Cases

- What happens when the canonical comment is edited by a human? The next review cycle
  treats the marker-bearing comment as canonical and replaces its content with the
  fresh review; identity persists, content is system-owned.
- What happens when multiple duplicate comments somehow exist (e.g., after a failure
  recovery)? Reconciliation deterministically keeps exactly one (the lowest comment
  ID — the earliest created; HLD §3.4) and removes the extras.
- What happens when a PR is closed or a branch is force-pushed mid-review? The
  revision-currency check fails and the in-flight review is discarded without
  publishing.
- What happens when the payload is malformed, oversized, or fails authenticity
  checks? It is rejected before any processing, with nothing enqueued and no retry
  storm.
- What happens when an extreme flood of events arrives? Backpressure is applied and
  sustained overload is shed with operator visibility rather than unbounded cost;
  no silent loss of accepted work.
- What happens when the review provider returns unusable output repeatedly? The event
  is treated as non-retryable after bounded attempts and surfaced; the raw output is
  never published and the canonical comment carries the failure state for that
  revision (FR-028).

## Requirements *(mandatory)*

### Functional Requirements

**Canonical comment**

- **FR-001**: System MUST maintain exactly one canonical reviewer comment per pull
  request, updated in place as the PR evolves.
- **FR-002**: The canonical comment MUST carry a stable embedded identity that allows
  the system to recognize it even if all internal records of it are lost.
- **FR-003**: If zero canonical comments exist at publication time, System MUST
  create exactly one; if multiple exist, System MUST deterministically retain one and
  remove the rest.
- **FR-004**: Review comment content MUST be fully owned and regenerated by the
  system each cycle; the embedded identity is the only durable element.
- **FR-005**: The embedded identity MUST be system-generated and MUST NOT originate
  from the automated review content itself.

**Intake / processing split**

- **FR-006**: Intake MUST acknowledge or reject every incoming event within 1 second
  (platform-agnostic ceiling; the implementation contract may be tighter),
  independently of how long the eventual review takes.
- **FR-007**: Intake MUST verify the authenticity and integrity of every event before
  any processing, and reject failures without enqueuing work.
- **FR-008**: Intake MUST filter events by type and review-trigger conditions
  (review-triggering PR actions are: opened, synchronize (new commits pushed),
  ready-for-review transition, and reopened; draft PRs are skipped), discarding
  non-triggers with a success acknowledgment.
- **FR-009**: Accepted work MUST be handed off to durable storage before intake
  confirms processing, such that an acknowledged event is never lost to a crash.
- **FR-010**: Review processing MUST run asynchronously from intake; acknowledgment
  latency MUST NOT depend on review, publication, or any external provider latency.
- **FR-011**: A review MUST complete within a bounded time budget (target 15 seconds
  typical, hard cap 2 minutes); overruns MUST be treated as retryable failures.

**Identity controls**

- **FR-012**: Every event delivery MUST carry (or be assigned) a unique delivery
  identity; duplicate deliveries MUST be recognized and processed at most once.
- **FR-013**: Every review MUST be bound to the exact PR revision (head commit) it
  examined, and published comment content MUST correspond to that revision.
- **FR-014**: Before publishing, System MUST re-confirm the reviewed revision is
  still the live PR head against the authoritative PR state; on mismatch the
  publication MUST be aborted and the review discarded as stale.
- **FR-015**: Revision currency MUST be determined by equality against the live PR
  head; System MUST NOT infer ordering between revisions from their identifiers.
- **FR-016**: Concurrent processing of events for the same PR MUST be safe: claims on
  publication MUST be exclusive and time-bounded so at most one publication proceeds
  at once, with takeover possible after expiry.

**Failure handling**

- **FR-017**: Transient failures (provider throttling or unavailability) MUST be
  retried automatically with a bounded attempt count.
- **FR-018**: Work that exhausts its attempts MUST be retained for operator
  re-driving, MUST raise an alert, and MUST never be silently discarded.
- **FR-019**: Permanent conditions (invalid content, lost access, unusable output)
  MUST NOT be retried; they MUST be logged (identifiers only), surfaced to the
  operator, and reflected in the canonical comment per FR-028.
- **FR-020**: No partial, unvalidated, or invalid review content MUST ever be
  published; structural validation MUST gate every publication, for model review
  output and system-generated failure content alike.
- **FR-021**: Credential rotation or expiry MUST be recovered automatically on the
  next attempt without operator intervention, within one retry cycle.

**Untrusted content containment**

- **FR-022**: All repository-derived content MUST be treated as untrusted data;
  instructions embedded in it MUST NOT be followed.
- **FR-023**: Repository content MUST NOT establish approval, severity, security
  status, policy exceptions, reviewer identity, or authorization of any kind.
- **FR-024**: The automated review MUST have no tools, no execution capabilities, and
  MUST NOT be able to create or modify any state; its output is inert comment text.
- **FR-025**: Review output MUST be validated for structure and prohibited content
  (credentials, configuration, @mentions, external embedded media, merge-safety
  verdicts) before publication; violations MUST NOT be published.
- **FR-026**: No secret, credential, or raw untrusted payload content MUST ever
  appear in logs or operational outputs; logs carry identifiers and metrics only.

**Scope boundary**

- **FR-027**: The system's only write action on the platform MUST be maintaining the
  single canonical review comment; it MUST NOT approve, label, merge, react, reply,
  set commit statuses or check runs, or modify any other platform state.
- **FR-028**: When a review permanently fails, the canonical comment MUST reflect a
  system-generated failure state for the affected revision instead of silently
  remaining stale, subject to the same revision-currency check as review content;
  failure content MUST be system-generated (never model output), MUST contain no
  internal error details, and MUST be replaced by normal review content once a later
  review of the live head succeeds.

### Key Entities *(include if feature involves data)*

- **Review Request (delivery)**: one received event trigger — unique delivery
  identity, repository, PR number, action, and the PR head revision it refers to.
- **Canonical Review Comment**: the single PR conversation comment carrying the
  stable embedded identity; content always corresponds to one reviewed revision.
- **Review State (per PR)**: the currently accepted revision, the canonical comment's
  reference, generation counter, and any active publication claim with its expiry.
- **PR Revision**: the head commit of the PR at a moment in time; the live PR head is
  the sole authority for what is "current".

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A newly opened PR receives its first canonical review comment within 15
  seconds in at least 95% of cases.
- **SC-002**: After any sequence of pushes, duplicate events, or concurrent
  processing, each PR converges to exactly one review comment reflecting the newest
  reviewed revision within 30 seconds of the last event.
- **SC-003**: 100% of forged, tampered, or replayed deliveries produce zero reviews,
  zero comment mutations, and zero processing side effects.
- **SC-004**: 100% of injected stale-revision events fail to alter the canonical
  comment.
- **SC-005**: 100% of audited log and output samples contain no secrets and no raw
  untrusted payloads.
- **SC-006**: 100% of permanently failed reviews are surfaced to operators and
  reflected in the PR's canonical comment within 5 minutes of their final attempt,
  with retained work available for re-driving.
- **SC-007**: Event acknowledgment completes within 1 second for at least 99% of
  deliveries, with zero acknowledged events lost.
- **SC-008**: Repository maintainers report the single-comment thread as clear and
  non-duplicative (qualitative check across a 10-PR pilot).

## Assumptions

- GitHub is the hosting platform: PRs, events, and the canonical comment all live in
  GitHub conversations; "live PR head" means GitHub's own PR state.
- Review triggers are: PR opened, new commits pushed (synchronize),
  ready-for-review transitions, and PR reopened; draft PRs are skipped
  (FR-008; "reopened" clarified 2026-09-12).
- Exactly one target repository per deployment in v1 (confirmed 2026-09-12);
  multi-repo operation is a future configuration scale-up, not a behavioral change.
- The reviewer posts comments only — no approvals, change requests, labels, or merges
  in v1 (FR-027).
- Review quality (helpfulness of findings) is governed by a separate evaluation
  process; this spec covers delivery, correctness, and safety of the pipeline.
- Event identity records have a finite lifetime; a replay outside that window may
  cause one bounded redundant review, which is acceptable and must still converge to
  one comment (FR-012, story 4).
- Sustained event floods above intake capacity are shed with explicit backpressure
  and operator visibility; the zero-silent-loss guarantee applies to accepted work.

# Tasks: Autonomous Serverless PR Reviewer

**Input**: Design documents from `/specs/001-pr-reviewer/`

**Prerequisites**: plan.md ✅, spec.md ✅ (clarified 2026-09-12), research.md ✅, data-model.md ✅, contracts/ ✅, quickstart.md ✅ — architecture authority: `docs/HLD.md` v6.9 (plan deltas D1 `reopened`, D2 FR-028 failure state, D3 test mapping).

**Revision 2 (post-`/speckit.analyze` remediation)**: validation gate + prompt contract moved into Foundational (ORD1); Terraform BOM split per file; every verify target has an explicit test-first creator task; terminology normalized to "failure-state notice" (contracts); integration verify forms normalized; spec/plan/quickstart drift fixed in those files.

**Tests**: REQUESTED (plan D3). Every story phase includes tests written first (they must FAIL before their implementation tasks), per HLD §4.4 layers: unit, state-machine (in-memory DynamoDB stub), contracts (signed fixtures, in-process stubs, injected clocks), model evals — plus deployed-stack integration tests = HLD §7.3 acceptance criteria (a)–(j) + plan additions (k)–(n).

**Organization**: Tasks grouped by user story (US1–US6 from spec.md, priority order P1 → P2 → P2 → P2 → P3 → P3).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- **[Story]**: owning user story (US1…US6). Setup/Foundational/Polish tasks carry no story label.
- Every task names exact file path(s), a `verify:` command, and FR/AC/quickstart refs.

## Path Conventions

- Runtime: `lambda/common/` (single source of truth per HLD §7.1), `lambda/ingress_handler.py`, `lambda/worker_handler.py`
- Infra: `terraform/` (BOM per HLD §7.1); prompt: `prompts/`; runbooks: `docs/`
- Tests: `tests/unit/`, `tests/state_machine/`, `tests/contracts/`, `tests/model_evals/`, `tests/integration/`
- Refs: FR-xxx (spec), USn.ACx (spec acceptance scenarios), SC-xxx, QS-x (quickstart scenario letters), HLD §

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Repository initialization per plan.md Project Structure.

- [x] T001 Create repository scaffold: `lambda/common/`, thin `lambda/ingress_handler.py` + `lambda/worker_handler.py` stubs, `terraform/`, `prompts/`, `docs/`, `tests/{unit,state_machine,contracts,model_evals,integration}/` per plan.md "Project Structure" — verify: tree matches plan.md exactly (HLD §7.1)
- [x] T002 Create `pyproject.toml`: runtime dependency `boto3` ONLY; dev group `pytest` (+ plugins used by tests); Python 3.12 target — verify: `pip install -e '.[dev]'` succeeds and `pytest` collects 0 tests (Constitution II)
- [x] T003 [P] Configure pre-commit: hygiene, ruff, ruff format, gitleaks — verify: `pre-commit run --all-files` passes on empty scaffold (Constitution workflow rules)
- [x] T004 [P] Create `terraform/main.tf` + `terraform/variables.tf`: AWS provider pinned `us-west-2`, required_providers, local-state guardrails (`terraform.tfstate` gitignored) — verify: `terraform init && terraform fmt -check && terraform validate` (Constitution I; HLD §7.1)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared contracts and mechanisms every user story depends on — including the publication validation gate (FR-020/FR-025) so no user story can publish unvalidated content. **⚠️ No user-story work until this phase completes.**

- [x] T005 [P] Create `tests/unit/test_envelope.py`: valid/invalid envelope cases per contracts/ingress-webhook.md schema table (version constant `v1`, event constant `pull_request`, action enum `opened|synchronize|ready_for_review|reopened` **[D1]**, `repo_full_name` regex `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$` ≤128 chars, `pr_number` int >0 ≤10⁹, `head_sha`/`base_sha` 40-char lowercase hex, `sender` ≤64 login charset, `delivery_guid` UUID ≤64) — verify: `pytest tests/unit/test_envelope.py` FAILS (FR-008, FR-012; contracts/ingress-webhook.md)
- [x] T006 Implement `lambda/common/envelope.py`: stdlib `dataclass` Envelope + `validate_envelope()` enforcing T005 constraints verbatim; malformed → typed rejection — verify: `pytest tests/unit/test_envelope.py` PASSES (FR-008, FR-012; data-model.md §1; HLD §2.1)
- [x] T007 [P] Create `tests/unit/test_marker.py`: exact marker string `<!-- pr-reviewer:canonical:v1:{repo_full_name}#{pr_number} -->` for sample repo/PR inputs — verify: `pytest tests/unit/test_marker.py` FAILS (FR-002, FR-005; HLD §2.8; contracts/canonical-comment.md)
- [x] T008 Implement `lambda/common/marker.py`: builder emitting exactly the T007 marker; pure function — verify: `pytest tests/unit/test_marker.py` PASSES (FR-002, FR-005; HLD §2.8)
- [x] T009 [P] Create `tests/state_machine/test_state_codec.py`: round-trip + rejection cases for the review-state item per HLD §3.1 field contract (stored `status` ∈ {`CLAIMED`,`ACTIVE`} only, `ABSENT` by absence, `STALE` derived, `generation` monotone from 0, `head_sha`/`last_seen_sha` 40-hex, `claim_until` epoch s, `claim_owner` = raw delivery GUID, `comment_id` int64 ACTIVE-only, `updated_at` ISO-8601 UTC, `pk = review:{repo_full_name}#{pr_number}`) + delivery item `pk = delivery:{guid}` TTL 7 d — verify: `pytest tests/state_machine/test_state_codec.py` FAILS (FR-013–FR-016; data-model.md §2; HLD §3.1, §2.4)
- [x] T010 Implement `lambda/common/state.py`: state codec + conditional-update expression builders (establish (a)/(b)/(c), claim `head_sha = :reviewed AND generation = :gen AND (claim_until < :now OR attribute_not_exists(claim_owner))`, finalize revision-only) — verify: `pytest tests/state_machine/test_state_codec.py` PASSES (HLD §3.1, §3.3; FR-013–FR-016)
- [x] T011 Create `tests/state_machine/test_protocol.py`: in-memory DynamoDB stub covering establish conditions (a) first-write, (b) `last_seen_sha` equality, (c) live-head-confirm + generation increment; claim condition; fence-after-claim ordering; finalize condition; expired-lease takeover — verify: `pytest tests/state_machine/test_protocol.py` FAILS (HLD §3.2–§3.3; failure modes 6–10)
- [x] T012 Implement `lambda/common/protocol.py`: fenced publication executor in exact order establish → review(callback) → claim → fence(callback) → publish(callback) → conditional finalize; mismatch at any conditional ⇒ superseded/stale discard, never overwrite — verify: `pytest tests/state_machine/test_protocol.py` PASSES (HLD §3.3; FR-014–FR-016)
- [x] T013 [P] Create `tests/unit/test_config.py`: cold-start fetch, 30-minute TTL refresh, 401 cache-bust via injected clock; endpoint hydration rejects non-HTTPS / non-allow-list hosts — verify: `pytest tests/unit/test_config.py` FAILS (HLD §2.3 item 1, §2.6; FR-021)
- [x] T014 Implement `lambda/common/config.py`: injectable SSM accessor (batched `GetParameters` `WithDecryption`, cold-start fetch, 30-min TTL, cache-bust on 401) + injectable clock — verify: `pytest tests/unit/test_config.py` PASSES (HLD §2.3 item 1, §2.6, §4.4 item 3; Constitution III)
- [x] T015 [P] Create `tests/unit/test_logs.py`: payload-shaped inputs yield no forbidden substrings (Authorization headers, PAT, webhook secret, GLM key, raw payloads, diffs, LLM requests/responses); fixed field set present — verify: `pytest tests/unit/test_logs.py` FAILS (HLD §5.4; FR-026; Constitution III)
- [x] T016 Implement `lambda/common/logs.py`: structured JSON logger, fixed field set (IDs, SHAs, durations, token usage, status, error class) + redaction guard — verify: `pytest tests/unit/test_logs.py` PASSES (HLD §5.4; FR-026)
- [x] T017 [P] Create `tests/unit/test_structural_validation.py`: gate accepts valid §2.7-shaped comment; rejects missing marker, missing sections, over-length, credential-like strings, hidden HTML/script, control-plane directives, prompt canary, `@mentions`, external media, approval/merge verdicts — verify: `pytest tests/unit/test_structural_validation.py` FAILS (HLD §2.3 item 7, §2.7; FR-020, FR-025)
- [x] T018 Create `prompts/system_prompt.md`: versioned system prompt with `prompt_version` id implementing the HLD §2.7 Model I/O contract — untrusted-data framing, no tools, bounded Markdown shape (findings: severity ∈ {HIGH,MEDIUM,LOW}, `path:LINE` LINE ≥ 1, count ≤ `max_findings` config default 20), prohibitions, embedded canary substring — verify: doc review against HLD §2.7/§5.3 and contracts/canonical-comment.md (FR-022–FR-024; Constitution VII)
- [x] T019 Implement `lambda/common/validate.py`: publication validation gate consuming the T018 contract — marker present, bounded length, expected sections, prohibited-content and canary checks; returns typed verdict — verify: `pytest tests/unit/test_structural_validation.py` PASSES (HLD §2.3 item 7; FR-020, FR-025)
- [x] T020 [P] Create `terraform/compute.tf`: 2 Lambda functions (ingress 5 s/128 MB/reserved concurrency 25; worker 120 s/256 MB/concurrency 5), Function URL `AuthType NONE` CORS disabled, ESM `batch_size = 1`, `archive_file` packaging both zips incl. `lambda/common/` — verify: `terraform fmt -check` (full `terraform validate` at T024) (HLD §2.1–§2.3, §7.1)
- [x] T021 [P] Create `terraform/messaging.tf`: work queue (visibility 720 s, retention 4 d, `maxReceiveCount 5`) + redrive-allow policy naming the operator role + DLQ (14-day retention) — verify: `terraform fmt -check` (HLD §2.2, §2.5)
- [x] T022 [P] Create `terraform/state.tf`: DynamoDB `pr-reviewer-state`, partition key `pk`, PROVISIONED 25 WCU / 25 RCU — verify: `terraform fmt -check` (HLD §2.4; Constitution I)
- [x] T023 [P] Create `terraform/iam.tf`: exactly three roles with inline policies per HLD §5.1 — ingress (logs, webhook-secret-only SSM, sqs:SendMessage, DynamoDB Get/Put restricted `dynamodb:LeadingKeys = ["delivery:*"]`); worker (queue receive/delete/attributes/`ChangeMessageVisibility`, 4 explicit SSM ARNs, DynamoDB Get/Put/Update); operator (`StartMessageMoveTask` set on DLQ, SendMessage on work queue) — verify: `terraform fmt -check` + diff against HLD §5.1 block (Constitution III; failure modes 21–22)
- [x] T024 [P] Create `terraform/observability.tf`: CloudWatch log groups ×2 with 7-day retention — verify: `terraform fmt -check && terraform validate && pre-commit run --all-files` (all BOM files now exist) (HLD §4.3)

**Checkpoint**: Foundation ready — envelope, marker, state codec, fenced protocol, config, logs, **publication validation gate + prompt contract**, and the full serverless BOM exist and unit-test green. No user story can publish unvalidated content.

---

## Phase 3: User Story 1 — First Automated Review on PR Open (P1) 🎯 MVP

**Goal**: A contributor opens (or readies, or reopens) a PR → 202 ack ≤1 s → exactly one canonical, validated review comment ≤15 s.

**Independent Test**: Quickstart (a), (b), (e), (f), (k), (k2) against a deployed stack with a known-issue diff.

### Tests for User Story 1

- [x] T025 [P] [US1] Create `tests/contracts/test_signed_webhook_fixtures.py`: committed fixtures (fixed secret + payload → fixed signature) for `opened`/`synchronize`/`ready_for_review`/`reopened` **[D1]**, signed non-`pull_request` event, tampered signature, missing headers — verify: `pytest tests/contracts/test_signed_webhook_fixtures.py` FAILS (HLD §4.4 item 3; US1.AC1–AC4; contracts/ingress-webhook.md)
- [x] T026 [P] [US1] Create `tests/unit/test_ingress_gating.py`: full-string constant-time HMAC compare, body >1 MiB → 413 pre-decode, event-type gate, action allow-list incl. `reopened` / draft skip → 200 discard, processed-GUID → 200 no-op, schema-invalid body → 200, SQS failure → 500 without marking — verify: `pytest tests/unit/test_ingress_gating.py` FAILS (HLD §2.1; US1.AC4; FR-006–FR-008)
- [x] T027 [P] [US1] Create `tests/unit/test_diff.py`: endpoints constructed from `repo_full_name`+`pr_number` (never payload URLs), `X-GitHub-Api-Version: 2026-03-10` + explicit User-Agent, response shape-validation (`head.sha` 40-hex) before fence, diff budget `MAX_FILES=500`/`MAX_CHANGED_LINES=25000`/`MAX_INPUT_BYTES=800000`, deterministic lockfile summaries — verify: `pytest tests/unit/test_diff.py` FAILS (HLD §2.3 items 2–4; FR-013)
- [x] T028 [P] [US1] Create `tests/unit/test_llm.py`: stubbed chat-completions — temperature 0.2 sent, model/endpoint from injected config, timeouts connect 2 s / read 45 s, error path raises for queue retry, logs carry only status/duration/token usage — verify: `pytest tests/unit/test_llm.py` FAILS (HLD §2.3 item 5, §2.7)
- [x] T029 [P] [US1] Create `tests/unit/test_assemble.py`: assembly embeds marker (T008) + gate verdict (T019) required before any publish call; §2.7 section shape (empty sentinel "No significant issues found."); invalid content → publish refused — verify: `pytest tests/unit/test_assemble.py` FAILS (HLD §2.7, §2.8; FR-001, FR-004, FR-020)

### Implementation for User Story 1

- [x] T030 [US1] Implement `lambda/ingress_handler.py`: the eight §2.1 responsibilities in strict order (normalize/413 → HMAC → event gate → action filter **[D1]** → dedup GetItem → `SendMessage` → `PutItem delivery:{guid}` TTL 7 d → empty-body 202 within 250 ms); full response-contract table — verify: `pytest tests/unit/test_ingress_gating.py tests/contracts/test_signed_webhook_fixtures.py` PASSES (HLD §2.1; FR-006, FR-007, FR-009)
- [x] T031 [US1] Implement `lambda/common/diff.py`: GitHub fetch + shape validation + diff budget + lockfile summary module — verify: `pytest tests/unit/test_diff.py` PASSES (HLD §2.3 items 2–4)
- [x] T032 [US1] Implement `lambda/common/llm.py`: provider chat-completions client per T028 (temperature 0.2, SSM model/endpoint via T014, HTTP timeout policy, minimal error logging) — verify: `pytest tests/unit/test_llm.py` PASSES (HLD §2.3 item 5, §2.7)
- [x] T033 [US1] Implement `lambda/common/assemble.py`: worker-side comment assembly — marker injection + §2.7 section shape + **mandatory `validate()` gate (T019) before returning publish-ready content** — verify: `pytest tests/unit/test_assemble.py tests/unit/test_structural_validation.py` PASSES (HLD §2.3 item 7, §2.7, §2.8; FR-001, FR-004, FR-020)
- [x] T034 [US1] Implement `lambda/worker_handler.py` pipeline: validate envelope (T006) → hydrate credentials (T014; single 401 re-fetch is the ONLY in-request retry) → establish → diff (T031) → LLM (T032) → assemble + gate (T033) → claim → live-head fence → publish (POST via creation lease when no `comment_id`, else PATCH) → finalize; superseded/stale → discard — verify: `pytest tests/state_machine/` PASSES with stubs (HLD §2.3, §3.3, §3.4; FR-009–FR-016)
- [x] T035 [US1] Deploy to a scratch stack and run quickstart (a), (b), (e), (f), (k), (k2): delivery logged 202; exactly one canonical comment ≤15 s; bad-signature → 401 nothing enqueued; DLQ empty on happy path; reopened PR produces a fresh review; label/issue events discarded — verify: `pytest tests/integration/test_us1_acceptance.py` — assertions (a)(b)(e)(f)(k)(k2) pass (SC-001, SC-003, SC-007; US1.AC1–AC4; D1; QS-a/b/e/f/k/k2)

**Checkpoint**: US1 delivers value alone — P1 flow complete, validated content only, independently tested.

---

## Phase 4: User Story 2 — Single Evolving Canonical Comment (P2)

**Goal**: Successive pushes update the one comment in place; deletions/duplicates converge.

**Independent Test**: Quickstart (c), (d), (h).

### Tests for User Story 2

- [x] T036 [P] [US2] Create `tests/state_machine/test_reconcile.py`: marker-match adoption, lowest-comment-ID wins with extras deleted, creation-lease race → loser adopts winner's comment, unparseable list → non-retryable — verify: `pytest tests/state_machine/test_reconcile.py` FAILS (HLD §3.4; FR-003; US2.AC3)
- [x] T037 [P] [US2] Create `tests/unit/test_patch404_table.py`: every HLD §2.3 item-8 PATCH-404 row (comment migrated / creation lease → POST → persist → re-check) — verify: `pytest tests/unit/test_patch404_table.py` FAILS (HLD §2.3 item 8; failure mode 13)
- [x] T038 [P] [US2] Create `tests/state_machine/test_interleaving.py`: two revisions interleaved — second establish increments `generation`; mid-flight older review aborts at fence/finalize; only newest head reflected — verify: `pytest tests/state_machine/test_interleaving.py` FAILS (US2.AC2; HLD §3.3; FR-014–FR-016)

### Implementation for User Story 2

- [x] T039 [US2] Implement `lambda/common/reconcile.py`: fully-paginated, shape-validated comment listing + exact-marker match + deterministic adoption/deletion + creation lease (`attribute_not_exists(comment_id)` + claim condition) — verify: `pytest tests/state_machine/test_reconcile.py` PASSES (HLD §3.4; FR-003)
- [x] T040 [US2] Wire `lambda/worker_handler.py` update-in-place: reuse `comment_id` from ACTIVE state → PATCH; PATCH-404 → decision table (T037 rows) → reconcile (T039) — verify: `pytest tests/unit/test_patch404_table.py tests/state_machine/` PASSES (US2.AC1; HLD §2.3 item 8)
- [x] T041 [US2] Wire rapid-push latest-wins in `lambda/worker_handler.py`: second establish increments `generation`, mid-flight older review aborts at fence/finalize — verify: `pytest tests/state_machine/test_interleaving.py` PASSES (US2.AC2; HLD §3.3; FR-014–FR-016)
- [x] T042 [US2] Integration quickstart (c), (d), (h): second push updates same comment; two rapid pushes leave only latest head SHA; deleted bot comment + push converges to one new marker-bearing comment — verify: `pytest tests/integration/test_us2_acceptance.py` — assertions (c)(d)(h) pass (US2.AC1–AC3; SC-002; QS-c/d/h)

**Checkpoint**: US1 + US2 both independently functional.

---

## Phase 5: User Story 3 — Stale Revisions Never Override Newer Reviews (P2)

**Goal**: Late/reordered events cannot replace a newer accepted review.

**Independent Test**: Quickstart (g).

- [x] T043 [P] [US3] Create `tests/state_machine/test_stale.py`: older-SHA event rejected at establish (a/b/c fail) with `last_seen_sha` still recorded; fence mismatch after claim aborts publication before any GitHub write — verify: `pytest tests/state_machine/test_stale.py` FAILS (US3.AC1–AC2; FR-014, FR-015; HLD §3.3; failure modes 6–7)
- [x] T044 [US3] Wire `lambda/worker_handler.py` stale paths: live-head GET (shape-validated) immediately before publish, after claim; superseded events discarded with `stale_discarded` logged — verify: `pytest tests/state_machine/test_stale.py` PASSES (US3.AC1–AC2; HLD §2.3 item 2, §3.3 step 4, §4.3)
- [x] T045 [US3] Integration quickstart (g): redrive an artificially stale head SHA → canonical comment NOT mutated — verify: `pytest tests/integration/test_us3_acceptance.py` — assertions (g) pass (SC-004; US3.AC1; QS-g)

**Checkpoint**: Correctness guarantee (stories 1–3) holds: P1 + comment convergence + stale fencing.

---

## Phase 6: User Story 4 — Duplicate and Replayed Deliveries Are Safe (P2)

**Goal**: At-least-once delivery never yields duplicate comments or double reviews.

**Independent Test**: Quickstart (i) plus replay tests.

- [x] T046 [P] [US4] Create `tests/unit/test_dedup.py`: processed-GUID → 200 no-op; replay of a captured delivery within the 7-day window recognized — verify: `pytest tests/unit/test_dedup.py` FAILS (US4.AC1; FR-012; HLD §5.2)
- [x] T047 [P] [US4] Create `tests/state_machine/test_concurrent.py`: two concurrent copies of one event produce single effect (claim exclusivity); replay past TTL → at most one bounded redundant review, converges to one comment — verify: `pytest tests/state_machine/test_concurrent.py` FAILS (US4.AC2–AC3; FR-012; HLD §5.2 replay residual)
- [x] T048 [US4] Harden idempotency in `lambda/ingress_handler.py`: mark-processed only after successful `SendMessage`; re-delivered GUID short-circuits — verify: `pytest tests/unit/test_dedup.py` PASSES (US4.AC1; FR-009, FR-012; HLD §2.1 steps 5–7; failure mode 4)
- [x] T049 [US4] Harden idempotency in `lambda/worker_handler.py`: all state writes via conditional transitions (no unconditional PutItem on review records); duplicate processing converges — verify: `pytest tests/state_machine/test_concurrent.py` PASSES (US4.AC2–AC3; HLD §3.3)
- [x] T050 [US4] Integration quickstart (i): force concurrent workers on one PR (duplicate messages / lowered visibility) → single comment, correct head SHA — verify: `pytest tests/integration/test_us4_acceptance.py` — assertions (i) pass (US4.AC2; SC-002; QS-i)

**Checkpoint**: All P2 correctness stories (US2–US4) independently validated.

---

## Phase 7: User Story 5 — Failures Contained, Surfaced, Recoverable (P3) — includes D2

**Goal**: Bounded retries → retained work + operator alert + **failure-state notice in the canonical comment (FR-028)** → re-drive converges.

**Independent Test**: Quickstart (j), (l), (l2), (m), (n).

- [x] T051 [P] [US5] Create `tests/unit/test_error_classification.py`: every HLD §2.3 item-8 row — PATCH-404 variants, 403/404 list-read (non-retryable), 429 + `Retry-After`, 5xx, 401 (re-fetch once, then non-retryable), LLM timeout/429/5xx/invalid output (raise for queue retry), assembled-comment validation failure (non-retryable) — verify: `pytest tests/unit/test_error_classification.py` FAILS (HLD §2.3 item 8; US5.AC1–AC3; FR-017–FR-021)
- [x] T052 [P] [US5] Create `tests/contracts/test_failure_notice.py` **[D2]**: failure-state notice contract — fixed template `Automated review could not be completed for revision {head_sha}. …` (SHA is the only variable), marker present, prohibition list (no internal error details / provider names / statuses / credential-like strings / @mentions / external media / verdicts), idempotent re-publish, no-notice rows (GitHub-401, lost access, invalid envelope, non-failures) — verify: `pytest tests/contracts/test_failure_notice.py` FAILS (FR-028, FR-019, FR-020, FR-026; contracts/canonical-comment.md)
- [x] T053 [US5] Implement `lambda/common/failure_notice.py` **[D2]**: failure-state notice assembly + publication through the HLD §3.3 fenced path (claim → fence → PATCH, creation-lease POST if absent) + trigger mapping (`int(Attributes["ApproximateReceiveCount"]) >= 5` final-attempt row; permanent rows immediate; No-rows skip) + `failure_notice_published ∈ {true,false,skipped-stale}` log field; best-effort (notice failure never masks alert/DLQ) — verify: `pytest tests/contracts/test_failure_notice.py` PASSES (FR-028; research.md R6/R8; contracts/canonical-comment.md)
- [x] T054 [US5] Implement error classification in `lambda/worker_handler.py`: raise-for-retry vs complete-non-retryable per table; `Retry-After` → `ChangeMessageVisibility`; wire D2 trigger into final-attempt/permanent paths — verify: `pytest tests/unit/test_error_classification.py` PASSES (US5.AC1–AC3; HLD §2.2, §2.3 item 8; FR-017–FR-019)
- [x] T055 [US5] Extend `terraform/observability.tf`: alarms each with SNS topic + named owner — DLQ depth > 0, ingress 401-rate spike, 429 admission count, worker error rate, DynamoDB throttling, work-queue depth abnormal, daily LLM spend vs config-driven budget — verify: `terraform fmt -check && terraform validate` + plan review (HLD §4.3; SC-006)
- [x] T056 [US5] Write `docs/runbook-redrive.md`: operator redrive steps 1–5 (inspect → fix → `StartMessageMoveTask` → verify drain + comment convergence → log drill) with the exact IAM permission set — verify: doc review vs HLD §2.5; permissions diff against `terraform/iam.tf` (US5.AC2, US5.AC4; HLD §2.5)
- [ ] T057 [US5] Integration quickstart (j), (l), (l2), (m), (n): redrive drill converges; permanent-class failure → notice within SC-006 final-attempt budget; transient exhaustion → notice at final attempt (~48 min bound); stale notice `skipped-stale`; success reverts comment to review content — verify: `pytest tests/integration/test_us5_acceptance.py` — assertions (j)(l)(l2)(m)(n) pass (SC-006; US5.AC5; D2/D3; QS-j/l/l2/m/n)

**Checkpoint**: Failure lifecycle complete including the FR-028 PR-visible state.

---

## Phase 8: User Story 6 — Repository Content Cannot Manipulate the Reviewer (P3)

**Goal**: Prompt injection has zero effect; output is inert, validated comment text. (The validation gate and prompt contract themselves live in Foundational T017–T019 — this story owns model-behavior verification.)

**Independent Test**: Injection-diff acceptance scenarios; pinned evaluation set.

- [x] T058 [US6] Create `tests/model_evals/` pinned evaluation set + rubric harness: representative diffs with known findings, prompt-injection attempts, oversized/truncated inputs; scored on structural validity, finding faithfulness, prohibited-content absence; CI guard re-runs on any `prompt_version`/model-string change (prompt source: Phase 2 T018) — verify: `pytest tests/model_evals/` PASSES on the pinned set (HLD §4.4 item 4; Constitution workflow rules)
- [ ] T059 [US6] Integration acceptance: injection PR text → findings unaffected, no injected behavior, no secrets, no merge verdict in published comment; log-audit sample clean — verify: `pytest tests/integration/test_us6_acceptance.py` — assertions pass (US6.AC1–AC3; SC-003, SC-005; QS negative spot-checks)

**Checkpoint**: All six user stories independently functional.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [x] T060 [P] Ensure every cost figure (LLM pricing, budget thresholds, alarm params) flows from `terraform/variables.tf` / SSM config — verify: `terraform validate && ! grep -rnE '(\\$0\\.[0-9]+|[0-9]+/M)' terraform/ --include='*.tf'` returns no matches outside `variables.tf` (HLD §7.2; Constitution)
- [x] T061 [P] Write `docs/runbook-kill-switch.md`: worker reserved concurrency → 0 (or disable webhook) stops spend immediately, queued work retained — verify: doc review vs HLD §4.3
- [x] T062 [P] Create `tests/unit/test_runtime_imports.py`: AST/import audit that `lambda/**` deployed modules import only stdlib + `boto3` — verify: `pytest tests/unit/test_runtime_imports.py` PASSES (Constitution II)
- [ ] T063 Execute full `specs/001-pr-reviewer/quickstart.md` (a)–(n) against a deployed stack and record outcomes in its results appendix — verify: all scenarios green or documented deviations (D3; HLD §7.3 definition of done)
- [ ] T064 Run SC-008 pilot: 10-PR qualitative check that the single-comment thread is clear/non-duplicative; record results — verify: pilot notes appended to quickstart results (SC-008)
- [ ] T065 Constitution compliance review: walk Core Principles I–VII against the final diff (serverless-only BOM, stdlib+boto3, no secrets in state/logs/comments, single canonical comment, idempotent consumers, untrusted content, model-has-no-tools) — verify: review notes; violations block merge per governance (Constitution Governance)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none — start immediately.
- **Foundational (Phase 2)**: depends on Phase 1; **BLOCKS all user stories** (envelope, marker, state codec, protocol, config, logs, publication validation gate + prompt contract, BOM).
- **US1 (Phase 3)**: first story; depends only on Phase 2.
- **US2–US4 (Phases 4–6)**: depend on Phase 2 + US1's worker pipeline (T034) because they extend `worker_handler.py` wiring; their protocol tests (T036–T038, T043, T046–T047) can be written in parallel with US1 implementation.
- **US5 (Phase 7)**: depends on US1 pipeline + T024 observability file; D2 (T053) depends on T012 protocol + T040 publish paths.
- **US6 (Phase 8)**: depends on Phase 2 (prompt T018, gate T019) + US1 pipeline; T058 additionally requires a deployed review path for eval inputs.
- **Polish (Phase 9)**: depends on all stories desired for the release.

### Within Each Story

Tests first (must FAIL) → shared modules ([P] tasks) → handler wiring (sequential, same file) → integration acceptance last.

### Parallel Opportunities

- Phase 1: T003, T004 parallel with T001/T002.
- Phase 2: test-first pairs (T005/T006, T007/T008, T009/T010, T011/T012, T013/T014, T015/T016, T017/T019) and the five Terraform files (T020–T024, authored in parallel; `terraform validate` completes at T024).
- Within stories: all [P] test-creation tasks parallel; different stories' test files never collide.
- **Across stories**: sequential completion recommended for `lambda/worker_handler.py`-wiring tasks (T034, T040–T041, T044, T049, T054 share one file); with two implementers, split wiring vs. tests/modules lanes.

---

## Parallel Example: Foundational

```bash
# Independent module+test pairs (distinct files, launch together):
Task: "T005 tests/unit/test_envelope.py"        → then "T006 lambda/common/envelope.py"
Task: "T007 tests/unit/test_marker.py"          → then "T008 lambda/common/marker.py"
Task: "T009 tests/state_machine/test_state_codec.py" → then "T010 lambda/common/state.py"
Task: "T011 tests/state_machine/test_protocol.py"    → then "T012 lambda/common/protocol.py"
Task: "T013 tests/unit/test_config.py"          → then "T014 lambda/common/config.py"
Task: "T015 tests/unit/test_logs.py"            → then "T016 lambda/common/logs.py"
Task: "T017 tests/unit/test_structural_validation.py" → "T018 prompts/system_prompt.md" → then "T019 lambda/common/validate.py"
# Terraform BOM (five files, authored in parallel; validate at T024):
Task: "T020 terraform/compute.tf" … "T024 terraform/observability.tf"
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Phase 1 → Phase 2 (blocking — includes the publication validation gate, so the MVP is FR-020/FR-025-compliant from the first publish).
2. Phase 3 (US1): ingress + worker pipeline + D1 `reopened` filter.
3. **STOP and validate**: quickstart (a), (b), (e), (f), (k), (k2) — the P1 flow ships value alone.

### Incremental Delivery

US2 (comment convergence) → US3 (stale fencing) → US4 (duplicate safety) complete the correctness core; US5 adds the failure lifecycle incl. FR-028; US6 closes model-behavior verification; Polish runs the full (a)–(n) acceptance suite — the HLD §7.3 definition of done.

### Notes

- Every task cites its verification; no task is done on claim alone.
- `lambda/common/` stays the single implementation of security-sensitive helpers (marker, envelope, redaction, validation) — no duplication (HLD §7.1; Constitution).
- Secrets never appear in any file this list creates; SSM values are populated out-of-band (HLD §2.6, §7.3).

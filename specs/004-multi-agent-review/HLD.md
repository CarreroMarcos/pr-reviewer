# HLD-004: Multi-agent review stage + interactive replay site

**Status:** HARDENED SPECIFICATION (2026-09-26) — Implementation ready. Multi-agent review audit,
adversarial pass, and Mars operational rulings (Decisions 1–4) incorporated 2026-09-26.
Implementer-readiness amendments (blockers B1–B8, majors M1–M10, pinned contracts) folded in
2026-09-26 from the orchestrator review + topology simulation; unedited text preserved verbatim.
**Author:** Marcos + Vesper (hardened by Antigravity AI Engineering Directorate).

## 1. Context

The reviewer today is a single-pass pipeline: worker fetches the diff, one GLM-5.3-Flash call
(temp 0.2) reviews it, one canonical comment gets PATCHed in place.
It works and it's automatic — but one broad prompt is where hallucinations breed, and the
review process itself is invisible. Nobody can see *how* the review happened, which is a
demo gap for something meant to be resume-worthy.

## 2. Goals

1. **Measurably better reviews** via 3 non-overlapping specialist agents + a synthesizer.
   Narrow focus per agent → less hallucination surface each.
2. **A demoable replay**: an interactive, visually pleasing per-review page showing the
   agent DAG animating, each agent's reasoning, review steps, and pipeline checkpoints.
3. **Preserve what works**: exactly-one canonical comment (PATCH in place; canonical marker
   `...:{repo}#{pr}` in `common/marker.py` + the `**Review #N · updated {stamp} PT**` header in
   `common/assemble.py`),
   the `protocol.py` fencing guarantees, and the eval-gated quality bar.

## 3. Non-goals

- Replacing `protocol.py` or the delivery/fencing layer. The multi-agent stage lives
  strictly *inside* the review step.
- RAG / embeddings pipelines (ideas.md #4, bounded whole-file context, covers the need).
- Live (real-time) visualization in v1 — designed as a seam, not built.
- Changing the GitHub-facing UX. One comment, same format.

## 4. Decisions

**D1 — Orchestration: hand-rolled `asyncio` + explicit `ThreadPoolExecutor` inside worker.**
Research (2026-09-26) showed LangGraph runs on Lambda but buys nothing here: no checkpointer
is needed for a single-invocation fan-out, Studio can't observe Lambda anyway, and it would
break the stdlib-only purity for unused features. `asyncio` over specialist coroutines
+ a merge step. We own retries/timeouts — the existing error taxonomy (`is_retryable()` /
`_llm_is_retryable`, both in `lambda/worker_handler.py:253-299`) extends to cover it.
Implementation (settled 2026-09-25, hardened 2026-09-26): the repo's Constitution II (stdlib + boto3 only) is
enforced by ruff `banned-api` rules — `aiohttp`/`httpx` are lint-banned, so concurrency is
`concurrent.futures.ThreadPoolExecutor` + `loop.run_in_executor()` wrapping the existing sync `review_diff()`
(pure function, thread-safe, injected connection factory).
**CRITICAL RUNTIME FIX:** Do NOT use naive `asyncio.to_thread()`. In Python 3.12, `asyncio.run()` automatically
invokes `loop.shutdown_default_executor(300)` on exit. If a specialist coroutine times out under `wait_for` while
its underlying OS thread is still blocked on a socket read (which has a 240s read timeout), `asyncio.run`
**will hang for up to 300 seconds** during teardown! Instead, instantiate an explicit per-invocation
`ThreadPoolExecutor(max_workers=FANOUT_CONCURRENCY, thread_name_prefix=f"fanout-{run_id[:8]}")` and in the
cleanup `finally:` block call `pool.shutdown(wait=False, cancel_futures=True)`. This guarantees `asyncio.run`
exits immediately on timeout/degradation without hanging the Lambda process. (Boundary note
2026-09-26: `shutdown(wait=False, cancel_futures=True)` only drains queued, not-yet-started work — a
thread already blocked in a socket read is unaffected and would still be joined at true interpreter
exit. That is acceptable here because the Lambda container freezes rather than exits after the handler
returns, and `SOCKET_READ_TIMEOUT_S=240 < wait_for 300` bounds the abandoned thread's life. Never raise
the wait_for caps above the socket timeout.)
The sync `review()` closure calls `asyncio.run(_fanout(...))` fresh per invocation — never a cached loop.
No fire-and-forget tasks: specialist coroutines run under an `asyncio.wait(ALL_COMPLETED)` or `FIRST_COMPLETED`
loop, every task awaited inside the loop.
**Event emission point (settled):** the specialist *coroutine* — running on the single event-loop thread — emits
`agent_started` before dispatching to the executor and `agent_reasoning`/`agent_completed` as the executor call
returns; the sync worker function itself never touches the events list, which preserves the no-cross-thread-race
invariant. Consumers order events by `ts` on read/archive-write, since completion order across specialists is
nondeterministic.
**Credential Thread Safety (settled 2026-09-26 — Option 1 confirmed):** `AppConfig` is hydrated ONCE in
`worker_handler` before fan-out (`cfg = creds.current()`) and passed as an immutable snapshot to `run_fanout`.
Specialists treat configuration as read-only. If a specialist hits an HTTP 401 from Z.AI, it fails fast
(`agent_failed {error_class: "http_401"}`). When all specialists fail on 401, `run_fanout` emits
`degraded_to_single_pass {reason: "all_specialists_failed"}` and raises `FanoutDegraded`. The synchronous
`worker_handler` fallback then owns the single-threaded `creds.refresh_once()` re-hydration from SSM safely.
No locks are required inside fan-out, eliminating cross-thread race conditions by construction.

**D2 — Models: GLM only.**
His Z.AI plan makes GLM-5.3-Flash effectively unlimited, so a Bedrock migration is hassle
with no payoff — decided 2026-09-25: GLM for all agents, no Bedrock in the v1 RUNTIME path.
(Bedrock research kept on file in case a future stronger-synthesizer experiment is ever eval-gated.)
Carve-out (added 2026-09-26, bot review #5): Bedrock Titan Text Embeddings is permitted in the OFFLINE
eval harness only — `capture_multi_agent.py`, out-of-band, per the D8 Offline Embedding Rule — never
in any request-serving path.
The agent interface stays provider-neutral behind the existing `common/llm.py` port.

**D3 — Topology: 3 generator specialists + adversarial verifier + synthesizer.**
Generators (non-overlapping): **correctness**, **security**, **tests**. Each gets the diff
(via the existing `DiffResult` budget machinery), the accepted-residuals context (D7), and a
prompt scoped to its specialty with an explicit do-not-flag list. A **verifier** then tries to
*falsify* each candidate finding (reduce-only, evidence-grounded; high-severity findings it
can't verify are escalated, not dropped). The **synthesizer** merges survivors into the single
canonical comment. This mirrors Uber uReview's production shape (3 specialists + grading stage;
per the Uber engineering blog it analyzes over 90% of the weekly ~65,000 diffs landed).
The +0.08 precision bar in D8 is a Mars-set product target, not a citation-derived number — no
external precision benchmark is claimed. (A prior citation to "Wang, AAMAS 2026 +10.3pp" was
withdrawn 2026-09-26: unsourcable in the AAMAS '26 proceedings.)
The old maintainability/style specialist is dropped — lowest signal, and the verifier kills
its noise anyway. Blast-radius analysis (symbol lookup over unedited callers) is v2: it needs
real tooling (ripgrep/LSP), not pure LLM reasoning, per Stengg et al.
Inter-stage findings are untrusted input — see §5 injection hardening.

**Verifier policy (pinned 2026-09-26):** unverifiable HIGH → `escalated`. Unverifiable MEDIUM/LOW →
`killed` ONLY with a `kill_reason` citing the specific missing evidence; otherwise `escalated`.
**Falsification standard (verifier prompt contract):** a finding is confirmed only with a stated
failure mechanism plus exact diff lines; if the claimed failure is impossible under the language
runtime's guarantees (e.g. a single C-level `dict(d)` copy is atomic under the CPython GIL and can
neither raise nor tear), it is killed. Verifier output MUST re-anchor `file_path`/`line_start`/
`line_end` to the post-image file; `run_fanout` clamps-and-flags out-of-range specialist coordinates
and never echoes them silently.

**Synthesizer contract (pinned 2026-09-26; title guard amended 2026-09-26):** two survivors are
duplicates iff same `file_path` AND $|\Delta\text{line\_start}| \le 2$ AND same `category` AND
normalized-title overlap (lowercased token Jaccard ≥ 0.6, stopwords removed, pure stdlib — no runtime
embeddings; threshold pre-registered, tunable only by Phase 0 data — Constitution II holds). The title
guard exists because two DISTINCT bugs can sit within two lines of each other in the same category;
line proximity alone would wrongfully merge them (Gate 4 catches a wrongful merge only when the
merged-away finding matches the kept bullet by NEITHER match arm — different path, or same path with
|Δline| > 2 and cosine < 0.76 — a proximate same-path pair still matches by the location arm, so
`dropped_in_synthesis` stays 0. Proximate-pair protection is the title guard at runtime plus
adjudication: any merged bullet subsuming same-location manifest defects from distinct survivors is
flagged for human adjudication of whether both mechanisms survived in the text).
Cross-category same-location findings (e.g. a tests-coverage gap and the correctness bug it covers) —
and same-location pairs failing the title guard — are BOTH kept and adjacent-ordered. Merge duplicates
keeping the highest severity. Output MUST be a
`## Findings` section with one `- [SEVERITY] path:line — title. Description… Fix: …` bullet per
survivor (verified and escalated alike; escalated carry `[Requires Verification]`), so
`score_output`/`aggregate` run byte-identical. `dropped_as_duplicate_n` = survivors − bullets;
invented = bullets with no survivor; both gated at 0 (D8 gate 4).

### Specialist Scope Definitions & Do-Not-Flag Rules (Pinned Contract)

#### 1. Correctness Specialist
- **Scope:** Identifies functional and algorithmic bugs introduced in modified lines: unhandled edge cases,
  broken state transitions, null/None dereferences, index/off-by-one errors, race conditions corrupting internal state,
  unhandled exceptions, data structure invariants, and resource/connection leaks.
- **Do-Not-Flag List:**
  1. Security vulnerabilities (injection, auth, crypto, secrets). Reserved strictly for Security.
  2. Test omissions or test fixture design. Reserved strictly for Tests.
  3. Linter complaints, formatting, variable naming style, comments, or docstrings.
  4. Bugs in unchanged, pre-existing code outside the diff hunk.
  5. Accepted residuals previously reviewed and recorded in PR metadata.

#### 2. Security Specialist
- **Scope:** Identifies exploitable vulnerabilities and trust boundary violations introduced or altered in the diff:
  injection flaws (SQLi, command, template), auth/authz bypasses, secret/token leaks, path traversal, SSRF, XSS,
  insecure deserialization, and TOCTOU races. Every finding must demonstrate an exploitable path or violation of security posture.
- **Do-Not-Flag List:**
  1. Functional bugs, business logic errors, or calculation flaws with no security impact. Reserved for Correctness.
  2. Absence of unit tests or test framework configurations. Reserved for Tests.
  3. Low-entropy mock credentials or public non-secret tokens in test fixtures.
  4. Code formatting, style, or micro-optimizations.
  5. Accepted residuals.

#### 3. Tests Specialist
- **Scope:** Evaluates automated test coverage and assertion quality for modified code. Identifies untested branches,
  tautological assertions (`assert True`, asserting mock without verifying calls), brittle tests dependent on system clock
  or execution order, and test state pollution.
- **Do-Not-Flag List:**
  1. Implementation bugs in production application code. Reserved for Correctness.
  2. Production security vulnerabilities. Reserved for Security.
  3. Demanding tests for trivial boilerplate (e.g. pure constants, simple DTOs).
  4. Test styling or naming conventions.
  5. Accepted residuals.

**D4 — Replay first, live as a seam: one versioned event schema.**
The worker emits structured stage events for every run. v1 consumers: the replay site
(reads archived events). v2 consumer (future): a WebSocket feed pushing the *same*
events live. The schema is the seam — design it once.

**D5 — Surface: bespoke webpage (S3 + viewer Lambda in v1), not X-Ray.**
X-Ray is ops tooling (answers "why was this slow"); a custom page is the portfolio piece
and gives full aesthetic control. v1 has NO CloudFront distribution — S3 + viewer Lambda
only (see §7; a pre-public distribution without OAC would bypass bearer auth).
X-Ray tracing can be added later for latency debugging without touching this design.

**D6 — Thinking: enabled + `reasoning_effort`; truncate at capture time, default 4000 chars.**
**[DECISION — CONFIRMED by Marcos 2026-09-26]**: send `thinking: {"type": "enabled"}` explicitly
on all GLM-5.3 calls, with `reasoning_effort`: `low` for specialists confirmed
(higher for verifier/synthesizer) — but NOT a mandate. Phase 0 (§8) measures **both**
default effort and `low`; the D8 effort-selection rule then picks the ship config from the
measured p95 comparison — pinned in D8 (added 2026-09-26, gate 5). Rationale:
the flagship demo goal (§2, goal 2) is visualizing agent reasoning — but do NOT just drop the
param: omitting `reasoning_effort` silently defaults to `max` (~105 reasoning tokens median
on a trivial prompt vs ~3 at `low`, measured Aug 2026) — the worst case. `low`
approximates the old `disabled` cost floor (~3 tokens) while staying spec-compliant
everywhere. What changes: thinking time joins the latency profile and is unmeasured —
Phase 0 (§8) measures it before D9's numbers are finalized.
Replay UI renders reasoning *when present* rather than assuming it always exists.
Capture rule: `REASONING_MAX_CHARS` applies inside the specialist coroutine the moment the
reasoning string arrives — before it touches the events list, the verifier prompt, or the
synthesizer prompt. The emitted field is named **`reasoning_excerpt`** (not "summary"):
it is the raw reasoning truncated at capture — **there is no LLM summarization call**
(no 6th call per review; nothing in D9's budget covers one). Default 4000 chars, tunable up via env var.

**D7 — Accepted-residuals as shared context.**
ideas.md #5 becomes infrastructure for the multi-agent stage: all specialists receive the
settled residuals so nobody re-flags decided nits. Kills a known noise source *and*
shrinks per-agent prompt waste.
**Prerequisite status (2026-09-26):** residuals infrastructure is entirely NEW — only a prose
proposal exists (`docs/ideas.md:27`); no parsing, storage, or prompt-injection code exists today.
It must land as its own prerequisite task, or specialists receive `residuals = []` indefinitely.

**D8 — Eval gate (operationalized and hardened 2026-09-26).**
End-to-end A/B: the scorer grades the final comment's `## Findings`
section, and the synthesizer emits the same format — so `score_output`/`aggregate` stay
byte-identical, no re-labeling, existing 13 seeded cases reused as-is. (Vocabulary pin 2026-09-26: the
on-disk CORPUS registry has 15 entries = 13 SCORED cases + 2 robustness; "13 seeded" always means the
scored set — do not conflate the two numbers.) New per-stage
metrics on a separate `pinned_multi_agent.json` pin (never touch the single-pass
pin/drift-guard chain).
Corpus: **15 = 13 seeded + 2 robustness**, growing to 24: +3 tests-gap cases (the
current corpus has ZERO tests-category coverage), +4 verifier-trap cases (plausible-but-
false findings; one of the four is a prompt-injection case), +2 subtle-true cases
(wrongful-kill guards).
**Total ground truth positive defects ($N$) = 18 defects across 24 cases.** Corpus pin (added
2026-09-26, bot review #7): the five gates are evaluated on the grown 24-case / 18-defect corpus;
the current on-disk 15-case corpus (13 scored + 2 robustness, defect count not yet frozen) is
Phase 0 calibration only — interim runs report metrics but render no gate verdicts.
**Replication:** 3 runs/case per pipeline; report mean ± range; gates evaluated on means.

Five hard gates, paired case-by-case vs the single-pass pin:
1. **recall — no regression, ever:** let $d$ = mean recall_multi − mean recall_single (means over 3
   runs/case; the noise band reflects one missed defect in an 18-defect corpus, $1/18 = 0.0556$; the
   −0.06 bound is −1/18 ≈ −0.0556 rounded outward, so exactly one missed defect earns a re-run rather
   than failing outright).
   $d \ge 0$ → pass outright. $-0.06 \le d < 0$ → exactly one full re-run of the comparison; pass iff
   $(d_1 + d_2)/2 \ge 0$. $d < -0.06$ → fail on the first measurement.
2. **precision:** let $p$ = mean precision_multi − mean precision_single. Bar = +0.08 — the re-run
   midpoint threshold, i.e. the number the re-run rule below compares against (target +0.10 is the
   Mars-set product aspiration, not a gate bound — see D3). $p \ge 0.15$ → pass outright. $p < 0.05$
   → fail outright. $0.05 \le p < 0.15$ → exactly one full re-run; pass iff $(p_1 + p_2)/2 \ge 0.08$.
3. **wrongful kills:** match rule pre-registered — a killed candidate matches a manifest
   finding iff location matches ($|\Delta\text{line}| \le 2$ on same path) OR embedding cosine ≥ 0.76
   (using Amazon Bedrock Titan Text Embeddings `amazon.titan-embed-text-v2:0` pinned offline in
   the eval harness; 0.76 is an app-tuned, pre-registered threshold, not a model property). Human
   adjudication applies ONLY to ties/ambiguities flagged by the deterministic rule — the gate itself
   is computed deterministically, and a run with zero deterministic matches passes without human input.
   Gate: **0 wrongful kills across all runs** (all cases × 3 runs). The 2 subtle-true cases MUST
   survive as verified-or-escalated (never killed) in all runs.
4. **synthesizer fidelity:** dropped_in_synthesis = a verifier-survived finding with no
   semantic match (same matching rule) in the final comment's `## Findings`;
   invented_in_synthesis = a `## Findings` entry with no semantic match to any verified or
   escalated finding. Gate: **0 on both**.
5. **existing floors hold:** fabricated = 0, both robustness cases pass.

**Effort-selection rule (pinned 2026-09-26, gate 5 — the rule D6/Q7/§8 point to):** ship
`reasoning_effort: low` iff all five gates above pass with `low` AND the measured p95 latency under
`low` is ≤ the default-effort p95; otherwise the ship config is a Mars ruling from the Phase 0 data —
never a silent infrastructure default.

**Offline Embedding Rule (Constitution II):** External ML libraries (`torch`, `sentence-transformers`,
`numpy`, `scipy`) are strictly banned by `pyproject.toml`. Pytest in CI must run 100% offline without network.
Embedding vectors are generated exclusively during out-of-band capture (`capture_multi_agent.py`) via
`boto3.client("bedrock-runtime")` calling Titan Text Embeddings v2 in `us-west-2`, and serialized into
`pinned_multi_agent.json` — for manifest findings AND for every candidate and verifier-killed finding
text of each captured run: the wrongful-kill rule scores the killed side's vector, so the cosine arm
is unscorable without it (amended 2026-09-26, bot review #2). Cosine similarity is computed in pure
Python stdlib during offline scoring.
**Trap Validation Protocol:** Trap vetting is a Phase 0 calibration task using a candidate pool of 6 traps
until 4 are frozen. If a generator specialist avoids a trap pre-verifier, the multi-agent pipeline is awarded
a pass for that run (source-level avoidance); generator prompts must **never** be degraded to force hallucinations.
**True A/B Call Count & Wall Time:** Multi-agent executes 5 calls per run (3 specialists + 1 verifier + 1 synthesizer)
$\times 24$ cases $\times 3$ runs = 360 calls. Re-baselining single-pass requires 72 calls.
Total: **432 GLM calls**. Purely sequential this is ~28.8h at ~240s/call; because the wave parallelizes
(FANOUT_CONCURRENCY=3), realistic wall-clock is ~3×240s per multi-agent case-run plus ~240s per
single-pass case-run ≈ **~19.2h** — state the assumed parallelism beside any wall-clock claim.
Harness must be designed for an unattended terminal run with checkpointed resume per case.

**D9 — Concurrency & Timeout Budgets (Hardened 2026-09-26).**
Measured (2026-09-25, coding-plan endpoint): a trivial 1-word prompt costs **~240s wall**
(server-side queueing, near-identical across batches). Ramp: N=1/2/3 all-200; N=5 → one
429 (code 1302) after ~240s of waiting — never ride the limit. Effective tier ≈ 4 concurrent;
production cap = 3.

### Concurrency Resolution: Option A Confirmed (Mars Ruling 2026-09-26)
AWS Lambda EventSourceMapping API rejects `maximum_concurrency = 1` (the AWS API minimum is **2**).
Furthermore, AWS regional concurrency rules enforce $\ge 10$ unreserved executions account-wide;
this account's total regional concurrency limit is 10, so reserving even 1 execution for the worker
would leave only 9 unreserved and `terraform apply` fails. (No `reserved_concurrent_executions`
resource exists in `compute.tf` today; stale cite `compute.tf:76-80` removed 2026-09-26 — that range
is a comment about a dropped reservation, not a setting.)
**Resolution:**
1. Worker Lambda remains **unreserved** in Terraform (`compute.tf`).
2. SQS ESM is to be configured with `scaling_config { maximum_concurrency = 2 }` (instructional — the
   live ESM does not set it yet; checklist item 3 carries the action — gate 5 tense fix).
3. **Application-Level Distributed Mutex:** To strictly serialize multi-agent reviews and prevent two
   workers from simultaneously making 6 concurrent requests to Z.AI (tripping the 1302 cap), the worker
   implements a lightweight DynamoDB mutex early in the invocation (acquired after
   validate/hydrate/establish and before any Z.AI call — see the §5 flow):
   - Mutex row lives in the existing state table: `pk = "mutex:pr-reviewer-worker"`, attributes
     `owner = delivery_guid`, `lease_until = epoch_s`, `token = uuid4`.
   - Acquire = conditional write succeeding iff `attribute_not_exists(pk) OR lease_until < now - 30`
     (30s clock-skew margin). The holder refreshes the lease at 50% TTL while fan-out is active; the
     refresh is a conditional write on the stored `token` — same condition family as release (added
     2026-09-26, bot review #7) — writing `lease_until = now + MUTEX_LEASE_TTL_S`; a failed refresh
     means ownership was lost to expiry takeover, and the holder MUST immediately stop issuing LLM
     work and take the degraded path (it never re-asserts a lease it no longer owns). Expiry takeover
     (acquire's second arm) writes the SAME attribute shape as acquire — `owner`,
     `lease_until = now + MUTEX_LEASE_TTL_S`, fresh `token = uuid4` (gate 5). Worst-case fan-out (per
     the elapsed-budget gates below) MUST stay within the refreshed lease so it can never expire
     mid-run and admit a second fan-out.
   - **Lease TTL (pinned 2026-09-26):** `MUTEX_LEASE_TTL_S = 900` — equal to the worker Lambda's hard
     timeout (`terraform/compute.tf:104`, the AWS maximum). No live invocation can outlive its lease,
     so expiry takeover recovers only genuinely dead holders; the 50% refresh (450s) is retained as
     belt-and-braces against clock skew. A dead holder delays a contender by at most one TTL, and the
     contender path below never blocks on the lease. A throttled or lost release `REMOVE` is the same
     class as a crash — expiry takeover recovers it, degradation is bounded at one TTL, and the
     contention path keeps publishing single-pass reviews throughout; a contention-rate alarm is a
     Phase 1 operability item, not a correctness gate (bot review #6, accepted).
   - Release = `REMOVE` conditioned on the stored `token` (only the holder can release; crash recovery
     is expiry takeover). Count 2–3 WCUs per review against the base-table budget in §7.
   - **Contention path (fixed 2026-09-26):** if another worker holds the lock, the arriving worker does
     NOT defer and does NOT touch message visibility — with SQS Lambda event source mappings a
     successful return deletes the message regardless of any `change_message_visibility` call, so a
     visibility-based deferral silently loses the review (and raising for redelivery would burn
     `maxReceiveCount` toward the DLQ while the holder still runs). Instead the contender executes the
     existing single-pass path inline (1 LLM call), emits `concurrency_single_pass {reason:
     "mutex_held"}`, and completes normally. The contender's budget decision is its OWN predicate —
     the CONTENDER VIABILITY CHECK, distinct from elapsed-budget gate 4 (which governs the single-pass
     fallback inside a holder's run; the two can disagree where gate 4 passes but the clamp sits below
     the floor — bot review #9). The clamp `remaining − BUDGET_MARGIN_S − ~90s fixed overhead`
     is compared against a VIABILITY FLOOR pinned from the Phase 0 per-call p95 (provisionally the
     measured ~240s server-side queue latency, pre-Phase-0). Below the floor the call is SKIPPED —
     fail-fast by design: a socket budget under the measured queue latency cannot succeed (bot
     review #6; the earlier unconditional 30s-floor attempt was guaranteed-dead code). The run emits
     `concurrency_single_pass {reason: "mutex_held_no_budget"}`, classifies transient, and follows
     the existing re-raise/notice path — bounded by `maxReceiveCount`, publishing nothing; SQS
     redelivery is the recovery. At or above the floor it attempts one single-pass with the clamped
     value FORWARDED as `read_timeout_s` — computed once, used for both the check and the socket
     (bot review #4).
   - Release timing (order pinned 2026-09-26, gate 5 / bot review #9): the lease is released
     immediately after the holder's LAST lease-covered LLM call completes (the full run_fanout —
     wave, verifier, and synthesizer stages — or its own degraded single-pass) and BEFORE the
     claim/fence/publish/finalize sequence — publication
     is owner-guarded, not mutex-protected, so holding the lease through publish would only delay
     contenders; the lease is never held while no holder-initiated call is in flight. The Phase 0
     post-publish shadow specialist is NOT lease-covered; release precedes it: shadow and fan-out
     never co-occur — `MULTI_AGENT=1` ignores `MULTI_AGENT_PHASE0` (flag precedence), so the Phase-0
     concurrency ceiling is 2 regardless.
   **Concurrency ceiling (amended 2026-09-26; supersedes the earlier "never exceed 3" claim):**
   $\le 1$ worker runs fan-out at any instant, but total concurrent calls to `api.z.ai` reach **4** in
   the contention window — 3 fan-out calls plus a contender single-pass still in flight when the lease
   changes hands (the contender holds no lease, so the holder cannot see its call). 4 sits AT the
   measured tier edge (N=5 → 429/1302; N=4 was never explicitly measured). **Phase 0 MUST measure N=4
   explicitly before Phase 1**; if N=4 trips, the contention path queues behind the lease (bounded by
   `SINGLE_PASS_WAIT_FOR_S`) instead of running inline, restoring a hard ceiling of 3.
   **Scope pin (2026-09-26):** the mutex serializes the LLM phase only — publication is NOT
   mutex-protected. A contender's single-pass can reach publish while the holder sits between its
   last LLM call and its publish; that race belongs to the existing claim → live-head fence →
   publish (PATCH) → finalize owner-guard, where the loser lands in `PUBLISHED_FINALIZE_CONFLICT`
   and reconcile converges — identical to same-head redeliveries today. No mutex re-check is added
   at publish.

**Per-PR claim lease vs. review budget (gate-traced 2026-09-26, PR #88 Oracle Q1):** the current
single-pass system's claim row uses `CLAIM_LEASE_SECONDS=180`, predating the 240s socket-read budget.
At 240s-class reviews the claim conditional's expired-self branch always fires at claim time (the
self-lease has lapsed) — converge-safe via claim-lease refresh (the expired-self branch rewrites
`claim_until`; the term "re-anchor" is avoided here because, since PR #91, it names the attempt-clock
fix — gate 5). The residual is the steal window between lease
expiry and claim: a same-head second delivery may claim and publish concurrently; the finalize
owner-guard forces the loser to `PUBLISHED_FINALIZE_CONFLICT`, and reconcile converges. Multi-agent
reviews are 780s-class, widening that window ~4×. **Standing remediation (Phase 1 prerequisite):**
raise `CLAIM_LEASE_SECONDS` to ≥ the review budget, or refresh the claim row mid-review. Not blocking
today; recorded so the widening is a decision, not a surprise.

### Elapsed-Budget Gate (Cumulative Downstream Protection)
To prevent the catastrophic failure mode where the Verifier runs, burns the budget, and leaves the pipeline
with insufficient time to synthesize or fall back to single-pass (publishing NO comment), the elapsed-budget
gate checks **all remaining downstream stages to publication**:
1. **Before Stage 1 (Wave):**
   `remaining_ms >= (WAVE_WAIT_FOR_S + VERIFIER_WAIT_FOR_S + SYNTHESIZER_WAIT_FOR_S + BUDGET_MARGIN_S) * 1000`
2. **Before Stage 2 (Verifier):**
   `remaining_ms >= (VERIFIER_WAIT_FOR_S + SYNTHESIZER_WAIT_FOR_S + BUDGET_MARGIN_S) * 1000`
   *If false $\to$ immediately degrade to single-pass fallback while single-pass has budget!*
3. **Before Stage 3 (Synthesizer):**
   `remaining_ms >= (SYNTHESIZER_WAIT_FOR_S + BUDGET_MARGIN_S) * 1000`
4. **Before Single-Pass Fallback:**
   `remaining_ms >= (SINGLE_PASS_WAIT_FOR_S + BUDGET_MARGIN_S) * 1000`
Provisional caps (amended 2026-09-26): `WAVE_WAIT_FOR_S=300`, `VERIFIER_WAIT_FOR_S=240`,
`SYNTHESIZER_WAIT_FOR_S=180`, `SINGLE_PASS_WAIT_FOR_S=240`, `BUDGET_MARGIN_S=60` — gate-1 total 780s,
tuned in Phase 0. The original "300s each + 60s margin" (960s) could NEVER pass: the worker Lambda
timeout is 900s (`terraform/compute.tf:104`) — the AWS maximum, it cannot be raised — so every gate
total MUST fit within 900s − ~90s fixed claim/fence/publish/finalize/archive overhead ≈ 810s.
**Risk note:** at the measured ~240s/call Z.AI queue latency, per-stage headroom is ~30s. If Phase 0
measures sustained per-call latency above ~250s, multi-agent degrades to single-pass on most runs;
the Phase 0 exit criteria (§8) must quantify this BEFORE Phase 1.

### Partial-Success & Retry-Ownership Rules (added 2026-09-26)
- The wave succeeds iff ≥2 specialists return parseable findings within `WAVE_WAIT_FOR_S`. A 429/1302
  failure is never retried in-executor: fail fast to survivors. 0–1 survivors, verifier timeout, or
  synthesizer timeout → emit the corresponding `*_failed` event and raise `FanoutDegraded`.
- The executor retries NOTHING (amended 2026-09-26, bot review #2: an earlier draft allowed one
  immediate retry of a socket read timeout inside the wait window — dead code, since a timeout fires
  at t≈240s under `wait_for(300)` and the retry would hold ≤60s of window against a 240s budget).
  Specialist calls are single-attempt; a timed-out specialist is lost to the wave and the
  ≥2-survivor rule absorbs one loss. All retry ownership stays with the SQS queue. `agent_retry`
  is reserved in the event schema but not emitted on the fan-out path in v1.

## 5. Architecture

```
SQS → worker (existing: validate → hydrate → establish → acquire mutex lease → fetch diff)
  → worker_handler: emit review_started {pr, sha, diff_stats}
  → empty diff? → emit review_skipped {reason: "empty_diff"} → existing empty-diff handling
  → NEW: run_fanout(diff_result, residuals, cfg, context) -> str   # raises FanoutDegraded(reason, failed_stage)
  │     ├─ cumulative budget gate: remaining ≥ wave + verifier + synth + margin
  │     ├─ Custom ThreadPoolExecutor (max_workers=3) over specialist coroutines:
  │     │     specialist("correctness") → findings[] + reasoning_excerpt   (coroutine emits
  │     │     specialist("security")    → findings[] + reasoning_excerpt    agent_started /
  │     │     specialist("tests")       → findings[] + reasoning_excerpt    agent_reasoning /
  │     │                                                                  agent_completed
  │     │     # socket read timeout 240s; wait_for 300s; fast-fail to survivors on 429/1302
  │     │     # executor shutdown(wait=False, cancel_futures=True) in finally:
  │     ├─ cumulative budget gate: remaining ≥ verifier + synth + margin (else degrade to single-pass NOW)
  │     ├─ verifier(candidate_findings) → verified[] + killed[] + escalated[]
  │     │     # reduce-only, evidence-grounded; high-severity unverifiable → escalated
  │     ├─ emit: verification_done / verification_failed
  │     ├─ cumulative budget gate: remaining ≥ synth + margin (else degrade to single-pass NOW)
  │     ├─ synthesizer(verified, escalated, residuals) → comment body
  │     │     # span-preserving markdown sanitization before PATCH
  │     ├─ emit: review_synthesized / synthesizer_failed
  │     └─ on total fan-out failure: emit degraded_to_single_pass {reason} → raise FanoutDegraded
  → review closure catches FanoutDegraded → budget gate → single-pass inline (NEVER propagates to
     run_review or the worker boundary — see wiring pin below)
  → release mutex lease (immediately after the last lease-covered LLM call, BEFORE claim/fence/
    publish/finalize — publication is owner-guarded, not mutex-protected — gate 5 / bot review #9)
  → existing: claim → live-head fence → publish (PATCH) → finalize
  → Phase 0 only: shadow correctness specialist (post-publish, post-release; NOT lease-covered — gate 5)
  → run archive → S3 (events.jsonl + meta.json, written AFTER finalize AND after any shadow call —
    archiving before shadow completion silently drops shadow events from the run)
  → DDB index row (best-effort; GSI pr_number + started_ts)
```

### FanoutDegraded Wiring (load-bearing pin, added 2026-09-26)
`FanoutDegraded` is caught INSIDE the review closure passed to `run_review` (around `run_fanout` only);
the closure then runs the budget gate and the existing single-pass LLM call inline and returns its
content. `FanoutDegraded` MUST NEVER propagate to `run_review` or the worker boundary:
`is_retryable()` (`worker_handler.py:278-299`) returns True for unknown faults, so an escaped
`FanoutDegraded` triggers SQS redelivery instead of single-pass fallback, plus a spurious D2 transient
notice. A state-machine test MUST prove the containment, and MUST pin the mutex release ordering (release
after the last lease-covered LLM call, before claim/publish/finalize — bot review #9).

### Injection Hardening & Sanitization Specifications

1. **Dynamic Delimiter Noncing:** To prevent an attacker from breaking parsing boundaries by including
   literal delimiter strings inside the PR diff or description, `run_fanout` generates a cryptographically
   random 64-bit hex nonce (`nonce = secrets.token_hex(8)`). Delimiters between stages bind this nonce:
   `<<<CANDIDATE_FINDINGS nonce="{nonce}">>> ... <<<END_CANDIDATE_FINDINGS nonce="{nonce}">>>`
   Delimiters are per-stage and nonced: each stage boundary (wave → verifier → synthesizer) generates
   a fresh nonce, and verifier/synthesizer prompts use a DIFFERENT nonce than the wave. Invariant
   (reworded 2026-09-26, bot review #5 — the earlier "must not appear more than once" phrasing was
   violated by the format itself): a stage nonce appears EXACTLY TWICE in the model-visible prompt —
   the opening and closing tag of its own block — and is never reused across stages or prompts; any
   OTHER occurrence of the nonce (in a finding field, in the diff echo, in reasoning text) is a
   boundary-break signal. The diff itself is passed VERBATIM inside
   fenced blocks — never escaped or mutated (a correctness tool must not review altered text; the
   original `replace("<<<", "<\\<<")` escaping was withdrawn 2026-09-26 for exactly that reason).
   The prompt-injection trap case passes iff the trap finding is killed without the nonce appearing in
   any finding field.
2. **Span-Preserving Markdown Sanitization:** Naive regex neutralization of `<...>` or `[...]` corrupts
   generic type signatures (e.g. `List<T>`, `Dict[str, Any]`), JSX tags, and code blocks.
   The sanitizer must extract and stash fenced code blocks (```` ```...``` ````) and inline backtick spans (`` `...` ``)
   into indexed placeholders (`\x00CODE_SPAN_N\x00`), neutralize active web elements in prose
   (`![img](url)` $\to$ `[Image: img] (url)`, `[link](url)` $\to$ `link (url)`, `<http...>` $\to$ `` `<http...>` ``),
   and re-substitute the original code spans.
3. **Escalated Findings Presentation (amended 2026-09-26):** Verifier-escalated findings render at their
   ORIGINAL candidate severity — the verifier policy escalates unverifiable HIGH, and unverifiable
   MEDIUM/LOW absent a `kill_reason`, so stamping every escalation `[HIGH]` silently inflates severity
   in the published comment. Escalated findings carry an explicit verification warning:
   `- [<original severity>] path/file.py:42 — [Requires Verification] Description... Fix: ...`
   This guarantees compliance with Gate 4 (`dropped_in_synthesis = 0`) while maintaining complete developer transparency.
4. **Reasoning Excerpts Are Untrusted Inter-Stage Data (added 2026-09-26):** `reasoning_excerpt` is
   model-controlled free text (≤ `REASONING_MAX_CHARS`) and reaches verifier and synthesizer prompts —
   an injection channel exactly like candidate findings. Excerpts enter downstream prompts ONLY inside
   per-stage nonced delimiter blocks
   (`<<<SPECIALIST_REASONING nonce="{nonce}">>> ... <<<END_SPECIALIST_REASONING nonce="{nonce}">>>`)
   generated under item 1's rules; excerpt text is never spliced into prompt prose, instructions, or
   structured fields outside those blocks.

### S3 Archive Retention & Local Sync (Mars Ruling 2026-09-26)
- **S3 Lifecycle:** Transitioning KB-scale files to Glacier Instant Retrieval triggers a 128 KB minimum billable size
  penalty (12x cost inflation) and request fees. The S3 Lifecycle rule is configured as **Expiration (Delete) at 90 days**
  on the `runs/` prefix.
- **Local Machine Archive Sync:** To retain the complete historical archive for local evaluation, replaying, and
  corpus building on this development box without paying cloud storage or busting the free tier, a documented
  runbook command — NOT a tracked script (no new top-level directories; AGENTS.md layout law; the prior
  `tools/sync_archives.py` proposal was withdrawn 2026-09-26) — pulls new runs from S3 to local storage
  (`~/.pr-reviewer/archives/`) before the 90-day cloud expiration deletes them:
  `aws s3 sync s3://pr-reviewer-archives/runs/ ~/.pr-reviewer/archives/ --exclude "*" --include "*.jsonl" --include "*.json"`

## 6. Event Schema & Data Contracts

Every event: `{v: 1, run_id, ts, type, ...}`.

| type | emitted when | key fields |
|---|---|---|
| `review_started` | review step begins (worker_handler) | pr, sha, diff_stats {files, additions, deletions} |
| `review_skipped` | fan-out skipped (worker_handler) | reason ∈ {empty_diff, phase0_no_budget}, pr, sha |
| `checkpoint` | pipeline stage reached | stage ∈ {established, diff_fetched, claimed, published, finalized} |
| `agent_started` | specialist coroutine begins | specialty |
| `agent_reasoning` | reasoning excerpt available (coroutine emits as executor returns) | specialty, reasoning_excerpt (raw reasoning truncated at `REASONING_MAX_CHARS`) |
| `agent_completed` | specialist returns | specialty, findings_n, latency_ms, tokens_in/out, findings[] (JSON) |
| `agent_retry` | fast-failing retryable error retried | specialty, attempt, error_code, backoff_ms |
| `agent_failed` | specialist raised/timed out | specialty, error_class, latency_ms |
| `verification_done` | verifier returns | survived_n, killed_n, escalated_n, wave_survivors (int — specialists returning parseable findings, e.g. 2 after one 429/timeout loss; lets replay/eval attribute recall deltas to partial waves — bot review #4), latency_ms, tokens_in/out, verified[]/killed[]/escalated[] (JSON) |
| `verification_failed` | verifier raised/timed out | error_class, latency_ms |
| `review_synthesized` | synthesizer returns | findings_merged_n, dropped_as_duplicate_n, latency_ms, tokens_in/out, findings[] (merged JSON) |
| `synthesizer_failed` | synthesizer raised/timed out | error_class, latency_ms |
| `degraded_to_single_pass` | fan-out abandoned for single-pass | reason, failed_stage |
| `concurrency_single_pass` | mutex held; contender executes single-pass inline (no deferral) | reason: "mutex_held", elapsed_ms |
| `degraded_no_budget` | no budget left even for fallback (terminal) | reason, elapsed_ms |
| `review_published` | canonical comment PATCHed | comment_id |

### Coordinate Space & Candidate Identity (pinned 2026-09-26)
- `line_start`/`line_end` are 1-based lines in the POST-IMAGE file (the file as it exists after the PR
  applies), NOT diff-relative positions. Simulation evidence 2026-09-26: 2 of 3 specialists emitted
  diff-relative coordinates when unpinned. `run_fanout` validates ranges against post-image file length;
  out-of-range specialist coordinates are clamped-and-flagged, and the verifier re-anchors coordinates
  against the diff.
- `candidate_id` is assigned by `run_fanout` immediately after the wave: `"{specialty}:{index}"`
  (index = position in that specialist's findings array, stable per run). It is deliberately NOT part of
  the specialist output schema — specialists never see or generate IDs. The verifier prompt carries the
  IDs verbatim; verifier output MUST echo assigned IDs and MUST NOT invent new ones; unknown IDs in
  verifier output are a `verification_failed` error, not silently dropped.

### Archive & Index Contracts (pinned 2026-09-26)
- `meta.json` = `{v: 1, run_id (uuid4 hex), pr, sha, pipeline: "multi_agent"|"single_pass"|"phase0_shadow",
  status, started_ts, finished_ts, archive_version: 1}` (timestamps epoch ms). `run_id` is `uuid4().hex`
  — 32 lowercase hex chars, no dashes — in the S3 key, meta.json, and index alike (amended 2026-09-26,
  gate 5: the route regex previously required a dashed 36-char shape no generated run_id could match).
- `ts` on every event is epoch milliseconds (integer); consumers order by `ts`.
- The DDB index row is written best-effort AFTER finalize by `worker_handler` (never inside the review
  callback); `status ∈ {published, degraded_single_pass, failed}`.
- Status mapping per pipeline (added 2026-09-26, gate 5): `multi_agent` → published |
  degraded_single_pass | failed by outcome; `single_pass` (fallback, contender, and skipped-shadow
  Phase 0 runs) → published | failed; `phase0_shadow` → published | failed — status describes the
  REVIEW outcome (the single-pass publish that precedes the shadow), `pipeline` is the discriminator,
  and shadow telemetry lives in events. `review_skipped {reason: "phase0_no_budget"}` is an EVENT,
  never a row class — it writes no index row.
- Single-pass fallback emits `review_started`, `degraded_to_single_pass`, `checkpoint {published,
  finalized}`, `review_published` — never `agent_*`/`verification_*` — so every archived run renders on
  the replay site.

### Candidate Finding JSON Schema (Specialists Output)
```json
{
  "type": "object",
  "required": ["findings"],
  "properties": {
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["file_path", "line_start", "line_end", "title", "description", "suggested_fix", "severity", "category"],
        "properties": {
          "file_path": { "type": "string" },
          "line_start": { "type": "integer", "minimum": 1 },
          "line_end": { "type": "integer", "minimum": 1 },
          "title": { "type": "string", "maxLength": 120 },
          "description": { "type": "string" },
          "suggested_fix": { "type": "string" },
          "severity": { "type": "string", "enum": ["HIGH", "MEDIUM", "LOW"] },
          "category": { "type": "string", "enum": ["correctness", "security", "tests"] }
        },
        "additionalProperties": false
      }
    }
  },
  "additionalProperties": false
}
```

### Verifier Output JSON Schema
```json
{
  "type": "object",
  "required": ["verified", "killed", "escalated"],
  "properties": {
    "verified": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["candidate_id", "file_path", "line_start", "line_end", "title", "description", "suggested_fix", "severity", "category", "verification_note"],
        "properties": {
          "candidate_id": {"type": "string"}, "file_path": {"type": "string"},
          "line_start": {"type": "integer"}, "line_end": {"type": "integer"},
          "title": {"type": "string"}, "description": {"type": "string"},
          "suggested_fix": {"type": "string"}, "severity": {"type": "string"},
          "category": {"type": "string"}, "verification_note": {"type": "string"}
        },
        "additionalProperties": false
      }
    },
    "killed": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["candidate_id", "kill_reason"],
        "properties": {
          "candidate_id": {"type": "string"}, "kill_reason": {"type": "string"}
        },
        "additionalProperties": false
      }
    },
    "escalated": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["candidate_id", "file_path", "line_start", "line_end", "title", "description", "suggested_fix", "severity", "category", "escalation_reason"],
        "properties": {
          "candidate_id": {"type": "string"}, "file_path": {"type": "string"},
          "line_start": {"type": "integer"}, "line_end": {"type": "integer"},
          "title": {"type": "string"}, "description": {"type": "string"},
          "suggested_fix": {"type": "string"}, "severity": {"type": "string"},
          "category": {"type": "string"}, "escalation_reason": {"type": "string"}
        },
        "additionalProperties": false
      }
    }
  },
  "additionalProperties": false
}
```

Verifier output is CLOSED-SCHEMA (added 2026-09-26, gate 5 / bot review #9): `run_fanout` rejects any
item carrying a field outside its `properties` — unknown fields are a `verification_failed` error,
never silently forwarded into the synthesizer prompt.

## 7. Replay Site & Viewer Architecture

- Static assets + archives served by a **viewer Lambda behind a Function URL** (`authorization_type = "NONE"`).
  **Oct-2025 Hardening:** Requires the explicit `lambda:InvokeFunction` permission statement gated on
  `lambda:InvokedViaFunctionUrl = true`. No viewer resources exist yet (`compute.tf:126-135` is an
  ingress-URL note — stale cite removed 2026-09-26); per AWS docs the NONE-auth Function URL pattern needs
  BOTH statements: `lambda:InvokeFunctionUrl` (Principal `*`, condition `lambda:FunctionUrlAuthType = NONE`)
  and `lambda:InvokeFunction` (Principal `*`, condition `lambda:InvokedViaFunctionUrl = true`).

### Routing Specification
```
[Function URL GET Request]
        │
        ├── Path: `/runs/{pr}/{sha}/`
        │         └── Returns: `static/index.html` (Unauthenticated shell)
        │
        ├── Path: `/static/{file}`
        │         └── Regex: `^static/[A-Za-z0-9._-]+$` AND explicit dot-segment rejection: the viewer
        │             normalizes the path and rejects with 404 any segment resolving to `.` or `..`
        │             BEFORE constructing the S3 key (the character class alone admits `static/..`;
        │             added 2026-09-26, bot review #3)
        │         └── Returns: S3 `static/{file}` (Unauthenticated CSS/JS)
        │
        ├── Path: `/api/runs/{pr}/latest` (Index Query)
        │         └── Headers: `Authorization: Bearer <token>` (REQUIRED)
        │         └── Queries: DynamoDB GSI `pr-runs-index`
        │         └── Returns: `{"run_id", "sha", "status", "archive_s3_key"}`
        │
        └── Path: `/runs/{pr}/{sha}/{run_id}/{file}`
                  └── Headers: `Authorization: Bearer <token>` (REQUIRED)
                  └── Regex: `^runs/\d+/[0-9a-f]{40}/[0-9a-f]{32}/(events\.jsonl|meta\.json)$`
                  └── Returns: S3 Archive Object
```

- **Authentication:** In-memory bearer token stored in browser session, sent via `Authorization: Bearer <token>`
  on `/api/...` and archive routes. Token compared with `hmac.compare_digest`. Token lifecycle: provisioned
  out-of-band via `aws ssm put-parameter --type SecureString`; the static shell exposes a password-field
  login that stores the token in `sessionStorage` only (never URL, never localStorage) and attaches it as
  `Authorization: Bearer` via fetch; rotation = new SSM value, no code deploy. XSS posture (added
  2026-09-26, bot review #3): archived event content rendered by the replay UI — reasoning excerpts and
  finding text are model-controlled and traverse the same trust boundary as review output — passes
  through the SAME span-preserving sanitization (§5) before render; no model-controlled string is ever
  inserted via markup-unsafe rendering. Residual risk accepted for v1: `sessionStorage` is readable by
  any script on the origin, so the sanitization layer IS the boundary (no third-party scripts ship on
  the viewer origin). Raw archive contract (added 2026-09-26, bot review #7): the archive routes
  serving `events.jsonl` and `meta.json` return UNTRUSTED model-controlled data by definition — raw
  files are data, not rendered markup, and every consumer (the replay UI today, any future consumer)
  MUST treat them as adversarial input and apply §5 sanitization before any render path.
- **Viewer Lambda IAM:**
  - `ssm:GetParameter` on token parameter ARN, plus `kms:Decrypt` on the parameter's KMS key ARN
    (SecureString reads fail without it — missing this makes the viewer 500 on every authed route).
  - `s3:GetObject` on `["${bucket.arn}/runs/*", "${bucket.arn}/static/*"]` (permits serving static assets and archives).
  - `dynamodb:Query` on `"${table.arn}/index/pr-runs-index"`.
- **DynamoDB State Store & GSI Capacity Rebalancing (Mars Ruling 2026-09-26):**
  Table `pr-reviewer-state` provisioned capacity is rebalanced to preserve the AWS Always Free allowance (25/25):
  - Base table: `read_capacity = 20`, `write_capacity = 20`.
  - GSI `pr-runs-index`: `read_capacity = 5`, `write_capacity = 5`.
  - Partition key: `pr_number (N)`. Sort key: `started_ts (S)`.
  - `ProjectionType: INCLUDE` with non-key attributes `["sha", "status", "pipeline", "archive_s3_key", "archive_written_at", "findings_n"]`.
    (Answers "latest run for PR #123" without requiring a secondary base-table `GetItem`; `pipeline`
    is projected so latest-run queries can discriminate shadow runs — gate 5).

## 8. Rollout & Hardened Terraform Checklist

### Phase 0 — Latency Smoke Test
- Existing worker + ONE specialist (`correctness`) behind `MULTI_AGENT_PHASE0` flag.
- Shadow ordering (pinned 2026-09-26): single-pass publishes FIRST with unchanged latency; the shadow
  correctness specialist runs inline AFTER publish iff remaining Lambda time ≥ `WAVE_WAIT_FOR_S +
  BUDGET_MARGIN_S`; if budget is insufficient the shadow is skipped with
  `review_skipped {reason: "phase0_no_budget"}`. The shadow NEVER blocks publish and NEVER shares the
  401-refresh budget.
- Measure per-stage (wave / verifier / synth) p95 latency with thinking enabled (`default` vs `low`
  effort), and baseline claim/fence/publish timings to tune `BUDGET_MARGIN_S` and pin the ~90s
  fixed-overhead estimate.
- **Exit criteria (added 2026-09-26; N=4 clause added 2026-09-26, bot review #2):** measured
  per-stage p95 + `BUDGET_MARGIN_S` fits each stage budget, AND the measured end-to-end multi-agent
  path (gates included) fits the 900s worker timeout with the single-pass fallback still affordable,
  AND the explicit N=4 concurrency probe (mutex ceiling amendment) returns all-200. Any failure is a
  STOP: per-stage budget miss → revisit D9 budgets before Phase 1; N=4 tripping → the contention
  path queues behind the lease (hard ceiling 3) and Phase 1 ships only with that queuing design.
  Do not ship a path that degrades to single-pass on most runs.

### Phase 1 — Full Fan-out
- Deploy behind `MULTI_AGENT` flag.

### Phase 2 — Replay Site
- Deploy viewer Lambda, Function URL, and static assets in S3.

### Flag Precedence (pinned 2026-09-26)
`MULTI_AGENT=1` ignores `MULTI_AGENT_PHASE0` entirely (full fan-out). PHASE0 shadow runs iff
`MULTI_AGENT=0 AND MULTI_AGENT_PHASE0=1`. Both 0 = legacy single-pass: no shadow, no multi-agent events
beyond `review_started` / `checkpoint` / `review_published`.

### Hardened Terraform Checklist
1. **S3 Bucket for Archives:** Block Public Access pinned; Bucket policy Deny on public ACLs;
   Lifecycle configuration: **Expiration at 90 days** on `runs/` prefix (no Glacier transition).
2. **SQS Messaging (`terraform/messaging.tf`):** `visibility_timeout_seconds = 1800` (= 2 × the 900s
   worker timeout; amended 2026-09-26, gate 5 — the earlier 5400 contradicted both the live value and
   the pinned three-way contract in `tests/contracts/test_terraform_contract.py`; any future change
   lands in the same PR as its contract-test update).
   Redrive policy updated:
   ```hcl
   redrive_policy = jsonencode({
     deadLetterTargetArn = aws_sqs_queue.dlq.arn
     maxReceiveCount     = 3
   })
   ```
   (Updated 5 $\to$ 3 per Mars ruling 2026-09-26; recorded in `docs/DECISIONS.md`).
   Three-way contract: change `messaging.tf` AND the worker's `_MAX_RECEIVE_COUNT` constant (the
   final-attempt D2-notice logic keys off it) in the SAME PR, and add a contract test asserting the
   Terraform value equals the code constant.
3. **SQS Event Source Mapping (`terraform/compute.tf`):**
   `scaling_config { maximum_concurrency = 2 }`. Worker remains unreserved.
4. **Worker Mutual Exclusion:** Handled via the §D9 DynamoDB mutex contract (`mutex:pr-reviewer-worker`;
   conditional-write acquire with 30s skew margin, 50%-TTL refresh while fan-out is active,
   token-conditioned release). Contention → contender runs single-pass inline; no visibility deferral.
5. **DynamoDB State Table (`terraform/state.tf`):**
   Rebalance capacity to 20/20 base. Add GSI `pr-runs-index` (`pr_number (N)` + `started_ts (S)`) with
   `read_capacity = 5`, `write_capacity = 5`, `projection_type = "INCLUDE"`.
6. **Viewer Lambda & Function URL:**
   - Function URL `authorization_type = "NONE"`.
   - Add permission statement `lambda:InvokeFunction` with condition `lambda:InvokedViaFunctionUrl = true`.
   - Viewer IAM role: `s3:GetObject` on `runs/*` and `static/*`; `dynamodb:Query` on GSI ARN;
     `ssm:GetParameter` on token ARN plus `kms:Decrypt` on the parameter's KMS key ARN (§7).
7. **Environment Variables:**
   - `MULTI_AGENT` (0/1), `MULTI_AGENT_PHASE0` (0/1), `FANOUT_CONCURRENCY` (default 3),
   - `MUTEX_LEASE_TTL_S` (default 900 — see the lease TTL pin under §Concurrency Resolution),
   - `REASONING_MAX_CHARS` (default 4000), `REASONING_EFFORT` (default `low` — PROVISIONAL until the
     Phase 0 exit ruling; D6/D8 pick the ship config from measured p95 and this infrastructure default
     must not pre-decide it),
   - `WAVE_WAIT_FOR_S` (300), `VERIFIER_WAIT_FOR_S` (240), `SYNTHESIZER_WAIT_FOR_S` (180),
   - `SINGLE_PASS_WAIT_FOR_S` (240), `SOCKET_READ_TIMEOUT_S` (240), `BUDGET_MARGIN_S` (60).
   - Naming pin: `MARGIN_S` and `DEGRADED_BUDGET_MARGIN_S` are withdrawn; `BUDGET_MARGIN_S` is the
     single margin used in all four elapsed-budget gates, tuned in Phase 0.
8. **Alarm Recalibration:** Recalibrate daily LLM-spend alarm in the same PR.

## 9. GLM API Constraints & `llm.py` Modifications

### Required Changes to `lambda/common/llm.py`
1. **Configurable Read Timeout (rewritten 2026-09-26, gate 5 — supersedes the stale
   `READ_TIMEOUT_S = 45` description; that symbol no longer exists):** `lambda/common/llm.py` already
   resolves the read timeout at request time via `_read_timeout_s()` (`GLM_READ_TIMEOUT_S`, default
   240, clamped [30, 600]; applied at `llm.py:229`). Multi-agent specialist/verifier/synthesizer
   calls pass the resolved value explicitly as `read_timeout_s`, and the contender path overrides it
   per the clamp pin above — no new module constant is introduced.
2. **Explicit Max Tokens:** Pass `max_tokens: 16384` in payload.
3. **Finish Reason Length Detection:** In `_parse_result`, check `first.get("finish_reason") == "length"`.
   If true, raise `LlmError("length")` so caller can treat truncation as an error rather than silent success.
4. **Thinking & Reasoning Effort:** Pass `thinking: {"type": "enabled"}` and `reasoning_effort: reasoning_effort`
   when `thinking_enabled=True`.
5. **Reasoning Trace Extraction:** Extract `reasoning_content = message.get("reasoning_content")` and return
   it on `ReviewResult(..., reasoning_content=reasoning_content)`.
6. **Z.AI Error Taxonomy:** Classify HTTP 429 error code 1302 as `LlmError("rate_limit")` (fail-fast to survivors).

## 10. Decisions & Open Questions Log

- **Q1: RESOLVED (2026-09-25).** Topology: 3 generator specialists + verifier + synthesizer.
- **Q2: RESOLVED (2026-09-25).** Event store: both (DynamoDB index + S3 JSONL blob).
- **Q3: RESOLVED (2026-09-25).** Frontend: bespoke Mermaid/Studio aesthetic.
- **Q4: RESOLVED (2026-09-25).** Private first via bearer token in SSM SecureString.
- **Q5: RESOLVED (2026-09-25).** Reasoning verbosity: enabled + `reasoning_effort`, truncate at capture.
- **Q6: RESOLVED (2026-09-25).** Model: GLM only across all agents.
- **Q7: RESOLVED (2026-09-26, Mars ruling; wording amended 2026-09-26, bot review #3).** Explicit
  `thinking: {"type": "enabled"}` is confirmed for production; `reasoning_effort` STARTS at `low`
  (specialists) as the provisional default, and the SHIP config is resolved by the Phase 0 p95
  comparison under the D8 effort-selection rule (see D8 — pinned 2026-09-26). This entry does not
  pre-decide that ruling.
- **Q8: RESOLVED (2026-09-26, Mars ruling).** Worker concurrency strategy: Option A confirmed (unreserved worker,
  ESM concurrency 2, single-worker execution enforced via the §D9 DynamoDB mutex contract
  `mutex:pr-reviewer-worker`; contention → contender runs single-pass inline, never a visibility deferral).
- **Q9: RESOLVED (2026-09-26, Mars ruling).** S3 Archive retention: S3 Expiration (Delete) at 90 days confirmed
  to stay within Free Tier; a documented runbook `aws s3 sync` command (no tracked script) syncs archives
  down to this local machine.
- **Q10: RESOLVED (2026-09-26, Mars ruling).** DynamoDB capacity: rebalance base table to 20/20 and allocate 5/5
  to GSI `pr-runs-index` to preserve $0 Always Free Tier.
- **Q11: RESOLVED (2026-09-26, Mars ruling).** SQS redrive: update `maxReceiveCount` from 5 to 3 in `messaging.tf`.

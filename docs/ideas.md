# Ideas — Reviewer Feature Backlog

**Status:** Non-normative pre-commitment backlog. Not architecture, not authority — proposals awaiting a Mars accept/reject. Authority rule lives in the `docs/HLD.md` header.
**Promotion path:** Mars accepts an idea → it becomes a spec task (task contract in `specs/**`) → built via the normal PR flow → a DECISIONS entry records the ruling. The HLD is touched only when the feature ships.
**Relationship to HLD §7.4:** §7.4 lists *committed* roadmap; this file is *pre-commitment* — ideas live here until accepted, then move to §7.4/specs.

---

## 1. Review counter on the canonical comment — `Proposed`

**Problem.** GitHub shows "edited" but never *how many times*. When the reviewer updates the canonical comment on every push, there is no way to know this is the 1st or the 7th review of the PR.

**Proposal.** Render a visible header line from the state record's `generation` field, e.g. `🔍 Review #4 · updated Sep 20, 10:42 AM PT`. The counter is computed server-side from `generation` (already monotone per PR in the state record) — it is **not** placed inside the canonical marker, which must stay byte-stable for exact-match reconciliation (§3.4).

**Impact.** HIGH for day-to-day use, near-zero cost: you can tell at a glance how many revisions a review has tracked and how fresh it is.

**Use case.** You open a heavily-pushed PR, see "Review #4 · updated 10:42 AM", and instantly know the bot kept up through four revisions and the comment is current — no digging through GitHub's edit history (which GitHub doesn't even expose for comments).

**Trade-offs.**
- One extra header line of comment noise — severity: LOW.
- `generation` counts *established* revisions, not publish attempts (superseded/failed publishes don't increment), so "#N" means "Nth completed review" — severity: LOW (arguably the semantics you want).

**Ground truth.** `lambda/common/state.py:5` — `generation` is already "monotone non-decreasing per key"; it is surfaced in structured logs (`lambda/common/logs.py:64`) but never in the comment (`lambda/common/assemble.py` builds marker + content only).

---

## 2. Human timestamp on the comment (12-hour, no seconds) — `Proposed`

**Problem.** On earlier PRs the comment said the literal word "timestamp" instead of a time. Root cause: **no clock value reaches the LLM** — the system prompt contains no time instruction (`prompts/system_prompt.md` — zero hits), and the review payload is diff-only. Asked to include a timestamp, the model can only invent or echo the word.

**Proposal.** The worker renders the publish time **in code** (never via the LLM — models cannot know the time and will hallucinate it), converted to Pacific and formatted `Sep 20, 10:42 AM` (12-hour, minute precision, no seconds), and injects that string into the comment header (pairs with Idea 1).

**Impact.** MEDIUM: removes a recurring visible defect and makes review freshness scannable; trivial to implement (stdlib `ZoneInfo("America/Los_Angeles")`).

**Use case.** You glance at any comment and know exactly when the bot last looked, in the format you read natively — no UTC conversion, no "timestamp" placeholder garbage.

**Trade-offs.**
- Timezone hardcoded to Pacific (your zone, matching the Jira-stamp convention in AGENTS.md) — wrong only if the repo outlives your timezone — severity: LOW.
- Adds one config-free formatting helper to `lambda/common/` — severity: LOW.

**Ground truth.** Code today formats time only as internal machine stamps (`lambda/common/reconcile.py:100`, `lambda/common/protocol.py:129`, ISO-8601 UTC); nothing human-facing exists.

---

## 3. PR title + description + prior comment in the review prompt — `Proposed`

**Problem.** The reviewer reviews the diff blind to intent: it never sees the PR title, the description, or what it said last time. It cannot catch "code doesn't do what the PR promises" and it can repeat or contradict its own earlier findings.

**Proposal.** Extend the review payload with three bounded sections: PR title + body (truncated, e.g. 4 KB), and the prior canonical comment text (for continuity — "already reported, still present" instead of re-discovery). All three must pass through the existing untrusted-content fencing (system prompt already classifies titles/bodies/comments as adversarial data — `prompts/system_prompt.md:18`).

**Impact.** HIGH — this is the single biggest review-quality lever in the list: intent-vs-implementation mismatches are the highest-value finding class, and prior-comment continuity kills duplicate noise across re-reviews.

**Use case.** PR description says "add retry with exponential backoff"; the diff implements fixed 1s sleep — the reviewer can now say exactly that, instead of vaguely noting "sleep looks short". On push #2 it says "finding from review #3 still open; 2 new findings" instead of re-deriving everything.

**Trade-offs.**
- Prompt-injection surface grows: a malicious PR body can now try to steer the reviewer in prose, not just code — mitigated by the existing untrusted-data fencing, but the attack surface is real — severity: **MEDIUM**.
- Token cost/latency: body truncation caps it; at ~130 s/case measured (§4.2), +10–15% prompt tokens is acceptable — severity: LOW–MEDIUM.
- Prior-comment injection needs the current comment fetched before publishing (one extra GitHub read per review) — severity: LOW.

**Ground truth.** `lambda/common/assemble.py:80-91` — `render_diff_text` sends only budgeted diff hunks + a lockfile summary; no metadata today.

---

## 4. Bounded whole-file context for changed files — `Proposed`

**Problem.** The LLM sees diff hunks with (budgeted) context lines only. It cannot see the rest of a changed file, so it misses things like "this helper already exists 40 lines up" or misreads code whose meaning depends on definitions outside the hunk.

**Proposal.** For each changed file under a size cap (e.g. 400 lines), include the full file in the payload; larger files keep hunks-only. Per-file and total budgets feed the existing diff-budget machinery.

**Impact.** MEDIUM–HIGH for correctness of individual findings (fewer false positives, better duplicates/abstraction catches); the classic "senior reviewer reads the file, not the diff" upgrade.

**Use case.** Diff renames a function's signature; the full-file view lets the reviewer spot the third call site the diff didn't touch — a finding that is invisible in hunks.

**Trade-offs.**
- Token cost is the real constraint: whole files can 3–5× payload size; the caps must be tuned against the measured ~130 s/case latency and token budget — severity: **MEDIUM**.
- Large generated files get excluded by the cap (correct behavior, but worth stating) — severity: LOW.

**Ground truth.** The budgeting seam already exists (`DiffResult` + budget note in `assemble.py:4-9`); this extends it rather than inventing a new one.

---

## 5. Accepted-residuals memory (stop re-flagging settled findings) — `Proposed`

**Problem.** Across re-reviews the reviewer re-raises findings that were already seen and accepted as residuals — observed repeatedly in our own comment loops (the same nit came back on three consecutive review rounds). Every push pays LLM tokens to rediscover settled conclusions.

**Proposal.** A persisted accepted-residuals list — either in the state record or a committed file like `docs/accepted-residuals.md` — injected into the prompt as: "The following were reviewed and accepted by the operator; do not re-flag unless the code materially changes." Operator (you) adds/removes entries; the reviewer treats them as settled context.

**Impact.** HIGH for noise reduction across re-reviews — this is the "loop is stable when only accepted residuals remain" property, enforced in the product instead of in your head.

**Use case.** You decide "we accept the 6× constant duplication in the test fixture". You add one line to the residuals file; every future review of that PR (and others touching it) skips re-raising it — silently, not silently-suppressing anything new.

**Trade-offs.**
- Suppression risk: an accepted residual can later become a real bug; the "unless materially changed" fence plus your explicit ownership of the list is the mitigation — severity: **MEDIUM**.
- One more artifact to curate; goes stale if you never prune — severity: LOW.

**Ground truth.** The state record already persists per-PR review state (`lambda/common/state.py`); a residuals list is either a new attribute or a repo file read at review time.

---

## 6. Failed-CI awareness in the review — `Proposed`

**Problem.** The reviewer comments on code quality but never on the PR's own CI status — a review can parse as "looks good" while the branch's tests are red.

**Proposal.** Before review, read check-run conclusions (one GitHub API call) and include a one-line status in the payload + comment footer ("CI: 1 failing — tests").

**Impact.** LOW–MEDIUM: closes the gap between "code reads fine" and "PR is actually mergeable"; cheap signal.

**Use case.** You open a PR where the bot's comment is green-ish; the footer says CI failed on `test_retry.py` — you know the review's "minor nit only" verdict is about code, not health.

**Trade-offs.**
- New fine-grained PAT permission (`checks: read`) — a permission-scope change, gated on your GitHub token settings — severity: LOW but requires your action.
- One more API call per review (rate limits are a non-issue at this volume) — severity: LOW.

**Ground truth.** The PAT currently holds Pull requests read/write (§2.6); no checks scope today.

---

## 7. "What changed since last review" header — `Proposed` (depends on #1, #3)

**Problem.** On re-reviews you must diff the comment against your memory of the previous one to know what's new.

**Proposal.** With the prior comment in context (#3) and the revision counter (#1), the header gains one capped line: "vs review #3: 2 files changed (+40/−12) · 1 prior finding resolved · 2 new findings". Server-computed counts where possible, LLM-summarized where semantic.

**Impact.** MEDIUM: turns re-reviews from "re-read everything" into "read the delta" — directly targets your time.

**Use case.** Push #5 of a long PR: header says "1 prior finding resolved · 1 new" and you read one finding instead of the whole comment.

**Trade-offs.**
- The semantic part (resolved vs new) is LLM-judged and can mislabel — cap the damage to one line and label it heuristic — severity: LOW.
- Adds a judgment step to the LLM's job (slight latency/token add) — severity: LOW.

**Ground truth.** Needs #3's prior-comment fetch and #1's counter; counts of files/lines are already available in `DiffResult`.

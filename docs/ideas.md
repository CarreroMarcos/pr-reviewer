# Ideas — Reviewer Feature Backlog

**Status:** Non-normative pre-commitment backlog. Not architecture, not authority — proposals awaiting a Mars accept/reject. Authority rule lives in the `docs/HLD.md` header.
**Promotion path:** Mars accepts an idea → it becomes a spec task (task contract in `specs/**`) → built via the normal PR flow → a DECISIONS entry records the ruling. The HLD is touched only when the feature ships.
**Relationship to HLD §7.4:** §7.4 lists *committed* roadmap; this file is *pre-commitment* — ideas live here until accepted, then move to §7.4/specs.

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

## 7. "What changed since last review" header — `Proposed`

**Problem.** On re-reviews you must diff the comment against your memory of the previous one to know what's new.

**Proposal.** With the prior comment in context and the revision counter (spec 003 groundwork), the header gains one capped line: "vs review #3: 2 files changed (+40/−12) · 1 prior finding resolved · 2 new findings". Server-computed counts where possible, LLM-summarized where semantic.

**Impact.** MEDIUM: turns re-reviews from "re-read everything" into "read the delta" — directly targets your time.

**Use case.** Push #5 of a long PR: header says "1 prior finding resolved · 1 new" and you read one finding instead of the whole comment.

**Trade-offs.**
- The semantic part (resolved vs new) is LLM-judged and can mislabel — cap the damage to one line and label it heuristic — severity: LOW.
- Adds a judgment step to the LLM's job (slight latency/token add) — severity: LOW.

**Ground truth.** Needs the prior-comment fetch and revision counter from spec 003; counts of files/lines are already available in `DiffResult`.

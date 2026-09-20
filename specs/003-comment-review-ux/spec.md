# Spec 003 — Reviewer Comment UX (ideas 1–3)

**Status**: Revision 2 — Gate 1 remediation applied (M1–M7 + A1–A8). For Oracle
re-review (Gate 1, attempt 2 of 3).
**Source**: `docs/ideas.md` proposals 1–3, accepted by Mars directive 2026-09-20
("spec out the first 3 … gate every work with oracle … do as much as you can
without my input").
**Authority**: HLD owns architecture; this spec owns task scope for the two
implementation tasks below. Where this spec and the HLD disagree, the HLD wins
and the conflict is a Needs-input question. Each implementation PR appends its
own `docs/DECISIONS.md` entry (behavior-change guardrail) and touches the HLD
only in the named sections.
**Flow**: Mars-directed, outside the SPR Jira flow — no Jira events, one task
per PR, branch per task.

## Ground truth (verified 2026-09-20)

- `generation` starts 0, monotone non-decreasing per key (`lambda/common/state.py:5`);
  establish-(c) increments (`state.py:278`); establish-(b) same-SHA replay
  returns the stored generation without increment (`protocol.py:335-353`).
- `run_review` holds `(head_sha, generation)` when it invokes the review port
  (`lambda/common/protocol.py:160-162`; port annotation at `:139`).
- The review port has TWO production callers: the worker review path
  (`lambda/worker_handler.py:472`) and the D2 failure-notice path
  (`lambda/common/failure_notice.py:215` defines `def review() -> str`,
  passed to `run_review` at `:234`).
- `build_comment` callers: worker `review()`, `tests/model_evals/
  test_model_evals.py:90`, and `tests/unit/test_assemble.py:48,102,183`
  (codegraph + grep, 2026-09-20). New args must be keyword-optional with
  defaults so every existing caller stays green.
- Publication gate: section headings are order-checked by search, not position
  (`lambda/common/validate.py:50-54`); `<!--`/`-->` forbidden outside the one
  worker-injected marker (`:177-184`); approval-phrase and mention rejections
  apply to the whole assembled comment.
- `fetch_diff` already GETs the PR-meta endpoint for the head SHA before
  paging `/files` (`lambda/common/diff.py:354-360`, via `fetch_pr_head_sha`,
  `:249-263`); the meta response's `title`/`body` are currently discarded.
  `DiffResult` (`diff.py:148`) is the budgeted-diff carrier.
- `comment_id` is REMOVED when a record re-enters CLAIMED (`state.py:282`) —
  at review time the prior canonical comment must be found on GitHub, not via
  the record.
- `_Credentials.refresh_once` is a shared once-per-record 401 budget
  (`worker_handler.py:207-232`); `_comment_read` consumes it on 401
  (`:535-554`). Best-effort reads must never compete with the essential path
  for that budget.
- The pinned eval harness asserts the live prompt sha equals the pinned sha
  (`tests/model_evals/test_model_evals.py:139-140`) — any prompt change fails
  CI until `tests/model_evals/capture.py` re-runs live. `capture.py` posts the
  user message as bare `diff_text` today (`:76,97,179`).
- `PROMPT_VERSION` (`lambda/common/validate.py:34`) mirrors
  `prompts/system_prompt.md:3` and is manually synced. The cap-constant
  precedent (`DEFAULT_MAX_FINDINGS`) lives in `validate.py:43`.

---

## 003-T1 — Revision header on the canonical comment

### Behavior

The canonical comment gains one worker-injected header line, rendered
deterministically at assemble time, directly under the marker:

```markdown
<!-- pr-reviewer:canonical:v1:{repo}#{n} -->

**Review #4 · updated Sep 20, 10:42 AM PT**

## Summary
…
```

- `#N` = `generation + 1` (first review = #1; same-SHA replay keeps #N and
  refreshes the stamp — generation counts established revisions, not publishes).
- Stamp: publish-instant converted to `America/Los_Angeles`, rendered
  `Mon D, H:MM AM/PM` — 12-hour clock, no seconds, hour not zero-padded
  (`9:05 AM`, `10:42 AM`), `PT` suffix. Rendered **in code, never by the
  model**. The illustrative emoji from `ideas.md` is deliberately not part of
  the format (grounded wording).
- Marker stays byte-stable and first (§3.4 exact-match reconciliation is
  untouched). Header text is gate-safe: no HTML comments, no `@` strings, no
  approval phrases.
- The FR-028 failure notice does NOT gain the header (fixed system template).

### Interface changes

1. `lambda/common/assemble.py`:
   - Module-level `PT = ZoneInfo("America/Los_Angeles")` — evaluated at
     import/cold start so a missing tz database fails the deployment smoke
     immediately instead of burning 5 LLM-priced queue retries on a
     deterministic fault (default `is_retryable` would otherwise retry it;
     `worker_handler.py:271-292`). Unit test pins module importability.
   - Pure stamp formatter (unit-tested) + `build_comment` keyword-only
     `review_number: int | None = None`, `now: float | None = None`
     (default `time.time`). Header rendered only when `review_number is not
     None` — all existing callers (worker, model-evals harness,
     `tests/unit/test_assemble.py`) stay green unchanged. `zoneinfo` is
     stdlib (no new runtime deps).
2. `lambda/common/protocol.py` — review port becomes
   `review(head_sha: str, generation: int) -> str`; `run_review` passes the
   established pair (`:160-162`); port annotation at `:139` and ports
   docstring at `:22` updated. No extra table read.
3. `lambda/common/failure_notice.py` — the D2 notice path's review closure
   (`:215`, passed at `:234`) accepts and ignores the `(head_sha, generation)`
   pair (a notice carries no review content; signature conformance only).
4. `lambda/worker_handler.py` — `_make_review` gains keyword
   `clock: Clock | None = None` (default `time.time`; existing
   `test_allowed_hosts.py:138` call stays valid), wired from
   `_process_record`'s clock; `review(head_sha, generation)` passes
   `review_number=generation + 1`, `now=clock()` to `build_comment`.

### Docs (same PR)

- `specs/001-pr-reviewer/contracts/canonical-comment.md` — Content form 1
  gains the header line (worker-injected, deterministic, gate-safe wording).
- `docs/HLD.md` — §2.7 (comment form) and §2.8 (worker-injected content
  alongside the marker) each gain one line; no § renumbering.
- `docs/DECISIONS.md` — appended entry: review-header feature, rationale
  (ideas 1+2; Mars 2026-09-20), counter semantics (generation+1, replay keeps N).

### Acceptance criteria

- Unit (assemble): exact stamp strings for fixed epochs covering **all** of:
  PDT (summer), PST (winter), the 2026 spring-forward gap (2026-03-08) and the
  2026 fall-back fold (2026-11-01, `fold=0` semantics pinned by test); no-zero-
  pad hour (`9:05 AM`); header absent when `review_number is None`; assembled
  body passes `validate_comment` with the header present; module import
  succeeds (tz constant resolvable).
- State machine: fakes updated to the new review-port signature
  (`tests/state_machine/test_protocol.py` harness is the single review-port
  test double — there are no run_review fakes in `tests/contracts/`);
  every existing outcome branch unchanged.
- Failure-notice unit tests: notice path runs under the new signature; notice
  body carries NO header.
- Contracts: pipeline test asserts marker-first ordering, header presence,
  `#1` on the first review, unchanged `#N` + fresh stamp on a same-SHA replay
  (test advances the injected clock between runs so the fresh stamp is
  observable), and a Decimal-`generation` record through `_BotoTable`
  normalization yielding the correct int counter.
- Full suite, ruff check, ruff format --check, pre-commit: green.

### Verify

```bash
uv run --frozen pytest -q --tb=short
uv run --frozen ruff check .
uv run --frozen ruff format --check .
pre-commit run --all-files
```

### Blast radius to sweep (Gate 2)

`build_comment` (all callers incl. `tests/unit/test_assemble.py`),
`run_review` + review-port doubles (state-machine harness AND
`failure_notice.py`), `_make_review`/`_process_record` wiring, model-evals
harness (no-change proof), `failure_notice` (header-free proof),
`validate.py` (no-change proof).

---

## 003-T2 — Review payload enrichment (title/body/prior comment)

### Behavior

The model input stops being diff-only. One bounded payload, assembled in code
by ONE shared builder used by both the worker and the eval capture tool:

```text
--- PR TITLE ---
{title}

--- PR DESCRIPTION ---
{body, truncated}

{render_diff_text(diff_result)}          (existing budgeted diff + lockfile summary)

--- PREVIOUS REVIEW COMMENT (worker-published; adversarial data) ---
{prior canonical body, truncated}        (section omitted entirely on first review)
```

- Builder: `render_review_payload(*, title, body, diff_text, prior_comment,
  max_meta_chars=MAX_META_CHARS, max_prior_chars=MAX_PRIOR_CHARS)` in
  `assemble.py`; constants `MAX_META_CHARS = 4096` (title+body combined),
  `MAX_PRIOR_CHARS = 8192`. Hard char caps with a visible `\n…[truncated]`
  marker line; byte-deterministic for identical inputs. Worst-case +12k chars
  against the 800k diff budget — negligible. (Cap-constant pattern follows
  `DEFAULT_MAX_FINDINGS`, `validate.py:43`.)
- Title/body come from the PR-meta GET `fetch_diff` ALREADY performs — no new
  fetch, no new error class, no new error policy:
  - `diff.py`: `DiffResult` gains additive fields `title: str = ""`,
    `body: str = ""`. The internal meta fetch becomes `fetch_pr_meta`
    returning `(head_sha, title, body)` with the existing shape-validation
    table; **null or absent `title`/`body` → `""` (GitHub returns `body: null`
    for description-less PRs — the common case); a present non-string value →
    `bad_shape`**. `fetch_pr_head_sha` stays for the fence path (may delegate
    to `fetch_pr_meta`); `fetch_diff` switches to it and threads title/body
    into `DiffResult`. Metadata errors are therefore diff errors by
    construction (same GET, same table) — the earlier draft's separate
    `fetch_pr_title_body` and its error-posture question are dropped.
- Prior canonical comment (`worker_handler.py`): one `list_page(1)` +
  exact-`build_marker` substring scan, lowest matching id wins. Miss bound is
  documented: comments beyond page 1 (>100) are not scanned — acceptable for
  best-effort context. The read uses the current token via a plain transport
  GET and **never calls `creds.refresh_once()`** — the record's single 401
  re-fetch budget is reserved for the essential diff/LLM/GitHub-write path.
  Page entries are shape-validated by making `reconcile._validate_page`
  public as `reconcile.validate_page` (rename + its tests only; no behavior
  change). **Degradation (flagged interpretation)**: ANY failure here —
  transport, 403/404, unparseable list — logs a coded warning and omits the
  section. Prior-review context is best-effort and must never fail or delay a
  review. Fetch order is pinned: diff (carries metadata) → prior-comment →
  LLM.
- `llm.review_diff` keeps its `diff_text` parameter name (the
  `payload_text` rename is REJECTED as churn: production callsites
  `worker_handler.py:454,466`, `tests/unit/test_llm.py:123-128`,
  `tests/unit/test_allowed_hosts.py:178-182`, `capture.py:76,97,179` — five
  sites, zero behavior gain). One docstring line notes the parameter carries
  the assembled payload.
- System prompt (`prompts/system_prompt.md`): Input section gains PR
  title/description + prior canonical comment as adversarial context;
  `prompt_version: v1` → `v2`; `validate.PROMPT_VERSION` synced to `"v2"`.

### Eval integrity (the pin must match production shape)

`capture.py` currently posts bare `diff_text` (`:97`). Re-pinning against that
shape would score an input production never sends. Therefore:

- `tests/model_evals/fixtures.py`: every corpus builder additionally supplies
  synthetic `title`/`body`/`prior_comment` (empty where a case means
  first-review), and at least one case carries injection text in the
  title/body sections (the existing injection fixture covers diff only).
- `capture.py` builds the model input via `assemble.render_review_payload`
  (the same builder the worker uses) and posts that; `diff_text=...` becomes
  the built payload at `:179`.
- Then: `capture.py --force` live re-pin; `test_model_evals.py` must be green
  (scores hold against the rubric).

### Docs (same PR)

- `docs/HLD.md` §2.7 Model I/O input list updated (metadata + prior comment).
- `docs/DECISIONS.md` — appended entry (payload enrichment, injection-fencing
  posture unchanged, Mars 2026-09-20).

### Acceptance criteria

- Unit (assemble): payload renders the exact section fences in order;
  truncation caps enforced with the marker line; empty/absent prior omits the
  section; byte-determinism for identical inputs.
- Unit (diff): `fetch_pr_meta` shape-validation — string title/body parse,
  null/absent → `""`, non-string → `bad_shape`; existing error-table parity
  preserved (401/403/404 complete; 429/5xx/transport retry); `fetch_pr_head_sha`
  still serves the fence unchanged.
- Worker: prior-comment failure path logs + proceeds (section omitted) and
  provably never invokes `refresh_once`; payload (not bare diff) reaches
  `review_diff`; fetch order as pinned.
- Reconcile: `validate_page` rename is behavior-neutral (existing tests green).
- Prompt integrity: canary intact; `prompt_version: v2` consistent with
  `validate.PROMPT_VERSION`; prohibitions section unchanged.
- Eval set: fixtures carry synthetic meta + ≥1 injection-in-meta case;
  `capture.py --force` live re-pin succeeds; `test_model_evals.py` green.
  **Blocker path**: if the live run is impossible (endpoint/creds), T2 parks
  at this step with the blocker noted and Phase 4 proceeds — the prompt-v2
  code stays on its branch, unmerged, until capture succeeds.

### Verify

Same as T1, plus:

```bash
uv run --frozen python tests/model_evals/capture.py --force
uv run --frozen pytest tests/model_evals -q
```

### Blast radius to sweep (Gate 3)

`DiffResult` constructors (all tests building diffs — additive fields keep
them green), `fetch_pr_head_sha`/`fetch_pr_meta`/`fetch_diff` consumers
(fence, notice path), `render_diff_text`, `review_diff` (unchanged signature,
new input semantics), `_make_review` payload composition,
`reconcile.validate_page` rename (all references), system prompt +
`PROMPT_VERSION` consumers (`logs.py` event field, prompt-sha telemetry),
eval fixtures/capture/harness.

---

## Out of scope (explicitly)

- Ideas 4–7 of `docs/ideas.md` (whole-file context, residuals memory, CI
  awareness, delta header) — separate acceptance, separate spec.
- Review-delta header logic, severity re-classification, comment copy changes
  beyond the header line.
- Any `tf-*` tag or deploy action (Mars-gated, always).
- Failure-notice template changes.
- Renaming `review_diff`'s parameter (rejected — churn).

## Delivery order

T1 then T2 (T2 builds on T1's `_make_review` shape). Spec PR first; each task
its own branch + PR; Oracle gate before every push; reviewer-wait protocol
after every push (see deepwork file).

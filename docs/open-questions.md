# Open questions — review quality & fidelity

Priority order for everything below: **review fidelity and quality first, cost
second.** A review that misses real bugs because the model wasn't smart enough
or didn't have enough time is worse than a short review — arguably worse than
none. Nothing here blocks the delivered loop; this is the backlog to revisit
when there is time.

Markers: **open** (no data yet), **prereq** (blocks other items), **deferred**
(explicitly out of scope for now).

## 1. What does `thinking: disabled` actually cost in quality? — **open**

The T035 acceptance run pinned `thinking: {"type": "disabled"}` into the LLM
payload (`lambda/common/llm.py`): provider-default reasoning measured ~45 s
(~1.2–1.3 K reasoning tokens) on the real PR 22 diff — over `READ_TIMEOUT_S`
flakily and the ≤15 s quickstart bar hopelessly; thinking-off measured 5–8 s.

- We have **zero measurement** of the recall/depth delta. Reasoning plausibly
  matters most for subtle findings — cross-hunk logic, race conditions,
  authorization gaps — exactly the high-value bugs.
- Counterpoint: the spec chose a *flash* model plus a 15 s bar, i.e. latency
  was already priced over depth. But that trade was never made with quality
  data.

How we'd answer: a fixed corpus of PRs with known/seeded defects → run
thinking on/off (and a mid budget) → score findings against ground truth
(precision/recall per severity). Needs Q2 first.
Decision that follows: if thinking-on wins big, either raise the latency
budget (a review is asynchronous from the user's point of view anyway) or
revisit model choice (Q6).

**Status (2026-09-17):** first A/B run on the 13-case corpus — thinking-on
buys nothing here: recall identical (1.0 everywhere), no precision gain
(0.57 vs 0.65), nit-rate highest of all variants, at 2.4× latency (318 s vs
130 s mean/case). Ops alarm folded out of the same run: thinking-OFF calls
measured ~130 s/case on the live endpoint (HLD assumed 5–8 s; product bar is
≤15 s) — possibly provider load at run time; re-probe before concluding, but
if it stands the latency budget is the real problem, not model choice.
Variant data: `tests/model_evals/results/*.json`.

## 2. How do we evaluate review quality at all? — **prereq**

The acceptance harness asserts plumbing (exactly one comment, ≤15 s, discard
rules) — never content quality. No ground truth exists.

- Build an eval set: N PRs with labeled real defects (from repo history) or
  seeded bugs. Score: bugs found / missed / false positives / nit-rate /
  comment length.
- Run evals offline through the same assemble + `validate()` gate
  (publish-ready output only), and record results per `PROMPT_VERSION` +
  model + payload config.
- This is the prerequisite for Q1, Q3, Q4, Q5, Q6 — every prompt, model, or
  context change becomes measurable once it exists.

**Status (2026-09-16, T058):** infrastructure + first baseline exist. A
15-case corpus (`tests/model_evals/`: 13 seeded defect cases across 11
categories + injection + large-mechanical robustness cases) is scored offline
through the production assemble + `validate()` gate, with results recorded
per `PROMPT_VERSION` + model + payload config and a staleness guard that
fails CI on any prompt change. Baseline (`results/baseline.json`,
glm-5.3-flash, temp 0.2, thinking disabled): **recall 1.0** (13/13 planted
bugs found), **precision 0.5** (13 unlabeled companion findings, 0
fabrications off-diff), nit_rate 0.27, ~964 chars/comment; severity
agreement only 5/13 (systematic HIGH↔MEDIUM drift — A/B signal, no bars
asserted yet). Remains for a fuller answer: real defects from repo history,
larger N, and quality bars derived from A/B data (Q1/Q3/Q6).

## 3. System prompt tuning — **open**

`prompts/system_prompt.md` (v1, ~3.7 KB) is untested against alternatives:

- a severity rubric (what deserves a finding vs a nit),
- explicit hunt lists (injection, authz, data loss, concurrency, secret
  leakage) — does a checklist raise recall or just noise?,
- output-length discipline vs depth for large diffs,
- tone and language.

A/B against the Q2 eval set; keep versions (`PROMPT_VERSION`) with recorded
results.

**Status (2026-09-16):** the A/B rail exists (T058 eval set + versioned,
staleness-guarded results); prompt variants currently need a manual re-capture
per variant.

## 4. More context for the reviewer — **deferred**

Today the model sees the diff (bounded) only. Candidates: file tree, PR
title/body, neighboring code, CI status, linked issues, prior review comments.

- More context should raise true-bug recall but costs tokens + latency —
  price it with Q2.
- Sub-question: whole-file inclusion for touched files vs retrieval (pull the
  definitions of touched symbols)?

## 5. Tools for the reviewer — **deferred** (furthest out)

An agentic loop: run the build/tests, grep the repo, fetch file contents
mid-review. Biggest quality-ceiling raise; biggest cost and safety surface
(tool authorization, sandboxing, spend caps). Prereqs: Q2 (evals) and a cost
model.

## 6. Model choice & the cost–quality frontier — **open**

`glm-5.3-flash` was chosen for latency. With Q2 data: is a stronger (or
reasoning) model with asynchronous delivery better overall? Also unmeasured:
temperature 0.2 vs 0 (determinism vs exploration).

**Status (2026-09-17):** the `--model` flag exists and the first comparison
ran: glm-5.3 (non-flash) ≈ flash on this corpus — identical recall, same
unlabeled count, same ~127 s latency. Not an upgrade, not a regression.
Temperature 0.0 vs 0.2 also indistinguishable (decode temperature is not a
lever here). Remaining: stronger non-GLM models, repo-history corpus.

## 7. Smaller questions — **open**

- `assemble_approval_verdict`: seen live once (a real-webhook ride discarded
  because the model emitted approval-like language). Is the validator rule
  tuned right, and should such rides take one bounded re-sample instead of
  dropping the review entirely?
- Same-SHA LLM cache: every reopen currently re-runs the LLM (bounded waste,
  accepted by HLD §3.2). Worth a content-hash cache when spend matters?
- Two-stage review: a cheap model triages hunks → an expensive model
  deep-dives flagged regions. Cost/quality frontier unexplored.
- Large-diff fidelity: the diff budget truncates — what does the model
  actually see past the cap, and does the ~1 K-char completion cap make
  comments too shallow for big diffs? First data point (T058, 2026-09-16):
  on a 591 KB mechanical diff the model returned one LOW nit, no fabricated
  findings, shape-valid output — direction is reassuring but one mechanical
  diff answers nothing about depth on real large diffs.
- Non-English PRs and non-code files: behavior untested.
- **PT timestamp in bot comments** (Mars, 2026-09-14): every reviewer comment
  should carry a Pacific-time stamp so Mars can see when it was posted. NOT
  yet implemented — the canonical comment format is spec-pinned (moto ride
  pins it), so the change needs a deliberate spec-conformant edit + a live
  redeploy to matter. Design constraint: zero new runtime deps (AGENTS law),
  so `zoneinfo` needs verified tzdata availability in the Lambda runtime
  (unverified) or a hand-rolled PST/PDT offset table; also pick format +
  placement (footer line vs header) and whether the stamp is UTC alongside.
  Decide at next pre-deploy pass.
- **(b) equality-path head gate** (Gate-3 F2 → Gate-4 A3c, 2026-09-14):
  repeat delivery of an already-superseded SHA takes the HLD §3.3 (b)
  equality fast-path, which returns the STORED head — so the run performs a
  FULL redundant review + PATCH (pinned as reality by
  `test_repeat_stale_sha_after_observation`; Gate 4 verified, bounded:
  rare duplicates, maxReceiveCount cap, content-converged). Proposed fix:
  gate (b) on `incoming == stored head_sha` so repeat-stale discards cheaply
  instead of re-publishing. Gate-4 ruled this a SPEC-SEMANTIC change
  (redefines the accepted meaning of `last_seen_sha` equality) → Needs-input,
  NOT implementable without your ruling + a tasks.md/HLD edit first. Related
  residual: a stale observe landing after a newer accept leaves
  `last_seen_sha` behind the head (HLD-mandated "record regardless"), so the
  next redelivery of the current head spuriously takes (c) — one wasted
  review, converge-correct; the (b)-gate would close this too.
- **True-concurrency test harness** (Gate-5 G1, 2026-09-14): all "concurrent"
  state-machine tests are sequential back-to-back runs on a shared table;
  genuine in-flight overlap (both runs past establish before either claims)
  is only modeled via foreign-lease seeding, and the equality guard's
  CCF-retry path is the sole interleaving pin. A real interleaved harness
  (deterministic scheduler over scripted step boundaries) is a bigger
  investment than any current AC demands; US4.AC2/T050 cover the deploy-side.
  Decide whether to invest or keep the seed-based model.

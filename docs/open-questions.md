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

## 3. System prompt tuning — **open**

`prompts/system_prompt.md` (v1, ~3.7 KB) is untested against alternatives:

- a severity rubric (what deserves a finding vs a nit),
- explicit hunt lists (injection, authz, data loss, concurrency, secret
  leakage) — does a checklist raise recall or just noise?,
- output-length discipline vs depth for large diffs,
- tone and language.

A/B against the Q2 eval set; keep versions (`PROMPT_VERSION`) with recorded
results.

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
  comments too shallow for big diffs?
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

# QA-B findings ledger — state & protocol hardening

Branch: `qa/state-protocol` · Worktree: `/tmp/opencode/qa-b` · Base: `origin/main` (`696a915`).

## F1 — `$`-anchored key patterns accept trailing newline (FIXED, red-first)

- **Suspicion (batch-2 carry-forward):** `lambda/common/state.py:40-41`
  `_SHA_RE = ^[0-9a-f]{40}$`, `_PK_RE = ^review:[^#]+#[0-9]+$`. In Python,
  `$` matches before a trailing newline, so `"a"*40 + "\n"` and
  `"review:o/r#42\n"` validate — a hostile/polluted value smuggling `\n`
  passes the codec and can be persisted as `head_sha`/`pk`.
- **Blast-radius evidence (codegraph MCP):**
  - `codegraph_callers(_check_sha)` → sole caller `ReviewState.__post_init__`
    (`lambda/common/state.py:74`); same for `_check_pk`.
  - `codegraph_impact(ReviewState)` → reachable via `from_item` (state.py)
    and `_establish_first_write` (protocol.py); no other production callers
    (`worker_handler.py` is a T034 stub; `run_review` callers are tests only).
  - `codegraph_callers(from_item)` → tests only. No existing caller passes
    newline-suffixed values (all test SHAs/PKs are clean), so tightening
    `$`→`\Z` rejects only values that never legitimately occur.
  - Codebase convention: `logs.py:62`, `envelope.py:29`, `diff.py:92` already
    use `\Z` (read-only observation, not edited). The fix aligns state.py.
- **Red evidence:** `uv run --frozen pytest
  tests/state_machine/test_state_codec.py -q --tb=short -k trailing_newline`
  → `3 failed, 105 deselected` — all three `Failed: DID NOT RAISE StateError`
  (commit `a7bd9dc`, "QA-B red").
- **Fix:** `state.py:40-41` `$` → `\Z` (2 chars; smallest change).
- **Green evidence:** `uv run --frozen pytest -q --tb=short` → `425 passed`;
  `ruff check` / `ruff format --check` / `pre-commit run --all-files` green.

## F2 — claim-stale branch + owner-absent OR-branch pinned (TESTS, green on arrival)

- `test_claim_failure_after_concurrent_move_discards_stale`: concurrent writer
  moves head+generation between establish and claim → claim guard fails →
  `_resolve_claim_failure` re-read shows moved → `DISCARDED_STALE`, no publish,
  newer record intact. Covers the residual `protocol.py:342` miss (98→100%).
- `test_claim_succeeds_when_owner_absent`: legacy record without
  `claim_owner`/`claim_until` satisfies the
  `attribute_not_exists(claim_owner)` OR-branch → claim succeeds, lease granted.
- Both exercise public seam `run_review` only (§4).

## Evaluated, no change (per §6 — no demonstrable in-scope failure)

- **D1 — same-owner redelivery double-publishes. DEMONSTRATED, DEFERRED.**
  Scratch probe (not committed): two `run_review` deliveries, same owner+SHA,
  → `published` + `published`, 2 publish calls. The different-owner duplicate
  is guarded (`DISCARDED_CLAIM_HELD`, existing test); the same-owner retry
  (e.g. Lambda retry after a lost finalize response) re-publishes. Whether an
  ACTIVE short-circuit is wanted is an HLD-level behavior decision, and the
  mode-10 marker reconciliation is T039 scope per the protocol docstring.
  Owner: orchestrator → HLD decision (T034-adjacent). No unilateral fix.
- **D2 — `_establish_confirm` (c-path) writes `incoming_sha` without
  `ReviewState` validation.** Latent only: the (c) path requires `fence() ==
  incoming_sha`, and production fence is a live GitHub head fetch (trusted
  shape). Poisoning needs a hostile fence double. Owner: orchestrator/T034
  input-layering decision. No speculative validation added.
- **D3 — marker hostile content (newlines, `-->`, `#` collisions).**
  `build_marker` is a pure formatter over envelope-validated inputs; spoofing
  defense is validate-owned (carry-forward ⑥, QA-C lane). Owner: QA-C.
  No validation duplicated into marker.py.
- **D4 — canary sync test.** Evaluated: zero occurrences of
  canary/`PROMPT_VERSION`/`VALIDATION_MARKER` in `state.py`, `protocol.py`,
  `marker.py`, or any owned test/stub (grep exit 1). These modules share no
  constants with `prompts/`; no meaningful sync assertion exists from
  state/protocol-owned surfaces. Owner: QA-C (prompts consistency check).
- **D5 — builder int validation / `max_establish_attempts=0` / clock skew.**
  Builders are pure string templates over caller-supplied ints; validation
  lives at the codec boundary by HLD layering. Attempts≤0 and clock-skew
  shapes are caller misuse with no production path. Noted, no change.

## Coverage (measure command per plan)

`uv run --frozen --with pytest-cov pytest --cov=lambda/common
--cov=lambda/ingress_handler --cov-branch --cov-report=term-missing -q`

| Module | Before (main) | After |
| --- | --- | --- |
| state | 100% | 100% |
| protocol | 98% (1 miss: line 342) | 100% |
| marker | 100% | 100% |
| TOTAL | 91% (78 miss) | 94% (37 miss — remainder outside QA-B scope) |

Residual missed lines in QA-B scope: none — no justification needed.
Suite: 420 → 425 passed (+3 anchor red-first, +2 protocol edges).
`tests/conftest.py` untouched (no shared-fixture need arose).

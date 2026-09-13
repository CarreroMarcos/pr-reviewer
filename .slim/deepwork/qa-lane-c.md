# QA Lane C — untrusted content pipeline: findings ledger

Branch: `qa/content-pipeline` · Scope: `lambda/common/{validate,assemble,diff,llm}.py`,
`tests/unit/{test_structural_validation,test_assemble,test_diff,test_llm}.py`
(`prompts/system_prompt.md` read-only). 463 tests green at tip (was 420 on main).

## F1 — ⑥ marker spoofing: verbatim duplicate marker passed the gate (BUG, fixed red-first)

- **Suspicion (Gate-3 carry-forward ⑥):** model content carrying a verbatim T008 marker
  passed validation (probe: 2 markers, `ok=True`).
- **Blast-radius evidence (codegraph, index built 2026-09-13):**
  - `codegraph_callers(validate_comment)` → only test doubles (`recording`,
    `check`); no production caller yet (`worker_handler.py` is the T034 stub,
    `raise NotImplementedError`). Adding a reason code is safe.
  - `codegraph_impact(build_marker)` → `assemble.build_comment` +
    `validate.validate_comment` + marker/validation/assemble tests. Only consumer of
    `reasons` is `AssembleError(verdict)`; no publish path exists yet, so refusing at
    the gate provably prevents any self-referential published comment.
  - Root cause confirmed in code: `validate_comment` checks `marker not in content`
    (satisfied by the worker-injected marker) and the `hidden_html` check strips **all**
    occurrences via `content.replace(marker, "")`, so a verbatim duplicate is fully
    laundered — neither check sees it.
- **Red evidence** (`uv run --frozen pytest tests/unit/test_structural_validation.py
  tests/unit/test_assemble.py -q --tb=short -k spoof` → 9 failed):
  - `E assert True is False` / `+ where True = ValidationVerdict(ok=True, reasons=()).ok`
    (verbatim duplicate accepted by the gate)
  - `E Failed: DID NOT RAISE AssembleError` (spoofed model content assembles to
    publish-ready content)
  - cross-repo spoof refused only as `('hidden_html',)` — no dedicated reason.
- **Fix (`lambda/common/validate.py`, smallest change):** validate-owned `marker_spoofed`
  reason. Exactly one legitimate occurrence excused (`content.replace(marker, "", 1)` —
  the worker prepends its marker first); the remainder is scanned invisible-char-stripped
  (`U+200B/200C/200D/FEFF/00AD`) and case-insensitively for `pr-reviewer\s*:\s*canonical`.
  Catches: verbatim duplicates, cross-repo markers, case variants, zero-width-obfuscated
  markers, truncated prefixes carrying the core, spoofs inside fenced code blocks.
  Reason vocabulary in the module docstring extended (`marker_spoofed`, plus the
  previously undocumented `not_text` — the batch-3 `not_text` docstring-noise item).
- **Green evidence:** `uv run --frozen pytest tests/unit/test_structural_validation.py
  tests/unit/test_assemble.py -q` → `57 passed` (then `463 passed` full suite at tip).
  Propagation pinned test-side: `build_comment` with spoofed `review_content` raises
  `AssembleError` with `marker_spoofed` in `verdict.reasons` — no publish-ready content
  exists on that path.

## F2 — ⑦ assemble.py docstring: empty-output refusal cited queue-retry (doc fix)

- Stale text claimed empty inference output "§2.3 item 8 sends to queue retry". Per the
  Gate-3 record the correct row is the validation row ("Non-retryable: complete and
  alert" — see also ⑧). One-line fix: now reads "which the §2.3 item 8 validation row
  completes non-retryably (alert)". No production-behavior edit.

## F3 — ① diff.py docstring: timeout-split citation (doc fix)

- Per G2① the 2 s/10 s (GitHub) + 2 s/45 s (LLM) split lives in HLD §2.3 line 197, not
  §2.7. The interpretation-4 note named no section; one-line fix pins it explicitly
  ("the HLD §2.3 GitHub read budget (10 s)"). No literal §2.7 string existed in
  `diff.py`; verified by grep before editing. No production-behavior edit.

## F4 — routed finding (NOT fixed): diff transport exceptions bypass `DiffError`

- **Demonstrated:** `fetch_pr_head_sha(..., _transport=<raising TimeoutError>)` propagates
  raw `TimeoutError` — the module's documented `Raises DiffError` contract
  (`bad_repo`/`bad_pr_number`/`http_error`/`bad_shape`/`bad_sha`) has no timeout row, and
  `_default_transport` (`urlopen`, `TIMEOUT_SECONDS = 10`) is the production source.
- **Why not fixed here:** wrapping invents taxonomy (`timeout` vs `http_error`?) that the
  HLD §2.3 item-8 table must define — classification is T034/T054-owned (same reason ⑤⑧
  are test-only). Fixing now would be speculative hardening against an unwritten table.
- **Owner:** T034 worker lane / T054 error classification. Suggested row: transport
  timeout → retryable (transient) with `DiffError`-typed signal; needs HLD backing.

## D1 — ③ capped/Content-Length-guarded response read: DEFERRED (no demonstrated failure)

- **Evaluation:** `_default_transport` does an unbounded `response.read()` before any
  budget applies — the seam is real. But no real failure was demonstrated: the peer is
  the authenticated GitHub API (paginated, `per_page=100`), not attacker-controlled
  bytes; Lambda memory (256 MB worker) + 120 s timeout bound the blast radius; and the
  injected `Transport` contract returns already-materialized `HttpResponse`, so a cap
  changes the seam contract (new `DiffError` reason / streaming read) — speculative
  without HLD backing. A synthetic "huge FakeTransport body" test would prove only that
  in-memory JSON parsing uses memory, not a code bug.
- **Owner + reason:** T034/T035 (worker/deploy) — revisit with HLD-backed transport
  limits if live traffic shows oversized pages; not a QA-C production change.

## R1 — residual: bare homoglyph marker without fences passes (no test enshrined)

- Demonstrated: `<!-- pr-reviewer:canonicаl:… -->` (Cyrillic а U+0430) **fenced** →
  refused via `hidden_html` (defense-in-depth holds whenever fences are present);
  the same string **bare** (no `<!--`/`-->`) → `ok=True`.
- Why residual, not a fix: a bare homoglyph matches no byte-exact tooling and carries no
  fence, so it is inert text, human-confusion only. Full Unicode confusable folding is a
  table-driven feature needing an HLD decision (which scripts fold? false-positive risk
  on legitimate non-Latin review prose?). Per M2 this ledger entry is the disposition;
  no test pins the gap as accepted behavior.

## Test-only pins (no production-behavior edits, no `worker_handler.py` touches)

- **⑤ llm retryability contract (`tests/unit/test_llm.py`):** pins CURRENT emission —
  `bad_endpoint` for empty key/model, non-string/hostless/non-https endpoints;
  `invalid_response` for non-dict bodies, non-list choices, non-dict messages, non-string
  content; lenient usage defaulting (missing/non-dict → zeros); `timeout` vs
  `connection_error` across connect/request/response/factory phases; `http_{status}`
  classes; close-failure never masks success. T054/T051 own the future NON-retryable flip.
- **⑧ AssembleError contract (`tests/unit/test_assemble.py`):** pins `AssembleError` as
  the `ValueError` (complete-and-alert, non-retryable) class carrying the failing
  `verdict` with machine-readable codes in the message; whitespace refusal =
  `missing_sections`. T034 owns the wiring.
- **④ pagination determinism (`tests/unit/test_diff.py`):** multi-page (100 + 2 entries)
  accumulation is order-stable and byte-identical across calls; `Link` headers with evil
  URLs are never followed (constructed URLs only — failure mode 18); short first page
  stops after one `/files` call; empty first page yields empty, non-truncated content.

## Prompts consistency check (read-only — no drift, no edit)

- `PROMPT_VERSION == "v1"` matches `` `prompt_version: v1` `` in
  `prompts/system_prompt.md`; `CANARY_SUBSTRING` appears verbatim (line 84). Verified by
  script (both `True`). No prompt edit per the read-only mandate.

## Adversarial matrix (boundaries 5–7) — disposition per case

- B5 diff transport: constructed-URLs-only (pinned, incl. evil-`Link` test); pagination
  edges empty/short/multi (pinned); timeout taxonomy → F4 routed to T034/T054; response
  size → D1 deferred; truncation correctness (existing budget tests + determinism pin).
- B6 llm: valid-JSON/wrong-schema shapes (pinned: non-dict, non-list choices, non-dict
  message, non-string content, non-dict usage); streaming N/A (single `read()`, no
  stream seam — nothing to edge-test); timeout/retry interplay pinned, retry decision
  worker-owned (⑤).
- B7 validate/assemble: marker spoofing → F1 fixed; verdict integrity pinned (closed
  vocabulary, never echoes input, frozen dataclass); injection shapes in reasons/bodies
  (hostile-mix test); truncation-note gate-safety (existing assemble tests);
  homoglyph/case evasion → F1 covers case/zero-width, R1 residual for bare homoglyphs.

## Coverage before → after (mandated command)

`uv run --frozen --with pytest-cov pytest --cov=lambda/common --cov=lambda/ingress_handler
--cov-branch --cov-report=term-missing -q`

| Module | Before (main) | After (tip) |
| --- | --- | --- |
| diff | 89% (13 missed) | 98% (201–203 only) |
| llm | 89% (8 missed) | 98% (79 + 226→231 only) |
| validate | 96% | 100% |
| assemble | 100% | 100% |
| TOTAL | 91% | 97% (463 passed) |

Justified residuals (lane scope only; logs/envelope/protocol/ingress_handler gaps belong
to QA-A/QA-B):
- `diff.py` 201–203 (`_default_transport` body): requires live network — acceptance
  live-fire territory; exercising it unit-side means testing `urlopen` itself.
- `llm.py` 79 (`_default_factory` one-line constructor): no I/O at construction, but it
  is a private seam — §4 forbids testing privates for coverage.
- `llm.py` 226→231 (false-arc of `if conn is not None:` in `finally`): **provably
  uncoverable, not a gap.** `conn` is `None` at `finally` only when the factory raises,
  i.e. only with an in-flight exception — and coverage records no arcs inside `finally`
  during exceptional unwinding (proven: minimal repro script taking exactly that path
  records zero arcs from line 226, yet prints `connection_error`). Both outcomes are
  behavior-pinned (`test_factory_failure_is_connection_error_today` + every success
  test). No code touch for a measurement artifact (§6).

## Gate evidence (exact commands, all green at tip `qa/content-pipeline`)

- `uv run --frozen pytest -q --tb=short` → `463 passed`
- `uv run --frozen ruff check .` → `All checks passed!`
- `uv run --frozen ruff format --check .` → `50 files already formatted`
  (one self-applied `ruff format` to `tests/unit/test_diff.py` before the final check)
- `pre-commit run --all-files` → all hooks `Passed` (incl. ruff, ruff format, terraform fmt)
- Runtime-import invariant: lane modules import stdlib only (`re`, `json`, `http.client`,
  `urllib`, `logging`, `time`, `dataclasses`, …) — no new deps; `pyproject.toml`/`uv.lock`
  untouched (`git status` shows only the 7 intended files + this ledger).
- Red evidence retained above (F1); tip is green — never pushed a red tip.

## Findings routed to orchestrator

1. **F4** (diff timeout taxonomy) → T034/T054 owner.
2. **D1** (capped response read) → T034/T035 owner, deferred with reason.
3. **R1** (bare-homoglyph residual) → future hardening, needs HLD confusable-table decision.
4. **Ledger location note:** `.slim/` is gitignored (`.gitignore:64`); this file is
   force-added (`git add -f`) per the lane brief's "committed in your PR" instruction —
   revert to untracked if the ignore is intentional for lane ledgers.
5. Shared-fixture need: none (`tests/conftest.py` untouched).

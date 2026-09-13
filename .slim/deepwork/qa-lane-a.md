# QA-A findings ledger — boundary & secrets (`qa/boundary-secrets`)

Lane scope: `lambda/ingress_handler.py`, `lambda/common/{config,envelope,logs}.py`,
`tests/unit/{test_ingress_gating,test_config,test_envelope,test_logs}.py`,
`tests/contracts/test_signed_webhook_fixtures.py`.
No other files touched. Runtime-import invariant holds: production diffs use
`isinstance` only — no new imports (stdlib or otherwise).

> NOTE (routed to orchestrator): `.slim/` is gitignored (`.gitignore:64`), so this
> ledger is committed via `git add -f`. If the phase wants ledgers visible by
> default, the ignore rule needs an exception.

## Findings

### F1 (REAL, fixed red-first): `build_envelope_body` raised on hostile shapes instead of 200-discard

- **Suspicion:** HMAC-valid but schema-hostile payloads must discard with 200
  (module docstring). `action not in ALLOWED_ACTIONS` raises `TypeError` for
  unhashable actions; `repository/head/base/sender .get()` raises
  `AttributeError` for truthy non-dict shapes. Only `EnvelopeError` was caught.
- **Blast-radius evidence (codegraph):** `codegraph_callers(build_envelope_body)`
  → exactly one production caller: `handler` (`lambda/ingress_handler.py:199`).
  `codegraph_impact(build_envelope_body, depth 2)` → `handler` + 15 contract/unit
  tests through it. An escaping exception bypasses the HLD §2.1 step-4 → 200
  contract and surfaces as a Lambda error (retry-inducing) instead of a discard.
- **Red evidence** — `uv run --frozen pytest tests/unit/test_ingress_gating.py
  tests/unit/test_config.py -q --tb=short -k "unhashable or string_repository or
  string_sender or string_head or list_base or subset_parameter"` (commit `71a4655`):
  ```text
  lambda/ingress_handler.py:256: in handler
      envelope_body = build_envelope_body(payload, delivery_guid)
                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  lambda/ingress_handler.py:185: in build_envelope_body
      "base_sha": base.get("sha"),
                  ^^^^^^^^
  E   AttributeError: 'list' object has no attribute 'get'
  _______________ test_subset_parameter_names_raise_typed_missing ________________
  tests/unit/test_config.py:225: in test_subset_parameter_names_raise_typed_missing
      provider.get()
  lambda/common/config.py:113: in get
      self._cached = self._fetch()
                     ^^^^^^^^^^^^^
  lambda/common/config.py:139: in _fetch
      self._check_endpoint(values["glm_endpoint"])
                           ^^^^^^^^^^^^^^^^^^^^^^
  E   KeyError: 'glm_endpoint'
  =========================== short test summary info ============================
  FAILED tests/unit/test_ingress_gating.py::test_unhashable_action_discarded - ...
  FAILED tests/unit/test_ingress_gating.py::test_string_repository_discarded - ...
  FAILED tests/unit/test_ingress_gating.py::test_string_sender_discarded - Attr...
  FAILED tests/unit/test_ingress_gating.py::test_string_head_discarded - Attrib...
  FAILED tests/unit/test_ingress_gating.py::test_list_base_discarded - Attribut...
  FAILED tests/unit/test_config.py::test_subset_parameter_names_raise_typed_missing
  6 failed, 35 deselected in 0.37s
  ```
  (Probe pre-check also showed `TypeError: unhashable type: 'list'` for a list
  action and `AttributeError` for string repository/sender/head.)
- **Fix** (`lambda/ingress_handler.py`): `isinstance(action, str)` guard before
  the allow-list test; `isinstance(..., dict)` coercion to `{}` for
  repository/head/base/sender. Dict behavior unchanged; hostile shapes now flow
  into `validate_envelope` → `EnvelopeError` → `None` → 200.
- **Green evidence** — same `-k` filter after fix: `6 passed, 35 deselected in 0.16s`.
  Full suite: `478 passed`.

### F2a (REAL, fixed red-first): subset `parameter_names` raised bare `KeyError` instead of typed `ConfigError`

- **Suspicion:** module docstring promises missing/invalid parameters raise typed
  `ConfigError`. A provider constructed with a subset of names crashed with
  `KeyError: 'glm_endpoint'` at `values["glm_endpoint"]`.
- **Blast-radius evidence:** `grep ConfigProvider|parameter_names` across the
  worktree → constructed only in `tests/unit/test_config.py`; no production
  caller yet (worker is a T034 stub). Fix blast radius is therefore confined to
  `_fetch` internals: default-names behavior is unchanged (all five logical
  fields present → new loop is a no-op).
- **Red evidence:** same command as F1 (6th failure, `KeyError: 'glm_endpoint'`
  verbatim above, commit `71a4655`).
- **Fix** (`lambda/common/config.py`): after the fetch loop, require the five
  logical fields (`github_token`, `webhook_secret`, `glm_api_key`, `glm_model`,
  `glm_endpoint`), raising `ConfigError(field, "missing")` for the first absent
  one.
- **Green evidence:** same as F1.

### Evaluated, NO change (§6 — no real failure mode demonstrated)

- **E1 — `verify_signature` with non-str secret (int/None) raises `TypeError`.**
  Blast radius: sole production caller is `handler`; secret sources are SSM
  `Value` (boto3 contract: always `str`), the warm cache (stores that `str`),
  and `_secret` (test injection only). Unreachable in production → speculative
  hardening, declined.
- **E2 — `assert_clean(non-str)` raises `TypeError`.** Every internal call site
  passes `str` (isinstance/regex-checked first; `emit` serializes before
  scanning). Public-misuse only → declined.
- **E3 — `config._fetch` assumes boto3 `Parameters` row shape (`p["Name"]` /
  `p["Value"]`).** Response shape is boto3-controlled, not attacker-controlled;
  a malformed row raising `KeyError` carries the key name, never a secret value
  → declined (reasoned rule-out, no probe).
- **E4 — endpoint URL with userinfo/whitespace.** Host allow-list still enforced
  on `hostname`; endpoint string is not secret material → declined.
- **E5 — uppercase `SHA256=` not matched by the `sha256=` guard pattern.**
  `status`/`error_class` charsets otherwise constrain content; an uppercase
  marker carries no credential → declined.
- **E6 — `handler` with a non-dict event raises `AttributeError`.** Lambda proxy
  events are always dicts → declined.

## Adversarial matrix (M2 boundaries 1–4) — case → test/ledger

1. **Webhook HMAC/signature, body limits, headers:** unhashable action,
   non-dict repository/sender/head/base → F1 tests (200 discard); non-str/int/
   list signature, empty suffix, whitespace-padded (no-strip fail-closed),
   bytes-secret verify → `test_ingress_gating` + fixtures end-to-end (401);
   non-numeric/oversized/within-bound Content-Length, non-str body, valid/
   invalid base64 → `test_ingress_gating`; SSM/DynamoDB outage mapping,
   production zero-injection wiring (boto3 doubled), lazy table/SQS branches →
   `test_ingress_gating`; committed vectors unchanged → fixtures file.
   E1/E6 → evaluated, no change (above).
2. **SSM hydration + config accessors:** subset names → F2a test; whitespace-only
   and non-string values (fail-closed `empty`, leak-free message), exact-TTL
   refetch → `test_config.py`; repr/error non-exposure (pre-existing, kept green).
   E3/E4 → evaluated, no change.
3. **Log redaction:** per-cleaner rejects via `build_event` (non-str/overlong
   repo, homoglyph repo, bad pr_number/sha/guid, non-str/empty/overlong
   prompt_version, `True` generation, overlong error_class), PAT-in-error_class /
   bearer-in-status / bearer-in-prompt_version guard trips, nested hostile
   extras never emitted, non-dict/non-string-keyed emit rejects, uppercase GUID
   acceptance → `test_logs.py`. Residual 168–169 → justified below. E2/E5 →
   evaluated, no change.
4. **Envelope parsing:** extra (incl. hostile) fields ignored, unicode repo
   reject, uppercase GUID accept, hyphen-less GUID reject, newline anchoring
   (pre-existing) → `test_envelope.py`. Residual 122–123 → justified below.

## Coverage (measure: `uv run --frozen --with pytest-cov pytest --cov=ingress_handler --cov=common --cov-branch --cov-report=term-missing -q`)

| Module | Before (420 tests) | After (478 tests) |
| --- | --- | --- |
| ingress_handler | 75% (38 miss, 8 BrPart) | **100%** |
| config | 100% | **100%** |
| logs | 86% (12 miss, 10 BrPart) | **99%** (168–169) |
| envelope | 98% (122–123) | **98%** (122–123) |

Justified residuals (not contortions — unreachable defense-in-depth retained):
- `envelope.py:122-123` + `logs.py:168-169`: `uuid.UUID(guid)` → `ValueError`
  fallback after a strict hex-shape regex. Evidence: 20,000 random
  regex-matching GUIDs + zero/FF/mixed-case edge samples → **0** `uuid.UUID`
  rejections (probe via `uv run --frozen python`, random seed 7). Any string the
  regex accepts parses; the `except` arm cannot fire.

## Gate evidence (worktree `/tmp/opencode/qa-a`, tip at push)

- `uv run --frozen pytest -q --tb=short` → `478 passed`
- `uv run --frozen ruff check .` → `All checks passed!`
- `uv run --frozen ruff format --check .` → `50 files already formatted`
- `pre-commit run --all-files` → all hooks `Passed` (incl. ruff, ruff-format,
  detect secrets/private-key)
- Coverage re-measure → table above (TOTAL 96%, scope modules ≥98%)

# T070 verifier hardening — kill-set re-run evidence (T046 R1 carrier)

Ticket: SPR-160 / 004-T070 · Captures 2026-09-28/29 · Scorer: `t046_exit_scoring.py`
Pin of record: `pinned_multi_agent.json` sha256 `63afe5e59f2658716d169876ce20d3793f3bb94768c43ef80e8696be46ed9015` (4,769,309 bytes, 72 runs, 0 errored).

## Wrongful-kill count, before → after

| Round | Wrongful kills (of 15 kill-set) | Notes |
| --- | --- | --- |
| Before (T046 exit report) | **10** | all location-arm, cosine 0 — kill gate too eager |
| Attempt 1 (verifier hardening only) | **6** | pin sha `d8ea9b19…` (backed up `/tmp/opencode/pinned_multi_agent.r1-verifier-only.json`) — 1/72 errored; **floors FAIL**: synthesizer laundered injection payload quotes into the posted comment |
| Attempt 2 (verifier + synthesizer §5 rule) — **pin of record** | **3** | floors PASS, 0 forbidden hits, injection comments clean 3/3 |

The 0-wrongful-kill promotion gate (T050 activation bar, HLD D8) remains red at 3 — recorded honestly. T070's own verify bar ("wrongful kills drop without new missed-kill regressions") is met:

- Wrongful kills 10 → 3 (−70%).
- Missed-kill check: recall delta **+0.037** (before-side +0.0185 — improved, no regression); subtle violations **2 → 0**; fidelity dropped/invented **0**; fabricated **0**.
- No kill-gate recalibration, no D8 spec change (Mars ruling 2026-09-28): prompt-only — `prompts/verifier.md` (verify-or-escalate for location-pinned candidates, test-code parity) and, after attempt 1's floors fail, `prompts/synthesizer.md` §5 precedence rule (describe attack payloads in own words; the floors doctrine "must not launder injections even as quotations" names the synthesizer as owner). Fidelity matching is location-based, so paraphrase is safe by construction.

## Trap vetting (fixtures `TRAP_VETTING`)

Both former kill-arm traps flipped to `generator-avoidance` on live evidence — decoys never proposed across **9 runs each** (3 re-vets + capture r1 ×3 + capture r2 ×3):

- `gil_atomic_copy`: candidates were the active_names filtering behavior + its missing tests; race decoy never emitted.
- `injected_finding_order`: only a tests-coverage candidate (one run proposed nothing); the fabricated "SQL injection at line 9" never emitted.

Verifier sha `9cf782e33a53e0bc07c8ed2d7320c65585a66714d4a72808dd18e64213b98eb1` (live file = pin meta = fixtures pin). Synthesizer `cc285b2bf5e8015f8dbbafdaaba0390413c9028eb434e5696dd6fd270c397218` (not trap-pinned).

## T071 precision read (same run, R2 carrier)

p1 = +0.0845 (pre-R1) , p2 = +0.037 (this capture) → mean **+0.0608 < 0.08** → **real precision fail = STOP + revisit D9 before T050** (SPR-161 → Needs input). Robustness: attempt 1's errored run scored as precision-0; excluding it moves p2 by ≈ +0.0005 — the conclusion does not hinge on error handling.

Methodology note (bot review F4, 2026-09-29): the mean deliberately crosses
prompt revisions — that is T071's own pre-ratified contract ("p=+0.0845 landed
inside the [0.05, 0.15) re-run band; one re-capture decides — pass iff mean ≥
0.08"), not a pooling choice made here. The fail does not hinge on the pooling:
p2 = +0.037 stands alone under the current prompt state and is itself far below
the 0.08 line.

## Durability boundaries (bot review F2, 2026-09-29)

The attempt-1 (verifier-only) pin backup lives at `/tmp/opencode/pinned_multi_agent.r1-verifier-only.json`
(4,953,663 bytes) and is **ephemeral** — it is provenance color for the 10→6
intermediate, not a load-bearing artifact. Durable attempt-1 evidence: the
numbers recorded here and in DECISIONS.md (2026-09-29 entries), the scoring
output of record, and the checkpoint file `results/multi-run-20260928-r1-checkpoint.json`
(on disk, untracked). The pin of record (this capture, sha `63afe5e5…`) is the
only artifact whose per-run data backs live gate verdicts.

## Provenance

- Capture r2: `results/multi-run-20260929-r2-checkpoint.json`, 72/72 completed, wall 2831.8 s, FANOUT_CONCURRENCY=3.
- Attempt-1 (verifier-only) capture: `results/multi-run-20260928-r1-checkpoint.json`.
- Scoring output: `results/t046-scoring.json`; manifest: `results/multi-pin-manifest.json`.

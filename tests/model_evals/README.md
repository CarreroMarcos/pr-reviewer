Pinned model-eval set + Q2 rubric (T058, HLD §4.4; open-questions Q2).
Corpus: 15 cases — 13 seeded defects (representative SQLi + 12 categories)
plus robustness probes (prompt-injection, 591KB large/truncation padding).
Offline: pytest scores pinned_outputs.json only — never calls the LLM.
Re-capture: `aws login`, export creds, then `python tests/model_evals/capture.py` (--force to overwrite).
Staleness: CI fails here on any prompt_version/system-prompt change until re-captured.
Cost: ~15 calls ≈ $0.05–0.10 per capture (single pass, never retried by this tool).
Capture is out-of-band per HLD §4.4 item 3: humans run it, CI never does.
results/baseline.json is the Q2 baseline: per-case + aggregate metrics, drift-guarded in CI.
Model string is recorded in pinned meta for humans; offline checks pin version + prompt SHA.
Fixtures are deterministic builders; the large diff is generated, never committed as bytes.
No quality bars yet — this baseline anchors later A/Bs; CI asserts validity + metric consistency.

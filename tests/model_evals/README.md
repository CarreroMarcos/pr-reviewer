Pinned model-eval set + rubric (T058, HLD §4.4).
Offline: pytest scores pinned_outputs.json only — never calls the LLM.
Re-capture: `aws login`, export creds into the environment first.
Then run `python tests/model_evals/capture.py` (--force to overwrite).
Staleness: CI fails here on any prompt_version/system-prompt change.
A human must re-capture after any such change (HLD §4.4 item 4).
Cost: ~$0.03 per capture (3 model calls, never retried by this tool).
Capture is out-of-band per HLD §4.4 item 3: humans run it, CI never does.
Model string is recorded in pinned meta for humans; offline checks pin version + prompt SHA.
Fixtures are deterministic builders; the large diff is generated, never committed as bytes.

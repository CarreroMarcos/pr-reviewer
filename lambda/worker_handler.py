"""Thin worker Lambda entry point (plan.md Project Structure).

All logic lives in lambda/common/. The SQS pipeline (envelope -> state ->
diff -> LLM -> validate gate -> publish -> finalize, HLD §3.3) is
implemented in T034; this stub only anchors the packaging boundary.
"""


def handler(event: dict, context: object) -> dict:
    """SQS batch event (batch_size=1) -> per-record completion. Implemented in T034."""
    raise NotImplementedError

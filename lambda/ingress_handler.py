"""Thin ingress Lambda entry point (plan.md Project Structure).

All logic lives in lambda/common/. The §2.1 responsibilities (normalize,
HMAC, event gate, action filter, dedup, enqueue, delivery marker) are
implemented in T030; this stub only anchors the packaging boundary.
"""


def handler(event: dict, context: object) -> dict:
    """Function-URL proxy event -> HTTP response. Implemented in T030."""
    raise NotImplementedError

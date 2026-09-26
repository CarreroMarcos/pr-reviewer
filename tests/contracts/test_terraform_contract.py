"""Three-way queue contract: terraform/messaging.tf ↔ worker constant ↔
failure-notice default (HLD §2.2).

Parses the terraform TEXT (no terraform binary needed) and asserts the
redrive budget and visibility window cannot drift from the code that
mirrors them:

* `maxReceiveCount` == `worker_handler._MAX_RECEIVE_COUNT` ==
  `common.failure_notice.DEFAULT_MAX_RECEIVE_COUNT` — the worker derives
  final-attempt notice timing from this value;
* `visibility_timeout_seconds` == 1800 == 2 × the worker Lambda timeout
  (terraform/compute.tf) — the safety proof: an invocation can never
  outlive the invisibility window, so no concurrent duplicate
  processing; failed messages redeliver at ~30 min, which is the delay
  the retrying notice quotes to users.
"""

import re
from pathlib import Path

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
MESSAGING_TF = (TERRAFORM_DIR / "messaging.tf").read_text(encoding="utf-8")
COMPUTE_TF = (TERRAFORM_DIR / "compute.tf").read_text(encoding="utf-8")

EXPECTED_VISIBILITY = 1800
EXPECTED_MAX_RECEIVE_COUNT = 3
EXPECTED_WORKER_TIMEOUT = 900


def _int_assignment(text: str, key: str) -> int:
    """Value of the FIRST `key = <int>` assignment (HCL, `=` style)."""
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(\d+)\s*(?:#.*)?$", text, re.MULTILINE)
    assert match is not None, f"{key!r} not found in terraform text"
    return int(match.group(1))


def _max_receive_count() -> int:
    match = re.search(r"maxReceiveCount\s*=\s*(\d+)", MESSAGING_TF)
    assert match is not None, "maxReceiveCount not found in redrive policy"
    return int(match.group(1))


def test_visibility_timeout_is_1800_seconds():
    assert _int_assignment(MESSAGING_TF, "visibility_timeout_seconds") == EXPECTED_VISIBILITY


def test_terraform_max_receive_count_is_3():
    assert _max_receive_count() == EXPECTED_MAX_RECEIVE_COUNT


def test_worker_constant_matches_terraform_max_receive_count():
    from worker_handler import _MAX_RECEIVE_COUNT

    assert _MAX_RECEIVE_COUNT == _max_receive_count()


def test_failure_notice_default_matches_terraform_max_receive_count():
    from common.failure_notice import DEFAULT_MAX_RECEIVE_COUNT

    assert DEFAULT_MAX_RECEIVE_COUNT == _max_receive_count()


def _worker_lambda_timeout() -> int:
    """`timeout` inside the `aws_lambda_function" "worker"` block (the
    ingress function declares its own, smaller, timeout first)."""
    block = re.search(r'resource "aws_lambda_function" "worker" \{', COMPUTE_TF)
    assert block is not None, "worker lambda resource not found"
    match = re.search(
        r"^\s*timeout\s*=\s*(\d+)\s*(?:#.*)?$", COMPUTE_TF[block.end() :], re.MULTILINE
    )
    assert match is not None, "worker timeout not found"
    return int(match.group(1))


def test_visibility_covers_worker_timeout_no_duplicate_processing():
    """Safety proof: visibility >= worker timeout means an invocation can
    never outlive the invisibility window (no concurrent duplicate
    processing); the pinned ratio is exactly 2× (one full retry gap of
    ~30 min between deliveries)."""
    worker_timeout = _worker_lambda_timeout()
    assert worker_timeout == EXPECTED_WORKER_TIMEOUT
    visibility = _int_assignment(MESSAGING_TF, "visibility_timeout_seconds")
    assert visibility >= worker_timeout
    assert visibility == 2 * worker_timeout

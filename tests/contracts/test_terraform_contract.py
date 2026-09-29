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
* ESM `batch_size` == 1 (terraform/compute.tf) — the ordering-semantics
  pin (see the ordering comment in messaging.tf): one message per worker
  invocation, so the worker never reorders within a batch.
"""

import re
from pathlib import Path

from common.mutex import MUTEX_LEASE_TTL_S

TERRAFORM_DIR = Path(__file__).resolve().parent.parent.parent / "terraform"
MESSAGING_TF = (TERRAFORM_DIR / "messaging.tf").read_text(encoding="utf-8")
COMPUTE_TF = (TERRAFORM_DIR / "compute.tf").read_text(encoding="utf-8")

EXPECTED_VISIBILITY = 1800
EXPECTED_MAX_RECEIVE_COUNT = 3
EXPECTED_WORKER_TIMEOUT = 900
EXPECTED_BATCH_SIZE = 1


def _hcl_canary(match: re.Match[str] | None, what: str) -> re.Match[str]:
    """T065 (Gate-13 aside): every terraform-text extraction is bounded
    by this canary. A None match means the HCL SHAPE changed (reformat,
    rename, restructure) — not that a pinned value drifted — so fail
    with the loud, actionable message instead of a bare "not found"."""
    assert match is not None, (
        f"HCL SHAPE CHANGED: {what} no longer matches the expected "
        "terraform layout — a reformat/rename broke this contract-file "
        "extraction. Update the extraction; do not weaken the pin."
    )
    return match


def _int_assignment(text: str, key: str) -> int:
    """Value of the FIRST `key = <int>` assignment (HCL, `=` style)."""
    match = _hcl_canary(
        re.search(rf"^\s*{re.escape(key)}\s*=\s*(\d+)\s*(?:#.*)?$", text, re.MULTILINE),
        f"{key!r} assignment",
    )
    return int(match.group(1))


def _max_receive_count() -> int:
    match = _hcl_canary(
        re.search(r"maxReceiveCount\s*=\s*(\d+)", MESSAGING_TF),
        "maxReceiveCount in the messaging.tf redrive policy",
    )
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


def test_esm_batch_size_is_one():
    """Ordering pin (messaging.tf ordering comment): the ESM delivers one
    message per worker invocation — a drift here silently changes the
    delivery semantics that comment describes."""
    assert _int_assignment(COMPUTE_TF, "batch_size") == EXPECTED_BATCH_SIZE


def _worker_lambda_timeout() -> int:
    """`timeout` inside the `aws_lambda_function" "worker"` block (the
    ingress function declares its own, smaller, timeout first)."""
    block = _hcl_canary(
        re.search(r'resource "aws_lambda_function" "worker" \{', COMPUTE_TF),
        '`resource "aws_lambda_function" "worker"` block in compute.tf',
    )
    match = _hcl_canary(
        re.search(r"^\s*timeout\s*=\s*(\d+)\s*(?:#.*)?$", COMPUTE_TF[block.end() :], re.MULTILINE),
        "`timeout` assignment inside the worker lambda block",
    )
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


def test_mutex_lease_ttl_matches_worker_timeout_three_way():
    """T063 (D9 lease TTL pin, Gate-15): the mutex lease constant, the
    worker-timeout constant, and the parsed compute.tf worker timeout
    are one value — the mutex module and the terraform contract pin it
    separately, so without this link either side can drift silently
    while the other stays green."""
    assert MUTEX_LEASE_TTL_S == EXPECTED_WORKER_TIMEOUT
    assert MUTEX_LEASE_TTL_S == _worker_lambda_timeout()

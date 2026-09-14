"""T035: US1 live acceptance harness (HLD §7.3; QS-a/b/e/f/k/k2).

Live-only test module: every test here POSTs signed deliveries to the
deployed scratch ingress Function URL, drives PR 22 on CarreroMarcos/pr-reviewer
via `gh`, and asserts end-to-end behaviour:

  (a) signed action=opened delivery → HTTP 202, delivery logged in DynamoDB
  (b) exactly ONE canonical comment on PR 22 within ≤15 s end-to-end
  (e) wrong signature → HTTP 401, nothing enqueued (work queue depth unchanged)
  (f) DLQ empty on the happy path
  (k) close → reopen PR 22 + signed action=reopened delivery → canonical
      comment refreshed (updated_at moves past pre-reopen value, still one)
  (k2) signed X-GitHub-Event `label` / `issues` deliveries → 200 discard

Opt-in gate: the module SKIPS unless ACCEPTANCE_LIVE=1, so the default
`pytest -q` unit path stays green without AWS/gh access. The T035 verify is::

    ACCEPTANCE_LIVE=1 uv run --frozen pytest \\
        tests/integration/test_us1_acceptance.py -q --tb=short -m integration

Env config (all but ACCEPTANCE_LIVE have scratch-stack defaults):

  ACCEPTANCE_FUNCTION_URL  ingress Function URL
  ACCEPTANCE_REPO          e.g. CarreroMarcos/pr-reviewer
  ACCEPTANCE_PR            e.g. 22
  ACCEPTANCE_HEAD_SHA / ACCEPTANCE_BASE_SHA  live PR SHAs (defaults: resolved
      once via `gh api` at session start; explicit env wins)
  AWS_REGION               default us-west-2
  ACCEPTANCE_TABLE         default pr-reviewer-state
  ACCEPTANCE_WORK_QUEUE    default pr-reviewer-work
  ACCEPTANCE_DLQ           default pr-reviewer-dlq
  ACCEPTANCE_SSM_SECRET    default /pr-reviewer/webhook-secret

Order matters (pytest runs definition order): (e) runs FIRST so its
nothing-enqueued assertion sees a quiet stack; (a)→(b)→(f) form the happy
path; (k) mutates PR state; (k2) discards last. Shared state travels in the
module-level `_STATE` dict — no sleeps without polling.

Secrets: the webhook secret is read from SSM at runtime and is NEVER printed,
logged, or embedded in assertion messages.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.request
import uuid

import boto3
import pytest

from common.marker import build_marker

pytestmark = pytest.mark.integration

if os.environ.get("ACCEPTANCE_LIVE") != "1":
    pytest.skip(
        "live acceptance only: set ACCEPTANCE_LIVE=1 to run against the scratch stack",
        allow_module_level=True,
    )

FUNCTION_URL = os.environ.get(
    "ACCEPTANCE_FUNCTION_URL",
    "https://og4jaj6uskhf2zcwpydshk7kjq0ceick.lambda-url.us-west-2.on.aws/",
)
REPO = os.environ.get("ACCEPTANCE_REPO", "CarreroMarcos/pr-reviewer")
PR_NUMBER = int(os.environ.get("ACCEPTANCE_PR", "22"))
REGION = os.environ.get("AWS_REGION", "us-west-2")
TABLE_NAME = os.environ.get("ACCEPTANCE_TABLE", "pr-reviewer-state")
WORK_QUEUE_NAME = os.environ.get("ACCEPTANCE_WORK_QUEUE", "pr-reviewer-work")
DLQ_NAME = os.environ.get("ACCEPTANCE_DLQ", "pr-reviewer-dlq")
SECRET_NAME = os.environ.get("ACCEPTANCE_SSM_SECRET", "/pr-reviewer/webhook-secret")

REVIEW_BUDGET_SECONDS = 15.0  # (b): hard end-to-end bound, never loosened
REOPEN_BUDGET_SECONDS = 60.0  # (k): close/reopen + refresh convergence
POLL_INTERVAL_SECONDS = 1.0

_STATE: dict[str, object] = {}
_SECRET_CACHE: dict[str, str] = {}


# --- helpers --------------------------------------------------------------


def _gh_api(*args: str) -> object:
    """Run `gh api <args...>` and return the parsed JSON payload."""
    proc = subprocess.run(  # noqa: S603 (fixed argv, no shell)
        ["gh", "api", *args],  # noqa: S607 (gh CLI authenticated on this box)
        capture_output=True,
        text=True,
        timeout=60,
        shell=False,
    )
    assert proc.returncode == 0, f"gh api {' '.join(args)} failed: {proc.stderr[:300]}"
    return json.loads(proc.stdout)


def _resolve_shas() -> tuple[str, str]:
    """Live head/base SHAs: explicit env wins, else `gh api` on the target PR."""
    head = os.environ.get("ACCEPTANCE_HEAD_SHA")
    base = os.environ.get("ACCEPTANCE_BASE_SHA")
    if head and base:
        return head, base
    pr = _gh_api(f"repos/{REPO}/pulls/{PR_NUMBER}")
    assert isinstance(pr, dict)
    return str(pr["head"]["sha"]), str(pr["base"]["sha"])


def _webhook_secret() -> str:
    """Fetch the signing secret from SSM (cached; never logged)."""
    if "value" not in _SECRET_CACHE:
        ssm = boto3.client("ssm", region_name=REGION)
        param = ssm.get_parameter(Name=SECRET_NAME, WithDecryption=True)
        _SECRET_CACHE["value"] = str(param["Parameter"]["Value"])
    return _SECRET_CACHE["value"]


def _sign(raw: bytes) -> str:
    secret = _STATE.get("webhook_secret")
    if not secret:
        pytest.skip("prerequisite steps not run; run the full module in order")
    assert isinstance(secret, str)
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _require_state(*names: str) -> None:
    """Skip when prerequisite steps did not run (subset/shuffled runs)."""
    if any(name not in _STATE for name in names):
        pytest.skip("prerequisite steps not run; run the full module in order")


def _post_delivery(
    payload: dict[str, object],
    event_type: str = "pull_request",
    signature: str | None = None,
) -> tuple[int, str, str]:
    """POST one signed delivery with a fresh GUID. Returns (status, body, guid)."""
    # The ingress verifies the HMAC over the exact raw request bytes, so the
    # harness must sign the exact bytes it sends.
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    guid = str(uuid.uuid4())
    request = urllib.request.Request(  # noqa: S310 (https-only Function URL)
        FUNCTION_URL,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature if signature is not None else _sign(raw),
            "X-GitHub-Event": event_type,
            "X-GitHub-Delivery": guid,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, response.read().decode(), guid
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), guid


def _pr_payload(action: str, head_sha: str, base_sha: str) -> dict[str, object]:
    """Minimal realistic pull_request webhook payload (mirrors contract fixtures)."""
    return {
        "action": action,
        "number": PR_NUMBER,
        "pull_request": {
            "base": {"sha": base_sha},
            "draft": False,
            "head": {"sha": head_sha},
            "number": PR_NUMBER,
        },
        "repository": {"full_name": REPO},
        "sender": {"login": "acceptance-harness"},
    }


def _canonical_comments() -> list[dict[str, object]]:
    """Issue comments on the target PR whose body carries the canonical marker."""
    marker = build_marker(REPO, PR_NUMBER)
    comments = _gh_api(f"repos/{REPO}/issues/{PR_NUMBER}/comments", "--paginate")
    assert isinstance(comments, list)
    return [c for c in comments if isinstance(c, dict) and marker in str(c.get("body", ""))]


def _queue_depths(queue_url: str) -> dict[str, int]:
    sqs = boto3.client("sqs", region_name=REGION)
    attrs = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
        ],
    )["Attributes"]
    return {key: int(attrs.get(key, "0")) for key in attrs}


def _queue_url(name: str) -> str:
    sqs = boto3.client("sqs", region_name=REGION)
    return str(sqs.get_queue_url(QueueName=name)["QueueUrl"])


def _wait_for(
    predicate: str,
    deadline: float,
    what: str,
) -> tuple[list[dict[str, object]], float]:
    """Poll canonical comments until `predicate` holds or the deadline passes."""
    start = time.monotonic()
    while True:
        comments = _canonical_comments()
        now = time.monotonic()
        if predicate == "exactly-one" and len(comments) == 1:
            return comments, now - start
        if predicate == "refreshed":
            _require_state("comment_updated_at")
            before = str(_STATE["comment_updated_at"])
            if len(comments) == 1 and str(comments[0].get("updated_at", "")) > before:
                return comments, now - start
        elapsed = now - start
        assert elapsed < deadline, f"{what}: timed out after {elapsed:.1f}s"
        time.sleep(POLL_INTERVAL_SECONDS)


# --- session setup --------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _live_config() -> None:
    if "ACCEPTANCE_REPO" not in os.environ or "ACCEPTANCE_PR" not in os.environ:
        if os.environ.get("ACCEPTANCE_ALLOW_DEFAULT_TARGET") != "1":
            pytest.fail(
                "refusing to target the default live repo/PR: set ACCEPTANCE_REPO "
                "and ACCEPTANCE_PR explicitly (this harness mutates PR state), or "
                "set ACCEPTANCE_ALLOW_DEFAULT_TARGET=1 to allow the default target"
            )
    head_sha, base_sha = _resolve_shas()
    _STATE["head_sha"] = head_sha
    _STATE["base_sha"] = base_sha
    _STATE["work_url"] = _queue_url(WORK_QUEUE_NAME)
    _STATE["dlq_url"] = _queue_url(DLQ_NAME)
    secret = _webhook_secret()
    assert secret, "webhook secret from SSM is empty"
    _STATE["webhook_secret"] = secret


# --- (e) bad signature → 401, nothing enqueued (runs FIRST) ----------------


def test_01_e_bad_signature_rejected_nothing_enqueued() -> None:
    """QS-e: wrong HMAC → 401, empty body, work-queue depth unchanged, DLQ at 0."""
    _require_state("work_url", "dlq_url", "head_sha", "base_sha", "webhook_secret")
    work_url = str(_STATE["work_url"])
    dlq_url = str(_STATE["dlq_url"])
    before = _queue_depths(work_url)["ApproximateNumberOfMessages"]
    payload = _pr_payload("opened", str(_STATE["head_sha"]), str(_STATE["base_sha"]))
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    bad = "sha256=" + "0" * 64
    assert bad != _sign(raw)  # guard: the forged signature must actually differ
    status, body, _guid = _post_delivery(payload, signature=bad)
    assert status == 401, f"expected 401, got {status}"
    assert body == "", f"expected empty body, got {body!r}"
    after = _queue_depths(work_url)
    assert after["ApproximateNumberOfMessages"] == before, (
        f"work queue moved on bad signature: {before} -> {after}"
    )
    dlq = _queue_depths(dlq_url)
    assert dlq["ApproximateNumberOfMessages"] == 0, f"DLQ not empty: {dlq}"


# --- (a) signed opened → 202, delivery logged -------------------------------


def test_02_a_opened_accepted_and_logged() -> None:
    """QS-a: signed action=opened → 202 empty body + delivery row in DynamoDB."""
    _require_state("head_sha", "base_sha", "webhook_secret")
    payload = _pr_payload("opened", str(_STATE["head_sha"]), str(_STATE["base_sha"]))
    status, body, guid = _post_delivery(payload)
    assert status == 202, f"expected 202, got {status} body={body!r}"
    assert body == "", f"expected empty body, got {body!r}"
    _STATE["opened_guid"] = guid
    _STATE["opened_at"] = time.monotonic()
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    item = table.get_item(Key={"pk": f"delivery:{guid}"}).get("Item")
    assert item is not None, f"delivery row missing for guid {guid}"
    _STATE["opened_guid_logged"] = True


# --- (b) exactly one canonical comment ≤15 s --------------------------------


def test_03_b_exactly_one_canonical_comment_within_budget() -> None:
    """QS-b: one marker-bearing comment end-to-end ≤15 s from the (a) delivery."""
    _require_state("opened_guid_logged", "opened_at")
    assert _STATE.get("opened_guid_logged") is True, "test_02_a must run first"
    comments, waited = _wait_for("exactly-one", REVIEW_BUDGET_SECONDS, "first review")
    end_to_end = time.monotonic() - float(_STATE["opened_at"])
    assert len(comments) == 1, f"expected exactly 1 canonical comment, got {len(comments)}"
    assert end_to_end <= REVIEW_BUDGET_SECONDS, (
        f"review took {end_to_end:.1f}s, budget is {REVIEW_BUDGET_SECONDS:.0f}s"
    )
    _STATE["comment_id"] = comments[0]["id"]
    _STATE["comment_updated_at"] = str(comments[0].get("updated_at", ""))
    print(
        f"\n(b) comment id={comments[0]['id']} poll-wait={waited:.1f}s end-to-end={end_to_end:.1f}s"
    )


# --- (f) DLQ empty on the happy path ----------------------------------------


def test_04_f_dlq_empty_on_happy_path() -> None:
    """QS-f: no DLQ entries after the (a)/(b) happy path."""
    _require_state("dlq_url")
    dlq = _queue_depths(str(_STATE["dlq_url"]))
    assert dlq["ApproximateNumberOfMessages"] == 0, f"DLQ not empty: {dlq}"


# --- (k) close → reopen → fresh review --------------------------------------


def test_05_k_reopen_produces_fresh_review() -> None:
    """QS-k/D1: close + reopen PR 22, signed reopened delivery (same head SHA).

    Same-SHA replay is allowed per plan D1; the canonical comment must refresh
    (updated_at moves past the pre-reopen value) with still exactly one.
    """
    _require_state("comment_updated_at", "head_sha", "base_sha", "webhook_secret")
    before_updated_at = str(_STATE["comment_updated_at"])
    _gh_api(f"repos/{REPO}/pulls/{PR_NUMBER}", "-X", "PATCH", "-f", "state=closed")
    try:
        state = _gh_api(f"repos/{REPO}/pulls/{PR_NUMBER}")
        assert isinstance(state, dict) and state.get("state") == "closed"
    finally:
        _gh_api(f"repos/{REPO}/pulls/{PR_NUMBER}", "-X", "PATCH", "-f", "state=open")
    state = _gh_api(f"repos/{REPO}/pulls/{PR_NUMBER}")
    assert isinstance(state, dict) and state.get("state") == "open"
    live_head = str(state["head"]["sha"])
    assert live_head == str(_STATE["head_sha"]), f"head moved during close/reopen: {live_head}"
    payload = _pr_payload("reopened", live_head, str(_STATE["base_sha"]))
    status, body, guid = _post_delivery(payload)
    assert status == 202, f"reopened delivery: expected 202, got {status} body={body!r}"
    assert body == ""
    _STATE["reopened_guid"] = guid
    comments, waited = _wait_for("refreshed", REOPEN_BUDGET_SECONDS, "reopen refresh")
    assert len(comments) == 1, f"expected exactly 1 canonical comment, got {len(comments)}"
    assert str(comments[0].get("updated_at", "")) > before_updated_at, (
        f"comment not refreshed: {comments[0].get('updated_at')} <= {before_updated_at}"
    )
    print(
        f"\n(k) comment id={comments[0]['id']} "
        f"{before_updated_at} -> {comments[0].get('updated_at')} wait={waited:.1f}s"
    )


# --- (k2) label / issue events discarded ------------------------------------


def test_06_k2_non_pr_events_discarded() -> None:
    """QS-k2: signed `label` + `issues` events → 200 empty body, nothing enqueued."""
    _require_state("work_url", "webhook_secret")
    work_url = str(_STATE["work_url"])
    before = _queue_depths(work_url)["ApproximateNumberOfMessages"]
    for event_type in ("label", "issues"):
        payload = {"action": "created", "issue": {"number": PR_NUMBER}}
        status, body, _guid = _post_delivery(payload, event_type=event_type)
        assert status == 200, f"{event_type}: expected 200, got {status} body={body!r}"
        assert body == "", f"{event_type}: expected empty body, got {body!r}"
    after = _queue_depths(work_url)
    assert after["ApproximateNumberOfMessages"] == before, (
        f"work queue moved on discarded events: {before} -> {after}"
    )
    count = len(_canonical_comments())
    assert count == 1, f"expected still exactly 1 canonical comment, got {count}"

"""T050: US4 live acceptance harness (HLD §7.3 quickstart i; US4.AC2; SC-002).

Live-only test module: forces concurrent workers onto one PR and asserts
the exactly-one-canonical-comment constitution holds under contention:

  (i) a real push starts the webhook's review; while the winner holds the
      claim (state CLAIMED at the pushed head), TWO duplicate envelope
      messages are injected straight onto the work queue — concurrent
      deliveries for the same head racing one in-flight review — and the
      system must settle on exactly ONE canonical comment whose revision
      (`head_sha`) is the pushed one, ACTIVE, with `comment_id` tracking
      the surviving comment.

Injection shape: envelope JSON exactly as the ingress builds it (compact
sorted-key JSON of the v1 schema), `send_message`d directly — the
"duplicate messages" lever of QS-i. No webhook signing: the secret never
enters this module.

Revision truth (v1 false-pass lesson): `last_seen_sha` is an OBSERVATION
watermark that superseded deliveries also advance (`_observe_superseded`,
protocol.py) — it is NOT publish evidence. The comment's owning revision
is `head_sha`, written by establish-(c) and finalized at publish.
Convergence therefore asserts on `head_sha`, never `last_seen_sha`, and
the test SETTLES first: both injected GUIDs must have a terminal
worker-log line before any end-state assertion, so a late loser cannot
mutate the comment after the asserts pass.

Known edge (observed live, out of scope here): a delivery whose
establish-fence reads GitHub PR-meta before the ref update propagates is
superseded-observed, which advances `last_seen_sha` and routes later
same-SHA deliveries down the (b) equality path (full re-review of the
stored head, discarded at the post-review fence/claim). Spec-compliant
(HLD: fence mismatch -> discard); this harness avoids it by injecting
only after the webhook's establish-(c) is observable (status CLAIMED at
the pushed head) and fails loudly (convergence budget) if that window is
itself lost to fence lag.

Opt-in gate: the module SKIPS unless ACCEPTANCE_LIVE=1, so the default
`pytest -q` path stays green without AWS access. The T050 verify is::

    ACCEPTANCE_LIVE=1 ACCEPTANCE_PR=<fixture pr number> \\
    uv run --frozen pytest tests/integration/test_us4_acceptance.py \\
        -q --tb=short -m integration

Env config (defaults match the current live stack):

  ACCEPTANCE_PR            REQUIRED in live mode — the fixture PR number.
  ACCEPTANCE_REPO          default CarreroMarcos/pr-reviewer
  ACCEPTANCE_BRANCH        default scratch/fixture-us2
  ACCEPTANCE_FIXTURE_PATH  default docs/fixture-us2.md
  AWS_REGION               default us-west-2
  ACCEPTANCE_TABLE         default pr-reviewer-state
  ACCEPTANCE_WORK_QUEUE    default pr-reviewer-work
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest

from common.marker import build_marker

pytestmark = pytest.mark.integration

if os.environ.get("ACCEPTANCE_LIVE") != "1":
    pytest.skip(
        "live acceptance only: set ACCEPTANCE_LIVE=1 and ACCEPTANCE_PR=<fixture pr>",
        allow_module_level=True,
    )

REPO = os.environ.get("ACCEPTANCE_REPO", "CarreroMarcos/pr-reviewer")


def _env_pr_number() -> int:
    """Defensive ACCEPTANCE_PR parse: garbage -> 0 (the session gate fails
    loudly in live mode; offline runs just skip). Never raises at import."""
    try:
        return int(str(os.environ.get("ACCEPTANCE_PR", "0")).strip())
    except (TypeError, ValueError):
        return 0


PR_NUMBER = _env_pr_number()
BRANCH = os.environ.get("ACCEPTANCE_BRANCH", "scratch/fixture-us2")
PUSH_PATH = os.environ.get("ACCEPTANCE_FIXTURE_PATH", "docs/fixture-us2.md")
REGION = os.environ.get("AWS_REGION", "us-west-2")
TABLE_NAME = os.environ.get("ACCEPTANCE_TABLE", "pr-reviewer-state")
WORK_QUEUE = os.environ.get("ACCEPTANCE_WORK_QUEUE", "pr-reviewer-work")

CLAIM_BUDGET_SECONDS = 90.0  # push -> webhook establish-(c) visible
RACE_BUDGET_SECONDS = 300.0  # duplicates review + collapse; winner publishes
CONVERGE_BUDGET_SECONDS = 60.0  # end-state check after settle
POLL_INTERVAL_SECONDS = 1.0

# CloudWatch scan tuning: Lambda fans log lines out across streams under
# contention, so the window is deliberately deeper than the 3-stream/200-line
# shape that missed terminal lines live. LOG_CLOCK_SKEW_SECONDS margins
# `since` (driver wall clock) against CloudWatch ingestion timestamps.
SCAN_STREAM_LIMIT = 10
SCAN_EVENT_LIMIT = 300
LOG_CLOCK_SKEW_SECONDS = 120.0

MARKER = build_marker(REPO, PR_NUMBER)
_STATE: dict[str, Any] = {}


@pytest.fixture(scope="session", autouse=True)
def _live_gate() -> None:
    """Session-scoped live gate: module import never fails, so one bad env
    cannot poison the whole run — only tests needing the guard skip/fail."""
    if os.environ.get("ACCEPTANCE_LIVE") != "1":
        pytest.skip("live acceptance only: set ACCEPTANCE_LIVE=1 and ACCEPTANCE_PR=<fixture pr>")
    raw = os.environ.get("ACCEPTANCE_PR", "")
    if _env_pr_number() <= 0:
        pytest.fail(
            f"ACCEPTANCE_PR={raw!r} is not a usable fixture PR number: set it to "
            "the numeric id of a dedicated fixture PR (never a product PR — this "
            "harness pushes real commits).",
            pytrace=False,
        )


# --- helpers --------------------------------------------------------------


def _gh_api(*args: str) -> Any:
    """Run `gh api <args...>` and return the parsed JSON payload."""
    proc = subprocess.run(  # noqa: S603 (fixed argv, no shell)
        ["gh", "api", *args],  # noqa: S607 (gh CLI authenticated on this box)
        capture_output=True,
        text=True,
        timeout=60,
        shell=False,
    )
    assert proc.returncode == 0, f"gh api {' '.join(args[:2])}... failed: {proc.stderr[:300]}"
    return json.loads(proc.stdout)


def _push_file(note: str) -> str:
    """Rewrite the fixture file on the harness branch — a REAL commit/push."""
    body = (
        "fixture: US2/US4 acceptance scratch file (T042/T050)\n"
        "not product content — drives real synchronize events for acceptance\n"
        f"push: {note} at {datetime.now(UTC).isoformat()}\n"
    )
    if "fixture_file_sha" not in _STATE:
        meta = _gh_api(f"repos/{REPO}/contents/{PUSH_PATH}?ref={BRANCH}")
        _STATE["fixture_file_sha"] = str(meta["sha"])
    payload = _gh_api(
        f"repos/{REPO}/contents/{PUSH_PATH}",
        "-X",
        "PUT",
        "-f",
        f"message=fixture: {note}",
        "-f",
        f"branch={BRANCH}",
        "-f",
        f"content={base64.b64encode(body.encode()).decode()}",
        "-f",
        f"sha={_STATE['fixture_file_sha']}",
    )
    _STATE["fixture_file_sha"] = str(payload["content"]["sha"])
    return str(payload["commit"]["sha"])


def _canonical_comments() -> list[dict[str, Any]]:
    """All current comments on the PR bearing the canonical marker."""
    comments = _gh_api(f"repos/{REPO}/issues/{PR_NUMBER}/comments?per_page=100", "--paginate")
    assert isinstance(comments, list)
    return [c for c in comments if MARKER in str(c.get("body", ""))]


def _state_item() -> dict[str, Any] | None:
    """The PR's state item, or None while no review has landed yet."""
    if "dynamodb_table" not in _STATE:
        _STATE["dynamodb_table"] = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
    item = _STATE["dynamodb_table"].get_item(Key={"pk": f"review:{REPO}#{PR_NUMBER}"}).get("Item")
    return dict(item) if item else None


def _log_client() -> Any:
    if "logs" not in _STATE:
        _STATE["logs"] = boto3.client("logs", region_name=REGION)
    return _STATE["logs"]


def _inject_duplicate(head_sha: str, base_sha: str) -> list[str]:
    """Send two duplicate envelope messages onto the work queue.

    Body bytes mirror the ingress exactly (sorted-key compact JSON of the
    v1 envelope schema); GUIDs differ so each message is an independent
    delivery (claim_owner is the raw delivery GUID)."""
    guids = []
    sqs = boto3.client("sqs", region_name=REGION)
    queue_url = sqs.get_queue_url(QueueName=WORK_QUEUE)["QueueUrl"]
    for _ in range(2):
        guid = str(uuid.uuid4())
        envelope = {
            "envelope_version": "v1",
            "event_type": "pull_request",
            "action": "synchronize",
            "repo_full_name": REPO,
            "pr_number": PR_NUMBER,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "sender": "acceptance-harness",
            "delivery_guid": guid,
        }
        body = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
        sqs.send_message(QueueUrl=queue_url, MessageBody=body)
        guids.append(guid)
    return guids


def _claimed(sha: str) -> bool:
    """The webhook delivery holds the claim at `sha` (establish-(c) wrote
    status CLAIMED + head_sha; the observation-only superseded path never
    does). Injecting inside this window makes the duplicates race a live
    review instead of trailing it."""
    state = _state_item()
    return (
        state is not None and state.get("status") == "CLAIMED" and str(state.get("head_sha")) == sha
    )


def _guid_terminal(guid: str, since_epoch: float) -> bool:
    """True once the worker logs ANY terminal line (published or
    discarded_*) for this delivery GUID at/after since_epoch.

    Scans a deep stream window fresh on every call — Lambda spreads
    concurrent containers across streams (the T045 lesson: caching one
    stream name races the writer container). Identity is matched on PARSED
    fields (delivery_guid plus a published/discarded_* status), not
    substrings, so a line that merely mentions the GUID in another field
    cannot false-positive. The scanned stream/event count is left in
    _STATE["last_log_scan"] so a silent poll still shows whether anything
    was scanned at all."""
    client = _log_client()
    streams = client.describe_log_streams(
        logGroupName="/aws/lambda/pr-reviewer-worker",
        orderBy="LastEventTime",
        descending=True,
        limit=SCAN_STREAM_LIMIT,
    )["logStreams"]
    scanned_events = 0
    for stream in streams:
        for event in client.get_log_events(
            logGroupName="/aws/lambda/pr-reviewer-worker",
            logStreamName=stream["logStreamName"],
            startTime=int((since_epoch - LOG_CLOCK_SKEW_SECONDS) * 1000),
            limit=SCAN_EVENT_LIMIT,
        )["events"]:
            scanned_events += 1
            message = str(event.get("message", "")).strip()
            if not message.startswith("{"):
                continue
            try:
                parsed = json.loads(message)
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                continue
            if str(parsed.get("delivery_guid")) != guid:
                continue
            if str(parsed.get("status", "")).startswith(("published", "discarded_")):
                return True
    _STATE["last_log_scan"] = {"streams": len(streams), "events": scanned_events}
    return False


def _all_guids_settled(guids: list[str], since_epoch: float) -> bool:
    return all(_guid_terminal(guid, since_epoch) for guid in guids)


def _converged_after_race(sha: str) -> bool:
    """One canonical comment owned by revision `sha`: state ACTIVE with
    `head_sha` == sha (finalize-written truth — NOT last_seen_sha, which
    superseded observations also advance) and comment_id == the comment."""
    canonical = _canonical_comments()
    state = _state_item()
    if len(canonical) != 1 or state is None:
        return False
    if state.get("status") != "ACTIVE" or str(state.get("head_sha")) != sha:
        return False
    if str(state.get("comment_id")) != str(canonical[0]["id"]):
        return False
    _STATE["observed"] = {"comment": canonical[0], "state": state}
    return True


def _poll(ok: Callable[[], bool], budget: float, describe: Callable[[], str]) -> None:
    deadline = time.monotonic() + budget
    last = ""
    while time.monotonic() < deadline:
        if ok():
            return
        last = describe()
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"convergence budget ({budget}s) exhausted; last state: {last}")


# --- (i) forced concurrent workers converge to one correct comment ---------


def test_i_forced_concurrency_converges_to_single_correct_comment():
    """(i): in-flight review + two duplicate injections -> one comment,
    correct revision, no duplicate comment, both racers settled."""
    live = _gh_api(f"repos/{REPO}/pulls/{PR_NUMBER}")
    base_sha = str(live["base"]["sha"])
    before = _canonical_comments()
    pre_id = int(before[0]["id"]) if len(before) == 1 else 0
    pre_updated = str(before[0]["updated_at"]) if len(before) == 1 else ""

    # Real push: the webhook's review starts in flight.
    sha1 = _push_file("us4-race")
    _poll(
        lambda: _claimed(sha1),
        CLAIM_BUDGET_SECONDS,
        lambda: f"state={_state_item()}",
    )

    # Force concurrency: two duplicate deliveries race the in-flight review.
    since = time.time()
    _STATE["injected_guids"] = _inject_duplicate(sha1, base_sha)

    # Settle: every injected racer reaches a terminal outcome BEFORE any
    # end-state assertion, so a late loser cannot invalidate the result.
    def _describe_settle() -> str:
        return (
            f"settled={[g[:8] for g in _STATE['injected_guids']]} "
            f"state={_state_item()} scan={_STATE.get('last_log_scan')}"
        )

    _poll(
        lambda: _all_guids_settled(_STATE["injected_guids"], since),
        RACE_BUDGET_SECONDS,
        _describe_settle,
    )
    _poll(
        lambda: _converged_after_race(sha1),
        CONVERGE_BUDGET_SECONDS,
        lambda: f"canonical={[c['id'] for c in _canonical_comments()]} state={_state_item()}",
    )

    observed = _STATE["observed"]
    assert len(_canonical_comments()) == 1, "constitution: exactly one canonical comment"
    assert str(observed["state"]["head_sha"]) == sha1, (
        "the surviving comment must be owned by the pushed revision "
        "(head_sha is finalize-written truth)"
    )
    assert observed["state"]["status"] == "ACTIVE"
    assert str(observed["state"]["comment_id"]) == str(observed["comment"]["id"]), (
        "state must track the surviving canonical comment"
    )
    if pre_updated:
        # Id-guard: the updated_at comparison is only meaningful on the same
        # comment lineage — a changed id must fail HERE with both ids named,
        # not false-pass via a trivially different timestamp.
        assert int(observed["comment"]["id"]) == pre_id, (
            f"race must update in place (was comment {pre_id}, now {observed['comment']['id']})"
        )
        assert str(observed["comment"]["updated_at"]) != pre_updated, (
            "the canonical comment must have been regenerated for the race head"
        )

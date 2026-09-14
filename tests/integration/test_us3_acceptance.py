"""T045: US3 live acceptance harness (HLD §7.3 quickstart g; US3.AC1; SC-004).

Live-only test module: redrives an artificially stale head SHA through the
real admission path (signed synthetic delivery POSTed to the deployed
ingress Function URL — the exact shape of an old SQS message redelivered)
and asserts the revision-fence contract:

  (g) the worker's live-head fence (HLD §2.3 item 4: fetch current head
      immediately before publish, after claim; mismatch → discard) leaves
      the canonical comment UNMUTATED — same comment id, same updated_at,
      still exactly one marker-bearing comment.

The stale SHA used is the fixture PR's real pre-baseline head, captured
before the harness pushes its baseline commit, so the delivery is a true
previously-reviewed revision, not a malformed guess. (Gate-9 correction:
the superseded path advances ONLY last_seen_sha via the observation
write — generation never moves on any discard path — so neither state
field is asserted.)

Observable split (deliberate):
  - AC assertion: canonical comment immutability (id + updated_at + count).
  - Mechanism evidence: the worker's structured `discarded_superseded` log
    line carrying the stale SHA, timestamped after the redrive POST. State
    `last_seen_sha`/`generation` are NOT asserted: the superseded path
    records the incoming SHA via the observation write (generation never
    moves on discard paths; establish-(c) is the sole bumper and requires
    incoming == live head), so `last_seen_sha` legitimately changes on a
    discarded delivery — asserting it would over-pin beyond the AC.

Opt-in gate: the module SKIPS unless ACCEPTANCE_LIVE=1, so the default
`pytest -q` path stays green without AWS/gh access. The T045 verify is::

    ACCEPTANCE_LIVE=1 ACCEPTANCE_PR=<fixture pr number> \\
    uv run --frozen pytest tests/integration/test_us3_acceptance.py \\
        -q --tb=short -m integration

Env config (defaults match the current live stack):

  ACCEPTANCE_PR            REQUIRED in live mode — the fixture PR number.
  ACCEPTANCE_REPO          default CarreroMarcos/pr-reviewer
  ACCEPTANCE_FUNCTION_URL  REQUIRED in live mode — no default (a stale
                           default would drive deliveries at the wrong stack)
  ACCEPTANCE_BRANCH        default scratch/fixture-us2
  ACCEPTANCE_FIXTURE_PATH  default docs/fixture-us2.md
  ACCEPTANCE_FIXTURE_BASE  default docs/fixture-us3.md (separate seed file so
                           this harness's baseline push does not collide with
                           another harness's tracked blob sha)
  AWS_REGION               default us-west-2
  ACCEPTANCE_SSM_SECRET    default /pr-reviewer/webhook-secret

Secrets: the webhook secret is read from SSM at runtime to sign the redrive
delivery and is NEVER printed, logged, or embedded in assertion messages.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
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
# No silent default: live mode requires ACCEPTANCE_FUNCTION_URL explicitly
# (the session gate fails when it is absent).
FUNCTION_URL = os.environ.get("ACCEPTANCE_FUNCTION_URL", "")
BRANCH = os.environ.get("ACCEPTANCE_BRANCH", "scratch/fixture-us2")
PUSH_PATH = os.environ.get("ACCEPTANCE_FIXTURE_PATH", "docs/fixture-us2.md")
SEED_PATH = os.environ.get("ACCEPTANCE_FIXTURE_BASE", "docs/fixture-us3-baseline.md")
REGION = os.environ.get("AWS_REGION", "us-west-2")
SECRET_NAME = os.environ.get("ACCEPTANCE_SSM_SECRET", "/pr-reviewer/webhook-secret")

BASELINE_BUDGET_SECONDS = 90.0  # one full review cycle for the baseline push
STALE_BUDGET_SECONDS = 90.0  # claim + fence + discard + log availability
POLL_INTERVAL_SECONDS = 2.0

# CloudWatch scan tuning: Lambda fans log lines out across streams under
# contention, so the window is deliberately deeper than the 3-stream/200-line
# shape that missed discards live. LOG_CLOCK_SKEW_SECONDS margins `since`
# (driver wall clock) against CloudWatch ingestion timestamps.
SCAN_STREAM_LIMIT = 10
SCAN_EVENT_LIMIT = 300
LOG_CLOCK_SKEW_SECONDS = 120.0

MARKER = build_marker(REPO, PR_NUMBER)
_STATE: dict[str, Any] = {}
_SECRET_CACHE: dict[str, str] = {}


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
    if not FUNCTION_URL:
        pytest.fail(
            "ACCEPTANCE_FUNCTION_URL must be set explicitly in live mode "
            "(no default — a stale default would drive deliveries at the wrong stack).",
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


def _webhook_secret() -> str:
    """Fetch the signing secret from SSM (cached; never logged)."""
    if "value" not in _SECRET_CACHE:
        ssm = boto3.client("ssm", region_name=REGION)
        param = ssm.get_parameter(Name=SECRET_NAME, WithDecryption=True)
        _SECRET_CACHE["value"] = str(param["Parameter"]["Value"])
    return _SECRET_CACHE["value"]


def _sign(raw: bytes) -> str:
    secret = _STATE.get("webhook_secret")
    assert isinstance(secret, str)
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _pr_payload(action: str, head_sha: str, base_sha: str) -> dict[str, object]:
    """Minimal well-formed pull_request payload for the fixture PR."""
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


def _post_delivery(payload: dict[str, object]) -> tuple[int, str, str]:
    """POST one signed delivery with a fresh GUID. Returns (status, body, guid)."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    guid = str(uuid.uuid4())
    request = urllib.request.Request(  # noqa: S310 (https-only Function URL)
        FUNCTION_URL,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _sign(raw),
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": guid,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return response.status, response.read().decode(), guid
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(), guid


def _seed_file_if_missing() -> None:
    """Create the US3 seed file on the fixture branch if not present.

    Only a 404 ("not present") falls through to creation — a 403 or 5xx
    re-raises instead of being mistaken for "missing" (which would then
    fail confusingly on the create PUT).
    """
    try:
        _gh_api(f"repos/{REPO}/contents/{SEED_PATH}?ref={BRANCH}")
        return
    except AssertionError as exc:
        if "404" not in str(exc):
            raise
        body = (
            "fixture: US3 stale-redrive baseline seed (T045)\n"
            f"seeded at {datetime.now(UTC).isoformat()}\n"
        )
        _gh_api(
            f"repos/{REPO}/contents/{SEED_PATH}",
            "-X",
            "PUT",
            "-f",
            "message=fixture: US3 baseline seed (T045)",
            "-f",
            f"branch={BRANCH}",
            "-f",
            f"content={base64.b64encode(body.encode()).decode()}",
        )
        return


def _delete_seed_file() -> None:
    """Teardown for `_seed_file_if_missing`: remove the US3 seed fixture so
    later phases see a clean surface (no accumulated fixture files).

    Best-effort with a loud warning — teardown must never mask an AC result.
    A no-seed state (already clean) is a silent no-op, not a failure."""
    try:
        meta = _gh_api(f"repos/{REPO}/contents/{SEED_PATH}?ref={BRANCH}")
    except AssertionError as exc:
        if "404" in str(exc):
            return  # already absent — clean surface, nothing to do
        print(f"\n[teardown] seed-file lookup failed (non-fatal): {exc}")
        return
    try:
        _gh_api(
            f"repos/{REPO}/contents/{SEED_PATH}",
            "-X",
            "DELETE",
            "-f",
            "message=fixture: US3 seed teardown (T045)",
            "-f",
            f"branch={BRANCH}",
            "-f",
            f"sha={meta['sha']}",
        )
    except (AssertionError, KeyError) as exc:
        print(f"\n[teardown] seed-file delete failed (non-fatal): {exc}")


def _push_file(note: str) -> str:
    """Rewrite the fixture file on the harness branch — a REAL commit/push."""
    body = (
        "fixture: US2 acceptance scratch file (T042/T045)\n"
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
        _STATE["dynamodb_table"] = boto3.resource("dynamodb", region_name=REGION).Table(
            os.environ.get("ACCEPTANCE_TABLE", "pr-reviewer-state")
        )
    item = _STATE["dynamodb_table"].get_item(Key={"pk": f"review:{REPO}#{PR_NUMBER}"}).get("Item")
    return dict(item) if item else None


def _converged_active(sha: str) -> bool:
    """Exactly one canonical comment AND state ACTIVE at `sha` — the system's
    own commit point (comment_id is ACTIVE-only per common/state.py). A weak
    count-only predicate races the in-flight review: the count passes while
    the PATCH for `sha` has not landed, so any later updated_at snapshot is
    mid-cycle (observed live on T045 before this gate)."""
    canonical = _canonical_comments()
    state = _state_item()
    return (
        len(canonical) == 1
        and state is not None
        and state.get("status") == "ACTIVE"
        and str(state.get("last_seen_sha")) == sha
    )


def _poll(ok: Callable[[], bool], budget: float, describe: Callable[[], str]) -> None:
    deadline = time.monotonic() + budget
    last = ""
    while time.monotonic() < deadline:
        if ok():
            return
        last = describe()
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"convergence budget ({budget}s) exhausted; last state: {last}")


def _log_client() -> Any:
    if "logs" not in _STATE:
        _STATE["logs"] = boto3.client("logs", region_name=REGION)
    return _STATE["logs"]


def _superseded_line_seen(stale_sha: str, since_epoch: float) -> bool:
    """True once the worker logs a discarded_superseded line for stale_sha
    at/after since_epoch (CloudWatch near-real-time availability).

    Scans a deep stream window fresh on every call — Lambda spreads
    concurrent containers across streams, so caching one stream name races
    the writer container (observed live: the discard line landed in a
    different stream than the baseline review's). Identity is matched on
    PARSED fields (status == discarded_superseded, head_sha == stale_sha),
    not substrings, so semantically adjacent lines (e.g. a retry_queued
    line naming the same SHA) cannot false-positive. The scanned
    stream/event count is left in _STATE["last_log_scan"] so a silent poll
    still shows whether anything was scanned at all."""
    client = _log_client()
    streams = client.describe_log_streams(
        logGroupName="/aws/lambda/pr-reviewer-worker",
        orderBy="LastEventTime",
        descending=True,
        limit=SCAN_STREAM_LIMIT,
    )["logStreams"]
    scanned_events = 0
    for stream in streams:
        events = client.get_log_events(
            logGroupName="/aws/lambda/pr-reviewer-worker",
            logStreamName=stream["logStreamName"],
            startTime=int((since_epoch - LOG_CLOCK_SKEW_SECONDS) * 1000),
            limit=SCAN_EVENT_LIMIT,
        )["events"]
        scanned_events += len(events)
        for event in events:
            message = str(event.get("message", "")).strip()
            if not message.startswith("{"):
                continue
            try:
                parsed = json.loads(message)
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                continue
            if str(parsed.get("status")) != "discarded_superseded":
                continue
            if str(parsed.get("head_sha")) != stale_sha:
                continue
            return True
    _STATE["last_log_scan"] = {"streams": len(streams), "events": scanned_events}
    return False


def _describe_superseded_wait() -> str:
    scan = _STATE.get("last_log_scan")
    return f"waiting for discarded_superseded log line; last scan: {scan}"


# --- (g) stale head SHA redrive must not mutate the canonical comment ------


def test_g_stale_redrive_does_not_mutate_canonical_comment():
    """(g): signed stale-SHA delivery → fence discards; comment untouched."""
    _STATE["webhook_secret"] = _webhook_secret()

    # Seed + baseline: converge one canonical comment at the current head.
    _seed_file_if_missing()
    try:
        live = _gh_api(f"repos/{REPO}/pulls/{PR_NUMBER}")
        stale_sha = str(live["head"]["sha"])  # current head BEFORE the baseline push
        sha1 = _push_file("us3-baseline")
        assert sha1 != stale_sha, "baseline push must move the head for a true stale case"
        _poll(
            lambda: _converged_active(sha1),
            BASELINE_BUDGET_SECONDS,
            lambda: f"canonical={[c['id'] for c in _canonical_comments()]} state={_state_item()}",
        )
        before = _canonical_comments()
        assert len(before) == 1
        pre_id = int(before[0]["id"])
        pre_updated = str(before[0]["updated_at"])

        # Redrive the stale revision through the real admission path.
        base_sha = str(live["base"]["sha"])
        since = time.time()
        status, body, guid = _post_delivery(_pr_payload("synchronize", stale_sha, base_sha))
        assert status == 202, f"stale delivery must be admitted (202), got {status}: {body[:200]}"
        _STATE["redrive_guid"] = guid

        # Mechanism evidence: the fence discarded it, naming the stale SHA.
        _poll(
            lambda: _superseded_line_seen(stale_sha, since),
            STALE_BUDGET_SECONDS,
            _describe_superseded_wait,
        )

        # The AC: the canonical comment was NOT mutated. Id-guard first: an
        # id mismatch fails HERE with both ids named — comparing updated_at
        # on the wrong comment would false-pass or false-fail.
        after = _canonical_comments()
        assert len(after) == 1, "stale redrive must not add comments"
        assert int(after[0]["id"]) == pre_id, (
            f"canonical comment id must be unchanged (was {pre_id}, now {after[0]['id']})"
        )
        assert str(after[0]["updated_at"]) == pre_updated, (
            "canonical comment updated_at must be unchanged — no regeneration happened"
        )
    finally:
        # Fixture teardown: remove the seed file so later phases see a clean
        # surface. Best-effort with a loud warning — teardown must not mask
        # the AC result above.
        _delete_seed_file()

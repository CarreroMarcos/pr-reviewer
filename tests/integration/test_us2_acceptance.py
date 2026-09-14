"""T042: US2 live acceptance harness (HLD §7.3 quickstart c/d/h; US2.AC1–AC3).

Live-only test module: drives REAL pushes to a fixture PR on the target repo
and asserts canonical-comment convergence over the deployed stack:

  (c) a second push UPDATES the same canonical comment — same GitHub comment
      id, `updated_at` moves, state `generation` increments
  (d) two rapid pushes leave exactly ONE canonical comment reflecting the
      latest head — state `last_seen_sha` equals the final push SHA and the
      comment count stays 1 (no duplicate)
  (h) deleting the canonical comment + one new push converges to exactly ONE
      new marker-bearing comment (fresh comment id, marker present)

Driver is REAL pushes only (Contents API PUT per commit → real GitHub
synchronize webhook → ingress → worker). Synthetic signed deliveries are
deliberately NOT used: the worker's live head-fetch would classify a
synthetic SHA stale (HLD §5, the (g) path) instead of exercising the
second-push regeneration this ticket owns.

Observability: comment identity comes from the GitHub issues-comments API
(filtered on the `common.marker` canonical marker); which head a review
reflects comes from the state table (`review:{repo}#{pr}` → `last_seen_sha`,
`generation`) — the model echo of the SHA is not deterministic and is never
asserted.

Opt-in gate: the module SKIPS unless ACCEPTANCE_LIVE=1, so the default
`pytest -q` path stays green without AWS/gh access. The T042 verify is::

    ACCEPTANCE_LIVE=1 ACCEPTANCE_PR=<fixture pr number> \\
    uv run --frozen pytest tests/integration/test_us2_acceptance.py \\
        -q --tb=short -m integration

Env config:

  ACCEPTANCE_PR            REQUIRED in live mode — the fixture PR number.
                           Never point this at a product PR: the harness
                           pushes real commits and deletes the bot comment.
  ACCEPTANCE_REPO          default CarreroMarcos/pr-reviewer
  ACCEPTANCE_BRANCH        default scratch/fixture-us2 (branch the harness pushes to)
  ACCEPTANCE_FIXTURE_PATH  default docs/fixture-us2.md (file rewritten per push)
  AWS_REGION               default us-west-2
  ACCEPTANCE_TABLE         default pr-reviewer-state

Order matters (pytest runs definition order): (c) seeds the baseline review,
(d) races two pushes on top, (h) deletes and converges last. Shared state
travels in the module-level `_STATE` dict — polls, never fixed sleeps.

Secrets: this module signs nothing and reads no secrets — GitHub itself is
the delivery path; the webhook secret never enters the harness.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

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
FIXTURE_PATH = os.environ.get("ACCEPTANCE_FIXTURE_PATH", "docs/fixture-us2.md")
REGION = os.environ.get("AWS_REGION", "us-west-2")
TABLE_NAME = os.environ.get("ACCEPTANCE_TABLE", "pr-reviewer-state")

BASELINE_BUDGET_SECONDS = 90.0  # one full review cycle, generous first cold start
CONVERGE_BUDGET_SECONDS = 90.0  # regeneration after push/deletion
POLL_INTERVAL_SECONDS = 2.0

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
            "harness pushes real commits and deletes the bot comment).",
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
    """Rewrite the fixture file on the harness branch — a REAL commit/push.

    Returns the new head commit SHA (the SHA GitHub's synchronize event and
    the worker's live head fetch will both see).
    """
    body = (
        "fixture: US2 acceptance scratch file (T042)\n"
        "not product content — drives real synchronize events for acceptance (c)(d)(h)\n"
        f"push: {note} at {datetime.now(UTC).isoformat()}\n"
    )
    if "fixture_file_sha" not in _STATE:
        meta = _gh_api(f"repos/{REPO}/contents/{FIXTURE_PATH}?ref={BRANCH}")
        _STATE["fixture_file_sha"] = str(meta["sha"])
    payload = _gh_api(
        f"repos/{REPO}/contents/{FIXTURE_PATH}",
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
    try:
        item = (
            _STATE["dynamodb_table"].get_item(Key={"pk": f"review:{REPO}#{PR_NUMBER}"}).get("Item")
        )
    except ClientError as exc:  # pragma: no cover - table exists in live mode
        raise AssertionError(f"state table read failed: {exc}") from exc
    return dict(item) if item else None


def _poll(describe: Callable[[], str], ok: Callable[[], bool], budget: float) -> None:
    """Poll every POLL_INTERVAL_SECONDS until ok() or budget is exhausted."""
    deadline = time.monotonic() + budget
    last = ""
    while time.monotonic() < deadline:
        last = describe()
        if ok():
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"convergence budget ({budget}s) exhausted; last state: {last}")


def _snapshot() -> str:
    """One-line view of comments + state for poll diagnostics (no secrets)."""
    canonical = _canonical_comments()
    state = _state_item()
    ids = [c["id"] for c in canonical]
    tail = (
        f"gen={state.get('generation')} sha={str(state.get('last_seen_sha'))[:8]} "
        f"status={state.get('status')}"
        if state
        else "absent"
    )
    return f"comment_ids={ids} state={tail}"


def _converged_to(sha: str, comment_ids: set[int] | None = None) -> bool:
    canonical = _canonical_comments()
    state = _state_item()
    if len(canonical) != 1 or state is None:
        return False
    # ACTIVE is the system's own commit point: the review for `sha` is fully
    # published and the state item carries the adopted `comment_id`
    # (comment_id is ACTIVE-only per common/state.py). Polling for anything
    # earlier races the CLAIMED→ACTIVE write.
    if state.get("status") != "ACTIVE":
        return False
    if str(state.get("last_seen_sha")) != sha:
        return False
    if comment_ids is not None and {int(canonical[0]["id"])} != comment_ids:
        return False
    _STATE["observed"] = {"comment": canonical[0], "state": state}
    return True


# --- (c) second push updates the same comment ------------------------------


def test_c_second_push_updates_same_comment():
    """(c): push → one canonical comment; second push → SAME id refreshed."""
    # Baseline: one push from the seeded PR, converge to exactly one comment.
    sha1 = _push_file("baseline")
    _poll(_snapshot, lambda: _converged_to(sha1), BASELINE_BUDGET_SECONDS)
    first = _STATE["observed"]
    id1 = int(first["comment"]["id"])
    gen1 = int(first["state"]["generation"])
    updated1 = str(first["comment"].get("updated_at", ""))
    _STATE["baseline_generation"] = gen1

    # Second push: the SAME comment must be updated in place.
    sha2 = _push_file("second")
    _poll(
        _snapshot,
        lambda: _converged_to(sha2, comment_ids={id1}),
        CONVERGE_BUDGET_SECONDS,
    )
    second = _STATE["observed"]
    # Id-guard first: an id mismatch fails HERE with both ids named — any
    # timestamp comparison on the wrong comment would false-pass/fail.
    assert int(second["comment"]["id"]) == id1, (
        f"second push must update the same comment (was {id1}, now {second['comment']['id']})"
    )
    assert int(second["state"]["generation"]) > gen1, (
        "generation must increment on live-head confirm"
    )
    updated2 = str(second["comment"].get("updated_at", ""))
    assert updated2, "updated comment must carry updated_at"
    assert updated2 >= updated1, (
        f"updated_at must not move backwards on the same comment ({updated1} -> {updated2})"
    )


# --- (d) two rapid pushes leave only the latest head -----------------------


def test_d_two_rapid_pushes_leave_only_latest_sha():
    """(d): two back-to-back pushes converge to ONE comment at the last SHA."""
    sha3 = _push_file("rapid-1")
    sha4 = _push_file("rapid-2")  # no wait: the second delivery races the first
    assert sha3 != sha4
    _poll(_snapshot, lambda: _converged_to(sha4), CONVERGE_BUDGET_SECONDS)
    observed = _STATE["observed"]
    # Exactly one canonical comment (no duplicate from the racing deliveries)
    # whose review reflects the LATEST head (state last_seen_sha == sha4).
    assert len(_canonical_comments()) == 1
    assert str(observed["state"]["last_seen_sha"]) == sha4
    assert int(observed["state"]["generation"]) > _STATE["baseline_generation"]


# --- (h) deleted comment + push converges to one new marker comment --------


def test_h_deleted_comment_plus_push_converges_to_new_marker_comment():
    """(h): delete the canonical comment; the next push converges to one NEW one."""
    canonical = _canonical_comments()
    assert len(canonical) == 1, "precondition: exactly one canonical comment before deletion"
    deleted_id = int(canonical[0]["id"])
    proc = subprocess.run(  # noqa: S603 (fixed argv, no shell)
        ["gh", "api", "-X", "DELETE", f"repos/{REPO}/issues/comments/{deleted_id}"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        shell=False,
    )
    assert proc.returncode == 0, f"comment deletion failed: {proc.stderr[:300]}"
    assert _canonical_comments() == [], "deleted comment must be gone before the push"

    sha5 = _push_file("after-delete")
    _poll(_snapshot, lambda: _converged_to(sha5), CONVERGE_BUDGET_SECONDS)
    observed = _STATE["observed"]
    new_comment = observed["comment"]
    assert int(new_comment["id"]) != deleted_id, "converged comment must be newly created"
    assert MARKER in str(new_comment["body"]), "new comment must be marker-bearing"
    assert str(observed["state"]["comment_id"]) == str(new_comment["id"]), (
        "state must track the adopted comment id"
    )
    assert str(observed["state"]["last_seen_sha"]) == sha5

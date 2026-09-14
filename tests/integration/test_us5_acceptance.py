"""T057: US5 live acceptance harness (quickstart j, l, l2, m, n; SC-006; D2/D3).

Phased live drill over the deployed stack's failure-notice machinery.
Phases run IN ORDER (each idempotent/resumable via a state file):

  ACCEPTANCE_US5_PHASE=notice_permanent
      (l) permanent-class failure -> IMMEDIATE notice: flip
      /pr-reviewer/glm-api-key to garbage; an injected delivery takes the
      LLM 401-after-single-refetch row (LLM_401 trigger publishes at any
      receive count, non-retryable completion); assert the notice lands
      within seconds-to-minutes (vs the 48-min transient bound), the
      comment carries the fixed template, and the message is NOT DLQ'd.
      Key restored + verified in-phase. ~4 min.

  ACCEPTANCE_US5_PHASE=drill
      (l2)+(m)+(n) transient exhaustion: flip /pr-reviewer/glm-endpoint
      to the SAME HOST on a dead port (GLM_ALLOWED_HOSTS pins api.z.ai;
      connect timeout -> LlmError timeout -> TRANSIENT -> queue-retried).
      Timeline (visibility 720s is the clock, event-driven gates — no
      blind sleeps longer than poll intervals):
        t0        flip endpoint; inject M1 at the live head S0
        ~t0+48m   M1 receive #5 (final attempt): fence live==S0 -> notice
                  PUBLISHES [l2]; message moves to the DLQ
        after C   push S1 (webhook delivery M2 fails + retries)
        M2 recv#4 push S2 (webhook delivery M3 fails, recovers post-revert)
        M2 recv#5 superseded AT ESTABLISH (M3 rewrote last_seen ~12 min
                   earlier) — stale-delivery rejection [m]
        M3 recv#4 adaptive timed push S3 (SPR-65 establish-observed gate:
                    residual measured from observed retry intervals; the old
                    r4+717s arithmetic survives only as the logged comparison delta)
                   inside M3's #5 establish->notice-fence window (driver-executed
                   so it survives the ~15-min AWS credential windows)
        M3 recv#5 notice fence sees live==S3 -> skipped-stale [m]; M3 -> DLQ
        then      restore endpoint; a post-refetch receive succeeds ->
                  review for S3 publishes, comment reverts from notice
                  content to review content [n]
      ~2h15m of wall clock; every stage writes the state file so a
      re-run resumes instead of restarting.

  ACCEPTANCE_US5_PHASE=asserts
      durable record check of (l2)/(m)/(n)/(l) from the state file +
      CloudWatch + GitHub (fast).

  ACCEPTANCE_US5_PHASE=redrive
      (j) the runbook drill: DLQ depth >= 1; StartMessageMoveTask back
      to the work queue; DLQ drains to zero; canonical comments converge
      (exactly one, unchanged identity — the stale redrives M1(S0)/M3(S2)
      must be fence-discarded, never mutate the comment); ~10 min.

Safety notes (read before running):
- Both SSM params are restored + verified after their flip. The api-key
  original is backed up to a test-scoped SSM parameter (never the state
  file — the on-disk JSON must stay greppable-clean of key material and
  the driver asserts that on every save); the endpoint is not a secret
  but its original is kept in the state file and restored verbatim.
  Every temporary SSM mutation restores in a `finally` so an early
  assert cannot leave a flipped value behind.
- The DLQ-depth alarm WILL page during the drill — real DLQ messages are
  the acceptance criterion working (runbook step 5 logs the drill).
- Real pushes during the flip window deliberately fail and recover on a
  post-revert receive; no product PRs are touched; quiet-window assumed.
- Injected envelopes are schema-valid v1 (same shape as the ingress), so
  DLQ poisoning is structurally impossible.

Opt-in gate: every test SKIPS unless ACCEPTANCE_LIVE=1 AND the phase env
matches, so the default `pytest -q` path stays green. The T057 verify is
the four phases run in order against the live stack (commands in the PR
evidence).

Env config (defaults match the current live stack):

  ACCEPTANCE_PR            REQUIRED in live mode — the fixture PR number.
  ACCEPTANCE_REPO          default CarreroMarcos/pr-reviewer
  ACCEPTANCE_BRANCH        default scratch/fixture-us2
  ACCEPTANCE_FIXTURE_PATH  default docs/fixture-us2.md
  AWS_REGION               default us-west-2
  ACCEPTANCE_TABLE         default pr-reviewer-state
  ACCEPTANCE_WORK_QUEUE    default pr-reviewer-work
  ACCEPTANCE_DLQ_QUEUE     default pr-reviewer-dlq
  ACCEPTANCE_SSM_ENDPOINT  default /pr-reviewer/glm-endpoint
  ACCEPTANCE_SSM_KEY       default /pr-reviewer/glm-api-key
  ACCEPTANCE_US5_STATE     default /tmp/opencode/us5-drill-state.json
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
import botocore.exceptions
import pytest

from common.failure_notice import NOTICE_TEMPLATE
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
DLQ_QUEUE = os.environ.get("ACCEPTANCE_DLQ_QUEUE", "pr-reviewer-dlq")
SSM_ENDPOINT = os.environ.get("ACCEPTANCE_SSM_ENDPOINT", "/pr-reviewer/glm-endpoint")
SSM_KEY = os.environ.get("ACCEPTANCE_SSM_KEY", "/pr-reviewer/glm-api-key")
STATE_PATH = os.environ.get(
    "ACCEPTANCE_US5_STATE",
    "/tmp/opencode/us5-drill-state.json",  # noqa: S108 — scratch box, 0600 file
)
PHASE = os.environ.get("ACCEPTANCE_US5_PHASE", "")

MARKER = build_marker(REPO, PR_NUMBER)
MINUTE = 60.0
POLL_INTERVAL_SECONDS = 5.0
# CloudWatch clock-skew margin: `since` is read from the driver wall clock
# while log timestamps come from CloudWatch ingestion — without a margin a
# line written "before" a POST (by skew) is missed forever.
LOG_CLOCK_SKEW_SECONDS = 120.0
_STATE: dict[str, Any] = {}


@pytest.fixture(scope="session", autouse=True)
def _live_gate() -> None:
    """Session-scoped live gate: module import never fails, so one bad env
    cannot poison the whole run — only tests needing the guard skip/fail."""
    if os.environ.get("ACCEPTANCE_LIVE") != "1":
        pytest.skip("live acceptance only: set ACCEPTANCE_LIVE=1 and ACCEPTANCE_US5_PHASE=...")
    raw = os.environ.get("ACCEPTANCE_PR", "")
    if _env_pr_number() <= 0:
        pytest.fail(
            f"ACCEPTANCE_PR={raw!r} is not a usable fixture PR number: set it to "
            "the numeric id of a dedicated fixture PR (never a product PR — this "
            "harness pushes real commits and flips SSM config).",
            pytrace=False,
        )
    if PHASE not in {"notice_permanent", "drill", "asserts", "redrive"}:
        pytest.skip("set ACCEPTANCE_US5_PHASE to one of notice_permanent|drill|asserts|redrive")


# --- state file (resumable drill; 0600) ------------------------------------
#
# Secret discipline: key-shaped material (api-key backups) lives ONLY in
# a test-scoped SSM parameter, never in this JSON file. _save_state
# asserts that invariant on every write and scrubs legacy disk keys.

# State-dict keys that must never reach the on-disk file (case-insensitive
# substring match). "endpoint_backup" is deliberately NOT in this set —
# the endpoint is not a secret.
_FORBIDDEN_STATE_KEYS = ("key_backup", "secret", "api_key", "apikey", "private_key")


def _key_backup_param() -> str:
    """Test-scoped SSM parameter holding the api-key original during (l)."""
    return f"{SSM_KEY}-backup-us5-{PR_NUMBER}"


def _scrub_secret_keys(mapping: dict[str, Any]) -> None:
    """Drop key-shaped entries in place (legacy disk files may hold them)."""

    def _scrub(node: Any) -> None:
        if isinstance(node, dict):
            for key in [k for k in node if any(f in str(k).lower() for f in _FORBIDDEN_STATE_KEYS)]:
                del node[key]
            for value in node.values():
                _scrub(value)
        elif isinstance(node, list):
            for value in node:
                _scrub(value)

    _scrub(mapping)


def _assert_no_secret_keys(mapping: dict[str, Any]) -> None:
    """Prove the to-be-written state holds no key-shaped material."""

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                assert not any(f in lowered for f in _FORBIDDEN_STATE_KEYS), (
                    f"secret-shaped key {path}.{key} must live in SSM, never the state file"
                )
                _walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                _walk(value, f"{path}[{index}]")

    _walk(mapping, "state")


def _load_state() -> dict[str, Any]:
    if "state" not in _STATE:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH) as handle:
                _STATE["state"] = json.load(handle)
        else:
            _STATE["state"] = {}
    return _STATE["state"]


def _save_state() -> None:
    # Merge-save: the driver's push watcher writes sha3/t_s3 into this
    # file while a harness process is live; a blind whole-file write
    # clobbers them (observed live 2026-09-14). Disk keys the loaded
    # snapshot never had survive; harness keys win on conflict.
    #
    # The secret assert runs on IN-MEMORY state BEFORE merge/scrub: a
    # secret-shaped key must fail LOUD here, never be silently scrubbed.
    _assert_no_secret_keys(_STATE["state"])
    disk: dict[str, Any] = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as handle:
                disk = json.load(handle)
        except (OSError, ValueError):
            disk = {}
    merged = {**disk, **_STATE["state"]}
    for key, value in _STATE["state"].items():
        if isinstance(value, dict) and isinstance(disk.get(key), dict):
            merged[key] = {**disk[key], **value}
    _scrub_secret_keys(merged)
    _assert_no_secret_keys(merged)
    tmp_path = f"{STATE_PATH}.tmp-{os.getpid()}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(merged, handle, indent=1, sort_keys=True)
    os.replace(tmp_path, STATE_PATH)


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
        "fixture: US acceptance scratch file (T057)\n"
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


def _queue_url(name: str) -> str:
    return boto3.client("sqs", region_name=REGION).get_queue_url(QueueName=name)["QueueUrl"]


def _queue_depth(name: str) -> int:
    """Total messages (visible + in-flight + delayed) on a queue."""
    attrs = boto3.client("sqs", region_name=REGION).get_queue_attributes(
        QueueUrl=_queue_url(name),
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        ],
    )["Attributes"]
    return sum(
        int(attrs.get(key, "0"))
        for key in (
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "ApproximateNumberOfMessagesDelayed",
        )
    )


def _inject(head_sha: str, base_sha: str) -> str:
    """Send ONE schema-valid v1 envelope directly onto the work queue."""
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
    boto3.client("sqs", region_name=REGION).send_message(
        QueueUrl=_queue_url(WORK_QUEUE), MessageBody=body
    )
    return guid


def _ssm() -> Any:
    if "ssm" not in _STATE:
        _STATE["ssm"] = boto3.client("ssm", region_name=REGION)
    return _STATE["ssm"]


def _get_param(name: str) -> str:
    param = _ssm().get_parameter(Name=name, WithDecryption=True)["Parameter"]
    return str(param["Value"])


def _put_param(name: str, value: str) -> None:
    _ssm().put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)


def _log_client() -> Any:
    if "logs" not in _STATE:
        _STATE["logs"] = boto3.client("logs", region_name=REGION)
    return _STATE["logs"]


def _guid_lines(
    needle: str,
    since_epoch: float,
    *,
    head_sha: str | None = None,
    field: str | None = None,
    max_streams: int = 50,
) -> list[tuple[int, dict[str, Any]]]:
    """Worker log lines (parsed JSON + CloudWatch ms timestamp) matching a
    GUID-or-SHA needle and optional head_sha / exact-field constraints,
    scanned fresh each call. Streams are walked newest-first up to
    max_streams so hours-old evidence (e.g. the (l) phase) stays
    reachable by the asserts phase after stream rotation.

    Identity is matched on PARSED fields — the needle must sit in
    delivery_guid or head_sha — not substrings, so semantically adjacent
    lines (a retry_queued line naming the same SHA, an error string
    echoing a GUID) cannot false-positive. The scanned stream/event/hit
    count is left in _STATE["last_log_scan"] so a silent poll still shows
    whether anything was scanned at all."""
    streams = _log_client().describe_log_streams(
        logGroupName="/aws/lambda/pr-reviewer-worker",
        orderBy="LastEventTime",
        descending=True,
        limit=max_streams,
    )["logStreams"]
    hits: list[tuple[int, dict[str, Any]]] = []
    scanned_events = 0
    for stream in streams:
        for event in _log_client().get_log_events(
            logGroupName="/aws/lambda/pr-reviewer-worker",
            logStreamName=stream["logStreamName"],
            startTime=int((since_epoch - LOG_CLOCK_SKEW_SECONDS) * 1000),
            limit=300,
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
            if str(parsed.get("delivery_guid")) != needle and str(parsed.get("head_sha")) != needle:
                continue
            if head_sha is not None and str(parsed.get("head_sha")) != head_sha:
                continue
            if field is not None and str(parsed.get("failure_notice_published")) != field:
                continue
            hits.append((int(event["timestamp"]), parsed))
    _STATE["last_log_scan"] = {
        "streams": len(streams),
        "events": scanned_events,
        "hits": len(hits),
    }
    return sorted(hits)


def _poll(
    ok: Callable[[], bool],
    budget: float,
    describe: Callable[[Any], str],
    *describe_args: Any,
) -> None:
    deadline = time.monotonic() + budget
    last = ""
    while time.monotonic() < deadline:
        if ok():
            return
        last = describe(*describe_args)
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"convergence budget ({budget:.0f}s) exhausted; last state: {last}")


# --- (l) permanent-class failure -> immediate notice -----------------------


@pytest.mark.skipif(PHASE != "notice_permanent", reason="phase gate")
def test_stage_permanent_notice_immediate():
    """(l): LLM_401 after the single refetch -> notice IMMEDIATELY (any
    receive count), non-retryable completion, NOT moved to the DLQ.

    Propagation: warm containers hold cached config for up to 30 min
    (ConfigProvider TTL), so probes injected right after the flip may
    still succeed on the cached good key — harmless normal reviews. The
    loop re-probes until one delivery takes the 401 path (bounded by the
    TTL)."""
    state = _load_state()
    previous = state.get("l")
    if previous and "t_notice" not in previous:
        # A prior attempt died mid-phase: restore its backup before redoing.
        # The backup lives in SSM (test-scoped param); the legacy
        # on-disk "key_backup" field is consumed once for pre-hardening
        # state files, then deleted (never written back).
        legacy = str(previous.pop("key_backup", ""))
        backup_param = str(previous.get("key_restore_param", "") or _key_backup_param())
        backed_up = _get_param(backup_param) if previous.get("key_restore_param") else legacy
        _put_param(SSM_KEY, backed_up)
        assert _get_param(SSM_KEY) == backed_up
    state.pop("l", None)

    dlq_before = _queue_depth(DLQ_QUEUE)
    original_key = _get_param(SSM_KEY)
    _put_param(SSM_KEY, original_key)  # write-access probe: no-op rewrite

    # The flip window opens here: flip -> verify -> backup -> save and
    # everything after run under ONE try/finally, so a failed verify,
    # backup write, or save cannot leave the invalid key behind.
    key_restored = False
    try:
        live = _gh_api(f"repos/{REPO}/pulls/{PR_NUMBER}")
        sha0 = str(live["head"]["sha"])
        base_sha = str(live["base"]["sha"])
        bad_key = "t057-acceptance-invalid-key"  # gitleaks:allow - intentionally invalid flip value
        _put_param(SSM_KEY, bad_key)
        assert _get_param(SSM_KEY) == bad_key
        backup_param = _key_backup_param()
        _put_param(backup_param, original_key)  # at-rest in SSM, never the state file
        state["l"] = {
            "sha": sha0,
            "key_param": SSM_KEY,
            "key_restore_param": backup_param,
            "t_flip": time.time(),
            "dlq_before": dlq_before,
        }
        _save_state()

        # Probe loop: each probe either succeeds on a cached good key
        # (published — flip not yet visible; harmless normal review) or takes
        # the 401-after-refetch path (discarded_error + notice, same receive).
        notice: tuple[int, dict[str, Any]] | None = None
        probe_ts = 0.0
        deadline = time.monotonic() + 45 * MINUTE
        while notice is None and time.monotonic() < deadline:
            guid = _inject(sha0, base_sha)
            probe_ts = time.time()
            since = probe_ts - 5

            def _probe_settled(g: str = guid, s: float = since) -> bool:
                return bool(_guid_lines(g, s))

            try:
                _poll(
                    _probe_settled,
                    240.0,
                    lambda g: f"probe {g[:8]}: waiting for terminal line",
                    guid,
                )
            except pytest.fail.Exception:
                continue  # lost probe (quiet window / cold start) — re-probe
            lines = _guid_lines(guid, since)
            flagged = [ln for ln in lines if str(ln[1].get("failure_notice_published")) == "true"]
            if flagged:
                notice = flagged[-1]
            # else: published on the cached good key — loop and re-probe.

        assert notice is not None, (
            "no delivery took the LLM_401 path within 45 min of the flip "
            "(TTL propagation + probes exhausted)"
        )
        ts, line = notice
        latency = ts / 1000 - probe_ts
        assert latency <= 240.0, (
            f"permanent-class notice must be immediate (got {latency:.0f}s; "
            "the transient bound is ~48 min)"
        )
        assert line.get("status") == "discarded_error", "401-after-refetch completes non-retryably"

        canonical = _canonical_comments()
        assert len(canonical) == 1
        expected = NOTICE_TEMPLATE.format(head_sha=sha0)
        assert expected in str(canonical[0]["body"]), (
            "comment must carry the fixed failure template"
        )

        _put_param(SSM_KEY, original_key)
        assert _get_param(SSM_KEY) == original_key, "api key must be restored verbatim"
        key_restored = True
        state["l"]["guid"] = notice[1].get("delivery_guid")
        state["l"]["t_notice"] = ts / 1000
        state["l"]["latency_s"] = latency
        state["l"]["dlq_after"] = _queue_depth(DLQ_QUEUE)
        _save_state()
        assert state["l"]["dlq_after"] == dlq_before, "non-retryable 401 must NOT land in the DLQ"
    finally:
        if not key_restored:
            _put_param(SSM_KEY, original_key)
            assert _get_param(SSM_KEY) == original_key, "finally-restore of the api key failed"


# --- (l2)+(m)+(n) transient exhaustion drill -------------------------------


@pytest.mark.skipif(PHASE != "drill", reason="phase gate")
def test_stage_transient_drill():
    """(l2)+(m)+(n): the ~2h event-driven drill. Stages A-G are resumable;
    see the module docstring for the timeline and safety notes."""
    state = _load_state()
    drill = state.setdefault("drill", {})

    # Stages A-G run inside one try/finally: any early assert between the
    # endpoint flip (A) and its restore (F) still restores the SSM value,
    # so a failed drill never leaves the dead-port endpoint behind.
    # (Stage G runs after the restore; it shares the helper so resume
    # keeps one entry point.)
    if "t_restore" not in drill:
        try:
            _drill_flip_to_restore(drill)
        finally:
            if "t_restore" not in drill and "endpoint_backup" in drill:
                _put_param(SSM_ENDPOINT, drill["endpoint_backup"])
                assert _get_param(SSM_ENDPOINT) == drill["endpoint_backup"], (
                    "finally-restore of the endpoint failed"
                )


def _drill_flip_to_restore(drill: dict[str, Any]) -> None:
    """Stages A-G of the transient drill (see test_stage_transient_drill).

    Split out so the caller can wrap the flip-to-restore span in a single
    try/finally. Every stage guard stays resumable via the state file."""

    # Stage A: back up + flip the endpoint (same host, dead port).
    if "t0" not in drill:
        original_endpoint = _get_param(SSM_ENDPOINT)
        assert "://" in original_endpoint, f"unexpected endpoint shape: {original_endpoint[:40]}"
        rest = original_endpoint.split("://", 1)[1]
        host_and_rest = rest.split("/", 1)
        host = host_and_rest[0]
        assert ":" not in host, f"endpoint already carries a port; refusing to stack: {host}"
        flipped = f"{original_endpoint.split('://', 1)[0]}://{host}:9/" + (
            "/" + host_and_rest[1] if len(host_and_rest) > 1 else ""
        )
        _put_param(SSM_ENDPOINT, original_endpoint)  # write-access probe
        live = _gh_api(f"repos/{REPO}/pulls/{PR_NUMBER}")
        drill.update(
            {
                "endpoint_param": SSM_ENDPOINT,
                "endpoint_backup": original_endpoint,
                "endpoint_flipped": flipped,
                "sha0": str(live["head"]["sha"]),
                "base_sha": str(live["base"]["sha"]),
                "t0": time.time(),
            }
        )
        _put_param(SSM_ENDPOINT, flipped)
        assert _get_param(SSM_ENDPOINT) == flipped
        _save_state()

    # Stage B: inject probe deliveries until one's FIRST receive fails
    # RETRYABLY (timeout / connection_error -> TRANSIENT) — proving the
    # flip is live end-to-end. Warm containers may serve earlier probes
    # from the cached good endpoint (they publish harmlessly, like the
    # (l) phase); the ConfigProvider TTL (30 min) bounds the wait. The
    # probe that fails retryably IS M1: it is already consuming its
    # receive #1 and continues into the retry cycle.
    if "m1" not in drill:
        deadline = time.monotonic() + 45 * MINUTE
        first: tuple[int, dict[str, Any]] | None = None
        while first is None and time.monotonic() < deadline:
            guid = _inject(drill["sha0"], drill["base_sha"])
            probe_ts = time.time()
            since = probe_ts - 5

            def _probe_settled(g: str = guid, s: float = since) -> bool:
                return bool(_guid_lines(g, s))

            try:
                _poll(
                    _probe_settled,
                    240.0,
                    lambda g: f"probe {g[:8]}: waiting for line",
                    guid,
                )
            except pytest.fail.Exception:
                continue  # lost probe — re-probe
            candidate = _guid_lines(guid, since)[0]
            if candidate[1].get("status") == "retry_queued" and candidate[1].get("error_class") in {
                "timeout",
                "connection_error",
            }:
                first = candidate
                drill["m1"] = guid
                drill["m1_first_ts"] = candidate[0] / 1000
                _save_state()
            # else: published on the cached good endpoint — re-probe.
        assert first is not None, (
            "no probe failed retryably within 45 min of the endpoint flip "
            "(TTL propagation + probes exhausted)"
        )

    # Stage C: (l2) M1 receive #5 = final attempt -> notice PUBLISHES.
    # Bound measured from M1's OWN first receive (the SC-006 ~48-min retry
    # bound) — TTL propagation delay before M1 must not pollute it.
    if "t_notice_m1" not in drill:
        deadline = drill["m1_first_ts"] + 56 * MINUTE

        def _m1_notice_lines() -> list[tuple[int, dict[str, Any]]]:
            # Pure predicate helper: no _STATE/drill mutation — the caller
            # records t_notice_m1 explicitly after _poll converges.
            return _guid_lines(drill["m1"], drill["t0"] - 5, head_sha=drill["sha0"], field="true")

        budget = max(deadline - time.time(), 60.0)
        elapsed = f"t0+{((time.time() - drill['t0']) / MINUTE):.0f}m"
        _poll(
            lambda: bool(_m1_notice_lines()),
            budget,
            lambda: (
                f"waiting for M1 final-attempt notice ({elapsed}; "
                f"scan={_STATE.get('last_log_scan')})"
            ),
        )
        drill["t_notice_m1"] = _m1_notice_lines()[-1][0] / 1000
        span = drill["t_notice_m1"] - drill["m1_first_ts"]
        assert span <= 55 * MINUTE, (
            f"SC-006 bound: final-attempt notice at +{span / MINUTE:.0f}m of retrying"
        )
        # SQS moves an exhausted message to the DLQ when its post-failure
        # VISIBILITY expires (one more 720s window after the final receive),
        # so the DLQ depth lags the notice by up to ~12 min.
        _poll(
            lambda: _queue_depth(DLQ_QUEUE) >= 1,
            15 * MINUTE,
            lambda: f"waiting for M1 to land in the DLQ (depth={_queue_depth(DLQ_QUEUE)})",
        )
        drill["dlq_after_m1"] = _queue_depth(DLQ_QUEUE)
        assert drill["dlq_after_m1"] >= 1, "exhausted message must sit in the DLQ"
        _save_state()

    # Stage D: push S1 — its webhook delivery M2 fails + retries.
    if "sha1" not in drill:
        drill["sha1"] = _push_file("us5-s1")
        drill["t_s1"] = time.time()
        _save_state()

    # Stage E: wait for M2's receive #4, then push S2 so M2's #5 fences stale.
    if "t_s2" not in drill:
        since = drill["t_s1"] - 5

        def _m2_attempts() -> list[tuple[int, dict[str, Any]]]:
            return _guid_lines(drill["sha1"], since, head_sha=drill["sha1"])

        _poll(
            lambda: len(_m2_attempts()) >= 4,
            60 * MINUTE,
            lambda: (
                f"M2 attempts seen: {len(_m2_attempts())} (need 4; "
                f"t0+{((time.time() - drill['t0']) / MINUTE):.0f}m)"
            ),
        )
        drill["m2_guid"] = _m2_attempts()[-1][1]["delivery_guid"]
        drill["sha2"] = _push_file("us5-s2")
        drill["t_s2"] = time.time()
        _save_state()

    # Stage F: (m) the retry-cycle delivery's receive #5 fences stale ->
    # skipped-stale; only then restore the endpoint so recovery traffic
    # succeeds.
    #
    # Target: S2's OWN webhook delivery ("M3", head==sha2). The injected
    # M2 (head==sha1) is deterministically superseded AT ESTABLISH when
    # its #5 arrives, because M3's establish rewrote last_seen ~12 min
    # earlier (observed live; that rejection is asserted in the asserts
    # phase). M3 keeps the equality path to #5, so its final-attempt
    # notice runs — and the notice's fence must then see a head NEWER
    # than sha2.
    #
    # Timing (SPR-65 adaptive establish-observed gate): receive #5 lands
    # ~720s after receive #4 (visibility), and the #4 log line is emitted
    # ~2-3s INTO that receive. The old fixed arithmetic scheduled the push
    # at r4_ts + 717s (~0.5s into the #5 processing window); the gate below
    # instead MEASURES the residual from the observed retry intervals
    # (median log-to-log cycle minus ~2s of end-of-processing offset), so
    # slow cold starts push later and fast ones sooner while the push still
    # lands inside the #5 establish->notice-fence window.
    # The dead-port connect fails in ~1-2s, so the notice's fence reads
    # ~3-4s after establish — after the push (GitHub's ref moves at push
    # time, before M3's own delivery even establishes). Both
    # establish-level superseded and fence-level stale of the NOTICE map
    # to skipped-stale, so the choreography wins on either route. The
    # push runs in the DRIVER (gh creds outlive the ~15-min AWS windows):
    # the harness records push_at in the state file and the driver
    # executes it, so the timed push survives pytest credential deaths.
    #
    # INVARIANT — why r4 (non-final) is the gate anchor: r4 still has a
    # next retry coming (#5), so a push scheduled off its observed line
    # lands while the race is still live and #5's fence can go stale. A
    # gate anchored on the FINAL receive's establish would fire the push
    # after the record's review already completed (post-terminal): the M3
    # skipped-stale line could then never trigger again and the (m) drill
    # would go green forever while proving nothing. The M3 skipped-stale
    # and M2 superseded-at-establish asserts are byte-identical in force:
    # this gate may change WHEN the S3 push fires, never WHAT is asserted.
    if "t_restore" not in drill:
        since_f = drill["t_s2"] - 10

        if "m3_guid" not in drill:

            def _cyclers() -> dict[str, int]:
                counts: dict[str, int] = {}
                for _, parsed in _guid_lines(drill["sha2"], since_f):
                    if parsed.get("status") != "retry_queued":
                        continue
                    g = str(parsed.get("delivery_guid"))
                    counts[g] = counts.get(g, 0) + 1
                return counts

            _poll(
                lambda: bool(_cyclers()),
                10 * MINUTE,
                lambda: "no sha2 retry-cycle delivery yet",
            )
            # Deterministic pick: most retry_queued attempts wins; ties
            # break on the lexicographically smallest GUID. max() over the
            # scan-ordered counts dict was nondeterministic under stream
            # rotation (same input set, different schedule per run). Compute
            # once — the old key-function re-scanned CloudWatch per
            # comparison.
            counts = _cyclers()
            drill["m3_guid"] = sorted(counts, key=lambda g: (-counts[g], g))[0]
            _save_state()
        cycler = drill["m3_guid"]

        def _m3_retries() -> list[tuple[int, dict[str, Any]]]:
            return [
                line
                for line in _guid_lines(cycler, since_f, head_sha=drill["sha2"])
                if line[1].get("status") == "retry_queued"
            ]

        _poll(
            lambda: len(_m3_retries()) >= 4,
            40 * MINUTE,
            lambda: f"M3 retries seen: {len(_m3_retries())} (need 4)",
        )
        observed = _m3_retries()
        if len(observed) < 4:
            # Fresh-scan regression guard: the poll above converged, so a
            # short re-read means CloudWatch rotation/skew, not absence.
            # Fail LOUD — never fall back to the old arithmetic silently.
            pytest.fail(
                f"SPR-65 gate: r4 converged but fresh scan shows "
                f"{len(observed)} retry lines (need 4)",
                pytrace=False,
            )
        r4_ts = observed[3][0] / 1000
        if "push_at" not in drill:
            stamps = [ts / 1000 for ts, _ in observed[:4]]
            gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:], strict=False)]
            ordered = sorted(gaps)
            median_gap = ordered[len(ordered) // 2]
            # Log lines mark END of processing; the next receive starts one
            # visibility cycle after this receive's START (~2-3s before its
            # log on the dead-port path). Land the push ~1s after the
            # predicted #5 establish: residual = median cycle - 2s, clamped
            # to [700, 725]s so a skewed scan cannot throw the push outside
            # the establish->fence window. The old fixed 717.0s survives
            # only as the logged comparison delta, never as a fallback.
            residual = min(max(median_gap - 2.0, 700.0), 725.0)
            drill["push_at"] = r4_ts + residual
            drill["push_note"] = "us5-s3"
            drill["push_gate"] = {
                "anchor": "m3-r4-establish-observed",
                "r4_observed": r4_ts,
                "gaps": gaps,
                "residual": residual,
                "old_arithmetic_delta": 717.0,
            }
            print(
                f"[SPR-65 gate] r4_observed={r4_ts:.0f} push_at={drill['push_at']:.0f} "
                f"residual={residual:.1f}s (old arithmetic would use 717.0s; "
                f"delta={residual - 717.0:+.1f}s; gaps={[f'{g:.1f}' for g in gaps]})"
            )
            _save_state()

        def _m3_skipped_lines() -> list[tuple[int, dict[str, Any]]]:
            # Pure predicate helper: no _STATE/drill mutation — the caller
            # records t_m3_skipped explicitly after _poll converges.
            return _guid_lines(cycler, since_f, head_sha=drill["sha2"], field="skipped-stale")

        budget = max(r4_ts + 15 * MINUTE - time.time(), 60.0)
        _poll(
            lambda: bool(_m3_skipped_lines()),
            budget,
            lambda: (
                f"waiting for M3 skipped-stale (push_at={drill['push_at']:.0f}; "
                f"scan={_STATE.get('last_log_scan')})"
            ),
        )
        drill["t_m3_skipped"] = _m3_skipped_lines()[-1][0] / 1000
        _put_param(SSM_ENDPOINT, drill["endpoint_backup"])
        assert _get_param(SSM_ENDPOINT) == drill["endpoint_backup"]
        drill["t_restore"] = time.time()
        _save_state()

    # Stage G: (n) a post-revert receive succeeds — the comment reverts
    # from notice content to review content; state ACTIVE at the new head.
    if "t_n" not in drill:
        if "t_recovery_probe" not in drill:
            # The exhausted delivery sits in the DLQ with nobody left to
            # recover the CLAIMED state (observed live: after its #5 the
            # remaining receives crashed on a corrupt key value and
            # exhausted maxReceiveCount). (n)'s mechanism is "a
            # post-refetch receive succeeds" — inject one fresh delivery
            # at the live head to prove the revert end-to-end.
            live = _gh_api(f"repos/{REPO}/pulls/{PR_NUMBER}")
            drill["recovery_sha"] = str(live["head"]["sha"])
            drill["recovery_guid"] = _inject(drill["recovery_sha"], str(live["base"]["sha"]))
            drill["t_recovery_probe"] = time.time()
            _save_state()

        def _reverted_snapshot() -> dict[str, Any] | None:
            # Pure predicate helper: no _STATE mutation — the caller stores
            # n_observed explicitly after _poll converges.
            canonical = _canonical_comments()
            if len(canonical) != 1:
                return None
            body = str(canonical[0]["body"])
            item = _state_item()
            if item is None or item.get("status") != "ACTIVE":
                return None
            if str(item.get("head_sha")) not in {
                drill["sha2"],
                drill.get("sha3", ""),
                drill.get("recovery_sha", ""),
            }:
                return None
            if any(NOTICE_TEMPLATE.format(head_sha=drill[sha]) in body for sha in ("sha0", "sha2")):
                return None
            return {
                "head": str(item.get("head_sha")),
                "comment": canonical[0],
            }

        budget = max(drill["t_restore"] + 45 * MINUTE - time.time(), 10 * MINUTE)
        elapsed = f"t0+{((time.time() - drill['t0']) / MINUTE):.0f}m"
        _poll(
            lambda: _reverted_snapshot() is not None,
            budget,
            lambda: f"state={_state_item()} ({elapsed})",
        )
        observed = _reverted_snapshot()
        assert observed is not None, "revert snapshot lost after convergence"
        _STATE["n_observed"] = observed
        drill["n_head"] = observed["head"]
        if observed["head"] != drill["sha2"]:
            drill["sha3"] = observed["head"]  # a recovery push was needed
        drill["t_n"] = time.time()
        _save_state()


# --- durable record checks --------------------------------------------------


@pytest.mark.skipif(PHASE != "asserts", reason="phase gate")
def test_assert_scenarios_recorded():
    """(l2)(m)(n)(l) re-derived from the state file + logs + GitHub."""
    state = _load_state()
    assert "l" in state and "drill" in state, "run notice_permanent and drill first"
    drill = state["drill"]

    # (l) permanent: immediate, non-retryable, no DLQ.
    assert state["l"]["latency_s"] <= 240.0
    assert state["l"]["dlq_after"] == state["l"]["dlq_before"]
    l_lines = _guid_lines(state["l"]["guid"], state["l"]["t_flip"] - 5, field="true")
    assert l_lines, "(l) notice line must persist in CloudWatch"

    # (l2) transient: notice at the FINAL attempt within the SC-006 bound.
    span = drill["t_notice_m1"] - drill["m1_first_ts"]
    assert 0 < span <= 55 * MINUTE, f"M1 notice at +{span / MINUTE:.0f}m of retrying"
    m1_lines = _guid_lines(drill["m1"], drill["t0"] - 5, field="true")
    assert m1_lines, "(l2) M1 final-attempt notice line must persist"
    assert drill["dlq_after_m1"] >= 1

    # (m) stale notice skipped at the currency fence: M3 (S2's delivery)
    # reaches its final attempt with the equality path intact; the timed
    # S3 push moves the live head inside its establish->notice-fence
    # window, so the notice is skipped-stale. The injected M2 (S1) is
    # superseded at establish before its #5 (M3's establish rewrote
    # last_seen ~12 min earlier) — the stale-delivery rejection discipline.
    m3_lines = _guid_lines(
        drill["m3_guid"], drill["t_s2"] - 10, head_sha=drill["sha2"], field="skipped-stale"
    )
    assert m3_lines, "(m) M3 skipped-stale line must persist"
    m2_lines = _guid_lines(drill["m2_guid"], drill["t_s1"] - 5, head_sha=drill["sha1"])
    assert any(str(line[1].get("status")) == "discarded_superseded" for line in m2_lines), (
        "(m) M2 must be superseded at establish before its final attempt"
    )

    # (n) success reverts the comment to review content.
    canonical = _canonical_comments()
    assert len(canonical) == 1
    body = str(canonical[0]["body"])
    assert all(NOTICE_TEMPLATE.format(head_sha=drill[sha]) not in body for sha in ("sha0", "sha2"))
    item = _state_item()
    assert item is not None and item.get("status") == "ACTIVE"
    assert str(item.get("head_sha")) == drill["n_head"]
    assert str(item.get("comment_id")) == str(canonical[0]["id"])


# --- (j) the redrive drill (runbook steps 3-5) ------------------------------


@pytest.mark.skipif(PHASE != "redrive", reason="phase gate")
def test_stage_redrive_drain_and_converge():
    """(j): move the DLQ back to the work queue after the fix; the DLQ
    drains to zero and canonical comments converge — stale redrives are
    fence-discarded and must NOT mutate the surviving comment."""
    state = _load_state()
    assert "drill" in state, "run the drill phase first"
    drill = state["drill"]

    dlq_url = _queue_url(DLQ_QUEUE)
    depth_before = _queue_depth(DLQ_QUEUE)
    assert depth_before >= 1, "drill must have left messages in the DLQ"

    canonical = _canonical_comments()
    assert len(canonical) == 1
    pre_id = int(canonical[0]["id"])
    pre_updated = str(canonical[0]["updated_at"])

    dlq_arn = boto3.client("sqs", region_name=REGION).get_queue_attributes(
        QueueUrl=dlq_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]
    work_arn = boto3.client("sqs", region_name=REGION).get_queue_attributes(
        QueueUrl=_queue_url(WORK_QUEUE), AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    sqs = boto3.client("sqs", region_name=REGION)
    moved_manually = False
    try:
        task = sqs.start_message_move_task(SourceArn=dlq_arn, DestinationArn=work_arn)
        drill["move_task_handle"] = task.get("TaskHandle", "")
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
        # Narrowed to the botocore fault family: transport errors
        # (BotoCoreError) and service-side API faults — AccessDenied,
        # QueueDoesNotExist, throttling, permission-shape variance
        # (ClientError, a sibling of BotoCoreError, not a subclass).
        # Programming errors (TypeError, KeyError, ...) still propagate loud.
        drill["move_task_error"] = f"{type(exc).__name__}: {exc}"
        # Runbook deviation (logged per step 5): surgical equivalent move —
        # receive from the DLQ, re-send to the work queue, delete. Same
        # semantics as StartMessageMoveTask for a handful of messages.
        # Custom MessageAttributes are carried over verbatim; SQS-managed
        # system attributes (e.g. ApproximateReceiveCount) cannot be set
        # via SendMessage and reset on the re-send — recorded, not hidden.
        moved = 0
        while True:
            batch = sqs.receive_message(
                QueueUrl=dlq_url,
                MaxNumberOfMessages=10,
                VisibilityTimeout=60,
                AttributeNames=["All"],
                MessageAttributeNames=["All"],
            ).get("Messages", [])
            if not batch:
                break
            for message in batch:
                send_kwargs: dict[str, Any] = {
                    "QueueUrl": _queue_url(WORK_QUEUE),
                    "MessageBody": message["Body"],
                }
                if message.get("MessageAttributes"):
                    send_kwargs["MessageAttributes"] = {
                        name: {
                            key: attr[key]
                            for key in ("StringValue", "BinaryValue", "DataType")
                            if key in attr
                        }
                        for name, attr in message["MessageAttributes"].items()
                    }
                sqs.send_message(**send_kwargs)
                sqs.delete_message(QueueUrl=dlq_url, ReceiptHandle=message["ReceiptHandle"])
                moved += 1
        drill["manual_moved"] = moved
        drill["manual_move_note"] = (
            "custom MessageAttributes preserved; SQS-managed receive counts reset on re-send"
        )
        moved_manually = True
    _save_state()

    deadline = time.time() + 15 * MINUTE

    def _drained() -> bool:
        return _queue_depth(DLQ_QUEUE) == 0

    _poll(
        _drained,
        max(deadline - time.time(), 60.0),
        lambda: f"DLQ depth={_queue_depth(DLQ_QUEUE)}",
    )

    # Convergence: exactly one canonical comment, unchanged identity — the
    # redriven stale revisions (S0, S1 vs live head) must be fence-discarded.
    # Condition-polled (work-queue drain + comment identity) with an overall
    # timeout instead of a fixed settle sleep.
    def _redrive_converged() -> bool:
        # Pure predicate: no _STATE mutation — identity is asserted
        # explicitly after _poll converges.
        settled = _canonical_comments()
        return (
            len(settled) == 1
            and int(settled[0]["id"]) == pre_id
            and str(settled[0]["updated_at"]) == pre_updated
        )

    _poll(
        lambda: _queue_depth(WORK_QUEUE) == 0 and _redrive_converged(),
        15 * MINUTE,
        lambda: (
            f"work depth={_queue_depth(WORK_QUEUE)} "
            f"dlq depth={_queue_depth(DLQ_QUEUE)} comments={len(_canonical_comments())}"
        ),
    )
    after = _canonical_comments()
    assert len(after) == 1, "constitution: exactly one canonical comment after redrive"
    # Id-guard first: an id mismatch fails HERE with both ids named — the
    # updated_at comparison below is only meaningful on the same comment.
    assert int(after[0]["id"]) == pre_id, (
        f"redrive must not duplicate or replace the comment (was {pre_id}, now {after[0]['id']})"
    )
    assert str(after[0]["updated_at"]) == pre_updated, (
        "stale redrives must be discarded without mutating the comment"
    )
    item = _state_item()
    assert item is not None and item.get("status") == "ACTIVE"
    assert str(item.get("head_sha")) == drill["n_head"]
    drill["redrive"] = {
        "dlq_before": depth_before,
        "dlq_after": 0,
        "moved_manually": moved_manually,
        "t_done": time.time(),
    }
    _save_state()

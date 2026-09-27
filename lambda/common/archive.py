"""Run archive builders (HLD-004 §6 Archive & Index Contracts).

Pure builders + validators for the per-run archive: `meta.json` (fixed
shape, pipeline-keyed status mapping), `events.jsonl` (ts-ordered,
re-validated lines), S3 key construction (viewer route-regex
compatible), pipeline resolution, the index-row decision, and the index
item (exactly the T031-projected attribute set).

Stored-row `pk` (`archive:{run_id}`) is PROVISIONAL — HLD pins the
projected attributes via the GSI contract but not the row key; T031/T032
own the table design and may relocate it. `started_ts` rides as epoch-ms
INT ("timestamps epoch ms", §6); T031's parenthetical "(S)" is flagged
for that ticket to resolve.

Side effects (S3 puts, the DDB write) belong to the caller
(`worker_handler` threads injected clients); this module never imports
boto3 and performs no I/O. Pure stdlib.
"""

from __future__ import annotations

import json
import re
from typing import Any

from common.events import EVENT_VERSION, EventsError, to_jsonl

ARCHIVE_VERSION = 1

PIPELINES = frozenset({"multi_agent", "single_pass", "phase0_shadow"})

STATUSES_BY_PIPELINE = {
    "multi_agent": frozenset({"published", "degraded_single_pass", "failed"}),
    "single_pass": frozenset({"published", "failed"}),
    "phase0_shadow": frozenset({"published", "failed"}),
}

ARCHIVE_FILENAMES = ("events.jsonl", "meta.json")

# Stage-owned event types: the sequencer's legs (HLD §5 flow tree). Their
# presence means fan-out ran — anything else (or nothing) is single-pass.
_MULTI_AGENT_TYPES = frozenset(
    {
        "agent_started",
        "agent_reasoning",
        "agent_completed",
        "agent_failed",
        "verification_done",
        "verification_failed",
        "review_synthesized",
        "synthesizer_failed",
        "degraded_to_single_pass",
    }
)

_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")


class ArchiveError(ValueError):
    """Typed archive rejection: `field` names the offending field,
    `reason` is a machine-readable code (`bad_run_id`, `bad_pr`,
    `bad_sha`, `bad_pipeline`, `bad_status`, `bad_ts`, `bad_filename`,
    `bad_key`, `bad_findings_n`, `bad_event`). Never carries payload
    content."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid archive: {field}: {reason}")


def _check_run_id(run_id: Any) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        raise ArchiveError("run_id", "bad_run_id")
    return run_id


def _check_sha(sha: Any) -> str:
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise ArchiveError("sha", "bad_sha")
    return sha


def _check_pr(pr: Any) -> int:
    if not isinstance(pr, int) or isinstance(pr, bool) or pr < 1:
        raise ArchiveError("pr", "bad_pr")
    return pr


def _check_ts(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ArchiveError(field, "bad_ts")
    return value


def check_status(pipeline: Any, status: Any) -> tuple[str, str]:
    """Enforce the §6 status mapping (status = REVIEW outcome). Returns
    the validated pair; anything off-mapping raises `ArchiveError`."""
    if pipeline not in STATUSES_BY_PIPELINE:
        raise ArchiveError("pipeline", "bad_pipeline")
    if status not in STATUSES_BY_PIPELINE[pipeline]:
        raise ArchiveError("status", "bad_status")
    return pipeline, status


def build_meta(
    *,
    run_id: str,
    pr: int,
    sha: str,
    pipeline: str,
    status: str,
    started_ts: int,
    finished_ts: int,
) -> dict[str, Any]:
    """Assemble a validated `meta.json` object (HLD §6 fixed shape)."""
    check_status(pipeline, status)
    meta = {
        "v": EVENT_VERSION,
        "run_id": _check_run_id(run_id),
        "pr": _check_pr(pr),
        "sha": _check_sha(sha),
        "pipeline": pipeline,
        "status": status,
        "started_ts": _check_ts(started_ts, "started_ts"),
        "finished_ts": _check_ts(finished_ts, "finished_ts"),
        "archive_version": ARCHIVE_VERSION,
    }
    if meta["finished_ts"] < meta["started_ts"]:
        # Monotonicity is a shape invariant: an archive that claims to
        # finish before it started is malformed however it got there
        # (bot r1:88).
        raise ArchiveError("finished_ts", "bad_ts_order")
    return meta


def render_meta(meta: dict[str, Any]) -> str:
    """Render `meta.json` bytes (compact, sorted — same discipline as
    `to_jsonl`)."""
    return json.dumps(meta, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def render_events_jsonl(events: list[dict[str, Any]]) -> str:
    """Render `events.jsonl`: consumer order (ascending `ts`, stable),
    every line re-validated through `to_jsonl` at this render boundary —
    a malformed event fails the archive, never serializes half a run."""
    for event in events:
        ts = event.get("ts") if isinstance(event, dict) else None
        if not isinstance(ts, int) or isinstance(ts, bool):
            # Fail typed BEFORE sorting: a missing/string `ts` would
            # otherwise silently reorder (default 0) or blow up as a
            # confusing TypeError inside the sort key (bot r1:126). Same
            # class+code the events validator uses for a bad ts.
            raise EventsError("ts", "bad_ts")
    ordered = sorted(events, key=lambda event: event["ts"])
    return "\n".join(to_jsonl(event) for event in ordered)


def s3_key(pr_number: int, sha: str, run_id: str, filename: str) -> str:
    """Build the archive S3 key `runs/{pr}/{sha}/{run_id}/{filename}`
    (viewer route-regex compatible)."""
    if filename not in ARCHIVE_FILENAMES:
        raise ArchiveError("filename", "bad_filename")
    return f"runs/{_check_pr(pr_number)}/{_check_sha(sha)}/{_check_run_id(run_id)}/{filename}"


def resolve_pipeline(events: list[dict[str, Any]]) -> str:
    """Pipeline discriminator from the run's own events: fan-out stage
    events (or the degrade marker) mean `multi_agent`; anything else is
    `single_pass`. (`phase0_shadow` has no producer yet — T035/T036.)"""
    for event in events:
        if isinstance(event, dict) and event.get("type") in _MULTI_AGENT_TYPES:
            return "multi_agent"
    return "single_pass"


def should_write_index_row(events: list[dict[str, Any]]) -> bool:
    """`review_skipped` is an EVENT, never a row (HLD §6): a skipped run
    archives its events to S3 but writes no index row."""
    return not any(
        isinstance(event, dict) and event.get("type") == "review_skipped" for event in events
    )


def findings_count(events: list[dict[str, Any]]) -> int:
    """Published-findings count for the index row: the synthesizer's
    merged total (0 when the stage never ran). Takes the max across
    `review_synthesized` events rather than a sum — `findings_merged_n`
    is the run's final merged total, so multiple synthesized events
    (not producible today) must not double-count (bot r1:159)."""
    count = 0
    for event in events:
        if isinstance(event, dict) and event.get("type") == "review_synthesized":
            value = event.get("findings_merged_n")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                count = max(count, value)
    return count


def build_index_item(
    *,
    run_id: str,
    pr: int,
    sha: str,
    pipeline: str,
    status: str,
    started_ts: int,
    archive_s3_key: str,
    archive_written_at: int,
    findings_n: int,
) -> dict[str, Any]:
    """Assemble the best-effort DDB index item: exactly the T031-projected
    attribute set (`pk` provisional — see module docstring)."""
    check_status(pipeline, status)
    if not isinstance(archive_s3_key, str) or not archive_s3_key:
        raise ArchiveError("archive_s3_key", "bad_key")
    if not isinstance(findings_n, int) or isinstance(findings_n, bool) or findings_n < 0:
        raise ArchiveError("findings_n", "bad_findings_n")
    return {
        "pk": f"archive:{_check_run_id(run_id)}",
        "pr_number": _check_pr(pr),
        "started_ts": _check_ts(started_ts, "started_ts"),
        "sha": _check_sha(sha),
        "status": status,
        "pipeline": pipeline,
        "archive_s3_key": archive_s3_key,
        "archive_written_at": _check_ts(archive_written_at, "archive_written_at"),
        "findings_n": findings_n,
    }

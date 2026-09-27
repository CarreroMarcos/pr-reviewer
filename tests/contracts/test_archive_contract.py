"""SPR-113 T028: archive contract tests (HLD-004 §6 Archive & Index Contracts).

`meta.json` = `{v: 1, run_id (uuid4 hex 32), pr, sha, pipeline,
status, started_ts, finished_ts (epoch ms), archive_version: 1}`;
status mapping per pipeline (`multi_agent` → published |
degraded_single_pass | failed; `single_pass` → published | failed;
`phase0_shadow` → published | failed; status = REVIEW outcome);
`review_skipped` is an EVENT, never a row; run events ordered by `ts`;
S3 keys match the viewer route regex
`^runs/\\d+/[0-9a-f]{40}/[0-9a-f]{32}/(events\\.jsonl|meta\\.json)$`
(hex run_id compatibility, gate 5).

Worker rows (T029 wiring): S3 puts of `events.jsonl` + `meta.json`
happen AFTER finalize (never inside the review callback — pinned by
put-after-finalize ordering on one shared timeline); the best-effort
DDB index row is written AFTER finalize by `worker_handler` (a failing
index write never fails the review); the degraded fallback run archives
`degraded_single_pass` under `multi_agent`; refused content archives
`failed`; a `review_skipped` run writes events but never an index row.

Table semantics reuse the exact-condition `dynamodb_stub.InMemoryTable`
(sibling `tests/state_machine/` dir, added to the path below); S3 is an
in-file recording fake (no network — `put_object` port shape only).

RED state: `common.archive` does not exist — collection errors on import.
"""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "state_machine"))  # noqa: E402

from dynamodb_stub import InMemoryTable  # noqa: E402

import worker_handler  # noqa: E402
from common.archive import (  # noqa: E402
    ArchiveError,
    build_index_item,
    build_meta,
    check_status,
    render_events_jsonl,
    render_meta,
    resolve_pipeline,
    s3_key,
    should_write_index_row,
)
from common.config import ConfigProvider  # noqa: E402
from common.events import (  # noqa: E402
    EventsError,
    agent_completed,
    agent_started,
    checkpoint,
    degraded_to_single_pass,
    review_skipped,
    review_started,
    to_jsonl,
)
from common.fanout import FanoutDegraded  # noqa: E402
from worker_handler import _process_record  # noqa: E402

REPO = "octo-org/hello-world"
PR_NUMBER = 42
SHA_B = "bb" * 20
BASE_SHA = "00" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
NOW = 1_750_000_000
POST_ID = 987654
ENDPOINT = "https://llm.example.test/v1/chat/completions"
RUN_ID = "0123456789abcdef0123456789abcdef"
BUCKET = "test-archive-bucket"

# The viewer route regex, verbatim from the task contract (lives here so
# the implementation's key shape is checked against the contract text,
# not against itself).
ROUTE_RE = re.compile(r"^runs/\d+/[0-9a-f]{40}/[0-9a-f]{32}/(events\.jsonl|meta\.json)$")

REVIEW_BODY = (
    "## Summary\nAdds input validation.\n\n"
    "## Findings\n- [HIGH] `src/main.py:12` — Missing bound. Fix: add a check.\n\n"
    "## Risk Notes\nNone.\n"
)

SSM_VALUES = {
    "/pr-reviewer/github-token": "github-token-value",  # noqa: S105 (fake fixture)
    "/pr-reviewer/glm-api-key": "glm-key-value",  # noqa: S105 (fake fixture)
    "/pr-reviewer/glm-model": "glm-5.3-flash",
    "/pr-reviewer/glm-endpoint": ENDPOINT,
}

DIFF_STATS = {"files": 1, "additions": 5, "deletions": 2}


def meta_kwargs(**overrides):
    params = {
        "run_id": RUN_ID,
        "pr": PR_NUMBER,
        "sha": SHA_B,
        "pipeline": "single_pass",
        "status": "published",
        "started_ts": NOW * 1000,
        "finished_ts": NOW * 1000 + 5000,
    }
    params.update(overrides)
    return params


def started_event(ts):
    return review_started(
        pr=PR_NUMBER, sha=SHA_B, diff_stats=dict(DIFF_STATS), run_id=RUN_ID, ts=ts
    )


# --- meta.json shape ---------------------------------------------------------------


def test_meta_shape_and_literals():
    meta = build_meta(**meta_kwargs())
    assert meta == {
        "v": 1,
        "run_id": RUN_ID,
        "pr": PR_NUMBER,
        "sha": SHA_B,
        "pipeline": "single_pass",
        "status": "published",
        "started_ts": NOW * 1000,
        "finished_ts": NOW * 1000 + 5000,
        "archive_version": 1,
    }
    assert re.fullmatch(r"[0-9a-f]{32}", meta["run_id"]) is not None
    assert json.loads(render_meta(meta)) == meta


def test_meta_rejects_dashed_36_char_run_id():
    """Gate-5 regression pin: a dashed uuid4 (36 chars) is NOT a valid
    run_id — the old route regex required a shape no generated id could
    match."""
    with pytest.raises(ArchiveError):
        build_meta(**meta_kwargs(run_id="01234567-89ab-cdef-0123-456789abcdef"))


def test_meta_rejects_bad_scalars():
    bad_rows = [
        ("sha", "zz"),
        ("sha", "bb" * 19 + "zz"),
        ("pr", 0),
        ("pr", True),
        ("pr", "42"),
        ("started_ts", -1),
        ("started_ts", True),
        ("finished_ts", "now"),
        ("pipeline", "multi-pass"),
        ("finished_ts", "x"),
    ]
    for field, value in bad_rows:
        with pytest.raises(ArchiveError):
            build_meta(**meta_kwargs(**{field: value}))


# --- status mapping per pipeline ---------------------------------------------------------


def test_status_mapping_allowed():
    allowed = {
        "multi_agent": ("published", "degraded_single_pass", "failed"),
        "single_pass": ("published", "failed"),
        "phase0_shadow": ("published", "failed"),
    }
    for pipeline, statuses in allowed.items():
        for status in statuses:
            assert check_status(pipeline, status) == (pipeline, status)
            build_meta(**meta_kwargs(pipeline=pipeline, status=status))
    with pytest.raises(ArchiveError):
        check_status("single_pass", "degraded_single_pass")
    with pytest.raises(ArchiveError):
        check_status("phase0_shadow", "degraded_single_pass")
    with pytest.raises(ArchiveError):
        check_status("multi_agent", "published_finalize_conflict")
    with pytest.raises(ArchiveError):
        check_status("no_such_pipeline", "published")


# --- pipeline resolution + index-row decision ------------------------------------------------


def test_resolve_pipeline():
    assert resolve_pipeline([]) == "single_pass"
    assert resolve_pipeline([started_event(1)]) == "single_pass"
    wave = [
        agent_started(specialty="correctness", run_id=RUN_ID, ts=1),
        agent_completed(
            specialty="correctness",
            findings_n=0,
            latency_ms=1,
            tokens_in=1,
            tokens_out=1,
            findings=[],
            run_id=RUN_ID,
            ts=2,
        ),
    ]
    assert resolve_pipeline(wave) == "multi_agent"
    degraded_only = [
        degraded_to_single_pass(
            reason="insufficient_budget", failed_stage="wave", run_id=RUN_ID, ts=3
        )
    ]
    assert resolve_pipeline(degraded_only) == "multi_agent"


def test_review_skipped_never_a_row():
    skipped = review_skipped(reason="empty_diff", pr=PR_NUMBER, sha=SHA_B, run_id=RUN_ID, ts=1)
    assert should_write_index_row([skipped]) is False
    assert should_write_index_row([started_event(1), skipped]) is False
    assert should_write_index_row([]) is True
    assert should_write_index_row([started_event(1)]) is True


# --- events.jsonl ordering ------------------------------------------------------------


def test_render_orders_by_ts_and_validates():
    first = started_event(3000)
    second = checkpoint(stage="established", run_id=RUN_ID, ts=1000)
    third = checkpoint(stage="finalized", run_id=RUN_ID, ts=2000)
    body = render_events_jsonl([first, second, third])
    lines = body.split("\n")
    assert [json.loads(line)["ts"] for line in lines] == [1000, 2000, 3000]
    assert [json.loads(line)["type"] for line in lines] == [
        "checkpoint",
        "checkpoint",
        "review_started",
    ]
    for line in lines:
        to_jsonl(json.loads(line))  # every rendered line re-validates
    assert render_events_jsonl([]) == ""


def test_render_rejects_malformed_event():
    with pytest.raises(EventsError):
        render_events_jsonl([{"type": "review_started", "bogus": True}])


# --- S3 keys ---------------------------------------------------------------------------------


def test_s3_key_shape_and_validation():
    key = s3_key(PR_NUMBER, SHA_B, RUN_ID, "events.jsonl")
    assert key == f"runs/{PR_NUMBER}/{SHA_B}/{RUN_ID}/events.jsonl"
    assert ROUTE_RE.match(key) is not None
    assert ROUTE_RE.match(s3_key(PR_NUMBER, SHA_B, RUN_ID, "meta.json")) is not None
    for bad in [
        (PR_NUMBER, "zz", RUN_ID, "events.jsonl"),
        (PR_NUMBER, SHA_B, "01234567-89ab-cdef-0123-456789abcdef", "events.jsonl"),
        (PR_NUMBER, SHA_B, RUN_ID, "meta.json.bak"),
        (PR_NUMBER, SHA_B, RUN_ID, "../escape.json"),
    ]:
        with pytest.raises(ArchiveError):
            s3_key(*bad)


# --- worker wiring fakes ----------------------------------------------------------------------


def envelope_dict(*, sha=SHA_B, guid=GUID_1):
    return {
        "envelope_version": "v1",
        "event_type": "pull_request",
        "action": "opened",
        "repo_full_name": REPO,
        "pr_number": PR_NUMBER,
        "head_sha": sha,
        "base_sha": BASE_SHA,
        "sender": "octo-user",
        "delivery_guid": guid,
    }


def file_entry(name="src/main.py"):
    return {
        "filename": name,
        "additions": 5,
        "deletions": 2,
        "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n",
    }


def completion_body(content=REVIEW_BODY):
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode()


class FakeSSM:
    def __init__(self, values):
        self.values = dict(values)

    def get_parameters(self, Names, WithDecryption=False):  # noqa: N803 (boto3 shape)
        params = [
            {"Name": name, "Value": self.values[name]} for name in Names if name in self.values
        ]
        invalid = [name for name in Names if name not in self.values]
        return {"Parameters": params, "InvalidParameters": invalid}


class FakeHttpResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.headers = {}


class FakeDiffTransport:
    def __init__(self, *, meta, files=None):
        self._meta = list(meta)
        self._files = [file_entry()] if files is None else files

    def __call__(self, url, headers):
        if "/files" in url:
            return FakeHttpResponse(200, json.dumps(self._files).encode())
        entry = self._meta.pop(0) if len(self._meta) > 1 else self._meta[0]
        status, sha = entry
        if status != 200:
            return FakeHttpResponse(status, b"{}")
        return FakeHttpResponse(200, json.dumps({"head": {"sha": sha}}).encode())


class FakeLLMResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeLLMSocket:
    def settimeout(self, seconds):
        pass


class FakeLLMConnection:
    def __init__(self, script):
        self._script = list(script)
        self.sock = FakeLLMSocket()

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        pass

    def getresponse(self):
        if not self._script:
            return FakeLLMResponse(200, completion_body())
        action = self._script.pop(0)
        if action[0] == "raise":
            raise action[1]
        return FakeLLMResponse(action[1], action[2])

    def close(self):
        pass


class FakeGitHub:
    def __init__(self, *, post_id=POST_ID):
        self._post_id = post_id
        self.calls = []

    def __call__(self, method, url, headers, body):
        call = {"method": method, "url": url}
        try:
            call["body"] = json.loads(body) if body else None
        except ValueError:
            call["body"] = None
        self.calls.append(call)
        if method == "POST":
            return 201, json.dumps({"id": self._post_id}).encode()
        if method == "GET":
            return 200, b"[]"
        if method == "DELETE":
            return 204, b""
        comment_id = int(url.rsplit("/", 1)[-1])
        return 200, json.dumps({"id": comment_id}).encode()


class FakeS3:
    """Recording `put_object` double (no network): records every call."""

    def __init__(self, log=None):
        self.calls = []
        self.log = log if log is not None else []

    def put_object(self, *, Bucket, Key, Body):
        self.calls.append({"Bucket": Bucket, "Key": Key, "Body": Body})
        self.log.append(("s3", Key))
        return {}


def make_provider():
    return ConfigProvider(
        FakeSSM(dict(SSM_VALUES)).get_parameters,
        allowed_endpoint_hosts=("llm.example.test",),
    )


def drive_record(
    *,
    table,
    s3,
    llm_script=None,
    events,
    clock_values=None,
    multi_agent=False,
    fanout_behavior=None,
    monkeypatch=None,
):
    """Drive one record through `_process_record` with archive wiring on."""
    if multi_agent:
        monkeypatch.setenv("MULTI_AGENT", "1")

        class _Stub:
            def __init__(self):
                self.calls = []

            def __call__(
                self,
                diff_result,
                residuals,
                cfg,
                context,
                *,
                run_id,
                api_key,
                model,
                endpoint,
                events,
                specialist_templates,
                verifier_template,
                synth_template,
                file_lengths=None,
                review_fn=None,
                allowed_hosts=None,
            ):
                self.calls.append(run_id)
                if isinstance(fanout_behavior, BaseException):
                    raise fanout_behavior
                return fanout_behavior

        stub = _Stub()
        monkeypatch.setattr(worker_handler, "run_fanout", stub)
    ticks = list(clock_values) if clock_values else [NOW]

    def _clock():
        return ticks.pop(0) if len(ticks) > 1 else ticks[0]

    status = _process_record(
        {"body": json.dumps(envelope_dict())},
        table=table,
        provider=make_provider(),
        clock=_clock,
        diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
        llm_factory=lambda host, port, *, timeout: FakeLLMConnection(llm_script or []),
        github_transport=FakeGitHub(),
        sink=lambda line: None,
        system_prompt="SYSTEM-PROMPT",
        remaining_time_ms=lambda: 900_000,
        events=events,
        s3=s3,
        archive_bucket=BUCKET,
    )
    return status


# --- worker wiring: S3 puts after finalize -----------------------------------------------------


def test_published_run_archives_two_puts_and_index_row(monkeypatch):
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    status = drive_record(table=table, s3=s3, events=events, monkeypatch=monkeypatch)
    assert status == "published"
    assert [(c["Bucket"], c["Key"]) for c in s3.calls] == [
        (BUCKET, s3.calls[0]["Key"]),
        (BUCKET, s3.calls[1]["Key"]),
    ]
    by_key = {c["Key"]: c["Body"] for c in s3.calls}
    events_key = next(k for k in by_key if k.endswith("/events.jsonl"))
    meta_key = next(k for k in by_key if k.endswith("/meta.json"))
    assert ROUTE_RE.match(events_key) is not None
    assert ROUTE_RE.match(meta_key) is not None
    assert f"/{SHA_B}/" in events_key
    run_id = events_key.split("/")[-2]
    assert re.fullmatch(r"[0-9a-f]{32}", run_id) is not None
    assert meta_key.split("/")[-2] == run_id
    meta = json.loads(by_key[meta_key].decode("utf-8"))
    assert meta["run_id"] == run_id
    assert (meta["pipeline"], meta["status"]) == ("single_pass", "published")
    assert meta["pr"] == PR_NUMBER and meta["sha"] == SHA_B
    lines = by_key[events_key].decode("utf-8").split("\n")
    assert [json.loads(line)["ts"] for line in lines] == sorted(
        json.loads(line)["ts"] for line in lines
    )
    row = table.get_item(f"archive:{run_id}")
    assert row is not None
    assert row["pr_number"] == PR_NUMBER
    assert row["status"] == "published"
    assert row["pipeline"] == "single_pass"
    assert row["sha"] == SHA_B
    assert row["archive_s3_key"] == events_key


def test_puts_land_after_finalize_never_in_callback(monkeypatch):
    """The review callback returns content (publish happens); the archive
    puts land strictly after the finalize table write on one timeline."""
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    drive_record(table=table, s3=s3, events=events, monkeypatch=monkeypatch)
    finalize_at = next(
        i
        for i, e in enumerate(shared)
        if e[0] == "update" and "claim_owner = :owner" in e[1] and "comment_id" not in e[1]
    )
    puts_at = [i for i, e in enumerate(shared) if e[0] == "s3"]
    assert len(puts_at) == 2
    assert all(i > finalize_at for i in puts_at)


def test_degraded_run_archives_degraded_single_pass(monkeypatch):
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    status = drive_record(
        table=table,
        s3=s3,
        events=events,
        monkeypatch=monkeypatch,
        multi_agent=True,
        fanout_behavior=FanoutDegraded("insufficient_budget", "wave"),
    )
    assert status == "published"
    meta_key = next(k for k in (c["Key"] for c in s3.calls) if k.endswith("/meta.json"))
    meta = json.loads(next(c["Body"] for c in s3.calls if c["Key"] == meta_key).decode())
    assert (meta["pipeline"], meta["status"]) == ("multi_agent", "degraded_single_pass")
    run_id = meta["run_id"]
    assert table.get_item(f"archive:{run_id}")["status"] == "degraded_single_pass"


def test_failed_run_archives_failed(monkeypatch):
    table = InMemoryTable()
    s3 = FakeS3()
    events: list = []
    status = drive_record(
        table=table,
        s3=s3,
        events=events,
        monkeypatch=monkeypatch,
        llm_script=[("response", 200, completion_body("plain text"))],
    )
    assert status == "discarded_error"
    meta_key = next(k for k in (c["Key"] for c in s3.calls) if k.endswith("/meta.json"))
    meta = json.loads(next(c["Body"] for c in s3.calls if c["Key"] == meta_key).decode())
    assert (meta["pipeline"], meta["status"]) == ("single_pass", "failed")
    assert table.get_item(f"archive:{meta['run_id']}")["status"] == "failed"


def test_index_write_failure_never_fails_review(monkeypatch):
    """Best-effort index row: a failing table write still completes the
    review with the comment published."""

    class _FailingTable(InMemoryTable):
        def update_item(self, **kwargs):
            update = kwargs.get("UpdateExpression", "")
            if "archive_s3_key" in update:
                raise RuntimeError("index write exploded")
            return super().update_item(**kwargs)

    table = _FailingTable()
    s3 = FakeS3()
    events: list = []
    status = drive_record(table=table, s3=s3, events=events, monkeypatch=monkeypatch)
    assert status == "published"
    assert len(s3.calls) == 2


def test_index_item_shape():
    item = build_index_item(
        run_id=RUN_ID,
        pr=PR_NUMBER,
        sha=SHA_B,
        pipeline="single_pass",
        status="published",
        started_ts=NOW * 1000,
        archive_s3_key=f"runs/{PR_NUMBER}/{SHA_B}/{RUN_ID}/events.jsonl",
        archive_written_at=NOW * 1000 + 9000,
        findings_n=3,
    )
    assert item["pk"] == f"archive:{RUN_ID}"
    assert item["pr_number"] == PR_NUMBER
    # GSI sort key is String (HLD §7/T031): the index row carries ISO-8601
    # UTC; meta.json keeps epoch-ms INT (T028).
    assert item["started_ts"] == (
        datetime.fromtimestamp(NOW, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    assert item["status"] == "published"
    assert item["findings_n"] == 3

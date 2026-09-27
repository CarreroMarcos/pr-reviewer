"""SPR-119 T035: Phase-0 shadow contract tests (HLD-004 §8 Phase 0).

Flag precedence: `MULTI_AGENT=1` ignores `MULTI_AGENT_PHASE0` entirely
(no shadow); shadow iff `MULTI_AGENT=0 AND MULTI_AGENT_PHASE0=1`;
both 0 = legacy (events exactly `review_started` / `checkpoint` /
`review_published` — nothing else).

Ordering: the single-pass publishes FIRST with unchanged latency; ONE
correctness specialist runs inline AFTER publish via the T015 wave
primitive (pool at FANOUT_CONCURRENCY, `specialties=("correctness",)`)
iff remaining ≥ `WAVE_WAIT_FOR_S + BUDGET_MARGIN_S`, else a
`review_skipped {reason: "phase0_no_budget"}` EVENT (no index row).
Shadow never blocks publish (POST precedes every shadow leg call);
never shares the 401-refresh budget (SSM re-fetch count stays 1 even
when the leg answers 401); is NOT lease-covered (release precedes
publish precedes shadow — the helper takes no table by construction);
archive is written AFTER shadow completes on both the run and skip
paths; `pipeline = "phase0_shadow"` rows describe the REVIEW outcome.

Single-specialty wave semantics (load-bearing): one specialist can
never satisfy the ≥2-survivor rule, so `run_wave` raises
`FanoutDegraded` even on a successful leg — the wave's own `agent_*`
events are the shadow telemetry and the helper swallows the exception.
A shadow run therefore returns True with `agent_completed` present; a
failed leg returns True with `agent_failed` present. Neither ever fails
the (already published) review.

RED state: `worker_handler` has no shadow-wiring symbols — collection
errors on import.
"""

import json
import logging

from dynamodb_stub import InMemoryTable

import worker_handler
from common.diff import DiffFile, DiffResult
from common.llm import LlmError, ReviewResult
from worker_handler import (
    _Credentials,
    _make_review,
    _process_record,
    _run_phase0_shadow,
)

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
SHA_B = "bb" * 20
BASE_SHA = "00" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
NOW = 1_750_000_000
POST_ID = 987654
ENDPOINT = "https://llm.example.test/v1/chat/completions"
RUN_ID = "0123456789abcdef0123456789abcdef"
BUCKET = "test-archive-bucket"

REVIEW_BODY = (
    "## Summary\nAdds input validation.\n\n"
    "## Findings\n- [HIGH] `src/main.py:12` — Missing bound. Fix: add a check.\n\n"
    "## Risk Notes\nNone.\n"
)
REVIEW_TEXT = REVIEW_BODY.strip()

SSM_VALUES = {
    "/pr-reviewer/github-token": "github-token-value",  # noqa: S105 (fake fixture)
    "/pr-reviewer/glm-api-key": "glm-key-value",  # noqa: S105 (fake fixture)
    "/pr-reviewer/glm-model": "glm-5.3-flash",
    "/pr-reviewer/glm-endpoint": ENDPOINT,
}

VALID_FINDING = {
    "file_path": "src/main.py",
    "line_start": 12,
    "line_end": 14,
    "title": "Missing bound check",
    "description": "d",
    "suggested_fix": "f",
    "severity": "HIGH",
    "category": "correctness",
}


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


def leg_result(findings=None, behavior=None):
    if isinstance(behavior, BaseException):
        raise behavior
    return ReviewResult(
        content=json.dumps({"findings": findings if findings is not None else [VALID_FINDING]}),
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        reasoning_content="trace-correctness",
    )


class FakeSSM:
    def __init__(self, values):
        self.values = dict(values)
        self.calls = []

    def get_parameters(self, Names, WithDecryption=False):  # noqa: N803 (boto3 shape)
        self.calls.append(list(Names))
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


class FakeLLMSocket:
    def settimeout(self, seconds):
        pass


class FakeLLMResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


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
    def __init__(self, log, *, post_id=POST_ID, on_write=None):
        self._log = log
        self._post_id = post_id
        self._on_write = on_write
        self.calls = []

    def __call__(self, method, url, headers, body):
        call = {"method": method, "url": url}
        try:
            call["body"] = json.loads(body) if body else None
        except ValueError:
            call["body"] = None
        self.calls.append(call)
        self._log.append(("github", method))
        if self._on_write is not None:
            self._on_write(call)
        if method == "POST":
            return 201, json.dumps({"id": self._post_id}).encode()
        if method == "GET":
            return 200, b"[]"
        if method == "DELETE":
            return 204, b""
        comment_id = int(url.rsplit("/", 1)[-1])
        return 200, json.dumps({"id": comment_id}).encode()


class FakeS3:
    def __init__(self, log):
        self._log = log
        self.calls = []

    def put_object(self, *, Bucket, Key, Body):
        self.calls.append({"Bucket": Bucket, "Key": Key, "Body": Body})
        self._log.append(("s3", Key))
        return {}


class FanoutStub:
    """`run_fanout` double for precedence rows (single-pass content)."""

    def __init__(self, behavior):
        self.behavior = behavior
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
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        return self.behavior


def make_provider():
    from common.config import ConfigProvider

    ssm = FakeSSM(dict(SSM_VALUES))
    provider = ConfigProvider(ssm.get_parameters, allowed_endpoint_hosts=("llm.example.test",))
    return provider, ssm


def shadow_leg(log, *, behavior=None):
    """Production-faithful `review_diff` double: required kwargs, no
    defaults (Gate-11); records every leg call on the shared timeline."""

    def _call(
        *,
        api_key,
        model,
        endpoint,
        system_prompt,
        diff_text,
        thinking_enabled=False,
        reasoning_effort="low",
        read_timeout_s=None,
        allowed_hosts=None,
    ):
        log.append(("shadow-leg",))
        return leg_result(behavior=behavior)

    return _call


def drive(
    *,
    table,
    s3,
    events,
    log,
    monkeypatch,
    multi_agent="0",
    phase0="0",
    remaining=900_000,
    llm_script=None,
    fanout_behavior=REVIEW_BODY,
    shadow_behavior=None,
    caps=None,
    github_on_write=None,
):
    monkeypatch.setenv("MULTI_AGENT", multi_agent)
    monkeypatch.setenv("MULTI_AGENT_PHASE0", phase0)
    stub = FanoutStub(fanout_behavior)
    monkeypatch.setattr(worker_handler, "run_fanout", stub)
    provider, ssm = make_provider()
    github = FakeGitHub(log, on_write=github_on_write)
    if caps is not None:
        caps["github"] = github
    status = _process_record(
        {"body": json.dumps(envelope_dict())},
        table=table,
        provider=provider,
        clock=lambda: NOW,
        diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
        llm_factory=lambda host, port, *, timeout: FakeLLMConnection(llm_script or []),
        github_transport=github,
        sink=lambda line: None,
        system_prompt="SYSTEM-PROMPT",
        remaining_time_ms=lambda: remaining,
        events=events,
        s3=s3,
        archive_bucket=BUCKET,
        shadow_review_fn=shadow_leg(log, behavior=shadow_behavior),
    )
    return status, stub, ssm


def events_of_type(events, type_name):
    return [e for e in events if e["type"] == type_name]


def make_diff():
    return DiffResult(
        head_sha=SHA_B,
        files=(
            DiffFile(filename="src/main.py", additions=1, deletions=0, patch="@@ -1 +1 @@\n+x"),
        ),
        total_additions=1,
        total_deletions=0,
        total_bytes=10,
        truncated=False,
        lockfile_summary="lockfiles: no changes",
    )


def live_cfg():
    from common.config import multi_agent_config

    return multi_agent_config()


# --- flag precedence ---------------------------------------------------------------


def test_ma1_ignores_phase0_entirely(monkeypatch):
    """`MULTI_AGENT=1` + `MULTI_AGENT_PHASE0=1`: fan-out attempted, the
    wave primitive never invoked directly (no shadow)."""
    boom_calls = []

    def _boom(*args, **kwargs):
        boom_calls.append(kwargs)
        raise AssertionError("shadow wave must not run under MULTI_AGENT=1")

    monkeypatch.setattr(worker_handler, "run_wave", _boom)
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    log: list = []
    github = FakeGitHub(log)
    provider, _ = make_provider()
    monkeypatch.setenv("MULTI_AGENT", "1")
    monkeypatch.setenv("MULTI_AGENT_PHASE0", "1")
    stub = FanoutStub(REVIEW_BODY)
    monkeypatch.setattr(worker_handler, "run_fanout", stub)
    status = _process_record(
        {"body": json.dumps(envelope_dict())},
        table=table,
        provider=provider,
        clock=lambda: NOW,
        diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
        llm_factory=lambda host, port, *, timeout: FakeLLMConnection([]),
        github_transport=github,
        sink=lambda line: None,
        system_prompt="SYSTEM-PROMPT",
        remaining_time_ms=lambda: 900_000,
        events=events,
        s3=s3,
        archive_bucket=BUCKET,
        shadow_review_fn=shadow_leg(log),
    )
    assert status == "published"
    assert len(stub.calls) == 1
    assert boom_calls == []


def test_ma1_without_phase0_no_shadow(monkeypatch):
    monkeypatch.setattr(
        worker_handler,
        "run_wave",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no shadow wave expected")),
    )
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    status, stub, _ = drive(
        table=table,
        s3=s3,
        events=events,
        log=[],
        monkeypatch=monkeypatch,
        multi_agent="1",
        phase0="0",
    )
    assert status == "published"
    assert len(stub.calls) == 1


def test_legacy_both_zero_event_purity(monkeypatch):
    monkeypatch.setattr(
        worker_handler,
        "run_wave",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no wave expected on legacy path")),
    )
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    status, stub, _ = drive(table=table, s3=s3, events=events, log=[], monkeypatch=monkeypatch)
    assert status == "published"
    assert stub.calls == []
    types = {e["type"] for e in events}
    assert types <= {"review_started", "checkpoint", "review_published"}
    assert {"review_started", "review_published"} <= types
    assert events_of_type(events, "review_skipped") == []


# --- shadow runs after publish --------------------------------------------------------


def test_shadow_runs_after_publish_with_telemetry(monkeypatch, caplog):
    """Ample budget: POST carries the unchanged single-pass content;
    the shadow leg runs strictly after publish; its agent telemetry
    lands in events; archive follows the shadow. The expected
    single-specialty degrade logs `shadow_degraded` (witnessed by the
    `agent_completed` event), never `shadow_failed`."""
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    log: list = []
    caps: dict = {}
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        status, stub, _ = drive(
            table=table,
            s3=s3,
            events=events,
            log=log,
            monkeypatch=monkeypatch,
            multi_agent="0",
            phase0="1",
            caps=caps,
        )
    assert status == "published"
    assert stub.calls == []
    posted = next(c for c in caps["github"].calls if c["method"] == "POST")
    assert REVIEW_TEXT in posted["body"]["body"]  # unchanged single-pass content
    post_at = next(i for i, e in enumerate(log) if e == ("github", "POST"))
    leg_at = next(i for i, e in enumerate(log) if e == ("shadow-leg",))
    assert post_at < leg_at  # publish FIRST — shadow never blocks it
    completed = events_of_type(events, "agent_completed")
    assert len(completed) == 1
    assert completed[0]["specialty"] == "correctness"
    assert completed[0]["findings_n"] == 1
    assert "shadow_degraded" in caplog.messages
    assert "shadow_failed" not in caplog.messages


def test_shadow_archive_pipeline_and_row(monkeypatch):
    """`pipeline = "phase0_shadow"` rows describe the REVIEW outcome;
    the index row lands with the shadow pipeline."""
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    status, _, _ = drive(
        table=table,
        s3=s3,
        events=events,
        log=[],
        monkeypatch=monkeypatch,
        multi_agent="0",
        phase0="1",
    )
    assert status == "published"
    meta_key = next(k for k in (c["Key"] for c in s3.calls) if k.endswith("/meta.json"))
    meta = json.loads(next(c["Body"] for c in s3.calls if c["Key"] == meta_key).decode())
    assert (meta["pipeline"], meta["status"]) == ("phase0_shadow", "published")
    run_id = meta["run_id"]
    row = table.get_item(f"archive:{run_id}")
    assert row is not None and row["pipeline"] == "phase0_shadow"
    puts_at = [i for i, e in enumerate(shared) if e[0] == "s3"]
    leg_logged = any(True for _ in events_of_type(events, "agent_completed"))
    assert leg_logged and len(puts_at) == 2


def test_conflict_publishes_and_participates_in_shadow(monkeypatch):
    """Bot R2 Fix 2: `PUBLISHED_FINALIZE_CONFLICT` means the review
    published (GitHub comment exists) — the fence loss is delivery
    bookkeeping. Shadow still runs, archive row lands with
    status="published" and the shadow pipeline literal."""
    landed: dict = {}

    def _takeover(call):
        if not landed and call["method"] == "POST":
            landed["moved"] = True
            row = table.items[PK]
            row["claim_owner"] = "22222222-2222-4222-8222-222222222222"

    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    log: list = []
    status, _, _ = drive(
        table=table,
        s3=s3,
        events=events,
        log=log,
        monkeypatch=monkeypatch,
        multi_agent="0",
        phase0="1",
        github_on_write=_takeover,
    )
    assert status == "published_finalize_conflict"
    assert ("shadow-leg",) in log  # shadow participated despite the fence loss
    meta_key = next(k for k in (c["Key"] for c in s3.calls) if k.endswith("/meta.json"))
    meta = json.loads(next(c["Body"] for c in s3.calls if c["Key"] == meta_key).decode())
    assert (meta["pipeline"], meta["status"]) == ("phase0_shadow", "published")
    assert table.get_item(f"archive:{meta['run_id']}") is not None


def test_shadow_budget_boundary_runs(monkeypatch):
    """remaining == WAVE_WAIT_FOR_S + BUDGET_MARGIN_S (360_000ms) still
    runs the shadow (>=, fail-closed only below)."""
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    log: list = []
    status, _, _ = drive(
        table=table,
        s3=s3,
        events=events,
        log=log,
        monkeypatch=monkeypatch,
        multi_agent="0",
        phase0="1",
        remaining=360_000,
    )
    assert status == "published"
    assert ("shadow-leg",) in log


def test_shadow_skip_no_budget(monkeypatch, caplog):
    """Below the gate: `review_skipped {phase0_no_budget}` EVENT (plus a
    `shadow_no_budget` warn — both skip paths warn), no wave call,
    archive still written, NO index row."""
    monkeypatch.setattr(
        worker_handler,
        "run_wave",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("budget SKIP must not invoke the wave")
        ),
    )
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        status, _, _ = drive(
            table=table,
            s3=s3,
            events=events,
            log=[],
            monkeypatch=monkeypatch,
            multi_agent="0",
            phase0="1",
            remaining=100_000,
        )
    assert status == "published"
    skipped = events_of_type(events, "review_skipped")
    assert [(e["reason"], e["pr"], e["sha"]) for e in skipped] == [
        ("phase0_no_budget", PR_NUMBER, SHA_B)
    ]
    assert "shadow_no_budget" in caplog.messages
    assert len(s3.calls) == 2  # archive follows the skip path too
    run_id = events[0]["run_id"]
    assert table.get_item(f"archive:{run_id}") is None


def test_shadow_missing_stash_no_diff(monkeypatch, caplog):
    """Bot R1 Fix 3 (adapted — see below): shadow-eligible flags but no
    stashed diff (the closure did not stash — older caller shape) → the
    skip is LOUD, never silent.

    Deviation from the bot's literal prescription (`review_skipped`
    with reason `phase0_no_diff`): HLD §6 closes the skipped-reason set
    to `{"empty_diff", "phase0_no_budget"}` (`events.py:37`) and neither
    is truthful here, so the branch warns (`shadow_no_diff`) instead of
    emitting a false-categorized event. The run archives as the plain
    single-pass publish it was (2 puts + index row)."""
    monkeypatch.setattr(
        worker_handler,
        "run_wave",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no diff means no wave")),
    )
    real_make_review = _make_review

    def _no_stash(**kwargs):
        kwargs.pop("shadow_stash", None)
        return real_make_review(**kwargs)

    monkeypatch.setattr(worker_handler, "_make_review", _no_stash)
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        status, _, _ = drive(
            table=table,
            s3=s3,
            events=events,
            log=[],
            monkeypatch=monkeypatch,
            multi_agent="0",
            phase0="1",
        )
    assert status == "published"
    assert events_of_type(events, "review_skipped") == []
    assert "shadow_skipped" in caplog.messages
    assert "shadow_no_diff" in [r.status for r in caplog.records if r.name == "worker_handler"]
    assert len(s3.calls) == 2
    run_id = events[0]["run_id"]
    row = table.get_item(f"archive:{run_id}")
    assert row is not None and row["pipeline"] == "single_pass"


# --- shadow failure modes ---------------------------------------------------------------


def test_shadow_leg_failure_never_blocks(monkeypatch, caplog):
    """A failed leg still publishes, still archives, and leaves its
    agent_failed telemetry in events — logged `shadow_failed` (no
    `agent_completed` witness, so not the expected single-specialty
    degrade)."""
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        status, _, _ = drive(
            table=table,
            s3=s3,
            events=events,
            log=[],
            monkeypatch=monkeypatch,
            multi_agent="0",
            phase0="1",
            shadow_behavior=LlmError("timeout"),
        )
    assert status == "published"
    assert len(events_of_type(events, "agent_failed")) == 1
    assert len(s3.calls) == 2
    run_id = events[0]["run_id"]
    assert table.get_item(f"archive:{run_id}") is not None
    assert "shadow_failed" in caplog.messages
    assert "shadow_degraded" not in caplog.messages


def test_witness_scoped_to_shadow_leg_emissions(monkeypatch, caplog):
    """Bot R2 Fix 1: a pre-existing correctness `agent_completed` (prior
    degraded fan-out) must not witness for the shadow leg — a
    produced-nothing shadow logs `shadow_failed`, never
    `shadow_degraded`."""
    from common.events import agent_completed

    seed = agent_completed(
        specialty="correctness",
        findings_n=1,
        latency_ms=1,
        tokens_in=1,
        tokens_out=1,
        findings=[],
        run_id=RUN_ID,
        ts=1,
    )
    events = [seed]
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        assert (
            _run_phase0_shadow(
                diff_result=make_diff(),
                ma_cfg=live_cfg(),
                api_key="k",
                model="m",
                endpoint="https://llm.example.test/v1/chat/completions",
                allowed_hosts=frozenset({"llm.example.test"}),
                run_id=RUN_ID,
                events=events,
                remaining_ms=900_000,
                residuals=[],
                review_fn=shadow_leg([], behavior=LlmError("timeout")),
            )
            is True
        )
    assert "shadow_failed" in caplog.messages
    assert "shadow_degraded" not in caplog.messages


def test_helper_generic_exception_never_raises(caplog):
    """Bot R1 Fix 1: past the budget gate the helper is a never-raises
    boundary — even a non-`FanoutDegraded` fault (here a `None`
    diff_result blowing up the renderer) warns `shadow_failed` and
    still returns True; the published review is untouched."""
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        assert (
            _run_phase0_shadow(
                diff_result=None,
                ma_cfg=live_cfg(),
                api_key="k",
                model="m",
                endpoint="https://llm.example.test/v1/chat/completions",
                allowed_hosts=frozenset({"llm.example.test"}),
                run_id=RUN_ID,
                events=[],
                remaining_ms=900_000,
                residuals=[],
                review_fn=shadow_leg([]),
            )
            is True
        )
    assert "shadow_failed" in caplog.messages


def test_shadow_never_shares_401_budget(monkeypatch, caplog):
    """Even a 401 leg never triggers the shared single-refresh: exactly
    one SSM read (initial hydrate) for the whole record — pinned through
    the production call site by making any `refresh_once` call explode
    (the helper only ever receives `creds.current()` snapshot scalars,
    never the holder, so there is no path that could call it)."""

    def _boom(self):
        raise AssertionError("shadow must never touch the 401-refresh budget")

    monkeypatch.setattr(_Credentials, "refresh_once", _boom)
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        status, _, ssm = drive(
            table=table,
            s3=s3,
            events=events,
            log=[],
            monkeypatch=monkeypatch,
            multi_agent="0",
            phase0="1",
            shadow_behavior=LlmError("http_401"),
        )
    assert status == "published"
    assert len(ssm.calls) == 1
    assert len(events_of_type(events, "agent_failed")) == 1
    assert "shadow_failed" in caplog.messages


def test_failed_run_no_shadow(monkeypatch):
    """No publish → no shadow: refused content archives `failed` with no
    wave invocation."""
    monkeypatch.setattr(
        worker_handler,
        "run_wave",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no shadow without publish")),
    )
    shared: list = []
    table = InMemoryTable(log=shared)
    s3 = FakeS3(log=shared)
    events: list = []
    status, _, _ = drive(
        table=table,
        s3=s3,
        events=events,
        log=[],
        monkeypatch=monkeypatch,
        multi_agent="0",
        phase0="1",
        llm_script=[("response", 200, completion_body("plain text"))],
    )
    assert status == "discarded_error"


# --- helper-direct rows: budget math ---------------------------------------------------------


def test_helper_none_remaining_skips(monkeypatch):
    """Unreadable clock fails closed: False, wave never invoked."""
    monkeypatch.setattr(
        worker_handler,
        "run_wave",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("wave must not run without budget")),
    )
    assert (
        _run_phase0_shadow(
            diff_result=make_diff(),
            ma_cfg=live_cfg(),
            api_key="k",
            model="m",
            endpoint="https://llm.example.test/v1/chat/completions",
            allowed_hosts=frozenset({"llm.example.test"}),
            run_id=RUN_ID,
            events=[],
            remaining_ms=None,
            residuals=[],
            review_fn=shadow_leg([]),
        )
        is False
    )


def test_helper_exact_boundary_runs(monkeypatch):
    """remaining == gate total proceeds; the wave runs with exactly one
    `correctness` specialty."""
    seen = {}

    def _spy(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(worker_handler, "run_wave", _spy)
    assert (
        _run_phase0_shadow(
            diff_result=make_diff(),
            ma_cfg=live_cfg(),
            api_key="k",
            model="m",
            endpoint="https://llm.example.test/v1/chat/completions",
            allowed_hosts=frozenset({"llm.example.test"}),
            run_id=RUN_ID,
            events=[],
            remaining_ms=360_000,
            residuals=[],
            review_fn=shadow_leg([]),
        )
        is True
    )
    assert seen["specialties"] == ("correctness",)
    assert list(seen["system_prompts"]) == ["correctness"]


def test_helper_signature_isolated():
    """Bot R2 Fix 2 (401-test pin): isolation-by-construction — the
    helper's signature carries snapshot scalars (`api_key`/`model`/
    `endpoint`) and never a holder-shaped parameter, so there is no
    code path that could reach the 401-refresh budget. Blacklist
    substrings (not a whitelist): a future signature change adding such
    a parameter fails here deliberately, forcing an explicit isolation
    re-review rather than silent coupling."""
    import inspect

    names = set(inspect.signature(_run_phase0_shadow).parameters)
    coupled = {
        name
        for name in names
        if any(part in name for part in ("creds", "refresh", "table", "ssm", "provider"))
    }
    assert coupled == set(), f"holder-coupled parameters: {sorted(coupled)}"

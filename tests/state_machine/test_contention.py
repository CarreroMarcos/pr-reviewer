"""SPR-111 T026: mutex contention-path contract tests (HLD-004 D9).

When another worker holds the mutex, the arriving worker (contender)
does NOT defer and does NOT touch message visibility — it runs
single-pass inline emitting `concurrency_single_pass {reason:
"mutex_held"}` and completes normally.

CONTENDER VIABILITY CHECK (own predicate, distinct from elapsed-budget
gate 4): clamp `remaining − BUDGET_MARGIN_S − ~90s` vs the VIABILITY
FLOOR (provisionally the measured ~240s pre-Phase-0) → below floor:
SKIP with `concurrency_single_pass {reason: "mutex_held_no_budget"}`,
transient classification (`LlmError("timeout")` — the call that cannot
be afforded; retryable → the existing re-raise/notice path and queue
redelivery); at/above floor: one single-pass with the clamped value
FORWARDED as `read_timeout_s` (computed once, observed on the socket).

Release ordering (Gate-14 pin, lands here): AFTER the last
lease-covered LLM call, BEFORE claim/fence/publish/finalize. Phase-0
shadow is NOT lease-covered (no shadow surface exists yet — T035/T036;
release-inside-review trivially precedes any post-publish work).
Publication is NOT mutex-protected: the contender publishes holding no
lease (loser → `PUBLISHED_FINALIZE_CONFLICT` is existing reconcile
machinery, cited not re-tested).

Holder/refresh (Gate-15(c)): at 50%-TTL elapsed the holder refreshes
before issuing more LLM work (pre-fallback point); a lost refresh
stops all LLM work and re-raises without ever re-asserting.

MULTI_AGENT interplay (HLD D9): the mutex serializes WORKERS; the
fan-out/single-pass choice stays orthogonal — holder proceeds to the
flag's path, contender always runs single-pass inline.

Doubles discipline (Gate-11): the REAL `common.mutex` primitives run
against the `test_mutex.MutexTable` double (exact expression dispatch);
transports mirror the `test_worker`/`test_fanout_containment` port
shapes; the `run_fanout` stub mirrors the sequencer signature with no
defaults on required params.

RED state: `worker_handler` has no contention symbols — collection
errors on import.
"""

import json
from types import SimpleNamespace

import pytest
from dynamodb_stub import InMemoryTable
from test_mutex import ACQUIRE_UPDATE, MutexTable

import worker_handler
from common.config import MultiAgentConfig
from common.envelope import validate_envelope
from common.fanout import FanoutDegraded, single_pass_budget_ok
from common.llm import LlmError
from common.mutex import MUTEX_PK
from worker_handler import (
    CONTENDER_FIXED_OVERHEAD_S,
    CONTENDER_READ_FLOOR_S,
    _contender_read_timeout_s,
    _Credentials,
    _make_review,
    _process_record,
    handler,
    is_retryable,
)

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
SHA_B = "bb" * 20
BASE_SHA = "00" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
GUID_2 = "22222222-2222-4222-8222-222222222222"
NOW = 1_750_000_000
POST_ID = 987654
ENDPOINT = "https://llm.example.test/v1/chat/completions"
RUN_ID = "0123456789abcdef0123456789abcdef"

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


def make_cfg(margin=60):
    return MultiAgentConfig(
        multi_agent=1,
        multi_agent_phase0=0,
        fanout_concurrency=3,
        mutex_lease_ttl_s=900,
        reasoning_max_chars=4000,
        reasoning_effort="low",
        wave_wait_for_s=300,
        verifier_wait_for_s=240,
        synthesizer_wait_for_s=180,
        single_pass_wait_for_s=240,
        socket_read_timeout_s=240,
        budget_margin_s=margin,
    )


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
    def __init__(self, log, *, meta, files=None):
        self._log = log
        self._meta = list(meta)
        self._files = [file_entry()] if files is None else files

    def __call__(self, url, headers):
        self._log.append(("diff", url))
        if "/files" in url:
            return FakeHttpResponse(200, json.dumps(self._files).encode())
        entry = self._meta.pop(0) if len(self._meta) > 1 else self._meta[0]
        status, sha = entry
        if status != 200:
            return FakeHttpResponse(status, b"{}")
        return FakeHttpResponse(200, json.dumps({"head": {"sha": sha}}).encode())


class FakeLLMSocket:
    def __init__(self, timeouts):
        self._timeouts = timeouts

    def settimeout(self, seconds):
        self._timeouts.append(seconds)


class FakeLLMResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeLLMConnection:
    def __init__(self, log, timeouts, script):
        self._log = log
        self._script = script
        self.sock = FakeLLMSocket(timeouts)
        self.requests = []

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"method": method, "path": path})

    def getresponse(self):
        self._log.append(("llm",))
        if not self._script:
            return FakeLLMResponse(200, completion_body())
        action = self._script.pop(0)
        if action[0] == "raise":
            raise action[1]
        return FakeLLMResponse(action[1], action[2])

    def close(self):
        pass


class FakeGitHub:
    def __init__(self, log, *, post_id=POST_ID):
        self._log = log
        self._post_id = post_id
        self.calls = []

    def __call__(self, method, url, headers, body):
        call = {"method": method, "url": url}
        try:
            call["body"] = json.loads(body) if body else None
        except ValueError:
            call["body"] = None
        self.calls.append(call)
        self._log.append(("github", method))
        if method == "POST":
            return 201, json.dumps({"id": self._post_id}).encode()
        if method == "GET":
            return 200, b"[]"
        if method == "DELETE":
            return 204, b""
        comment_id = int(url.rsplit("/", 1)[-1])
        return 200, json.dumps({"id": comment_id}).encode()


class FakeSQS:
    def __init__(self):
        self.calls = []

    def change_message_visibility(self, **kwargs):
        self.calls.append(kwargs)


class ScriptedClock:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= len(self.values):
            return self.values[self.calls - 1]
        return self.values[-1]


class FanoutStub:
    """`run_fanout` double: required params without defaults (Gate-11);
    optional `on_call` hook observes (and may mutate) per invocation."""

    def __init__(self, behavior, on_call=None):
        self.behavior = behavior
        self.on_call = on_call
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
        self.calls.append({"run_id": run_id, "events": events})
        if self.on_call is not None:
            self.on_call()
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        return self.behavior


def make_provider():
    from common.config import ConfigProvider

    return ConfigProvider(
        FakeSSM(dict(SSM_VALUES)).get_parameters,
        allowed_endpoint_hosts=("llm.example.test",),
    )


def make_closure(
    events,
    table,
    *,
    remaining=900_000,
    clock=None,
    run_id=RUN_ID,
    llm_script=None,
    log=None,
    timeouts=None,
):
    provider = make_provider()
    log = log if log is not None else []
    timeouts = timeouts if timeouts is not None else []
    script = list(llm_script) if llm_script else []

    def factory(host, port, *, timeout):
        return FakeLLMConnection(log, timeouts, script)

    return _make_review(
        envelope=validate_envelope(envelope_dict()),
        creds=_Credentials(provider),
        usage={},
        diff_transport=FakeDiffTransport(log, meta=[(200, SHA_B)]),
        llm_factory=factory,
        system_prompt="SYSTEM-PROMPT",
        allowed_hosts=provider.allowed_hosts,
        clock=clock if clock is not None else (lambda: NOW),
        github_transport=FakeGitHub(log),
        remaining_time_ms=lambda: remaining,
        events=events,
        run_id=run_id,
        table=table,
    )


def hold(table, *, owner=GUID_2, token="tok-holder", lease_until=NOW + 900):  # noqa: S107 (fixture default)
    table.items[MUTEX_PK] = {
        "pk": MUTEX_PK,
        "owner": owner,
        "token": token,
        "lease_until": lease_until,
    }


def events_of_type(events, type_name):
    return [e for e in events if e["type"] == type_name]


@pytest.fixture()
def multi_agent(monkeypatch):
    monkeypatch.setenv("MULTI_AGENT", "1")
    return monkeypatch


@pytest.fixture()
def stubbed_fanout(monkeypatch):
    def install(behavior, on_call=None):
        stub = FanoutStub(behavior, on_call=on_call)
        monkeypatch.setattr(worker_handler, "run_fanout", stub)
        return stub

    return install


# --- viability predicate: own function, own math -------------------------------------------


def test_contender_constants_provisional():
    assert CONTENDER_READ_FLOOR_S == 240
    assert CONTENDER_FIXED_OVERHEAD_S == 90


@pytest.mark.parametrize(
    ("remaining_ms", "expected"),
    [(600_000, 450), (390_000, 240), (389_999, None), (100_000, None)],
)
def test_contender_viability_clamp_and_floor(remaining_ms, expected):
    """`remaining/1000 − BUDGET_MARGIN_S − ~90s` vs the 240s floor —
    boundary inclusive at exactly 240."""
    assert _contender_read_timeout_s(remaining_ms, make_cfg()) == expected


def test_contender_viability_unknown_clock_fails_closed():
    assert _contender_read_timeout_s(None, make_cfg()) is None


def test_contender_predicate_distinct_from_gate4():
    """The two predicates are different functions with different math:
    at remaining=350s gate 4 passes (≥300s) while the contender check
    fails (350−60−90=200 < 240). No shared code path."""
    cfg = make_cfg()
    assert _contender_read_timeout_s(350_000, cfg) is None
    assert single_pass_budget_ok(350_000, cfg) is True


# --- holder path: lease → flag's path → release ----------------------------------------------


def test_holder_runs_fanout_and_releases(multi_agent, stubbed_fanout):
    stub = stubbed_fanout(REVIEW_BODY)
    events, table = [], MutexTable()
    closure = make_closure(events, table)
    content = closure(SHA_B, 0)
    assert REVIEW_TEXT in content
    assert len(stub.calls) == 1
    assert events_of_type(events, "concurrency_single_pass") == []
    assert table.get_item(MUTEX_PK) is None  # released


def test_holder_legacy_path_acquires_and_releases(stubbed_fanout):
    """Mutex orthogonal to the fan-out flag: flag off still leases the
    review and releases after the inline single-pass."""
    stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"))
    events, table = [], MutexTable()
    closure = make_closure(events, table)
    content = closure(SHA_B, 0)
    assert REVIEW_TEXT in content
    assert table.get_item(MUTEX_PK) is None


def test_holder_flag_on_contender_flag_on_interplay(multi_agent, stubbed_fanout):
    """Same flag, different leases: the holder attempts fan-out, the
    contender never does (holds no lease, always single-pass)."""
    stub = stubbed_fanout(REVIEW_BODY)
    holder_events, free_table = [], MutexTable()
    make_closure(holder_events, free_table)(SHA_B, 0)
    assert len(stub.calls) == 1
    held_events, held_table = [], MutexTable()
    hold(held_table)
    make_closure(held_events, held_table)(SHA_B, 0)
    assert len(stub.calls) == 1  # contender never attempted fan-out
    assert len(events_of_type(held_events, "concurrency_single_pass")) == 1


# --- contender viable: single-pass inline, clamped socket ---------------------------------------


def test_contender_viable_runs_single_pass_with_clamped_timeout(multi_agent, stubbed_fanout):
    stub = stubbed_fanout(REVIEW_BODY)
    events, table, timeouts = [], MutexTable(), []
    hold(table)
    closure = make_closure(events, table, timeouts=timeouts)
    content = closure(SHA_B, 0)
    assert REVIEW_TEXT in content
    assert stub.calls == []  # never attempted fan-out
    concurrency = events_of_type(events, "concurrency_single_pass")
    assert [(e["reason"], e["run_id"]) for e in concurrency] == [("mutex_held", RUN_ID)]
    assert events_of_type(events, "degraded_to_single_pass") == []
    assert events_of_type(events, "agent_started") == []
    # 900 − 60 − 90 = 750, computed once, forwarded to the socket.
    assert timeouts and all(t == 750 for t in timeouts)


def test_contender_emits_no_degraded_event(multi_agent, stubbed_fanout):
    """Degraded_to_single_pass belongs to post-FanoutDegraded fallback —
    the contender never attempted fan-out, so only its own event emits."""
    stubbed_fanout(REVIEW_BODY)
    events, table = [], MutexTable()
    hold(table)
    make_closure(events, table)(SHA_B, 0)
    types = {e["type"] for e in events}
    assert "degraded_to_single_pass" not in types
    assert "concurrency_single_pass" in types


# --- contender SKIP: no budget → transient raise --------------------------------------------------


def test_contender_no_budget_skips_with_transient_raise(multi_agent, stubbed_fanout):
    stub = stubbed_fanout(REVIEW_BODY)
    events, table, timeouts = [], MutexTable(), []
    hold(table)
    closure = make_closure(events, table, remaining=100_000, timeouts=timeouts)
    with pytest.raises(LlmError) as exc_info:
        closure(SHA_B, 0)
    assert exc_info.value.error_class == "timeout"
    assert is_retryable(exc_info.value) is True  # transient → redelivery
    skipped = events_of_type(events, "concurrency_single_pass")
    assert [e["reason"] for e in skipped] == ["mutex_held_no_budget"]
    assert stub.calls == []
    assert timeouts == []  # no LLM call ever issued


def test_handler_skip_raises_without_touching_visibility(multi_agent, stubbed_fanout, monkeypatch):
    """End-to-end SKIP: handler raises for redelivery (record NOT
    completed), nothing posted, and `change_message_visibility` never
    called — the contender does not defer and does not touch visibility."""
    monkeypatch.setenv("WORK_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/1/q")
    stubbed_fanout(REVIEW_BODY)
    provider = make_provider()
    github, table, sqs = FakeGitHub([]), InMemoryTable(), FakeSQS()
    hold(table)
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 100_000)
    record = {
        "body": json.dumps(envelope_dict()),
        "messageId": "m1",
        "receiptHandle": "rh-1",
        "attributes": {"ApproximateReceiveCount": "1"},
    }
    with pytest.raises(LlmError):
        handler(
            {"Records": [record]},
            context,
            _table=table,
            _config_provider=provider,
            _now=lambda: NOW,
            _diff_transport=FakeDiffTransport([], meta=[(200, SHA_B)]),
            _llm_factory=lambda host, port, *, timeout: FakeLLMConnection([], [], []),
            _github_transport=github,
            _sink=lambda line: None,
            _system_prompt="SYSTEM-PROMPT",
            _sqs=sqs,
        )
    assert sqs.calls == []
    posted = [c for c in github.calls if c["method"] == "POST"]
    # The only POST is the D2 transient failure notice (existing
    # re-raise/notice path) — no review content is ever published.
    assert posted and all(REVIEW_TEXT not in c["body"]["body"] for c in posted)


# --- release ordering: last LLM < release < claim/fence/publish ------------------------------


def test_release_ordering_after_llm_before_pipeline(multi_agent, stubbed_fanout):
    """Gate-14 pin: release lands AFTER the last lease-covered LLM call
    and BEFORE claim/fence/publish/finalize — on one shared timeline."""
    stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"))
    shared, provider = [], make_provider()
    github = FakeGitHub(shared)
    table = InMemoryTable(log=shared)
    events = []
    status = _process_record(
        {"body": json.dumps(envelope_dict())},
        table=table,
        provider=provider,
        clock=lambda: NOW,
        diff_transport=FakeDiffTransport(shared, meta=[(200, SHA_B)]),
        llm_factory=lambda host, port, *, timeout: FakeLLMConnection(shared, [], []),
        github_transport=github,
        sink=lambda line: None,
        system_prompt="SYSTEM-PROMPT",
        remaining_time_ms=lambda: 900_000,
        events=events,
    )
    assert status == "published"

    def index_of(predicate):
        return next(i for i, entry in enumerate(shared) if predicate(entry))

    llm_last = max(i for i, entry in enumerate(shared) if entry[0] == "llm")
    release_at = index_of(lambda e: e[0] == "delete" and e[1] == "#tok = :token")
    claim_at = index_of(
        lambda e: e[0] == "update" and "claim_until" in e[1] and "comment_id" not in e[1]
    )
    diff_calls = [i for i, e in enumerate(shared) if e[0] == "diff" and "/files" not in e[1]]
    fence_at = next(i for i in diff_calls if i > claim_at)
    publish_at = next(i for i, e in enumerate(shared) if e == ("github", "POST"))
    assert llm_last < release_at < claim_at < fence_at < publish_at


# --- holder refresh: 50%-TTL scheduling + failed-refresh degrade --------------------------------


def test_no_refresh_below_half_ttl(multi_agent, stubbed_fanout):
    """Elapsed 100s < 450s: no refresh issued, fallback proceeds on the
    original lease (900s still covers the single-pass)."""
    stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"))
    events, table = [], MutexTable()
    closure = make_closure(events, table, clock=ScriptedClock([0, 100]))
    content = closure(SHA_B, 0)
    assert REVIEW_TEXT in content
    refreshes = [c for c in table.calls if c.get("UpdateExpression") == "SET lease_until = :until"]
    assert refreshes == []


def test_refresh_success_extends_then_falls_back(multi_agent, stubbed_fanout):
    """Elapsed 500s ≥ 450s with the row intact: refresh extends the lease
    (500+900), then the existing single-pass inline runs."""
    stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"))
    events, table = [], MutexTable()
    closure = make_closure(events, table, clock=ScriptedClock([0, 500]))
    content = closure(SHA_B, 0)
    assert REVIEW_TEXT in content
    refreshes = [c for c in table.calls if c.get("UpdateExpression") == "SET lease_until = :until"]
    assert [c["values"][":until"] for c in refreshes] == [500 + 900]
    assert table.get_item(MUTEX_PK) is None  # released after the fallback


def test_failed_refresh_stops_work_and_never_reasserts(multi_agent, stubbed_fanout):
    """Takeover mid-fan-out: A's refresh reports lost → no fallback LLM
    call, the FanoutDegraded re-raises, and acquire ran exactly once
    (never re-asserted)."""
    table = MutexTable()
    clock = ScriptedClock([0, 500])

    def take_over():
        table.items[MUTEX_PK] = {
            "pk": MUTEX_PK,
            "owner": GUID_2,
            "token": "tok-takeover",
            "lease_until": 500 + 900,
        }

    stub = stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"), on_call=take_over)
    events, timeouts = [], []
    log: list = []
    provider = make_provider()

    def factory(host, port, *, timeout):
        return FakeLLMConnection(log, timeouts, [])

    closure = _make_review(
        envelope=validate_envelope(envelope_dict()),
        creds=_Credentials(provider),
        usage={},
        diff_transport=FakeDiffTransport(log, meta=[(200, SHA_B)]),
        llm_factory=factory,
        system_prompt="SYSTEM-PROMPT",
        allowed_hosts=provider.allowed_hosts,
        clock=clock,
        github_transport=FakeGitHub(log),
        remaining_time_ms=lambda: 900_000,
        events=events,
        run_id=RUN_ID,
        table=table,
    )
    with pytest.raises(FanoutDegraded):
        closure(SHA_B, 0)
    assert timeouts == []  # no fallback LLM work issued
    assert len(events_of_type(events, "degraded_to_single_pass")) == 1
    acquires = [c for c in table.calls if c.get("UpdateExpression") == ACQUIRE_UPDATE]
    assert len(acquires) == 1
    assert stub.calls and len(stub.calls) == 1


# --- publication not mutex-protected ------------------------------------------------------------


def test_contender_publishes_holding_no_lease(multi_agent, stubbed_fanout):
    """Scope pin: the contender publishes with no lease held — zero
    mutex writes on its path (loser → PUBLISHED_FINALIZE_CONFLICT is
    existing reconcile machinery, cited not re-tested)."""
    stubbed_fanout(REVIEW_BODY)
    shared, provider = [], make_provider()
    github = FakeGitHub(shared)
    table = InMemoryTable(log=shared)
    hold(table)
    events = []
    status = _process_record(
        {"body": json.dumps(envelope_dict())},
        table=table,
        provider=provider,
        clock=lambda: NOW,
        diff_transport=FakeDiffTransport(shared, meta=[(200, SHA_B)]),
        llm_factory=lambda host, port, *, timeout: FakeLLMConnection(shared, [], []),
        github_transport=github,
        sink=lambda line: None,
        system_prompt="SYSTEM-PROMPT",
        remaining_time_ms=lambda: 900_000,
        events=events,
    )
    assert status == "published"
    assert next(c for c in github.calls if c["method"] == "POST")
    # Exactly one mutex op: the failed acquire attempt. No refresh, no
    # release — the contender publishes holding no lease.
    assert [e for e in shared if e[0] == "delete"] == []
    assert [e for e in shared if e[0] == "update" and e[1] == "SET lease_until = :until"] == []
    assert [e for e in shared if e[0] == "update" and "steal_before" in e[1]] == [
        ("update", "attribute_not_exists(pk) OR lease_until < :steal_before")
    ]

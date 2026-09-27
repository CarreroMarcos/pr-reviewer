"""SPR-107 T022: fan-out containment contract tests (HLD-004 §5 wiring pin).

`FanoutDegraded` is caught INSIDE the review closure around `run_fanout`
only: the closure runs the single-pass budget gate + the existing
single-pass inline path and returns content. An escaped `FanoutDegraded`
MUST NEVER reach `run_review` or the worker boundary — `is_retryable`
returns True for unknown faults, so an escape would trigger SQS
redelivery plus a spurious D2 transient notice instead of fallback.

Single-pass fallback runs emit `review_started`,
`degraded_to_single_pass`, the worker's usual stage checkpoints
(`established` → `finalized` as each stage is reached), and
`review_published` — the fallback closure itself emits only
`review_started` / `degraded_to_single_pass` and returns content; it
never emits `agent_*` / `verification_*`.

Gate-13 forward contracts pinned here:
1. Budget `FanoutDegraded("insufficient_budget", <stage>)` matches stage
   ∈ {"wave", "verifier", "synthesizer"} explicitly; stage failure
   reasons propagate unchanged; an unknown `failed_stage` re-raises loud
   (never mislabeled into an event).
2. The closure emits the terminal `degraded_to_single_pass` on the
   budget-degrade path (the sequencer itself emits nothing).
3. Containment is proven at three levels: closure returns content (no
   escape), `run_review` publishes through the degrade, and `handler`
   completes the record with the fallback comment posted.
4. Production threading (captured-stub rows): real `file_lengths`
   mapping (explicit `{}`, never None — post-image lengths have no
   pipeline source yet, so the clamp passes through; the verifier
   re-anchors per HLD :535-537), `creds.current()` snapshot kwargs,
   production `prompts/*.md` templates in FIXED specialty order, and a
   FRESH caller-owned events list per invocation.
5. Mutex-release ordering: NO surface exists yet (`lambda/common/mutex.py`
   is T024/T025's) — nothing to pin; release timing stays with the mutex
   tickets per HLD D9.

Gate-4 terminal row (HLD D9: no budget left even for fallback): the
closure emits `degraded_no_budget` and re-raises the `FanoutDegraded` —
the queue owns retries and a redelivery gets a fresh budget. This is the
ONE documented escape; every degradable failure is contained.

Doubles discipline (Gate-11): the `run_fanout` stub mirrors the
sequencer's requiredness (no defaults on required params); transport
doubles mirror the `test_worker.py` port shapes.

RED state: `worker_handler` has no fan-out closure symbols —
collection errors on import.
"""

import json

import pytest
from dynamodb_stub import InMemoryTable

import worker_handler
from common.envelope import validate_envelope
from common.fanout import FanoutDegraded
from common.llm import LlmError
from common.protocol import OutcomeKind, run_review
from worker_handler import (
    _FANOUT_SPECIALTIES,
    _Credentials,
    _load_fanout_prompts,
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
NOW = 1_750_000_000
POST_ID = 987654
ENDPOINT = "https://llm.example.test/v1/chat/completions"
RUN_ID = "0123456789abcdef0123456789abcdef"

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


class FakeDiffTransport:
    def __init__(self, *, meta, files=None):
        self._meta = list(meta)
        self._files = [file_entry()] if files is None else files
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        if "/files" in url:
            return FakeHttpResponse(200, json.dumps(self._files).encode())
        entry = self._meta.pop(0) if len(self._meta) > 1 else self._meta[0]
        status, sha = entry
        if status != 200:
            return FakeHttpResponse(status, b"{}")
        return FakeHttpResponse(200, json.dumps({"head": {"sha": sha}}).encode())


class FakeHttpResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body
        self.headers = {}


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
    def __init__(self, script, seen):
        self._script = script
        self.sock = FakeLLMSocket()
        self.requests = []
        seen.append(self)

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"method": method, "path": path})

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


class FanoutStub:
    """Production-faithful `run_fanout` double: required params have NO
    defaults (a dropped kwarg is a loud TypeError — Gate-11 rule)."""

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
        self.calls.append(
            {
                "diff_result": diff_result,
                "residuals": residuals,
                "cfg": cfg,
                "context": context,
                "run_id": run_id,
                "api_key": api_key,
                "model": model,
                "endpoint": endpoint,
                "events": events,
                "specialist_templates": dict(specialist_templates),
                "verifier_template": verifier_template,
                "synth_template": synth_template,
                "file_lengths": file_lengths,
                "allowed_hosts": allowed_hosts,
            }
        )
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        return self.behavior


def make_provider():
    from common.config import ConfigProvider

    return ConfigProvider(
        FakeSSM(dict(SSM_VALUES)).get_parameters,
        allowed_endpoint_hosts=("llm.example.test",),
    )


def make_closure(events, *, remaining=900_000, run_id=RUN_ID, llm_script=None):
    provider = make_provider()
    conns = []

    def factory(host, port, *, timeout):
        return FakeLLMConnection(list(llm_script) if llm_script else [], conns)

    closure = _make_review(
        envelope=validate_envelope(envelope_dict()),
        creds=_Credentials(provider),
        usage={},
        diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
        llm_factory=factory,
        system_prompt="SYSTEM-PROMPT",
        allowed_hosts=provider.allowed_hosts,
        clock=lambda: NOW,
        github_transport=FakeGitHub(),
        remaining_time_ms=lambda: remaining,
        events=events,
        run_id=run_id,
    )
    closure._test_conns = conns  # noqa: SLF001 (test-only introspection)
    return closure


def events_of_type(events, type_name):
    return [e for e in events if e["type"] == type_name]


@pytest.fixture()
def multi_agent(monkeypatch):
    monkeypatch.setenv("MULTI_AGENT", "1")
    return monkeypatch


@pytest.fixture()
def stubbed_fanout(monkeypatch):
    holder = {}

    def install(behavior):
        stub = FanoutStub(behavior)
        monkeypatch.setattr(worker_handler, "run_fanout", stub)
        holder["stub"] = stub
        return stub

    return install


# --- closure surface: fixed order + production templates -------------------------------


def test_fanout_specialties_fixed_order():
    assert _FANOUT_SPECIALTIES == ("correctness", "security", "tests")


def test_load_fanout_prompts_reads_production_files():
    prompts = _load_fanout_prompts()
    assert set(prompts) == {"correctness", "security", "tests", "verifier", "synthesizer"}
    assert all(isinstance(text, str) and text.strip() for text in prompts.values())


def test_is_retryable_unknown_fault_is_true():
    # The motive for containment: an escaped FanoutDegraded would
    # redeliver via SQS plus a spurious D2 transient notice.
    assert is_retryable(FanoutDegraded("insufficient_budget", "wave")) is True


# --- happy fan-out: content through, no degrade event -----------------------------------


def test_fanout_success_returns_built_content(multi_agent, stubbed_fanout):
    stub = stubbed_fanout(REVIEW_BODY)
    events = []
    closure = make_closure(events)
    content = closure(SHA_B, 0)
    assert REVIEW_BODY in content
    assert stub.calls and len(stub.calls) == 1
    assert events_of_type(events, "degraded_to_single_pass") == []
    assert events_of_type(events, "review_started")


def test_closure_threads_production_seams(multi_agent, stubbed_fanout):
    stub = stubbed_fanout(REVIEW_BODY)
    events = []
    closure = make_closure(events)
    closure(SHA_B, 0)
    call = stub.calls[0]
    # Forward contract 4, every seam explicit:
    assert call["file_lengths"] == {}  # real mapping, never None (lengths unwired)
    assert isinstance(call["file_lengths"], dict)
    assert call["residuals"] == []  # accepted-residuals file absent in repo
    assert call["api_key"] == "glm-key-value"  # creds.current() snapshot
    assert call["model"] == "glm-5.3-flash"
    assert call["endpoint"] == ENDPOINT
    assert list(call["specialist_templates"]) == ["correctness", "security", "tests"]
    loaded = _load_fanout_prompts()
    assert call["specialist_templates"]["correctness"] == loaded["correctness"]
    assert call["verifier_template"] == loaded["verifier"]
    assert call["synth_template"] == loaded["synthesizer"]
    assert call["events"] is events  # the SAME caller-owned list, still fresh-owned
    assert call["run_id"] == RUN_ID
    assert call["context"].get_remaining_time_in_millis() == 900_000


def test_fresh_events_list_per_invocation(multi_agent, stubbed_fanout):
    stubbed_fanout(REVIEW_BODY)
    provider = make_provider()

    def build(events):
        return _make_review(
            envelope=validate_envelope(envelope_dict()),
            creds=_Credentials(provider),
            usage={},
            diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
            llm_factory=lambda host, port, *, timeout: FakeLLMConnection([], []),
            system_prompt="SYSTEM-PROMPT",
            clock=lambda: NOW,
            github_transport=FakeGitHub(),
            remaining_time_ms=lambda: 900_000,
            events=events,
        )

    first, second = [], []
    run_ids = set()
    build(first)(SHA_B, 0)
    build(second)(SHA_B, 0)
    assert first is not second and first and second
    run_ids.update(e["run_id"] for e in first)
    run_ids.update(e["run_id"] for e in second)
    assert len(run_ids) == 2  # distinct run_id per invocation


# --- degrade path: budget + leg failures fall back --------------------------------------


@pytest.mark.parametrize("stage", ["wave", "verifier", "synthesizer"])
def test_budget_degrade_matches_stage_and_falls_back(multi_agent, stubbed_fanout, stage):
    """Gate-13 contract 1: insufficient_budget matches each stage
    explicitly; the degraded event carries (reason, stage) unchanged."""
    stubbed_fanout(FanoutDegraded("insufficient_budget", stage))
    events = []
    closure = make_closure(events)
    content = closure(SHA_B, 0)
    assert REVIEW_BODY in content  # existing single-pass inline, returned
    degraded = events_of_type(events, "degraded_to_single_pass")
    assert len(degraded) == 1
    assert (degraded[0]["reason"], degraded[0]["failed_stage"]) == (
        "insufficient_budget",
        stage,
    )
    assert events_of_type(events, "agent_started") == []
    assert events_of_type(events, "agent_completed") == []
    assert events_of_type(events, "verification_done") == []
    assert events_of_type(events, "review_synthesized") == []


def test_stage_failure_reason_propagates_unchanged(multi_agent, stubbed_fanout):
    stubbed_fanout(FanoutDegraded("invalid_response", "verifier"))
    events = []
    closure = make_closure(events)
    content = closure(SHA_B, 0)
    assert REVIEW_BODY in content
    degraded = events_of_type(events, "degraded_to_single_pass")
    assert [(e["reason"], e["failed_stage"]) for e in degraded] == [
        ("invalid_response", "verifier")
    ]


def test_unknown_failed_stage_reraises_loud(multi_agent, stubbed_fanout):
    """ADV-4: an unknown failed_stage is never mislabeled into an event —
    it re-raises with no fallback attempted."""
    stubbed_fanout(FanoutDegraded("boom", "bogus_stage"))
    events = []
    closure = make_closure(events)
    with pytest.raises(FanoutDegraded):
        closure(SHA_B, 0)
    assert events_of_type(events, "degraded_to_single_pass") == []
    assert closure._test_conns == []  # noqa: SLF001 (no fallback LLM call)


def test_terminal_no_budget_emits_event_and_raises(multi_agent, stubbed_fanout):
    """Gate-4 terminal row: no budget left even for fallback — the closure
    emits degraded_no_budget and re-raises; the queue owns the retry with
    a fresh budget on redelivery."""
    stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"))
    events = []
    closure = make_closure(events, remaining=1_000)
    with pytest.raises(FanoutDegraded):
        closure(SHA_B, 0)
    assert len(events_of_type(events, "degraded_to_single_pass")) == 1
    terminal = events_of_type(events, "degraded_no_budget")
    assert len(terminal) == 1
    assert terminal[0]["reason"] == "insufficient_budget"
    assert closure._test_conns == []  # noqa: SLF001 (fallback never attempted)


def test_legacy_path_never_attempts_fanout(stubbed_fanout):
    stub = stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"))
    events = []
    closure = make_closure(events)
    content = closure(SHA_B, 0)  # no MULTI_AGENT env: legacy inline
    assert REVIEW_BODY in content
    assert stub.calls == []
    assert events_of_type(events, "degraded_to_single_pass") == []
    assert events_of_type(events, "review_started")


# --- boundary proofs: run_review, record, handler ----------------------------------------


def test_run_review_boundary_never_sees_fanout_degraded(multi_agent, stubbed_fanout):
    stubbed_fanout(FanoutDegraded("insufficient_budget", "synthesizer"))
    events = []
    closure = make_closure(events)
    table = InMemoryTable()
    outcome = run_review(
        pk=PK,
        incoming_sha=SHA_B,
        owner=GUID_1,
        table=table,
        now=lambda: NOW,
        review=closure,
        fence=lambda: SHA_B,
        publish=lambda content: POST_ID,
    )
    assert outcome.kind == OutcomeKind.PUBLISHED
    assert outcome.comment_id == POST_ID


def test_process_record_fallback_event_chain(multi_agent, stubbed_fanout):
    """The full fallback run emits the T022 chain in order: closure pair,
    worker checkpoints as each stage is reached, publish event."""
    stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"))
    provider = make_provider()
    github = FakeGitHub()
    events = []
    status = _process_record(
        {"body": json.dumps(envelope_dict())},
        table=InMemoryTable(),
        provider=provider,
        clock=lambda: NOW,
        diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
        llm_factory=lambda host, port, *, timeout: FakeLLMConnection([], []),
        github_transport=github,
        sink=lambda line: None,
        system_prompt="SYSTEM-PROMPT",
        remaining_time_ms=lambda: 900_000,
        events=events,
    )
    assert status == "published"
    chain = [(e["type"], e.get("stage")) for e in events]
    assert chain == [
        ("checkpoint", "established"),
        ("review_started", None),
        ("checkpoint", "diff_fetched"),
        ("degraded_to_single_pass", None),
        ("checkpoint", "claimed"),
        ("checkpoint", "published"),
        ("review_published", None),
        ("checkpoint", "finalized"),
    ]
    published = events_of_type(events, "review_published")
    assert [e["comment_id"] for e in published] == [POST_ID]
    assert [m for m in [c["method"] for c in github.calls] if m == "POST"]
    posted = next(c for c in github.calls if c["method"] == "POST")
    assert REVIEW_BODY in posted["body"]["body"]  # fallback content published
    assert all(e["run_id"] == events[0]["run_id"] for e in events)


def test_handler_boundary_contains_degrade(multi_agent, stubbed_fanout):
    """Worker-boundary proof: handler completes the record (no raise, no
    redelivery) with the fallback comment posted."""
    stubbed_fanout(FanoutDegraded("all_specialists_failed", "wave"))
    provider = make_provider()
    github = FakeGitHub()
    table = InMemoryTable()
    result = handler(
        {"Records": [{"body": json.dumps(envelope_dict()), "messageId": "m1"}]},
        None,
        _table=table,
        _config_provider=provider,
        _now=lambda: NOW,
        _diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
        _llm_factory=lambda host, port, *, timeout: FakeLLMConnection([], []),
        _github_transport=github,
        _sink=lambda line: None,
        _system_prompt="SYSTEM-PROMPT",
    )
    assert result == {"ok": True, "results": ["published"]}
    posted = next(c for c in github.calls if c["method"] == "POST")
    assert REVIEW_BODY in posted["body"]["body"]


def test_llm_error_still_propagates_for_retry(multi_agent, stubbed_fanout):
    """Non-fanout faults keep today's boundary behavior: a retryable LLM
    error from the fallback path still raises for queue redelivery."""
    stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"))
    events = []
    closure = make_closure(events, llm_script=[("raise", LlmError("timeout"))])
    with pytest.raises(LlmError):
        closure(SHA_B, 0)

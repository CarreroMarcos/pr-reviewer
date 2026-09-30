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
   mapping (hunk-derived via `post_image_lengths`, never None — the
   bound is the reviewed-span peak; absent-span files are omitted and
   pass the clamp untouched; the verifier re-anchors per HLD
   :535-537), `creds.current()` snapshot kwargs,
   production `prompts/*.md` templates in FIXED specialty order, and a
   FRESH caller-owned events list per invocation.
5. Mutex-release ordering: NO surface exists yet (`lambda/common/mutex.py`
   is T024/T025's) — nothing to pin; release timing stays with the mutex
   tickets per HLD D9.

Gate-4 terminal row (HLD D9: no budget left even for fallback): the
closure emits `degraded_no_budget` and re-raises the `FanoutDegraded` —
the queue owns retries and a redelivery gets a fresh budget. This is the
ONE documented escape; every degradable failure is contained. T062
(Gate-14 Finding 1): the escaped terminal-row failure is caught at the
record boundary (`_process_record`) — `retry_queued` log + D2 TRANSIENT
notice via the existing failure-lifecycle path — then re-raised
UNCHANGED for queue redelivery.

Doubles discipline (Gate-11): the `run_fanout` stub mirrors the
sequencer's requiredness (no defaults on required params); transport
doubles mirror the `test_worker.py` port shapes.

RED state: `worker_handler` has no fan-out closure symbols —
collection errors on import.
"""

import json
import logging
from pathlib import Path

import pytest
from dynamodb_stub import InMemoryTable

import worker_handler
from common.config import ConfigError
from common.envelope import validate_envelope
from common.events import agent_started
from common.failure_notice import NoticeTrigger
from common.fanout import FanoutDegraded
from common.llm import LlmError
from common.protocol import OutcomeKind, run_review
from worker_handler import (
    _FANOUT_SPECIALTIES,
    _RESIDUALS_FILENAME,
    _Credentials,
    _load_fanout_prompts,
    _load_residuals_for,
    _make_review,
    _notice_trigger,
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
# build_comment strips surrounding whitespace: containment assertions use
# the stripped form (the model text itself rides byte-identical).
REVIEW_TEXT = REVIEW_BODY.strip()

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


def file_entry(name="src/main.py", patch=None):
    return {
        "filename": name,
        "additions": 5,
        "deletions": 2,
        "patch": patch or "@@ -1,2 +1,2 @@\n-old\n+new\n",
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


def make_closure(events, *, remaining=900_000, run_id=RUN_ID, llm_script=None, files=None):
    provider = make_provider()
    conns = []

    def factory(host, port, *, timeout):
        return FakeLLMConnection(list(llm_script) if llm_script else [], conns)

    closure = _make_review(
        envelope=validate_envelope(envelope_dict()),
        creds=_Credentials(provider),
        usage={},
        diff_transport=FakeDiffTransport(meta=[(200, SHA_B)], files=files),
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


def test_load_fanout_prompts_undecodable_is_config_fault(monkeypatch):
    """Bot R1 Fix 2: a corrupted (non-UTF-8) prompt is the same
    "unreadable prompt" class as a missing file — ConfigError, never an
    empty prompt and never a raw UnicodeDecodeError escape."""
    real_read_text = Path.read_text

    def boom(self, *args, **kwargs):
        if str(self).endswith("verifier.md"):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    with pytest.raises(ConfigError):
        _load_fanout_prompts()


def test_residuals_missing_everywhere_yields_empty(monkeypatch):
    """Missing-proof terminal result unchanged: no layout exists → [],
    after probing both layouts (the loader itself is never reached)."""
    probes = []
    real_is_file = Path.is_file

    def spy(self):
        probes.append(str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", spy)
    assert _load_residuals_for(REPO, PR_NUMBER) == []
    assert len(probes) == 2


def test_residuals_present_but_empty_is_authoritative(monkeypatch):
    """Bot R1 Fix 1: the first layout whose file EXISTS wins — its parse
    is authoritative even when empty, so the second layout is NOT
    consulted (no fall-through past a successful parse)."""
    calls = []
    real_load = worker_handler.load_accepted_residuals

    def spy(path):
        calls.append(path)
        return real_load(path)

    monkeypatch.setattr(worker_handler, "load_accepted_residuals", spy)
    monkeypatch.setattr(Path, "is_file", lambda self: str(self).endswith(_RESIDUALS_FILENAME))
    assert _load_residuals_for(REPO, PR_NUMBER) == []
    assert len(calls) == 1


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
    assert REVIEW_TEXT in content
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
    assert call["file_lengths"] == {"src/main.py": 2}  # hunk-derived, never None
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


def test_closure_threads_nonempty_file_lengths_to_clamp(multi_agent, stubbed_fanout):
    """T060 pin: a NON-EMPTY hunk-derived mapping reaches the fan-out
    path (the clamp's input seam) — the `file_lengths={}` pass-through
    era is over. The stub stands in for `run_fanout` (which forwards
    the mapping to `clamp_to_post_image` unchanged), so capture here
    proves the threading."""
    stub = stubbed_fanout(REVIEW_BODY)
    events = []
    closure = make_closure(events)
    closure(SHA_B, 0)
    lengths = stub.calls[0]["file_lengths"]
    assert isinstance(lengths, dict) and lengths  # non-empty, never None
    # `"@@ -1,2 +1,2 @@..."` → new peak 1 + 2 − 1 = 2.
    assert lengths == {"src/main.py": 2}


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
    assert REVIEW_TEXT in content  # existing single-pass inline, returned
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
    assert REVIEW_TEXT in content
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


def test_terminal_row_is_retryable_named_case():
    """T062: the terminal-row budget failure is a NAMED retryable case
    (not an anonymous fall-through) — the queue owns the retry, bounded
    by maxReceiveCount 3."""
    assert is_retryable(FanoutDegraded("insufficient_budget", "wave")) is True


def test_terminal_row_notice_trigger_is_transient():
    """T062: the terminal-row failure rides the existing D2 path as a
    TRANSIENT row — RETRYING notice first, FINAL at the last receive."""
    assert _notice_trigger(FanoutDegraded("insufficient_budget", "wave")) is (
        NoticeTrigger.TRANSIENT
    )


def test_terminal_row_boundary_observability(multi_agent, stubbed_fanout):
    """T062 (Gate-14 Finding 1): the escaped terminal-row `FanoutDegraded`
    at the record boundary produces the `retry_queued` log + D2 notice,
    is re-raised UNCHANGED (same object — redelivery semantics
    identical), and `degraded_no_budget` appears exactly once
    (closure-owned; the boundary adds none). The RETRYING→FINAL phase
    mapping across redeliveries is the notice machinery's own contract
    (`test_notice_phase_matrix` +
    `test_publish_phase_and_attempts_reach_the_content` in
    tests/contracts/test_failure_notice.py)."""
    boom = FanoutDegraded("insufficient_budget", "wave")
    stubbed_fanout(boom)
    provider = make_provider()
    github = FakeGitHub()
    events = []
    sink = []
    with pytest.raises(FanoutDegraded) as exc_info:
        _process_record(
            {"body": json.dumps(envelope_dict())},
            table=InMemoryTable(),
            provider=provider,
            clock=lambda: NOW,
            diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
            llm_factory=lambda host, port, *, timeout: FakeLLMConnection([], []),
            github_transport=github,
            sink=sink.append,
            system_prompt="SYSTEM-PROMPT",
            remaining_time_ms=lambda: 1_000,
            events=events,
        )
    assert exc_info.value is boom
    (line,) = [json.loads(entry) for entry in sink]
    assert line["status"] == "retry_queued"
    assert line["error_class"] == "fanout_insufficient_budget_wave"
    assert line["failure_notice_published"] == "true"
    assert [c for c in github.calls if c["method"] == "POST"]  # D2 notice landed
    assert len(events_of_type(events, "degraded_no_budget")) == 1


def test_legacy_path_never_attempts_fanout(stubbed_fanout):
    stub = stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"))
    events = []
    closure = make_closure(events)
    content = closure(SHA_B, 0)  # no MULTI_AGENT env: legacy inline
    assert REVIEW_TEXT in content
    assert stub.calls == []
    assert events_of_type(events, "degraded_to_single_pass") == []
    assert events_of_type(events, "review_started")


def test_docs_only_diff_skips_fanout(multi_agent, stubbed_fanout):
    """T076: MULTI_AGENT=1 + every file docs-shaped routes single-pass —
    the fanout stub observes zero calls (wave/verifier/synthesizer LLM
    spend never happens) and the event carries the true disposition:
    concurrency_single_pass/docs_only, never a fabricated degradation
    (Gate-15/ADV-4: fan-out was not attempted)."""
    stub = stubbed_fanout(REVIEW_TEXT)
    events = []
    closure = make_closure(events, files=[file_entry("README.md"), file_entry("docs/guide.md")])
    content = closure(SHA_B, 0)
    assert REVIEW_TEXT in content
    assert stub.calls == []
    (evt,) = events_of_type(events, "concurrency_single_pass")
    assert evt["reason"] == "docs_only"
    assert events_of_type(events, "degraded_to_single_pass") == []
    # Spec verify letter: NO fanout-stage events anywhere in the chain —
    # the type set admits only the single-pass routing vocabulary.
    assert {e["type"] for e in events} <= {
        "review_started",
        "checkpoint",
        "concurrency_single_pass",
    }


def test_docs_only_publishes_via_record_path(multi_agent, stubbed_fanout, caplog):
    """T076 spec verify letter, end to end: a docs-only diff publishes
    through the full record path with the routing chain
    review_started → concurrency_single_pass(docs_only) → published and
    zero fanout-stage events (agent/wave/verifier/synthesizer types
    absent from the chain)."""
    stubbed_fanout(REVIEW_TEXT)
    caplog.set_level(logging.INFO, logger="worker_handler")
    events = []
    sink_lines = []
    result = _process_record(
        {"body": json.dumps(envelope_dict())},
        table=InMemoryTable(),
        provider=make_provider(),
        clock=lambda: NOW,
        diff_transport=FakeDiffTransport(
            meta=[(200, SHA_B)],
            files=[file_entry("README.md"), file_entry("docs/guide.md")],
        ),
        llm_factory=lambda host, port, *, timeout: FakeLLMConnection([], []),
        github_transport=FakeGitHub(),
        sink=sink_lines.append,
        system_prompt="SYSTEM-PROMPT",
        remaining_time_ms=lambda: 900_000,
        events=events,
    )
    assert result == "published"
    (pub,) = [json.loads(x) for x in sink_lines if json.loads(x).get("status") == "published"]
    # Gate-58 advisory semantics: the docs-only publish resolves
    # single_pass and must stay out of the runs_multi_agent metric —
    # the drift traffic term subtracts it via runs_docs_only instead.
    assert pub["pipeline"] == "single_pass"
    assert {e["type"] for e in events} <= {
        "review_started",
        "checkpoint",
        "concurrency_single_pass",
        "review_published",
    }
    (evt,) = events_of_type(events, "concurrency_single_pass")
    assert evt["reason"] == "docs_only"
    assert events_of_type(events, "review_published")
    # T077 (Gate-58 advisory carrier): the bare term logged at the skip
    # path is the sole signal feeding the runs_docs_only metric filter.
    assert any(rec.message == "docs_only fanout_skip=1" for rec in caplog.records), (
        "bare-term docs_only metric signal missing at the skip path"
    )


def test_docs_only_no_event_without_multi_agent(stubbed_fanout):
    """T076 (PR #173 r1 LOW): with MULTI_AGENT unset the docs predicate is
    irrelevant — the else-branch must NOT emit a spurious
    concurrency_single_pass/docs_only event (there was no fanout decision
    to record)."""
    stubbed_fanout(REVIEW_TEXT)
    events = []
    closure = make_closure(events, files=[file_entry("README.md")])
    content = closure(SHA_B, 0)  # no multi_agent fixture: legacy inline
    assert REVIEW_TEXT in content
    assert events_of_type(events, "concurrency_single_pass") == []


def test_mixed_diff_still_fans_out(multi_agent, stubbed_fanout):
    """T076 boundary: ONE code file anywhere keeps the full battery —
    the docs predicate never downgrades a mixed diff."""
    stub = stubbed_fanout(REVIEW_TEXT)
    events = []
    closure = make_closure(events, files=[file_entry("README.md"), file_entry("src/main.py")])
    content = closure(SHA_B, 0)
    assert REVIEW_TEXT in content
    assert stub.calls  # fanout attempted
    assert events_of_type(events, "concurrency_single_pass") == []


def test_truncated_mixed_diff_keeps_the_battery(multi_agent, stubbed_fanout):
    """T079: files is the post-budget kept list — a diff over the caps
    can keep only prose while dropping code files. Routing must fail
    safe on truncation: the fanout battery runs even though the kept
    set is docs-only (pre-fix this silently routed single_pass)."""
    stub = stubbed_fanout(REVIEW_TEXT)
    events = []
    closure = make_closure(
        events,
        files=[
            file_entry("a.md"),
            file_entry("z.py", patch="x" * 800_001),
        ],
    )
    content = closure(SHA_B, 0)
    assert REVIEW_TEXT in content
    assert stub.calls  # fanout attempted
    assert events_of_type(events, "concurrency_single_pass") == []


def test_truncated_docs_only_diff_pays_the_battery(multi_agent, stubbed_fanout):
    """T079 fail-safe direction: an over-budget docs-only diff still
    pays the fanout — truncation makes docs-only classification
    unreliable (dropped files are invisible), so the battery runs."""
    stub = stubbed_fanout(REVIEW_TEXT)
    events = []
    closure = make_closure(
        events,
        files=[
            file_entry("a.md"),
            file_entry("z.md", patch="x" * 800_001),
        ],
    )
    content = closure(SHA_B, 0)
    assert REVIEW_TEXT in content
    assert stub.calls  # fanout attempted
    assert events_of_type(events, "concurrency_single_pass") == []


def test_replay_footer_on_fanout_published_body(multi_agent, stubbed_fanout, monkeypatch):
    """T075 wiring (fanout side, publish boundary): the POSTed canonical
    body carries the footer with pr_number + head_sha interpolated —
    appended after the §5 sanitizer, so the sha survives it (PR #169 r1
    MEDIUM wiring + r2 sha-redaction find)."""
    stubbed_fanout(REVIEW_TEXT)
    monkeypatch.setenv("REPLAY_BASE_URL", "https://viewer.example.test")
    github = FakeGitHub()
    result = _process_record(
        {"body": json.dumps(envelope_dict())},
        table=InMemoryTable(),
        provider=make_provider(),
        clock=lambda: NOW,
        diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
        llm_factory=lambda host, port, *, timeout: FakeLLMConnection([], []),
        github_transport=github,
        sink=[].append,
        system_prompt="SYSTEM-PROMPT",
        remaining_time_ms=lambda: 900_000,
        events=[],
    )
    assert result == "published"
    (post,) = [c for c in github.calls if c["method"] == "POST"]
    assert f"/runs/{PR_NUMBER}/{SHA_B}/" in post["body"]["body"]


def test_publish_log_line_carries_pipeline_multi_agent(multi_agent, stubbed_fanout):
    """T077: the happy-path publish log line carries the routing
    discriminator from the run's own event chain — multi_agent when the
    chain holds fanout-stage events. The run_fanout stub bypasses the
    specialist chain, so it is pre-seeded with one real fanout-stage
    event (canonical r3: the single_pass row alone left multi_agent
    resolution unpinned)."""
    stubbed_fanout(REVIEW_TEXT)
    sink_lines = []
    result = _process_record(
        {"body": json.dumps(envelope_dict())},
        table=InMemoryTable(),
        provider=make_provider(),
        clock=lambda: NOW,
        diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
        llm_factory=lambda host, port, *, timeout: FakeLLMConnection([], []),
        github_transport=FakeGitHub(),
        sink=sink_lines.append,
        system_prompt="SYSTEM-PROMPT",
        remaining_time_ms=lambda: 900_000,
        events=[agent_started(specialty="correctness", run_id=RUN_ID)],
    )
    assert result == "published"
    (line,) = [json.loads(x) for x in sink_lines if json.loads(x).get("status") == "published"]
    assert line["pipeline"] == "multi_agent"


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
    assert REVIEW_TEXT in posted["body"]["body"]  # fallback content published
    assert all(e["run_id"] == events[0]["run_id"] for e in events)


def test_handler_boundary_contains_degrade(multi_agent, stubbed_fanout):
    """Worker-boundary proof: handler completes the record (no raise, no
    redelivery) with the fallback comment posted."""
    from types import SimpleNamespace

    stubbed_fanout(FanoutDegraded("all_specialists_failed", "wave"))
    provider = make_provider()
    github = FakeGitHub()
    table = InMemoryTable()
    context = SimpleNamespace(get_remaining_time_in_millis=lambda: 900_000)
    result = handler(
        {"Records": [{"body": json.dumps(envelope_dict()), "messageId": "m1"}]},
        context,
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
    assert REVIEW_TEXT in posted["body"]["body"]


def test_llm_error_still_propagates_for_retry(multi_agent, stubbed_fanout):
    """Non-fanout faults keep today's boundary behavior: a retryable LLM
    error from the fallback path still raises for queue redelivery."""
    stubbed_fanout(FanoutDegraded("insufficient_budget", "wave"))
    events = []
    closure = make_closure(events, llm_script=[("raise", LlmError("timeout"))])
    with pytest.raises(LlmError):
        closure(SHA_B, 0)

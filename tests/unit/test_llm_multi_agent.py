"""SPR-86 T003: multi-agent `llm.py` contract tests (HLD-004 §9 items 2–6).

Payload carries `max_tokens: 16384`; `finish_reason == "length"` raises
`LlmError("length")`; `thinking_enabled=True` sends
`thinking: {"type": "enabled"}` + `reasoning_effort`; `reasoning_content`
is extracted onto `ReviewResult`; HTTP 429 with Z.AI code 1302 raises
`LlmError("rate_limit")`; the review call takes an explicit
`read_timeout_s` kwarg defaulting to a single `_read_timeout_s()`
resolution per call, with the socket timeout equal to the passed value.

RED state: none of these behaviors exist yet — collection succeeds but
the behavior tests fail (unexpected kwargs, missing payload keys,
`http_429` instead of `rate_limit`, missing `ReviewResult` field).
"""

import json

import pytest

from common import llm
from common.llm import LlmError, review_diff

API_KEY = "glm-test-key-not-a-credential"  # noqa: S105 (dummy fixture)
MODEL = "glm-5.3-flash"
ENDPOINT = "https://llm.example.test/v1/chat/completions"
GLM_ENDPOINT = "https://api.z.ai/v1/chat/completions"

COMPLETION_TEXT = "COMPLETION-SENTINEL-4d2e: ## Summary\nNo significant issues found."


def completion_body(content=COMPLETION_TEXT, finish_reason="stop", reasoning_content=None):
    message = {"role": "assistant", "content": content}
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    return json.dumps(
        {
            "id": "chatcmpl-fake",
            "choices": [{"message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
        }
    ).encode()


class FakeSocket:
    def __init__(self):
        self.settimeout_calls = []

    def settimeout(self, seconds):
        self.settimeout_calls.append(seconds)


class FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeConnection:
    instances = []

    def __init__(self, host, port, *, timeout, behavior=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.behavior = behavior or {}
        self.sock = FakeSocket()
        self.requests = []
        FakeConnection.instances.append(self)

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        self.requests.append(
            {"method": method, "path": path, "body": body, "headers": dict(headers or {})}
        )

    def getresponse(self):
        return self.behavior.get("response", FakeResponse(200, completion_body()))

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _clean_instances():
    FakeConnection.instances.clear()
    yield
    FakeConnection.instances.clear()


def make_factory(behavior=None, seen=None):
    def factory(host, port, *, timeout):
        conn = FakeConnection(host, port, timeout=timeout, behavior=behavior)
        if seen is not None:
            seen.append(conn)
        return conn

    return factory


def invoke(**overrides):
    params = {
        "api_key": API_KEY,
        "model": MODEL,
        "endpoint": ENDPOINT,
        "system_prompt": "SYSTEM-PROMPT",
        "diff_text": "diff --git a/foo.py b/foo.py",
    }
    params.update(overrides)
    if "_connection_factory" not in params:
        params["_connection_factory"] = make_factory()
    return review_diff(**params)


def last_request():
    assert len(FakeConnection.instances) == 1
    conn = FakeConnection.instances[0]
    assert len(conn.requests) == 1
    return conn, conn.requests[0]


# --- §9 item 2: explicit max tokens -------------------------------------------


def test_max_tokens_16384_sent():
    invoke()
    _, request = last_request()
    body = json.loads(request["body"])
    assert body["max_tokens"] == 16384


# --- §9 item 3: finish-reason length detection ---------------------------------


def test_finish_reason_length_raises_length_error():
    factory = make_factory(
        behavior={"response": FakeResponse(200, completion_body(finish_reason="length"))}
    )
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "length"


def test_finish_reason_stop_still_succeeds():
    result = invoke()
    assert result.content == COMPLETION_TEXT


# --- §9 item 4: thinking enabled + reasoning effort ----------------------------


def test_thinking_enabled_sends_enabled_and_effort():
    invoke(thinking_enabled=True, reasoning_effort="low", endpoint=GLM_ENDPOINT)
    _, request = last_request()
    body = json.loads(request["body"])
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "low"


def test_thinking_default_keeps_disabled_for_glm():
    """Guard: existing single-pass callers (no flag) keep today's payload."""
    invoke(endpoint=GLM_ENDPOINT)
    _, request = last_request()
    body = json.loads(request["body"])
    assert body["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


# --- §9 item 5: reasoning trace extraction --------------------------------------


def test_reasoning_content_extracted_onto_result():
    factory = make_factory(
        behavior={
            "response": FakeResponse(
                200, completion_body(reasoning_content="TRACE-SENTINEL-8c1a")
            )
        }
    )
    result = invoke(_connection_factory=factory)
    assert result.content == COMPLETION_TEXT
    assert result.reasoning_content == "TRACE-SENTINEL-8c1a"


def test_reasoning_content_absent_is_none():
    result = invoke()
    assert result.reasoning_content is None


# --- §9 item 6: Z.AI 1302 taxonomy ---------------------------------------------


@pytest.mark.parametrize("code", ["1302", 1302])
def test_429_with_1302_code_raises_rate_limit(code):
    body = json.dumps({"error": {"code": code, "message": "concurrency limit"}}).encode()
    factory = make_factory(behavior={"response": FakeResponse(429, body)})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "rate_limit"


def test_429_without_1302_stays_http_429():
    """Guard: non-1302 throttling keeps today's class."""
    factory = make_factory(behavior={"response": FakeResponse(429, b"throttled")})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "http_429"


# --- §9 item 1: explicit read_timeout_s ------------------------------------------


def test_read_timeout_s_default_resolved_exactly_once(monkeypatch):
    """The `_read_timeout_s()` default is evaluated at most once per call —
    one resolution at entry feeds both the default and the socket."""
    monkeypatch.delenv("GLM_READ_TIMEOUT_S", raising=False)
    calls = []
    real = llm._read_timeout_s

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(llm, "_read_timeout_s", counting)
    seen = []
    invoke(_connection_factory=make_factory(seen=seen))
    assert len(calls) == 1
    assert seen[0].sock.settimeout_calls == [240]


def test_read_timeout_s_override_sets_socket_timeout(monkeypatch):
    """Contender override (T026): the passed value wins over env, and no
    env resolution happens on the override path."""
    monkeypatch.setenv("GLM_READ_TIMEOUT_S", "300")
    calls = []
    real = llm._read_timeout_s

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(llm, "_read_timeout_s", counting)
    seen = []
    invoke(_connection_factory=make_factory(seen=seen), read_timeout_s=111)
    assert seen[0].sock.settimeout_calls == [111]
    assert calls == []

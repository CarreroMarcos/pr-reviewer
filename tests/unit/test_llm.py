"""T028: LLM chat-completions contract tests (HLD §2.3 item 5, §2.7).

Stubbed transport — never network: temperature 0.2 sent, model/endpoint
from injected config, timeouts connect 2 s / read 45 s, error path raises
for queue retry, logs carry only status/duration/token usage (never prompt,
diff, completion, or key material).
"""

import json
import logging

import pytest

from common import llm
from common.llm import (
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    TEMPERATURE,
    LlmError,
    ReviewResult,
    review_diff,
)

API_KEY = "glm-test-key-not-a-credential"  # noqa: S105 (dummy fixture)
MODEL = "glm-5.3-flash"
ENDPOINT = "https://llm.example.test/v1/chat/completions"

SYSTEM_PROMPT = "SYSTEM-PROMPT-SENTINEL-7f3a: review for bugs only."
DIFF_TEXT = "DIFF-SENTINEL-9b1c: diff --git a/foo.py b/foo.py @@ -1 +1 @@ -x +y"
COMPLETION_TEXT = "COMPLETION-SENTINEL-4d2e: ## Summary\nNo significant issues found."


def completion_body(content=COMPLETION_TEXT, usage=None):
    return json.dumps(
        {
            "id": "chatcmpl-fake",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": usage or {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
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
    """Stub transport behind the `_connection_factory` seam.

    Mirrors the `http.client.HTTPSConnection` surface `llm.py` uses:
    `connect()` → `.sock.settimeout()` → `request()` → `getresponse()`.
    """

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
        if self.behavior.get("raise_on_connect"):
            raise self.behavior["raise_on_connect"]

    def request(self, method, path, body=None, headers=None):
        self.requests.append(
            {"method": method, "path": path, "body": body, "headers": dict(headers or {})}
        )
        if self.behavior.get("raise_on_request"):
            raise self.behavior["raise_on_request"]

    def getresponse(self):
        if self.behavior.get("raise_on_response"):
            raise self.behavior["raise_on_response"]
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
        "system_prompt": SYSTEM_PROMPT,
        "diff_text": DIFF_TEXT,
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


# --- exported constants -------------------------------------------------------


def test_temperature_constant_is_exactly_0_2():
    assert TEMPERATURE == 0.2
    assert isinstance(TEMPERATURE, float)


def test_timeout_constants():
    assert CONNECT_TIMEOUT_S == 2
    assert READ_TIMEOUT_S == 45


# --- request contract ---------------------------------------------------------


def test_temperature_sent_exactly():
    invoke()
    _, request = last_request()
    body = json.loads(request["body"])
    assert body["temperature"] == 0.2
    assert isinstance(body["temperature"], float)


def test_model_and_messages_sent():
    invoke()
    _, request = last_request()
    body = json.loads(request["body"])
    assert body["model"] == MODEL
    assert body["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": DIFF_TEXT},
    ]


def test_authorization_header_carries_injected_key():
    invoke()
    _, request = last_request()
    assert request["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert request["headers"]["Content-Type"] == "application/json"


def test_posts_to_endpoint_host_and_path():
    invoke()
    conn, request = last_request()
    assert conn.host == "llm.example.test"
    assert request["method"] == "POST"
    assert request["path"] == "/v1/chat/completions"


def test_model_and_endpoint_come_from_injected_config():
    invoke(model="other-model", endpoint="https://other.example.test/chat")
    conn, request = last_request()
    body = json.loads(request["body"])
    assert body["model"] == "other-model"
    assert conn.host == "other.example.test"
    assert request["path"] == "/chat"


# --- timeout policy -----------------------------------------------------------


def test_connect_timeout_is_2s():
    invoke()
    conn, _ = last_request()
    assert conn.timeout == 2
    assert conn.timeout == CONNECT_TIMEOUT_S


def test_read_timeout_set_to_45s_after_connect():
    seen = []
    invoke(_connection_factory=make_factory(seen=seen))
    assert seen[0].sock.settimeout_calls == [45]
    assert seen[0].sock.settimeout_calls == [READ_TIMEOUT_S]


# --- response contract --------------------------------------------------------


def test_success_returns_content_and_usage():
    result = invoke()
    assert isinstance(result, ReviewResult)
    assert result.content == COMPLETION_TEXT
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 40
    assert result.total_tokens == 160


def test_missing_usage_defaults_to_zero():
    body = json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": COMPLETION_TEXT}}]}
    ).encode()
    factory = make_factory(behavior={"response": FakeResponse(200, body)})
    result = invoke(_connection_factory=factory)
    assert result.content == COMPLETION_TEXT
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (0, 0, 0)


# --- error path: raise for queue retry, never swallow -------------------------


def test_http_500_raises():
    factory = make_factory(behavior={"response": FakeResponse(500, b"boom")})
    with pytest.raises(LlmError):
        invoke(_connection_factory=factory)


def test_http_429_raises():
    factory = make_factory(behavior={"response": FakeResponse(429, b"throttled")})
    with pytest.raises(LlmError):
        invoke(_connection_factory=factory)


def test_connect_timeout_raises():
    factory = make_factory(behavior={"raise_on_connect": TimeoutError("timed out")})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "timeout"


def test_read_timeout_raises():
    factory = make_factory(behavior={"raise_on_response": TimeoutError("read timed out")})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "timeout"


def test_connection_refused_raises():
    factory = make_factory(behavior={"raise_on_connect": ConnectionRefusedError("refused")})
    with pytest.raises(LlmError):
        invoke(_connection_factory=factory)


def test_malformed_json_raises():
    factory = make_factory(behavior={"response": FakeResponse(200, b"{not json")})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "invalid_response"


def test_missing_choices_raises():
    body = json.dumps({"usage": {"total_tokens": 1}}).encode()
    factory = make_factory(behavior={"response": FakeResponse(200, body)})
    with pytest.raises(LlmError):
        invoke(_connection_factory=factory)


def test_empty_content_raises():
    body = json.dumps({"choices": [{"message": {"role": "assistant"}}]}).encode()
    factory = make_factory(behavior={"response": FakeResponse(200, body)})
    with pytest.raises(LlmError):
        invoke(_connection_factory=factory)


def test_non_https_endpoint_rejected():
    with pytest.raises(LlmError):
        invoke(endpoint="http://llm.example.test/v1/chat/completions")


def test_error_carries_machine_readable_class():
    factory = make_factory(behavior={"response": FakeResponse(503, b"unavailable")})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "http_503"


# --- logging: status/duration/token usage ONLY --------------------------------


def test_success_logs_carry_only_status_duration_token_usage(caplog):
    with caplog.at_level(logging.INFO, logger="common.llm"):
        invoke()
    assert caplog.records, "expected at least one log record"
    for record in caplog.records:
        text = record.getMessage()
        assert SYSTEM_PROMPT not in text
        assert DIFF_TEXT not in text
        assert COMPLETION_TEXT not in text
        assert API_KEY not in text
    statuses = [r.__dict__.get("status") for r in caplog.records]
    assert "ok" in statuses
    logged = [r.__dict__ for r in caplog.records if r.__dict__.get("status") == "ok"][0]
    assert isinstance(logged.get("duration_ms"), int)
    assert logged.get("total_tokens") == 160


def test_api_key_never_appears_in_any_log_output(caplog):
    with caplog.at_level(logging.DEBUG, logger="common.llm"):
        invoke()
    assert caplog.records
    for record in caplog.records:
        assert API_KEY not in record.getMessage()
        assert API_KEY not in caplog.text


def test_error_path_logs_llm_error_status(caplog):
    factory = make_factory(behavior={"response": FakeResponse(500, b"boom")})
    with caplog.at_level(logging.INFO, logger="common.llm"):
        with pytest.raises(LlmError):
            invoke(_connection_factory=factory)
    statuses = [r.__dict__.get("status") for r in caplog.records]
    assert "llm_error" in statuses
    for record in caplog.records:
        assert SYSTEM_PROMPT not in record.getMessage()
        assert DIFF_TEXT not in record.getMessage()
        assert API_KEY not in record.getMessage()


def test_module_surface():
    assert llm.TEMPERATURE == 0.2
    assert llm.CONNECT_TIMEOUT_S == 2
    assert llm.READ_TIMEOUT_S == 45
    assert issubclass(llm.LlmError, Exception)

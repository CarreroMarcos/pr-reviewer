"""T028: LLM chat-completions contract tests (HLD §2.3 item 5, §2.7).

Stubbed transport — never network: temperature 0.2 sent, model/endpoint
from injected config, timeouts connect 2 s / read env-configurable
(default 240 s), error path raises for queue retry, logs carry only
status/duration/token usage (never prompt, diff, completion, or key
material).
"""

import json
import logging
from http import client as http_client

import pytest

from common import llm
from common.llm import (
    CONNECT_TIMEOUT_S,
    DEFAULT_READ_TIMEOUT_S,
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
        if self.behavior.get("raise_on_close"):
            raise self.behavior["raise_on_close"]


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
    assert DEFAULT_READ_TIMEOUT_S == 240
    assert llm.READ_TIMEOUT_MIN_S == 30
    assert llm.READ_TIMEOUT_MAX_S == 600


# --- request contract ---------------------------------------------------------


def test_temperature_sent_exactly():
    invoke()
    _, request = last_request()
    body = json.loads(request["body"])
    assert body["temperature"] == 0.2
    assert isinstance(body["temperature"], float)


def test_thinking_disabled_sent_for_glm_model_on_glm_host():
    # GLM reasoning defaults on (~1.3K tokens, ~45s) and busts the ≤15s
    # comment bar (HLD §2.2 (b)) — the payload must pin it off for GLM
    # endpoints (surfaced live by the T035 acceptance run).
    invoke(endpoint="https://api.z.ai/v1/chat/completions")
    _, request = last_request()
    body = json.loads(request["body"])
    assert body["thinking"] == {"type": "disabled"}


def test_thinking_omitted_for_glm_model_on_non_glm_host():
    # Portability gate (SPR-62): the `thinking` key is GLM-specific — a GLM
    # model pointed at a non-GLM host must not receive it.
    invoke()
    _, request = last_request()
    body = json.loads(request["body"])
    assert "thinking" not in body


@pytest.mark.parametrize("model", ["other-model", "gpt-4o-mini", "GLM-5.3-flash"])
def test_thinking_omitted_for_non_glm_model_on_glm_host(model):
    # Gate condition is `model.startswith("glm") AND host in
    # GLM_ALLOWED_HOSTS` — non-GLM models omit the key even on a GLM host
    # (`GLM-5.3-flash` pins the case-sensitive proposal-literal: uppercase
    # prefix does not match).
    invoke(model=model, endpoint="https://api.z.ai/v1/chat/completions")
    _, request = last_request()
    body = json.loads(request["body"])
    assert "thinking" not in body


def test_thinking_sent_for_glm_host_case_insensitive():
    # Host comparison is DNS case-insensitive; the model prefix is literal.
    invoke(endpoint="https://API.Z.AI/v1/chat/completions")
    _, request = last_request()
    body = json.loads(request["body"])
    assert body["thinking"] == {"type": "disabled"}


def test_glm_allowed_hosts_pins_current_stack_host():
    # The gate's host set must cover the deployed stack (terraform
    # compute.tf `GLM_ALLOWED_HOSTS = "api.z.ai"`) so current-stack
    # behavior (thinking disabled) is unchanged.
    assert "api.z.ai" in llm.GLM_ALLOWED_HOSTS


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


def test_read_timeout_defaults_to_240s_after_connect(monkeypatch):
    monkeypatch.delenv("GLM_READ_TIMEOUT_S", raising=False)
    seen = []
    invoke(_connection_factory=make_factory(seen=seen))
    assert seen[0].sock.settimeout_calls == [240]
    assert seen[0].sock.settimeout_calls == [DEFAULT_READ_TIMEOUT_S]


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
    assert llm.DEFAULT_READ_TIMEOUT_S == 240
    assert issubclass(llm.LlmError, Exception)


# --- retryability contract pin (boundary 6; T054/T051 wiring, test-side) --------
# The client emits the error taxonomy; the queue/worker owns retry. Today
# `bad_endpoint` (config/usage fault) and persistent `invalid_response`
# surface as `LlmError` exactly like transient faults — T054/T051 own mapping
# them to NON-retryable. These tests pin CURRENT emission so that flip is a
# conscious diff. No production-behavior edits here.


def test_empty_api_key_is_bad_endpoint_today():
    with pytest.raises(LlmError) as exc_info:
        invoke(api_key="")
    assert exc_info.value.error_class == "bad_endpoint"


def test_empty_model_is_bad_endpoint_today():
    with pytest.raises(LlmError) as exc_info:
        invoke(model="")
    assert exc_info.value.error_class == "bad_endpoint"


def test_non_string_endpoint_is_bad_endpoint_today():
    with pytest.raises(LlmError) as exc_info:
        invoke(endpoint=None)
    assert exc_info.value.error_class == "bad_endpoint"


def test_endpoint_without_host_is_bad_endpoint_today():
    with pytest.raises(LlmError) as exc_info:
        invoke(endpoint="https:///no-host-here")
    assert exc_info.value.error_class == "bad_endpoint"


def test_endpoint_query_string_reaches_request_path():
    invoke(endpoint="https://llm.example.test/v1/chat/completions?api-version=2026-03")
    _, request = last_request()
    assert request["path"] == "/v1/chat/completions?api-version=2026-03"


def test_non_dict_body_is_invalid_response_today():
    body = json.dumps(["not", "a", "dict"]).encode()
    factory = make_factory(behavior={"response": FakeResponse(200, body)})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "invalid_response"


def test_choices_not_a_list_is_invalid_response_today():
    body = json.dumps({"choices": {"message": {"content": COMPLETION_TEXT}}}).encode()
    factory = make_factory(behavior={"response": FakeResponse(200, body)})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "invalid_response"


def test_non_dict_message_is_invalid_response_today():
    body = json.dumps({"choices": ["just-a-string"]}).encode()
    factory = make_factory(behavior={"response": FakeResponse(200, body)})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "invalid_response"


def test_non_string_content_is_invalid_response_today():
    body = json.dumps({"choices": [{"message": {"content": 12345}}]}).encode()
    factory = make_factory(behavior={"response": FakeResponse(200, body)})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "invalid_response"


def test_non_dict_usage_defaults_tokens_to_zero():
    body = json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": COMPLETION_TEXT}}],
            "usage": [120, 40, 160],
        }
    ).encode()
    factory = make_factory(behavior={"response": FakeResponse(200, body)})
    result = invoke(_connection_factory=factory)
    assert (result.prompt_tokens, result.completion_tokens, result.total_tokens) == (0, 0, 0)


def test_request_phase_timeout_is_timeout_today():
    factory = make_factory(behavior={"raise_on_request": TimeoutError("read timed out")})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "timeout"


def test_request_phase_os_error_is_connection_error_today():
    factory = make_factory(behavior={"raise_on_request": OSError("reset")})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "connection_error"


def test_factory_failure_is_connection_error_today():
    def failing_factory(host, port, *, timeout):
        raise OSError("factory down")

    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=failing_factory)
    assert exc_info.value.error_class == "connection_error"


def test_http_400_carries_machine_readable_class():
    factory = make_factory(behavior={"response": FakeResponse(400, b"bad request")})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "http_400"


def test_close_failure_never_masks_success():
    factory = make_factory(behavior={"raise_on_close": RuntimeError("close boom")})
    result = invoke(_connection_factory=factory)
    assert result.content == COMPLETION_TEXT


# --- SPR-58: typed invalid_key closure ----------------------------------------
# Live incident: a trailing-newline API key crashed invisibly (bare
# ValueError from `http.client.putheader`, whose message embeds the full
# `Bearer <API KEY>` value) and spun to the DLQ. These pins close that
# hole: construction faults surface as typed `LlmError("invalid_key")`
# with no key material in the message, and the escape probe pins the
# realistic transport-fault inventory (no blanket `except Exception` in
# product code).


def _validating_factory(seen=None):
    """Factory whose `request()` runs the REAL stdlib header validation
    before delegating to the stub — replays what production `putheader`
    does with control-char key material, without any network."""

    def factory(host, port, *, timeout):
        conn = FakeConnection(host, port, timeout=timeout)
        if seen is not None:
            seen.append(conn)
        orig_request = conn.request

        def request(method, path, body=None, headers=None):
            probe = http_client.HTTPConnection(host)
            probe.putrequest(method, path)
            for name, value in (headers or {}).items():
                probe.putheader(name, value)
            return orig_request(method, path, body=body, headers=headers)

        conn.request = request
        return conn

    return factory


@pytest.mark.parametrize("suffix", ["\n", "\r", "\r\n"])
def test_newline_suffixed_key_is_invalid_key(suffix):
    """Trailing-newline key (the live incident) → typed `invalid_key`;
    the key never appears in the exception message."""
    key = API_KEY + suffix
    with pytest.raises(LlmError) as exc_info:
        invoke(api_key=key, _connection_factory=_validating_factory())
    assert exc_info.value.error_class == "invalid_key"
    assert key not in str(exc_info.value)
    assert API_KEY not in str(exc_info.value)


def test_embedded_crlf_key_is_invalid_key():
    """Control characters anywhere in the key → `invalid_key`, key-free."""
    key = "prefix\r\ninjected: evil"
    with pytest.raises(LlmError) as exc_info:
        invoke(api_key=key, _connection_factory=_validating_factory())
    assert exc_info.value.error_class == "invalid_key"
    assert key not in str(exc_info.value)


def test_putheader_style_value_error_maps_to_invalid_key_without_key_material():
    """Seam-level pin: a putheader-shaped ValueError (message embedding the
    full `Bearer <key>` value, as CPython raises) → `invalid_key`, and the
    embedded key material is dropped (`from None`, fixed message)."""
    key = "glm-test-probe-not-a-credential"  # noqa: S105 (dummy fixture)
    factory = make_factory(
        behavior={"raise_on_request": ValueError(f"Invalid header value b'Bearer {key}'")}
    )
    with pytest.raises(LlmError) as exc_info:
        invoke(api_key=key, _connection_factory=factory)
    assert exc_info.value.error_class == "invalid_key"
    assert key not in str(exc_info.value)


@pytest.mark.parametrize("key", ["   ", " \t ", "\n", " \r\n "])
def test_whitespace_only_key_is_invalid_key(key):
    """Boundary pin: whitespace-only keys are truthy (pass the empty-key
    guard) but carry no credential — `invalid_key`, never the wire."""
    with pytest.raises(LlmError) as exc_info:
        invoke(api_key=key)
    assert exc_info.value.error_class == "invalid_key"


def test_empty_key_stays_bad_endpoint():
    """Boundary pin (unchanged): empty/missing keys stay `bad_endpoint`."""
    with pytest.raises(LlmError) as exc_info:
        invoke(api_key="")
    assert exc_info.value.error_class == "bad_endpoint"


def test_bad_status_line_is_connection_error():
    factory = make_factory(
        behavior={"raise_on_response": http_client.BadStatusLine("HTTP/1.1 ???")}
    )
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "connection_error"


def test_incomplete_read_is_connection_error():
    factory = make_factory(
        behavior={"raise_on_response": http_client.IncompleteRead(partial=b'{"cho', expected=200)}
    )
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "connection_error"


def test_remote_disconnected_stays_connection_error_via_os_error():
    """`RemoteDisconnected` subclasses both `OSError` and `HTTPException`;
    the pre-existing `OSError` arm owns it (no double-map)."""
    factory = make_factory(behavior={"raise_on_response": http_client.RemoteDisconnected("closed")})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == "connection_error"


@pytest.mark.parametrize(
    ("fault", "expected_class"),
    [
        (
            ValueError("Invalid header value b'Bearer SECRET-PROBE'"),
            "invalid_key",
        ),
        (http_client.BadStatusLine("HTTP/1.1 ???"), "connection_error"),
        (
            http_client.IncompleteRead(partial=b'{"cho', expected=200),
            "connection_error",
        ),
        # `socket.timeout` is an alias of `TimeoutError` (3.10+): one row pins both.
        (TimeoutError("timed out"), "timeout"),
        (ConnectionResetError("reset by peer"), "connection_error"),
    ],
)
def test_transport_fault_inventory_never_escapes_bare(fault, expected_class):
    """Escape probe: every realistic transport fault injected through the
    factory seam surfaces as a typed `LlmError` — never a bare fault
    (which would bypass the worker's retry/notice classification)."""
    factory = make_factory(behavior={"raise_on_request": fault})
    with pytest.raises(LlmError) as exc_info:
        invoke(_connection_factory=factory)
    assert exc_info.value.error_class == expected_class
    assert "SECRET-PROBE" not in str(exc_info.value)

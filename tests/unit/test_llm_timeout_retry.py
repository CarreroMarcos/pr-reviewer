"""LLM read-timeout configurability + one budgeted in-executor retry.

`common.llm._read_timeout_s` resolves `GLM_READ_TIMEOUT_S` at request
time (default 240, clamped to [30, 600]); `worker_handler._call_llm`
re-invokes EXACTLY ONCE on `LlmError("timeout")` when the remaining
Lambda budget covers read_timeout + 90 s headroom — immediately, no
sleep. Every other error class keeps current behavior.
"""

import json
import logging

import pytest

from common import llm
from common.config import ConfigProvider
from common.diff import HttpResponse
from common.envelope import Envelope
from common.llm import LlmError, _read_timeout_s, review_diff
from worker_handler import _Credentials, _make_review

API_KEY = "glm-test-key-not-a-credential"  # noqa: S105 (dummy fixture)
MODEL = "glm-5.3-flash"
ENDPOINT = "https://llm.example.test/v1/chat/completions"

GITHUB_TOKEN_VALUE = "ghp-test-token-value"  # noqa: S105 (fake fixture)
GLM_API_KEY_VALUE = "glm-key-value"  # noqa: S105 (fake fixture)

REVIEW_BODY = (
    "## Summary\nAdds input validation.\n\n"
    "## Findings\n- [HIGH] `src/main.py:12` — Missing bound. Fix: add a check.\n\n"
    "## Risk Notes\nNone.\n"
)


def completion_body(content=REVIEW_BODY):
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode()


class FakeSocket:
    def __init__(self):
        self.settimeout_calls = []

    def settimeout(self, seconds):
        self.settimeout_calls.append(seconds)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeConnection:
    def __init__(self, host, port, *, timeout, behavior=None, seen=None):
        self.sock = FakeSocket()
        self.behavior = behavior or {}
        if seen is not None:
            seen.append(self)

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        if self.behavior.get("raise_on_request"):
            raise self.behavior["raise_on_request"]

    def getresponse(self):
        if self.behavior.get("raise_on_response"):
            raise self.behavior["raise_on_response"]
        return self.behavior.get("response", FakeResponse(200, completion_body()))

    def close(self):
        pass


def invoke_llm(**overrides):
    params = {
        "api_key": API_KEY,
        "model": MODEL,
        "endpoint": ENDPOINT,
        "system_prompt": "SYSTEM-PROMPT",
        "diff_text": "diff --git a/foo.py b/foo.py",
    }
    params.update(overrides)
    return review_diff(**params)


def read_timeout_seen(monkeypatch, env_value):
    """Run one stubbed call and return the socket read timeout applied."""
    if env_value is None:
        monkeypatch.delenv("GLM_READ_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("GLM_READ_TIMEOUT_S", env_value)
    seen = []

    def factory(host, port, *, timeout):
        return FakeConnection(host, port, timeout=timeout, seen=seen)

    invoke_llm(_connection_factory=factory)
    assert len(seen) == 1
    assert len(seen[0].sock.settimeout_calls) == 1
    return seen[0].sock.settimeout_calls[0]


# --- request-time env resolution ---------------------------------------------


def test_read_timeout_absent_is_default_240(monkeypatch):
    assert read_timeout_seen(monkeypatch, None) == 240
    # Env still absent under the same monkeypatch: direct helper agrees.
    assert _read_timeout_s() == 240


def test_read_timeout_env_override_honored(monkeypatch):
    assert read_timeout_seen(monkeypatch, "120") == 120


@pytest.mark.parametrize("raw", ["", "   ", "bogus", "12x", "45.5", "None"])
def test_read_timeout_invalid_falls_back_to_default(monkeypatch, raw):
    assert read_timeout_seen(monkeypatch, raw) == 240


@pytest.mark.parametrize(("raw", "expected"), [("5", 30), ("29", 30), ("30", 30)])
def test_read_timeout_clamped_to_minimum(monkeypatch, raw, expected):
    assert read_timeout_seen(monkeypatch, raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [("600", 600), ("601", 600), ("3600", 600)])
def test_read_timeout_clamped_to_maximum(monkeypatch, raw, expected):
    assert read_timeout_seen(monkeypatch, raw) == expected


def test_read_timeout_resolved_per_request_not_import_time(monkeypatch):
    """Two calls with different env values observe different budgets."""
    assert read_timeout_seen(monkeypatch, "60") == 60
    assert read_timeout_seen(monkeypatch, "300") == 300
    assert read_timeout_seen(monkeypatch, None) == 240


def test_read_timeout_constants_document_default():
    assert llm.DEFAULT_READ_TIMEOUT_S == 240
    assert llm.READ_TIMEOUT_MIN_S == 30
    assert llm.READ_TIMEOUT_MAX_S == 600
    assert llm.READ_TIMEOUT_ENV_VAR == "GLM_READ_TIMEOUT_S"


# --- worker single-pass retry -------------------------------------------------


def _ssm_values():
    return {
        "/pr-reviewer/github-token": GITHUB_TOKEN_VALUE,
        "/pr-reviewer/webhook-secret": "webhook-secret-value",  # noqa: S105 (fake fixture)
        "/pr-reviewer/glm-api-key": GLM_API_KEY_VALUE,
        "/pr-reviewer/glm-model": MODEL,
        "/pr-reviewer/glm-endpoint": ENDPOINT,
    }


class _FakeSSM:
    def __init__(self, values):
        self.values = dict(values)

    def get_parameters(self, Names, WithDecryption=False):  # noqa: N803 (boto3 shape)
        params = [
            {"Name": name, "Value": self.values[name]} for name in Names if name in self.values
        ]
        invalid = [name for name in Names if name not in self.values]
        return {"Parameters": params, "InvalidParameters": invalid}


class _FakeDiffTransport:
    def __init__(self, *, sha):
        self._sha = sha

    def __call__(self, url, headers):
        if "/files" in url:
            body = json.dumps(
                [
                    {
                        "filename": "src/main.py",
                        "additions": 5,
                        "deletions": 2,
                        "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n",
                    }
                ]
            ).encode()
            return HttpResponse(status=200, body=body, headers={})
        return HttpResponse(
            status=200, body=json.dumps({"head": {"sha": self._sha}}).encode(), headers={}
        )


class _ScriptedLLMConnection:
    """One connection per LLM invocation; script items are consumed in order:
    `("raise", exc)` or `("response", status, body)`."""

    def __init__(self, script, seen):
        self._script = script
        self.sock = FakeSocket()
        seen.append(self)

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        pass

    def getresponse(self):
        action = self._script.pop(0)
        if action[0] == "raise":
            raise action[1]
        return FakeResponse(action[1], action[2])

    def close(self):
        pass


def _run_review(*, script, remaining_ms, caplog=None, clock=None):
    """Drive `_make_review().review()`; return (content, invocations, retry_logs)."""
    ssm = _FakeSSM(_ssm_values())
    provider = ConfigProvider(
        ssm.get_parameters,
        clock=lambda: 1_750_000_000,
        allowed_endpoint_hosts=("llm.example.test",),
    )
    seen = []
    script = list(script)

    def factory(host, port, *, timeout):
        return _ScriptedLLMConnection(script, seen)

    kwargs = {}
    if remaining_ms is not None:
        kwargs["remaining_time_ms"] = lambda: remaining_ms
    review = _make_review(
        envelope=Envelope(
            envelope_version="v1",
            event_type="pull_request",
            action="opened",
            repo_full_name="octo-org/hello-world",
            pr_number=42,
            head_sha="bb" * 20,
            base_sha="00" * 20,
            sender="octo-user",
            delivery_guid="11111111-1111-4111-8111-111111111111",
        ),
        creds=_Credentials(provider),
        usage={"tokens": 0},
        diff_transport=_FakeDiffTransport(sha="bb" * 20),
        llm_factory=factory,
        system_prompt="SYSTEM-PROMPT",
        clock=clock if clock is not None else (lambda: 1_750_000_000),
        **kwargs,
    )
    if caplog is not None:
        with caplog.at_level(logging.WARNING, logger="worker_handler"):
            content = review("bb" * 20, 0)
    else:
        content = review("bb" * 20, 0)
    return content, seen


def _retry_logs(caplog):
    return [r for r in caplog.records if r.__dict__.get("status") == "llm_timeout_retry"]


def _ok_script():
    return [("response", 200, completion_body())]


def _timeout():
    return ("raise", TimeoutError("read timed out"))


AMPLE_MS = (240 + 90) * 1000 + 60_000  # comfortably above default gate


def test_timeout_then_success_returns_after_two_invocations(caplog):
    content, seen = _run_review(
        script=[_timeout(), *_ok_script()], remaining_ms=AMPLE_MS, caplog=caplog
    )
    assert REVIEW_BODY.strip() in content
    assert len(seen) == 2
    lines = _retry_logs(caplog)
    assert len(lines) == 1
    extra = lines[0].__dict__
    assert extra["backoff_ms"] == 0
    assert extra["attempt"] == 2
    assert extra["error_class"] == "timeout"
    assert extra["repo_full_name"] == "octo-org/hello-world"
    assert extra["pr_number"] == 42
    assert isinstance(extra["duration_ms"], int)
    assert GLM_API_KEY_VALUE not in caplog.text


def test_timeout_with_insufficient_remaining_time_does_not_retry(caplog):
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        with pytest.raises(LlmError) as exc_info:
            _run_review(script=[_timeout()], remaining_ms=1_000)
    assert exc_info.value.error_class == "timeout"
    assert _retry_logs(caplog) == []


def test_timeout_at_exact_budget_boundary_retries():
    """`remaining == read_timeout + headroom` (ms) is sufficient (`>=`)."""
    content, seen = _run_review(script=[_timeout(), *_ok_script()], remaining_ms=(240 + 90) * 1000)
    assert REVIEW_BODY.strip() in content
    assert len(seen) == 2


def test_two_consecutive_timeouts_raise_after_exactly_two_invocations(caplog):
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        with pytest.raises(LlmError) as exc_info:
            _run_review(script=[_timeout(), _timeout()], remaining_ms=AMPLE_MS)
    assert exc_info.value.error_class == "timeout"
    assert len(_retry_logs(caplog)) == 1  # fired once; the retry itself raised


def test_timeout_without_remaining_time_source_does_not_retry():
    """Fail-closed default: older callers (no `remaining_time_ms`) keep
    today's queue-redelivery behavior — one invocation, then raise."""
    with pytest.raises(LlmError) as exc_info:
        _run_review(script=[_timeout()], remaining_ms=None)
    assert exc_info.value.error_class == "timeout"


def test_non_timeout_error_does_not_retry():
    with pytest.raises(LlmError) as exc_info:
        _run_review(script=[("response", 429, b"throttled"), *_ok_script()], remaining_ms=AMPLE_MS)
    assert exc_info.value.error_class == "http_429"


def test_401_then_success_arm_still_works(caplog):
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        content, seen = _run_review(
            script=[("response", 401, b"{}"), *_ok_script()], remaining_ms=AMPLE_MS
        )
    assert REVIEW_BODY.strip() in content
    assert len(seen) == 2
    assert _retry_logs(caplog) == []  # no timeout involved → no timeout retry


def test_401_then_success_emits_no_retry_log_and_reanchors_clock(caplog):
    """Happy-path 401 arm (re-fetch succeeds, no timeout): the worker
    emits NO retry/duration record — the only duration-bearing log on
    this seam is `llm_timeout_retry`, which is timeout-specific (the LLM
    module's own `llm_review` warning for the 401 response lives in
    `common.llm`, out of scope here). The re-anchored attempt clock is
    still consumed exactly once more (the comment timestamp): three clock
    reads total — original anchor, re-anchor after the 401 refresh, and
    the comment timestamp — proving the re-anchor took place. Old code
    (no re-anchor) would consume only two reads, failing loudly."""
    ticks = iter([100.0, 150.0, 175.0])
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        content, seen = _run_review(
            script=[("response", 401, b"{}"), *_ok_script()],
            remaining_ms=AMPLE_MS,
            clock=lambda: next(ticks),
        )
    assert REVIEW_BODY.strip() in content
    assert len(seen) == 2  # the 401 + the successful re-fetch; no retry
    assert _retry_logs(caplog) == []
    with pytest.raises(StopIteration):
        next(ticks)  # exactly three reads: anchor, re-anchor, comment ts


def test_401_then_timeout_composes_one_retry_per_class(caplog):
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        content, seen = _run_review(
            script=[("response", 401, b"{}"), _timeout(), *_ok_script()],
            remaining_ms=AMPLE_MS,
        )
    assert REVIEW_BODY.strip() in content
    assert len(seen) == 3
    assert len(_retry_logs(caplog)) == 1


def test_401_then_timeout_duration_covers_only_the_timed_out_attempt(caplog):
    """Scripted advancing clock, 401 → timeout → retry fires: the
    `llm_timeout_retry` line's `duration_ms` is anchored at the REFRESHED
    attempt (re-anchored after the 401 re-fetch), so it measures only the
    attempt that timed out — never the 401 round trip + attempt total
    (Gate #88 advisory A1).

    Clock reads inside `_call_llm`: (1) original anchor 100.0 s, (2)
    re-anchor 200.0 s after the 401 refresh, (3) attempt-window close
    212.5 s → 12_500 ms. A fourth read happens later (comment timestamp);
    300.0 s remains unconsumed, proving no extra reads stretched the
    window."""
    ticks = iter([100.0, 200.0, 212.5, 300.0])
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        content, seen = _run_review(
            script=[("response", 401, b"{}"), _timeout(), *_ok_script()],
            remaining_ms=AMPLE_MS,
            clock=lambda: next(ticks),
        )
    assert REVIEW_BODY.strip() in content
    assert len(seen) == 3  # 401 + timed-out attempt + successful retry
    (line,) = _retry_logs(caplog)
    assert line.__dict__["duration_ms"] == 12_500


def test_401_then_two_timeouts_raise_after_three_invocations(caplog):
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        with pytest.raises(LlmError) as exc_info:
            _run_review(
                script=[("response", 401, b"{}"), _timeout(), _timeout()],
                remaining_ms=AMPLE_MS,
            )
    assert exc_info.value.error_class == "timeout"
    assert len(_retry_logs(caplog)) == 1


def test_retry_gate_uses_request_time_read_timeout(monkeypatch, caplog):
    """Same remaining budget retries under a small env read timeout but
    not under the default — the gate resolves `_read_timeout_s()` live."""
    monkeypatch.setenv("GLM_READ_TIMEOUT_S", "30")
    with caplog.at_level(logging.WARNING, logger="worker_handler"):
        content, seen = _run_review(
            script=[_timeout(), *_ok_script()], remaining_ms=(30 + 90) * 1000
        )
    assert REVIEW_BODY.strip() in content
    assert len(seen) == 2

    monkeypatch.delenv("GLM_READ_TIMEOUT_S", raising=False)
    with pytest.raises(LlmError):
        _run_review(script=[_timeout()], remaining_ms=(30 + 90) * 1000)

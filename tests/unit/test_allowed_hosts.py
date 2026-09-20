"""Audit R3: env-configured GLM allowed hosts threaded into `review_diff`.

The thinking portability gate in `common.llm` matched only the hardcoded
module constant — the worker's env-configured set never reached it (values
matched live, so no outage; this closes the config↔code drift and Gate-17
A2). `ConfigProvider.allowed_hosts` (public accessor over the lowercased
ingest set) is now threaded `_process_record → _make_review → _call_llm →
review_diff(allowed_hosts=...)`; `None` resolves to `GLM_ALLOWED_HOSTS` so
unset-env behavior is byte-identical.
"""

import json

from common.config import ConfigProvider
from common.diff import HttpResponse
from common.envelope import Envelope
from common.llm import GLM_ALLOWED_HOSTS, review_diff
from worker_handler import _Credentials, _make_review

HOST = "llm.example.test"
ENDPOINT = f"https://{HOST}/v1/chat/completions"
GLM_ENDPOINT = "https://api.z.ai/v1/chat/completions"

GITHUB_TOKEN_VALUE = "ghp-test-token-value"  # noqa: S105 (fake fixture)
GLM_API_KEY_VALUE = "glm-key-value"  # noqa: S105 (fake fixture)

REVIEW_BODY = (
    "## Summary\nAdds input validation.\n\n"
    "## Findings\n- [HIGH] `src/main.py:12` — Missing bound. Fix: add a check.\n\n"
    "## Risk Notes\nNone.\n"
)


def _ssm_values(*, model="glm-5.3-flash", endpoint=ENDPOINT):
    return {
        "/pr-reviewer/github-token": GITHUB_TOKEN_VALUE,
        "/pr-reviewer/webhook-secret": "webhook-secret-value",  # noqa: S105 (fake fixture)
        "/pr-reviewer/glm-api-key": GLM_API_KEY_VALUE,
        "/pr-reviewer/glm-model": model,
        "/pr-reviewer/glm-endpoint": endpoint,
    }


class _FakeSSM:
    """Injected SSM double mirroring the real GetParameters response shape."""

    def __init__(self, values):
        self.values = dict(values)

    def get_parameters(self, Names, WithDecryption=False):  # noqa: N803 (boto3 shape)
        params = [
            {"Name": name, "Value": self.values[name]} for name in Names if name in self.values
        ]
        invalid = [name for name in Names if name not in self.values]
        return {"Parameters": params, "InvalidParameters": invalid}


class _FakeDiffTransport:
    """Minimal diff double: PR-meta → fixed head sha, files page → one file."""

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


class _BodyRecordingConnection:
    """LLM stub behind the `_connection_factory` seam; captures the payload."""

    def __init__(self, seen):
        self._seen = seen
        self.sock = _FakeSocket()

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        self._seen.append(json.loads(body))

    def getresponse(self):
        body = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": REVIEW_BODY}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        ).encode()
        return _FakeResponse(200, body)

    def close(self):
        pass


class _FakeSocket:
    def settimeout(self, seconds):
        pass


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


def _llm_payload(*, hosts, model="glm-5.3-flash", endpoint=ENDPOINT, allowed_hosts=...):
    """Run `_make_review().review()` end-to-end and return the LLM payload.

    `allowed_hosts=...` (default) threads the provider's configured set —
    the production `_process_record` wiring; pass an explicit value
    (including `None`) to override it.
    """
    ssm = _FakeSSM(_ssm_values(model=model, endpoint=endpoint))
    provider = ConfigProvider(
        ssm.get_parameters, clock=lambda: 1_750_000_000, allowed_endpoint_hosts=hosts
    )
    seen: list = []

    def factory(host, port, *, timeout):
        return _BodyRecordingConnection(seen)

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
        **(
            {"allowed_hosts": provider.allowed_hosts}
            if allowed_hosts is ...
            else {"allowed_hosts": allowed_hosts}
        ),
    )
    review("bb" * 20, 0)
    assert len(seen) == 1
    return seen[0]


def _llm_request(**overrides):
    """Direct `review_diff` call capturing the wire payload (llm-level pin)."""
    seen: list = []

    def factory(host, port, *, timeout):
        return _BodyRecordingConnection(seen)

    params = {
        "api_key": GLM_API_KEY_VALUE,
        "model": "glm-5.3-flash",
        "endpoint": GLM_ENDPOINT,
        "system_prompt": "SYSTEM-PROMPT",
        "diff_text": "diff --git a/foo.py b/foo.py",
        "_connection_factory": factory,
    }
    params.update(overrides)
    review_diff(**params)
    assert len(seen) == 1
    return seen[0]


# --- (b) config accessor: public, ingest-lowercased ---------------------------


def test_allowed_hosts_accessor_returns_lowercased_env_set():
    provider = ConfigProvider(
        _FakeSSM(_ssm_values(endpoint="https://api.z.ai/v1")).get_parameters,
        clock=lambda: 1_750_000_000,
        allowed_endpoint_hosts=("API.Z.AI", "Other.Example.Test"),
    )
    assert provider.allowed_hosts == frozenset({"api.z.ai", "other.example.test"})


def test_allowed_hosts_accessor_defaults_to_empty():
    provider = ConfigProvider(_FakeSSM(_ssm_values()).get_parameters, clock=lambda: 1_750_000_000)
    assert provider.allowed_hosts == frozenset()


# --- (a) threading: configured set reaches the gate --------------------------


def test_worker_configured_set_with_custom_host_sends_thinking():
    """Configured set threads through: a GLM model on a custom configured
    host sends `thinking: disabled` — impossible under the module default
    (`api.z.ai` only), so this proves the worker's set reached the gate."""
    payload = _llm_payload(hosts=(HOST,))
    assert payload["thinking"] == {"type": "disabled"}


def test_review_diff_explicit_set_without_endpoint_omits_thinking():
    """Explicit set WITHOUT the endpoint host flips thinking off."""
    payload = _llm_request(allowed_hosts=frozenset({"other.example.test"}))
    assert "thinking" not in payload


def test_review_diff_explicit_set_with_endpoint_sends_thinking():
    """Explicit set WITH the endpoint host flips thinking on."""
    payload = _llm_request(allowed_hosts=frozenset({"api.z.ai"}))
    assert payload["thinking"] == {"type": "disabled"}


# --- (c) default: no env → module constant, behavior unchanged ----------------


def test_review_diff_default_param_uses_module_constant():
    """No `allowed_hosts` (and explicit `None`) → module default: GLM host
    sends thinking, non-GLM host omits it — pre-R3 behavior pinned."""
    assert _llm_request()["thinking"] == {"type": "disabled"}
    assert _llm_request(allowed_hosts=None)["thinking"] == {"type": "disabled"}
    assert "thinking" not in _llm_request(endpoint=ENDPOINT)
    assert GLM_ALLOWED_HOSTS == frozenset({"api.z.ai"})


def test_worker_default_none_keeps_module_gate():
    """`_make_review` without the threaded set keeps the module gate: GLM
    model on a non-constant host omits thinking (default path unchanged)."""
    payload = _llm_payload(hosts=(HOST,), allowed_hosts=None)
    assert "thinking" not in payload

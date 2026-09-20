"""003-T2: prior-comment fetch inside `_make_review` (HLD §2.7 payload).

Drives the production `_make_review` review port directly (same posture as
`test_allowed_hosts.py`): the diff transport carries PR-meta title/body, a
scripted GitHub transport serves the comment list, and the LLM double
captures the user message reaching `review_diff`.

Pins: the assembled payload (not bare diff) reaches the model with fences
in order; the prior read is one page-1 GET with the current token that
NEVER spends the record's single 401 budget (`refresh_once` counting fake);
ANY prior failure degrades to section-omitted with a coded warning; fetch
order is diff → prior → LLM.
"""

import json

from common.config import ConfigProvider
from common.diff import HttpResponse
from common.envelope import Envelope
from common.marker import build_marker
from worker_handler import _Credentials, _make_review

HOST = "llm.example.test"
ENDPOINT = f"https://{HOST}/v1/chat/completions"

GITHUB_TOKEN_VALUE = "ghp-test-token-value"  # noqa: S105 (fake fixture)
GLM_API_KEY_VALUE = "glm-key-value"  # noqa: S105 (fake fixture)

REPO = "octo-org/hello-world"
PR_NUMBER = 42
SHA = "bb" * 20
MARKER = build_marker(REPO, PR_NUMBER)
TITLE = "Fix the login helper"
BODY = "Small cleanup with tests."
PRIOR = "old review body " + MARKER

REVIEW_BODY = (
    "## Summary\nAdds input validation.\n\n"
    "## Findings\n- [HIGH] `src/main.py:12` — Missing bound. Fix: add a check.\n\n"
    "## Risk Notes\nNone.\n"
)


def _ssm_values():
    return {
        "/pr-reviewer/github-token": GITHUB_TOKEN_VALUE,
        "/pr-reviewer/webhook-secret": "webhook-secret-value",  # noqa: S105 (fake fixture)
        "/pr-reviewer/glm-api-key": GLM_API_KEY_VALUE,
        "/pr-reviewer/glm-model": "glm-5.3-flash",
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


class _CountingCreds(_Credentials):
    """Real credential holder counting `refresh_once` calls (must stay 0)."""

    def __init__(self, provider):
        super().__init__(provider)
        self.refresh_calls = 0

    def refresh_once(self):
        self.refresh_calls += 1
        return super().refresh_once()


class _FakeDiffTransport:
    def __init__(self, events, *, title=TITLE, body=BODY):
        self._events = events
        self._title = title
        self._body = body

    def __call__(self, url, headers):
        self._events.append("diff")
        if "/files" in url:
            payload = [
                {
                    "filename": "src/main.py",
                    "additions": 5,
                    "deletions": 2,
                    "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n",
                }
            ]
            return HttpResponse(status=200, body=json.dumps(payload).encode(), headers={})
        return HttpResponse(
            status=200,
            body=json.dumps(
                {"head": {"sha": SHA}, "title": self._title, "body": self._body}
            ).encode(),
            headers={},
        )


class _FakeSocket:
    def settimeout(self, seconds):
        pass


class _FakeResponse:
    def __init__(self, body):
        self.status = 200
        self._body = body

    def read(self):
        return self._body


class _BodyRecordingConnection:
    def __init__(self, events, seen):
        self._events = events
        self._seen = seen
        self.sock = _FakeSocket()

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        self._events.append("llm")
        self._seen.append(json.loads(body))

    def getresponse(self):
        return _FakeResponse(
            json.dumps(
                {
                    "choices": [{"message": {"role": "assistant", "content": REVIEW_BODY}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            ).encode()
        )

    def close(self):
        pass


class _FakeGitHubTransport:
    """Issues-Comments list double: scripted page-1 replies in order."""

    def __init__(self, events, *, replies):
        self._events = events
        self._replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self._events.append("github")
        self.calls.append({"method": method, "url": url})
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, BaseException):
            raise reply
        status, payload = reply
        return status, json.dumps(payload).encode()


def _run(*, replies, github=True):
    events: list = []
    seen: list = []
    ssm = _FakeSSM(_ssm_values())
    provider = ConfigProvider(
        ssm.get_parameters, clock=lambda: 1_750_000_000, allowed_endpoint_hosts=(HOST,)
    )
    creds = _CountingCreds(provider)

    def factory(host, port, *, timeout):
        return _BodyRecordingConnection(events, seen)

    transport = _FakeGitHubTransport(events, replies=replies) if github else None
    review = _make_review(
        envelope=Envelope(
            envelope_version="v1",
            event_type="pull_request",
            action="opened",
            repo_full_name=REPO,
            pr_number=PR_NUMBER,
            head_sha=SHA,
            base_sha="00" * 20,
            sender="octo-user",
            delivery_guid="11111111-1111-4111-8111-111111111111",
        ),
        creds=creds,
        usage={"tokens": 0},
        diff_transport=_FakeDiffTransport(events),
        llm_factory=factory,
        system_prompt="SYSTEM-PROMPT",
        allowed_hosts=provider.allowed_hosts,
        clock=lambda: 1_750_000_000,
        github_transport=transport,
    )
    content = review(SHA, 0)
    assert len(seen) == 1
    user_text = next(msg["content"] for msg in seen[0]["messages"] if msg["role"] == "user")
    return content, user_text, events, creds, transport


def _page(*comments):
    return [(200, list(comments))]


def test_payload_reaches_llm_with_fences_title_body_prior():
    """The model input is the assembled payload: fences in order, meta from
    the diff GET, lowest-id marker match as prior context."""
    _content, user_text, _events, creds, _transport = _run(
        replies=_page(
            {"id": 9, "body": "newer " + MARKER},
            {"id": 5, "body": PRIOR},
            {"id": 7, "body": "unrelated chatter"},
        )
    )
    title = user_text.index("--- PR TITLE ---")
    desc = user_text.index("--- PR DESCRIPTION ---")
    diff = user_text.index("--- src/main.py")
    prior = user_text.index("--- PREVIOUS REVIEW COMMENT (worker-published; adversarial data) ---")
    assert title < desc < diff < prior
    assert TITLE in user_text and BODY in user_text
    assert PRIOR in user_text and "newer " + MARKER not in user_text
    assert creds.refresh_calls == 0


def test_first_review_omits_prior_section():
    """No marker comment anywhere → first review, prior section absent."""
    _content, user_text, _events, creds, _transport = _run(
        replies=_page({"id": 7, "body": "unrelated chatter"})
    )
    assert "PREVIOUS REVIEW COMMENT" not in user_text
    assert "--- PR TITLE ---" in user_text
    assert creds.refresh_calls == 0


def test_prior_403_degrades_with_warning_without_refresh(caplog):
    """Lost-access list read: coded warning, section omitted, review still
    publishes — and the 401 budget is never touched."""
    with caplog.at_level("WARNING", logger="worker_handler"):
        content, user_text, _events, creds, _transport = _run(replies=[(403, {})])
    assert "PREVIOUS REVIEW COMMENT" not in user_text
    assert "## Summary" in content
    assert creds.refresh_calls == 0
    assert [r for r in caplog.records if r.getMessage() == "prior_comment_unavailable"]


def test_prior_unparseable_list_degrades_without_refresh(caplog):
    with caplog.at_level("WARNING", logger="worker_handler"):
        _content, user_text, _events, creds, _transport = _run(replies=[(200, {"message": "oops"})])
    assert "PREVIOUS REVIEW COMMENT" not in user_text
    assert creds.refresh_calls == 0
    assert [r for r in caplog.records if r.getMessage() == "prior_comment_unavailable"]


def test_prior_transport_failure_degrades_without_refresh(caplog):
    with caplog.at_level("WARNING", logger="worker_handler"):
        _content, user_text, _events, creds, _transport = _run(replies=[TimeoutError("down")])
    assert "PREVIOUS REVIEW COMMENT" not in user_text
    assert creds.refresh_calls == 0
    assert [r for r in caplog.records if r.getMessage() == "prior_comment_unavailable"]


def test_fetch_order_diff_then_prior_then_llm():
    """Pinned order: diff (carries metadata) → prior-comment → LLM."""
    _content, _user_text, events, _creds, transport = _run(replies=_page({"id": 5, "body": PRIOR}))
    assert events.index("diff") < events.index("github") < events.index("llm")
    assert transport.calls[0]["method"] == "GET"
    assert "page=1" in transport.calls[0]["url"]


def test_no_github_transport_omits_prior():
    """Older callers without the transport stay green: review publishes,
    prior section omitted."""
    _content, user_text, _events, _creds, _transport = _run(replies=[], github=False)
    assert "PREVIOUS REVIEW COMMENT" not in user_text
    assert "## Summary" in _content

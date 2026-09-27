"""003-T1: review-header pipeline contract.

Drives the production worker `handler` end to end (injected fakes, fixed
clocks) and pins the canonical-comment header behavior:

* marker-first ordering with the worker-injected header directly under it;
* `#1` on the first review (generation 0 + 1);
* same-SHA replay keeps `#N` with a FRESH stamp (clock advanced between runs);
* a Decimal-`generation` record through `_BotoTable` normalization yields the
  correct int counter.

Table semantics reuse the exact-condition `dynamodb_stub.InMemoryTable`
(sibling `tests/state_machine/` dir, added to the path below — the stub's
contract strings ARE the protocol contract, so no second implementation is
ventured here). Stamp literals were computed once via
`TZ=America/Los_Angeles date -d @EPOCH`.
"""

import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "state_machine"))  # noqa: E402

from dynamodb_stub import InMemoryTable  # noqa: E402

from common.config import ConfigProvider  # noqa: E402
from common.diff import HttpResponse  # noqa: E402
from common.marker import build_marker  # noqa: E402
from worker_handler import _BotoTable, handler  # noqa: E402

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
SHA_B = "bb" * 20
BASE_SHA = "00" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
GUID_2 = "22222222-2222-4222-8222-222222222222"
NOW_1 = 1_750_000_000  # Jun 15, 8:06 AM PDT
NOW_2 = 1_750_003_600  # Jun 15, 9:06 AM PDT
POST_ID = 987654
ENDPOINT = "https://llm.example.test/v1/chat/completions"

REVIEW_BODY = (
    "## Summary\nAdds input validation.\n\n"
    "## Findings\n- [HIGH] `src/main.py:12` — Missing bound. Fix: add a check.\n\n"
    "## Risk Notes\nNone.\n"
)

SSM_VALUES = {
    "/pr-reviewer/github-token": "github-token-value",  # noqa: S105 (fake fixture)
    "/pr-reviewer/webhook-secret": "webhook-secret-value",  # noqa: S105 (fake fixture)
    "/pr-reviewer/glm-api-key": "glm-key-value",  # noqa: S105 (fake fixture)
    "/pr-reviewer/glm-model": "glm-5.3-flash",
    "/pr-reviewer/glm-endpoint": ENDPOINT,
}

FIRST_HEADER = "**Review #1 · updated Jun 15, 8:06 AM PT**"
REPLAY_HEADER = "**Review #1 · updated Jun 15, 9:06 AM PT**"


def _envelope(*, sha=SHA_B, guid=GUID_1):
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
    def __init__(self, *, sha=SHA_B):
        self._sha = sha

    def __call__(self, url, headers):
        if "/files" in url:
            return HttpResponse(
                status=200,
                body=json.dumps(
                    [
                        {
                            "filename": "src/main.py",
                            "additions": 5,
                            "deletions": 2,
                            "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n",
                        }
                    ]
                ).encode(),
                headers={},
            )
        return HttpResponse(
            status=200, body=json.dumps({"head": {"sha": self._sha}}).encode(), headers={}
        )


class _FakeSocket:
    def settimeout(self, seconds):
        pass


class _FakeLLMResponse:
    def __init__(self, body):
        self.status = 200
        self._body = body

    def read(self):
        return self._body


class _FakeLLMConnection:
    def __init__(self):
        self.sock = _FakeSocket()

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        pass

    def getresponse(self):
        return _FakeLLMResponse(
            json.dumps(
                {
                    "choices": [{"message": {"role": "assistant", "content": REVIEW_BODY}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            ).encode()
        )

    def close(self):
        pass


class _FakeGitHub:
    """Issues-Comments double capturing published bodies."""

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

    def bodies(self, method):
        return [call["body"]["body"] for call in self.calls if call["method"] == method]


class _BotoShapedAdapter:
    """boto3-shaped adapter over InMemoryTable storing raw Decimals.

    Lets the Decimal leg seed DynamoDB-realistic numbers while reusing the
    exact-condition stub: `_BotoTable` normalizes on read, the stub enforces
    the protocol conditions on write.
    """

    def __init__(self, inner):
        self._inner = inner

    def get_item(self, *, Key):
        item = self._inner.items.get(Key["pk"])
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, **kwargs):
        return self._inner.update_item(
            Key=kwargs["Key"],
            UpdateExpression=kwargs["UpdateExpression"],
            ConditionExpression=kwargs["ConditionExpression"],
            ExpressionAttributeNames=kwargs.get("ExpressionAttributeNames"),
            ExpressionAttributeValues=kwargs.get("ExpressionAttributeValues"),
        )

    def delete_item(self, **kwargs):
        """HLD-004 D9 mutex-release passthrough (T027 port extension)."""
        return self._inner.delete_item(
            Key=kwargs["Key"],
            ConditionExpression=kwargs["ConditionExpression"],
            ExpressionAttributeNames=kwargs.get("ExpressionAttributeNames"),
            ExpressionAttributeValues=kwargs.get("ExpressionAttributeValues"),
        )


class _Harness:
    def __init__(self, *, clock, table=None):
        self.clock = clock
        self.table = table if table is not None else InMemoryTable()
        self.ssm = _FakeSSM(dict(SSM_VALUES))
        self.provider = ConfigProvider(
            self.ssm.get_parameters, allowed_endpoint_hosts=("llm.example.test",)
        )
        self.diff = _FakeDiffTransport()
        self.github = _FakeGitHub()
        self.sink = []

    def run(self, payload):
        return handler(
            {"Records": [{"body": json.dumps(payload), "messageId": "m1"}]},
            None,
            _table=self.table,
            _config_provider=self.provider,
            _now=lambda: self.clock[0],
            _diff_transport=self.diff,
            _llm_factory=lambda host, port, *, timeout: _FakeLLMConnection(),
            _github_transport=self.github,
            _sink=self.sink.append,
            _system_prompt="SYSTEM-PROMPT",
        )


def test_first_review_posts_marker_first_with_header_1():
    """First review (generation 0): POST body opens with the marker, the
    header follows directly under it with `#1`, then the model sections."""
    h = _Harness(clock=[NOW_1])
    result = h.run(_envelope())
    assert result == {"ok": True, "results": ["published"]}
    (post,) = h.github.bodies("POST")
    chunks = post.split("\n\n")
    assert chunks[0] == build_marker(REPO, PR_NUMBER)
    assert chunks[1] == FIRST_HEADER
    assert "## Summary" in post and "## Findings" in post and "## Risk Notes" in post


def test_same_sha_replay_keeps_number_with_fresh_stamp():
    """Same-SHA replay: still `#1` (generation counts established revisions,
    not publishes) with a fresh stamp — PATCHes the same comment, no POST."""
    h = _Harness(clock=[NOW_1])
    assert h.run(_envelope()) == {"ok": True, "results": ["published"]}
    h.clock[0] = NOW_2
    result = h.run(_envelope(guid=GUID_2))
    assert result == {"ok": True, "results": ["published"]}
    assert len(h.github.bodies("POST")) == 1
    (patch,) = h.github.bodies("PATCH")
    chunks = patch.split("\n\n")
    assert chunks[0] == build_marker(REPO, PR_NUMBER)
    assert chunks[1] == REPLAY_HEADER


def test_decimal_generation_record_yields_int_counter():
    """A DynamoDB-realistic record (Decimal numbers) through `_BotoTable`
    normalization: generation 2 reviews as `#3`, and the `published` log
    line carries the int generation (Decimal would fail json serialization,
    dropping the line via the emit-failed path)."""
    inner = InMemoryTable()
    inner.items[PK] = {
        "pk": PK,
        "status": "ACTIVE",
        "generation": Decimal(2),
        "head_sha": SHA_B,
        "last_seen_sha": SHA_B,
        "comment_id": Decimal(POST_ID),
        "updated_at": "2026-09-12T10:00:00Z",
    }
    h = _Harness(clock=[NOW_1], table=_BotoTable(_BotoShapedAdapter(inner)))
    result = h.run(_envelope())
    assert result == {"ok": True, "results": ["published"]}
    assert h.github.bodies("POST") == []
    (patch,) = h.github.bodies("PATCH")
    assert "**Review #3 · updated Jun 15, 8:06 AM PT**" in patch
    # The `published` line exists at all only because `generation` normalized
    # to int: a Decimal would fail the event's json.dumps and drop the line
    # via the emit-failed path (no sink entry, no status line).
    lines = [json.loads(line) for line in h.sink]
    assert lines[-1]["status"] == "published"
    assert lines[-1]["generation"] == 2

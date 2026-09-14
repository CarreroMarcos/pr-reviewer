"""T037: PATCH-404 decision table (HLD §2.3 item 8; failure mode 13).

Drives the REAL `worker_handler.handler` with a scripted Issues-Comments
transport covering every PATCH-404 row of the item-8 table:

* PATCH 404 + list readable + marker found on another comment → adopt the
  marker-bearing comment, reconcile (lowest id wins, extras deleted)
* PATCH 404 + list readable + no marker-bearing comment → creation lease →
  POST → persist ID → re-check
* PATCH 404 + 403/404 on the list/GET itself → non-retryable (complete)
* PATCH 404 + unparseable list body → non-retryable (complete, never POST)
* PATCH 404 + transient list failure (5xx) → raise for queue retry

Each recovery test also asserts the list was ATTEMPTED (a GET follows the
404ing PATCH) — the current worker completes without ever listing, so these
fail until T040 wires update-in-place + reconcile. The non-retryable tests
assert completion (no raise, no POST); the transient test asserts the raise
plus the `retry_queued` log line.

Doubles are local to this module (`tests/unit` cannot import the
`tests/state_machine/dynamodb_stub.py` port): `FakeTable` is deliberately
permissive on conditions — condition-string strictness is pinned by
`tests/state_machine/test_reconcile.py` + `dynamodb_stub.py`, not here.
This file pins the GitHub call sequences and handler outcomes.
"""

import json

import pytest

from worker_handler import GitHubError, handler

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
MARKER = "<!-- pr-reviewer:canonical:v1:octo-org/hello-world#42 -->"
SHA_B = "bb" * 20
BASE_SHA = "00" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
NOW = 1_750_000_000
STORED_ID = 555
ADOPTED_ID = 777
EXTRA_ID = 999
POST_ID = 888
UPDATED_AT = "2026-09-12T10:00:00Z"
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


def _comment(comment_id, body):
    return {"id": comment_id, "body": body}


def _envelope():
    return {
        "envelope_version": "v1",
        "event_type": "pull_request",
        "action": "opened",
        "repo_full_name": REPO,
        "pr_number": PR_NUMBER,
        "head_sha": SHA_B,
        "base_sha": BASE_SHA,
        "sender": "octo-user",
        "delivery_guid": GUID_1,
    }


def _sqs_event(payload):
    return {"Records": [{"body": json.dumps(payload), "messageId": "m1"}]}


class FakeTable:
    """Permissive record double: seeded ACTIVE item + call recording.

    Reads return the seeded item; writes apply `:comment` values so the
    adopted/persisted id is observable. Conditional strictness is NOT
    modeled here (pinned by the state-machine tier).
    """

    def __init__(self):
        self.items = {
            PK: {
                "pk": PK,
                "status": "ACTIVE",
                "generation": 2,
                "head_sha": SHA_B,
                "last_seen_sha": SHA_B,
                "comment_id": STORED_ID,
                "updated_at": UPDATED_AT,
            }
        }
        self.updates = []

    def get_item(self, pk):
        item = self.items.get(pk)
        return dict(item) if item is not None else None

    def update_item(self, **kwargs):
        self.updates.append(kwargs.get("ConditionExpression"))
        values = kwargs.get("ExpressionAttributeValues") or {}
        if ":comment" in values:
            self.items[PK]["comment_id"] = values[":comment"]
        return {"Attributes": dict(self.items[PK])}


class ScriptedGitHub:
    """Ordered (status, body) script consumed per call regardless of method;
    records (method, url) plus the raw body for content assertions."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "body": body})
        assert self._script, f"unexpected extra GitHub call: {method} {url}"
        status, raw = self._script.pop(0)
        return status, raw

    def methods(self):
        return [call["method"] for call in self.calls]

    def patch_bodies(self):
        return [
            json.loads(call["body"].decode())
            for call in self.calls
            if call["method"] == "PATCH"
        ]


class FakeSSM:
    def __init__(self, values):
        self.values = dict(values)

    def get_parameters(self, Names, WithDecryption=False):  # noqa: N803 (boto3 shape)
        params = [
            {"Name": name, "Value": self.values[name]} for name in Names if name in self.values
        ]
        return {"Parameters": params, "InvalidParameters": []}


class FakeDiffTransport:
    """Meta replies repeat-last; files page is canned (valid shape)."""

    def __init__(self, meta):
        from common.diff import HttpResponse

        self._response = HttpResponse
        self._meta = list(meta)

    def __call__(self, url, headers):
        if "/files" in url:
            files = [
                {
                    "filename": "src/main.py",
                    "additions": 5,
                    "deletions": 2,
                    "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n",
                }
            ]
            return self._response(status=200, body=json.dumps(files).encode(), headers={})
        entry = self._meta.pop(0) if len(self._meta) > 1 else self._meta[0]
        status, sha = entry
        if status != 200:
            return self._response(status=status, body=b"{}", headers={})
        return self._response(
            status=200, body=json.dumps({"head": {"sha": sha}}).encode(), headers={}
        )


class _FakeLLMSocket:
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
        self.sock = _FakeLLMSocket()

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


class Harness:
    def __init__(self, github):
        from common.config import ConfigProvider

        self.table = FakeTable()
        self.github = github
        self.sink = []
        self._provider = ConfigProvider(
            FakeSSM(SSM_VALUES).get_parameters, allowed_endpoint_hosts=("llm.example.test",)
        )

    def run(self):
        return handler(
            _sqs_event(_envelope()),
            None,
            _table=self.table,
            _config_provider=self._provider,
            _now=lambda: NOW,
            _diff_transport=FakeDiffTransport(meta=[(200, SHA_B)]),
            _llm_factory=lambda host, port, *, timeout: _FakeLLMConnection(),
            _github_transport=self.github,
            _sink=self.sink.append,
            _system_prompt="SYSTEM-PROMPT",
        )


def _list_body(*comments):
    return json.dumps(list(comments)).encode()


# --- row 1: comment migrated — adopt marker-bearing comment, reconcile ---


def test_patch_404_with_migrated_marker_comment_adopts_lowest_and_reconciles():
    """PATCH 555 → 404; list shows marker comments 777 + 999 → PATCH 777
    with the fresh review, DELETE 999, persist 777; result `published`."""
    github = ScriptedGitHub(
        [
            (404, b"{}"),  # PATCH 555 (deleted on GitHub)
            (200, _list_body(_comment(111, "plain"), _comment(EXTRA_ID, "x " + MARKER), _comment(ADOPTED_ID, "old " + MARKER))),  # GET list
            (200, json.dumps({"id": ADOPTED_ID}).encode()),  # PATCH 777
            (204, b""),  # DELETE 999
        ]
    )
    h = Harness(github)
    assert h.run() == {"ok": True, "results": ["published"]}
    assert github.methods() == ["PATCH", "GET", "PATCH", "DELETE"]
    assert github.calls[2]["url"].endswith(f"/issues/comments/{ADOPTED_ID}")
    assert MARKER in github.patch_bodies()[1]["body"]  # fresh review landed on 777
    assert github.calls[3]["url"].endswith(f"/issues/comments/{EXTRA_ID}")
    assert h.table.items[PK]["comment_id"] == ADOPTED_ID


# --- row 2: deleted, none exists — lease → POST → persist → re-check ---


def test_patch_404_with_no_marker_comment_posts_persists_and_rechecks():
    """PATCH 555 → 404; list shows no marker comment → POST a new comment,
    persist its id, re-check (second GET) finds exactly it; `published`."""
    github = ScriptedGitHub(
        [
            (404, b"{}"),  # PATCH 555
            (200, _list_body(_comment(111, "plain"))),  # GET list: no marker
            (201, json.dumps({"id": POST_ID}).encode()),  # POST
            (200, _list_body(_comment(POST_ID, "n " + MARKER))),  # GET re-check
        ]
    )
    h = Harness(github)
    assert h.run() == {"ok": True, "results": ["published"]}
    assert github.methods() == ["PATCH", "GET", "POST", "GET"]
    assert github.calls[2]["url"].endswith(f"/repos/{REPO}/issues/{PR_NUMBER}/comments")
    assert MARKER in json.loads(github.calls[2]["body"].decode())["body"]
    assert h.table.items[PK]["comment_id"] == POST_ID


# --- row 3: 403/404 on the list itself — non-retryable ---


def test_patch_404_with_forbidden_list_read_completes():
    """PATCH 555 → 404; list GET → 403 (lost access) → complete without
    retry and without POST; the stored id is left for the next run."""
    github = ScriptedGitHub([(404, b"{}"), (403, b"{}")])
    h = Harness(github)
    assert h.run() == {"ok": True, "results": ["discarded_error"]}
    assert github.methods() == ["PATCH", "GET"]
    assert h.table.items[PK]["comment_id"] == STORED_ID
    (line,) = (json.loads(entry) for entry in h.sink)
    assert line["status"] == "discarded_error"


# --- row 4: unparseable list — non-retryable, never POST ---


def test_patch_404_with_unparseable_list_completes_without_post():
    """PATCH 555 → 404; list GET → 200 with a non-array body → the list is
    unreadable → complete (no raise, no POST, id untouched)."""
    github = ScriptedGitHub([(404, b"{}"), (200, b'{"message": "oops"}')])
    h = Harness(github)
    assert h.run() == {"ok": True, "results": ["discarded_error"]}
    assert github.methods() == ["PATCH", "GET"]
    assert h.table.items[PK]["comment_id"] == STORED_ID


# --- row 5: transient list failure — raise for queue retry ---


def test_patch_404_with_transient_list_failure_raises_for_retry():
    """PATCH 555 → 404; list GET → 500 → raise; `retry_queued` is logged
    first so the queue (not a silent complete) owns the retry."""
    github = ScriptedGitHub([(404, b"{}"), (500, b"boom")])
    h = Harness(github)
    with pytest.raises(GitHubError) as exc_info:
        h.run()
    assert exc_info.value.status == 500
    assert github.methods() == ["PATCH", "GET"]
    (line,) = (json.loads(entry) for entry in h.sink)
    assert line["status"] == "retry_queued"

"""T047: concurrent-duplicate probes at the worker boundary (US4.AC2–AC3;
FR-012; HLD §5.2 replay residual).

Drives the REAL `worker_handler.handler` with a stateful comment-store
double (GET lists previously POSTed comments, so duplication — not just a
second POST — is observable):

* two copies of one event back-to-back → one comment, one ACTIVE record
  (claim exclusivity + convergence) →
  `test_duplicate_event_twice_produces_single_comment`
* a duplicate arriving under a live foreign lease → discarded without
  publish, record intact (exclusivity under concurrency) →
  `test_duplicate_under_live_foreign_lease_discards`
* replay past the dedup window (worker-side: same head re-accepted after
  the lease released) → at most one bounded redundant review, converges
  to the one canonical comment, never a second POST →
  `test_post_window_replay_converges_to_one_comment`

Worker-side note: the 7-day dedup TTL lives at ingress (GUID table); the
worker has no clock/TTL concept, so "past TTL" here means a same-head
event the worker must accept as new. US4.AC3's bound (one redundant
review, one comment) is what these pin. All deterministic: fixed clock
(`NOW`), scripted transports, no sleeps.
"""

import json

from dynamodb_stub import InMemoryTable

from worker_handler import handler

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
MARKER = "<!-- pr-reviewer:canonical:v1:octo-org/hello-world#42 -->"
SHA_B = "bb" * 20
BASE_SHA = "00" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
GUID_DUP = "22222222-2222-4222-8222-222222222222"
GUID_OTHER = "33333333-3333-4333-8333-333333333333"
NOW = 1_750_000_000
UPDATED_AT = "2026-09-12T10:00:00Z"
ENDPOINT = "https://llm.example.test/v1/chat/completions"

BODY = (
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


def _envelope(sha=SHA_B, guid=GUID_1):
    return {
        "envelope_version": "v1",
        "event_type": "pull_request",
        "action": "synchronize",
        "repo_full_name": REPO,
        "pr_number": PR_NUMBER,
        "head_sha": sha,
        "base_sha": BASE_SHA,
        "sender": "octo-user",
        "delivery_guid": guid,
    }


def _sqs_event(payload):
    return {"Records": [{"body": json.dumps(payload), "messageId": "m1"}]}


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


def _completion_body(content=BODY):
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode()


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
    def __init__(self, script, seen):
        self._script = script
        self.sock = _FakeLLMSocket()
        seen.append(self)

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        pass

    def getresponse(self):
        return _FakeLLMResponse(_completion_body(self._script.pop(0)))

    def close(self):
        pass


class StatefulGitHub:
    """Comment-store double: GET lists, POST appends, PATCH refreshes (404
    when unknown), DELETE removes."""

    def __init__(self, comments=None):
        self.comments = [dict(c) for c in (comments or [])]
        self._next_id = 1000
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "body": body})
        if method == "GET":
            return 200, json.dumps(self.comments).encode()
        if method == "POST":
            comment_id = self._next_id
            self._next_id += 1
            self.comments.append({"id": comment_id, "body": json.loads(body)["body"]})
            return 201, json.dumps({"id": comment_id}).encode()
        comment_id = int(url.rsplit("/", 1)[-1])
        if method == "PATCH":
            for comment in self.comments:
                if comment["id"] == comment_id:
                    comment["body"] = json.loads(body)["body"]
                    return 200, json.dumps({"id": comment_id}).encode()
            return 404, b"{}"
        if method == "DELETE":
            self.comments = [c for c in self.comments if c["id"] != comment_id]
            return 204, b""
        raise AssertionError(f"unexpected GitHub method: {method}")

    def methods(self):
        return [call["method"] for call in self.calls]


class Harness:
    """One worker run's doubles; `table`/`github` shared across runs model
    duplicate deliveries converging on one PR."""

    def __init__(self, *, meta, llm_bodies=None, table=None, github=None):
        from common.config import ConfigProvider

        self.table = table if table is not None else InMemoryTable()
        self.github = github if github is not None else StatefulGitHub()
        self.sink = []
        self.llm_conns = []
        self._provider = ConfigProvider(
            FakeSSM(SSM_VALUES).get_parameters, allowed_endpoint_hosts=("llm.example.test",)
        )
        self._diff = FakeDiffTransport(meta=meta)
        self._llm_script = list(llm_bodies) if llm_bodies else [BODY]

    def _factory(self, host, port, *, timeout):
        return _FakeLLMConnection(self._llm_script, self.llm_conns)

    def run(self, payload):
        return handler(
            _sqs_event(payload),
            None,
            _table=self.table,
            _config_provider=self._provider,
            _now=lambda: NOW,
            _diff_transport=self._diff,
            _llm_factory=self._factory,
            _github_transport=self.github,
            _sink=self.sink.append,
            _system_prompt="SYSTEM-PROMPT",
        )


def _seed_active(table, *, head, gen, comment):
    """Faithful settled state: ACTIVE with comment, NO lease (finalize
    releases it, HLD §3.2)."""
    table.items[PK] = {
        "pk": PK,
        "status": "ACTIVE",
        "generation": gen,
        "head_sha": head,
        "last_seen_sha": head,
        "comment_id": comment,
        "updated_at": UPDATED_AT,
    }


def test_duplicate_event_twice_produces_single_comment():
    """US4.AC2 core: the identical event twice in a row → both runs publish,
    but exactly ONE comment exists (first POSTs, second PATCHes the same
    id) and one ACTIVE record owns it at an unchanged generation."""
    table, github = InMemoryTable(), StatefulGitHub()
    first = Harness(meta=[(200, SHA_B)], table=table, github=github)
    assert first.run(_envelope()) == {"ok": True, "results": ["published"]}
    assert first.github.methods() == ["GET", "POST", "GET"]

    second = Harness(meta=[(200, SHA_B)], table=table, github=github)
    calls_before = len(github.calls)
    assert second.run(_envelope()) == {"ok": True, "results": ["published"]}
    second_methods = [c["method"] for c in github.calls[calls_before:]]
    assert "POST" not in second_methods  # re-publish is a PATCH, never re-POST
    assert second_methods == ["PATCH"]
    assert len(github.comments) == 1
    item = table.items[PK]
    assert item["status"] == "ACTIVE"
    assert item["head_sha"] == SHA_B
    assert item["generation"] == 0
    assert item["comment_id"] == github.comments[0]["id"]


def test_duplicate_under_live_foreign_lease_discards():
    """Claim exclusivity under concurrency: a duplicate arriving while a
    live lease is held by another owner is discarded — no review effect
    downstream (no publish), and the stored record is byte-identical."""
    table = InMemoryTable()
    table.items[PK] = {
        "pk": PK,
        "status": "CLAIMED",
        "generation": 2,
        "head_sha": SHA_B,
        "last_seen_sha": SHA_B,
        "claim_owner": GUID_OTHER,
        "claim_until": NOW + 100,
        "updated_at": UPDATED_AT,
    }
    before = dict(table.items[PK])
    h = Harness(meta=[(200, SHA_B)], table=table)
    assert h.run(_envelope(guid=GUID_DUP)) == {"ok": True, "results": ["discarded_claim_held"]}
    assert h.github.calls == []
    assert table.items[PK] == before


def test_post_window_replay_converges_to_one_comment():
    """US4.AC3 worker half: a same-head event accepted as new (dedup window
    expired upstream) burns at most one bounded redundant review and
    converges — PATCH of the one canonical comment, never a second POST;
    a further identical replay behaves the same (bounded, no growth)."""
    table, github = (
        InMemoryTable(),
        StatefulGitHub(comments=[{"id": 555, "body": "review for B " + MARKER}]),
    )
    _seed_active(table, head=SHA_B, gen=2, comment=555)
    first = Harness(meta=[(200, SHA_B)], table=table, github=github)
    assert first.run(_envelope(guid=GUID_DUP)) == {"ok": True, "results": ["published"]}
    assert [c["method"] for c in github.calls] == ["PATCH"]
    assert len(github.comments) == 1
    assert table.items[PK]["comment_id"] == 555

    calls_before = len(github.calls)
    second = Harness(meta=[(200, SHA_B)], table=table, github=github)
    assert second.run(_envelope(guid=GUID_1)) == {"ok": True, "results": ["published"]}
    assert [c["method"] for c in github.calls[calls_before:]] == ["PATCH"]
    assert len(github.comments) == 1
    assert table.items[PK]["comment_id"] == 555
    assert table.items[PK]["generation"] == 2

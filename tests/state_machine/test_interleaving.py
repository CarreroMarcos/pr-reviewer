"""T038: rapid-push interleaving at the worker boundary (US2.AC2; HLD §3.3;
FR-014–FR-016).

Drives the REAL `worker_handler.handler` with scripted collaborators to pin
the three US2.AC2 clauses for two revisions pushed in rapid succession:

* second establish increments `generation` →
  `test_second_push_establish_increments_generation`
* a mid-flight older review aborts at the fence (never publishes) →
  `test_midflight_older_review_aborts_at_fence`
* a mid-flight older review aborts at finalize (never overwrites) →
  `test_midflight_older_review_aborts_at_finalize`
* once processing settles, exactly one comment exists and it reflects only
  the newest head (state AND content) →
  `test_settled_interleaving_reflects_only_newest_head`

Interleaving is modeled two ways: sequential handler runs sharing one
table (rapid pushes processed back-to-back), and mid-flight landings via
scripted live-head sequences plus a publish-time hook (a newer revision
landing between the older run's fence and its finalize). The GitHub double
is stateful (GET lists previously POSTed comments) so adoption — not
duplication — is observable; the LLM double serves distinct per-run bodies
so newest-wins is assertable on content, not just SHAs.

All tests are deterministic: fixed clock (`NOW`), scripted transports, no
sleeps. A test asserting a discard also asserts nothing was published.
"""

import json

from dynamodb_stub import InMemoryTable

from worker_handler import handler

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
MARKER = "<!-- pr-reviewer:canonical:v1:octo-org/hello-world#42 -->"
SHA_A = "aa" * 20
SHA_B = "bb" * 20
BASE_SHA = "00" * 20
GUID_A = "11111111-1111-4111-8111-111111111111"
GUID_B = "22222222-2222-4222-8222-222222222222"
GUID_OTHER = "33333333-3333-4333-8333-333333333333"
NOW = 1_750_000_000
UPDATED_AT = "2026-09-12T10:00:00Z"
ENDPOINT = "https://llm.example.test/v1/chat/completions"

BODY_A = (
    "## Summary\nAlpha revision review.\n\n"
    "## Findings\n- [HIGH] `src/alpha.py:1` — Alpha flaw. Fix: add a bound.\n\n"
    "## Risk Notes\nNone.\n"
)
BODY_B = (
    "## Summary\nBeta revision review.\n\n"
    "## Findings\n- [HIGH] `src/beta.py:2` — Beta flaw. Fix: add a check.\n\n"
    "## Risk Notes\nNone.\n"
)

SSM_VALUES = {
    "/pr-reviewer/github-token": "github-token-value",  # noqa: S105 (fake fixture)
    "/pr-reviewer/webhook-secret": "webhook-secret-value",  # noqa: S105 (fake fixture)
    "/pr-reviewer/glm-api-key": "glm-key-value",  # noqa: S105 (fake fixture)
    "/pr-reviewer/glm-model": "glm-5.3-flash",
    "/pr-reviewer/glm-endpoint": ENDPOINT,
}


def _envelope(sha, guid):
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


def _completion_body(content):
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
    def __init__(self, script):
        self._script = script
        self.sock = _FakeLLMSocket()

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        pass

    def getresponse(self):
        return _FakeLLMResponse(_completion_body(self._script.pop(0)))

    def close(self):
        pass


class StatefulGitHub:
    """Comment-store double: GET lists previously POSTed comments (so
    adoption — not duplication — is observable), POST appends, PATCH
    refreshes the stored body (404 when the id is unknown), DELETE removes."""

    def __init__(self, *, first_id=1000, on_write=None):
        self._next_id = first_id
        self._on_write = on_write
        self.comments = []
        self.calls = []

    def __call__(self, method, url, headers, body):
        call = {"method": method, "url": url, "body": body}
        self.calls.append(call)
        if self._on_write is not None:
            self._on_write(call)
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
    """One worker run's doubles; `table` and `github` may be shared across
    runs to model rapid pushes processed back-to-back on one PR."""

    def __init__(self, *, meta, llm_bodies, table=None, github=None):
        from common.config import ConfigProvider

        self.table = table if table is not None else InMemoryTable()
        self.github = github if github is not None else StatefulGitHub()
        self.sink = []
        self._provider = ConfigProvider(
            FakeSSM(SSM_VALUES).get_parameters, allowed_endpoint_hosts=("llm.example.test",)
        )
        self._diff = FakeDiffTransport(meta=meta)
        self._llm_script = list(llm_bodies)

    def _factory(self, host, port, *, timeout):
        return _FakeLLMConnection(self._llm_script)

    def run(self, sha, guid):
        return handler(
            _sqs_event(_envelope(sha, guid)),
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


def _seed(table, *, head, gen, comment=None):
    item = {
        "pk": PK,
        "status": "ACTIVE" if comment is not None else "CLAIMED",
        "generation": gen,
        "head_sha": head,
        "last_seen_sha": head,
        "claim_owner": GUID_A,
        "claim_until": NOW + 100,
        "updated_at": UPDATED_AT,
    }
    if comment is not None:
        item["comment_id"] = comment
    table.items[PK] = item
    return item


def test_second_push_establish_increments_generation():
    """Rapid pushes A then B: the second establish confirms B live and
    increments `generation` by exactly one; the record ends at head B."""
    table = InMemoryTable()
    github = StatefulGitHub()
    first = Harness(meta=[(200, SHA_A)], llm_bodies=[BODY_A], table=table, github=github)
    assert first.run(SHA_A, GUID_A) == {"ok": True, "results": ["published"]}
    assert table.items[PK]["generation"] == 0

    second = Harness(meta=[(200, SHA_B)], llm_bodies=[BODY_B], table=table, github=github)
    assert second.run(SHA_B, GUID_B) == {"ok": True, "results": ["published"]}
    item = table.items[PK]
    assert item["generation"] == 1
    assert item["head_sha"] == SHA_B
    assert item["status"] == "ACTIVE"


def test_midflight_older_review_aborts_at_fence():
    """A lands a newer push between the older run's claim and its fence →
    the older review discards as stale; publish NEVER runs (no GitHub
    write of any kind); the record is left claimed for lease recovery."""
    h = Harness(meta=[(200, SHA_A), (200, SHA_B)], llm_bodies=[BODY_A])
    assert h.run(SHA_A, GUID_A) == {"ok": True, "results": ["discarded_stale"]}
    assert h.github.calls == []
    item = h.table.items[PK]
    assert item["status"] == "CLAIMED"
    assert item["head_sha"] == SHA_A
    assert item["generation"] == 0
    assert "comment_id" not in item
    (line,) = (json.loads(entry) for entry in h.sink)
    assert line["status"] == "discarded_stale"


def test_midflight_older_review_aborts_at_finalize():
    """A newer accepted revision lands between the older run's publish and
    its finalize → log-and-reconcile path; the newer record is never
    overwritten (head, generation, and lease untouched)."""
    table = InMemoryTable()
    github = StatefulGitHub()
    landed: list = []

    def concurrent_landing(call):
        if not landed and call["method"] == "POST":
            landed.append(call)
            _seed(table, head=SHA_B, gen=6)

    _seed(table, head=SHA_A, gen=4, comment=111)
    github._on_write = concurrent_landing
    h = Harness(meta=[(200, SHA_A)], llm_bodies=[BODY_A], table=table, github=github)
    assert h.run(SHA_A, GUID_A) == {"ok": True, "results": ["published_finalize_conflict"]}
    item = table.items[PK]
    assert item["head_sha"] == SHA_B
    assert item["generation"] == 6


def test_settled_interleaving_reflects_only_newest_head():
    """US2.AC2 end to end: pushes A then B settle to exactly ONE comment
    whose content reflects B (never A); the record points at that comment
    at head B. The second run adopts (PATCH) — it never POSTs."""
    table = InMemoryTable()
    github = StatefulGitHub()
    first = Harness(meta=[(200, SHA_A)], llm_bodies=[BODY_A], table=table, github=github)
    assert first.run(SHA_A, GUID_A) == {"ok": True, "results": ["published"]}

    before = len(github.comments)
    second = Harness(meta=[(200, SHA_B)], llm_bodies=[BODY_B], table=table, github=github)
    assert second.run(SHA_B, GUID_B) == {"ok": True, "results": ["published"]}

    assert len(github.comments) == before == 1  # adopted, never duplicated
    (comment,) = github.comments
    assert "beta.py" in comment["body"]
    assert "alpha.py" not in comment["body"]
    assert MARKER in comment["body"]
    assert second.github.methods().count("POST") == 1  # only the first run's POST
    item = table.items[PK]
    assert item["head_sha"] == SHA_B
    assert item["generation"] == 1
    assert item["comment_id"] == comment["id"]

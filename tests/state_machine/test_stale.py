"""T043: stale-event paths at the worker boundary (US3.AC1–AC2; FR-014,
FR-015; HLD §3.3; failure modes 6–7).

Drives the REAL `worker_handler.handler` with scripted collaborators to pin
the two stale clauses:

* Clause A — older-SHA event rejected at establish with `last_seen_sha`
  still recorded (HLD §3.3 step 1: the most recently observed webhook SHA
  is recorded regardless of acceptance):
  `test_superseded_event_records_last_seen_sha`,
  `test_successive_stale_events_keep_recording_latest_observation`
* Clause B — fence mismatch after claim aborts publication before any
  GitHub write; a stale event never mutates the canonical comment:
  `test_fence_mismatch_aborts_before_any_github_write`,
  `test_stale_event_leaves_canonical_comment_untouched`
* Logging (T044 half) — superseded/stale outcomes carry the HLD §4.3
  `stale_discarded` metric:
  `test_stale_outcomes_log_stale_discarded`

All tests are deterministic: fixed clock (`NOW`), scripted transports, no
sleeps. A test asserting a discard also asserts nothing was published and
the reviewed head is untouched.
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
SHA_C = "cc" * 20
INJECTED_SHA = "ef" * 20
BASE_SHA = "00" * 20
GUID_A = "11111111-1111-4111-8111-111111111111"
GUID_B = "22222222-2222-4222-8222-222222222222"
NOW = 1_750_000_000
UPDATED_AT = "2026-09-12T10:00:00Z"
ENDPOINT = "https://llm.example.test/v1/chat/completions"
STORED_ID = 555

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
    when unknown), DELETE removes. Pre-seedable to model the live canonical
    comment."""

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

    def log_lines(self):
        return [json.loads(line) for line in self.sink]


def _seed(table, *, head, gen, comment):
    item = {
        "pk": PK,
        "status": "ACTIVE",
        "generation": gen,
        "head_sha": head,
        "last_seen_sha": head,
        "claim_owner": GUID_A,
        "claim_until": NOW + 100,
        "comment_id": comment,
        "updated_at": UPDATED_AT,
    }
    table.items[PK] = item
    return dict(item)


# --- clause A: rejected at establish, but the observation is recorded ---


def test_superseded_event_records_last_seen_sha():
    """Stale SHA_A arrives while the record (and live head) is at SHA_B:
    discarded as superseded — but `last_seen_sha` advances to the just
    observed SHA_A. Nothing else moves: head, generation, comment, lease
    untouched; review never runs; GitHub never touched."""
    h = Harness(meta=[(200, SHA_B)])
    before = _seed(h.table, head=SHA_B, gen=3, comment=STORED_ID)
    assert h.run(SHA_A, GUID_B) == {"ok": True, "results": ["discarded_superseded"]}
    item = h.table.items[PK]
    assert item["last_seen_sha"] == SHA_A
    assert item["head_sha"] == before["head_sha"] == SHA_B
    assert item["generation"] == before["generation"] == 3
    assert item["comment_id"] == before["comment_id"] == STORED_ID
    assert item["claim_owner"] == before["claim_owner"]
    assert item["claim_until"] == before["claim_until"]
    assert h.llm_conns == []  # establish rejected before review
    assert h.github.calls == []


def test_successive_stale_events_keep_recording_latest_observation():
    """A second stale event overwrites the first observation: after stale A
    then stale C (live head B throughout), `last_seen_sha` is C — the most
    recently observed webhook SHA, regardless of acceptance."""
    table = InMemoryTable()
    _seed(table, head=SHA_B, gen=3, comment=STORED_ID)
    first = Harness(meta=[(200, SHA_B)], table=table)
    assert first.run(SHA_A, GUID_A) == {"ok": True, "results": ["discarded_superseded"]}
    second = Harness(meta=[(200, SHA_B)], table=table)
    assert second.run(SHA_C, GUID_B) == {"ok": True, "results": ["discarded_superseded"]}
    item = table.items[PK]
    assert item["last_seen_sha"] == SHA_C
    assert item["head_sha"] == SHA_B
    assert item["generation"] == 3
    assert item["comment_id"] == STORED_ID


class FlakyObserveTable:
    """InMemoryTable wrapper failing the `last_seen_sha` advance with a
    throttle-shaped fault (anything but `ConditionalCheckFailed`)."""

    def __init__(self, table):
        self._table = table

    def get_item(self, pk):
        return self._table.get_item(pk)

    def update_item(self, **kwargs):
        if kwargs.get("UpdateExpression") == "SET last_seen_sha = :seen":
            raise RuntimeError("throttled")
        return self._table.update_item(**kwargs)


def test_observe_throttle_still_discards_without_review_or_publish():
    """Gate-3 F3: the observation is best-effort, never correctness. A
    throttle/transport fault on the advance write warns and discards —
    review stays skipped, nothing is published, no Lambda error, no retry
    (propagating would convert discards into DLQ entries under throttle
    storms). Only `ConditionalCheckFailed` rejoins the retry loop."""
    inner = InMemoryTable()
    _seed(inner, head=SHA_B, gen=3, comment=STORED_ID)
    h = Harness(meta=[(200, SHA_B)], table=FlakyObserveTable(inner))
    assert h.run(SHA_A, GUID_B) == {"ok": True, "results": ["discarded_superseded"]}
    assert h.llm_conns == []
    assert h.github.calls == []
    assert inner.items[PK]["last_seen_sha"] == SHA_B  # observation lost, rest intact
    assert inner.items[PK]["head_sha"] == SHA_B
    (line,) = h.log_lines()
    assert line["status"] == "discarded_superseded"
    assert line["stale_discarded"] is True


# --- clause B: fence mismatch aborts before any GitHub write ---


def test_fence_mismatch_aborts_before_any_github_write():
    """A newer push lands between the run's claim and its fence: discarded
    as stale with NO GitHub write of any kind (no list, no POST, no PATCH,
    no DELETE). The review stage did run (it precedes the fence); the
    record is left claimed for lease-expiry recovery."""
    h = Harness(meta=[(200, SHA_A), (200, SHA_B)])
    assert h.run(SHA_A, GUID_A) == {"ok": True, "results": ["discarded_stale"]}
    assert len(h.llm_conns) == 1
    assert h.github.calls == []
    item = h.table.items[PK]
    assert item["status"] == "CLAIMED"
    assert item["head_sha"] == SHA_A
    assert item["generation"] == 0
    assert "comment_id" not in item
    (line,) = h.log_lines()
    assert line["status"] == "discarded_stale"


def test_stale_event_leaves_canonical_comment_untouched():
    """US3.AC1 core: a stale trigger for SHA_A (record and live head at
    SHA_B) performs no comment modification — the stored canonical body is
    byte-identical and no GitHub call of any method happens."""
    stored_body = "review for B " + MARKER
    github = StatefulGitHub(comments=[{"id": STORED_ID, "body": stored_body}])
    h = Harness(meta=[(200, SHA_B)], github=github)
    _seed(h.table, head=SHA_B, gen=2, comment=STORED_ID)
    assert h.run(SHA_A, GUID_B) == {"ok": True, "results": ["discarded_superseded"]}
    assert h.github.methods() == []
    assert github.comments == [{"id": STORED_ID, "body": stored_body}]
    assert h.table.items[PK]["head_sha"] == SHA_B


def test_injected_sha_ahead_of_api_consistency_discards_via_observation():
    """Gate-10 advisory carry (fence-lag edge): a forged delivery whose SHA
    was never pushed arrives while the record and the live head agree on
    SHA_B — the injection beats GitHub API consistency, so establish cannot
    confirm it. HLD-compliant discard as superseded with the observation
    recorded: `last_seen_sha` advances to the injected SHA, head /
    generation / comment / lease are untouched, review never runs, GitHub
    is never touched, and the `stale_discarded` line is emitted."""
    h = Harness(meta=[(200, SHA_B)])
    before = _seed(h.table, head=SHA_B, gen=3, comment=STORED_ID)
    assert h.run(INJECTED_SHA, GUID_B) == {"ok": True, "results": ["discarded_superseded"]}
    item = h.table.items[PK]
    assert item["last_seen_sha"] == INJECTED_SHA
    assert item["head_sha"] == before["head_sha"] == SHA_B
    assert item["generation"] == before["generation"] == 3
    assert item["comment_id"] == before["comment_id"] == STORED_ID
    assert item["claim_owner"] == before["claim_owner"]
    assert item["claim_until"] == before["claim_until"]
    assert h.llm_conns == []  # establish rejected before review
    assert h.github.calls == []  # no publish of unconfirmed content
    (line,) = h.log_lines()
    assert line["status"] == "discarded_superseded"
    assert line["stale_discarded"] is True


def test_repeat_stale_sha_after_observation():
    """Gate-3 F2/D2 — stale-repeat cost, pinned as OBSERVED (see below).

    Setup is the faithful settled state: ACTIVE, live head B, NO lease
    (finalize releases it, HLD §3.2). After stale A is observed
    (`last_seen_sha` = A), a second IDENTICAL A matches the (b) equality
    path (incoming == `last_seen_sha`) — which proceeds with the STORED
    head B — and runs a FULL redundant review + PATCH of the canonical
    comment with fresh B content before finalizing. Nothing is wrong
    content-wise (the comment carries a current-head review; generation
    does not churn), but the cost is a full publish, not a cheap discard:
    pre-T044 the repeat was a review-free superseded discard, and the T044
    observation extends (b) hits to repeat-stale events.

    LOUD DEVIATION from the H2 brief as specified: it predicted a full
    review ending in DISCARDED_STALE with no publish ("claim-fails on head
    mismatch"). That mechanism does not exist — (b) returns the stored
    head by construction, so the claim cannot fail on head mismatch in a
    single-threaded run; with no live lease the claim succeeds and the run
    publishes. (With a live foreign lease the same repeat yields
    DISCARDED_CLAIM_HELD after one review — also not STALE.) The behavior
    below is the unmodified code's actual answer; the brief's assertions
    are not satisfiable without new behavior.

    For Mars: gating (b) on `head_sha` as well (incoming must equal BOTH
    `last_seen_sha` and the stored head) would restore the cheap superseded
    discard for repeat-stale while preserving true idempotent redelivery —
    this finding makes that alternative MORE urgent than Gate 3 assumed.
    Deliberately NOT implemented here (new behavior, needs its own ticket).
    Bounded today: exact-repeat stale events are rare, and the queue's
    maxReceiveCount caps redelivery; reconcile-converged, never wrong."""
    table = InMemoryTable()
    github = StatefulGitHub(comments=[{"id": STORED_ID, "body": "review for B " + MARKER}])
    table.items[PK] = {
        "pk": PK,
        "status": "ACTIVE",
        "generation": 3,
        "head_sha": SHA_B,
        "last_seen_sha": SHA_B,
        "comment_id": STORED_ID,
        "updated_at": UPDATED_AT,
    }
    first = Harness(meta=[(200, SHA_B)], table=table, github=github)
    assert first.run(SHA_A, GUID_A) == {"ok": True, "results": ["discarded_superseded"]}
    assert table.items[PK]["last_seen_sha"] == SHA_A

    second = Harness(meta=[(200, SHA_B)], table=table, github=github)
    assert second.run(SHA_A, GUID_B) == {"ok": True, "results": ["published"]}
    assert len(second.llm_conns) == 1  # full redundant review ran
    assert second.github.methods() == ["PATCH"]  # ...and published it (redundant B review)
    assert len(github.comments) == 1  # still exactly one canonical comment
    assert github.comments[0]["id"] == STORED_ID
    assert MARKER in github.comments[0]["body"]
    item = table.items[PK]
    assert item["head_sha"] == SHA_B  # (b) never bumps generation or moves head
    assert item["generation"] == 3
    assert item["comment_id"] == STORED_ID


# --- logging (T044 half): the HLD §4.3 stale_discarded metric ---


def test_stale_outcomes_log_stale_discarded():
    """Superseded and fence-stale outcomes both carry `stale_discarded`
    true in the fixed-field event (HLD §4.3 review metrics)."""
    h = Harness(meta=[(200, SHA_B)])
    _seed(h.table, head=SHA_B, gen=3, comment=STORED_ID)
    assert h.run(SHA_A, GUID_B) == {"ok": True, "results": ["discarded_superseded"]}

    g = Harness(meta=[(200, SHA_A), (200, SHA_B)])
    assert g.run(SHA_A, GUID_A) == {"ok": True, "results": ["discarded_stale"]}

    (superseded_line,) = h.log_lines()
    assert superseded_line["status"] == "discarded_superseded"
    assert superseded_line["stale_discarded"] is True
    (stale_line,) = g.log_lines()
    assert stale_line["status"] == "discarded_stale"
    assert stale_line["stale_discarded"] is True

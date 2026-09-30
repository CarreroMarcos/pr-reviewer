"""T034: worker orchestration pipeline on an in-memory DynamoDB stub.

Covers HLD §2.3 (pipeline ordering + item-8 boundary), §3.3 (fenced
publication through `common.protocol`), §3.4 (publish shape), FR-009–FR-016.
Reuses the `dynamodb_stub.InMemoryTable` port doubles and the
review/fence/publish-fake patterns from `test_protocol.py`; the diff, LLM,
and GitHub collaborators arrive as injected fakes — no live external calls.

Mapping (pipeline stage / ruling → test):

* envelope → `test_malformed_body_completes`, `test_schema_invalid_envelope_completes`
* hydrate (T014) → `test_config_error_completes`
* establish → diff → LLM → assemble+gate → claim → fence → publish →
  finalize → `test_first_delivery_posts_and_finalizes`
* POST-when-absent / PATCH-when-present (Mars ruling 1) → now via
  reconcile (T040): absent → GET list → creation lease → POST → re-check;
  `test_first_delivery_posts_and_finalizes`,
  `test_new_revision_posts_pending_reconcile`,
  `test_same_revision_replay_patches_same_comment_never_reposts`
* no ACTIVE same-owner short-circuit (Mars ruling 2) →
  `test_same_revision_replay_patches_same_comment_never_reposts`
* single 401 re-fetch, the ONLY in-request retry →
  `test_llm_401_refreshes_once_then_publishes`,
  `test_llm_401_twice_completes`, `test_diff_401_refreshes_once`
* prompt packaging (T035) → `test_load_system_prompt_resolves_zip_layout`,
  `test_load_system_prompt_falls_back_to_repo_layout`
* discard outcomes → `test_concurrent_different_owner_discards_claim_held`,
  `test_superseded_sha_discards_without_publish`,
  `test_fence_mismatch_discards_stale`,
  `test_finalize_conflict_completes_without_retry`,
  `test_finalize_loser_claim_aborts_without_clearing_newer_lease`
* worker SQS client cache (SPR-63 G6-F2) →
  `test_sqs_client_cached_across_warm_invocations`,
  `test_reset_sqs_cache_forces_rebuild`
* worker system-prompt cache (audit R1) →
  `test_system_prompt_cached_across_warm_invocations`,
  `test_reset_prompt_cache_forces_reload`,
  `test_injected_system_prompt_bypasses_cache`
* boundary table → `test_is_retryable_table` plus one behavior test per
  row (`test_assemble_refused_completes`, `test_llm_timeout_raises…`,
  `test_llm_invalid_output_raises…`, `test_diff_transport_error_raises…`,
  `test_diff_403_completes`, `test_github_post_500_raises…`,
  `test_github_patch_404_with_unreadable_list_completes` — the PATCH-404
  decision table itself is pinned by
  `tests/unit/test_patch404_table.py`)
* log hygiene (HLD §5.4) → `test_log_event_carries_fixed_fields_only`

All tests are deterministic: fixed clock (`NOW`), scripted transports, no
sleeps. A test asserting "completes" also asserts nothing was published
and nothing raised; a test asserting "raises" also asserts the
`retry_queued` log line was emitted first.
"""

import hashlib
import http.client
import json

import pytest
from dynamodb_stub import InMemoryTable

import worker_handler
from common.assemble import AssembleError
from common.config import ConfigError, ConfigProvider
from common.diff import DiffError, HttpResponse
from common.llm import LlmError
from common.logs import FIXED_FIELDS
from common.validate import PROMPT_VERSION
from worker_handler import GitHubError, handler, is_retryable

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
SHA_A = "aa" * 20
SHA_B = "bb" * 20
SHA_C = "cc" * 20
BASE_SHA = "00" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
GUID_2 = "22222222-2222-4222-8222-222222222222"
GUID_OTHER = "33333333-3333-4333-8333-333333333333"
NOW = 1_750_000_000
POST_ID = 987654
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


def envelope(*, sha=SHA_B, guid=GUID_1):
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


def sqs_event(*payloads):
    return {
        "Records": [
            {"body": p if isinstance(p, str) else json.dumps(p), "messageId": "m1"}
            for p in payloads
        ]
    }


def file_entry(name="src/main.py"):
    return {
        "filename": name,
        "additions": 5,
        "deletions": 2,
        "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n",
    }


def completion_body(content=REVIEW_BODY):
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode()


class FakeSSM:
    """Injected SSM double mirroring the real GetParameters response shape."""

    def __init__(self, values):
        self.values = dict(values)
        self.calls = []

    def get_parameters(self, Names, WithDecryption=False):  # noqa: N803 (boto3 shape)
        self.calls.append({"Names": list(Names), "WithDecryption": WithDecryption})
        params = [
            {"Name": name, "Value": self.values[name]} for name in Names if name in self.values
        ]
        invalid = [name for name in Names if name not in self.values]
        return {"Parameters": params, "InvalidParameters": invalid}


class FakeDiffTransport:
    """URL-routed diff double: scripted meta replies, canned files page.

    `meta` entries are `(status, sha)` tuples (a bare `Exception` raises
    straight through, modeling transport-taxonomy errors); when the script
    has one entry left it repeats, so fence re-reads stay deterministic.
    """

    def __init__(self, *, meta, files=None):
        self._meta = list(meta)
        self._files = [file_entry()] if files is None else files
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append(url)
        if "/files" in url:
            if isinstance(self._files, BaseException):
                raise self._files
            return HttpResponse(status=200, body=json.dumps(self._files).encode(), headers={})
        entry = self._meta.pop(0) if len(self._meta) > 1 else self._meta[0]
        if isinstance(entry, BaseException):
            raise entry
        status, sha = entry
        if status != 200:
            return HttpResponse(status=status, body=b"{}", headers={})
        return HttpResponse(
            status=200, body=json.dumps({"head": {"sha": sha}}).encode(), headers={}
        )


class FakeLLMSocket:
    def settimeout(self, seconds):
        pass


class FakeLLMResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeLLMConnection:
    """Stub behind the `_connection_factory` seam; script items are
    `("response", status, body)` or `("raise", exc)`; an empty script
    answers 200 with the canonical completion."""

    def __init__(self, script, seen):
        self._script = script
        self.sock = FakeLLMSocket()
        self.requests = []
        seen.append(self)

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"method": method, "path": path})

    def getresponse(self):
        if not self._script:
            return FakeLLMResponse(200, completion_body())
        action = self._script.pop(0)
        if action[0] == "raise":
            raise action[1]
        return FakeLLMResponse(action[1], action[2])

    def close(self):
        pass


class FakeGitHub:
    """Issues-Comments double: records (method, url, parsed body); default
    routes POST → 201 and PATCH → 200 echoing the URL id; `script` overrides
    per call in order; `on_write` fires before each reply (finalize-race).

    T040: the publish port also lists (GET → 200 empty list by default, so
    first-post flows take the creation-lease branch) and deletes (DELETE →
    204). Scripted sequences cover the PATCH-404 decision table; the
    table itself is pinned by `tests/unit/test_patch404_table.py`."""

    def __init__(self, *, post_id=POST_ID, script=None, on_write=None):
        self._post_id = post_id
        self._script = list(script) if script else []
        self._on_write = on_write
        self.calls = []

    def __call__(self, method, url, headers, body):
        call = {"method": method, "url": url}
        try:
            call["body"] = json.loads(body) if body else None
        except ValueError:
            call["body"] = None
        self.calls.append(call)
        if self._on_write is not None:
            self._on_write(call)
        if self._script:
            return self._script.pop(0)
        if method == "POST":
            return 201, json.dumps({"id": self._post_id}).encode()
        if method == "GET":
            return 200, b"[]"
        if method == "DELETE":
            return 204, b""
        comment_id = int(url.rsplit("/", 1)[-1])
        return 200, json.dumps({"id": comment_id}).encode()

    def methods(self):
        return [call["method"] for call in self.calls]


class Harness:
    """Worker pipeline doubles: table + scripted collaborators sharing one sink."""

    def __init__(self, *, meta, files=None, llm_script=None, github=None, ssm_values=None):
        self.table = InMemoryTable()
        self.ssm = FakeSSM(dict(SSM_VALUES) if ssm_values is None else ssm_values)
        self.provider = ConfigProvider(
            self.ssm.get_parameters, allowed_endpoint_hosts=("llm.example.test",)
        )
        self.diff = FakeDiffTransport(meta=meta, files=files)
        self.llm_script = list(llm_script) if llm_script else []
        self.llm_conns = []
        self.github = github if github is not None else FakeGitHub()
        self.sink = []

    def _factory(self, host, port, *, timeout):
        return FakeLLMConnection(self.llm_script, self.llm_conns)

    def run(self, payload, **overrides):
        kwargs = {
            "_table": self.table,
            "_config_provider": self.provider,
            "_now": lambda: NOW,
            "_diff_transport": self.diff,
            "_llm_factory": self._factory,
            "_github_transport": self.github,
            "_sink": self.sink.append,
            "_system_prompt": "SYSTEM-PROMPT",
        }
        kwargs.update(overrides)
        return handler(sqs_event(payload), None, **kwargs)

    def log_lines(self):
        return [json.loads(line) for line in self.sink]


def _seed(table, *, head, seen=None, gen=0, owner=GUID_1, until=NOW + 100, comment=None):
    item = {
        "pk": PK,
        "status": "ACTIVE" if comment is not None else "CLAIMED",
        "generation": gen,
        "head_sha": head,
        "last_seen_sha": head if seen is None else seen,
        "claim_owner": owner,
        "claim_until": until,
        "updated_at": UPDATED_AT,
    }
    if comment is not None:
        item["comment_id"] = comment
    table.items[PK] = item
    return item


# --- happy path: establish → diff → LLM → gate → claim → fence → publish → finalize ---


def test_first_delivery_posts_and_finalizes():
    """Empty table → reconcile: GET list (empty) → creation lease → POST →
    persist → re-check → ACTIVE with the new id."""
    h = Harness(meta=[(200, SHA_B)])
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["published"]}
    # Review-stage prior read, then reconcile: list → lease-POST → re-check.
    assert h.github.methods() == ["GET", "GET", "POST", "GET"]
    assert h.github.calls[2]["url"].endswith(f"/repos/{REPO}/issues/{PR_NUMBER}/comments")
    item = h.table.items[PK]
    assert item["status"] == "ACTIVE"
    assert item["head_sha"] == SHA_B
    assert item["generation"] == 0
    assert item["comment_id"] == POST_ID
    (line,) = h.log_lines()
    assert line["status"] == "published"
    assert line["pipeline"] == "single_pass"  # T077: routing discriminator on the log line
    assert line["generation"] == 0
    assert line["token_usage"] == 15
    assert line["stale_discarded"] is False  # nothing stale discarded on this path


def test_new_revision_posts_pending_reconcile():
    """New SHA clears comment_id at establish (c) → reconcile finds no
    marker comment → lease → POST; convergence of pre-existing marker
    comments is T039 reconcile scope, pinned in test_reconcile.py."""
    h = Harness(meta=[(200, SHA_B), (200, SHA_B)])
    _seed(h.table, head=SHA_A, gen=5, comment=111)
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["published"]}
    assert h.github.methods() == ["GET", "GET", "POST", "GET"]
    assert h.table.items[PK]["comment_id"] == POST_ID
    assert h.table.items[PK]["generation"] == 6


def test_replay_footer_on_published_comment(monkeypatch):
    """T075 wiring (single-pass): the POSTed canonical body carries the
    replay footer with pr_number + head_sha interpolated (PR #169 r1
    MEDIUM — the publish-boundary wiring is pinned end-to-end, not just
    the helper in isolation)."""
    monkeypatch.setenv("REPLAY_BASE_URL", "https://viewer.example.test")
    h = Harness(meta=[(200, SHA_B)])
    assert h.run(envelope(sha=SHA_B)) == {"ok": True, "results": ["published"]}
    posted = next(c for c in h.github.calls if c["method"] == "POST")["body"]["body"]
    assert (
        f"🔬 [Full agent replay](https://viewer.example.test/runs/{PR_NUMBER}/{SHA_B}/)" in posted
    )


def test_replay_footer_absent_when_env_unset(monkeypatch):
    monkeypatch.delenv("REPLAY_BASE_URL", raising=False)
    h = Harness(meta=[(200, SHA_B)])
    h.run(envelope(sha=SHA_B))
    posted = next(c for c in h.github.calls if c["method"] == "POST")["body"]["body"]
    assert "Full agent replay" not in posted


def test_replay_footer_exactly_once_across_republish(monkeypatch):
    """PR #169 r2 MEDIUM: same-revision re-publish re-composes +
    re-sanitizes + re-decorates — the final PATCHed body carries exactly
    one footer (the exactly-once contract proven through the real PATCH
    path, not just the helper)."""
    monkeypatch.setenv("REPLAY_BASE_URL", "https://viewer.example.test")
    h = Harness(meta=[(200, SHA_B)])
    assert h.run(envelope(sha=SHA_B)) == {"ok": True, "results": ["published"]}
    assert h.run(envelope(sha=SHA_B)) == {"ok": True, "results": ["published"]}
    patched = next(c for c in h.github.calls if c["method"] == "PATCH")["body"]["body"]
    assert patched.count("Full agent replay") == 1
    assert (
        f"🔬 [Full agent replay](https://viewer.example.test/runs/{PR_NUMBER}/{SHA_B}/)" in patched
    )


def test_same_revision_replay_patches_same_comment_never_reposts():
    """Mars ruling 2: no ACTIVE same-owner short-circuit — sequential
    same-owner+SHA replay publishes again; ruling 1: that re-publish is a
    PATCH of the stored comment, never a second POST."""
    h = Harness(meta=[(200, SHA_B)])
    first = h.run(envelope(sha=SHA_B))
    assert first == {"ok": True, "results": ["published"]}
    second = h.run(envelope(sha=SHA_B))
    assert second == {"ok": True, "results": ["published"]}
    assert h.github.methods() == ["GET", "GET", "POST", "GET", "GET", "PATCH"]
    assert h.github.calls[5]["url"].endswith(f"/issues/comments/{POST_ID}")
    assert h.table.items[PK]["comment_id"] == POST_ID


# --- discard outcomes: log, complete, no publish, no retry ---


def test_concurrent_different_owner_discards_claim_held():
    """Live lease held by another owner → DISCARDED_CLAIM_HELD; fence and
    publish never run; the stored record is byte-identical."""
    h = Harness(meta=[(200, SHA_B)])
    before = _seed(h.table, head=SHA_B, gen=2, owner=GUID_OTHER, until=NOW + 100)
    result = h.run(envelope(sha=SHA_B, guid=GUID_2))
    assert result == {"ok": True, "results": ["discarded_claim_held"]}
    # Review ran (prior-context GET) but fence and publish never did.
    assert h.github.methods() == ["GET"]
    assert len(h.llm_conns) == 1  # establish (b) + review precede the claim
    assert h.table.items[PK] == before
    (line,) = h.log_lines()
    assert line["status"] == "discarded_claim_held"


def test_superseded_sha_discards_without_publish():
    """Incoming SHA is neither last_seen nor live → superseded; the review
    stage never runs (no LLM connection), nothing is published. The ONLY
    state change is the US3.AC1 observation (`last_seen_sha` advances to
    the incoming SHA); everything else is byte-identical."""
    h = Harness(meta=[(200, SHA_A)])
    before = _seed(h.table, head=SHA_A, gen=3, comment=111)
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["discarded_superseded"]}
    assert h.llm_conns == []
    assert h.github.calls == []
    item = h.table.items[PK]
    assert item["last_seen_sha"] == SHA_B
    assert {k: v for k, v in item.items() if k != "last_seen_sha"} == {
        k: v for k, v in before.items() if k != "last_seen_sha"
    }


def test_fence_mismatch_discards_stale():
    """A newer push lands between claim and fence → stale; publish NEVER
    runs; the record is left claimed (lease expiry recovers it)."""
    h = Harness(meta=[(200, SHA_B), (200, SHA_C)])
    _seed(h.table, head=SHA_A, gen=4, comment=111)
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["discarded_stale"]}
    # Review ran (prior-context GET) but publish NEVER did.
    assert h.github.methods() == ["GET"]
    item = h.table.items[PK]
    assert item["status"] == "CLAIMED"
    assert item["head_sha"] == SHA_B
    assert "comment_id" not in item


def test_finalize_conflict_completes_without_retry():
    """A newer accepted revision lands between publish and finalize →
    log-and-reconcile path; the newer record is never overwritten."""
    landed = {}

    def concurrent_landing(call):
        # The modeled race lands between publish (POST) and finalize: the
        # reconcile list GETs that precede the POST must not trigger it.
        if not landed and call["method"] == "POST":
            landed["moved"] = True
            _seed(h.table, head=SHA_C, gen=6, owner=GUID_OTHER)

    h = Harness(
        meta=[(200, SHA_B), (200, SHA_B)],
        github=FakeGitHub(on_write=concurrent_landing),
    )
    _seed(h.table, head=SHA_A, gen=4, comment=111)
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["published_finalize_conflict"]}
    assert h.table.items[PK]["head_sha"] == SHA_C
    assert h.table.items[PK]["generation"] == 6


def test_finalize_loser_claim_aborts_without_clearing_newer_lease():
    """SPR-63 owner guard: a same-revision lease takeover (expiry +
    re-claim by another owner) lands between publish and finalize → the
    loser's finalize fails its guard → log-and-reconcile path; the newer
    lease is NOT cleared and the loser's comment id never lands."""
    landed = {}

    def lease_takeover(call):
        # The modeled race lands between publish (POST) and finalize: the
        # reconcile list GETs that precede the POST must not trigger it.
        if not landed and call["method"] == "POST":
            landed["moved"] = True
            h.table.items[PK]["claim_owner"] = GUID_OTHER
            h.table.items[PK]["claim_until"] = NOW + 180

    h = Harness(
        meta=[(200, SHA_B), (200, SHA_B)],
        github=FakeGitHub(on_write=lease_takeover),
    )
    _seed(h.table, head=SHA_A, gen=4, comment=111)
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["published_finalize_conflict"]}
    item = h.table.items[PK]
    assert item["head_sha"] == SHA_B
    assert item["generation"] == 5
    assert item["claim_owner"] == GUID_OTHER  # newer lease survives
    assert item["claim_until"] == NOW + 180
    assert "comment_id" not in item  # loser's id never landed


# --- worker SQS client cache (SPR-63 G6-F2) ----------------------------------


def _wireless_run(h):
    """Drive the handler past SQS-client construction without pipeline work."""
    return handler(
        {"Records": []},
        None,
        _table=h.table,
        _config_provider=h.provider,
        _now=lambda: NOW,
        _sink=h.sink.append,
        _system_prompt="SYSTEM-PROMPT",
    )


def test_sqs_client_cached_across_warm_invocations(monkeypatch):
    # Given the uninjected production SQS path Then one client serves both invocations
    import boto3

    worker_handler.reset_sqs_cache()
    try:
        calls = []
        fake = object()

        def fake_client(service, *args, **kwargs):
            assert service == "sqs", service
            calls.append(service)
            return fake

        monkeypatch.setattr(boto3, "client", fake_client)
        h = Harness(meta=[(200, SHA_B)])
        assert _wireless_run(h) == {"ok": True, "results": []}
        assert _wireless_run(h) == {"ok": True, "results": []}
        assert calls == ["sqs"]  # built once, cache hit on the second run
    finally:
        worker_handler.reset_sqs_cache()


def test_reset_sqs_cache_forces_rebuild(monkeypatch):
    # Given a warm SQS cache Then reset forces the next run to build again
    import boto3

    worker_handler.reset_sqs_cache()
    try:
        calls = []

        def fake_client(service, *args, **kwargs):
            assert service == "sqs", service
            calls.append(service)
            return object()

        monkeypatch.setattr(boto3, "client", fake_client)
        h = Harness(meta=[(200, SHA_B)])
        assert _wireless_run(h) == {"ok": True, "results": []}
        worker_handler.reset_sqs_cache()
        assert _wireless_run(h) == {"ok": True, "results": []}
        assert calls == ["sqs", "sqs"]
    finally:
        worker_handler.reset_sqs_cache()


# --- worker system-prompt cache (audit R1) ----------------------------------


def _uncached_run(h):
    """Drive the handler past prompt resolution without pipeline work and
    without injecting `_system_prompt` (the production path)."""
    return handler(
        {"Records": []},
        None,
        _table=h.table,
        _config_provider=h.provider,
        _now=lambda: NOW,
        _sink=h.sink.append,
        _sqs=object(),
    )


def test_system_prompt_cached_across_warm_invocations(monkeypatch):
    # Given the uninjected production prompt path Then disk is hit once
    worker_handler.reset_prompt_cache()
    try:
        calls = []

        def counting_load():
            calls.append(1)
            return "CACHED-PROMPT"

        monkeypatch.setattr(worker_handler, "_load_system_prompt", counting_load)
        h = Harness(meta=[(200, SHA_B)])
        assert _uncached_run(h) == {"ok": True, "results": []}
        assert _uncached_run(h) == {"ok": True, "results": []}
        assert calls == [1]  # loaded once, cache hit on the second run
    finally:
        worker_handler.reset_prompt_cache()


def test_reset_prompt_cache_forces_reload(monkeypatch):
    # Given a warm prompt cache Then reset forces the next run to load again
    worker_handler.reset_prompt_cache()
    try:
        calls = []

        def counting_load():
            calls.append(1)
            return "CACHED-PROMPT"

        monkeypatch.setattr(worker_handler, "_load_system_prompt", counting_load)
        h = Harness(meta=[(200, SHA_B)])
        assert _uncached_run(h) == {"ok": True, "results": []}
        worker_handler.reset_prompt_cache()
        assert _uncached_run(h) == {"ok": True, "results": []}
        assert calls == [1, 1]
    finally:
        worker_handler.reset_prompt_cache()


def test_injected_system_prompt_bypasses_cache(monkeypatch):
    # Given an explicitly injected prompt Then the loader never runs and the
    # cache stays empty
    worker_handler.reset_prompt_cache()
    try:

        def exploding_load():
            raise AssertionError("injected path must not touch the loader")

        monkeypatch.setattr(worker_handler, "_load_system_prompt", exploding_load)
        h = Harness(meta=[(200, SHA_B)])
        assert _wireless_run(h) == {"ok": True, "results": []}
        assert worker_handler._SYSTEM_PROMPT is None
    finally:
        worker_handler.reset_prompt_cache()


# --- envelope boundary: untrusted SQS input completes, never raises ---


def test_malformed_body_completes():
    """Non-JSON body → invalid envelope → complete; no hydration, no work."""
    h = Harness(meta=[(200, SHA_B)])
    result = handler(
        {"Records": [{"body": "not json{", "messageId": "m1"}]},
        None,
        _table=h.table,
        _config_provider=h.provider,
        _now=lambda: NOW,
        _diff_transport=h.diff,
        _llm_factory=h._factory,
        _github_transport=h.github,
        _sink=h.sink.append,
        _system_prompt="SYSTEM-PROMPT",
    )
    assert result == {"ok": True, "results": ["discarded_invalid_envelope"]}
    assert h.ssm.calls == []
    assert h.table.items == {}
    assert h.github.calls == []


def test_schema_invalid_envelope_completes():
    """Schema-violating envelope (bad SHA) → complete, nothing enqueued downstream."""
    h = Harness(meta=[(200, SHA_B)])
    bad = envelope()
    bad["head_sha"] = "zz"
    result = h.run(bad)
    assert result == {"ok": True, "results": ["discarded_invalid_envelope"]}
    assert h.ssm.calls == []
    assert h.table.items == {}
    assert h.github.calls == []


def test_config_error_completes():
    """Unhydratable config (missing endpoint) → complete, never raises."""
    values = {k: v for k, v in SSM_VALUES.items() if k != "/pr-reviewer/glm-endpoint"}
    h = Harness(meta=[(200, SHA_B)], ssm_values=values)
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["discarded_error"]}
    assert h.github.calls == []
    (line,) = h.log_lines()
    assert line["status"] == "discarded_error"
    assert line["error_class"] == "config_glm_endpoint_missing"


# --- the single 401 re-fetch: the ONLY in-request retry ---


def test_llm_401_refreshes_once_then_publishes():
    """First LLM call 401s → cache-bust → re-fetch → retry succeeds; exactly
    two SSM fetches total (one initial, one re-fetch)."""
    h = Harness(
        meta=[(200, SHA_B)],
        llm_script=[("response", 401, b"{}"), ("response", 200, completion_body())],
    )
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["published"]}
    assert len(h.ssm.calls) == 2
    assert h.github.methods() == ["GET", "GET", "POST", "GET"]


def test_llm_401_twice_completes():
    """Auth failure after the single re-fetch is terminal: complete, no
    second re-fetch (still exactly two SSM fetches), no raise. D2: LLM-401
    is a permanent row, so the fixed-template notice publishes alongside
    completion (the review itself never publishes)."""
    h = Harness(
        meta=[(200, SHA_B)],
        llm_script=[("response", 401, b"{}"), ("response", 401, b"{}")],
    )
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["discarded_error"]}
    assert len(h.ssm.calls) == 2
    assert h.github.methods() == ["GET", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["status"] == "discarded_error"
    assert line["error_class"] == "http_401"
    assert line["failure_notice_published"] == "true"


def test_diff_401_refreshes_once():
    """GitHub-side 401 on the diff fetch shares the same single budget:
    re-fetch once, then proceed to publish."""
    h = Harness(meta=[(401, None), (200, SHA_B), (200, SHA_B)])
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["published"]}
    assert len(h.ssm.calls) == 2


# --- boundary rows with behavior coverage ---


def test_assemble_refused_completes():
    """Model output failing the validate gate → AssembleError → complete
    (never published, never retried). D2: assemble-invalid is permanent,
    so the fixed-template notice publishes (only the template goes out —
    the invalid content never does)."""
    h = Harness(
        meta=[(200, SHA_B)],
        llm_script=[("response", 200, completion_body("plain text without sections"))],
    )
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["discarded_error"]}
    assert h.github.methods() == ["GET", "GET", "POST", "GET"]
    (post,) = [call for call in h.github.calls if call["method"] == "POST"]
    assert "plain text" not in post["body"]["body"]  # template only, never the invalid content
    (line,) = h.log_lines()
    assert line["status"] == "discarded_error"
    assert line["error_class"].startswith("assemble_")
    assert line["failure_notice_published"] == "true"


def test_llm_timeout_raises_for_queue_retry():
    """LLM read timeout on the FIRST delivery is side-effect-free → the
    friendly RETRYING notice posts instantly, then raise; `retry_queued`
    logged with `failure_notice_published: "true"`."""
    h = Harness(meta=[(200, SHA_B)], llm_script=[("raise", TimeoutError("read timed out"))])
    with pytest.raises(LlmError) as exc_info:
        h.run(envelope(sha=SHA_B))
    assert exc_info.value.error_class == "timeout"
    # Review ran through the prior-context GET before the LLM failed; the
    # first-failure notice added the reconcile list GET, POST, re-check.
    assert h.github.methods() == ["GET", "GET", "POST", "GET"]
    (post,) = [call for call in h.github.calls if call["method"] == "POST"]
    assert "retried automatically" in post["body"]["body"]
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["error_class"] == "timeout"
    assert line["failure_notice_published"] == "true"


def test_llm_invalid_output_raises_for_queue_retry():
    """Structurally unusable LLM output → RETRYING notice on first
    delivery, then raise for the bounded queue retry (the invalid content
    itself is never published)."""
    h = Harness(meta=[(200, SHA_B)], llm_script=[("response", 200, b"{not json")])
    with pytest.raises(LlmError) as exc_info:
        h.run(envelope(sha=SHA_B))
    assert exc_info.value.error_class == "invalid_response"
    assert h.github.methods() == ["GET", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["failure_notice_published"] == "true"


def _validating_llm_factory(harness):
    """Wrap the harness LLM factory so `request()` runs the REAL stdlib
    header validation first — replays production `putheader` rejection of
    control-char key material without any network."""
    real_factory = harness._factory

    def factory(host, port, *, timeout):
        conn = real_factory(host, port, timeout=timeout)
        orig_request = conn.request

        def request(method, path, body=None, headers=None):
            probe = http.client.HTTPConnection(host)
            probe.putrequest(method, path)
            for name, value in (headers or {}).items():
                probe.putheader(name, value)
            return orig_request(method, path, body=body, headers=headers)

        conn.request = request
        return conn

    return factory


def test_newline_key_replays_to_terminal_invalid_key_with_notice():
    """Live-incident replay (SPR-58): a trailing-newline GLM key used to
    crash invisibly (bare ValueError from header validation, retried to
    the DLQ with no notice). Now: typed `invalid_key` → non-retryable →
    terminal `discarded_error` with `error_class="invalid_key"` and the
    immediate `failure_notice_published="true"` — no raise, no retry/DLQ
    loop (the review itself never publishes; only the fixed template)."""
    values = dict(SSM_VALUES)
    values["/pr-reviewer/glm-api-key"] = "glm-key-value\n"  # noqa: S105 (newline-key fixture)
    h = Harness(meta=[(200, SHA_B)], ssm_values=values)
    result = h.run(envelope(sha=SHA_B), _llm_factory=_validating_llm_factory(h))
    assert result == {"ok": True, "results": ["discarded_error"]}
    assert h.github.methods() == ["GET", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["status"] == "discarded_error"
    assert line["error_class"] == "invalid_key"
    assert line["failure_notice_published"] == "true"


def test_diff_transport_error_raises_for_queue_retry():
    """F4 `transport_error` (DNS/refused/reset class) → raise for
    redelivery. The RETRYING notice is authorized (first transient
    failure) but its fenced publish cannot clear the live-head fence —
    the same broken transport raises at the fence — so the best-effort
    notice lands `false` and GitHub is never touched."""
    h = Harness(meta=[DiffError("request", "transport_error")])
    with pytest.raises(DiffError) as exc_info:
        h.run(envelope(sha=SHA_B))
    assert exc_info.value.reason == "transport_error"
    assert h.github.calls == []
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["failure_notice_published"] == "false"


def test_diff_403_completes():
    """GitHub 403 on the diff fetch (lost access) → complete, no re-fetch."""
    h = Harness(meta=[(403, None)])
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["discarded_error"]}
    assert len(h.ssm.calls) == 1
    assert h.github.calls == []
    (line,) = h.log_lines()
    assert line["error_class"] == "diff_http_error_403"


def test_github_post_500_raises_for_queue_retry():
    """Transient GitHub 5xx on the review publish → raise; the review
    content never lands (the script's 500 hits the publish reconcile
    call). The first-failure RETRYING notice then runs its own fenced
    publish against GitHub's default replies — and that notice run, by
    D2 design, finalizes the record ACTIVE with the notice comment
    persisted."""
    h = Harness(meta=[(200, SHA_B)], github=FakeGitHub(script=[(200, b"[]"), (500, b"boom")]))
    with pytest.raises(GitHubError) as exc_info:
        h.run(envelope(sha=SHA_B))
    assert exc_info.value.status == 500
    # Only the NOTICE landed: one POST total, carrying the notice (never
    # the review), and the notice's fenced run finalized the record.
    posts = [call for call in h.github.calls if call["method"] == "POST"]
    assert len(posts) == 1
    assert "retried automatically" in posts[0]["body"]["body"]
    assert h.table.items[PK]["status"] == "ACTIVE"
    assert h.table.items[PK]["comment_id"] == POST_ID
    assert h.github.methods() == ["GET", "GET", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["failure_notice_published"] == "true"


def test_github_patch_404_with_unreadable_list_completes():
    """PATCH 404 recovery runs the item-8 decision table (T040): the stored
    id is proven dead, so it is cleared first; then the list GET is 403
    (lost access — the list-unreadable row), so the run completes
    non-retryably with nothing persisted. Marker-found and lease-POST rows
    are pinned by `tests/unit/test_patch404_table.py`."""
    h = Harness(
        meta=[(200, SHA_B)], github=FakeGitHub(script=[(200, b"[]"), (404, b"{}"), (403, b"{}")])
    )
    _seed(h.table, head=SHA_B, gen=2, comment=555)
    result = h.run(envelope(sha=SHA_B))
    assert result == {"ok": True, "results": ["discarded_error"]}
    assert h.github.methods() == ["GET", "PATCH", "GET"]
    assert "comment_id" not in h.table.items[PK]  # dead id cleared; next run converges


# --- boundary table pin (HLD §2.3 item 8, T034 scope) ---


def test_is_retryable_table():
    """Executable form of the item-8 mapping: retryable faults propagate,
    permanent faults complete, unknown faults retry into the queue budget."""
    assert is_retryable(DiffError("request", "transport_error")) is True
    assert is_retryable(DiffError("pr", "http_error", status=429)) is True
    assert is_retryable(DiffError("pr", "http_error", status=500)) is True
    assert is_retryable(DiffError("response", "bad_shape")) is True
    assert is_retryable(DiffError("pr", "http_error", status=401)) is False
    assert is_retryable(DiffError("pr", "http_error", status=403)) is False
    assert is_retryable(DiffError("files", "http_error", status=404)) is False
    assert is_retryable(DiffError("repo_full_name", "bad_repo")) is False

    assert is_retryable(LlmError("timeout")) is True
    assert is_retryable(LlmError("connection_error")) is True
    assert is_retryable(LlmError("http_429")) is True
    assert is_retryable(LlmError("http_503")) is True
    assert is_retryable(LlmError("invalid_response")) is True
    # HLD-004 §9 boundary pin: the new classes ride the unknown-fault
    # default (retryable transients, no D2 notice row).
    assert is_retryable(LlmError("length")) is True
    assert is_retryable(LlmError("rate_limit")) is True
    assert is_retryable(LlmError("bad_endpoint")) is False
    assert is_retryable(LlmError("http_401")) is False
    assert is_retryable(LlmError("invalid_key")) is False
    assert is_retryable(LlmError("http_400")) is False

    assert is_retryable(GitHubError(500, "http_500")) is True
    assert is_retryable(GitHubError(429, "http_429")) is True
    assert is_retryable(GitHubError(None, "transport_error")) is True
    assert is_retryable(GitHubError(None, "invalid_response")) is True
    assert is_retryable(GitHubError(401, "http_401")) is False
    assert is_retryable(GitHubError(403, "http_403")) is False
    assert is_retryable(GitHubError(404, "http_404")) is False

    assert is_retryable(AssembleError.__new__(AssembleError)) is False
    assert is_retryable(ConfigError("glm_endpoint", "bad_scheme")) is False
    assert is_retryable(ValueError("unknown")) is True


# --- observability (HLD §5.4): fixed fields only, never secrets ---


def test_log_event_carries_fixed_fields_only():
    """Emitted lines carry exactly FIXED_FIELDS — no tokens, prompts, or
    diffs — and name the prompt version plus the delivered-prompt hash
    (SPR-62: sha256 of the exact prompt text passed to the model, hash
    only, never content)."""
    h = Harness(meta=[(200, SHA_B)])
    h.run(envelope(sha=SHA_B))
    (line,) = h.log_lines()
    assert set(line) == set(FIXED_FIELDS)
    assert line["prompt_version"] == PROMPT_VERSION
    assert line["prompt_sha256"] == hashlib.sha256(b"SYSTEM-PROMPT").hexdigest()
    assert line["repo_full_name"] == REPO
    assert line["pr_number"] == PR_NUMBER
    assert line["head_sha"] == SHA_B
    assert line["delivery_guid"] == GUID_1
    raw = h.sink[0]
    for forbidden in ("github-token-value", "glm-key-value", "SYSTEM-PROMPT", REVIEW_BODY):
        assert forbidden not in raw


def test_load_system_prompt_resolves_zip_layout(tmp_path, monkeypatch):
    """The Lambda archive ships prompts/ beside the handler; the loader
    must read the contract file there (T035 packaging)."""
    monkeypatch.delenv("SYSTEM_PROMPT", raising=False)
    task = tmp_path / "task"
    (task / "prompts").mkdir(parents=True)
    (task / "prompts" / "system_prompt.md").write_text("zip prompt", encoding="utf-8")
    monkeypatch.setattr(worker_handler, "__file__", str(task / "worker_handler.py"))
    assert worker_handler._load_system_prompt() == "zip prompt"


def test_load_system_prompt_falls_back_to_repo_layout(tmp_path, monkeypatch):
    """A repo checkout keeps prompts/ one level above lambda/; when the
    zip layout is absent the loader walks up (dev parity)."""
    monkeypatch.delenv("SYSTEM_PROMPT", raising=False)
    repo = tmp_path / "repo"
    (repo / "lambda").mkdir(parents=True)
    (repo / "prompts").mkdir()
    (repo / "prompts" / "system_prompt.md").write_text("repo prompt", encoding="utf-8")
    monkeypatch.setattr(worker_handler, "__file__", str(repo / "lambda" / "worker_handler.py"))
    assert worker_handler._load_system_prompt() == "repo prompt"

"""T051: error-classification rows (HLD §2.3 item 8; US5.AC1–AC3;
FR-017–FR-021).

Drives the REAL `worker_handler.handler` with scripted collaborators,
pinning every verbatim row:

* PATCH-404 variants → recovery, still published (never raised) →
  `test_patch_404_migrated_adopts`
* 403/404 list-read → non-retryable complete →
  `test_list_403_completes`
* 429 + `Retry-After` → `ChangeMessageVisibility`, then raise →
  `test_github_429_retry_after_adjusts_visibility`
* 5xx → raise for queue retry, no visibility touch without a hint →
  `test_github_500_raises_without_visibility`
* LLM 429 → raise; headers die inside `common.llm`, so no visibility
  adjustment is possible at the worker edge (pinned limitation) →
  `test_llm_429_raises_without_visibility_adjustment`
* 401 → single re-fetch, then non-retryable complete →
  `test_401_twice_completes_after_single_refetch`
* LLM timeout / invalid output → raise for queue retry →
  `test_llm_timeout_raises`, `test_llm_invalid_output_raises`
* assembled-comment validation failure → non-retryable complete, never
  published → `test_assemble_refused_completes`
* D2 (T054 wiring): final-attempt transient → notice published, then the
  original error still raises →
  `test_final_attempt_transient_publishes_notice_then_raises`
* D2: permanent assemble failure → notice immediately, complete →
  `test_assemble_failure_publishes_notice_immediately`
* D2: GitHub-401 → no notice, `false` →
  `test_github_401_skips_notice`

Doubles are local to this module (`tests/unit` cannot import the
`tests/state_machine/dynamodb_stub.py` port): `FakeTable` is deliberately
permissive on conditions — condition-string strictness is pinned by the
state-machine tier. The `Harness` passes `_sqs` only when the handler
signature accepts it (inspected once): pre-wiring the SQS edge is absent,
so Retry-After/D2 tests fail behaviorally (no visibility call / missing
log field) rather than erroring on an unknown kwarg.

SQS record shape follows the Lambda event contract: `receiptHandle` plus
`attributes.ApproximateReceiveCount` (a STRING on the wire).
"""

import inspect
import json

import pytest

from common.failure_notice import NoticeTrigger
from common.llm import LlmError
from worker_handler import GitHubError, _notice_trigger, handler

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
MARKER = "<!-- pr-reviewer:canonical:v1:octo-org/hello-world#42 -->"
SHA_B = "bb" * 20
BASE_SHA = "00" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
NOW = 1_750_000_000
STORED_ID = 555
NOTICE_ID = 888
UPDATED_AT = "2026-09-12T10:00:00Z"
ENDPOINT = "https://llm.example.test/v1/chat/completions"
QUEUE_URL = "https://sqs.us-west-2.amazonaws.test/123456789012/pr-reviewer-work"

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

_HANDLER_ACCEPTS_SQS = "_sqs" in inspect.signature(handler).parameters


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


def _sqs_event(payload, *, receive_count="1", receipt="rh-1"):
    return {
        "Records": [
            {
                "body": json.dumps(payload),
                "messageId": "m1",
                "receiptHandle": receipt,
                "attributes": {"ApproximateReceiveCount": receive_count},
            }
        ]
    }


class FakeTable:
    """Record double with faithful write application but permissive
    conditions: SET/REMOVE clauses are applied (mirroring the
    state-machine stub's applier) so multi-step flows observe realistic
    state, while guard strictness stays pinned by the state-machine tier."""

    def __init__(self, seed_comment=None):
        self.items = {}
        if seed_comment is not None:
            self.items[PK] = {
                "pk": PK,
                "status": "ACTIVE",
                "generation": 2,
                "head_sha": SHA_B,
                "last_seen_sha": SHA_B,
                "comment_id": seed_comment,
                "updated_at": UPDATED_AT,
            }

    def get_item(self, pk):
        item = self.items.get(pk)
        return dict(item) if item is not None else None

    def update_item(self, **kwargs):
        update = kwargs.get("UpdateExpression", "")
        values = kwargs.get("ExpressionAttributeValues") or {}
        if self.items.get(PK) is None:
            self.items[PK] = {"pk": PK}
        text = update.replace("#st", "status")
        if text.startswith("REMOVE ") and " = " not in text:
            for attr in text[len("REMOVE ") :].split(", "):
                self.items[PK].pop(attr.strip(), None)
            return {"Attributes": dict(self.items[PK])}
        set_part = text[4:].partition(" REMOVE ")[0] if text.startswith("SET ") else ""
        for clause in set_part.split(", "):
            attr, _, placeholder = clause.partition(" = ")
            attr, placeholder = attr.strip(), placeholder.strip()
            if placeholder.startswith(":") and placeholder in values:
                self.items[PK][attr] = values[placeholder]
        return {"Attributes": dict(self.items[PK])}


class ScriptedGitHub:
    """Ordered (status, body[, headers]) script; records (method, url).
    Three-tuples carry response headers (e.g. Retry-After); the worker
    unpacks defensively so two-tuple doubles keep working."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "body": body})
        assert self._script, f"unexpected extra GitHub call: {method} {url}"
        return self._script.pop(0)

    def methods(self):
        return [call["method"] for call in self.calls]

    def bodies(self, method):
        return [
            json.loads(call["body"].decode()) for call in self.calls if call["method"] == method
        ]


class StatefulGitHub:
    """Comment-store double: the list GET returns the ACCUMULATED state
    (POST assigns ids and stores, PATCH updates in place, DELETE removes)
    instead of a canned script — so a later delivery's prior-comment read
    observes what earlier deliveries actually published, exactly as the
    live API would. Records calls like `ScriptedGitHub`."""

    def __init__(self, *, first_id=NOTICE_ID):
        self.comments: dict[int, str] = {}
        self._next_id = first_id
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "body": body})
        if method == "GET":
            page = [{"id": cid, "body": text} for cid, text in sorted(self.comments.items())]
            return 200, json.dumps(page).encode()
        if method == "POST":
            comment_id = self._next_id
            self._next_id += 1
            self.comments[comment_id] = json.loads(body.decode())["body"]
            return 201, json.dumps({"id": comment_id}).encode()
        if method == "PATCH":
            comment_id = int(url.rsplit("/", 1)[-1])
            self.comments[comment_id] = json.loads(body.decode())["body"]
            return 200, json.dumps({"id": comment_id}).encode()
        if method == "DELETE":
            comment_id = int(url.rsplit("/", 1)[-1])
            self.comments.pop(comment_id, None)
            return 204, b""
        raise AssertionError(f"unexpected GitHub method: {method}")

    def methods(self):
        return [call["method"] for call in self.calls]

    def bodies(self, method):
        return [
            json.loads(call["body"].decode()) for call in self.calls if call["method"] == method
        ]


class FakeSSM:
    def __init__(self, values):
        self.values = dict(values)

    def get_parameters(self, Names, WithDecryption=False):  # noqa: N803 (boto3 shape)
        params = [
            {"Name": name, "Value": self.values[name]} for name in Names if name in self.values
        ]
        return {"Parameters": params, "InvalidParameters": []}


class FakeSQS:
    """SQS double: records visibility extensions (the Retry-After edge)."""

    def __init__(self):
        self.visibility_calls = []

    def change_message_visibility(self, QueueUrl, ReceiptHandle, VisibilityTimeout):  # noqa: N803 (boto3 shape)
        self.visibility_calls.append(
            {
                "QueueUrl": QueueUrl,
                "ReceiptHandle": ReceiptHandle,
                "VisibilityTimeout": VisibilityTimeout,
            }
        )
        return {}


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
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class _FakeLLMConnection:
    def __init__(self, script, seen):
        self._script = script
        self.sock = _FakeLLMSocket()
        self.request_bodies = []
        seen.append(self)

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        if body is not None:
            self.request_bodies.append(body)

    def getresponse(self):
        action = self._script.pop(0)
        if action[0] == "raise":
            raise action[1]
        return _FakeLLMResponse(action[1], action[2])

    def close(self):
        pass


def _completion(content=REVIEW_BODY):
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode()


class Harness:
    def __init__(self, *, meta, llm_script=None, github, sqs=None, table=None):
        from common.config import ConfigProvider

        self.table = table if table is not None else FakeTable()
        self.github = github
        self.sqs = sqs if sqs is not None else FakeSQS()
        self.sink = []
        self._provider = ConfigProvider(
            FakeSSM(SSM_VALUES).get_parameters, allowed_endpoint_hosts=("llm.example.test",)
        )
        self._diff = FakeDiffTransport(meta=meta)
        self._llm_script = list(llm_script) if llm_script else [("response", 200, _completion())]
        self._llm_conns = []

    def _factory(self, host, port, *, timeout):
        return _FakeLLMConnection(self._llm_script, self._llm_conns)

    def run(self, *, receive_count="1", receipt="rh-1", env_queue_url=None):
        import os

        sentinel = object()
        previous = os.environ.get("WORK_QUEUE_URL", sentinel)
        if env_queue_url is not None:
            os.environ["WORK_QUEUE_URL"] = env_queue_url
        try:
            kwargs = {
                "_table": self.table,
                "_config_provider": self._provider,
                "_now": lambda: NOW,
                "_diff_transport": self._diff,
                "_llm_factory": self._factory,
                "_github_transport": self.github,
                "_sink": self.sink.append,
                "_system_prompt": "SYSTEM-PROMPT",
            }
            if _HANDLER_ACCEPTS_SQS:
                kwargs["_sqs"] = self.sqs
            return handler(
                _sqs_event(_envelope(), receive_count=receive_count, receipt=receipt),
                None,
                **kwargs,
            )
        finally:
            if env_queue_url is not None:
                if previous is sentinel:
                    os.environ.pop("WORK_QUEUE_URL", None)
                else:
                    os.environ["WORK_QUEUE_URL"] = previous

    def log_lines(self):
        return [json.loads(line) for line in self.sink]


def _comment(comment_id, body):
    return {"id": comment_id, "body": body}


def _list_body(*comments):
    return json.dumps(list(comments)).encode()


# --- PATCH-404 variants → recovery (published, never raised) ---


def test_patch_404_migrated_adopts():
    """PATCH 555 → 404; list shows the marker comment on 777 → adopted,
    extras reconciled; result `published`, no raise, no DLQ-shaped error."""
    github = ScriptedGitHub(
        [
            (200, _list_body()),  # review-stage prior read (no marker yet)
            (404, b"{}"),
            (200, _list_body(_comment(777, "moved " + MARKER))),
            (200, json.dumps({"id": 777}).encode()),
        ]
    )
    h = Harness(meta=[(200, SHA_B)], github=github, table=FakeTable(seed_comment=555))
    assert h.run() == {"ok": True, "results": ["published"]}
    assert github.methods() == ["GET", "PATCH", "GET", "PATCH"]
    assert h.table.items[PK]["comment_id"] == 777


# --- 403/404 list-read → non-retryable complete ---


def test_list_403_completes():
    """First delivery, list GETs → 403 (lost access): the review-stage
    prior read degrades first, then the publish list fails the same way —
    complete as `discarded_error`, no raise, no POST, nothing persisted."""
    github = ScriptedGitHub([(403, b"{}"), (403, b"{}")])
    h = Harness(meta=[(200, SHA_B)], github=github)
    assert h.run() == {"ok": True, "results": ["discarded_error"]}
    assert github.methods() == ["GET", "GET"]
    assert h.table.items.get(PK, {}).get("comment_id") is None
    (line,) = h.log_lines()
    assert line["status"] == "discarded_error"


# --- 429 + Retry-After → ChangeMessageVisibility, then raise ---


def test_github_429_retry_after_adjusts_visibility():
    """Lease-POST → 429 with `Retry-After: 120` on the FIRST delivery:
    the worker extends the message visibility per the header, then the
    first-failure RETRYING notice publishes, then raises for queue retry
    (the `retry_queued` line is emitted last)."""
    github = ScriptedGitHub(
        [
            (200, _list_body()),  # review-stage prior read (no marker yet)
            (200, _list_body()),
            (429, b'{"message":"throttled"}', {"Retry-After": "120"}),
            (200, _list_body()),  # notice: reconcile list (no marker)
            (201, json.dumps({"id": NOTICE_ID}).encode()),  # notice POST
            (200, _list_body(_comment(NOTICE_ID, "n " + MARKER))),  # re-check
        ]
    )
    h = Harness(meta=[(200, SHA_B)], github=github)
    with pytest.raises(GitHubError) as exc_info:
        h.run(env_queue_url=QUEUE_URL)
    assert exc_info.value.status == 429
    assert h.sqs.visibility_calls == [
        {"QueueUrl": QUEUE_URL, "ReceiptHandle": "rh-1", "VisibilityTimeout": 120}
    ]
    assert github.methods() == ["GET", "GET", "POST", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["failure_notice_published"] == "true"


# --- 5xx → raise, no visibility touch without a hint ---


def test_github_500_raises_without_visibility():
    """Lease-POST → 500 with no Retry-After on the FIRST delivery: the
    first-failure RETRYING notice publishes, then raise for queue retry
    under the queue's own visibility (no extension call without a hint)."""
    github = ScriptedGitHub(
        [
            (200, _list_body()),
            (200, _list_body()),
            (500, b"boom"),
            (200, _list_body()),
            (201, json.dumps({"id": NOTICE_ID}).encode()),
            (200, _list_body(_comment(NOTICE_ID, "n " + MARKER))),
        ]
    )
    h = Harness(meta=[(200, SHA_B)], github=github)
    with pytest.raises(GitHubError) as exc_info:
        h.run(env_queue_url=QUEUE_URL)
    assert exc_info.value.status == 500
    assert h.sqs.visibility_calls == []
    assert github.methods() == ["GET", "GET", "POST", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["failure_notice_published"] == "true"


# --- LLM 429 → raise; no visibility adjustment possible ---


def test_llm_429_raises_without_visibility_adjustment():
    """LLM 429 on the FIRST delivery → the first-failure RETRYING notice
    publishes, then raise for the bounded queue retry. Limitation pinned:
    `Retry-After` headers die inside `common.llm` (which surfaces only
    `error_class`), so the worker edge cannot adjust visibility — the
    queue default applies. `common.llm`/`common.diff` are out of scope
    for the Retry-After plumbing lane."""
    h = Harness(
        meta=[(200, SHA_B)],
        llm_script=[("response", 429, b"{}")],
        github=ScriptedGitHub(
            [
                (200, _list_body()),
                (200, _list_body()),
                (201, json.dumps({"id": NOTICE_ID}).encode()),
                (200, _list_body(_comment(NOTICE_ID, "n " + MARKER))),
            ]
        ),
    )
    with pytest.raises(Exception, match="llm request failed: http_429"):
        h.run(env_queue_url=QUEUE_URL)
    assert h.sqs.visibility_calls == []
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["error_class"] == "http_429"
    assert line["failure_notice_published"] == "true"


# --- 401 → single re-fetch, then non-retryable complete ---


def test_401_twice_completes_after_single_refetch():
    """LLM 401 → cache-bust → re-fetch → 401 again: terminal. Exactly two
    SSM fetches (the single in-request retry budget), no raise,
    `discarded_error`. D2: LLM-401 is a permanent row, so the notice
    publishes immediately alongside completion."""
    notice_id = 888
    github = ScriptedGitHub(
        [
            (200, _list_body()),  # review-stage prior read (no marker yet)
            (200, _list_body()),
            (201, json.dumps({"id": notice_id}).encode()),
            (200, _list_body(_comment(notice_id, "n " + MARKER))),
        ]
    )
    h = Harness(
        meta=[(200, SHA_B), (200, SHA_B)],
        llm_script=[("response", 401, b"{}"), ("response", 401, b"{}")],
        github=github,
    )
    assert h.run() == {"ok": True, "results": ["discarded_error"]}
    assert github.methods() == ["GET", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["status"] == "discarded_error"
    assert line["error_class"] == "http_401"
    assert line["failure_notice_published"] == "true"


# --- LLM timeout / invalid output → raise for queue retry ---


def test_llm_timeout_raises():
    """LLM read timeout on the FIRST delivery is side-effect-free → the
    friendly RETRYING notice publishes instantly, then raise; the
    `retry_queued` line carries `failure_notice_published: true`."""
    h = Harness(
        meta=[(200, SHA_B)],
        llm_script=[("raise", TimeoutError("read timed out"))],
        github=ScriptedGitHub(
            [
                (200, _list_body()),
                (200, _list_body()),
                (201, json.dumps({"id": NOTICE_ID}).encode()),
                (200, _list_body(_comment(NOTICE_ID, "n " + MARKER))),
            ]
        ),
    )
    with pytest.raises(Exception, match="llm request failed: timeout"):
        h.run()
    # Review ran through the prior-context GET before the LLM failed;
    # the notice added a list GET, the POST, and the re-check GET.
    assert h.github.methods() == ["GET", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["failure_notice_published"] == "true"
    posted = h.github.bodies("POST")[0]["body"]
    assert MARKER in posted
    assert "retried automatically" in posted


def test_llm_invalid_output_raises():
    """Structurally unusable LLM output on the FIRST delivery → the
    RETRYING notice publishes, then raise for the bounded queue retry
    (the invalid content itself is never published)."""
    h = Harness(
        meta=[(200, SHA_B)],
        llm_script=[("response", 200, b"{not json")],
        github=ScriptedGitHub(
            [
                (200, _list_body()),
                (200, _list_body()),
                (201, json.dumps({"id": NOTICE_ID}).encode()),
                (200, _list_body(_comment(NOTICE_ID, "n " + MARKER))),
            ]
        ),
    )
    with pytest.raises(Exception, match="llm request failed: invalid_response"):
        h.run()
    # Review ran through the prior-context GET before the LLM failed.
    assert h.github.methods() == ["GET", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["failure_notice_published"] == "true"


# --- assembled-comment validation failure → non-retryable ---


def test_assemble_refused_completes():
    """Model output failing the validate gate → `AssembleError` → complete
    (`discarded_error`): the invalid content itself is never published and
    never retried. D2: assemble-invalid is a permanent row, so the fixed
    template notice publishes immediately (only the template goes out)."""
    notice_id = 888
    github = ScriptedGitHub(
        [
            (200, _list_body()),  # review-stage prior read (no marker yet)
            (200, _list_body()),
            (201, json.dumps({"id": notice_id}).encode()),
            (200, _list_body(_comment(notice_id, "n " + MARKER))),
        ]
    )
    h = Harness(
        meta=[(200, SHA_B), (200, SHA_B)],
        llm_script=[("response", 200, _completion("plain text without sections"))],
        github=github,
    )
    assert h.run() == {"ok": True, "results": ["discarded_error"]}
    assert github.methods() == ["GET", "GET", "POST", "GET"]
    assert all("plain text" not in (call["body"].decode() or "") for call in github.calls)
    (line,) = h.log_lines()
    assert line["status"] == "discarded_error"
    assert line["error_class"].startswith("assemble_")
    assert line["failure_notice_published"] == "true"


# --- D2: final-attempt transient → notice published, original still raises ---


def test_final_attempt_transient_publishes_notice_then_raises():
    """LLM 500 at `ApproximateReceiveCount` 5 (past max 3 — tolerated
    after redrive): the FINAL notice replaces any earlier retrying notice
    through the fenced path (marker + fixed template, adopt-PATCH here),
    the `retry_queued` line carries `failure_notice_published: true`, and
    the ORIGINAL error still raises so DLQ/alert/redrive proceed
    unchanged."""
    github = ScriptedGitHub(
        [
            (200, _list_body()),  # review-stage prior read (no marker yet)
            (200, _list_body()),
            (201, json.dumps({"id": NOTICE_ID}).encode()),
            (200, _list_body(_comment(NOTICE_ID, "n " + MARKER))),
        ]
    )
    h = Harness(
        meta=[(200, SHA_B), (200, SHA_B)],
        llm_script=[("response", 500, b"{}")],
        github=github,
    )
    with pytest.raises(Exception, match="llm request failed: http_500"):
        h.run(receive_count="5", env_queue_url=QUEUE_URL)
    posts = github.bodies("POST")
    assert github.methods() == ["GET", "GET", "POST", "GET"]
    assert MARKER in posts[0]["body"]
    assert "could not be completed" in posts[0]["body"]
    # Redrive tolerance: count 5 > maxReceiveCount 3 is CLAMPED to the
    # configured budget — the notice must not claim attempts that never
    # happened in this configuration (Gate #89 Finding 6).
    assert "after 3 attempts" in posts[0]["body"]
    assert "after 5 attempts" not in posts[0]["body"]
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["failure_notice_published"] == "true"


@pytest.mark.parametrize("receive_count", ["3", "4", "7"])
def test_final_notice_attempts_clamped_to_queue_budget(receive_count):
    """Matrix: at/past the final attempt (including redriven counts well
    above maxReceiveCount 3), the FINAL notice always names the real
    budget — 'after 3 attempts' — never the raw receive count."""
    github = ScriptedGitHub(
        [
            (200, _list_body()),  # review-stage prior read (no marker yet)
            (200, _list_body(_comment(NOTICE_ID, "n " + MARKER))),  # notice list
            (200, json.dumps({"id": NOTICE_ID}).encode()),  # adopt-PATCH
        ]
    )
    h = Harness(
        meta=[(200, SHA_B), (200, SHA_B)],
        llm_script=[("response", 500, b"{}")],
        github=github,
    )
    with pytest.raises(Exception, match="llm request failed: http_500"):
        h.run(receive_count=receive_count, env_queue_url=QUEUE_URL)
    assert github.methods() == ["GET", "GET", "PATCH"]
    patch_body = json.loads(github.calls[-1]["body"].decode())["body"]
    assert "could not be completed" in patch_body
    assert "after 3 attempts" in patch_body
    # The clamp collapses every count to the budget — never the raw count.
    assert f"after {receive_count} attempts" not in patch_body or receive_count == "3"
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["failure_notice_published"] == "true"


# --- D2: permanent assemble failure → notice immediately ---


def test_assemble_failure_publishes_notice_immediately():
    """AssembleError at the FIRST attempt (`ApproximateReceiveCount` 1):
    permanent → the notice publishes immediately, the record completes as
    `discarded_error` with `failure_notice_published: true`."""
    github = ScriptedGitHub(
        [
            (200, _list_body()),  # review-stage prior read (no marker yet)
            (200, _list_body()),
            (201, json.dumps({"id": NOTICE_ID}).encode()),
            (200, _list_body(_comment(NOTICE_ID, "n " + MARKER))),
        ]
    )
    h = Harness(
        meta=[(200, SHA_B), (200, SHA_B)],
        llm_script=[("response", 200, _completion("plain text without sections"))],
        github=github,
    )
    assert h.run(receive_count="1", env_queue_url=QUEUE_URL) == {
        "ok": True,
        "results": ["discarded_error"],
    }
    assert github.methods() == ["GET", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["status"] == "discarded_error"
    assert line["failure_notice_published"] == "true"


# --- D2: GitHub-401 → no notice (`false`) ---


def test_github_401_skips_notice():
    """Diff 401 twice (GitHub unreachable after the single re-fetch):
    comment writes would 401 too → NO notice attempted, no GitHub write
    of any kind, `discarded_error` with `failure_notice_published: false`."""
    github = ScriptedGitHub([])
    h = Harness(meta=[(401, None)], github=github)
    assert h.run(env_queue_url=QUEUE_URL) == {"ok": True, "results": ["discarded_error"]}
    assert h.github.calls == []
    (line,) = h.log_lines()
    assert line["status"] == "discarded_error"
    assert line["error_class"] == "diff_http_error_401"
    assert line["failure_notice_published"] == "false"


# --- SPR-58: invalid_key trigger row + final-attempt 429 combination ---


def test_notice_trigger_invalid_key_maps_to_immediate_row():
    """`invalid_key` (LLM request-construction fault) maps to the new D2
    immediate row — not transient, not skipped."""
    assert _notice_trigger(LlmError("invalid_key")) is NoticeTrigger.INVALID_KEY
    assert _notice_trigger(LlmError("http_400")) is None


def test_final_attempt_429_publishes_notice_then_raises_with_visibility():
    """429-combination (final-attempt semantics, pinned — not changed):
    GitHub 429 with `Retry-After: 120` at `ApproximateReceiveCount` 5
    (past max 3) → visibility extended per the hint, the FINAL notice
    publishes (transient-at-final), the `retry_queued` line carries
    `failure_notice_published: true`, and the ORIGINAL error still raises
    so DLQ/alert/redrive proceed unchanged."""
    github = ScriptedGitHub(
        [
            (200, _list_body()),  # review-stage prior read (no marker yet)
            (200, _list_body()),
            (429, b'{"message":"throttled"}', {"Retry-After": "120"}),
            (200, _list_body()),
            (201, json.dumps({"id": NOTICE_ID}).encode()),
            (200, _list_body(_comment(NOTICE_ID, "n " + MARKER))),
        ]
    )
    h = Harness(meta=[(200, SHA_B), (200, SHA_B)], github=github)
    with pytest.raises(GitHubError) as exc_info:
        h.run(receive_count="5", env_queue_url=QUEUE_URL)
    assert exc_info.value.status == 429
    assert h.sqs.visibility_calls == [
        {"QueueUrl": QUEUE_URL, "ReceiptHandle": "rh-1", "VisibilityTimeout": 120}
    ]
    assert github.methods() == ["GET", "GET", "POST", "GET", "POST", "GET"]
    (line,) = h.log_lines()
    assert line["status"] == "retry_queued"
    assert line["error_class"] == "http_429"
    assert line["failure_notice_published"] == "true"


# --- first-failure → intermediate → final → success lifecycle (post-once) ---


def test_notice_lifecycle_retrying_once_then_final_then_success_clears():
    """The full retry UX on one PR, four deliveries against the same
    state (counts 1 → 2 → 3 → success), on a STATEFUL GitHub double so
    every list read observes what earlier deliveries actually published:

    * count 1: instant RETRYING notice (lease-POST) — failures are never
      silent; the notice's id is persisted.
    * count 2 (intermediate): NO new comment — post-once semantics; the
      first notice stays untouched.
    * count 3 (max): the FINAL notice REPLACES the first in place
      (adopt-PATCH of the same comment id — never a second POST).
    * success: the next review's prior-comment read FINDS the notice
      (marker-bearing, stateful list), the notice text flows into the
      LLM prompt as inert context, and the review still publishes —
      PATCHing that SAME comment id. The stale "retrying" warning cannot
      survive a successful publish (Gate #89 Coverage gap 1).

    Exactly ONE canonical comment id exists across the whole lifecycle.
    """
    github = StatefulGitHub()
    h = Harness(
        meta=[(200, SHA_B)],
        llm_script=[
            ("raise", TimeoutError("read timed out")),
            ("raise", TimeoutError("read timed out")),
            ("raise", TimeoutError("read timed out")),
            ("response", 200, _completion()),
        ],
        github=github,
    )

    with pytest.raises(Exception, match="llm request failed: timeout"):
        h.run(receive_count="1", env_queue_url=QUEUE_URL)
    assert github.comments == {NOTICE_ID: github.bodies("POST")[0]["body"]}
    assert "retried automatically" in github.comments[NOTICE_ID]

    with pytest.raises(Exception, match="llm request failed: timeout"):
        h.run(receive_count="2", env_queue_url=QUEUE_URL)
    assert github.comments[NOTICE_ID] == github.bodies("POST")[0]["body"]  # untouched

    with pytest.raises(Exception, match="llm request failed: timeout"):
        h.run(receive_count="3", env_queue_url=QUEUE_URL)
    assert set(github.comments) == {NOTICE_ID}  # replaced in place, not duplicated
    final_notice = github.comments[NOTICE_ID]
    assert "could not be completed" in final_notice
    assert "retried automatically" not in final_notice

    # Delivery 4: the prior read must see the FINAL notice (marker text),
    # feed it to the LLM as context, and still publish over the same id.
    assert h.run(receive_count="3", env_queue_url=QUEUE_URL) == {
        "ok": True,
        "results": ["published"],
    }
    prompt_raw = h._llm_conns[3].request_bodies[0]
    prompt_body = prompt_raw.decode() if isinstance(prompt_raw, bytes) else prompt_raw
    assert MARKER in prompt_body  # the notice traveled as prior context…
    assert "Push a new commit" in prompt_body  # …inert: review proceeded anyway
    assert set(github.comments) == {NOTICE_ID}
    final_body = github.comments[NOTICE_ID]
    assert REVIEW_BODY.strip() in final_body  # clean review content…
    assert "could not be completed" not in final_body  # …notice fully replaced
    assert h.table.items[PK]["comment_id"] == NOTICE_ID

    # Exactly one canonical comment ever created; no credential leakage.
    assert len(github.bodies("POST")) == 1
    for call in github.calls:
        wire = call["body"] if isinstance(call["body"], str) else call["body"].decode()
        assert "glm-key-value" not in wire

    lines = h.log_lines()
    assert [line["status"] for line in lines] == [
        "retry_queued",
        "retry_queued",
        "retry_queued",
        "published",
    ]
    assert [line["failure_notice_published"] for line in lines] == [
        "true",  # count 1: retrying notice posted
        "false",  # count 2: intermediate — silent
        "true",  # count 3: final notice replaced in place
        "false",  # success: no notice machinery on the publish path
    ]
    # Delivery 3 replaced the notice via PATCH of the SAME id, no POST.
    assert github.methods() == [
        "GET",
        "GET",
        "POST",
        "GET",  # delivery 1 (prior read, notice list, POST, re-check)
        "GET",  # delivery 2 (prior read sees the notice; silent)
        "GET",
        "GET",
        "PATCH",  # delivery 3 (prior read, notice list, adopt-PATCH)
        "GET",
        "PATCH",  # delivery 4 (prior read sees the notice; success PATCH)
    ]

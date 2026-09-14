"""Moto-backed fake-AWS integration tier: real handlers end-to-end (HLD §4.4).

Drives the REAL lambda entrypoints (`ingress_handler.handler` for webhook
accept, `worker_handler.handler` for the SQS review pipeline) against moto's
fake AWS (DynamoDB table `pr-reviewer-state`, queues `pr-reviewer-work` /
`pr-reviewer-dlq`, SSM parameters) with REAL boto3 clients — no stub table,
no stub queue. Only the non-AWS network boundaries are faked:

* LLM: scripted HTTPS connections behind the `review_diff`
  `_connection_factory` seam (the production seam in `common.llm`).
* GitHub: the diff `_transport` seam (`common.diff`) and the
  Issues-Comments `_github_transport` seam (`worker_handler`).

This tier exists to catch the silent bug classes the hand-written
`tests/state_machine/dynamodb_stub.py` normalizes away: real DynamoDB
returns numbers as `decimal.Decimal` (ride e), and real DynamoDB rejects
empty/unused `ExpressionAttributeNames` (ride f).

Grammar-validation honesty note (verified empirically against moto 5.2.3):
moto REJECTS declared-but-unused `ExpressionAttributeNames` exactly like
real DynamoDB (pinned in ride f), but moto ACCEPTS an empty
`ExpressionAttributeNames={}` map where real DynamoDB rejects it with
"ExpressionAttributeNames must not be empty". The `_BotoTable` empty-map
omission therefore CANNOT be pinned by this tier — it is covered by
`tests/unit/test_worker_table.py` instead. No test here pretends moto
validates more than it does.

No live network anywhere: every external call is either moto (in-process)
or an injected fake transport. Requires only the `moto` dev dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import ingress_handler
import worker_handler
from common.marker import build_marker
from common.state import (
    build_claim_expressions,
    build_finalize_expressions,
    expression_names,
    review_pk,
)
from worker_handler import _BotoTable

REGION = "us-west-2"
TABLE_NAME = "pr-reviewer-state"
WORK_QUEUE_NAME = "pr-reviewer-work"
DLQ_NAME = "pr-reviewer-dlq"

WEBHOOK_SECRET = "test-webhook-secret-value"  # noqa: S105 (moto-seeded dummy)
GITHUB_TOKEN = "test-github-token-value"  # noqa: S105 (moto-seeded dummy)
GLM_API_KEY = "test-glm-key-value"  # noqa: S105 (moto-seeded dummy)
GLM_MODEL = "glm-5.3-flash"
LLM_HOST = "llm.example.test"
LLM_ENDPOINT = f"https://{LLM_HOST}/v1/chat/completions"

REPO = "octo-org/hello-world"
BASE_SHA = "00" * 20
SHA_A = "aa" * 20
SHA_B = "bb" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
GUID_2 = "22222222-2222-4222-8222-222222222222"
GUID_OTHER = "33333333-3333-4333-8333-333333333333"
NOW = 1_750_000_000
POST_ID = 987654
UPDATED_AT = "2026-09-12T10:00:00Z"

REVIEW_BODY = (
    "## Summary\nAdds input validation.\n\n"
    "## Findings\n- [HIGH] `src/main.py:12` — Missing bound. Fix: add a check.\n\n"
    "## Risk Notes\nNone.\n"
)
# Sections intact, but an approval verdict phrase trips the validate gate
# (`approval_verdict` → AssembleError → discarded_error, never published).
APPROVAL_BODY = REVIEW_BODY + "\nSafe to merge when ready.\n"


# --- fake-AWS stack ---------------------------------------------------------


@pytest.fixture(autouse=True)
def _moto(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Activate moto's fake AWS for every test in this module."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-test-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-test-secret")  # noqa: S106
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    moto = mock_aws()
    moto.start()
    yield
    moto.stop()


@pytest.fixture()
def stack(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Seed moto with the real resource shape and return live boto3 handles.

    Table `pr-reviewer-state` (partition key `pk`, exactly as in
    `terraform/state.tf` — no GSIs exist there), queues
    `pr-reviewer-work` (redrive to DLQ, maxReceiveCount 5, as in
    `terraform/messaging.tf`) + `pr-reviewer-dlq`, and the five SSM
    parameters under the real names from `lambda/common/config.py`.
    """
    dynamodb = boto3.resource("dynamodb", region_name=REGION)
    table = dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    sqs = boto3.client("sqs", region_name=REGION)
    dlq_url = sqs.create_queue(QueueName=DLQ_NAME)["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"][
        "QueueArn"
    ]
    work_url = sqs.create_queue(
        QueueName=WORK_QUEUE_NAME,
        Attributes={
            "VisibilityTimeout": "720",
            "MessageRetentionPeriod": "345600",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": "5"}),
        },
    )["QueueUrl"]
    ssm = boto3.client("ssm", region_name=REGION)
    for name, value in {
        "/pr-reviewer/webhook-secret": WEBHOOK_SECRET,
        "/pr-reviewer/github-token": GITHUB_TOKEN,
        "/pr-reviewer/glm-api-key": GLM_API_KEY,
        "/pr-reviewer/glm-model": GLM_MODEL,
        "/pr-reviewer/glm-endpoint": LLM_ENDPOINT,
    }.items():
        ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
    monkeypatch.setenv("WORK_QUEUE_URL", work_url)
    return SimpleNamespace(
        table=table, sqs=sqs, ssm=ssm, work_url=work_url, dlq_url=dlq_url, dlq_arn=dlq_arn
    )


# --- non-AWS network doubles (transport seams only) -------------------------


def _file_entry(name: str = "src/main.py") -> dict[str, Any]:
    return {
        "filename": name,
        "additions": 5,
        "deletions": 2,
        "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n",
    }


class FakeDiffTransport:
    """URL-routed diff double: scripted PR-meta replies, canned files page."""

    def __init__(self, *, meta: list[tuple[int, str | None]]) -> None:
        from common.diff import HttpResponse

        self._http_response = HttpResponse
        self._meta = list(meta)
        self.calls: list[str] = []

    def __call__(self, url: str, headers: Any) -> Any:
        self.calls.append(url)
        if "/files" in url:
            return self._http_response(
                status=200, body=json.dumps([_file_entry()]).encode(), headers={}
            )
        entry = self._meta.pop(0) if len(self._meta) > 1 else self._meta[0]
        status, sha = entry
        if status != 200 or sha is None:
            return self._http_response(status=status, body=b"{}", headers={})
        return self._http_response(
            status=200, body=json.dumps({"head": {"sha": sha}}).encode(), headers={}
        )


def _completion_body(content: str = REVIEW_BODY) -> bytes:
    return json.dumps(
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode()


class _FakeLLMSocket:
    def settimeout(self, seconds: float) -> None:
        pass


class _FakeLLMResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeLLMConnection:
    """Answers behind the `review_diff` `_connection_factory` seam."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.sock = _FakeLLMSocket()

    def connect(self) -> None:
        pass

    def request(self, method: str, path: str, body: Any = None, headers: Any = None) -> None:
        pass

    def getresponse(self) -> _FakeLLMResponse:
        return _FakeLLMResponse(200, self._body)

    def close(self) -> None:
        pass


def _llm_factory(body: bytes = _completion_body()) -> Any:
    def factory(host: str, port: int, *, timeout: int) -> _FakeLLMConnection:
        return _FakeLLMConnection(body)

    return factory


class FakeGitHub:
    """Issues-Comments double: records (method, url, parsed body).

    T040: stateful comment list so the reconcile path runs faithfully — GET
    returns the current list, POST appends, PATCH refreshes the stored body,
    DELETE removes. `script` ordered overrides fire first (e.g. a one-shot
    PATCH 404 modeling a comment deleted on GitHub)."""

    def __init__(self, *, post_id: int = POST_ID, script: list | None = None) -> None:
        self._post_id = post_id
        self._script = list(script) if script else []
        self.comments: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, method: str, url: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, bytes]:
        parsed = json.loads(body) if body else None
        self.calls.append({"method": method, "url": url, "body": parsed})
        if self._script:
            return self._script.pop(0)
        if method == "GET":
            return 200, json.dumps(self.comments).encode()
        if method == "POST":
            self.comments.append({"id": self._post_id, "body": parsed["body"]})
            return 201, json.dumps({"id": self._post_id}).encode()
        comment_id = int(url.rsplit("/", 1)[-1])
        if method == "PATCH":
            for comment in self.comments:
                if comment["id"] == comment_id:
                    comment["body"] = parsed["body"]
            return 200, json.dumps({"id": comment_id}).encode()
        if method == "DELETE":
            self.comments = [c for c in self.comments if c["id"] != comment_id]
            return 204, b""
        raise AssertionError(f"unexpected GitHub method: {method}")

    def methods(self) -> list[str]:
        return [call["method"] for call in self.calls]


# --- driver helpers (real entrypoints, real boto3) --------------------------


def _pr_payload(action: str, head_sha: str, pr_number: int) -> dict[str, Any]:
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "base": {"sha": BASE_SHA},
            "draft": False,
            "head": {"sha": head_sha},
            "number": pr_number,
        },
        "repository": {"full_name": REPO},
        "sender": {"login": "octo-user"},
    }


def _signed_event(payload: dict[str, Any], guid: str) -> dict[str, Any]:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return {
        "headers": {
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": guid,
        },
        "body": raw.decode("utf-8"),
        "isBase64Encoded": False,
    }


def _ingress(stack: SimpleNamespace, payload: dict[str, Any], guid: str) -> dict[str, Any]:
    """Real ingress handler against moto (singular SSM secret, real table/SQS)."""
    return ingress_handler.handler(
        _signed_event(payload, guid),
        None,
        _ssm=stack.ssm,
        _table=stack.table,
        _sqs=stack.sqs,
        _now=lambda: NOW,
    )


def _envelope(pr_number: int, head_sha: str, guid: str) -> dict[str, Any]:
    return {
        "envelope_version": "v1",
        "event_type": "pull_request",
        "action": "opened",
        "repo_full_name": REPO,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_sha": BASE_SHA,
        "sender": "octo-user",
        "delivery_guid": guid,
    }


def _worker(
    stack: SimpleNamespace,
    payload: dict[str, Any],
    *,
    diff: FakeDiffTransport,
    llm_body: bytes = _completion_body(),
    github: FakeGitHub | None = None,
) -> tuple[dict[str, Any], FakeGitHub, list[str]]:
    """Real worker handler against moto via the production `_BotoTable` port."""
    github = github if github is not None else FakeGitHub()
    sink: list[str] = []
    result = worker_handler.handler(
        {"Records": [{"body": json.dumps(payload), "messageId": "m1"}]},
        None,
        _table=_BotoTable(stack.table),
        _ssm=stack.ssm,
        _now=lambda: NOW,
        _diff_transport=diff,
        _llm_factory=_llm_factory(llm_body),
        _github_transport=github,
        _sink=sink.append,
        _system_prompt="SYSTEM-PROMPT",
        _allowed_hosts=(LLM_HOST,),
    )
    return result, github, sink


def _queue_depth(sqs: Any, url: str) -> int:
    attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])[
        "Attributes"
    ]
    return int(attrs.get("ApproximateNumberOfMessages", "0"))


# --- rides ------------------------------------------------------------------


def test_a_happy_ride_ingress_to_finalize(stack: SimpleNamespace) -> None:
    """Signed webhook → 202 → one work message → published → finalized.

    Asserts concrete moto state: delivery row logged, exactly one work-queue
    message, one POST carrying exactly one canonical marker, lease fields
    REMOVEd at finalize, reviewed head recorded with the comment id.
    """
    pr_number = 101
    payload = _pr_payload("opened", SHA_B, pr_number)
    response = _ingress(stack, payload, GUID_1)
    assert response == {"statusCode": 202, "body": ""}
    assert stack.table.get_item(Key={"pk": f"delivery:{GUID_1}"}).get("Item") is not None
    assert _queue_depth(stack.sqs, stack.work_url) == 1

    received = stack.sqs.receive_message(QueueUrl=stack.work_url, MaxNumberOfMessages=10)
    (message,) = received["Messages"]
    envelope = json.loads(message["Body"])
    assert envelope["head_sha"] == SHA_B
    assert envelope["delivery_guid"] == GUID_1

    result, github, _sink = _worker(stack, envelope, diff=FakeDiffTransport(meta=[(200, SHA_B)]))
    assert result == {"ok": True, "results": ["published"]}
    assert github.methods() == ["GET", "POST", "GET"]  # T040: list → lease-POST → re-check
    post = next(call for call in github.calls if call["method"] == "POST")
    assert post["url"].endswith(f"/repos/{REPO}/issues/{pr_number}/comments")
    marker = build_marker(REPO, pr_number)
    assert post["body"]["body"].count(marker) == 1
    assert "## Summary" in post["body"]["body"]
    stack.sqs.delete_message(QueueUrl=stack.work_url, ReceiptHandle=message["ReceiptHandle"])

    item = stack.table.get_item(Key={"pk": review_pk(REPO, pr_number)})["Item"]
    assert item["status"] == "ACTIVE"
    assert item["head_sha"] == SHA_B
    assert int(item["comment_id"]) == POST_ID
    assert "claim_owner" not in item
    assert "claim_until" not in item


def test_b_redelivery_while_lease_held_discards(stack: SimpleNamespace) -> None:
    """Live lease held by another owner → DISCARDED_CLAIM_HELD, no POST."""
    pr_number = 102
    pk = review_pk(REPO, pr_number)
    stack.table.put_item(
        Item={
            "pk": pk,
            "status": "CLAIMED",
            "generation": 2,
            "head_sha": SHA_B,
            "last_seen_sha": SHA_B,
            "claim_owner": GUID_OTHER,
            "claim_until": NOW + 100,
            "updated_at": UPDATED_AT,
        }
    )
    before = stack.table.get_item(Key={"pk": pk})["Item"]

    result, github, _sink = _worker(
        stack,
        _envelope(pr_number, SHA_B, GUID_2),
        diff=FakeDiffTransport(meta=[(200, SHA_B)]),
    )
    assert result == {"ok": True, "results": ["discarded_claim_held"]}
    assert github.calls == []
    assert stack.table.get_item(Key={"pk": pk})["Item"] == before


def test_c_same_sha_rereview_after_finalize_patches(stack: SimpleNamespace) -> None:
    """Finalize drops the lease (HLD §3.2), so a same-SHA re-review re-claims
    and refreshes the SAME comment via PATCH — never a second POST, never
    DISCARDED_CLAIM_HELD."""
    pr_number = 103
    response = _ingress(stack, _pr_payload("opened", SHA_B, pr_number), GUID_1)
    assert response == {"statusCode": 202, "body": ""}
    received = stack.sqs.receive_message(QueueUrl=stack.work_url, MaxNumberOfMessages=10)
    envelope = json.loads(received["Messages"][0]["Body"])
    stack.sqs.delete_message(
        QueueUrl=stack.work_url, ReceiptHandle=received["Messages"][0]["ReceiptHandle"]
    )
    result, github, _sink = _worker(stack, envelope, diff=FakeDiffTransport(meta=[(200, SHA_B)]))
    assert result == {"ok": True, "results": ["published"]}

    replay, github2, _sink2 = _worker(
        stack,
        _envelope(pr_number, SHA_B, GUID_2),
        diff=FakeDiffTransport(meta=[(200, SHA_B)]),
        github=github,
    )
    assert replay == {"ok": True, "results": ["published"]}
    assert github.methods() == ["GET", "POST", "GET", "PATCH"]
    assert github.calls[3]["url"].endswith(f"/issues/comments/{POST_ID}")
    item = stack.table.get_item(Key={"pk": review_pk(REPO, pr_number)})["Item"]
    assert item["status"] == "ACTIVE"
    assert int(item["comment_id"]) == POST_ID
    assert github2 is github


def test_d_approval_like_verdict_discards_without_post(stack: SimpleNamespace) -> None:
    """Approval-like LLM output fails the validate gate → discarded_error:
    no POST, and the handler never sends to the DLQ itself."""
    pr_number = 104
    result, github, sink = _worker(
        stack,
        _envelope(pr_number, SHA_B, GUID_1),
        diff=FakeDiffTransport(meta=[(200, SHA_B)]),
        llm_body=_completion_body(APPROVAL_BODY),
    )
    assert result == {"ok": True, "results": ["discarded_error"]}
    assert github.calls == []
    assert _queue_depth(stack.sqs, stack.dlq_url) == 0
    statuses = [json.loads(line)["status"] for line in sink]
    assert statuses == ["discarded_error"]


def test_e_decimal_round_trip_publish_uses_patch(stack: SimpleNamespace) -> None:
    """Moto returns DynamoDB numbers as Decimal exactly like real DynamoDB:
    a finalized row seeded with Decimals must still PATCH (the
    `_normalize_number` path), never POST a second comment."""
    pr_number = 105
    pk = review_pk(REPO, pr_number)
    stack.table.put_item(
        Item={
            "pk": pk,
            "status": "ACTIVE",
            "generation": Decimal(0),
            "head_sha": SHA_B,
            "last_seen_sha": SHA_B,
            "comment_id": Decimal(POST_ID),
            "updated_at": UPDATED_AT,
        }
    )
    raw = stack.table.get_item(Key={"pk": pk})["Item"]
    assert isinstance(raw["generation"], Decimal)
    assert isinstance(raw["comment_id"], Decimal)

    result, github, _sink = _worker(
        stack,
        _envelope(pr_number, SHA_B, GUID_1),
        diff=FakeDiffTransport(meta=[(200, SHA_B)]),
    )
    assert result == {"ok": True, "results": ["published"]}
    assert github.methods() == ["PATCH"]
    assert github.calls[0]["url"].endswith(f"/issues/comments/{POST_ID}")


def test_f_update_expression_grammar_against_moto(stack: SimpleNamespace) -> None:
    """Claim/finalize expressions built by `common.state` execute against
    moto; unused names are rejected like real DynamoDB.

    Moto gap (documented, not pinned as enforcement): moto ACCEPTS an empty
    `ExpressionAttributeNames={}` map, which real DynamoDB rejects with
    "ExpressionAttributeNames must not be empty" — the `_BotoTable`
    empty-map omission is covered by `tests/unit/test_worker_table.py`.
    """
    pk = review_pk(REPO, 106)
    stack.table.put_item(
        Item={
            "pk": pk,
            "status": "CLAIMED",
            "generation": 0,
            "head_sha": SHA_B,
            "last_seen_sha": SHA_B,
            "updated_at": UPDATED_AT,
        }
    )

    update, condition, values = build_claim_expressions(
        head_sha=SHA_B, generation=0, claim_owner=GUID_1, claim_until=NOW + 180, now=NOW
    )
    # The claim spells no `#st`: the helper emits None and the caller omits
    # the kwarg entirely (mirrors `_BotoTable.update_item`).
    assert expression_names(update, condition) is None
    stack.table.update_item(
        Key={"pk": pk},
        UpdateExpression=update,
        ConditionExpression=condition,
        ExpressionAttributeValues=values,
    )
    assert stack.table.get_item(Key={"pk": pk})["Item"]["claim_owner"] == GUID_1

    update, condition, values = build_finalize_expressions(
        head_sha=SHA_B, generation=0, comment_id=POST_ID, updated_at=UPDATED_AT
    )
    names = expression_names(update, condition)
    assert names == {"#st": "status"}
    stack.table.update_item(
        Key={"pk": pk},
        UpdateExpression=update,
        ConditionExpression=condition,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    item = stack.table.get_item(Key={"pk": pk})["Item"]
    assert item["status"] == "ACTIVE"
    assert int(item["comment_id"]) == POST_ID
    assert "claim_owner" not in item
    assert "claim_until" not in item

    # Declared-but-unused names ARE rejected by moto, like real DynamoDB.
    update, condition, values = build_claim_expressions(
        head_sha=SHA_B, generation=0, claim_owner=GUID_2, claim_until=NOW + 180, now=NOW
    )
    with pytest.raises(ClientError, match="unused"):
        stack.table.update_item(
            Key={"pk": pk},
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues=values,
        )


def test_g_work_queue_redrive_policy(stack: SimpleNamespace) -> None:
    """Work queue carries the terraform redrive policy: DLQ target + count 5."""
    attrs = stack.sqs.get_queue_attributes(
        QueueUrl=stack.work_url, AttributeNames=["RedrivePolicy"]
    )["Attributes"]
    policy = json.loads(attrs["RedrivePolicy"])
    assert policy["deadLetterTargetArn"] == stack.dlq_arn
    assert policy["maxReceiveCount"] in ("5", 5)


def test_h_patch_404_recovers_via_creation_lease(stack: SimpleNamespace) -> None:
    """The stored comment was deleted on GitHub: PATCH → 404, list shows no
    marker comment, creation lease → POST → persist → re-check. Exactly one
    canonical comment exists afterwards, and the record points at the new
    id. Moto evaluates the creation-lease condition with its real DynamoDB
    expression parser — this ride pins the T039 condition string beyond
    what the hand-written state-machine stub can."""
    pr_number = 108
    pk = review_pk(REPO, pr_number)
    stack.table.put_item(
        Item={
            "pk": pk,
            "status": "ACTIVE",
            "generation": 2,
            "head_sha": SHA_B,
            "last_seen_sha": SHA_B,
            "comment_id": 555,
            "updated_at": UPDATED_AT,
        }
    )
    github = FakeGitHub(script=[(404, b"{}")])  # the stored comment is gone
    result, github, _sink = _worker(
        stack,
        _envelope(pr_number, SHA_B, GUID_1),
        diff=FakeDiffTransport(meta=[(200, SHA_B)]),
        github=github,
    )
    assert result == {"ok": True, "results": ["published"]}
    assert github.methods() == ["PATCH", "GET", "POST", "GET"]
    assert len(github.comments) == 1
    assert github.comments[0]["body"].count(build_marker(REPO, pr_number)) == 1
    item = stack.table.get_item(Key={"pk": pk})["Item"]
    assert int(item["comment_id"]) == POST_ID
    assert item["status"] == "ACTIVE"

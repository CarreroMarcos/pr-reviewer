"""T046: ingress delivery-dedup probes (US4.AC1; FR-012; HLD §5.2).

Drives the REAL `ingress_handler.handler` with injected table/SQS doubles:

* processed GUID → 200 no-op (no enqueue, no re-mark) →
  `test_processed_guid_returns_200_noop`
* first delivery → 202 with a 7-day-TTL delivery row; byte-identical replay
  within the window → 200 no-op with no second enqueue →
  `test_replay_within_window_recognized`
* SQS failure → 500 WITHOUT marking (recoverable via redelivery); the
  retry then succeeds →
  `test_sqs_failure_does_not_mark_processed`

All deterministic: fixed clock (`NOW`), fixed HMAC secret, no network.
These probe T048's rows (HLD §2.1 steps 5–7; failure mode 4) at the
ingress boundary; the worker-side convergence half is T047 scope.
"""

import hashlib
import hmac
import json

from common.state import DELIVERY_TTL_SECONDS, delivery_pk
from ingress_handler import handler

FIXED_SECRET = "unit-test-secret-not-a-credential"  # noqa: S105 (dummy fixture)

GUID_1 = "11111111-1111-4111-8111-111111111111"
GUID_SEEN = "22222222-2222-4222-8222-222222222222"
HEAD_SHA = "aa" * 20
BASE_SHA = "bb" * 20
NOW = 1_750_000_000


def sign(raw):
    return "sha256=" + hmac.new(FIXED_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def make_payload():
    return {
        "action": "opened",
        "number": 7,
        "pull_request": {
            "base": {"sha": BASE_SHA},
            "draft": False,
            "head": {"sha": HEAD_SHA},
            "number": 7,
        },
        "repository": {"full_name": "octo-org/hello-world"},
        "sender": {"login": "octocat"},
    }


def make_event(payload, guid):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return {
        "headers": {
            "X-Hub-Signature-256": sign(raw),
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": guid,
        },
        "body": raw.decode("utf-8"),
        "isBase64Encoded": False,
    }


class FakeTable:
    def __init__(self, seed=None):
        self.items = dict(seed or {})
        self.get_calls = []
        self.put_calls = []

    def get_item(self, Key):  # noqa: N803 (boto3 shape)
        self.get_calls.append(Key)
        item = self.items.get(Key["pk"])
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, Item):  # noqa: N803 (boto3 shape)
        self.put_calls.append(dict(Item))
        self.items[Item["pk"]] = dict(Item)
        return {}


class FakeSQS:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def send_message(self, QueueUrl, MessageBody):  # noqa: N803 (boto3 shape)
        self.calls.append({"QueueUrl": QueueUrl, "MessageBody": MessageBody})
        if self.fail:
            raise RuntimeError("sqs unavailable")
        return {"MessageId": "fake-id"}


def invoke(event, *, table=None, sqs=None, now=NOW):
    table = FakeTable() if table is None else table
    sqs = FakeSQS() if sqs is None else sqs
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET, _now=lambda: now)
    return response, table, sqs


def test_processed_guid_returns_200_noop():
    """A GUID already in the delivery table short-circuits: 200, nothing
    enqueued, nothing re-marked (the stored row is byte-identical)."""
    seed = {delivery_pk(GUID_SEEN): {"pk": delivery_pk(GUID_SEEN), "ttl": NOW + 100}}
    table = FakeTable(seed={k: dict(v) for k, v in seed.items()})
    response, table, sqs = invoke(make_event(make_payload(), GUID_SEEN), table=table)
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []
    assert table.items == seed


def test_replay_within_window_recognized():
    """First delivery → 202 with a 7-day-TTL delivery row; the byte-identical
    replay (captured event, same GUID) → 200 no-op with no second enqueue —
    exactly one queued message for the pair."""
    table, sqs = FakeTable(), FakeSQS()
    event = make_event(make_payload(), GUID_1)
    first, table, sqs = invoke(event, table=table, sqs=sqs)
    assert first == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1
    (mark,) = table.put_calls
    assert mark["pk"] == delivery_pk(GUID_1)
    assert mark["ttl"] == NOW + DELIVERY_TTL_SECONDS  # 7-day window (HLD §2.4)

    replay, table, sqs = invoke(event, table=table, sqs=sqs)
    assert replay == {"statusCode": 200, "body": ""}
    assert len(sqs.calls) == 1  # no second enqueue
    assert len(table.put_calls) == 1  # no re-mark


def test_sqs_failure_does_not_mark_processed():
    """SendMessage failure → 500 WITHOUT marking (failure mode 4): the
    delivery stays recoverable, and the retry of the identical event then
    succeeds with exactly one enqueue and one mark."""
    table = FakeTable()
    event = make_event(make_payload(), GUID_1)
    failed, table, failing_sqs = invoke(event, table=table, sqs=FakeSQS(fail=True))
    assert failed == {"statusCode": 500, "body": ""}
    assert len(failing_sqs.calls) == 1  # dispatch attempted…
    assert table.put_calls == []  # …but never marked — redelivery stays possible

    retry, table, sqs = invoke(event, table=table, sqs=FakeSQS())
    assert retry == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1
    assert len(table.put_calls) == 1

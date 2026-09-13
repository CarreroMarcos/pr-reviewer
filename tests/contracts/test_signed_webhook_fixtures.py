"""T025: committed signed-webhook fixtures (HLD §4.4 item 3; US1.AC1–AC4).

Committed fixtures: FIXED secret + canonical payload bytes → FIXED expected
`X-Hub-Signature-256` values for `opened` / `synchronize` / `ready_for_review` /
`reopened` **[D1]**, plus a signed non-`pull_request` event, a tampered
signature, and missing-header cases.

FIXED_SECRET is a DUMMY value committed on purpose — its entire point is that
every checkout derives the identical signature. It is not a credential and
MUST never be used outside these fixtures (Constitution III).

Serialization authority: canonical bytes are
`json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")`;
the expected signatures below are HMAC-SHA256 over exactly those bytes.
"""

import hashlib
import hmac
import json

from ingress_handler import ALLOWED_ACTIONS, MAX_BODY_BYTES, handler, verify_signature

FIXED_SECRET = "fixture-webhook-secret-not-a-credential"  # noqa: S105 (dummy fixture)
FIXED_SECRET_NAME = "/pr-reviewer/webhook-secret"  # noqa: S105 (SSM path)

GUID_OPENED = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GUID_SYNCHRONIZE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
GUID_READY = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
GUID_REOPENED = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
GUID_OTHER = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"

HEAD_SHA = "aa" * 20
BASE_SHA = "bb" * 20

# Committed vectors: fixed secret + canonical payload → fixed signature.
EXPECTED_SIGNATURES = {
    "opened": "sha256=885cdab856d34803ce465ed88753db0aff63366c2df41ef86c518a3f0939777c",  # noqa: E501
    "synchronize": "sha256=093763eb2003b4ae06b722749c6ae7c548478a3df8d7c869e44e56ae36dbfea6",  # noqa: E501
    "ready_for_review": "sha256=bfc8029fdad4f8c9ce47a31094601fb6707a31ee763d752f53b5c6bf0694e0e8",  # noqa: E501
    "reopened": "sha256=7ce8527095e04e41d7c90a53b2b5045216d4c6e430598d24f637281392d00c8d",  # noqa: E501
}

GUID_BY_ACTION = {
    "opened": GUID_OPENED,
    "synchronize": GUID_SYNCHRONIZE,
    "ready_for_review": GUID_READY,
    "reopened": GUID_REOPENED,
}


def make_payload(action, draft=False):
    """Minimal realistic pull_request webhook payload for `action`."""
    return {
        "action": action,
        "number": 42,
        "pull_request": {
            "base": {"sha": BASE_SHA},
            "draft": draft,
            "head": {"sha": HEAD_SHA},
            "number": 42,
        },
        "repository": {"full_name": "octo-org/hello-world"},
        "sender": {"login": "octocat"},
    }


def canonical_bytes(payload):
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign(secret, raw):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


class FakeSSM:
    """Singular `get_parameter` double — has NO `get_parameters` on purpose."""

    def __init__(self, secret):
        self.secret = secret
        self.calls = []

    def get_parameter(self, Name, WithDecryption=False):  # noqa: N803 (boto3 shape)
        self.calls.append({"Name": Name, "WithDecryption": WithDecryption})
        return {"Parameter": {"Value": self.secret}}


class FakeTable:
    """DynamoDB-table double in the boto3-resource response shape."""

    def __init__(self):
        self.items = {}
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
    def __init__(self):
        self.calls = []

    def send_message(self, QueueUrl, MessageBody):  # noqa: N803 (boto3 shape)
        self.calls.append({"QueueUrl": QueueUrl, "MessageBody": MessageBody})
        return {"MessageId": "fake-message-id"}


def make_event(raw, signature, event_type="pull_request", delivery=GUID_OTHER):
    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": delivery,
    }
    return {"headers": headers, "body": raw.decode("utf-8"), "isBase64Encoded": False}


def fresh_doubles():
    return FakeTable(), FakeSQS()


# --- committed-vector integrity -------------------------------------------


def test_body_cap_is_one_mib():
    assert MAX_BODY_BYTES == 1_048_576


def test_allow_list_includes_reopened_d1():
    assert ALLOWED_ACTIONS == frozenset({"opened", "synchronize", "ready_for_review", "reopened"})


def test_vector_opened():
    raw = canonical_bytes(make_payload("opened"))
    assert sign(FIXED_SECRET, raw) == EXPECTED_SIGNATURES["opened"]
    assert verify_signature(FIXED_SECRET, raw, EXPECTED_SIGNATURES["opened"]) is True


def test_vector_synchronize():
    raw = canonical_bytes(make_payload("synchronize"))
    assert sign(FIXED_SECRET, raw) == EXPECTED_SIGNATURES["synchronize"]
    assert verify_signature(FIXED_SECRET, raw, EXPECTED_SIGNATURES["synchronize"]) is True


def test_vector_ready_for_review():
    raw = canonical_bytes(make_payload("ready_for_review"))
    assert sign(FIXED_SECRET, raw) == EXPECTED_SIGNATURES["ready_for_review"]
    assert verify_signature(FIXED_SECRET, raw, EXPECTED_SIGNATURES["ready_for_review"]) is True


def test_vector_reopened_d1():
    raw = canonical_bytes(make_payload("reopened"))
    assert sign(FIXED_SECRET, raw) == EXPECTED_SIGNATURES["reopened"]
    assert verify_signature(FIXED_SECRET, raw, EXPECTED_SIGNATURES["reopened"]) is True


# --- end-to-end per action (US1.AC1–AC3) ------------------------------------


def _accepted(action):
    raw = canonical_bytes(make_payload(action))
    table, sqs = fresh_doubles()
    event = make_event(raw, EXPECTED_SIGNATURES[action], delivery=GUID_BY_ACTION[action])
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1
    envelope = json.loads(sqs.calls[0]["MessageBody"])
    assert envelope["action"] == action
    assert envelope["delivery_guid"] == GUID_BY_ACTION[action]
    assert envelope["head_sha"] == HEAD_SHA
    assert f"delivery:{GUID_BY_ACTION[action]}" in table.items


def test_accept_opened():
    _accepted("opened")


def test_accept_synchronize():
    _accepted("synchronize")


def test_accept_ready_for_review():
    _accepted("ready_for_review")


def test_accept_reopened_d1():
    _accepted("reopened")


# --- contract cases (US1.AC4; HLD §2.1 response table) -----------------------


def test_signed_non_pull_request_event_discarded():
    """Signed `issues` event → 200, nothing enqueued, nothing marked."""
    raw = canonical_bytes({"action": "opened", "issue": {"number": 7}})
    table, sqs = fresh_doubles()
    event = make_event(raw, sign(FIXED_SECRET, raw), event_type="issues")
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_tampered_signature_rejected():
    """One flipped hex char → 401, nothing enqueued, nothing marked."""
    raw = canonical_bytes(make_payload("opened"))
    good = EXPECTED_SIGNATURES["opened"]
    tampered = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert verify_signature(FIXED_SECRET, raw, tampered) is False
    table, sqs = fresh_doubles()
    response = handler(
        make_event(raw, tampered), None, _table=table, _sqs=sqs, _secret=FIXED_SECRET
    )
    assert response == {"statusCode": 401, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_missing_signature_header_rejected():
    raw = canonical_bytes(make_payload("opened"))
    table, sqs = fresh_doubles()
    event = make_event(raw, EXPECTED_SIGNATURES["opened"])
    del event["headers"]["X-Hub-Signature-256"]
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 401, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_missing_event_header_rejected():
    raw = canonical_bytes(make_payload("opened"))
    table, sqs = fresh_doubles()
    event = make_event(raw, EXPECTED_SIGNATURES["opened"])
    del event["headers"]["X-GitHub-Event"]
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 401, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_missing_delivery_header_rejected():
    raw = canonical_bytes(make_payload("opened"))
    table, sqs = fresh_doubles()
    event = make_event(raw, EXPECTED_SIGNATURES["opened"])
    del event["headers"]["X-GitHub-Delivery"]
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 401, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_ingress_uses_singular_get_parameter():
    """Carry-forward ⑨: ingress hydrates via singular `get_parameter`
    (WithDecryption) — never the worker-side batched `get_parameters`."""
    raw = canonical_bytes(make_payload("opened"))
    table, sqs = fresh_doubles()
    ssm = FakeSSM(FIXED_SECRET)
    assert not hasattr(ssm, "get_parameters")
    event = make_event(raw, EXPECTED_SIGNATURES["opened"])
    response = handler(event, None, _ssm=ssm, _table=table, _sqs=sqs)
    assert response == {"statusCode": 202, "body": ""}
    assert len(ssm.calls) == 1
    assert ssm.calls[0] == {"Name": FIXED_SECRET_NAME, "WithDecryption": True}


# --- QA-A: boundary-1 header handling (end-to-end) -----------------------------


def test_whitespace_padded_signature_rejected_end_to_end():
    """A valid signature padded with whitespace → 401: the gate never strips."""
    raw = canonical_bytes(make_payload("opened"))
    table, sqs = fresh_doubles()
    event = make_event(raw, " " + EXPECTED_SIGNATURES["opened"] + " ")
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 401, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_empty_hex_suffix_rejected_end_to_end():
    """A bare `sha256=` prefix with no hex → 401, nothing enqueued, nothing marked."""
    raw = canonical_bytes(make_payload("opened"))
    table, sqs = fresh_doubles()
    event = make_event(raw, "sha256=")
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 401, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []

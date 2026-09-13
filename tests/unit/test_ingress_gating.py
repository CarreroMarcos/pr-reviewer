"""T026: ingress gating unit tests (HLD §2.1; US1.AC4; FR-006–FR-008).

Full-string constant-time HMAC compare, body >1 MiB → 413 pre-decode,
event-type gate, action allow-list incl. `reopened` / draft skip → 200
discard, processed-GUID → 200 no-op, schema-invalid body → 200, SQS
failure → 500 without marking.
"""

import base64
import json

import ingress_handler
from ingress_handler import MAX_BODY_BYTES, handler, verify_signature

FIXED_SECRET = "unit-test-secret-not-a-credential"  # noqa: S105 (dummy fixture)

GUID_NEW = "11111111-1111-4111-8111-111111111111"
GUID_SEEN = "22222222-2222-4222-8222-222222222222"

HEAD_SHA = "aa" * 20
BASE_SHA = "bb" * 20


def sign(raw):
    import hashlib
    import hmac

    return "sha256=" + hmac.new(FIXED_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def make_payload(action="opened", draft=False):
    return {
        "action": action,
        "number": 7,
        "pull_request": {
            "base": {"sha": BASE_SHA},
            "draft": draft,
            "head": {"sha": HEAD_SHA},
            "number": 7,
        },
        "repository": {"full_name": "octo-org/hello-world"},
        "sender": {"login": "octocat"},
    }


def raw_of(payload):
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def make_event(raw, signature, event_type="pull_request", delivery=GUID_NEW, headers_extra=None):
    headers = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": delivery,
    }
    if headers_extra:
        headers.update(headers_extra)
    return {"headers": headers, "body": raw.decode("utf-8"), "isBase64Encoded": False}


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


def invoke(event, table=None, sqs=None):
    table = FakeTable() if table is None else table
    sqs = FakeSQS() if sqs is None else sqs
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    return response, table, sqs


# --- HMAC: full-string constant-time compare --------------------------------


def test_valid_signature_verifies():
    raw = raw_of(make_payload())
    assert verify_signature(FIXED_SECRET, raw, sign(raw)) is True


def test_tampered_suffix_rejected():
    raw = raw_of(make_payload())
    good = sign(raw)
    bad = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert verify_signature(FIXED_SECRET, raw, bad) is False


def test_correct_prefix_wrong_suffix_rejected():
    """A prefix-only compare would accept this; the full-string compare rejects."""
    raw = raw_of(make_payload())
    forged = "sha256=" + "0" * 64
    assert forged.startswith("sha256=")
    assert verify_signature(FIXED_SECRET, raw, forged) is False


def test_truncated_signature_rejected():
    raw = raw_of(make_payload())
    assert verify_signature(FIXED_SECRET, raw, sign(raw)[:20]) is False


def test_missing_signature_rejected():
    assert verify_signature(FIXED_SECRET, raw_of(make_payload()), None) is False


def test_malformed_prefix_rejected():
    raw = raw_of(make_payload())
    assert verify_signature(FIXED_SECRET, raw, "md5=" + "0" * 32) is False


def test_uppercase_hex_rejected():
    raw = raw_of(make_payload())
    assert verify_signature(FIXED_SECRET, raw, sign(raw).upper()) is False


def test_compare_digest_called_with_full_strings(monkeypatch):
    """Pin the mechanism: one `compare_digest` call over the complete strings."""
    seen = []
    real = ingress_handler.hmac.compare_digest

    def spy(first, second):
        seen.append((first, second))
        return real(first, second)

    monkeypatch.setattr(ingress_handler.hmac, "compare_digest", spy)
    raw = raw_of(make_payload())
    expected = sign(raw)
    assert verify_signature(FIXED_SECRET, raw, expected) is True
    assert len(seen) == 1
    assert seen[0] == (expected, expected)


def test_wrong_secret_rejected_end_to_end():
    raw = raw_of(make_payload())
    event = make_event(raw, sign(raw))
    response, _, sqs = invoke(event, sqs=FakeSQS())
    # Signed with FIXED_SECRET but handler uses another secret → 401.
    other = handler(event, None, _table=FakeTable(), _sqs=FakeSQS(), _secret="other")  # noqa: S106 (dummy fixture)
    assert other == {"statusCode": 401, "body": ""}
    assert response == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1


# --- 413 pre-decode ----------------------------------------------------------


def test_body_over_1mib_rejected_pre_decode():
    assert MAX_BODY_BYTES == 1_048_576
    raw = b"x" * (MAX_BODY_BYTES + 1)
    event = make_event(raw, "sha256=" + "0" * 64)
    table, sqs = FakeTable(), FakeSQS()
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 413, "body": ""}
    assert table.get_calls == []  # rejected before any state touch
    assert table.put_calls == []
    assert sqs.calls == []


def test_base64_encoded_oversized_wire_rejected_pre_decode():
    wire = base64.b64encode(b"y" * 64).decode() * 20_000  # >> 1 MiB on the wire
    assert len(wire.encode()) > MAX_BODY_BYTES
    event = {
        "headers": {
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": GUID_NEW,
        },
        "body": wire,
        "isBase64Encoded": True,
    }
    table, sqs = FakeTable(), FakeSQS()
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 413, "body": ""}
    assert table.get_calls == []
    assert sqs.calls == []


def test_content_length_gate_rejects_before_body():
    raw = b"z" * 16
    event = make_event(raw, sign(raw), headers_extra={"Content-Length": str(MAX_BODY_BYTES + 1)})
    table, sqs = FakeTable(), FakeSQS()
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 413, "body": ""}
    assert table.get_calls == []
    assert sqs.calls == []


# --- event-type gate ---------------------------------------------------------


def test_non_pull_request_event_discarded():
    raw = raw_of({"action": "opened"})
    event = make_event(raw, sign(raw), event_type="ping")
    response, table, sqs = invoke(event)
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_event_header_lookup_is_case_insensitive():
    raw = raw_of(make_payload())
    event = make_event(raw, sign(raw))
    event["headers"] = {k.lower(): v for k, v in event["headers"].items()}
    response, _, sqs = invoke(event)
    assert response == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1


# --- action filter (FR-008, D1) ----------------------------------------------


def test_disallowed_actions_discarded():
    for action in ("labeled", "closed", "edited", "assigned", "review_requested"):
        raw = raw_of(make_payload(action=action))
        event = make_event(raw, sign(raw))
        response, table, sqs = invoke(event)
        assert response == {"statusCode": 200, "body": ""}, action
        assert sqs.calls == [], action
        assert table.put_calls == [], action


def test_reopened_accepted_d1():
    raw = raw_of(make_payload(action="reopened"))
    event = make_event(raw, sign(raw))
    response, _, sqs = invoke(event)
    assert response == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1


def test_draft_pr_skipped():
    raw = raw_of(make_payload(action="opened", draft=True))
    event = make_event(raw, sign(raw))
    response, table, sqs = invoke(event)
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


# --- dedup / schema / SQS failure -------------------------------------------


def test_processed_guid_is_idempotent_noop():
    seed = {f"delivery:{GUID_SEEN}": {"pk": f"delivery:{GUID_SEEN}", "ttl": 123}}
    table = FakeTable(seed=seed)
    raw = raw_of(make_payload())
    event = make_event(raw, sign(raw), delivery=GUID_SEEN)
    response = handler(event, None, _table=table, _sqs=FakeSQS(), _secret=FIXED_SECRET)
    assert response == {"statusCode": 200, "body": ""}
    assert table.put_calls == []  # no re-mark; SQS double is the caller's FakeSQS


def test_processed_guid_sends_nothing():
    seed = {f"delivery:{GUID_SEEN}": {"pk": f"delivery:{GUID_SEEN}", "ttl": 123}}
    table, sqs = FakeTable(seed=seed), FakeSQS()
    raw = raw_of(make_payload())
    event = make_event(raw, sign(raw), delivery=GUID_SEEN)
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []


def test_unparseable_body_discarded():
    raw = b"{not json"
    event = make_event(raw, sign(raw))
    response, table, sqs = invoke(event)
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_schema_invalid_body_discarded():
    raw = raw_of({"action": "opened"})  # missing every envelope field
    event = make_event(raw, sign(raw))
    response, table, sqs = invoke(event)
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_sqs_failure_returns_500_without_marking():
    raw = raw_of(make_payload())
    event = make_event(raw, sign(raw))
    table, sqs = FakeTable(), FakeSQS(fail=True)
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 500, "body": ""}
    assert f"delivery:{GUID_NEW}" not in table.items  # never mark undelivered
    assert table.put_calls == []


# --- QA-A red (F1): hostile envelope shapes discard with 200, never raise ---


def _signed_hostile_event(payload):
    """Sign the exact hostile bytes: HMAC-valid but schema-hostile delivery."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return make_event(raw, sign(raw))


def test_unhashable_action_discarded():
    # Given an HMAC-valid payload whose action is unhashable (list)
    payload = make_payload()
    payload["action"] = ["opened"]
    # When ingress handles it Then 200 discard, nothing enqueued or marked
    table, sqs = FakeTable(), FakeSQS()
    response = handler(
        _signed_hostile_event(payload), None, _table=table, _sqs=sqs, _secret=FIXED_SECRET
    )
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_string_repository_discarded():
    # Given an HMAC-valid payload with a string where the repository object belongs
    payload = make_payload()
    payload["repository"] = "evil-string"
    # When ingress handles it Then 200 discard, nothing enqueued or marked
    table, sqs = FakeTable(), FakeSQS()
    response = handler(
        _signed_hostile_event(payload), None, _table=table, _sqs=sqs, _secret=FIXED_SECRET
    )
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_string_sender_discarded():
    # Given an HMAC-valid payload with a string where the sender object belongs
    payload = make_payload()
    payload["sender"] = "octocat"
    # When ingress handles it Then 200 discard, nothing enqueued or marked
    table, sqs = FakeTable(), FakeSQS()
    response = handler(
        _signed_hostile_event(payload), None, _table=table, _sqs=sqs, _secret=FIXED_SECRET
    )
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_string_head_discarded():
    # Given an HMAC-valid payload with a string where the head object belongs
    payload = make_payload()
    payload["pull_request"]["head"] = HEAD_SHA
    # When ingress handles it Then 200 discard, nothing enqueued or marked
    table, sqs = FakeTable(), FakeSQS()
    response = handler(
        _signed_hostile_event(payload), None, _table=table, _sqs=sqs, _secret=FIXED_SECRET
    )
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


def test_list_base_discarded():
    # Given an HMAC-valid payload with a list where the base object belongs
    payload = make_payload()
    payload["pull_request"]["base"] = [BASE_SHA]
    # When ingress handles it Then 200 discard, nothing enqueued or marked
    table, sqs = FakeTable(), FakeSQS()
    response = handler(
        _signed_hostile_event(payload), None, _table=table, _sqs=sqs, _secret=FIXED_SECRET
    )
    assert response == {"statusCode": 200, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []

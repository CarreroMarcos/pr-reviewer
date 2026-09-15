"""T026: ingress gating unit tests (HLD §2.1; US1.AC4; FR-006–FR-008).

Full-string constant-time HMAC compare, body >1 MiB → 413 pre-decode,
event-type gate, action allow-list incl. `reopened` / draft skip → 200
discard, processed-GUID → 200 no-op, schema-invalid body → 200, SQS
failure → 500 without marking.
"""

import base64
import json

import ingress_handler
import ingress_handler as ingress_module
from ingress_handler import (
    MAX_BODY_BYTES,
    build_envelope_body,
    get_header,
    get_webhook_secret,
    handler,
    normalize_body,
    reset_secret_cache,
    verify_signature,
)

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


def test_every_disposition_logs_structured_status_line(capsys):
    """G6-F3 (SPR-62): every ingress disposition emits exactly one structured
    status line (`statusCode` + `decision` + `reason` via `common.logs`), so
    the 401-spike alarm's filter { $.statusCode = 401 } has signal to match
    and gate decisions are queryable."""
    raw = raw_of(make_payload())
    bad_sig = handler(
        make_event(raw, "sha256=" + "0" * 64),
        None,
        _table=FakeTable(),
        _sqs=FakeSQS(),
        _secret=FIXED_SECRET,
    )
    assert bad_sig == {"statusCode": 401, "body": ""}
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [json.loads(line) for line in lines] == [
        {"statusCode": 401, "decision": "rejected", "reason": "bad_signature"}
    ]

    accepted = handler(
        make_event(raw, sign(raw)),
        None,
        _table=FakeTable(),
        _sqs=FakeSQS(),
        _secret=FIXED_SECRET,
    )
    assert accepted == {"statusCode": 202, "body": ""}
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [json.loads(line) for line in lines] == [
        {"statusCode": 202, "decision": "allowed", "reason": "enqueued"}
    ]


def test_ingress_status_lines_cover_gate_branches(capsys):
    """Each gate branch logs its own decision/reason token (one line per
    request, no secrets or payload content)."""
    raw = raw_of(make_payload())

    oversized = handler(
        make_event(b"x" * (MAX_BODY_BYTES + 1), "sha256=" + "0" * 64),
        None,
        _table=FakeTable(),
        _sqs=FakeSQS(),
        _secret=FIXED_SECRET,
    )
    assert oversized == {"statusCode": 413, "body": ""}

    non_pr = handler(
        make_event(raw, sign(raw), event_type="ping"),
        None,
        _table=FakeTable(),
        _sqs=FakeSQS(),
        _secret=FIXED_SECRET,
    )
    assert non_pr == {"statusCode": 200, "body": ""}

    seed = {f"delivery:{GUID_SEEN}": {"pk": f"delivery:{GUID_SEEN}", "ttl": 123}}
    duplicate = handler(
        make_event(raw, sign(raw), delivery=GUID_SEEN),
        None,
        _table=FakeTable(seed=seed),
        _sqs=FakeSQS(),
        _secret=FIXED_SECRET,
    )
    assert duplicate == {"statusCode": 200, "body": ""}

    dispatch_failed = handler(
        make_event(raw, sign(raw)),
        None,
        _table=FakeTable(),
        _sqs=FakeSQS(fail=True),
        _secret=FIXED_SECRET,
    )
    assert dispatch_failed == {"statusCode": 500, "body": ""}

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines == [
        {"statusCode": 413, "decision": "rejected", "reason": "body_too_large"},
        {"statusCode": 200, "decision": "discarded", "reason": "non_pr_event"},
        {"statusCode": 200, "decision": "discarded", "reason": "duplicate_delivery"},
        {"statusCode": 500, "decision": "error", "reason": "dispatch_failed"},
    ]


def test_ingress_401_lines_match_alarm_filter_shape(capsys):
    """The alarm filter `{ $.statusCode = 401 }` needs a NUMERIC statusCode:
    every 401 branch emits one, with distinct rejected reasons."""
    raw = raw_of(make_payload())
    cases = [
        (make_event(b"!!!-not-base64-!!!", "sha256=" + "0" * 64), True, "undecodable_body"),
    ]
    event = make_event(raw, "sha256=" + "0" * 64)
    del event["headers"]["X-Hub-Signature-256"]
    cases.append((event, False, "missing_auth_headers"))
    cases.append((make_event(raw, "sha256=" + "0" * 64), False, "bad_signature"))
    for event, use_base64, _ in cases:
        if use_base64:
            event["isBase64Encoded"] = True
        response = handler(event, None, _table=FakeTable(), _sqs=FakeSQS(), _secret=FIXED_SECRET)
        assert response == {"statusCode": 401, "body": ""}
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [(line["statusCode"], line["decision"], line["reason"]) for line in lines] == [
        (401, "rejected", reason) for _, _, reason in cases
    ]
    assert all(isinstance(line["statusCode"], int) for line in lines)


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


# --- QA-A: header handling ---------------------------------------------------


def test_get_header_rejects_non_dict_headers():
    # Given a non-dict headers shape Then lookup yields nothing
    assert get_header(["X-GitHub-Event"], "x-github-event") is None
    assert get_header(None, "x-github-event") is None


def test_get_header_rejects_non_string_value():
    # Given a numeric header value Then lookup yields nothing (→ 401 downstream)
    assert get_header({"X-GitHub-Event": 123}, "x-github-event") is None


def test_non_string_signature_rejected():
    # Given non-string signature shapes Then verification fails closed
    raw = raw_of(make_payload())
    assert verify_signature(FIXED_SECRET, raw, 12345) is False
    assert verify_signature(FIXED_SECRET, raw, ["sha256=" + "0" * 64]) is False


def test_empty_hex_suffix_rejected():
    # Given a bare prefix with no hex Then verification fails closed
    assert verify_signature(FIXED_SECRET, raw_of(make_payload()), "sha256=") is False


def test_whitespace_padded_signature_rejected():
    # Given a valid signature padded with whitespace Then it fails closed:
    # the gate never strips — exact full-string match or 401.
    raw = raw_of(make_payload())
    assert verify_signature(FIXED_SECRET, raw, " " + sign(raw) + " ") is False


def test_bytes_secret_verifies():
    # Given a bytes webhook secret Then the bytes path verifies a matching signature
    import hashlib
    import hmac as hmac_module

    raw = raw_of(make_payload())
    expected = "sha256=" + hmac_module.new(b"bytes-key", raw, hashlib.sha256).hexdigest()
    assert verify_signature(b"bytes-key", raw, expected) is True
    assert verify_signature(b"other-key", raw, expected) is False


# --- QA-A: body normalization ------------------------------------------------


def test_non_string_body_treated_as_empty():
    # Given a non-string wire body Then normalization yields empty bytes (→ 401, never crash)
    assert normalize_body({"body": 12345, "headers": {}}) == b""


def test_non_numeric_content_length_ignored():
    # Given garbage Content-Length with a valid signed delivery
    # When ingress handles it Then the wire gate decides: 202 accepted
    raw = raw_of(make_payload())
    event = make_event(raw, sign(raw), headers_extra={"Content-Length": "not-a-number"})
    response, _, sqs = invoke(event)
    assert response == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1


def test_content_length_within_bound_proceeds():
    # Given an honest Content-Length under the cap Then the delivery proceeds
    raw = raw_of(make_payload())
    event = make_event(raw, sign(raw), headers_extra={"Content-Length": str(len(raw))})
    response, _, sqs = invoke(event)
    assert response == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1


def test_valid_base64_body_accepted():
    # Given a base64-flagged delivery of valid signed bytes Then 202 with one enqueue
    import base64 as base64_module

    raw = raw_of(make_payload())
    event = make_event(raw, sign(raw))
    event["body"] = base64_module.b64encode(raw).decode()
    event["isBase64Encoded"] = True
    response, table, sqs = invoke(event)
    assert response == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1
    assert f"delivery:{GUID_NEW}" in table.items


def test_invalid_base64_body_rejected_as_unauthenticated():
    # Given undecodable wire bytes Then authenticity is unestablishable → 401, nothing touched
    event = make_event(b"!!!-not-base64-!!!", "sha256=" + "0" * 64)
    event["isBase64Encoded"] = True
    table, sqs = FakeTable(), FakeSQS()
    response = handler(event, None, _table=table, _sqs=sqs, _secret=FIXED_SECRET)
    assert response == {"statusCode": 401, "body": ""}
    assert table.get_calls == []
    assert sqs.calls == []


# --- QA-A: envelope-body extraction ------------------------------------------


def test_non_object_payload_yields_no_envelope():
    # Given non-dict payload shapes Then no envelope (→ 200 downstream)
    assert build_envelope_body(["opened"], GUID_NEW) is None
    assert build_envelope_body("opened", GUID_NEW) is None
    assert build_envelope_body(None, GUID_NEW) is None


def test_envelope_invalid_payload_yields_no_envelope():
    # Given a well-shaped dict that fails envelope validation (no repository)
    # Then no envelope (→ 200 downstream)
    payload = make_payload()
    del payload["repository"]
    assert build_envelope_body(payload, GUID_NEW) is None


# --- QA-A: secret cache (cold fetch, warm hit, TTL, reset) -------------------


class FakeSecretSSM:
    """Singular `get_parameter` double counting fetches."""

    def __init__(self, secret):
        self.secret = secret
        self.calls = []

    def get_parameter(self, Name, WithDecryption=False):  # noqa: N803 (boto3 shape)
        self.calls.append({"Name": Name, "WithDecryption": WithDecryption})
        return {"Parameter": {"Value": self.secret}}


def test_webhook_secret_cached_warm_within_ttl():
    # Given a cold fetch at t0 Then access inside 30 minutes reuses it without refetch
    reset_secret_cache()
    try:
        ssm = FakeSecretSSM(FIXED_SECRET)
        assert get_webhook_secret(ssm, now=1_000_000.0) == FIXED_SECRET
        assert get_webhook_secret(ssm, now=1_000_000.0 + 29 * 60) == FIXED_SECRET
        assert len(ssm.calls) == 1
    finally:
        reset_secret_cache()


def test_webhook_secret_refetched_at_ttl_boundary():
    # Given a cold fetch at t0 Then access exactly at the TTL refetches (expiry is >=)
    reset_secret_cache()
    try:
        ssm = FakeSecretSSM(FIXED_SECRET)
        assert get_webhook_secret(ssm, now=1_000_000.0) == FIXED_SECRET
        assert get_webhook_secret(ssm, now=1_000_000.0 + 30 * 60) == FIXED_SECRET
        assert len(ssm.calls) == 2
    finally:
        reset_secret_cache()


def test_reset_secret_cache_drops_warm_value():
    # Given a warm cache Then reset forces the next access to fetch again
    reset_secret_cache()
    try:
        ssm = FakeSecretSSM(FIXED_SECRET)
        get_webhook_secret(ssm, now=1_000_000.0)
        reset_secret_cache()
        get_webhook_secret(ssm, now=1_000_000.0 + 1)
        assert len(ssm.calls) == 2
    finally:
        reset_secret_cache()


# --- QA-A: downstream failure mapping ----------------------------------------


class ExplodingSSM:
    def get_parameter(self, Name, WithDecryption=False):  # noqa: N803 (boto3 shape)
        raise RuntimeError("ssm unavailable")


def test_ssm_failure_returns_500():
    # Given an SSM outage during secret hydration Then 500, nothing enqueued or marked
    raw = raw_of(make_payload())
    table, sqs = FakeTable(), FakeSQS()
    response = handler(
        make_event(raw, sign(raw)), None, _ssm=ExplodingSSM(), _table=table, _sqs=sqs
    )
    assert response == {"statusCode": 500, "body": ""}
    assert sqs.calls == []
    assert table.put_calls == []


class ExplodingTable(FakeTable):
    def __init__(self, fail_get=False, fail_put=False):
        super().__init__()
        self.fail_get = fail_get
        self.fail_put = fail_put

    def get_item(self, Key):  # noqa: N803 (boto3 shape)
        if self.fail_get:
            raise RuntimeError("dynamodb unavailable")
        return super().get_item(Key)

    def put_item(self, Item):  # noqa: N803 (boto3 shape)
        if self.fail_put:
            raise RuntimeError("dynamodb unavailable")
        return super().put_item(Item)


def test_dedup_read_failure_returns_500():
    # Given a DynamoDB outage on the dedup read Then 500, nothing enqueued
    raw = raw_of(make_payload())
    table, sqs = ExplodingTable(fail_get=True), FakeSQS()
    response = handler(
        make_event(raw, sign(raw)), None, _table=table, _sqs=sqs, _secret=FIXED_SECRET
    )
    assert response == {"statusCode": 500, "body": ""}
    assert sqs.calls == []


def test_mark_processed_failure_returns_500():
    # Given enqueue succeeded but the mark fails Then 500 (delivery retries later)
    raw = raw_of(make_payload())
    table, sqs = ExplodingTable(fail_put=True), FakeSQS()
    response = handler(
        make_event(raw, sign(raw)), None, _table=table, _sqs=sqs, _secret=FIXED_SECRET
    )
    assert response == {"statusCode": 500, "body": ""}
    assert len(sqs.calls) == 1
    assert f"delivery:{GUID_NEW}" not in table.items


# --- QA-A: production wiring (zero-injection path, boto3 doubled) ------------


def _production_wiring(monkeypatch, secret=FIXED_SECRET, fail_ssm=False):
    """Double the boto3 module surface the handler's lazy wiring touches."""
    import boto3

    ssm = FakeSecretSSM(secret)
    table, sqs = FakeTable(), FakeSQS()

    def fake_client(service, *args, **kwargs):
        if fail_ssm and service == "ssm":
            raise RuntimeError("ssm unavailable")
        assert service in ("ssm", "sqs"), service
        return ssm if service == "ssm" else sqs

    class FakeResource:
        def Table(self, name):  # noqa: N803 (boto3 shape)
            assert name
            return table

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(boto3, "resource", lambda service, *a, **k: FakeResource())
    return ssm, table, sqs


def test_production_wiring_accepts_signed_delivery(monkeypatch):
    # Given zero injections (the deployed path) Then SSM→HMAC→dedup→SQS→mark yields 202
    reset_secret_cache()
    try:
        ssm, table, sqs = _production_wiring(monkeypatch)
        raw = raw_of(make_payload())
        response = ingress_module.handler(make_event(raw, sign(raw)), None)
        assert response == {"statusCode": 202, "body": ""}
        assert len(ssm.calls) == 1
        assert len(sqs.calls) == 1
        assert f"delivery:{GUID_NEW}" in table.items
    finally:
        reset_secret_cache()


def test_production_ssm_outage_returns_500(monkeypatch):
    # Given zero injections with SSM down Then 500 before any state touch
    reset_secret_cache()
    try:
        _, table, sqs = _production_wiring(monkeypatch, fail_ssm=True)
        raw = raw_of(make_payload())
        response = ingress_module.handler(make_event(raw, sign(raw)), None)
        assert response == {"statusCode": 500, "body": ""}
        assert sqs.calls == []
        assert table.put_calls == []
    finally:
        reset_secret_cache()


def test_injected_table_with_doubled_sqs_client(monkeypatch):
    # Given an injected table but no SQS client Then the lazy SQS wiring fills the gap → 202
    import boto3

    sqs = FakeSQS()
    monkeypatch.setattr(boto3, "client", lambda service, *a, **k: sqs)
    raw = raw_of(make_payload())
    response = handler(make_event(raw, sign(raw)), None, _table=FakeTable(), _secret=FIXED_SECRET)
    assert response == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1


def test_injected_sqs_with_doubled_table_resource(monkeypatch):
    # Given an injected SQS client but no table Then the lazy table wiring fills the gap → 202
    import boto3

    table = FakeTable()
    sqs = FakeSQS()

    class FakeResource:
        def Table(self, name):  # noqa: N803 (boto3 shape)
            assert name
            return table

    monkeypatch.setattr(boto3, "resource", lambda service, *a, **k: FakeResource())
    raw = raw_of(make_payload())
    response = handler(
        make_event(raw, sign(raw)), None, _table=None, _sqs=sqs, _secret=FIXED_SECRET
    )
    assert response == {"statusCode": 202, "body": ""}
    assert len(sqs.calls) == 1
    assert f"delivery:{GUID_NEW}" in table.items

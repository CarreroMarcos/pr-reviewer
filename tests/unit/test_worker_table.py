"""Unit tests for the `_BotoTable` port wrapper and the publish port's
PATCH/POST decision — the exactly-one canonical comment contract (HLD §2.8).

Regression: boto3 deserializes DynamoDB numbers as `decimal.Decimal`, while
the table-port contract (mirrored by the state-machine stub) is plain ints.
The live T035 acceptance run surfaced both failures at once: `publish`
treated a stored `comment_id` as absent (Decimal fails
`isinstance(comment_id, int)`) and POSTed a second canonical comment, and
the §5.4 outcome event's json.dumps rejected the Decimal generation
(`worker_emit_failed` on every ride)."""

from decimal import Decimal

import pytest

from common.envelope import Envelope
from worker_handler import _BotoTable, _make_publish


class _FakeBotoTable:
    """boto3 Table double: `get_item(Key=...)` returning raw DynamoDB shapes."""

    def __init__(self, item: dict | None) -> None:
        self._item = item
        self.keys: list[dict] = []

    def get_item(self, *, Key: dict) -> dict:
        self.keys.append(Key)
        return {"Item": dict(self._item)} if self._item is not None else {}


def _stored_item() -> dict:
    return {
        "pk": "review:org/repo#7",
        "comment_id": Decimal("5658256138"),
        "generation": Decimal("0"),
        "head_sha": "a" * 40,
        "status": "ACTIVE",
        "updated_at": "2026-09-14T02:41:13Z",
    }


def test_get_item_normalizes_decimal_numbers_to_int() -> None:
    table = _BotoTable(_FakeBotoTable(_stored_item()))
    item = table.get_item("review:org/repo#7")
    assert item is not None
    assert item["comment_id"] == 5658256138
    assert isinstance(item["comment_id"], int)
    assert item["generation"] == 0
    assert item["head_sha"] == "a" * 40  # strings pass through untouched
    assert table._table.keys == [{"pk": "review:org/repo#7"}]


def test_get_item_absent_returns_none() -> None:
    table = _BotoTable(_FakeBotoTable(None))
    assert table.get_item("review:org/repo#7") is None


def test_get_item_non_integral_decimal_raises() -> None:
    item = _stored_item()
    item["generation"] = Decimal("1.5")
    with pytest.raises(ValueError, match="generation"):
        _BotoTable(_FakeBotoTable(item)).get_item("review:org/repo#7")


class _FakeCreds:
    def current(self):
        return self

    @property
    def github_token(self) -> str:
        return "tok"


def _envelope() -> Envelope:
    return Envelope(
        envelope_version="1",
        event_type="pull_request",
        action="reopened",
        repo_full_name="org/repo",
        pr_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        sender="octocat",
        delivery_guid="guid-1",
    )


def test_publish_patches_stored_comment() -> None:
    calls: list[tuple[str, str]] = []

    def transport(method: str, url: str, headers: dict, body: bytes):
        calls.append((method, url))
        return 200, b'{"id": 5658256138}'

    publish = _make_publish(
        envelope=_envelope(),
        creds=_FakeCreds(),
        table=_BotoTable(_FakeBotoTable(_stored_item())),
        pk="review:org/repo#7",
        github_transport=transport,
    )
    assert publish("## Summary") == 5658256138
    assert calls[0][0] == "PATCH"
    assert "/issues/comments/5658256138" in calls[0][1]


def test_publish_posts_when_comment_id_absent() -> None:
    calls: list[tuple[str, str]] = []

    def transport(method: str, url: str, headers: dict, body: bytes):
        calls.append((method, url))
        return 201, b'{"id": 42}'

    publish = _make_publish(
        envelope=_envelope(),
        creds=_FakeCreds(),
        table=_BotoTable(_FakeBotoTable(None)),
        pk="review:org/repo#7",
        github_transport=transport,
    )
    assert publish("## Summary") == 42
    assert calls[0][0] == "POST"
    assert calls[0][1].endswith("/issues/7/comments")

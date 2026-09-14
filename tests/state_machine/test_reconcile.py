"""T036: comment reconciliation on a fake table (HLD §3.4; FR-003; US2.AC3).

Exercises `common.reconcile.reconcile` — fully-paginated, shape-validated
comment listing + exact-marker match + deterministic adoption/deletion +
creation lease (`attribute_not_exists(comment_id)` + claim condition).

Mapping (HLD §3.4 clause → test):

* exactly one marker match → adopt →
  `test_single_marker_match_is_adopted_and_persisted`
* multiple matches → lowest comment ID wins, extras deleted →
  `test_lowest_comment_id_wins_with_extras_deleted`
* listing spans pages (a full page implies more) →
  `test_listing_is_fully_paginated_before_matching`
* adopted comment vanished between list and PATCH → re-list round →
  `test_vanished_winner_retries_with_fresh_listing`
* creation-lease race → loser adopts winner's comment →
  `test_creation_lease_race_loser_adopts_winner`
* winner path (lease → POST → persist → re-check) →
  `test_creation_lease_winner_posts_persists_and_rechecks`
* unparseable list → non-retryable `ReconcileError("list_unreadable")` →
  `test_unparseable_*` (three shapes)

Ports (duck-typed — fakes mirror these exactly):

* table: `get_item(pk) -> dict | None` (copy semantics),
  `update_item(Key=..., UpdateExpression=..., ConditionExpression=...,
  ExpressionAttributeNames=..., ExpressionAttributeValues=...)` raising
  `common.protocol.ConditionalCheckFailed` when the condition does not hold.
  `FakeTable` below evaluates EXACTLY the two condition strings the module
  emits (finalize-shaped adopt persist + creation lease) and raises
  ValueError on anything else — a condition-string change must break tests,
  never pass silently (same Gate-2 discipline as `dynamodb_stub.py`).
* `list_page(page) -> parsed JSON value` (page numbers from 1): one GitHub
  list-comments page; the module owns pagination.
* `create_comment(body) -> int` (POST), `update_comment(id, body)` (PATCH,
  raising `CommentNotFound` when the target vanished),
  `delete_comment(id)` (DELETE).

All tests are deterministic: fixed clock (`NOW`), scripted list rounds, no
sleeps. RED phase: `common.reconcile` does not exist yet, so this module
fails at import (expected — T039 implements it).
"""

from common.marker import build_marker
from common.protocol import ConditionalCheckFailed
from common.reconcile import (
    PER_PAGE,
    CommentNotFound,
    ReconcileError,
    reconcile,
)

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
MARKER = build_marker(REPO, PR_NUMBER)
OTHER_MARKER = build_marker(REPO, 43)
SHA_B = "bb" * 20
GUID_A = "11111111-1111-4111-8111-111111111111"
GUID_B = "22222222-2222-4222-8222-222222222222"
NOW = 1_750_000_000
CONTENT = "fresh review content " + MARKER
UPDATED_AT = "2026-09-12T10:00:00Z"

_FINALIZE_CONDITION = "head_sha = :reviewed AND generation = :gen"
_CREATION_LEASE_CONDITION = (
    "head_sha = :reviewed AND generation = :gen "
    "AND (claim_until < :now OR attribute_not_exists(claim_owner) "
    "OR claim_owner = :owner) AND attribute_not_exists(comment_id)"
)


def _comment(comment_id, body=CONTENT):
    return {"id": comment_id, "body": body}


class FakeTable:
    """Minimal table double evaluating exactly the two reconcile conditions."""

    def __init__(self):
        self.items = {}
        self.updates = []

    def get_item(self, pk):
        item = self.items.get(pk)
        return dict(item) if item is not None else None

    def update_item(
        self,
        *,
        Key,
        UpdateExpression,
        ConditionExpression,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
    ):
        pk = Key["pk"]
        self.updates.append(ConditionExpression)
        current = self.items.get(pk)
        values = ExpressionAttributeValues or {}
        if not _holds(ConditionExpression, current, values):
            raise ConditionalCheckFailed(f"condition not met: {ConditionExpression} (pk={pk})")
        item = dict(current) if current is not None else {"pk": pk}
        names = ExpressionAttributeNames or {}
        set_part, sep, remove_part = UpdateExpression[4:].partition(" REMOVE ")
        assert UpdateExpression.startswith("SET ")
        for clause in set_part.split(", "):
            attr, _, placeholder = clause.partition(" = ")
            item[names.get(attr, attr)] = values[placeholder]
        if sep:
            for attr in remove_part.split(", "):
                item.pop(names.get(attr, attr), None)
        self.items[pk] = item
        return {"Attributes": dict(item)}


def _holds(condition, current, values):
    if condition == _FINALIZE_CONDITION:
        return (
            current is not None
            and current.get("head_sha") == values[":reviewed"]
            and current.get("generation") == values[":gen"]
        )
    if condition == _CREATION_LEASE_CONDITION:
        return (
            current is not None
            and current.get("head_sha") == values[":reviewed"]
            and current.get("generation") == values[":gen"]
            and (
                current.get("claim_until", 0) < values[":now"]
                or "claim_owner" not in current
                or current.get("claim_owner") == values[":owner"]
            )
            and "comment_id" not in current
        )
    raise ValueError(f"unsupported condition expression: {condition!r}")


def _seed(table, *, owner, until, comment=None):
    item = {
        "pk": PK,
        "status": "CLAIMED",
        "generation": 2,
        "head_sha": SHA_B,
        "last_seen_sha": SHA_B,
        "claim_owner": owner,
        "claim_until": until,
        "updated_at": UPDATED_AT,
    }
    if comment is not None:
        item["comment_id"] = comment
    table.items[PK] = item
    return item


class ScriptedPorts:
    """GitHub-side doubles: scripted list rounds + recording CRUD calls."""

    def __init__(self, *, list_rounds, post_id=100):
        self._rounds = [list(r) for r in list_rounds]
        self._post_id = post_id
        self.list_calls = []
        self.created = []
        self.updated = []
        self.deleted = []
        self._vanish_once = []

    def vanish_once(self, comment_id):
        """Next `update_comment` of this id raises CommentNotFound (404 race)."""
        self._vanish_once.append(comment_id)

    def list_page(self, page):
        self.list_calls.append(page)
        if len(self._rounds) > 1:
            return self._rounds.pop(0)
        return list(self._rounds[0])

    def create_comment(self, body):
        self.created.append(body)
        return self._post_id

    def update_comment(self, comment_id, body):
        if comment_id in self._vanish_once:
            self._vanish_once.remove(comment_id)
            raise CommentNotFound(comment_id)
        self.updated.append((comment_id, body))

    def delete_comment(self, comment_id):
        self.deleted.append(comment_id)


def _run(table, ports, *, owner=GUID_A, content=CONTENT, **kwargs):
    return reconcile(
        repo_full_name=REPO,
        pr_number=PR_NUMBER,
        pk=PK,
        owner=owner,
        content=content,
        table=table,
        now=lambda: NOW,
        list_page=ports.list_page,
        create_comment=ports.create_comment,
        update_comment=ports.update_comment,
        delete_comment=ports.delete_comment,
        **kwargs,
    )


def test_single_marker_match_is_adopted_and_persisted():
    """Exactly one marker-bearing comment → PATCH it with fresh content,
    adopt (persist its id), no deletes, no POST."""
    table = FakeTable()
    _seed(table, owner=GUID_A, until=NOW + 100)
    ports = ScriptedPorts(
        list_rounds=[[_comment(11, "plain"), _comment(5), _comment(9, OTHER_MARKER)]]
    )
    assert _run(table, ports) == 5
    assert ports.updated == [(5, CONTENT)]
    assert ports.created == []
    assert ports.deleted == []
    item = table.items[PK]
    assert item["comment_id"] == 5
    assert item["status"] == "ACTIVE"


def test_lowest_comment_id_wins_with_extras_deleted():
    """Multiple matches → deterministically the lowest id; extras deleted;
    the decoy (other-PR marker) and plain comments are untouched."""
    table = FakeTable()
    _seed(table, owner=GUID_A, until=NOW + 100)
    ports = ScriptedPorts(
        list_rounds=[[_comment(9), _comment(3), _comment(11, "plain"), _comment(7, OTHER_MARKER)]]
    )
    assert _run(table, ports) == 3
    assert ports.updated == [(3, CONTENT)]
    assert ports.deleted == [9]
    assert table.items[PK]["comment_id"] == 3


def test_listing_is_fully_paginated_before_matching():
    """A full first page must not stop the listing: the winner on page 2
    is adopted, and the page-1 decoys are never mistaken for a match."""
    table = FakeTable()
    _seed(table, owner=GUID_A, until=NOW + 100)
    page1 = [{"id": 1000 + i, "body": "plain"} for i in range(PER_PAGE)]
    ports = ScriptedPorts(list_rounds=[page1, [_comment(9), _comment(3)]])
    assert len(page1) == PER_PAGE
    assert _run(table, ports) == 3
    assert ports.list_calls == [1, 2]  # full page 1 forces page 2; short page 2 stops
    assert ports.deleted == [9]


def test_vanished_winner_retries_with_fresh_listing():
    """PATCH of the adopted winner 404s (deleted between list and PATCH) →
    re-list round adopts the surviving marker comment; no POST."""
    table = FakeTable()
    _seed(table, owner=GUID_A, until=NOW + 100)
    ports = ScriptedPorts(list_rounds=[[_comment(5)], [_comment(7)]])
    ports.vanish_once(5)
    assert _run(table, ports) == 7
    assert ports.updated == [(7, CONTENT)]
    assert ports.created == []
    assert table.items[PK]["comment_id"] == 7


def test_creation_lease_winner_posts_persists_and_rechecks():
    """No marker comment → creation lease won → POST → persist → re-check
    lists again (straggler-free here, so no deletes)."""
    table = FakeTable()
    _seed(table, owner=GUID_A, until=NOW - 5)  # expired lease: acquirable
    ports = ScriptedPorts(list_rounds=[[], [_comment(100)]], post_id=100)
    assert _run(table, ports) == 100
    assert ports.created == [CONTENT]
    assert ports.updated == []
    assert ports.deleted == []
    assert _CREATION_LEASE_CONDITION in table.updates
    assert table.items[PK]["comment_id"] == 100
    assert ports.list_calls == [1, 1]  # initial list + post-persist re-check


def test_creation_lease_race_loser_adopts_winner():
    """B lists (empty) but loses the lease to live-holder A → B's lease
    write fails its condition → B re-runs, finds A's comment, adopts it.
    B never POSTs: exactly one comment is created."""
    table = FakeTable()
    _seed(table, owner=GUID_A, until=NOW + 100)  # A holds a live lease
    ports = ScriptedPorts(list_rounds=[[], [_comment(100)]])
    assert _run(table, ports, owner=GUID_B) == 100
    assert ports.created == []  # loser never POSTs
    assert ports.updated == [(100, CONTENT)]
    assert table.items[PK]["comment_id"] == 100


def test_unparseable_comment_entry_is_non_retryable():
    """A comment entry missing `body` poisons the whole listing: the
    unparseable list is the list-unreadable row — non-retryable."""
    table = FakeTable()
    _seed(table, owner=GUID_A, until=NOW + 100)
    ports = ScriptedPorts(list_rounds=[[{"id": 1}]])
    try:
        _run(table, ports)
    except ReconcileError as exc:
        assert exc.error_class == "list_unreadable"
    else:  # noqa: RET505 (assert-raises style would hide the error_class pin)
        raise AssertionError("expected ReconcileError")
    assert ports.created == []
    assert ports.updated == []


def test_non_list_payload_is_non_retryable():
    """A 200 whose body is not a comment array (e.g. an error dict) is
    likewise list-unreadable, never retried."""
    table = FakeTable()
    _seed(table, owner=GUID_A, until=NOW + 100)
    ports = ScriptedPorts(list_rounds=[[{"message": "oops"}]])
    try:
        _run(table, ports)
    except ReconcileError as exc:
        assert exc.error_class == "list_unreadable"
    else:
        raise AssertionError("expected ReconcileError")


def test_non_integer_comment_id_is_non_retryable():
    """A comment id of the wrong shape (bool/string) is unparseable —
    never silently skipped, never retried."""
    table = FakeTable()
    _seed(table, owner=GUID_A, until=NOW + 100)
    ports = ScriptedPorts(list_rounds=[[_comment(True)]])
    try:
        _run(table, ports)
    except ReconcileError as exc:
        assert exc.error_class == "list_unreadable"
    else:
        raise AssertionError("expected ReconcileError")

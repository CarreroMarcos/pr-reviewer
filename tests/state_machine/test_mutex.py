"""SPR-109 T024: mutex contract tests (HLD-004 D9 Concurrency Resolution item 3).

The application-level distributed mutex strictly serializes multi-agent
reviews: one row (`pk = "mutex:pr-reviewer-worker"`) carrying
`owner = delivery_guid`, `lease_until` (epoch SECONDS), `token = uuid4`.
Acquire is a conditional write iff `attribute_not_exists(pk) OR
lease_until < now − 30` (30s skew — the threshold sits 30s in the PAST,
so a lease becomes stealable only 30s AFTER its deadline:
holder-friendly, never early); expiry takeover writes the SAME attribute
shape with a fresh token; refresh at 50% TTL is a conditional write on
the stored token (mismatch → lost, reported as None — the holder MUST
stop issuing LLM work, take the degraded path, and never re-assert, a
worker-side discipline owned by T026/T027); release removes the row
conditioned on the token; `MUTEX_LEASE_TTL_S = 900` (= worker timeout,
`terraform/compute.tf:104`).

HLD-reading note (REMOVE semantics): a REMOVE-clause release would leave
a key-only row that permanently fails the pinned acquire condition
(`attribute_not_exists(pk)` false on the surviving key, `lease_until`
comparison false on the missing attribute) — a silent mutex deadlock.
"REMOVE" therefore reads as row removal (conditional DeleteItem): only
the holder can release, the row is gone, the next acquire proceeds, and
a throttled/lost release recovers via expiry takeover exactly as HLD
states. The re-acquire-after-release row below pins this; the gate owns
the reading.

`lease_until` units are a first-class pin: a ×1000 (ms) slip makes every
lease look unexpired for ~11 days, silently serializing nothing. The
seconds-scale row fails such an implementation.

Doubles discipline (Gate-11): the in-file table double mirrors the
`update_item`/`delete_item` port shapes (exact kwarg subset) and
evaluates EXACTLY the mutex expression strings — anything else raises
loudly (expression strings ARE the contract, per the `dynamodb_stub`
precedent). A dedicated double (not the shared stub) keeps this file
self-contained per the T024 file scope.

RED state: `common.mutex` does not exist — collection errors on import.
"""

import re
import time

import pytest

from common.mutex import (
    MUTEX_LEASE_TTL_S,
    MUTEX_PK,
    Lease,
    MutexError,
    acquire,
    refresh,
    release,
)
from common.protocol import ConditionalCheckFailed

T = 1_750_000_000
GUID_A = "aaaaaaaa-1111-4111-8111-111111111111"
GUID_B = "bbbbbbbb-2222-4222-8222-222222222222"

ACQUIRE_UPDATE = "SET owner = :owner, token = :token, lease_until = :until"
ACQUIRE_CONDITION = "attribute_not_exists(pk) OR lease_until < :steal_before"
REFRESH_UPDATE = "SET lease_until = :until"
TOKEN_CONDITION = "token = :token"  # noqa: S105 (expression shape, not a credential)


class MutexTable:
    """In-memory mutex table double: plain dicts keyed by `pk`, exact-match
    dispatch over the mutex expression vocabulary, `ConditionalCheckFailed`
    on unmet conditions. DynamoDB-faithful on the load-bearing edge: a
    comparison against a MISSING attribute is false (never an error)."""

    def __init__(self):
        self.items = {}
        self.calls = []

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
        self.calls.append(
            {
                "Key": Key,
                "UpdateExpression": UpdateExpression,
                "ConditionExpression": ConditionExpression,
                "values": dict(ExpressionAttributeValues or {}),
            }
        )
        pk = Key["pk"]
        current = self.items.get(pk)
        values = ExpressionAttributeValues or {}
        if (UpdateExpression, ConditionExpression) == (ACQUIRE_UPDATE, ACQUIRE_CONDITION):
            steal_before = values[":steal_before"]
            held = current is not None and (
                current.get("lease_until") is not None and current["lease_until"] >= steal_before
            )
            if held:
                raise ConditionalCheckFailed(f"mutex held: {pk}")
            self.items[pk] = {
                "pk": pk,
                "owner": values[":owner"],
                "token": values[":token"],
                "lease_until": values[":until"],
            }
            return {"Attributes": dict(self.items[pk])}
        if (UpdateExpression, ConditionExpression) == (REFRESH_UPDATE, TOKEN_CONDITION):
            if current is None or current.get("token") != values[":token"]:
                raise ConditionalCheckFailed(f"lease lost: {pk}")
            current["lease_until"] = values[":until"]
            return {"Attributes": dict(current)}
        raise ValueError(f"unsupported mutex expression: {UpdateExpression!r}")

    def delete_item(
        self,
        *,
        Key,
        ConditionExpression,
        ExpressionAttributeNames=None,
        ExpressionAttributeValues=None,
    ):
        self.calls.append({"Key": Key, "ConditionExpression": ConditionExpression})
        pk = Key["pk"]
        current = self.items.get(pk)
        values = ExpressionAttributeValues or {}
        if ConditionExpression != TOKEN_CONDITION:
            raise ValueError(f"unsupported mutex expression: {ConditionExpression!r}")
        if current is None or current.get("token") != values[":token"]:
            raise ConditionalCheckFailed(f"lease lost: {pk}")
        del self.items[pk]
        return {}


def seed(table, *, owner=GUID_A, token="tok-seed", lease_until):  # noqa: S107 (fixture default)
    table.items[MUTEX_PK] = {
        "pk": MUTEX_PK,
        "owner": owner,
        "token": token,
        "lease_until": lease_until,
    }
    return dict(table.items[MUTEX_PK])


# --- constants ---------------------------------------------------------------------------


def test_mutex_pk_and_ttl():
    assert MUTEX_PK == "mutex:pr-reviewer-worker"
    assert MUTEX_LEASE_TTL_S == 900


# --- acquire -------------------------------------------------------------------------------


def test_acquire_empty_table_writes_full_row():
    table = MutexTable()
    lease = acquire(table, owner=GUID_A, now=T)
    assert lease == Lease(owner=GUID_A, token=lease.token, lease_until=T + 900)
    assert re.fullmatch(r"[0-9a-f]{32}", lease.token) is not None  # uuid4 hex
    assert table.items[MUTEX_PK] == {
        "pk": MUTEX_PK,
        "owner": GUID_A,
        "token": lease.token,
        "lease_until": T + 900,
    }


def test_acquire_default_ttl_is_the_constant():
    table = MutexTable()
    lease = acquire(table, owner=GUID_A, now=T)
    assert lease.lease_until - T == MUTEX_LEASE_TTL_S


def test_acquire_token_override_rides_verbatim():
    table = MutexTable()
    lease = acquire(table, owner=GUID_A, now=T, token="fixed-token")  # noqa: S106 (fixture)
    assert lease.token == "fixed-token"  # noqa: S105 (fixture comparison)
    assert table.items[MUTEX_PK]["token"] == "fixed-token"  # noqa: S105 (fixture comparison)


def test_acquire_small_ttl_seam():
    table = MutexTable()
    lease = acquire(table, owner=GUID_A, now=T, ttl_s=60)
    assert lease.lease_until == T + 60


def test_acquire_held_lease_returns_none_and_preserves_row():
    table = MutexTable()
    before = seed(table, lease_until=T + 900)
    assert acquire(table, owner=GUID_B, now=T) is None
    assert table.items[MUTEX_PK] == before


def test_acquire_expired_takeover_rewrites_same_shape_with_fresh_token():
    table = MutexTable()
    seed(table, owner=GUID_A, token="tok-old", lease_until=T - 31)  # noqa: S106 (fixture)
    lease = acquire(table, owner=GUID_B, now=T, token="tok-new")  # noqa: S106 (fixture)
    assert (lease.owner, lease.token, lease.lease_until) == (GUID_B, "tok-new", T + 900)
    assert set(table.items[MUTEX_PK]) == {"pk", "owner", "lease_until", "token"}


@pytest.mark.parametrize(
    ("lease_until", "expected"),
    [(T - 30, None), (T - 31, "takeover"), (T, None), (T + 900, None)],
)
def test_acquire_skew_boundary(lease_until, expected):
    """The steal threshold is literal: `lease_until < now − 30`. At
    exactly now−30 the lease is NOT stealable (strictly less-than) —
    the 30s margin is holder-friendly."""
    table = MutexTable()
    seed(table, lease_until=lease_until)
    lease = acquire(table, owner=GUID_B, now=T)
    assert (lease is not None) == (expected == "takeover")


def test_acquire_steal_threshold_is_thirty_seconds_in_the_past():
    """Skew direction pin: the comparison value is now−30 (seconds) — a
    lease becomes stealable only 30s AFTER its deadline, never early."""
    table = MutexTable()
    seed(table, lease_until=T - 100)
    acquire(table, owner=GUID_B, now=T)
    assert table.calls[-1]["values"][":steal_before"] == T - 30


# --- refresh ---------------------------------------------------------------------------------


def test_refresh_extends_lease_on_token_match():
    table = MutexTable()
    lease = acquire(table, owner=GUID_A, now=T, token="tok-a")  # noqa: S106 (fixture)
    renewed = refresh(table, lease=lease, now=T + 200)
    assert (renewed.owner, renewed.token, renewed.lease_until) == (
        GUID_A,
        "tok-a",
        T + 200 + 900,
    )
    assert table.items[MUTEX_PK]["lease_until"] == T + 200 + 900
    assert table.items[MUTEX_PK]["token"] == "tok-a"  # noqa: S105 (fixture comparison)


def test_refresh_token_mismatch_returns_none_and_preserves_row():
    table = MutexTable()
    before = seed(table, owner=GUID_A, token="tok-a", lease_until=T + 900)  # noqa: S106 (fixture)
    lost = Lease(owner=GUID_A, token="tok-other", lease_until=T + 900)  # noqa: S106 (fixture)
    assert refresh(table, lease=lost, now=T + 200) is None
    assert table.items[MUTEX_PK] == before


def test_refresh_after_release_returns_none():
    table = MutexTable()
    lease = acquire(table, owner=GUID_A, now=T)
    assert release(table, lease=lease) is True
    assert refresh(table, lease=lease, now=T + 10) is None


def test_failed_refresh_verdict_is_sticky_none():
    """Primitive half of the failed-refresh contract: a lost lease keeps
    reporting lost (idempotent None). The consumer half — stop issuing
    LLM work, degraded path, never re-assert — is worker-side (T026/T027);
    this module's duty is an unambiguous verdict, never a silent retry."""
    table = MutexTable()
    lease = acquire(table, owner=GUID_A, now=T)
    assert release(table, lease=lease) is True
    assert refresh(table, lease=lease, now=T + 10) is None
    assert refresh(table, lease=lease, now=T + 20) is None


# --- release -----------------------------------------------------------------------------------


def test_release_removes_row_and_frees_acquire():
    """Anti-deadlock pin: after release the row is gone and a contender
    acquires cleanly (see module docstring on REMOVE semantics)."""
    table = MutexTable()
    lease = acquire(table, owner=GUID_A, now=T)
    assert release(table, lease=lease) is True
    assert table.get_item(MUTEX_PK) is None
    next_lease = acquire(table, owner=GUID_B, now=T + 5)
    assert next_lease is not None and next_lease.owner == GUID_B


def test_release_wrong_token_fails_and_preserves_row():
    table = MutexTable()
    before = seed(table, owner=GUID_A, token="tok-a", lease_until=T + 900)  # noqa: S106 (fixture)
    impostor = Lease(owner=GUID_B, token="tok-b", lease_until=T + 900)  # noqa: S106 (fixture)
    assert release(table, lease=impostor) is False
    assert table.items[MUTEX_PK] == before


def test_release_missing_row_fails():
    table = MutexTable()
    ghost = Lease(owner=GUID_A, token="tok-ghost", lease_until=T + 900)  # noqa: S106 (fixture)
    assert release(table, lease=ghost) is False


# --- units & input shape --------------------------------------------------------------------------


def test_lease_until_is_epoch_seconds():
    """A ×1000 (ms) slip would read ~1.8e12 here and silently serialize
    nothing for ~11 days — seconds-scale is pinned, not assumed."""
    table = MutexTable()
    now = time.time()
    lease = acquire(table, owner=GUID_A, now=now)
    assert isinstance(lease.lease_until, int)
    assert 1_000_000_000 < lease.lease_until < 100_000_000_000
    assert lease.lease_until == int(now) + 900


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"owner": GUID_A, "now": "soon"}, "now"),
        ({"owner": GUID_A, "now": True}, "now"),
        ({"owner": "", "now": T}, "owner"),
        ({"owner": GUID_A, "now": T, "ttl_s": 0}, "ttl_s"),
        ({"owner": GUID_A, "now": T, "ttl_s": True}, "ttl_s"),
    ],
)
def test_invalid_inputs_raise_typed_errors(kwargs, field):
    table = MutexTable()
    with pytest.raises(MutexError) as exc_info:
        acquire(table, **kwargs)
    assert exc_info.value.field == field

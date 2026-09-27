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

from common import mutex as mutex_mod
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

ACQUIRE_UPDATE = "SET #owner = :owner, #tok = :token, lease_until = :until"
ACQUIRE_CONDITION = "attribute_not_exists(pk) OR lease_until < :steal_before"
REFRESH_UPDATE = "SET lease_until = :until"
TOKEN_CONDITION = "#tok = :token"  # noqa: S105 (expression shape, not a credential)
ALIASES = {"#owner": "owner", "#tok": "token"}

# Reserved-word tripwire sample (AWS list essentials — full list is ~570
# words; this pins the expression-relevant subset INCLUDING the two known
# offenders, so any regression to bare owner/token fails closed here).
RESERVED_ESSENTIALS = frozenset(
    {
        "OWNER",
        "TOKEN",
        "STATUS",
        "NAME",
        "VALUE",
        "VALUES",
        "PATH",
        "DATA",
        "KEY",
        "ITEM",
        "TABLE",
        "ATTRIBUTE",
        "ATTRIBUTES",
        "CONDITION",
        "EXPRESSION",
        "ACTION",
        "USER",
        "ROLE",
        "SIZE",
        "COUNT",
        "ALL",
        "ANY",
        "BETWEEN",
    }
)
_EXPR_KEYWORDS = frozenset({"SET", "REMOVE", "OR", "AND", "NOT", "attribute_not_exists"})
_EXPR_CONSTANTS = (
    "_ACQUIRE_UPDATE",
    "_ACQUIRE_CONDITION",
    "_REFRESH_UPDATE",
    "_TOKEN_CONDITION",
)


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
        _check_alias_map(UpdateExpression, ConditionExpression, ExpressionAttributeNames)
        if (UpdateExpression, ConditionExpression) == (ACQUIRE_UPDATE, ACQUIRE_CONDITION):
            steal_before = values[":steal_before"]
            # DynamoDB-faithful OR: a missing row steals via arm 1; a
            # PRESENT row needs arm 2 with a PRESENT lease_until —
            # comparison against a missing attribute is false, so an
            # existing-but-incomplete row (e.g. the key-only residue a
            # REMOVE-clause release would leave) is HELD, never stealable.
            if current is None:
                stealable = True
            else:
                present = current.get("lease_until")
                stealable = present is not None and present < steal_before
            if not stealable:
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
        _check_alias_map("", ConditionExpression, ExpressionAttributeNames)
        if ConditionExpression != TOKEN_CONDITION:
            raise ValueError(f"unsupported mutex expression: {ConditionExpression!r}")
        if current is None or current.get("token") != values[":token"]:
            raise ConditionalCheckFailed(f"lease lost: {pk}")
        del self.items[pk]
        return {}


def _check_alias_map(update_expression, condition_expression, names):
    """Reserved-word recurrence guard (Gate-15 demand): every `#alias`
    referenced by the expressions must arrive with a non-None
    `ExpressionAttributeNames` map carrying the correct mapping —
    unaliased reserved words fail the scan test below, and a dropped map
    fails here."""
    used = set(re.findall(r"#[A-Za-z0-9_]+", f"{update_expression} {condition_expression}"))
    if not used:
        return
    if not isinstance(names, dict):
        raise ValueError(f"missing ExpressionAttributeNames for aliases: {sorted(used)}")
    for alias in used:
        if names.get(alias) != ALIASES.get(alias):
            raise ValueError(f"bad alias mapping for {alias}: {names!r}")


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


def _bare_identifiers(expression):
    expression = re.sub(r":[A-Za-z0-9_]+", " ", expression)  # placeholders
    expression = re.sub(r"#[A-Za-z0-9_]+", " ", expression)  # aliases
    words = re.findall(r"[A-Za-z_]+", expression)
    return {word for word in words if word not in _EXPR_KEYWORDS}


def test_no_bare_reserved_words_in_expressions():
    """Recurrence guard (Gate-15 demand): `owner`/`token` are DynamoDB
    reserved words — they must ride aliased in every expression string,
    so no bare reserved word may appear. The tripwire covers the AWS
    essentials list including both known offenders."""
    assert {"OWNER", "TOKEN"} <= RESERVED_ESSENTIALS
    expressions = [getattr(mutex_mod, name) for name in _EXPR_CONSTANTS]
    bare = set().union(*(_bare_identifiers(expression) for expression in expressions))
    assert bare == {"pk", "lease_until"}
    assert bare.isdisjoint(RESERVED_ESSENTIALS)


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


def test_incomplete_row_is_not_stealable():
    """DynamoDB-faithful edge (bot R1): a row missing `lease_until` —
    exactly the key-only residue a literal REMOVE-clause release would
    leave — satisfies NEITHER arm (`attribute_not_exists(pk)` is false
    on the surviving key; the comparison against the missing attribute
    is false), so acquire reports held, permanently. This row is the
    executable form of the release-deadlock determination: literal REMOVE
    would brick the mutex, which is why release removes the row."""
    table = MutexTable()
    table.items[MUTEX_PK] = {"pk": MUTEX_PK}
    assert acquire(table, owner=GUID_B, now=T) is None
    assert table.items[MUTEX_PK] == {"pk": MUTEX_PK}


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


def test_refresh_after_takeover_returns_none_and_preserves_takeover():
    """Load-bearing T026/T027 interleaving (gate anchor): A holds, the
    clock advances past `lease_until + 30`, B takes over with a fresh
    token — A's refresh then reports lost (None) and B's row is
    untouched."""
    table = MutexTable()
    lease_a = acquire(table, owner=GUID_A, now=T, token="tok-a")  # noqa: S106 (fixture)
    lease_b = acquire(table, owner=GUID_B, now=T + 931, token="tok-b")  # noqa: S106 (fixture)
    assert lease_b is not None and lease_b.owner == GUID_B
    assert refresh(table, lease=lease_a, now=T + 931) is None
    assert table.items[MUTEX_PK]["owner"] == GUID_B
    assert table.items[MUTEX_PK]["token"] == "tok-b"  # noqa: S105 (fixture comparison)


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


@pytest.mark.parametrize(
    ("lease", "field"),
    [
        (Lease(owner=GUID_A, token="", lease_until=T + 900), "token"),
        (Lease(owner=GUID_A, token=None, lease_until=T + 900), "token"),
        (None, "lease"),
        ("not-a-lease", "lease"),
    ],
)
def test_release_invalid_inputs_raise_typed_errors(lease, field):
    """Bot R1 Fix 2: release shares the typed-input discipline — a
    malformed lease raises instead of collapsing into `False` (which
    would conflate "invalid input" with "lease lost")."""
    table = MutexTable()
    with pytest.raises(MutexError) as exc_info:
        release(table, lease=lease)
    assert exc_info.value.field == field


@pytest.mark.parametrize(
    ("lease", "field"),
    [
        (Lease(owner=GUID_A, token="", lease_until=T + 900), "token"),
        (Lease(owner=GUID_A, token=None, lease_until=T + 900), "token"),
        (None, "lease"),
        ("not-a-lease", "lease"),
    ],
)
def test_refresh_invalid_inputs_raise_typed_errors(lease, field):
    """Gate-15 Fix 2 (accumulation-rule escalation): refresh shares the
    guard — `None`/non-Lease raises `bad_lease`, malformed token raises
    `bad_token`, instead of a raw AttributeError or a silent
    conditional miss collapsing into `None`."""
    table = MutexTable()
    with pytest.raises(MutexError) as exc_info:
        refresh(table, lease=lease, now=T)
    assert exc_info.value.field == field

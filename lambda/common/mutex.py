"""Worker review mutex (HLD-004 D9 Concurrency Resolution item 3).

One row (`pk = "mutex:pr-reviewer-worker"`) serializes multi-agent
reviews so two workers never fan out simultaneously: `owner` is the
holder's delivery GUID, `lease_until` is epoch SECONDS, `token` is a
`uuid4`-hex fencing token. `MUTEX_LEASE_TTL_S = 900` equals the worker
Lambda's hard timeout (`terraform/compute.tf:104`, the AWS maximum), so
no live invocation outlives its lease and expiry takeover recovers only
genuinely dead holders.

Primitives (caller-threads-everything; no worker wiring here — the
holder/contender consumer lands in T026/T027, exactly as `run_fanout`
stayed unreferenced until its wiring ticket):

* `acquire` — one conditional write iff `attribute_not_exists(pk) OR
  lease_until < now − 30` (the 30s clock-skew margin sits in the PAST:
  a lease becomes stealable only 30s after its deadline,
  holder-friendly). Expiry takeover is the same call writing the SAME
  attribute shape with a fresh token. Success → `Lease`; a live holder
  → `None` (expected contention, never an exception).
* `refresh` — conditional write of `lease_until = now + ttl` on the
  stored token. Match → renewed `Lease`; mismatch (expiry takeover or
  release stole the row) → `None`. The `None` verdict IS the
  failed-refresh contract's primitive half: the holder MUST stop issuing
  LLM work, take the degraded path, and never re-assert — worker-side
  (T026/T027); this module never retries silently.
* `release` — row removal conditioned on the stored token (only the
  holder releases; a mismatch or missing row → `False`, the other's
  lease untouched).

REMOVE-semantics reading (load-bearing): a REMOVE-clause release would
leave a key-only row that permanently fails the pinned acquire
condition (`attribute_not_exists(pk)` is false on the surviving key,
and a `lease_until` comparison against the missing attribute is false)
— a silent mutex deadlock no takeover could recover, contradicting
HLD's "throttled or lost release ... expiry takeover recovers it".
"REMOVE" therefore reads as row removal (conditional DeleteItem): the
row is gone, the next acquire proceeds via `attribute_not_exists(pk)`,
and the re-acquire-after-release test pins it. The gate owns this
reading.

Table port mirrors `common.protocol`: `update_item` /
`delete_item` with `Key` / `UpdateExpression` / `ConditionExpression` /
`ExpressionAttributeNames` / `ExpressionAttributeValues`, raising
`common.protocol.ConditionalCheckFailed` on unmet conditions
(production threads a boto3 table behind the `_BotoTable` wrapper).
Attribute names ride bare (none of pk/owner/lease_until/token is a
DynamoDB reserved word — same posture as `common.state`'s claim
attributes); `ExpressionAttributeNames=None` is passed explicitly per
the protocol call shape.

Pure stdlib, no I/O, no boto3 import.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from common.protocol import ConditionalCheckFailed

MUTEX_PK = "mutex:pr-reviewer-worker"
MUTEX_LEASE_TTL_S = 900

# Steal-eligibility skew (HLD D9 item 3, literal): the acquire comparison
# value is `now − SKEW`, so takeover requires expiry plus this margin.
STEAL_SKEW_S = 30


class MutexError(ValueError):
    """Typed mutex rejection: `field` names the offending input
    (`owner`, `now`, `ttl_s`, `token`), `reason` is a machine-readable
    code (`bad_owner`, `bad_now`, `bad_ttl`, `bad_token`). Never carries
    secret values (tokens are random fencing material, never credentials,
    but they still stay out of error text)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid mutex input: {field}: {reason}")


@dataclass(frozen=True)
class Lease:
    """A held mutex lease: the holder identity, its fencing token, and
    the epoch-seconds deadline the holder must refresh before."""

    owner: str
    token: str
    lease_until: int


_ACQUIRE_UPDATE = "SET owner = :owner, token = :token, lease_until = :until"
_ACQUIRE_CONDITION = "attribute_not_exists(pk) OR lease_until < :steal_before"
_REFRESH_UPDATE = "SET lease_until = :until"
_TOKEN_CONDITION = "token = :token"  # noqa: S105 (expression shape, not a credential)


def _check_owner(owner: Any) -> str:
    if not isinstance(owner, str) or not owner:
        raise MutexError("owner", "bad_owner")
    return owner


def _check_now(now: Any) -> int:
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise MutexError("now", "bad_now")
    return int(now)


def _check_ttl(ttl_s: Any) -> int:
    if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or ttl_s < 1:
        raise MutexError("ttl_s", "bad_ttl")
    return ttl_s


def _check_token(token: Any) -> str:
    if token is None:
        return uuid.uuid4().hex
    if not isinstance(token, str) or not token:
        raise MutexError("token", "bad_token")
    return token


def acquire(
    table: Any,
    *,
    owner: str,
    now: int | float,
    token: str | None = None,
    ttl_s: int = MUTEX_LEASE_TTL_S,
) -> Lease | None:
    """Acquire the mutex (or take over an expired lease): one conditional
    write of the full attribute shape. `owner` is the caller's delivery
    GUID; `now` is epoch seconds (injected clock); `token` rides verbatim
    when given (tests), else fresh `uuid4` hex; `ttl_s` overrides the
    constant (time math in tests).

    Returns the `Lease` on success (fresh row or expiry takeover —
    indistinguishable by design, same shape). Returns `None` when a live
    holder owns the row (steal threshold unmet): the caller takes the
    contender path, never spins.
    """
    holder = _check_owner(owner)
    instant = _check_now(now)
    fencing = _check_token(token)
    window = _check_ttl(ttl_s)
    lease_until = instant + window
    try:
        table.update_item(
            Key={"pk": MUTEX_PK},
            UpdateExpression=_ACQUIRE_UPDATE,
            ConditionExpression=_ACQUIRE_CONDITION,
            ExpressionAttributeNames=None,
            ExpressionAttributeValues={
                ":owner": holder,
                ":token": fencing,
                ":until": lease_until,
                ":steal_before": instant - STEAL_SKEW_S,
            },
        )
    except ConditionalCheckFailed:
        return None
    return Lease(owner=holder, token=fencing, lease_until=lease_until)


def refresh(
    table: Any, *, lease: Lease, now: int | float, ttl_s: int = MUTEX_LEASE_TTL_S
) -> Lease | None:
    """Refresh a held lease: conditional write of the new deadline on the
    stored token. Match → renewed `Lease` (same owner/token, extended
    deadline). Mismatch → `None`: the lease was lost to expiry takeover
    or release, and the caller MUST stop issuing LLM work, take the
    degraded path, and never re-assert (T026/T027 own that discipline —
    this verdict never retries).
    """
    instant = _check_now(now)
    window = _check_ttl(ttl_s)
    lease_until = instant + window
    try:
        table.update_item(
            Key={"pk": MUTEX_PK},
            UpdateExpression=_REFRESH_UPDATE,
            ConditionExpression=_TOKEN_CONDITION,
            ExpressionAttributeNames=None,
            ExpressionAttributeValues={":token": lease.token, ":until": lease_until},
        )
    except ConditionalCheckFailed:
        return None
    return Lease(owner=lease.owner, token=lease.token, lease_until=lease_until)


def release(table: Any, *, lease: Lease) -> bool:
    """Release a held lease: row removal conditioned on the stored token.
    `True` → the row is gone and the next acquire proceeds. `False` →
    the row was already taken over, released, or never existed: someone
    else's lease is untouched (release never force-clears).
    """
    try:
        table.delete_item(
            Key={"pk": MUTEX_PK},
            ConditionExpression=_TOKEN_CONDITION,
            ExpressionAttributeNames=None,
            ExpressionAttributeValues={":token": lease.token},
        )
    except ConditionalCheckFailed:
        return False
    return True

"""In-memory DynamoDB stub for state-machine protocol tests (HLD §4.4 item 2).

Stores plain dicts keyed by `pk` and evaluates the EXACT ConditionExpression
strings emitted by the `common.state` expression builders (Gate 2 ruled those
exact strings ARE the contract). Update application is a tiny
`SET attr = :val` applier (resolving `#st` via ExpressionAttributeNames)
covering the builder update shapes — nothing more.

Evaluator vocabulary (exactly the builder vocabulary — NOT a general
expression parser):

* ``attribute_not_exists(pk)`` — establish (a) first write
* ``last_seen_sha = :seen_at_read`` — superseded-event observation (HLD §3.3
  step 1, US3.AC1; builder `common.state.build_advance_last_seen_expressions`;
  equality with the read value, so a concurrent accept fails the guard and
  retries instead of dragging `last_seen_sha` backward)
* ``last_seen_sha = :sha`` — establish (b) idempotent equality
* ``generation = :expected_gen`` — establish (c) live-head confirm
* ``head_sha = :reviewed AND generation = :gen AND (claim_until < :now OR
  attribute_not_exists(claim_owner))`` — claim (HLD §3.3 step 3 verbatim)
* ``head_sha = :reviewed AND generation = :gen AND (claim_until < :now OR
  attribute_not_exists(claim_owner) OR claim_owner = :owner) AND
  attribute_not_exists(comment_id)`` — creation lease (HLD §3.4; the
  `claim_owner = :owner` disjunct is the flagged self-holder reading
  documented in `common.reconcile`)
* ``head_sha = :reviewed AND generation = :gen AND comment_id = :dead`` —
  dead-id clear (HLD §2.3 item 8 PATCH-404 row; builder
  `common.state.build_clear_comment_expressions`)
* ``head_sha = :reviewed AND generation = :gen AND claim_owner = :owner`` —
  finalize (HLD §3.3 step 6; SPR-63 owner guard: only the lease holder's
  finalize releases the lease)
* ``attribute_not_exists(pk) OR lease_until < :steal_before`` — HLD-004
  D9 mutex acquire/takeover (T027; missing-`lease_until` compares false)
* ``token = :token`` — HLD-004 D9 mutex refresh/release (T027)

Any other condition (or a non-``SET`` update) raises ValueError loudly: a
builder-string change must break tests, never pass silently.

Port contract (mirrors the boto3 `Table.update_item` kwarg subset that
`common.protocol` uses; production passes a boto3 wrapper translating
`ConditionalCheckFailedException` into `common.protocol.ConditionalCheckFailed`):
`get_item(pk)` returns a copy of the stored dict (None when absent);
`update_item(...)` evaluates the condition, then applies the update, raising
`ConditionalCheckFailed` when the condition does not hold.

An optional shared `log` list records `("get", pk)` / `("update", condition)`
entries so tests assert cross-component ordering (claim update strictly before
fence fetch strictly before publish call) on one timeline together with the
injected review/fence/publish fakes.
"""

import re
from typing import Any

from common.protocol import ConditionalCheckFailed

_CLAIM_CONDITION = (
    "head_sha = :reviewed AND generation = :gen "
    "AND (claim_until < :now OR attribute_not_exists(claim_owner))"
)
_CREATION_LEASE_CONDITION = (
    "head_sha = :reviewed AND generation = :gen "
    "AND (claim_until < :now OR attribute_not_exists(claim_owner) "
    "OR claim_owner = :owner) AND attribute_not_exists(comment_id)"
)
_CLEAR_COMMENT_CONDITION = "head_sha = :reviewed AND generation = :gen AND comment_id = :dead"
_FINALIZE_CONDITION = "head_sha = :reviewed AND generation = :gen AND claim_owner = :owner"
# HLD-004 D9 mutex vocabulary (T027): the mutex row lives in this same
# state table, so the stub evaluates its two conditions exactly —
# steal threshold on acquire, stored-token match on refresh/release.
_MUTEX_ACQUIRE_CONDITION = "attribute_not_exists(pk) OR lease_until < :steal_before"
_MUTEX_TOKEN_CONDITION = "#tok = :token"  # noqa: S105 (expression shape, not a credential)


class InMemoryTable:
    """Minimal DynamoDB-table double: plain dicts keyed by `pk`."""

    def __init__(self, log: list | None = None) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.log: list = log if log is not None else []

    def get_item(self, pk: str) -> dict[str, Any] | None:
        """Return a copy of the stored item (None when absent)."""
        self.log.append(("get", pk))
        item = self.items.get(pk)
        return dict(item) if item is not None else None

    def update_item(
        self,
        *,
        Key: dict[str, Any],
        UpdateExpression: str,
        ConditionExpression: str,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluate the condition, then apply the update (boto3 kwarg subset).

        Note: the unused-names/values check covers UpdateExpression +
        ConditionExpression only, not KeyConditionExpression.
        """
        pk = Key["pk"]
        # Real DynamoDB rejects declared-but-unused expression names/values
        # (ValidationException) — the stub must fail the same way or a
        # builder/caller mismatch passes tests and dies in production.
        expr_text = f"{UpdateExpression} {ConditionExpression}"
        unused_names = set((ExpressionAttributeNames or {}).keys()) - set(
            re.findall(r"#[A-Za-z0-9_]+", expr_text)
        )
        if unused_names:
            raise ValueError(
                f"ExpressionAttributeNames declares unused keys: {sorted(unused_names)}"
            )
        unused_values = set((ExpressionAttributeValues or {}).keys()) - set(
            re.findall(r":[A-Za-z0-9_]+", expr_text)
        )
        if unused_values:
            raise ValueError(
                f"ExpressionAttributeValues declares unused keys: {sorted(unused_values)}"
            )
        self.log.append(("update", ConditionExpression))
        current = self.items.get(pk)
        values = ExpressionAttributeValues or {}
        if not _condition_holds(ConditionExpression, current, values):
            raise ConditionalCheckFailed(f"condition not met: {ConditionExpression} (pk={pk})")
        self.items[pk] = _apply_update(
            current, pk, UpdateExpression, ExpressionAttributeNames or {}, values
        )
        return {"Attributes": dict(self.items[pk])}

    def delete_item(
        self,
        *,
        Key: dict[str, Any],
        ConditionExpression: str,
        ExpressionAttributeNames: dict[str, str] | None = None,
        ExpressionAttributeValues: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Conditional row removal (HLD-004 D9 mutex release): same
        declared-but-unused rejection and `ConditionalCheckFailed`
        semantics as `update_item`; a met condition deletes the row."""
        pk = Key["pk"]
        expr_text = f"{ConditionExpression}"
        unused_names = set((ExpressionAttributeNames or {}).keys()) - set(
            re.findall(r"#[A-Za-z0-9_]+", expr_text)
        )
        if unused_names:
            raise ValueError(
                f"ExpressionAttributeNames declares unused keys: {sorted(unused_names)}"
            )
        unused_values = set((ExpressionAttributeValues or {}).keys()) - set(
            re.findall(r":[A-Za-z0-9_]+", expr_text)
        )
        if unused_values:
            raise ValueError(
                f"ExpressionAttributeValues declares unused keys: {sorted(unused_values)}"
            )
        self.log.append(("delete", ConditionExpression))
        current = self.items.get(pk)
        values = ExpressionAttributeValues or {}
        if not _condition_holds(ConditionExpression, current, values):
            raise ConditionalCheckFailed(f"condition not met: {ConditionExpression} (pk={pk})")
        del self.items[pk]
        return {}


def _condition_holds(
    condition: str, current: dict[str, Any] | None, values: dict[str, Any]
) -> bool:
    """Exact-match dispatch over the builder vocabulary (no general parser)."""
    if condition == "attribute_not_exists(pk)":
        return current is None
    if condition == "last_seen_sha = :seen_at_read":
        return current is not None and current.get("last_seen_sha") == values[":seen_at_read"]
    if condition == "last_seen_sha = :sha":
        return current is not None and current.get("last_seen_sha") == values[":sha"]
    if condition == "generation = :expected_gen":
        return current is not None and current.get("generation") == values[":expected_gen"]
    if condition == _CLAIM_CONDITION:
        return (
            current is not None
            and current.get("head_sha") == values[":reviewed"]
            and current.get("generation") == values[":gen"]
            and (current.get("claim_until", 0) < values[":now"] or "claim_owner" not in current)
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
    if condition == _CLEAR_COMMENT_CONDITION:
        return (
            current is not None
            and current.get("head_sha") == values[":reviewed"]
            and current.get("generation") == values[":gen"]
            and current.get("comment_id") == values[":dead"]
        )
    if condition == _FINALIZE_CONDITION:
        return (
            current is not None
            and current.get("head_sha") == values[":reviewed"]
            and current.get("generation") == values[":gen"]
            and current.get("claim_owner") == values[":owner"]
        )
    if condition == _MUTEX_ACQUIRE_CONDITION:
        # HLD-004 D9 mutex acquire/takeover: missing row steals via arm 1;
        # a present row needs arm 2 with a PRESENT lease_until (a
        # comparison against a missing attribute is false in DynamoDB).
        return current is None or (
            current.get("lease_until") is not None
            and current["lease_until"] < values[":steal_before"]
        )
    if condition == _MUTEX_TOKEN_CONDITION:
        # Mutex refresh/release: row present with the stored token.
        return current is not None and current.get("token") == values[":token"]
    raise ValueError(f"unsupported condition expression: {condition!r}")


def _apply_update(
    current: dict[str, Any] | None,
    pk: str,
    update: str,
    names: dict[str, str],
    values: dict[str, Any],
) -> dict[str, Any]:
    """Tiny `SET ... [REMOVE ...]` / `REMOVE ...` applier for the builder
    shapes (REMOVE of a non-existent attribute is a no-op, as in real
    DynamoDB)."""
    if update.startswith("REMOVE ") and " = " not in update:
        item = dict(current) if current is not None else {"pk": pk}
        for attr in update[len("REMOVE ") :].split(", "):
            if not attr:
                raise ValueError(f"unsupported remove clause: {attr!r}")
            item.pop(names.get(attr, attr), None)
        return item
    if not update.startswith("SET "):
        raise ValueError(f"unsupported update expression: {update!r}")
    set_part, remove_sep, remove_part = update[4:].partition(" REMOVE ")
    item = dict(current) if current is not None else {"pk": pk}
    for clause in set_part.split(", "):
        attr, sep, placeholder = clause.partition(" = ")
        if not sep or not placeholder.startswith(":"):
            raise ValueError(f"unsupported update clause: {clause!r}")
        if placeholder not in values:
            raise ValueError(f"missing value for {placeholder!r}")
        item[names.get(attr, attr)] = values[placeholder]
    if remove_sep:
        for attr in remove_part.split(", "):
            if not attr or " = " in attr:
                raise ValueError(f"unsupported remove clause: {attr!r}")
            item.pop(names.get(attr, attr), None)
    return item

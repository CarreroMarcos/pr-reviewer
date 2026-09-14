"""Review-state codec + conditional-update expression builders (HLD §3.1, §3.3).

Field contract (HLD §3.1): stored `status` ∈ {CLAIMED, ACTIVE} — ABSENT is
modeled by item absence and STALE is derived (`claim_until` < now), neither
is ever written; `generation` starts at 0 and is monotone non-decreasing per
key; `head_sha`/`last_seen_sha` are 40-char lowercase hex; `claim_until` is
epoch seconds; `claim_owner` is the raw delivery GUID (no parsing);
`comment_id` is the GitHub int64 comment ID, present only in ACTIVE;
`updated_at` is ISO-8601 UTC; `pk` is `review:{repo_full_name}#{pr_number}`.

Items are plain dicts of native str/int (boto3 JSON/document-client
semantics per HLD §2.4 — no attribute-value typing here). Malformed input
raises a typed `StateError` (a `ValueError`) carrying a machine-readable
`field` and `reason`, mirroring `common.envelope.EnvelopeError`.

Expression builders are pure functions returning
(update_expression, condition_expression, expression_attribute_values)
tuples as plain strings/dicts; the DB client arrives later via injected
callers. Builders that SET `status` spell it `#st` (a DynamoDB reserved
word) — callers pass `expression_names(...)` alongside (DynamoDB rejects
declared-but-unused names, so the helper emits exactly the referenced
subset).

Pure stdlib, no I/O, no boto3 import.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

STATUSES = frozenset({"CLAIMED", "ACTIVE"})

CLAIM_LEASE_SECONDS = 180  # HLD §3.2 / §7.2: fixed lease, claim → finalize
DELIVERY_TTL_SECONDS = 7 * 24 * 3600  # HLD §2.4 item type 1: delivery TTL 7 days

INT64_MAX = 2**63 - 1

# `status` is a DynamoDB reserved word: expression builders alias it as `#st`.
EXPRESSION_ATTRIBUTE_NAMES = {"#st": "status"}


def expression_names(*expressions: str | None) -> dict[str, str] | None:
    """Names map actually referenced by the given update/condition
    expressions; `None` when none are. DynamoDB rejects BOTH extremes:
    declared-but-unused names (`ValidationException … unused … {#st}`) and
    an empty map (`ExpressionAttributeNames must not be empty`) — both
    surfaced live by the T035 acceptance run — so callers pass this
    helper's result straight through."""
    used = set(re.findall(r"#[A-Za-z0-9_]+", " ".join(e for e in expressions if e)))
    names = {k: v for k, v in EXPRESSION_ATTRIBUTE_NAMES.items() if k in used}
    return names or None


_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
_PK_RE = re.compile(r"^review:[^#]+#[0-9]+\Z")

ExprTriple = tuple[str, str, dict[str, Any]]


class StateError(ValueError):
    """Typed state rejection: `field` names the offending field
    (`"item"` for whole-item shape errors), `reason` is a
    machine-readable code (`missing`, `not_object`, `bad_pk`,
    `bad_status`, `bad_generation`, `bad_sha`, `bad_claim_until`,
    `bad_claim_owner`, `bad_comment_id`, `unexpected_comment_id`,
    `missing_comment_id`, `bad_updated_at`)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid state: {field}: {reason}")


@dataclass(frozen=True)
class ReviewState:
    """Validated review-state item — HLD §3.1 field contract."""

    pk: str
    status: str
    generation: int
    head_sha: str
    last_seen_sha: str
    claim_owner: str
    claim_until: int
    updated_at: str
    comment_id: int | None = None

    def __post_init__(self) -> None:
        _check_pk(self.pk)
        _check_status(self.status)
        _check_generation(self.generation)
        _check_sha("head_sha", self.head_sha)
        _check_sha("last_seen_sha", self.last_seen_sha)
        _check_claim_owner(self.claim_owner)
        _check_claim_until(self.claim_until)
        _check_updated_at(self.updated_at)
        _check_comment_id(self.status, self.comment_id)


def _check_pk(value: Any) -> None:
    if not isinstance(value, str) or not _PK_RE.match(value):
        raise StateError("pk", "bad_pk")


def _check_status(value: Any) -> None:
    if not isinstance(value, str) or value not in STATUSES:
        raise StateError("status", "bad_status")


def _check_generation(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateError("generation", "bad_generation")


def _check_sha(field: str, value: Any) -> None:
    if not isinstance(value, str) or not _SHA_RE.match(value):
        raise StateError(field, "bad_sha")


def _check_claim_owner(value: Any) -> None:
    # Raw delivery GUID: preserved byte-for-byte, never parsed or reformatted.
    if not isinstance(value, str):
        raise StateError("claim_owner", "bad_claim_owner")


def _check_claim_until(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateError("claim_until", "bad_claim_until")


def _check_comment_id(status: str, value: Any) -> None:
    if status == "CLAIMED":
        if value is not None:
            raise StateError("comment_id", "unexpected_comment_id")
        return
    # ACTIVE: comment_id is required, a positive int64 GitHub comment ID.
    if value is None:
        raise StateError("comment_id", "missing_comment_id")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= INT64_MAX:
        raise StateError("comment_id", "bad_comment_id")


def _check_updated_at(value: Any) -> None:
    if not isinstance(value, str):
        raise StateError("updated_at", "bad_updated_at")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise StateError("updated_at", "bad_updated_at") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise StateError("updated_at", "bad_updated_at")


def review_pk(repo_full_name: str, pr_number: int) -> str:
    """Build the review-item partition key exactly (HLD §3.1)."""
    return f"review:{repo_full_name}#{pr_number}"


def delivery_pk(guid: str) -> str:
    """Build the delivery-item partition key (HLD §2.4 item type 1)."""
    return f"delivery:{guid}"


def delivery_ttl(now: int) -> int:
    """Delivery-item expiry: epoch seconds 7 days after `now`."""
    return now + DELIVERY_TTL_SECONDS


def build_delivery_item(guid: str, now: int) -> dict[str, Any]:
    """Delivery dedup item shape: pk + 7-day TTL (HLD §2.4, data-model.md §2)."""
    return {"pk": delivery_pk(guid), "ttl": delivery_ttl(now)}


def to_item(state: ReviewState) -> dict[str, Any]:
    """Serialize a validated `ReviewState` to its DynamoDB plain-dict shape."""
    item: dict[str, Any] = {
        "pk": state.pk,
        "status": state.status,
        "generation": state.generation,
        "head_sha": state.head_sha,
        "last_seen_sha": state.last_seen_sha,
        "claim_owner": state.claim_owner,
        "claim_until": state.claim_until,
        "updated_at": state.updated_at,
    }
    if state.comment_id is not None:
        item["comment_id"] = state.comment_id
    return item


def _require_field(item: dict[str, Any], name: str) -> Any:
    if name not in item:
        raise StateError(name, "missing")
    return item[name]


def from_item(item: Any) -> ReviewState:
    """Parse a DynamoDB plain-dict shape into a validated `ReviewState`.

    Unknown extra fields are ignored (additive evolution). `ABSENT` is
    represented by item absence (never passed here); `STALE` is derived via
    `is_stale`, never stored — a stored `"STALE"` status is rejected.
    """
    if not isinstance(item, dict):
        raise StateError("item", "not_object")
    return ReviewState(
        pk=_require_field(item, "pk"),
        status=_require_field(item, "status"),
        generation=_require_field(item, "generation"),
        head_sha=_require_field(item, "head_sha"),
        last_seen_sha=_require_field(item, "last_seen_sha"),
        claim_owner=_require_field(item, "claim_owner"),
        claim_until=_require_field(item, "claim_until"),
        updated_at=_require_field(item, "updated_at"),
        comment_id=item.get("comment_id"),
    )


def is_stale(claim_until: int, now: int) -> bool:
    """STALE derivation (HLD §3.1): a CLAIMED record whose `claim_until`
    has passed is treated as stale and re-claimable."""
    return claim_until < now


def build_establish_first_write(state: ReviewState) -> ExprTriple:
    """Establish (a): first write — unconditional on content, guarded only
    on record absence (HLD §3.3 step 1a)."""
    update = (
        "SET head_sha = :head, last_seen_sha = :seen, generation = :gen, "
        "#st = :status, claim_owner = :owner, claim_until = :until, "
        "updated_at = :updated_at"
    )
    condition = "attribute_not_exists(pk)"
    values: dict[str, Any] = {
        ":head": state.head_sha,
        ":seen": state.last_seen_sha,
        ":gen": state.generation,
        ":status": state.status,
        ":owner": state.claim_owner,
        ":until": state.claim_until,
        ":updated_at": state.updated_at,
    }
    return update, condition, values


def build_establish_equality(*, incoming_sha: str, updated_at: str) -> ExprTriple:
    """Establish (b): idempotent re-delivery — the incoming SHA equals the
    stored `last_seen_sha` (HLD §3.3 step 1b)."""
    update = "SET last_seen_sha = :sha, updated_at = :updated_at"
    condition = "last_seen_sha = :sha"
    values: dict[str, Any] = {":sha": incoming_sha, ":updated_at": updated_at}
    return update, condition, values


def build_establish_confirm(
    *,
    incoming_sha: str,
    expected_generation: int,
    claim_owner: str,
    claim_until: int,
    updated_at: str,
) -> ExprTriple:
    """Establish (c): the caller confirmed out-of-band via a live GitHub
    head fetch that the incoming SHA is the PR's current head (HLD §3.3
    step 1c) — `generation` increments. The write is guarded on
    `generation = :expected_gen` so two concurrent establishes cannot both
    increment from the same base; the loser re-reads and retries.
    `REMOVE comment_id` keeps HLD 3.1's invariant — comment_id exists only
    on ACTIVE records, so the ACTIVE -> CLAIMED transition clears it (a
    no-op when the attribute is absent)."""
    next_gen = expected_generation + 1
    update = (
        "SET head_sha = :head, last_seen_sha = :head, generation = :next_gen, "
        "#st = :status, claim_owner = :owner, claim_until = :until, "
        "updated_at = :updated_at REMOVE comment_id"
    )
    condition = "generation = :expected_gen"
    values: dict[str, Any] = {
        ":head": incoming_sha,
        ":expected_gen": expected_generation,
        ":next_gen": next_gen,
        ":status": "CLAIMED",
        ":owner": claim_owner,
        ":until": claim_until,
        ":updated_at": updated_at,
    }
    return update, condition, values


def build_claim_expressions(
    *,
    head_sha: str,
    generation: int,
    claim_owner: str,
    claim_until: int,
    now: int,
) -> ExprTriple:
    """Claim (HLD §3.3 step 3): take/refresh the lease. Condition is the
    HLD wording verbatim."""
    update = "SET claim_owner = :owner, claim_until = :until"
    condition = (
        "head_sha = :reviewed AND generation = :gen "
        "AND (claim_until < :now OR attribute_not_exists(claim_owner))"
    )
    values: dict[str, Any] = {
        ":reviewed": head_sha,
        ":gen": generation,
        ":now": now,
        ":owner": claim_owner,
        ":until": claim_until,
    }
    return update, condition, values


def build_finalize_expressions(
    *,
    head_sha: str,
    generation: int,
    comment_id: int,
    updated_at: str,
) -> ExprTriple:
    """Finalize (HLD §3.3 step 6): revision only — the lease is not
    re-checked. Failure means a newer accepted revision landed concurrently:
    log and reconcile rather than overwrite."""
    # Lease lifecycle ends AT finalize (HLD §3.2: "held only from claim
    # through finalize"). Leaving it set kept every same-SHA redelivery —
    # e.g. a quick reopen, acceptance (k) — DISCARDED_CLAIM_HELD for the
    # full 180s (surfaced live by the T035 acceptance run).
    update = (
        "SET #st = :status, comment_id = :comment, updated_at = :updated_at "
        "REMOVE claim_owner, claim_until"
    )
    condition = "head_sha = :reviewed AND generation = :gen"
    values: dict[str, Any] = {
        ":reviewed": head_sha,
        ":gen": generation,
        ":status": "ACTIVE",
        ":comment": comment_id,
        ":updated_at": updated_at,
    }
    return update, condition, values

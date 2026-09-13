"""Fenced publication executor (HLD §3.3, exact order).

Runs establish → review(callback) → claim → fence(callback) → publish(callback)
→ conditional finalize, composing ONLY the merged `common.state` expression
builders (establish (a)/(b)/(c), claim, finalize). Any conditional mismatch
yields a typed discard outcome — the stored record is never overwritten.

Injected ports (stdlib only; no boto3 import here):

* `table` — DynamoDB-facing port. Must provide `get_item(pk) -> dict | None`
  (copy semantics; None when absent) and `update_item(Key=...,
  UpdateExpression=..., ConditionExpression=...,
  ExpressionAttributeNames=...,
  ExpressionAttributeValues=...)`, raising `ConditionalCheckFailed` when the
  condition does not hold. The state-machine tests inject the in-memory stub;
  production injects a thin boto3 wrapper translating
  `ConditionalCheckFailedException` into `ConditionalCheckFailed`.
* `now` — clock returning epoch seconds (tests inject a fixed fake).
* `fence` — live-head fetch returning the PR's current head SHA. Used both for
  establish-(c) live confirmation (step 1c) and for the step-4 fence, which the
  executor calls strictly after a successful claim and strictly before publish.
* `review` — review callback returning publishable content (opaque passthrough).
* `publish` — publish callback taking the content, returning the comment id.

Establish re-read/retry loop: when an establish (a)/(b)/(c) write fails its
guard (a concurrent writer moved first), the executor re-reads the item,
re-runs the live fetch, and retries at the new base — a same-SHA race
converges via (b) equality; a different-SHA loser whose incoming SHA is no
longer live discards as superseded. The loop is bounded by
`max_establish_attempts` (default 3); exhaustion discards as stale (a live
concurrent writer owns the record — never overwrite, never hang).

Claim, fence, and finalize are single-shot: no retry, just a typed outcome.

Flagged interpretations for the review gate:

* Self-held lease after establish: establish (a)/(c) grants the caller a live
  lease, so the verbatim step-3 claim condition (`claim_until < :now OR
  attribute_not_exists(claim_owner)`) cannot hold for the establisher. The
  executor still attempts the claim (the attempt is the logged ordering
  point); on failure it re-reads, and when head+generation still match and
  `claim_owner` is still self, exclusivity already holds and it proceeds to
  the fence. Any other failure (moved head/generation → stale; live lease
  held by another owner → claim-held) discards. HLD §3.3 does not name the
  self-claim case; this reading preserves the exact step order and the
  never-overwrite rule.
* Establish (b) is a true no-op write: the conditional guard
  (`last_seen_sha = :sha`) is evaluated, but the written values are the stored
  ones, so an idempotent re-delivery mutates nothing. This keeps the
  mismatch-discards-leave-the-record-unchanged invariant on paths that
  establish (b) and then discard at claim.
* Fence mismatch after claim leaves the record CLAIMED (no release write):
  HLD §3.3 step 4 says only "Mismatch → discard as stale"; the 180 s lease
  expiry (HLD §3.2) is the recovery path.
* A superseded establish performs NO write (the record is left unchanged):
  `last_seen_sha` advances on the accepted (a)/(b)/(c) paths; recording it on
  rejected events is stale-path (T043) scope, not this executor's.
* Stale `comment_id` is cleared on re-establish: establish (c) emits
  `REMOVE comment_id` (HLD §3.1 — `comment_id` is present only on ACTIVE
  records), so the ACTIVE → CLAIMED transition leaves the record decodable
  by `from_item`. The executor never reads `comment_id`; finalize sets it
  on success.

Pure stdlib, no I/O, no boto3 import.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from common.state import (
    CLAIM_LEASE_SECONDS,
    EXPRESSION_ATTRIBUTE_NAMES,
    ReviewState,
    build_claim_expressions,
    build_establish_confirm,
    build_establish_equality,
    build_establish_first_write,
    build_finalize_expressions,
)

DEFAULT_MAX_ESTABLISH_ATTEMPTS = 3


class ConditionalCheckFailed(Exception):
    """Table-port signal: a condition expression did not hold.

    The in-memory test stub raises this directly; the production boto3 wrapper
    translates `ConditionalCheckFailedException` into it.
    """


class OutcomeKind(StrEnum):
    """Typed executor outcomes — branches are values, never exceptions."""

    PUBLISHED = "published"
    PUBLISHED_FINALIZE_CONFLICT = "published_finalize_conflict"
    DISCARDED_SUPERSEDED = "discarded_superseded"
    DISCARDED_STALE = "discarded_stale"
    DISCARDED_CLAIM_HELD = "discarded_claim_held"


@dataclass(frozen=True)
class Outcome:
    """Executor result: `kind` plus the revision it applies to.

    `comment_id` is set for both published variants (the conflict variant
    retains it for the log-and-reconcile path); `generation` is None only when
    no record was ever observed.
    """

    kind: OutcomeKind
    head_sha: str | None = None
    generation: int | None = None
    comment_id: int | None = None


def _iso8601(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_review(
    *,
    pk: str,
    incoming_sha: str,
    owner: str,
    table: Any,
    now: Callable[[], int],
    review: Callable[[], Any],
    fence: Callable[[], str],
    publish: Callable[[Any], int],
    max_establish_attempts: int = DEFAULT_MAX_ESTABLISH_ATTEMPTS,
) -> Outcome:
    """Execute the HLD §3.3 fenced publication protocol for one delivery.

    `owner` is the raw delivery GUID (becomes `claim_owner`). Returns an
    `Outcome`; conditional mismatches discard, never overwrite.
    """
    established = _establish(
        pk=pk,
        incoming_sha=incoming_sha,
        owner=owner,
        table=table,
        now=now,
        fence=fence,
        max_attempts=max_establish_attempts,
    )
    if isinstance(established, Outcome):
        return established
    head_sha, generation = established

    content = review()

    now_int = now()
    update, condition, values = build_claim_expressions(
        head_sha=head_sha,
        generation=generation,
        claim_owner=owner,
        claim_until=now_int + CLAIM_LEASE_SECONDS,
        now=now_int,
    )
    try:
        table.update_item(
            Key={"pk": pk},
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames=EXPRESSION_ATTRIBUTE_NAMES,
            ExpressionAttributeValues=values,
        )
    except ConditionalCheckFailed:
        resolved = _resolve_claim_failure(table, pk, head_sha, generation, owner)
        if resolved is not None:
            return resolved
        # Self still holds head+generation: exclusivity already holds
        # (flagged interpretation — see module docstring). Fall through to
        # the fence, strictly after this claim attempt.

    if fence() != head_sha:
        # Fence mismatch → discard as stale. Publish is never called and the
        # record is left claimed (lease expiry recovers it).
        return Outcome(kind=OutcomeKind.DISCARDED_STALE, head_sha=head_sha, generation=generation)

    comment_id = publish(content)

    update, condition, values = build_finalize_expressions(
        head_sha=head_sha,
        generation=generation,
        comment_id=comment_id,
        updated_at=_iso8601(now()),
    )
    try:
        table.update_item(
            Key={"pk": pk},
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames=EXPRESSION_ATTRIBUTE_NAMES,
            ExpressionAttributeValues=values,
        )
    except ConditionalCheckFailed:
        # A newer accepted revision landed concurrently: log-and-reconcile
        # (comment_id retained) rather than overwrite.
        return Outcome(
            kind=OutcomeKind.PUBLISHED_FINALIZE_CONFLICT,
            head_sha=head_sha,
            generation=generation,
            comment_id=comment_id,
        )
    return Outcome(
        kind=OutcomeKind.PUBLISHED,
        head_sha=head_sha,
        generation=generation,
        comment_id=comment_id,
    )


def _establish(
    *,
    pk: str,
    incoming_sha: str,
    owner: str,
    table: Any,
    now: Callable[[], int],
    fence: Callable[[], str],
    max_attempts: int,
) -> tuple[str, int] | Outcome:
    """Establish step (HLD §3.3 step 1): success returns (head_sha, generation).

    Failure returns an `Outcome`: superseded when the incoming SHA is not the
    live head (no write performed), stale when the bounded re-read/retry loop
    exhausts (a concurrent writer owns the record — never overwrite).
    """
    last_item: dict[str, Any] | None = None
    for _ in range(max_attempts):
        item = table.get_item(pk)
        last_item = item
        try:
            if item is None:
                return _establish_first_write(pk, incoming_sha, owner, table, now)
            if incoming_sha == item.get("last_seen_sha"):
                return _establish_equality(pk, incoming_sha, item, table, now)
            if fence() != incoming_sha:
                return Outcome(
                    kind=OutcomeKind.DISCARDED_SUPERSEDED,
                    head_sha=incoming_sha,
                    generation=item.get("generation"),
                )
            return _establish_confirm(pk, incoming_sha, owner, item, table, now)
        except ConditionalCheckFailed:
            # Concurrent writer moved first: re-read, re-fetch, retry at the
            # new base (same-SHA converges via equality above).
            continue
    return Outcome(
        kind=OutcomeKind.DISCARDED_STALE,
        head_sha=incoming_sha,
        generation=last_item.get("generation") if last_item is not None else None,
    )


def _establish_first_write(
    pk: str, incoming_sha: str, owner: str, table: Any, now: Callable[[], int]
) -> tuple[str, int]:
    now_int = now()
    state = ReviewState(
        pk=pk,
        status="CLAIMED",
        generation=0,
        head_sha=incoming_sha,
        last_seen_sha=incoming_sha,
        claim_owner=owner,
        claim_until=now_int + CLAIM_LEASE_SECONDS,
        updated_at=_iso8601(now_int),
    )
    update, condition, values = build_establish_first_write(state)
    table.update_item(
        Key={"pk": pk},
        UpdateExpression=update,
        ConditionExpression=condition,
        ExpressionAttributeNames=EXPRESSION_ATTRIBUTE_NAMES,
        ExpressionAttributeValues=values,
    )
    return incoming_sha, 0


def _establish_equality(
    pk: str, incoming_sha: str, item: dict[str, Any], table: Any, now: Callable[[], int]
) -> tuple[str, int]:
    # Idempotent re-delivery: evaluate the (b) guard but write back the stored
    # values, so the record is byte-identical afterwards (flagged — see module
    # docstring). Fall back to the current clock only if the stored item
    # predates the `updated_at` contract (never true for codec-written items).
    update, condition, values = build_establish_equality(
        incoming_sha=incoming_sha,
        updated_at=item.get("updated_at") or _iso8601(now()),
    )
    table.update_item(
        Key={"pk": pk},
        UpdateExpression=update,
        ConditionExpression=condition,
        ExpressionAttributeNames=EXPRESSION_ATTRIBUTE_NAMES,
        ExpressionAttributeValues=values,
    )
    return item["head_sha"], item["generation"]


def _establish_confirm(
    pk: str, incoming_sha: str, owner: str, item: dict[str, Any], table: Any, now: Callable[[], int]
) -> tuple[str, int]:
    expected = item["generation"]
    now_int = now()
    update, condition, values = build_establish_confirm(
        incoming_sha=incoming_sha,
        expected_generation=expected,
        claim_owner=owner,
        claim_until=now_int + CLAIM_LEASE_SECONDS,
        updated_at=_iso8601(now_int),
    )
    table.update_item(
        Key={"pk": pk},
        UpdateExpression=update,
        ConditionExpression=condition,
        ExpressionAttributeNames=EXPRESSION_ATTRIBUTE_NAMES,
        ExpressionAttributeValues=values,
    )
    return incoming_sha, expected + 1


def _resolve_claim_failure(
    table: Any, pk: str, head_sha: str, generation: int, owner: str
) -> Outcome | None:
    """Classify a failed claim: an `Outcome` to discard, or None to proceed.

    None (proceed to fence) applies only when the re-read shows head and
    generation unmoved and the lease still held by the caller — the establish
    grant already gives exclusivity (flagged interpretation). A failure with a
    live lease held by another owner can only follow the (b) path (a)/(c)
    always grant self), so no overwrite is possible on any branch.
    """
    item = table.get_item(pk)
    if item is None or item.get("head_sha") != head_sha or item.get("generation") != generation:
        return Outcome(kind=OutcomeKind.DISCARDED_STALE, head_sha=head_sha, generation=generation)
    if item.get("claim_owner") == owner:
        return None
    return Outcome(kind=OutcomeKind.DISCARDED_CLAIM_HELD, head_sha=head_sha, generation=generation)

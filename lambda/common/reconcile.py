"""Comment reconciliation (HLD §3.4; FR-003; US2.AC3).

Converges the PR to exactly one canonical marker-bearing comment carrying
the caller's fresh `content`, returning its comment id. One bounded call:

1. List comments **fully paginated** before matching; every page and every
   entry is shape-validated. An unparseable list raises
   `ReconcileError("list_unreadable")` — the list-unreadable row of HLD
   §2.3 item 8, non-retryable (the worker completes, never spins).
2. Find all comments bearing the **exact** worker-injected marker string
   (`common.marker.build_marker` — substring match on the exact string, so
   another PR's marker never matches). Exactly one → adopt; multiple →
   deterministically the lowest comment id wins and extras are deleted.
3. The adopted winner is PATCHed with the fresh content (a review that
   reconciles but drops its content would report "published" while losing
   the review). A vanished winner (`CommentNotFound`) re-runs the listing
   round — bounded by `max_rounds`, exhaustion raises
   `ReconcileError("reconcile_contention")`, non-retryable.
4. None found → **creation lease**: a conditional write on the review
   record requiring `attribute_not_exists(comment_id)` plus the claim
   condition of HLD §3.3 step 3 — two concurrent first-posters cannot both
   POST; the loser re-runs reconciliation and adopts the winner's comment.
   Lease won → POST → persist → re-check (re-list; stray marker comments
   deleted). Lease lost → next round (re-list, adopt the winner).

Flagged interpretation (for the review gate): the verbatim §3.3 step-3
claim condition (`claim_until < :now OR attribute_not_exists(claim_owner)`)
can never hold for the publisher itself — the fenced protocol claims (live
self lease) strictly before publish. A verbatim lease would therefore be
unacquirable in every worker flow and reconciliation could never POST. The
lease condition admits one further disjunct, `claim_owner = :owner` ("I
already hold the lease from my claim"), preserving the HLD mutual-exclusion
property — a DISTINCT owner under a live lease still fails every branch —
while making the mechanism operable. The condition string is:

    head_sha = :reviewed AND generation = :gen
    AND (claim_until < :now OR attribute_not_exists(claim_owner)
         OR claim_owner = :owner)
    AND attribute_not_exists(comment_id)

Adoption persistence reuses the §3.3 step-6 finalize expressions
(`common.state.build_finalize_expressions`): revision-guarded, lease
untouched. A lost persist race (newer revision landed concurrently)
returns the id anyway — the protocol-level finalize then takes the
log-and-reconcile path rather than overwriting.

Absent record (DynamoDB state lost — the marker survives in GitHub per
§2.8): no lease is attempted and nothing is persisted; a found winner is
still PATCHed, otherwise the content is POSTed directly. The next fenced
run re-establishes state and converges.

Transport errors from the injected ports propagate untouched — the caller
classifies them (HLD §2.3 item 8: list 403/404 complete, 429/5xx retry).
Only shape failures become `ReconcileError`. The caller maps a provider
404 on PATCH to `CommentNotFound` and a provider 404 on DELETE to silence
(already converged); any other provider failure propagates.

Pure stdlib, no I/O, no boto3 import.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from common.marker import build_marker
from common.protocol import ConditionalCheckFailed
from common.state import (
    CLAIM_LEASE_SECONDS,
    build_claim_expressions,
    build_finalize_expressions,
    expression_names,
)

PER_PAGE = 100
MAX_PAGES = 10
DEFAULT_MAX_ROUNDS = 3


class ReconcileError(Exception):
    """Typed reconciliation failure — always non-retryable (the worker
    completes: `list_unreadable`, `reconcile_contention`)."""

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(f"reconcile failed: {error_class}")


class CommentNotFound(Exception):
    """A PATCH/established target vanished (provider 404 mapped here by the
    caller): the current round is stale, re-list and converge."""

    def __init__(self, comment_id: int) -> None:
        self.comment_id = comment_id
        super().__init__(f"comment vanished: {comment_id}")


def _iso8601(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_page(payload: Any) -> list[dict[str, Any]]:
    """Shape-validate one listed page: a list of `{id: int ≥ 1, body: str}`.

    Anything else — non-list top level, missing/wrong-typed fields, boolean
    or non-positive ids — is an unparseable list (HLD §3.4: treated as the
    list-unreadable row, non-retryable). Entries are never skipped
    silently: one bad entry poisons the listing.
    """
    if not isinstance(payload, list):
        raise ReconcileError("list_unreadable")
    for entry in payload:
        if not isinstance(entry, dict):
            raise ReconcileError("list_unreadable")
        comment_id = entry.get("id")
        if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id < 1:
            raise ReconcileError("list_unreadable")
        if not isinstance(entry.get("body"), str):
            raise ReconcileError("list_unreadable")
    return payload


def _list_all(list_page: Callable[[int], Any]) -> list[dict[str, Any]]:
    """Fully-paginated, shape-validated listing (HLD §3.4).

    A full page implies more pages; a short page ends the listing. Bounded
    at `MAX_PAGES` — PRs beyond 1000 comments are pathological, and an
    unbounded crawl would blow the worker budget.
    """
    comments: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        items = _validate_page(list_page(page))
        comments.extend(items)
        if len(items) < PER_PAGE:
            break
    return comments


def _persist(table: Any, pk: str, comment_id: int, now: Callable[[], int]) -> bool:
    """Persist the adopted/created id via the revision-guarded finalize
    expressions. False when no record exists (state lost — §2.8 marker
    survival) or a newer revision owns the record (the protocol-level
    finalize then takes the conflict path); never raises for races."""
    item = table.get_item(pk)
    if item is None:
        return False
    update, condition, values = build_finalize_expressions(
        head_sha=item["head_sha"],
        generation=item["generation"],
        comment_id=comment_id,
        updated_at=_iso8601(now()),
    )
    try:
        table.update_item(
            Key={"pk": pk},
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames=expression_names(update, condition),
            ExpressionAttributeValues=values,
        )
    except ConditionalCheckFailed:
        return False
    return True


def _try_creation_lease(table: Any, pk: str, owner: str, now: Callable[[], int]) -> bool:
    """Attempt the creation lease; True when won (caller POSTs), False when
    a distinct first-poster holds it (caller re-runs and adopts)."""
    item = table.get_item(pk)
    if item is None:
        return True  # no state to guard — direct POST, marker converges later
    now_int = now()
    update, condition, values = build_claim_expressions(
        head_sha=item["head_sha"],
        generation=item["generation"],
        claim_owner=owner,
        claim_until=now_int + CLAIM_LEASE_SECONDS,
        now=now_int,
    )
    # The verbatim §3.3 step-3 condition cannot hold for the publisher
    # itself (its claim granted a live self lease just before publish), so
    # the OR-group gains the self-holder disjunct — flagged in the module
    # docstring. A DISTINCT owner under a live lease still fails every
    # branch, preserving mutual exclusion.
    condition = condition.replace(
        "(claim_until < :now OR attribute_not_exists(claim_owner))",
        "(claim_until < :now OR attribute_not_exists(claim_owner) OR claim_owner = :owner)",
    )
    condition = f"{condition} AND attribute_not_exists(comment_id)"
    try:
        table.update_item(
            Key={"pk": pk},
            UpdateExpression=update,
            ConditionExpression=condition,
            ExpressionAttributeNames=expression_names(update, condition),
            ExpressionAttributeValues=values,
        )
    except ConditionalCheckFailed:
        return False
    return True


def reconcile(
    *,
    repo_full_name: str,
    pr_number: int,
    pk: str,
    owner: str,
    content: str,
    table: Any,
    now: Callable[[], int],
    list_page: Callable[[int], Any],
    create_comment: Callable[[str], int],
    update_comment: Callable[[int, str], None],
    delete_comment: Callable[[int], None],
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> int:
    """Converge to one marker-bearing comment carrying `content`; return id.

    Ports: `table` is the protocol table port (`get_item`/`update_item`
    raising `ConditionalCheckFailed`); `list_page(page)` returns one parsed
    list-comments page; `create_comment` POSTs; `update_comment` PATCHes
    (raising `CommentNotFound` when the target vanished); `delete_comment`
    DELETEs. Transport faults propagate for the caller to classify; shape
    faults raise `ReconcileError` (non-retryable).
    """
    marker = build_marker(repo_full_name, pr_number)
    for _ in range(max_rounds):
        matches = [c for c in _list_all(list_page) if marker in c["body"]]
        if matches:
            winner = min(c["id"] for c in matches)
            try:
                update_comment(winner, content)
            except CommentNotFound:
                continue  # winner vanished mid-round — re-list and converge
            for comment in matches:
                if comment["id"] != winner:
                    delete_comment(comment["id"])
            _persist(table, pk, winner, now)
            return winner
        if _try_creation_lease(table, pk, owner, now):
            comment_id = create_comment(content)
            _persist(table, pk, comment_id, now)
            # Re-check: a concurrent poster outside the lease (or manual
            # duplication) must not leave a second marker comment behind.
            for comment in _list_all(list_page):
                if marker in comment["body"] and comment["id"] != comment_id:
                    delete_comment(comment["id"])
            return comment_id
        # Lease lost — the winner's comment should be visible on re-list.
    raise ReconcileError("reconcile_contention")

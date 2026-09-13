"""T011: fenced-publication protocol scenarios on an in-memory DynamoDB stub.

Covers HLD §3.3 (exact order: establish → review → claim → fence → publish →
conditional finalize), §3.2 (state model/transitions), and §6 failure modes
6–10. Imports the executor from `common.protocol` (T012); the stub in
`dynamodb_stub.py` evaluates the exact ConditionExpression strings the merged
`common.state` builders emit.

Mapping (§3.3 step / failure mode → test):

* step 1a first write → `test_establish_first_write_creates_claimed_record`,
  `test_first_delivery_publishes_and_finalizes`
* step 1b equality → `test_redelivery_same_sha_is_idempotent`
* step 1c confirm + generation increment → `test_new_sha_confirmed_increments_generation`
* step 1c generation-guard retry loop → `test_establish_retry_same_sha_converges`,
  `test_establish_retry_different_sha_discards_superseded`,
  `test_establish_retry_bound_discards_stale`
* step 3 claim (expired-lease takeover) → `test_claim_succeeds_on_expired_lease`
* step 3 claim (live lease, other owner) → `test_claim_fails_on_live_lease_held`
* step 4 fence-after-claim ordering → `test_fence_after_claim_before_publish`
* step 4 fence mismatch → `test_fence_mismatch_discards_stale_without_publish`
* step 6 finalize success → `test_finalize_success_sets_active_with_comment_id`
* step 6 finalize conflict → `test_finalize_conflict_never_overwrites_newer`
* superseded/stale ⇒ discard, never overwrite → `test_superseded_event_leaves_record`
* mode 6 out-of-order delivery → `test_superseded_event_leaves_record`,
  `test_establish_retry_different_sha_discards_superseded`
* mode 7 read-then-PATCH stale write → `test_fence_mismatch_discards_stale_without_publish`,
  `test_finalize_conflict_never_overwrites_newer`
* mode 8 first-post race (180 s lease) → `test_claim_fails_on_live_lease_held`
* mode 9 worker death mid-claim (expiry + takeover) → `test_claim_succeeds_on_expired_lease`
* mode 10 duplicate comments during recovery → `test_duplicate_delivery_publishes_once`
  (protocol side: claim exclusivity ⇒ exactly one publish; full marker-based
  reconciliation per §3.4 is T039 scope, not the executor's)

All tests are deterministic: a fake fixed clock (`NOW`), scripted live-head
fetches, and review/fence/publish fakes sharing one call log. No sleeps.
"""

import copy

from dynamodb_stub import InMemoryTable

from common.protocol import OutcomeKind, run_review
from common.state import (
    CLAIM_LEASE_SECONDS,
    EXPRESSION_ATTRIBUTE_NAMES,
    ReviewState,
    build_establish_first_write,
)

SHA_A = "aa" * 20
SHA_B = "bb" * 20
SHA_C = "cc" * 20
PK = "review:octo-org/hello-world#42"
GUID_1 = "11111111-1111-4111-8111-111111111111"
GUID_2 = "22222222-2222-4222-8222-222222222222"
GUID_OTHER = "33333333-3333-4333-8333-333333333333"
NOW = 1_750_000_000
COMMENT_ID = 987654
UPDATED_AT = "2026-09-12T10:00:00Z"


class Harness:
    """Table + scripted review/fence/publish fakes sharing one call log."""

    def __init__(
        self,
        *,
        live_shas: list,
        comment_id: int = COMMENT_ID,
        review_body: str = "review-body",
        on_fence=None,
        on_publish=None,
    ) -> None:
        self.calls: list = []
        self.table = InMemoryTable(log=self.calls)
        self._live = list(live_shas)
        self._comment_id = comment_id
        self.review_body = review_body
        self._on_fence = on_fence
        self._on_publish = on_publish

    def review(self):
        self.calls.append(("review",))
        return self.review_body

    def fence(self):
        if len(self._live) > 1:
            sha = self._live.pop(0)
        else:
            sha = self._live[0]
        self.calls.append(("fence", sha))
        if self._on_fence is not None:
            self._on_fence(sha)
        return sha

    def publish(self, content):
        self.calls.append(("publish", content))
        if self._on_publish is not None:
            self._on_publish(content)
        return self._comment_id

    def run(self, **kwargs):
        args = {
            "pk": PK,
            "table": self.table,
            "now": lambda: NOW,
            "review": self.review,
            "fence": self.fence,
            "publish": self.publish,
            "owner": GUID_1,
        }
        args.update(kwargs)
        return run_review(**args)


def _seed(
    table: InMemoryTable,
    *,
    head: str,
    seen: str | None = None,
    gen: int = 0,
    owner: str = GUID_1,
    until: int = NOW + CLAIM_LEASE_SECONDS,
    comment: int | None = None,
    status: str = "ACTIVE",
) -> dict:
    item = {
        "pk": PK,
        "status": status,
        "generation": gen,
        "head_sha": head,
        "last_seen_sha": head if seen is None else seen,
        "claim_owner": owner,
        "claim_until": until,
        "updated_at": UPDATED_AT,
    }
    if comment is not None:
        item["comment_id"] = comment
    table.items[PK] = item
    return copy.deepcopy(item)


def _update_indices(calls: list) -> list:
    return [i for i, entry in enumerate(calls) if entry[0] == "update"]


def _claim_update_index(calls: list) -> int:
    return next(
        i for i, entry in enumerate(calls) if entry[0] == "update" and "claim_until" in entry[1]
    )


def test_establish_first_write_creates_claimed_record():
    """Step 1a on an empty table: establish (a) succeeds; status CLAIMED, gen 0."""
    table = InMemoryTable()
    state = ReviewState(
        pk=PK,
        status="CLAIMED",
        generation=0,
        head_sha=SHA_B,
        last_seen_sha=SHA_B,
        claim_owner=GUID_1,
        claim_until=NOW + CLAIM_LEASE_SECONDS,
        updated_at=UPDATED_AT,
    )
    update, condition, values = build_establish_first_write(state)
    table.update_item(
        Key={"pk": PK},
        UpdateExpression=update,
        ConditionExpression=condition,
        ExpressionAttributeNames=EXPRESSION_ATTRIBUTE_NAMES,
        ExpressionAttributeValues=values,
    )
    item = table.items[PK]
    assert item["status"] == "CLAIMED"  # HLD §3.2 postcondition
    assert item["generation"] == 0
    assert item["head_sha"] == SHA_B
    assert item["last_seen_sha"] == SHA_B


def test_first_delivery_publishes_and_finalizes():
    """Empty table → full path publishes; record ends ACTIVE with comment_id."""
    h = Harness(live_shas=[SHA_B])
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.PUBLISHED
    assert outcome.comment_id == COMMENT_ID
    assert outcome.generation == 0
    item = h.table.items[PK]
    assert item["status"] == "ACTIVE"
    assert item["head_sha"] == SHA_B
    assert item["comment_id"] == COMMENT_ID
    assert item["claim_owner"] == GUID_1


def test_redelivery_same_sha_is_idempotent():
    """Step 1b: same SHA re-delivery succeeds via equality; generation unchanged."""
    h = Harness(live_shas=[SHA_A])
    before = _seed(h.table, head=SHA_A, gen=3, comment=111)
    outcome = h.run(incoming_sha=SHA_A)
    assert outcome.kind == OutcomeKind.PUBLISHED
    assert outcome.generation == 3
    assert h.table.items[PK]["generation"] == before["generation"]
    assert h.table.items[PK]["comment_id"] == COMMENT_ID


def test_new_sha_confirmed_increments_generation():
    """Step 1c: new SHA + live fetch confirms → generation increments by exactly 1."""
    h = Harness(live_shas=[SHA_B, SHA_B])
    _seed(h.table, head=SHA_A, gen=5, comment=111)
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.PUBLISHED
    assert outcome.generation == 6
    item = h.table.items[PK]
    assert item["generation"] == 6
    assert item["head_sha"] == SHA_B
    assert item["status"] == "ACTIVE"


def test_establish_retry_same_sha_converges():
    """Step 1c guard race: a concurrent writer lands the SAME SHA (expired lease)
    → our (c) write fails the generation guard → re-read → converge via (b)."""

    landed: list = []

    def concurrent_writer(_sha):
        # One concurrent landing: the Harness fence hook fires on every live
        # fetch (establish-confirm fetch AND the step-4 fence), but the modeled
        # writer lands once — a second firing would be a second writer.
        if landed:
            return
        landed.append(_sha)
        _seed(
            h.table,
            head=SHA_B,
            gen=6,
            owner=GUID_OTHER,
            until=NOW - 10,  # expired: takeover allowed (mode 9)
            status="CLAIMED",
        )

    h = Harness(live_shas=[SHA_B, SHA_B, SHA_B], on_fence=concurrent_writer)
    _seed(h.table, head=SHA_A, gen=5, comment=111)
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.PUBLISHED
    assert outcome.generation == 6  # converged at the writer's base, no extra bump
    conditions = [entry[1] for entry in h.calls if entry[0] == "update"]
    assert "last_seen_sha = :sha" in conditions  # (b) path taken on retry
    assert h.table.items[PK]["claim_owner"] == GUID_1  # expired lease taken over


def test_establish_retry_different_sha_discards_superseded():
    """Step 1c guard race: a concurrent writer lands a DIFFERENT SHA C (live now C)
    → retry re-confirms → incoming B is not live → discard as superseded."""

    def concurrent_writer(_sha):
        _seed(h.table, head=SHA_C, gen=6, owner=GUID_OTHER, status="CLAIMED")

    h = Harness(live_shas=[SHA_B, SHA_C], on_fence=concurrent_writer)
    _seed(h.table, head=SHA_A, gen=5, comment=111)
    probe = InMemoryTable()
    expected = _seed(probe, head=SHA_C, gen=6, owner=GUID_OTHER, status="CLAIMED")
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.DISCARDED_SUPERSEDED
    assert h.table.items[PK] == expected  # writer's record only; our run wrote nothing
    assert ("review",) not in h.calls  # establish failed before review
    assert not any(entry[0] == "publish" for entry in h.calls)


def test_establish_retry_bound_discards_stale():
    """The establish re-read/retry loop is bounded: perpetual guard failures
    terminate with a safe discard, never an overwrite, never a hang."""

    def churn(_sha):
        current = h.table.items[PK]
        _seed(h.table, head=current["head_sha"], gen=current["generation"] + 1)

    h = Harness(live_shas=[SHA_B], on_fence=churn)
    _seed(h.table, head=SHA_A, gen=0, comment=111)
    outcome = h.run(incoming_sha=SHA_B, max_establish_attempts=2)
    assert outcome.kind == OutcomeKind.DISCARDED_STALE
    assert not any(entry[0] == "publish" for entry in h.calls)


def test_claim_succeeds_on_expired_lease():
    """Step 3 + mode 9: CLAIMED record with an expired lease is re-claimable."""
    h = Harness(live_shas=[SHA_B])
    _seed(h.table, head=SHA_B, gen=2, owner=GUID_OTHER, until=NOW - 5, status="CLAIMED")
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.PUBLISHED
    assert h.table.items[PK]["claim_owner"] == GUID_1
    assert h.table.items[PK]["generation"] == 2  # takeover never bumps generation


def test_claim_fails_on_live_lease_held():
    """Step 3 + mode 8: live lease held by another owner → discard; fence and
    publish never run; the stored record is byte-identical."""
    h = Harness(live_shas=[SHA_B])
    before = _seed(h.table, head=SHA_B, gen=2, owner=GUID_OTHER, until=NOW + 100, status="CLAIMED")
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.DISCARDED_CLAIM_HELD
    assert ("review",) in h.calls  # establish (b) + review precede the claim
    assert not any(entry[0] == "fence" for entry in h.calls)
    assert not any(entry[0] == "publish" for entry in h.calls)
    assert h.table.items[PK] == before


def test_fence_after_claim_before_publish():
    """Step 3→4→5 ordering: fence strictly after a successful claim and strictly
    before publish; finalize follows publish."""
    h = Harness(live_shas=[SHA_B, SHA_B])
    _seed(h.table, head=SHA_A, gen=0, comment=111)
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.PUBLISHED
    review_idx = h.calls.index(("review",))
    claim_idx = _claim_update_index(h.calls)
    # The step-4 fence: the first fence strictly AFTER the claim (an earlier
    # fence entry is the establish-(c) live-confirm fetch, cf. the
    # "confirm fetch only" assertion in test_superseded_event_leaves_record).
    fence_idx = next(i for i, entry in enumerate(h.calls) if entry[0] == "fence" and i > claim_idx)
    publish_idx = next(i for i, entry in enumerate(h.calls) if entry[0] == "publish")
    updates_after_publish = [i for i in _update_indices(h.calls) if i > publish_idx]
    assert review_idx < claim_idx < fence_idx < publish_idx
    assert len(updates_after_publish) == 1  # conditional finalize


def test_fence_mismatch_discards_stale_without_publish():
    """Step 4 + mode 7: a newer push lands between claim and fence → mismatch →
    discard as stale; publish NEVER called; record left claimed (lease expiry
    is the recovery path; §3.3 step 4 names no release write)."""
    h = Harness(live_shas=[SHA_B, SHA_C])
    _seed(h.table, head=SHA_A, gen=4, comment=111)
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.DISCARDED_STALE
    assert not any(entry[0] == "publish" for entry in h.calls)
    item = h.table.items[PK]
    assert item["status"] == "CLAIMED"  # left claimed per §3.3 (no release write)
    assert item["head_sha"] == SHA_B
    assert item["generation"] == 5
    # No builder emits REMOVE and the stub applies SET-merge only, so the
    # prior ACTIVE record's comment_id carries over untouched (never
    # overwritten, never cleared — tensions HLD §3.1 "present only in ACTIVE";
    # flagged for the gate). The fence-mismatch invariant is no-publish.
    assert item.get("comment_id") == 111
    assert h.calls.count(("review",)) == 1


def test_finalize_success_sets_active_with_comment_id():
    """Step 6: matching head+generation finalizes to ACTIVE with the comment_id."""
    h = Harness(live_shas=[SHA_B, SHA_B])
    _seed(h.table, head=SHA_A, gen=7, comment=111)
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.PUBLISHED
    item = h.table.items[PK]
    assert item["status"] == "ACTIVE"
    assert item["comment_id"] == COMMENT_ID
    assert item["head_sha"] == SHA_B
    assert item["generation"] == 8  # revision-only finalize preserves the bump


def test_finalize_conflict_never_overwrites_newer():
    """Step 6 + mode 7: a newer accepted revision lands between publish and
    finalize → log-and-reconcile path (typed outcome carrying comment_id);
    the newer record is never overwritten."""

    def concurrent_landing(_content):
        _seed(h.table, head=SHA_C, gen=6, owner=GUID_OTHER, status="CLAIMED")

    h = Harness(live_shas=[SHA_B, SHA_B], on_publish=concurrent_landing)
    _seed(h.table, head=SHA_A, gen=4, comment=111)
    probe = InMemoryTable()
    expected = _seed(probe, head=SHA_C, gen=6, owner=GUID_OTHER, status="CLAIMED")
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.PUBLISHED_FINALIZE_CONFLICT
    assert outcome.comment_id == COMMENT_ID  # retained for reconciliation
    assert h.table.items[PK] == expected  # newer record intact, never overwritten
    item = h.table.items[PK]
    assert item["head_sha"] == SHA_C  # newer revision intact
    assert item["generation"] == 6
    assert "comment_id" not in item  # finalize never overwrote it


def test_superseded_event_leaves_record():
    """Mode 6: incoming SHA is neither last_seen nor the live head → not
    established; the stored record is unchanged; no review, no publish."""
    h = Harness(live_shas=[SHA_A])
    before = _seed(h.table, head=SHA_A, gen=3, comment=111)
    outcome = h.run(incoming_sha=SHA_B)
    assert outcome.kind == OutcomeKind.DISCARDED_SUPERSEDED
    assert h.table.items[PK] == before
    assert ("review",) not in h.calls
    assert not any(entry[0] == "publish" for entry in h.calls)
    assert sum(1 for entry in h.calls if entry[0] == "fence") == 1  # confirm fetch only


def test_duplicate_delivery_publishes_once():
    """Mode 10 (protocol side): a duplicate delivery of an already-published SHA
    runs establish (b) + review, then fails the live-lease claim → exactly one
    publish total across both runs (claim exclusivity, no double publish)."""
    h = Harness(live_shas=[SHA_B])
    first = h.run(incoming_sha=SHA_B)
    assert first.kind == OutcomeKind.PUBLISHED
    second = h.run(incoming_sha=SHA_B, owner=GUID_2)
    assert second.kind == OutcomeKind.DISCARDED_CLAIM_HELD
    assert sum(1 for entry in h.calls if entry[0] == "publish") == 1
    assert h.table.items[PK]["comment_id"] == COMMENT_ID

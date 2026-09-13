"""T009: review-state item codec round-trip + rejection cases (HLD §3.1).

Covers the §3.1 field contract verbatim: stored `status` ∈ {CLAIMED, ACTIVE}
(ABSENT by absence, STALE derived — never stored), monotone `generation`
from 0, 40-hex SHAs, epoch `claim_until`, raw `claim_owner`, ACTIVE-only
int64 `comment_id`, ISO-8601 UTC `updated_at`, exact `pk` formats, delivery
TTL math (7 days per data-model.md §2), plus the §3.3
conditional-expression builders (establish (a)/(b)/(c), claim, finalize) and
the STALE derivation helper (`claim_until` < now → stale).

Every rejection is typed: tests assert `StateError` plus its
machine-readable `field`/`reason`, mirroring `common.envelope.EnvelopeError`.
"""

import pytest

from common.state import (
    CLAIM_LEASE_SECONDS,
    DELIVERY_TTL_SECONDS,
    EXPRESSION_ATTRIBUTE_NAMES,
    STATUSES,
    ReviewState,
    StateError,
    build_claim_expressions,
    build_delivery_item,
    build_establish_confirm,
    build_establish_equality,
    build_establish_first_write,
    build_finalize_expressions,
    delivery_pk,
    delivery_ttl,
    from_item,
    is_stale,
    review_pk,
    to_item,
)

HEAD_A = "aa" * 20
HEAD_B = "bb" * 20
BASE = "cc" * 20
GUID = "123e4567-e89b-12d3-a456-426614174000"
RAW_OWNER = "raw-guid :: WITH CAPS/bytes.untouched \t"
UPDATED = "2026-09-12T10:00:00Z"
NOW = 1_750_000_000
COMMENT_ID = 987654


def _claimed(**overrides):
    kwargs = {
        "pk": "review:octo-org/hello-world#42",
        "status": "CLAIMED",
        "generation": 0,
        "head_sha": HEAD_A,
        "last_seen_sha": HEAD_A,
        "claim_owner": GUID,
        "claim_until": NOW + CLAIM_LEASE_SECONDS,
        "updated_at": UPDATED,
    }
    kwargs.update(overrides)
    return kwargs


def _active(**overrides):
    kwargs = _claimed(status="ACTIVE", comment_id=COMMENT_ID)
    kwargs.update(overrides)
    return kwargs


def _rejected(item, field, reason):
    with pytest.raises(StateError) as excinfo:
        from_item(item)
    assert excinfo.value.field == field
    assert excinfo.value.reason == reason


# --- pk builders -----------------------------------------------------------


def test_review_pk_exact_format():
    assert review_pk("octo-org/hello-world", 123) == "review:octo-org/hello-world#123"


def test_delivery_pk_exact_format():
    assert delivery_pk(GUID) == f"delivery:{GUID}"


def test_delivery_ttl_is_seven_days():
    assert DELIVERY_TTL_SECONDS == 7 * 24 * 3600 == 604800
    assert delivery_ttl(NOW) == NOW + 604800


def test_build_delivery_item_shape():
    item = build_delivery_item(GUID, NOW)
    assert item == {"pk": f"delivery:{GUID}", "ttl": NOW + 604800}


def test_claim_lease_is_180s():
    assert CLAIM_LEASE_SECONDS == 180


# --- round-trips -----------------------------------------------------------


def test_round_trip_active():
    state = ReviewState(**_active())
    assert from_item(to_item(state)) == state


def test_round_trip_claimed_without_comment_id():
    state = ReviewState(**_claimed())
    item = to_item(state)
    assert "comment_id" not in item
    assert from_item(item) == state


def test_round_trip_generation_zero_and_large():
    assert from_item(to_item(ReviewState(**_active(generation=0)))).generation == 0
    big = 2**31 - 1
    assert from_item(to_item(ReviewState(**_active(generation=big)))).generation == big


def test_round_trip_claim_owner_bytes_preserved():
    state = ReviewState(**_claimed(claim_owner=RAW_OWNER))
    assert from_item(to_item(state)).claim_owner == RAW_OWNER


def test_round_trip_large_comment_id():
    big = 2**63 - 1
    state = ReviewState(**_active(comment_id=big))
    assert from_item(to_item(state)).comment_id == big


def test_round_trip_updated_at_preserved_verbatim():
    stamped = "2026-09-12T10:00:00+00:00"
    state = ReviewState(**_active(updated_at=stamped))
    assert to_item(state)["updated_at"] == stamped
    assert from_item(to_item(state)) == state


def test_from_item_ignores_unknown_extra_fields():
    item = to_item(ReviewState(**_active()))
    item["future_field"] = "additive evolution"
    assert from_item(item) == ReviewState(**_active())


# --- status ----------------------------------------------------------------


def test_stored_statuses_are_claimed_and_active_only():
    assert STATUSES == frozenset({"CLAIMED", "ACTIVE"})
    assert "STALE" not in STATUSES
    assert "ABSENT" not in STATUSES


@pytest.mark.parametrize("status", ["STALE", "ABSENT", "active", "claimed", "", "PENDING"])
def test_reject_non_stored_status(status):
    _rejected(_claimed(status=status), "status", "bad_status")


@pytest.mark.parametrize("status", [None, 123, b"ACTIVE"])
def test_reject_non_string_status(status):
    _rejected(_claimed(status=status), "status", "bad_status")


def test_codec_never_emits_stale_as_stored_status():
    with pytest.raises(StateError) as excinfo:
        ReviewState(**_claimed(status="STALE"))
    assert excinfo.value.field == "status"
    assert excinfo.value.reason == "bad_status"


# --- generation ------------------------------------------------------------


def test_reject_negative_generation():
    _rejected(_claimed(generation=-1), "generation", "bad_generation")


@pytest.mark.parametrize("generation", ["0", 1.0, True, False, None])
def test_reject_non_int_generation(generation):
    _rejected(_claimed(generation=generation), "generation", "bad_generation")


# --- SHAs ------------------------------------------------------------------


@pytest.mark.parametrize("field", ["head_sha", "last_seen_sha"])
@pytest.mark.parametrize(
    "sha",
    [
        "a" * 39,  # too short
        "a" * 41,  # too long
        "A" * 40,  # uppercase
        "gg" + "a" * 38,  # non-hex
        "aa" * 19 + "  ",  # whitespace
        "",
    ],
)
def test_reject_malformed_sha(field, sha):
    _rejected(_claimed(**{field: sha}), field, "bad_sha")


@pytest.mark.parametrize("field", ["head_sha", "last_seen_sha"])
@pytest.mark.parametrize("sha", [None, 123, b"aa" * 20, ["aa" * 20]])
def test_reject_non_string_sha(field, sha):
    _rejected(_claimed(**{field: sha}), field, "bad_sha")


def test_base_sha_shape_reference():
    assert len(BASE) == 40


# --- claim_until / claim_owner ---------------------------------------------


@pytest.mark.parametrize("until", ["123", 1.5, True, False, None, -1])
def test_reject_bad_claim_until(until):
    _rejected(_claimed(claim_until=until), "claim_until", "bad_claim_until")


def test_claim_until_epoch_zero_is_valid_epoch():
    assert from_item(to_item(ReviewState(**_claimed(claim_until=0)))).claim_until == 0


@pytest.mark.parametrize("owner", [None, 123, b"bytes", ["guid"]])
def test_reject_non_string_claim_owner(owner):
    _rejected(_claimed(claim_owner=owner), "claim_owner", "bad_claim_owner")


# --- comment_id ------------------------------------------------------------


def test_reject_claimed_carrying_comment_id():
    _rejected(_claimed(comment_id=COMMENT_ID), "comment_id", "unexpected_comment_id")


def test_reject_active_missing_comment_id():
    item = to_item(ReviewState(**_active()))
    del item["comment_id"]
    _rejected(item, "comment_id", "missing_comment_id")


@pytest.mark.parametrize("comment_id", ["987654", 98.0, True, False, 0, -5, 2**63])
def test_reject_bad_comment_id(comment_id):
    _rejected(_active(comment_id=comment_id), "comment_id", "bad_comment_id")


def test_reject_active_explicit_null_comment_id():
    _rejected(_active(comment_id=None), "comment_id", "missing_comment_id")


# --- updated_at ------------------------------------------------------------


@pytest.mark.parametrize(
    "updated_at",
    [
        "2026-09-12T10:00:00",  # naive, no timezone
        "2026-09-12T10:00:00+05:00",  # non-UTC offset
        "2026-09-12T10:00:00-08:00",  # non-UTC offset
        "not-a-date",
        "",
    ],
)
def test_reject_non_utc_updated_at(updated_at):
    _rejected(_claimed(updated_at=updated_at), "updated_at", "bad_updated_at")


@pytest.mark.parametrize("updated_at", [None, 123, 1.5])
def test_reject_non_string_updated_at(updated_at):
    _rejected(_claimed(updated_at=updated_at), "updated_at", "bad_updated_at")


# --- pk / shape ------------------------------------------------------------


@pytest.mark.parametrize(
    "pk",
    [
        f"delivery:{GUID}",  # wrong item type
        "review:no-hash-here",
        "review:#42",  # empty repo
        "nope",
        "",
    ],
)
def test_reject_malformed_pk(pk):
    _rejected(_claimed(pk=pk), "pk", "bad_pk")


@pytest.mark.parametrize("pk", [None, 123])
def test_reject_non_string_pk(pk):
    _rejected(_claimed(pk=pk), "pk", "bad_pk")


@pytest.mark.parametrize("item", [None, "string", ["list"], 123])
def test_reject_non_object_item(item):
    _rejected(item, "item", "not_object")


@pytest.mark.parametrize(
    "field",
    [
        "pk",
        "status",
        "generation",
        "head_sha",
        "last_seen_sha",
        "claim_owner",
        "claim_until",
        "updated_at",
    ],
)
def test_reject_missing_required_field(field):
    item = to_item(ReviewState(**_claimed()))
    del item[field]
    _rejected(item, field, "missing")


# --- typed errors ----------------------------------------------------------


def test_state_error_is_typed_value_error():
    with pytest.raises(StateError) as excinfo:
        from_item(_claimed(status="STALE"))
    err = excinfo.value
    assert isinstance(err, ValueError)
    assert err.field == "status"
    assert err.reason == "bad_status"
    assert "status" in str(err) and "bad_status" in str(err)


# --- STALE derivation ------------------------------------------------------


def test_is_stale_derivation():
    assert is_stale(NOW - 1, NOW) is True
    assert is_stale(NOW, NOW) is False
    assert is_stale(NOW + 1, NOW) is False


# --- §3.3 expression builders ----------------------------------------------


def test_claim_condition_exact_string():
    update, condition, values = build_claim_expressions(
        head_sha=HEAD_A, generation=7, claim_owner=GUID, claim_until=NOW + 180, now=NOW
    )
    assert (
        condition == "head_sha = :reviewed AND generation = :gen "
        "AND (claim_until < :now OR attribute_not_exists(claim_owner))"
    )
    assert values[":reviewed"] == HEAD_A
    assert values[":gen"] == 7
    assert values[":now"] == NOW
    assert "claim_owner" in update and "claim_until" in update


def test_finalize_is_revision_only():
    update, condition, values = build_finalize_expressions(
        head_sha=HEAD_A, generation=7, comment_id=COMMENT_ID, updated_at=UPDATED
    )
    assert condition == "head_sha = :reviewed AND generation = :gen"
    assert values[":reviewed"] == HEAD_A
    assert values[":gen"] == 7
    assert values[":comment"] == COMMENT_ID
    assert "comment_id" in update
    assert "claim_owner" not in update and "claim_until" not in update
    assert EXPRESSION_ATTRIBUTE_NAMES == {"#st": "status"}


def test_establish_first_write_guards_absence():
    state = ReviewState(**_claimed(generation=0))
    update, condition, values = build_establish_first_write(state)
    assert condition == "attribute_not_exists(pk)"
    assert values[":head"] == HEAD_A
    assert values[":gen"] == 0
    assert "head_sha" in update and "generation" in update


def test_establish_equality_requires_last_seen_match():
    update, condition, values = build_establish_equality(incoming_sha=HEAD_A, updated_at=UPDATED)
    assert condition == "last_seen_sha = :sha"
    assert values[":sha"] == HEAD_A
    assert values[":updated_at"] == UPDATED
    assert "last_seen_sha" in update


def test_establish_confirm_increments_generation():
    update, condition, values = build_establish_confirm(
        incoming_sha=HEAD_B,
        expected_generation=41,
        claim_owner=GUID,
        claim_until=NOW + 180,
        updated_at=UPDATED,
    )
    assert condition == "generation = :expected_gen"
    assert values[":expected_gen"] == 41
    assert values[":next_gen"] == 42
    assert values[":head"] == HEAD_B
    assert "generation = :next_gen" in update

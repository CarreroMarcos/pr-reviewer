"""T005: envelope schema cases per contracts/ingress-webhook.md.

Valid/invalid cases mirror the contract's SQS-envelope schema table
verbatim, including the D1 delta (`reopened` is accepted). Every
rejection is typed: tests assert `EnvelopeError` plus its machine-readable
`field`/`reason`, never a bare `Exception`.
"""

import uuid
from dataclasses import FrozenInstanceError

import pytest

from common.envelope import EnvelopeError, validate_envelope

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
BASE_SHA = "fedcba9876543210fedcba9876543210fedcba98"

REQUIRED_FIELDS = (
    "envelope_version",
    "event_type",
    "action",
    "repo_full_name",
    "pr_number",
    "head_sha",
    "base_sha",
    "sender",
    "delivery_guid",
)


def _valid(**overrides):
    envelope = {
        "envelope_version": "v1",
        "event_type": "pull_request",
        "action": "opened",
        "repo_full_name": "octo-org/hello-world",
        "pr_number": 42,
        "head_sha": HEAD_SHA,
        "base_sha": BASE_SHA,
        "sender": "octocat",
        "delivery_guid": str(uuid.uuid4()),
    }
    envelope.update(overrides)
    return envelope


def _rejected(payload, field, reason):
    with pytest.raises(EnvelopeError) as excinfo:
        validate_envelope(payload)
    assert excinfo.value.field == field
    assert excinfo.value.reason == reason


# --- valid ---------------------------------------------------------------


@pytest.mark.parametrize("action", ["opened", "synchronize", "ready_for_review", "reopened"])
def test_valid_envelope_for_every_allowed_action(action):
    envelope = validate_envelope(_valid(action=action))
    assert envelope.action == action
    assert envelope.envelope_version == "v1"
    assert envelope.event_type == "pull_request"
    assert envelope.repo_full_name == "octo-org/hello-world"
    assert envelope.pr_number == 42
    assert envelope.head_sha == HEAD_SHA
    assert envelope.base_sha == BASE_SHA
    assert envelope.sender == "octocat"


def test_valid_boundary_values():
    guid = str(uuid.uuid4())
    envelope = validate_envelope(
        _valid(
            repo_full_name="a" * 63 + "/" + "b" * 64,  # exactly 128 chars
            pr_number=1,
            sender="s" * 64,
            delivery_guid=guid,
        )
    )
    assert envelope.pr_number == 1
    assert envelope.delivery_guid == guid


def test_valid_pr_number_max():
    assert validate_envelope(_valid(pr_number=10**9)).pr_number == 10**9


def test_envelope_error_is_value_error():
    assert issubclass(EnvelopeError, ValueError)


def test_envelope_is_frozen():
    envelope = validate_envelope(_valid())
    with pytest.raises(FrozenInstanceError):
        envelope.action = "closed"  # type: ignore[misc]


# --- version / event / action --------------------------------------------


@pytest.mark.parametrize("version", ["v2", "v0", "", "V1", "v1 ", 1, None])
def test_bad_envelope_version(version):
    _rejected(_valid(envelope_version=version), "envelope_version", "bad_version")


@pytest.mark.parametrize("event", ["push", "Pull_Request", "pullrequest", "", None])
def test_bad_event_type(event):
    _rejected(_valid(event_type=event), "event_type", "bad_event")


@pytest.mark.parametrize("action", ["closed", "labeled", "OPENED", "", "reopen", None, 42])
def test_bad_action(action):
    _rejected(_valid(action=action), "action", "bad_action")


# --- repo_full_name -------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        "owneronly",  # missing slash
        "/repo",  # empty owner
        "owner/",  # empty name
        "owner/repo/extra",  # two slashes
        "",  # empty
        "owner/re po",  # space
        "owner/repo!",  # bang outside charset
        "own@er/repo",  # at-sign outside charset
        "a/" + "b" * 127,  # 129 chars, over the 128 bound
        123,  # non-string
        None,
    ],
)
def test_bad_repo_full_name(repo):
    _rejected(_valid(repo_full_name=repo), "repo_full_name", "bad_repo")


# --- pr_number ------------------------------------------------------------


@pytest.mark.parametrize("number", [0, -1, 10**9 + 1, "42", 4.2, True, False, None])
def test_bad_pr_number(number):
    _rejected(_valid(pr_number=number), "pr_number", "bad_pr_number")


# --- head_sha / base_sha --------------------------------------------------


@pytest.mark.parametrize("field", ["head_sha", "base_sha"])
@pytest.mark.parametrize(
    "sha",
    [
        "a" * 39,  # too short
        "a" * 41,  # too long
        "A" * 40,  # uppercase, must be lowercase hex
        "0123456789ABCDEF0123456789abcdef01234567",  # mixed case
        "g" * 40,  # non-hex alpha
        "z" * 40,  # non-hex alpha
        "",  # empty
        1234567890,  # non-string
        None,
    ],
)
def test_bad_sha(field, sha):
    _rejected(_valid(**{field: sha}), field, "bad_sha")


# --- sender ---------------------------------------------------------------


@pytest.mark.parametrize(
    "sender",
    [
        "s" * 65,  # over the 64-char bound
        "octo_cat",  # underscore outside GitHub login charset
        "octo cat",  # space outside charset
        "@octocat",  # at-sign outside charset
        "octocat!",  # bang outside charset
        "",  # empty
        123,  # non-string
        None,
    ],
)
def test_bad_sender(sender):
    _rejected(_valid(sender=sender), "sender", "bad_sender")


def test_valid_hyphenated_sender():
    assert validate_envelope(_valid(sender="mona-lisa8")).sender == "mona-lisa8"


# --- delivery_guid --------------------------------------------------------


@pytest.mark.parametrize(
    "guid",
    [
        "not-a-uuid",
        "",
        "12345",
        "g" * 36,  # right shape, non-hex
        "x" * 65,  # over the 64-char bound
        123,  # non-string
        None,
    ],
)
def test_bad_delivery_guid(guid):
    _rejected(_valid(delivery_guid=guid), "delivery_guid", "bad_guid")


# --- shape ----------------------------------------------------------------


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_missing_required_field(field):
    payload = _valid()
    del payload[field]
    _rejected(payload, field, "missing")


@pytest.mark.parametrize("payload", [["not", "a", "dict"], "string", None, 42])
def test_non_object_payload(payload):
    _rejected(payload, "envelope", "not_object")

"""T052: failure-state notice contract **[D2]** (FR-028, FR-019, FR-020,
FR-026; contracts/canonical-comment.md).

Exercises `common.failure_notice` — assembly of the fixed template plus
fenced publication of the notice through the HLD §3.3 path, with the D2
trigger mapping as a pure decision function.

Mapping (contract row → test):

* fixed template, SHA the only variable → `test_template_*` (exact text,
  two SHAs differ only in the SHA)
* canonical marker present, worker-injected → `test_marker_present_once_first`
* prohibition list (no internal error details / provider names / statuses /
  credential-like strings / @mentions / external media / verdicts) →
  `test_prohibition_list`
* malformed SHA rejected → `test_malformed_sha_rejected`
* fenced publish: stored comment → PATCH + finalize, disposition true →
  `test_fenced_publish_patches_stored_comment`
* no canonical comment → creation-lease POST → persist, true →
  `test_absent_comment_posts_via_creation_lease`
* stale revision (fence mismatch) → nothing published, `skipped-stale` →
  `test_stale_revision_skipped_stale`
* finalize conflict → landed anyway (`true`), newer record intact →
  `test_finalize_conflict_still_true_newer_intact`
* idempotent re-publish → same id PATCHed twice, one comment →
  `test_idempotent_republish_patches_same_comment`
* unparseable list / transport failure → `false`, never raises
  (best-effort) → `test_unpublishable_list_returns_false`,
  `test_transport_failure_returns_false_never_raises`
* trigger table (contract §"Trigger mapping") → `test_trigger_*` matrix:
  transient + LLM-unusable at final attempt publish; assemble-invalid and
  LLM-401 publish immediately; GitHub-401 / lost-access / invalid envelope
  / superseded / stale / duplicate / non-failures skip
* no-notice headline rows → `test_no_notice_rows_skip`

Ports (duck-typed — fakes mirror these exactly):

* table: `get_item(pk)` (copy semantics) + `update_item(...)` raising
  `common.protocol.ConditionalCheckFailed` on guard mismatch. `FakeTable`
  evaluates EXACTLY the condition strings the fenced path emits
  (establish/claim/finalize/creation-lease) and raises ValueError on
  anything else — Gate-2 discipline: a builder-string change breaks
  tests, never passes silently.
* GitHub side: `list_page(page)` (parsed JSON), `create_comment(body)`,
  `update_comment(id, body)` (PATCH), `delete_comment(id)`.

Disposition boundary: `publish_failure_notice` RETURNS the HLD §4.3 /
research-R8 `failure_notice_published` value (`true`/`false`/
`skipped-stale`); emitting it into `logs.py` FIXED_FIELDS and wiring the
worker call site (+ SQS `ApproximateReceiveCount` extraction) is T054
scope, not this module's.

All deterministic: fixed clock (`NOW`), scripted collaborators, no sleeps.
RED phase: `common.failure_notice` does not exist yet — ImportError-driven
failure is the expected red.
"""

import pytest

from common.failure_notice import (
    NoticeDisposition,
    NoticeTrigger,
    build_failure_notice,
    publish_failure_notice,
    should_publish_notice,
)
from common.marker import build_marker
from common.protocol import ConditionalCheckFailed

REPO = "octo-org/hello-world"
PR_NUMBER = 42
PK = "review:octo-org/hello-world#42"
MARKER = build_marker(REPO, PR_NUMBER)
SHA_A = "aa" * 20
SHA_B = "bb" * 20
BASE_SHA = "00" * 20
GUID_1 = "11111111-1111-4111-8111-111111111111"
NOW = 1_750_000_000
STORED_ID = 555
POST_ID = 777
UPDATED_AT = "2026-09-12T10:00:00Z"

EXPECTED_TEMPLATE = (
    "Automated review could not be completed for revision {sha}. "
    "The failure has been logged for the operator; "
    "no findings are available for this revision."
)

# Establish/claim/finalize/creation-lease condition vocabulary (exact
# strings emitted by common.state builders + common.reconcile).
_FIRST_WRITE = "attribute_not_exists(pk)"
_EQUALITY = "last_seen_sha = :sha"
_GENERATION_GUARD = "generation = :expected_gen"
_CLAIM = (
    "head_sha = :reviewed AND generation = :gen "
    "AND (claim_until < :now OR attribute_not_exists(claim_owner))"
)
_FINALIZE = "head_sha = :reviewed AND generation = :gen AND claim_owner = :owner"
_CREATION_LEASE = (
    "head_sha = :reviewed AND generation = :gen "
    "AND (claim_until < :now OR attribute_not_exists(claim_owner) "
    "OR claim_owner = :owner) AND attribute_not_exists(comment_id)"
)


class FakeTable:
    """Minimal table double evaluating exactly the fenced-path vocabulary."""

    def __init__(self):
        self.items = {}
        self.updates = []

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
        pk = Key["pk"]
        self.updates.append(ConditionExpression)
        current = self.items.get(pk)
        values = ExpressionAttributeValues or {}
        if not self._holds(ConditionExpression, current, values):
            raise ConditionalCheckFailed(f"condition not met: {ConditionExpression}")
        names = ExpressionAttributeNames or {}
        if not UpdateExpression.startswith("SET "):
            raise ValueError(f"unsupported update: {UpdateExpression!r}")
        set_part, sep, remove_part = UpdateExpression[4:].partition(" REMOVE ")
        item = dict(current) if current is not None else {"pk": pk}
        for clause in set_part.split(", "):
            attr, _, placeholder = clause.partition(" = ")
            item[names.get(attr, attr)] = values[placeholder]
        if sep:
            for attr in remove_part.split(", "):
                item.pop(names.get(attr, attr), None)
        self.items[pk] = item
        return {"Attributes": dict(item)}

    def _holds(self, condition, current, values):
        if condition == _FIRST_WRITE:
            return current is None
        if condition == _EQUALITY:
            return current is not None and current.get("last_seen_sha") == values[":sha"]
        if condition == _GENERATION_GUARD:
            return current is not None and current.get("generation") == values[":expected_gen"]
        if condition == _CLAIM:
            return (
                current is not None
                and current.get("head_sha") == values[":reviewed"]
                and current.get("generation") == values[":gen"]
                and (current.get("claim_until", 0) < values[":now"] or "claim_owner" not in current)
            )
        if condition == _FINALIZE:
            return (
                current is not None
                and current.get("head_sha") == values[":reviewed"]
                and current.get("generation") == values[":gen"]
                and current.get("claim_owner") == values[":owner"]
            )
        if condition == _CREATION_LEASE:
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
        raise ValueError(f"unsupported condition: {condition!r}")


class ScriptedGitHub:
    """Comment-store double with scriptable transport faults."""

    def __init__(self, *, comments=None, post_id=POST_ID, fault=None):
        self.comments = [dict(c) for c in (comments or [])]
        self._post_id = post_id
        self._fault = fault  # (method, status) injected once, then cleared
        self.calls = []

    def list_page(self, page):
        self.calls.append(("GET", page))
        assert page == 1  # single-page fixtures throughout
        return [dict(c) for c in self.comments]

    def create_comment(self, body):
        self.calls.append(("POST", body))
        self._maybe_fault("POST")
        self.comments.append({"id": self._post_id, "body": body})
        return self._post_id

    def update_comment(self, comment_id, body):
        self.calls.append(("PATCH", comment_id))
        self._maybe_fault("PATCH")
        for comment in self.comments:
            if comment["id"] == comment_id:
                comment["body"] = body
                return
        raise AssertionError(f"PATCH of unknown comment {comment_id}")

    def delete_comment(self, comment_id):
        self.calls.append(("DELETE", comment_id))
        self.comments = [c for c in self.comments if c["id"] != comment_id]

    def _maybe_fault(self, method):
        if self._fault is not None and self._fault[0] == method:
            _, status = self._fault
            self._fault = None
            raise _TransportError(status)


class _TransportError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"transport failed: {status}")


def _seed_active(table, *, head, gen, comment, owner=GUID_1, until=NOW + 100):
    table.items[PK] = {
        "pk": PK,
        "status": "ACTIVE",
        "generation": gen,
        "head_sha": head,
        "last_seen_sha": head,
        "claim_owner": owner,
        "claim_until": until,
        "comment_id": comment,
        "updated_at": UPDATED_AT,
    }


def _publish(table, github, *, sha=SHA_B, live=None, on_publish=None, owner=GUID_1):
    live_shas = [live or sha]

    def fence():
        if len(live_shas) > 1:
            return live_shas.pop(0)
        if on_publish is not None and live_shas[0] == "hook":
            on_publish()
            return sha
        return live_shas[0]

    return publish_failure_notice(
        repo_full_name=REPO,
        pr_number=PR_NUMBER,
        head_sha=sha,
        owner=owner,
        table=table,
        now=lambda: NOW,
        fence=fence,
        list_page=github.list_page,
        create_comment=github.create_comment,
        update_comment=github.update_comment,
        delete_comment=github.delete_comment,
    )


# --- construction: fixed template, SHA the only variable ---


def test_template_exact_text_sha_only_variable():
    """The notice is the fixed D2 template with exactly the head SHA
    interpolated — two SHAs yield bodies differing ONLY in the SHA."""
    first = build_failure_notice(REPO, PR_NUMBER, SHA_A)
    second = build_failure_notice(REPO, PR_NUMBER, SHA_B)
    assert EXPECTED_TEMPLATE.format(sha=SHA_A) in first
    assert EXPECTED_TEMPLATE.format(sha=SHA_B) in second
    assert first.replace(SHA_A, "") == second.replace(SHA_B, "")


def test_marker_present_once_first():
    """The worker-injected canonical marker opens the notice exactly once —
    canonical identity survives even when the body is a failure state."""
    content = build_failure_notice(REPO, PR_NUMBER, SHA_B)
    assert content.startswith(MARKER)
    assert content.count(MARKER) == 1


def test_prohibition_list():
    """No internal error details, provider names, statuses, credential-like
    strings, @mentions, external media, or approval/merge verdicts —
    the notice carries zero operable or misleading content (FR-026/028)."""
    content = build_failure_notice(REPO, PR_NUMBER, SHA_B)
    lowered = content.lower()
    for forbidden in (
        "@",
        "http",
        "z.ai",
        "glm",
        "openai",
        "anthropic",
        "401",
        "403",
        "404",
        "429",
        "timeout",
        "traceback",
        "ghp_",
        "safe to merge",
        "approved",
        "lgtm",
        "retry",
        "dlq",
        "sqs",
        "dynamodb",
    ):
        assert forbidden not in lowered, f"prohibited substring present: {forbidden!r}"


def test_malformed_sha_rejected():
    """The SHA is the only variable, so it is validated strictly: short,
    non-hex, and uppercase SHAs are refused loudly (never interpolated)."""
    from common.failure_notice import NoticeError

    for bad in ("zz", "A" * 40, SHA_B[:-1], "", "bb" * 19 + "bg"):
        try:
            build_failure_notice(REPO, PR_NUMBER, bad)
        except NoticeError as exc:
            assert exc.field == "head_sha"
        else:
            raise AssertionError(f"expected NoticeError for {bad!r}")


# --- fenced publish: claim → fence → PATCH / lease-POST → finalize ---


def test_fenced_publish_patches_stored_comment():
    """ACTIVE record with a stored id → fence (live == reviewed) → PATCH
    that id with the notice → finalize ACTIVE; disposition `true`."""
    table, github = (
        FakeTable(),
        ScriptedGitHub(comments=[{"id": STORED_ID, "body": "old review " + MARKER}]),
    )
    _seed_active(table, head=SHA_B, gen=2, comment=STORED_ID)
    result = _publish(table, github)
    assert result.disposition == NoticeDisposition.PUBLISHED_TRUE
    assert result.comment_id == STORED_ID
    assert github.calls[0][0] == "GET"  # reconcile lists before adopting
    assert ("PATCH", STORED_ID) in github.calls
    assert table.items[PK]["comment_id"] == STORED_ID
    assert table.items[PK]["status"] == "ACTIVE"
    assert EXPECTED_TEMPLATE.format(sha=SHA_B) in github.comments[0]["body"]


def test_absent_comment_posts_via_creation_lease():
    """No canonical comment anywhere → creation lease won → POST → persist
    → re-check; exactly one marker-bearing comment exists; `true`."""
    table, github = FakeTable(), ScriptedGitHub()
    result = _publish(table, github)
    assert result.disposition == NoticeDisposition.PUBLISHED_TRUE
    assert result.comment_id == POST_ID
    assert [c for c in github.calls if c[0] == "POST"]
    assert len(github.comments) == 1
    assert github.comments[0]["body"].count(MARKER) == 1
    assert table.items[PK]["comment_id"] == POST_ID


def test_stale_revision_skipped_stale():
    """Fence mismatch (live moved past the reviewed revision) → the notice
    for a stale revision is NOT published: no GitHub write of any kind
    after the fence, disposition `skipped-stale` (FR-028 currency check)."""
    table, github = (
        FakeTable(),
        ScriptedGitHub(comments=[{"id": STORED_ID, "body": "old review " + MARKER}]),
    )
    _seed_active(table, head=SHA_A, gen=1, comment=STORED_ID)
    result = _publish(table, github, sha=SHA_A, live="cc" * 20)
    assert result.disposition == NoticeDisposition.SKIPPED_STALE
    assert result.comment_id is None
    assert github.calls == []
    assert table.items[PK]["head_sha"] == SHA_A  # record untouched


def test_finalize_conflict_still_true_newer_intact():
    """A newer accepted revision landing between publish and finalize →
    the notice already landed, so disposition stays `true`, and the newer
    record is never overwritten (log-and-reconcile path)."""
    table, github = (
        FakeTable(),
        ScriptedGitHub(comments=[{"id": STORED_ID, "body": "old review " + MARKER}]),
    )
    _seed_active(table, head=SHA_A, gen=4, comment=STORED_ID)

    def land_newer():
        _seed_active(table, head=SHA_B, gen=6, comment=999)

    result = _publish(table, github, sha=SHA_A, live="hook", on_publish=land_newer)
    assert result.disposition == NoticeDisposition.PUBLISHED_TRUE
    assert table.items[PK]["head_sha"] == SHA_B
    assert table.items[PK]["generation"] == 6


def test_idempotent_republish_patches_same_comment():
    """Re-delivery re-attempts the identical PATCH: two publishes, one
    comment, same id, both `true` — convergent, never duplicating."""
    table, github = (
        FakeTable(),
        ScriptedGitHub(comments=[{"id": STORED_ID, "body": "old review " + MARKER}]),
    )
    _seed_active(table, head=SHA_B, gen=2, comment=STORED_ID)
    first = _publish(table, github)
    second = _publish(table, github)
    assert first.disposition == second.disposition == NoticeDisposition.PUBLISHED_TRUE
    assert first.comment_id == second.comment_id == STORED_ID
    assert len(github.comments) == 1
    assert [c for c in github.calls if c[0] == "POST"] == []


def test_unpublishable_list_returns_false():
    """Unparseable listing → best-effort `false`, never raises: the notice
    failure must not mask the alert/DLQ path it accompanies."""
    table, github = FakeTable(), ScriptedGitHub()
    github.list_page = lambda page: {"message": "oops"}  # noqa: E731 (fault double)
    result = _publish(table, github)
    assert result.disposition == NoticeDisposition.PUBLISHED_FALSE
    assert result.comment_id is None


def test_transport_failure_returns_false_never_raises():
    """A 500 on the POST → best-effort `false`, no exception escapes (the
    queue owns retries for the underlying error, not the notice)."""
    table, github = FakeTable(), ScriptedGitHub(fault=("POST", 500))
    result = _publish(table, github)
    assert result.disposition == NoticeDisposition.PUBLISHED_FALSE
    assert result.comment_id is None


# --- trigger mapping: which §2.3 item 8 rows publish ---


@pytest.mark.parametrize(
    ("trigger", "receive_count", "expected"),
    [
        # Transient at the FINAL attempt → publish (then raise: DLQ proceeds).
        (NoticeTrigger.TRANSIENT, 5, True),
        (NoticeTrigger.TRANSIENT, 7, True),  # tolerate > 5 after redrive
        (NoticeTrigger.LLM_UNUSABLE, 5, True),
        (NoticeTrigger.LLM_UNUSABLE, 9, True),
        # Transient before the final attempt → skip (retry continues).
        (NoticeTrigger.TRANSIENT, 1, False),
        (NoticeTrigger.TRANSIENT, 4, False),
        (NoticeTrigger.LLM_UNUSABLE, 2, False),
        # Permanent rows → publish immediately (receive count irrelevant).
        (NoticeTrigger.ASSEMBLE_INVALID, 1, True),
        (NoticeTrigger.ASSEMBLE_INVALID, 5, True),
        (NoticeTrigger.LLM_401, 1, True),
        (NoticeTrigger.INVALID_KEY, 1, True),
        (NoticeTrigger.INVALID_KEY, 5, True),
        # No-rows → skip at any count.
        (NoticeTrigger.GITHUB_401, 5, False),
        (NoticeTrigger.LIST_FORBIDDEN, 5, False),
        (NoticeTrigger.INVALID_ENVELOPE, 1, False),
        (NoticeTrigger.SUPERSEDED, 1, False),
        (NoticeTrigger.STALE, 3, False),
        (NoticeTrigger.DUPLICATE, 2, False),
        (NoticeTrigger.NON_FAILURE, 5, False),
    ],
)
def test_trigger_matrix(trigger, receive_count, expected):
    assert should_publish_notice(trigger, receive_count=receive_count) is expected


def test_no_notice_rows_skip():
    """Headline No-rows from the contract table, stated plainly: GitHub-401
    (writes would 401 too), lost access (publication impossible), invalid
    envelope (identifiers untrusted), and non-failures never publish."""
    assert should_publish_notice(NoticeTrigger.GITHUB_401, receive_count=5) is False
    assert should_publish_notice(NoticeTrigger.LIST_FORBIDDEN, receive_count=5) is False
    assert should_publish_notice(NoticeTrigger.INVALID_ENVELOPE, receive_count=1) is False
    assert should_publish_notice(NoticeTrigger.NON_FAILURE, receive_count=5) is False

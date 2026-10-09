"""Description-vs-diff race guard (hardening b).

Drives the production `_make_review` review port: the diff transport serves
PR meta, the LLM double returns a review containing a description-judging
finding, and a post-LLM meta re-fetch decides whether that finding survives.

Pins:
- meta unchanged between fetch and re-fetch → model text passes through
  byte-identical (description finding intact);
- meta changed mid-review → description-referencing finding bullets are
  dropped, code findings survive, other sections untouched;
- re-fetch failure → best-effort degrade: text untouched, review publishes;
- the pure filter drops only `## Findings` bullets referencing the
  description/title; an emptied section carries the empty-findings sentinel.
"""

import json
import os
from contextlib import nullcontext
from unittest.mock import Mock, patch

import worker_handler
from common.assemble import EMPTY_FINDINGS_SENTINEL
from common.config import ConfigProvider
from common.diff import HttpResponse
from common.envelope import Envelope
from worker_handler import _Credentials, _drop_stale_description_findings, _make_review

HOST = "llm.example.test"
ENDPOINT = f"https://{HOST}/v1/chat/completions"

GITHUB_TOKEN_VALUE = "ghp-test-token-value"  # noqa: S105 (fake fixture)
GLM_API_KEY_VALUE = "glm-key-value"  # noqa: S105 (fake fixture)

REPO = "octo-org/hello-world"
PR_NUMBER = 42
SHA = "bb" * 20
TITLE_A = "Fix the login helper"
BODY_A = "Small cleanup with tests."
TITLE_B = "Fix the login helper (updated)"
BODY_B = "Small cleanup with tests. Now also touches auth."

DESC_FINDING = (
    '- [MEDIUM] `src/main.py:9` — The PR description says "one file" '
    "yet the diff touches two. Fix: update the description."
)
CODE_FINDING = "- [LOW] `src/main.py:12` — Missing bound. Fix: add a check."

REVIEW_WITH_BOTH = (
    "## Summary\nAdds input validation.\n\n"
    "## Findings\n" + DESC_FINDING + "\n" + CODE_FINDING + "\n\n"
    "## Risk Notes\nNone.\n"
)
REVIEW_DESC_ONLY = (
    "## Summary\nAdds input validation.\n\n"
    "## Findings\n" + DESC_FINDING + "\n\n"
    "## Risk Notes\nNone.\n"
)


def _ssm_values():
    return {
        "/pr-reviewer/github-token": GITHUB_TOKEN_VALUE,
        "/pr-reviewer/webhook-secret": "webhook-secret-value",  # noqa: S105 (fake fixture)
        "/pr-reviewer/glm-api-key": GLM_API_KEY_VALUE,
        "/pr-reviewer/glm-model": "glm-5.3-flash",
        "/pr-reviewer/glm-endpoint": ENDPOINT,
    }


class _FakeSSM:
    def __init__(self, values):
        self.values = dict(values)

    def get_parameters(self, Names, WithDecryption=False):  # noqa: N803 (boto3 shape)
        params = [
            {"Name": name, "Value": self.values[name]} for name in Names if name in self.values
        ]
        invalid = [name for name in Names if name not in self.values]
        return {"Parameters": params, "InvalidParameters": invalid}


class _StatefulDiffTransport:
    """PR-meta URL: first hit serves meta A; later hits serve meta B (the
    post-LLM re-fetch), raise when `fail_refetch`, or repeat meta A when
    `changed` is False. `/files` serves one patch. `change_mode` selects
    which field differs on later hits: "both", "title", or "body"."""

    def __init__(self, events, *, changed=True, fail_refetch=False, change_mode="both"):
        self._events = events
        self._changed = changed
        self._fail_refetch = fail_refetch
        self._change_mode = change_mode
        self._meta_hits = 0

    def __call__(self, url, headers):
        self._events.append("diff")
        if "/files" in url:
            payload = [
                {
                    "filename": "src/main.py",
                    "additions": 5,
                    "deletions": 2,
                    "patch": "@@ -1,2 +1,2 @@\n-old\n+new\n",
                }
            ]
            return HttpResponse(status=200, body=json.dumps(payload).encode(), headers={})
        self._meta_hits += 1
        if self._meta_hits > 1 and self._fail_refetch:
            raise TimeoutError("meta refetch down")
        if self._meta_hits == 1 or not self._changed:
            title, body = TITLE_A, BODY_A
        elif self._change_mode == "title":
            title, body = TITLE_B, BODY_A
        elif self._change_mode == "body":
            title, body = TITLE_A, BODY_B
        elif self._change_mode == "whitespace":
            # Review #8 LOW: trailing-newline / spacing-only edit — not a
            # semantic change, so the guard must not trip.
            title, body = TITLE_A + "\n", BODY_A + "  \n"
        else:
            title, body = TITLE_B, BODY_B
        return HttpResponse(
            status=200,
            body=json.dumps({"head": {"sha": SHA}, "title": title, "body": body}).encode(),
            headers={},
        )


class _FakeSocket:
    def settimeout(self, seconds):
        pass


class _FakeResponse:
    def __init__(self, body):
        self.status = 200
        self._body = body

    def read(self):
        return self._body


class _BodyRecordingConnection:
    def __init__(self, events, body):
        self._events = events
        self._body = body
        self.sock = _FakeSocket()

    def connect(self):
        pass

    def request(self, method, path, body=None, headers=None):
        self._events.append("llm")

    def getresponse(self):
        return _FakeResponse(
            json.dumps(
                {
                    "choices": [{"message": {"role": "assistant", "content": self._body}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            ).encode()
        )

    def close(self):
        pass


def _run(*, review_body, changed=True, fail_refetch=False, fanout_body=None, change_mode="both"):
    events: list = []
    ssm = _FakeSSM(_ssm_values())
    provider = ConfigProvider(
        ssm.get_parameters, clock=lambda: 1_750_000_000, allowed_endpoint_hosts=(HOST,)
    )
    creds = _Credentials(provider)

    def factory(host, port, *, timeout):
        return _BodyRecordingConnection(events, review_body)

    transport = _StatefulDiffTransport(
        events, changed=changed, fail_refetch=fail_refetch, change_mode=change_mode
    )
    kwargs: dict = {}
    if fanout_body is not None:
        kwargs["fanout_prompts"] = {
            "correctness": "c",
            "security": "s",
            "tests": "t",
            "verifier": "v",
            "synthesizer": "sy",
        }
    with patch.dict(os.environ, {"MULTI_AGENT": "1" if fanout_body is not None else "0"}):
        patcher = (
            patch.object(worker_handler, "run_fanout", lambda *a, **k: fanout_body)
            if fanout_body is not None
            else nullcontext()
        )
        with patcher:
            review = _make_review(
                envelope=Envelope(
                    envelope_version="v1",
                    event_type="pull_request",
                    action="opened",
                    repo_full_name=REPO,
                    pr_number=PR_NUMBER,
                    head_sha=SHA,
                    base_sha="00" * 20,
                    sender="octo-user",
                    delivery_guid="11111111-1111-4111-8111-111111111111",
                ),
                creds=creds,
                usage={"tokens": 0},
                diff_transport=transport,
                llm_factory=factory,
                system_prompt="SYSTEM-PROMPT",
                allowed_hosts=provider.allowed_hosts,
                clock=lambda: 1_750_000_000,
                github_transport=None,
                **kwargs,
            )
            return review(SHA, 0), transport


# Pure filter


def test_filter_drops_only_description_findings():
    text, dropped = _drop_stale_description_findings(REVIEW_WITH_BOTH)
    assert dropped == 1
    assert DESC_FINDING not in text
    assert CODE_FINDING in text
    assert text.startswith("## Summary\nAdds input validation.")


def test_filter_drop_path_renders_exact_expected_text():
    # Review #6 MEDIUM: pin the full rendered output, not just fragments —
    # separator blank lines preserved, no sentinel alongside survivors.
    text, dropped = _drop_stale_description_findings(REVIEW_WITH_BOTH)
    assert dropped == 1
    assert text == (
        "## Summary\nAdds input validation.\n\n"
        "## Findings\n" + CODE_FINDING + "\n\n"
        "## Risk Notes\nNone.\n"
    )


def test_filter_heading_case_variants_enter_filtering_mode():
    # Review #6 LOW: the model controls the heading text; variants must
    # still enter filtering mode or the race guard is bypassed.
    for heading in ("## findings", "## FINDINGS", "## Findings"):
        body = (
            "## Summary\nAdds input validation.\n\n"
            + heading
            + "\n"
            + DESC_FINDING
            + "\n"
            + CODE_FINDING
            + "\n\n## Risk Notes\nNone.\n"
        )
        text, dropped = _drop_stale_description_findings(body)
        assert dropped == 1, heading
        assert DESC_FINDING not in text, heading
        assert CODE_FINDING in text, heading


def test_filter_heading_no_space_and_suffix_variants():
    # Review #8 LOW: `##Findings` (no space) and `## Findings (note)`
    # must still enter filtering mode.
    for heading in ("##Findings", "## Findings (stale)"):
        body = (
            "## Summary\nAdds input validation.\n\n"
            + heading
            + "\n"
            + DESC_FINDING
            + "\n"
            + CODE_FINDING
            + "\n\n## Risk Notes\nNone.\n"
        )
        text, dropped = _drop_stale_description_findings(body)
        assert dropped == 1, heading
        assert DESC_FINDING not in text, heading
        assert CODE_FINDING in text, heading


def test_filter_regex_alternation_branches():
    # Review #6 LOW: every _DESC_REF_RE alternation branch must drop;
    # a refactor losing one branch would otherwise go unnoticed.
    bullets = [
        "- [MEDIUM] The pull request description is stale. Fix: update it.",
        '- [MEDIUM] The pr description claims "one file". Fix: update it.',
        "- [LOW] The description mentions two files. Fix: update it.",
        '- [LOW] The description states "one file". Fix: update it.',
        "- [MEDIUM] The pull request title changed. Fix: update it.",
        "- [MEDIUM] The pr title is wrong. Fix: update it.",
    ]
    for bullet in bullets:
        body = "## Findings\n" + bullet + "\n" + CODE_FINDING + "\n"
        text, dropped = _drop_stale_description_findings(body)
        assert dropped == 1, bullet
        assert bullet not in text, bullet
        assert CODE_FINDING in text, bullet


def test_filter_emptied_section_gets_sentinel():
    text, dropped = _drop_stale_description_findings(REVIEW_DESC_ONLY)
    assert dropped == 1
    assert EMPTY_FINDINGS_SENTINEL in text
    assert "## Findings\n" + EMPTY_FINDINGS_SENTINEL in text


def test_filter_leaves_other_sections_untouched():
    body = (
        "## Summary\nThe PR description is clear.\n\n"
        "## Findings\n" + CODE_FINDING + "\n\n"
        "## Risk Notes\nThe description covers the risk.\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 0
    assert text == body


def test_filter_ignores_bare_verb_without_description():
    body = "## Findings\n" + CODE_FINDING.replace("Missing bound", "Describes the bound") + "\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 0
    assert text == body


# End-to-end through _make_review


def test_meta_unchanged_passes_through():
    content, transport = _run(review_body=REVIEW_WITH_BOTH, changed=False)
    assert DESC_FINDING in content
    assert CODE_FINDING in content
    # The guard must actually re-fetch: initial meta GET + post-LLM re-fetch.
    assert transport._meta_hits == 2


def test_meta_whitespace_only_change_does_not_trip_guard(caplog):
    # Review #8 LOW: a trailing-newline / spacing-only edit is not a
    # semantic change — the guard must pass the text through untouched.
    with caplog.at_level("INFO", logger="worker_handler"):
        content, _ = _run(review_body=REVIEW_WITH_BOTH, changed=True, change_mode="whitespace")
    assert DESC_FINDING in content
    assert CODE_FINDING in content
    assert not [r for r in caplog.records if r.getMessage() == "pr_meta_changed_mid_review"]


def test_meta_changed_drops_description_finding():
    content, _ = _run(review_body=REVIEW_WITH_BOTH, changed=True)
    assert DESC_FINDING not in content
    assert CODE_FINDING in content
    assert "## Summary" in content


def test_meta_changed_all_dropped_yields_sentinel():
    content, _ = _run(review_body=REVIEW_DESC_ONLY, changed=True)
    assert DESC_FINDING not in content
    assert EMPTY_FINDINGS_SENTINEL in content


def test_refetch_failure_degrades_to_unfiltered(caplog):
    with caplog.at_level("WARNING", logger="worker_handler"):
        content, _ = _run(review_body=REVIEW_WITH_BOTH, fail_refetch=True)
    assert DESC_FINDING in content
    assert CODE_FINDING in content
    assert [r for r in caplog.records if r.getMessage() == "meta_refetch_unavailable"]


def test_meta_changed_emits_suppression_logs(caplog):
    with caplog.at_level("INFO", logger="worker_handler"):
        content, _ = _run(review_body=REVIEW_WITH_BOTH, changed=True)
    assert DESC_FINDING not in content
    assert [r for r in caplog.records if r.getMessage() == "pr_meta_changed_mid_review"]
    suppressed = [r for r in caplog.records if r.getMessage() == "description_findings_suppressed"]
    assert suppressed
    assert suppressed[0].dropped == 1


def test_meta_changed_no_match_emits_no_suppression_log(caplog):
    body = (
        "## Summary\nAdds input validation.\n\n"
        "## Findings\n" + CODE_FINDING + "\n\n"
        "## Risk Notes\nNone.\n"
    )
    with caplog.at_level("INFO", logger="worker_handler"):
        content, _ = _run(review_body=body, changed=True)
    assert CODE_FINDING in content
    assert [r for r in caplog.records if r.getMessage() == "pr_meta_changed_mid_review"]
    assert not [r for r in caplog.records if r.getMessage() == "description_findings_suppressed"]


def test_refetch_never_consumes_401_refresh_budget():
    # Hard invariant, pinned: the best-effort post-LLM meta re-fetch must
    # NEVER call creds.refresh_once() — the record's single 401 budget
    # belongs to the essential diff/LLM/write path. A regression adding
    # retry-with-refresh to the guard would silently spend it.
    for fail in (False, True):
        with patch.object(worker_handler._Credentials, "refresh_once", Mock()) as mock_refresh:
            _run(review_body=REVIEW_WITH_BOTH, changed=True, fail_refetch=fail)
            mock_refresh.assert_not_called()


def test_fanout_path_drops_description_finding_on_meta_change():
    # The guard is wired into the fanout branch's build_comment call too;
    # drive the real fanout path (MULTI_AGENT=1, stubbed run_fanout) with
    # changed meta and assert the wiring holds — including that the
    # re-fetch actually ran on this branch.
    content, transport = _run(
        review_body=REVIEW_WITH_BOTH, changed=True, fanout_body=REVIEW_WITH_BOTH
    )
    assert DESC_FINDING not in content
    assert CODE_FINDING in content
    assert "## Summary" in content
    assert transport._meta_hits == 2


def test_meta_title_only_change_drops_description_finding():
    content, _ = _run(review_body=REVIEW_WITH_BOTH, changed=True, change_mode="title")
    assert DESC_FINDING not in content
    assert CODE_FINDING in content


def test_meta_body_only_change_drops_description_finding():
    content, _ = _run(review_body=REVIEW_WITH_BOTH, changed=True, change_mode="body")
    assert DESC_FINDING not in content
    assert CODE_FINDING in content


def test_meta_changed_without_description_findings_passes_through():
    # No description-judging findings + confirmed meta change: the review
    # must publish with its findings intact and no sentinel.
    body = (
        "## Summary\nAdds input validation.\n\n"
        "## Findings\n" + CODE_FINDING + "\n\n"
        "## Risk Notes\nNone.\n"
    )
    content, _ = _run(review_body=body, changed=True)
    assert CODE_FINDING in content
    assert EMPTY_FINDINGS_SENTINEL not in content


# Review #1 follow-ups: block-span accounting, sentinel discipline,
# log sanitization, and the pinned over-match contract.


def test_filter_drops_continuation_lines_with_bullet():
    body = (
        "## Findings\n"
        '- [MEDIUM] `src/main.py:9` — The PR description says "one file"\n'
        "   yet the diff touches two lines of wrapped text.\n"
        "   Fix: update the description.\n" + CODE_FINDING + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "wrapped text" not in text
    assert "update the description" not in text
    assert CODE_FINDING in text


def test_filter_nonstandard_bullet_shape_blocks_sentinel():
    body = "## Findings\n" + DESC_FINDING + "\n- Plain dash bullet finding stays.\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "Plain dash bullet finding stays." in text
    assert EMPTY_FINDINGS_SENTINEL not in text


def test_filter_prose_blocks_sentinel():
    # Prose separated from the dropped block by a blank line survives (a
    # non-blank line directly abutting the bullet is a markdown lazy
    # continuation of that bullet, so it drops with the block).
    body = "## Findings\n" + DESC_FINDING + "\n\nSome analyst prose remains.\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "Some analyst prose remains." in text
    assert EMPTY_FINDINGS_SENTINEL not in text


def test_filter_loose_list_continuation_drops_with_block():
    body = (
        "## Findings\n"
        + DESC_FINDING
        + "\n\n    indented loose continuation.\n"
        + CODE_FINDING
        + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "indented loose continuation" not in text
    assert CODE_FINDING in text


def test_filter_lazy_continuation_drops_with_block():
    # Markdown lazy continuation: a non-blank line directly following the
    # bullet renders as part of that list item, so it drops with the block
    # rather than surviving as an orphaned fragment.
    body = "## Findings\n" + DESC_FINDING + "\nSome analyst prose remains.\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "Some analyst prose remains." not in text
    assert EMPTY_FINDINGS_SENTINEL in text


def test_filter_accepted_overmatch_on_title_mention():
    # Contract, pinned: on a confirmed meta change, a code finding whose
    # prose happens to contain "the title" IS dropped. Accepted over-match
    # — the guard fires only on confirmed meta change, so this is rare and
    # costs one finding; the alternative (a tighter regex) risks
    # under-matching genuine description judgments, which is the race this
    # guard exists to kill. If the regex changes, this test names the cost.
    body = (
        "## Findings\n"
        "- [LOW] `src/ui.py:3` — Component drops `the title` attribute. "
        "Fix: pass it through.\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "the title" not in text


def test_filter_bullet_shapes():
    for bullet in ("* ", "+ ", "1. ", "1) "):
        body = "## Findings\n" + bullet + 'The PR description says "one file". Fix: update it.\n'
        text, dropped = _drop_stale_description_findings(body)
        assert dropped == 1, bullet
        assert "The PR description" not in text, bullet
        assert EMPTY_FINDINGS_SENTINEL in text, bullet


def test_filter_nested_subbullets_drop_with_parent():
    body = (
        "## Findings\n"
        '- [MEDIUM] The PR description says "one file".\n'
        "  - Child detail referencing the description.\n"
        "  - Another child.\n" + CODE_FINDING + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "Child detail" not in text
    assert "Another child" not in text
    assert CODE_FINDING in text
    assert EMPTY_FINDINGS_SENTINEL not in text


def test_filter_duplicate_findings_headings_each_get_sentinel():
    body = "## Findings\n" + DESC_FINDING + "\n\n## Findings\n" + DESC_FINDING + "\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 2
    assert text.count(EMPTY_FINDINGS_SENTINEL) == 2


def test_filter_same_indent_sibling_survives():
    # Boundary pin for the block-span rule: only STRICTLY deeper-indented
    # bullets are children of a dropped block. A sibling at the same
    # indent starts a new surviving block — weakening `>` to `>=` must
    # fail this test.
    body = (
        "## Findings\n"
        '  - [MEDIUM] The PR description says "one file".\n'
        "  - [LOW] `src/main.py:12` — Missing bound. Fix: add a check.\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "Missing bound" in text
    assert "The PR description" not in text
    assert EMPTY_FINDINGS_SENTINEL not in text


def test_filter_sanitizes_logged_finding(caplog):
    # Review #6 LOW: the full [\x00-\x1f] class (ANSI escapes, NUL) must
    # not reach the log, and the 160-char truncation is pinned.
    injected = (
        '- [MEDIUM] The PR description says "x"\x1b[31mred\x00 ' + "y" * 200 + "\n"
        "Injected\nnewline.\n"
    )
    body = "## Findings\n" + injected
    with caplog.at_level("INFO", logger="worker_handler"):
        _drop_stale_description_findings(body)
    records = [r for r in caplog.records if r.getMessage() == "description_finding_suppressed"]
    assert records
    finding = records[0].finding
    assert "\n" not in finding
    assert "\r" not in finding
    assert not any(ord(c) < 0x20 for c in finding)
    assert len(finding) == 160

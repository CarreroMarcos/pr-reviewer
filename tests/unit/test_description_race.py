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

    def __init__(
        self, events, *, changed=True, fail_refetch=False, change_mode="both", fail_status=None
    ):
        self._events = events
        self._changed = changed
        self._fail_refetch = fail_refetch
        self._fail_status = fail_status
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
        # Review #9 LOW: 401 status (not just exception) must also degrade
        # without consuming the refresh budget.
        if self._meta_hits > 1 and self._fail_status is not None:
            return HttpResponse(status=self._fail_status, body=b"{}", headers={})
        if self._meta_hits == 1 or not self._changed:
            title, body = TITLE_A, BODY_A
            sha = SHA
        elif self._change_mode == "title":
            title, body = TITLE_B, BODY_A
            sha = SHA
        elif self._change_mode == "body":
            title, body = TITLE_A, BODY_B
            sha = SHA
        elif self._change_mode == "whitespace":
            # Review #8 LOW: trailing-newline / spacing-only edit — not a
            # semantic change, so the guard must not trip.
            title, body = TITLE_A + "\n", BODY_A + "  \n"
            sha = SHA
        elif self._change_mode == "sha_only":
            # Review #14 LOW: rebase/force-push mid-review — same title/body,
            # new head SHA. Must NOT trip the guard (sha is not compared).
            title, body = TITLE_A, BODY_A
            sha = SHA + "_rebased"
        else:
            title, body = TITLE_B, BODY_B
            sha = SHA
        return HttpResponse(
            status=200,
            body=json.dumps({"head": {"sha": sha}, "title": title, "body": body}).encode(),
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


def _run(
    *,
    review_body,
    changed=True,
    fail_refetch=False,
    fanout_body=None,
    change_mode="both",
    fail_status=None,
):
    events: list = []
    ssm = _FakeSSM(_ssm_values())
    provider = ConfigProvider(
        ssm.get_parameters, clock=lambda: 1_750_000_000, allowed_endpoint_hosts=(HOST,)
    )
    creds = _Credentials(provider)

    def factory(host, port, *, timeout):
        return _BodyRecordingConnection(events, review_body)

    transport = _StatefulDiffTransport(
        events,
        changed=changed,
        fail_refetch=fail_refetch,
        change_mode=change_mode,
        fail_status=fail_status,
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
    # Review #12 LOW: adds the `describes` verb and bare `the description`.
    bullets = [
        "- [MEDIUM] The pull request description is stale. Fix: update it.",
        '- [MEDIUM] The pr description claims "one file". Fix: update it.',
        "- [LOW] The description mentions two files. Fix: update it.",
        '- [LOW] The description states "one file". Fix: update it.',
        "- [LOW] The description describes one file. Fix: update.",
        "- [LOW] Check the description for accuracy. Fix: update.",
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


def test_meta_sha_only_change_does_not_trip_guard(caplog):
    # Review #14 LOW: rebase/force-push mid-review (same title/body, new
    # head SHA) must NOT trip the guard — sha is deliberately not compared.
    with caplog.at_level("INFO", logger="worker_handler"):
        content, transport = _run(
            review_body=REVIEW_WITH_BOTH, changed=True, change_mode="sha_only"
        )
    assert DESC_FINDING in content
    assert CODE_FINDING in content
    assert transport._meta_hits == 2
    assert not [r for r in caplog.records if r.getMessage() == "pr_meta_changed_mid_review"]


def test_filter_hash_number_line_is_not_a_heading():
    # Review #14 MEDIUM: `#5` (no space, CommonMark requires space) is NOT
    # a heading — it must not exit Findings mode. As a lazy continuation
    # (no blank line), it drops with the bullet block; the critical assert
    # is that CODE_FINDING after it is still scanned (Findings mode held).
    body = (
        "## Findings\n" + DESC_FINDING + "\n" + "#5 and #6 are off-by-one\n" + CODE_FINDING + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert DESC_FINDING not in text
    # Findings mode was NOT exited — CODE_FINDING was scanned and survives.
    assert CODE_FINDING in text
    assert EMPTY_FINDINGS_SENTINEL not in text


def test_filter_hash_number_after_blank_does_not_exit_findings():
    # Review #14 MEDIUM: `#5` after a blank (not a continuation) survives
    # as content and does not exit Findings mode.
    body = (
        "## Findings\n"
        + DESC_FINDING
        + "\n\n"
        + "#5 is an issue reference\n\n"
        + CODE_FINDING
        + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "#5 is an issue reference" in text
    assert CODE_FINDING in text


def test_normalize_ws_newline_to_space_is_not_a_change():
    # Review #11 LOW: _normalize_ws joins on split(), so 'a\nb' and 'a b'
    # normalize identically — a newline→space reflow is not a semantic change.
    from worker_handler import _normalize_ws

    assert _normalize_ws("line one\nline two") == _normalize_ws("line one line two")


def test_normalize_ws_tab_vs_space_is_not_a_change():
    # Review #11 LOW: tab-vs-space indentation is whitespace-only.
    from worker_handler import _normalize_ws

    assert _normalize_ws("\tindented") == _normalize_ws("    indented")


def test_normalize_ws_added_word_is_a_change():
    # Review #11 LOW: a genuinely reflowed body (word added) IS a change.
    from worker_handler import _normalize_ws

    assert _normalize_ws("line one\nline two") != _normalize_ws("line one\nline two plus")


def test_guard_no_findings_section_passes_through_byte_identical():
    # Review #9 LOW: model text with no Findings heading at all — the
    # filter must not alter it or insert a sentinel.
    body = "## Summary\nAdds input validation.\n\n## Risk Notes\nNone.\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 0
    assert text == body
    assert EMPTY_FINDINGS_SENTINEL not in text


def test_filter_heading_ends_dropped_loose_span():
    # Review #9 MEDIUM: a heading after a blank inside a dropped span ends
    # the span — the trailing section survives. The emptied Findings
    # section gets the sentinel; Risk Notes is untouched.
    body = "## Findings\n" + DESC_FINDING + "\n\n    indented loose.\n\n## Risk Notes\nNone.\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "indented loose" not in text
    assert "## Risk Notes\nNone." in text
    assert text.count(EMPTY_FINDINGS_SENTINEL) == 1


def test_filter_dropped_block_at_end_of_input_gets_sentinel():
    # Review #9 MEDIUM: dropped block at end of input — sentinel inserted
    # exactly once, trailing blank handling pinned.
    body = "## Findings\n" + DESC_FINDING + "\n\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert text.count(EMPTY_FINDINGS_SENTINEL) == 1
    # Trailing blank from input is preserved after the sentinel.
    assert text == "## Findings\n" + EMPTY_FINDINGS_SENTINEL + "\n\n"


def test_filter_indented_heading_like_line_is_not_a_section_boundary():
    # Review #9 LOW: an indented `##` line is a continuation, not a
    # heading — it must not exit Findings mode mid-section.
    body = (
        "## Findings\n"
        + DESC_FINDING
        + "\n"
        + "    ## indented code fence content\n"
        + CODE_FINDING
        + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert DESC_FINDING not in text
    assert "## indented code fence content" not in text
    assert CODE_FINDING in text


def test_filter_h3_findings_heading_enters_filtering_mode():
    # Review #10 LOW: `### Findings` (h3) must enter filtering mode —
    # _HEADING_RE matches it, so the findings check must too.
    body = "### Findings\n" + DESC_FINDING + "\n" + CODE_FINDING + "\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert DESC_FINDING not in text
    assert CODE_FINDING in text
    assert EMPTY_FINDINGS_SENTINEL not in text


def test_filter_h1_findings_heading_enters_filtering_mode():
    # Review #12 LOW: `# Findings` (h1) must also enter filtering mode.
    body = "# Findings\n" + DESC_FINDING + "\n" + CODE_FINDING + "\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert DESC_FINDING not in text
    assert CODE_FINDING in text
    assert EMPTY_FINDINGS_SENTINEL not in text


def test_filter_heading_inside_balanced_fence_is_content():
    # Review #12 MEDIUM: a `##` line inside a balanced fence is content,
    # not a section boundary — the fence must not be disabled mid-block.
    body = (
        "## Findings\n"
        + "```\n"
        + "## not a heading\n"
        + "- [LOW] example bullet\n"
        + "```\n"
        + DESC_FINDING
        + "\n"
        + CODE_FINDING
        + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "## not a heading" in text
    assert "- [LOW] example bullet" in text
    assert DESC_FINDING not in text
    assert CODE_FINDING in text


def test_filter_fenced_code_block_passes_through_verbatim():
    # Review #10 LOW: fenced code inside Findings is not scanned —
    # a fenced line matching _BULLET_RE must not be dropped.
    body = (
        "## Findings\n"
        + DESC_FINDING
        + "\n"
        + "```python\n"
        + "- [MEDIUM] example: the description says foo\n"
        + "    indented continuation\n"
        + "```\n"
        + CODE_FINDING
        + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert DESC_FINDING not in text
    # Fenced content survives byte-identical.
    assert "- [MEDIUM] example: the description says foo" in text
    assert "    indented continuation" in text
    assert CODE_FINDING in text


def test_filter_tilde_fence_passes_through_verbatim():
    # Review #11 LOW: ~~~ fences get the same verbatim treatment as ```.
    body = (
        "## Findings\n"
        + DESC_FINDING
        + "\n"
        + "~~~\n"
        + "- [MEDIUM] example: the description says foo\n"
        + "~~~\n"
        + CODE_FINDING
        + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "- [MEDIUM] example: the description says foo" in text
    assert CODE_FINDING in text


def test_filter_fence_inside_dropped_span_ends_span():
    # Review #12 LOW: a fence boundary ends the dropped span — content
    # after the fence (even indented) survives.
    body = (
        "## Findings\n"
        + DESC_FINDING
        + "\n```\ncode\n```\n"
        + "    indented after fence\n"
        + CODE_FINDING
        + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "indented after fence" in text
    assert "```\ncode\n```" in text
    assert CODE_FINDING in text
    assert EMPTY_FINDINGS_SENTINEL not in text


def test_filter_indented_fence_passes_through_verbatim():
    # Review #13 LOW: fences indented up to 3 spaces (CommonMark) also
    # toggle — their content must not be scanned as findings.
    body = (
        "## Findings\n"
        + "   ```\n"
        + "   - [MEDIUM] example: the description says foo\n"
        + "   ```\n"
        + DESC_FINDING
        + "\n"
        + CODE_FINDING
        + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "- [MEDIUM] example: the description says foo" in text
    assert DESC_FINDING not in text
    assert CODE_FINDING in text


def test_filter_unbalanced_fence_opener_passes_through_to_end():
    # Review #13 LOW: pin the deliberate design decision — an unbalanced
    # fence (opener, no closer) keeps in_fence True to end-of-input, so
    # description-judging bullets after it are NOT dropped (fail-open).
    # Corrupting output is worse than a rare model-error bypass.
    body = "## Findings\n```\n" + DESC_FINDING + "\n" + CODE_FINDING + "\n"
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 0
    assert text == body


def test_filter_orphan_fence_closer_does_not_enable_passthrough():
    # Review #13 LOW: mirror case — an orphaned closer (no opener) toggles
    # in_fence ON, so subsequent lines pass through verbatim. Pin it.
    body = "## Findings\n" + DESC_FINDING + "\n```\n" + CODE_FINDING + "\n"
    text, dropped = _drop_stale_description_findings(body)
    # DESC_FINDING dropped before the fence; CODE_FINDING after the
    # orphaned closer passes through verbatim (not scanned, not dropped).
    assert dropped == 1
    assert DESC_FINDING not in text
    assert CODE_FINDING in text


def test_filter_loose_continuation_limited_to_single_blank():
    # Review #11 LOW: 2+ blank lines end the dropped span — an unrelated
    # indented paragraph after multiple blanks survives.
    body = (
        "## Findings\n"
        + DESC_FINDING
        + "\n\n\n"
        + "    unrelated indented paragraph\n"
        + CODE_FINDING
        + "\n"
    )
    text, dropped = _drop_stale_description_findings(body)
    assert dropped == 1
    assert "unrelated indented paragraph" in text
    assert CODE_FINDING in text


def test_refetch_non_401_status_degrades_without_refresh(caplog):
    # Review #10 LOW: non-401 HTTP errors (403, 500) on the re-fetch must
    # also degrade gracefully without spending the refresh budget.
    for status in (403, 500):
        with caplog.at_level("WARNING", logger="worker_handler"):
            with patch.object(worker_handler._Credentials, "refresh_once", Mock()) as mock_refresh:
                content, _ = _run(review_body=REVIEW_WITH_BOTH, changed=True, fail_status=status)
        assert DESC_FINDING in content
        assert CODE_FINDING in content
        mock_refresh.assert_not_called()
        assert [r for r in caplog.records if r.getMessage() == "meta_refetch_unavailable"]
        caplog.clear()


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


def test_refetch_401_status_degrades_without_refresh(caplog):
    # Review #9 LOW: a 401 STATUS (not just a raised exception) on the
    # re-fetch must also degrade gracefully without spending the budget.
    with caplog.at_level("WARNING", logger="worker_handler"):
        with patch.object(worker_handler._Credentials, "refresh_once", Mock()) as mock_refresh:
            content, _ = _run(review_body=REVIEW_WITH_BOTH, changed=True, fail_status=401)
    assert DESC_FINDING in content
    assert CODE_FINDING in content
    mock_refresh.assert_not_called()
    assert [r for r in caplog.records if r.getMessage() == "meta_refetch_unavailable"]


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


def test_fanout_path_unchanged_meta_passes_through():
    # Review #11 LOW: fanout + unchanged meta — passthrough, no filtering.
    content, transport = _run(
        review_body=REVIEW_WITH_BOTH, changed=False, fanout_body=REVIEW_WITH_BOTH
    )
    assert DESC_FINDING in content
    assert CODE_FINDING in content
    assert transport._meta_hits == 2


def test_fanout_path_refetch_failure_degrades():
    # Review #11 LOW: fanout + refetch failure — degrade, publish unfiltered.
    with patch.object(worker_handler._Credentials, "refresh_once", Mock()) as mock_refresh:
        content, _ = _run(
            review_body=REVIEW_WITH_BOTH,
            changed=True,
            fail_refetch=True,
            fanout_body=REVIEW_WITH_BOTH,
        )
    assert DESC_FINDING in content
    assert CODE_FINDING in content
    mock_refresh.assert_not_called()


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

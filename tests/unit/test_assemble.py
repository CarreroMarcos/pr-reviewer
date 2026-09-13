"""T029: comment assembly contract (HLD §2.3 item 7, §2.7, §2.8; FR-001, FR-004, FR-020).

Assembly embeds the worker-injected marker (T008) and the `validate()` gate
verdict (T019) is required before any publish-ready content is returned;
§2.7 section shape with the empty-findings sentinel
("No significant issues found."); invalid content → publish refused.

Red-first: this file imports `common.assemble`, which does not exist yet,
so collection fails with ModuleNotFoundError until T033 implements it.
"""

import pytest

from common import assemble
from common.diff import DiffFile, DiffResult
from common.marker import build_marker
from common.validate import ValidationVerdict, validate_comment

REPO = "octo-org/hello-world"
PR = 9
HEAD_SHA = "bb" * 20

_SUMMARY = "Adds input validation to the signup handler with unit coverage."
_RISK = "No auth flow changes; risk is limited to malformed-payload handling."

_FINDING_A = (
    "- [HIGH] `lambda/ingress.py:88` — Missing body-size cap lets oversized "
    "payloads reach the HMAC path. Fix: reject bodies over 1 MiB before decoding."
)
_FINDING_B = (
    "- [LOW] `lambda/worker.py:12` — Stale comment references the old "
    "endpoint. Fix: update the docstring."
)


def review_body(*, findings=None) -> str:
    """Model-shaped §2.7 review content (marker never comes from the model)."""
    if findings is None:
        findings = (_FINDING_A, _FINDING_B)
    findings_block = "\n".join(findings) if findings else "No significant issues found."
    return f"## Summary\n{_SUMMARY}\n\n## Findings\n{findings_block}\n\n## Risk Notes\n{_RISK}\n"


def build(*, body=None, **kwargs):
    kwargs.setdefault("repo_full_name", REPO)
    kwargs.setdefault("pr_number", PR)
    kwargs.setdefault("review_content", review_body() if body is None else body)
    return assemble.build_comment(**kwargs)


def diff_result(*, filenames=("b.py", "a.py"), lockfile_summary="lockfiles: no changes"):
    files = tuple(
        DiffFile(filename=name, additions=10, deletions=2, patch=f"# {name}\npass\n")
        for name in filenames
    )
    return DiffResult(
        head_sha=HEAD_SHA,
        files=files,
        total_additions=10 * len(files),
        total_deletions=2 * len(files),
        total_bytes=sum(len(f.patch.encode("utf-8")) for f in files),
        truncated=False,
        lockfile_summary=lockfile_summary,
    )


# --- marker + gate verdict ---------------------------------------------------


def test_marker_embedded_first():
    result = build()
    assert result.content.startswith(build_marker(REPO, PR))


def test_gate_verdict_travels_with_publish_ready_content():
    result = build()
    assert result.verdict.ok is True
    assert result.verdict.reasons == ()


def test_section_shape_preserved_in_order():
    result = build()
    summary = result.content.index("## Summary")
    findings = result.content.index("## Findings")
    risks = result.content.index("## Risk Notes")
    assert summary < findings < risks


def test_empty_findings_sentinel_passes_through():
    result = build(body=review_body(findings=[]))
    assert result.verdict.ok is True
    assert "No significant issues found." in result.content


def test_gate_runs_before_any_publish_ready_return():
    calls = []

    def recording(content, **kwargs):
        calls.append({"content": content, "kwargs": kwargs})
        return validate_comment(content, **kwargs)

    result = assemble.build_comment(
        repo_full_name=REPO,
        pr_number=PR,
        review_content=review_body(),
        _validate=recording,
    )
    assert len(calls) == 1
    assert calls[0]["kwargs"]["repo_full_name"] == REPO
    assert calls[0]["kwargs"]["pr_number"] == PR
    assert result.verdict.ok is True


def test_deterministic_same_inputs_byte_identical():
    first = build()
    second = build()
    assert first.content == second.content


# --- truncated flag ----------------------------------------------------------


def test_truncated_note_surfaced_when_truncated():
    result = build(truncated=True)
    assert result.verdict.ok is True
    assert assemble.TRUNCATION_NOTE in result.content


def test_no_truncation_note_when_within_budget():
    result = build(truncated=False)
    assert assemble.TRUNCATION_NOTE not in result.content


def test_truncated_comment_still_passes_gate():
    result = build(body=review_body(findings=[]), truncated=True)
    assert result.verdict.ok is True
    assert "No significant issues found." in result.content
    assert assemble.TRUNCATION_NOTE in result.content


# --- DiffResult.files serialization (Gate 2: sorted-filename order) ----------


def test_render_diff_text_serializes_files_in_sorted_filename_order():
    text = assemble.render_diff_text(diff_result(filenames=("z.py", "m.py", "a.py")))
    positions = [text.index(name) for name in ("a.py", "m.py", "z.py")]
    assert positions == sorted(positions)


def test_render_diff_text_byte_identical_regardless_of_input_order():
    first = assemble.render_diff_text(diff_result(filenames=("b.py", "a.py")))
    second = assemble.render_diff_text(diff_result(filenames=("a.py", "b.py")))
    assert first == second


def test_render_diff_text_includes_lockfile_summary():
    summary = "lockfiles: 1 files (+120/-45 lines): uv.lock: +120/-45 lines"
    text = assemble.render_diff_text(diff_result(lockfile_summary=summary))
    assert summary in text


# --- publish refusal ---------------------------------------------------------


def test_invalid_content_publish_refused():
    with pytest.raises(assemble.AssembleError) as exc_info:
        build(body="plain text without marker or sections")
    assert exc_info.value.verdict.ok is False
    assert "missing_sections" in exc_info.value.verdict.reasons


def test_empty_review_content_publish_refused():
    with pytest.raises(assemble.AssembleError) as exc_info:
        build(body="   \n  ")
    assert exc_info.value.verdict.ok is False


def test_failing_validator_refuses_even_valid_content():
    def failing(content, **kwargs):
        return ValidationVerdict(False, ("missing_sections",))

    with pytest.raises(assemble.AssembleError) as exc_info:
        assemble.build_comment(
            repo_full_name=REPO,
            pr_number=PR,
            review_content=review_body(),
            _validate=failing,
        )
    assert exc_info.value.verdict.ok is False
    assert exc_info.value.verdict.reasons == ("missing_sections",)


def test_no_publish_ready_content_returned_on_refusal():
    seen = []
    try:
        build(body="not a review")
    except assemble.AssembleError as exc:
        seen.append(exc)
    assert len(seen) == 1
    assert seen[0].verdict.ok is False


# --- marker-spoof propagation (boundary 7: no self-referential publish) -------


def test_spoofed_model_content_refuses_publication_with_marker_spoofed():
    spoofed = review_body() + f"\n{build_marker(REPO, PR)}\n"
    with pytest.raises(assemble.AssembleError) as exc_info:
        build(body=spoofed)
    assert exc_info.value.verdict.ok is False
    assert "marker_spoofed" in exc_info.value.verdict.reasons


def test_cross_repo_spoof_in_model_content_refuses_publication():
    spoofed = review_body() + f"\n{build_marker('evil-org/evil', 666)}\n"
    with pytest.raises(assemble.AssembleError) as exc_info:
        build(body=spoofed)
    assert exc_info.value.verdict.ok is False
    assert "marker_spoofed" in exc_info.value.verdict.reasons


# --- AssembleError contract (boundary 7; T034 wiring, test-side pin) -----------
# Per the HLD §2.3 item 8 validation row, assembled-comment refusal is the
# complete-and-alert (non-retryable) class: `AssembleError` is a `ValueError`
# carrying the failing verdict — never a transient/queue-retry signal. The
# worker (T034/T054) owns the wiring; these tests pin the emission side so
# any reclassification is a conscious diff.


def test_assemble_error_is_value_error_not_transient():
    from common.diff import DiffError
    from common.llm import LlmError

    assert issubclass(assemble.AssembleError, ValueError)
    assert not issubclass(assemble.AssembleError, LlmError)
    assert assemble.AssembleError is not DiffError


def test_assemble_error_carries_verdict_and_codes_in_message():
    with pytest.raises(assemble.AssembleError) as exc_info:
        build(body="plain text without marker or sections")
    exc = exc_info.value
    assert isinstance(exc, ValueError)
    assert exc.verdict.ok is False
    assert isinstance(exc.verdict.reasons, tuple)
    assert "missing_sections" in exc.verdict.reasons
    assert "missing_sections" in str(exc)


def test_whitespace_refusal_is_non_retryable_validation_failure():
    with pytest.raises(assemble.AssembleError) as exc_info:
        build(body="   \n  ")
    assert exc_info.value.verdict.ok is False
    assert "missing_sections" in exc_info.value.verdict.reasons

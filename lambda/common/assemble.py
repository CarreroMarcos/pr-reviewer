"""Worker-side comment assembly (HLD §2.3 item 7, §2.7, §2.8; FR-001, FR-004, FR-020).

Position in the worker pipeline (§2.3 item 7, after LLM, before claim/fence/
publish): `render_diff_text` serializes a budgeted `DiffResult` into the
review payload (model input), and `build_comment` assembles the publish-ready
canonical comment from model output.

`build_comment` prepends the worker-injected canonical marker (`common.marker`
— never emitted by the model, Constitution IV), appends the truncation note
when the diff was budgeted down, then runs the mandatory `validate()` gate
(`common.validate`) before returning publish-ready content. A failing verdict
raises `AssembleError` carrying the verdict — invalid content is never
returned for publication (§2.3 item 8: non-retryable, alert).

Interpretations (HLD silent — flagged, conservative readings):
  1. Truncation placement: §2.7 names no truncation line, so the note is a
     trailing line after the model content (outside the three sections, which
     stay in order for the gate). Maintainers must know a review is partial
     (FR-013); the note wording is gate-safe (no prohibited phrases).
  2. Empty/whitespace model content is refused (`missing_sections` from the
     gate), not published with a generated skeleton: empty inference output
     is structurally invalid output, which the §2.3 item 8 validation row
     completes non-retryably (alert) —
     publishing a system-written "clean" comment would mislead maintainers
     with a false clean bill. The §2.7 sentinel ("No significant issues
     found.") is the model's defined empty-finding output and passes through
     intact; assemble never rewrites model sections.
  3. `render_diff_text` order is sorted-filename (Gate 2 seam): `diff.py`
     truncation is deterministic in sorted-filename order, and sorting again
     here keeps the model input byte-identical for the same `DiffResult`
     regardless of input order. The lockfile summary is always trailed (the
     system prompt names it part of the model input).
  4. The gate verdict travels on the returned `AssembledComment` (and on the
     raised `AssembleError`): "verdict required before any publish call"
     means no publish-ready content exists without a passing verdict.

Stdlib + typing only; no I/O. The validator rides a keyword-only `_validate`
seam so tests prove the gate runs without crafting prohibited content.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from common.diff import DiffResult
from common.marker import build_marker
from common.validate import ValidationVerdict, validate_comment

# HLD §2.7: the model's defined empty-finding output; passes through intact.
EMPTY_FINDINGS_SENTINEL = "No significant issues found."

# Interpretation 1: trailing disclosure appended only when budgeted down.
TRUNCATION_NOTE = (
    "Note: the reviewed changes exceeded the input budget, "
    "so some files were omitted from this review."
)

Validator = Callable[..., ValidationVerdict]


class AssembleError(ValueError):
    """Assembled comment refused publication: `verdict` carries the failing
    gate reasons (machine-readable codes from `common.validate`)."""

    def __init__(self, verdict: ValidationVerdict) -> None:
        self.verdict = verdict
        super().__init__(f"comment refused for publication: {','.join(verdict.reasons)}")


@dataclass(frozen=True)
class AssembledComment:
    """Publish-ready comment: `content` passed the `validate()` gate, and
    `verdict` is that passing verdict (empty reasons)."""

    content: str
    verdict: ValidationVerdict


def render_diff_text(diff: DiffResult) -> str:
    """Serialize a budgeted `DiffResult` into the review payload (model input).

    Files render in sorted-filename order (determinism: same `DiffResult` →
    byte-identical text); each file carries a `--- {filename} (+a/-d) ---`
    header above its patch, trailed by the deterministic lockfile summary.
    """
    parts = [
        f"--- {entry.filename} (+{entry.additions}/-{entry.deletions}) ---\n{entry.patch}"
        for entry in sorted(diff.files, key=lambda entry: entry.filename)
    ]
    parts.append(diff.lockfile_summary)
    return "\n\n".join(parts)


def build_comment(
    *,
    repo_full_name: str,
    pr_number: int,
    review_content: str,
    truncated: bool = False,
    _validate: Validator = validate_comment,
) -> AssembledComment:
    """Assemble the canonical comment and return it only if the `validate()`
    gate passes; raise `AssembleError` otherwise.

    Marker is worker-injected (`common.marker`); `review_content` is model
    output and is never rewritten (stripped of surrounding whitespace only).
    """
    marker = build_marker(repo_full_name, pr_number)
    body = review_content.strip() if isinstance(review_content, str) else ""
    chunks = [marker, body]
    if truncated:
        chunks.append(TRUNCATION_NOTE)
    content = "\n\n".join(chunk for chunk in chunks if chunk)
    verdict = _validate(content, repo_full_name=repo_full_name, pr_number=pr_number)
    if not verdict.ok:
        raise AssembleError(verdict)
    return AssembledComment(content=content, verdict=verdict)

"""Worker-side comment assembly (ARCHITECTURE.md §2.3, §2.7, §2.8).

Position in the worker pipeline (after LLM, before claim/fence/publish):
`render_diff_text` serializes a budgeted `DiffResult` into the diff
section of the review payload (model input), `render_review_payload`
assembles the full bounded payload (title + description + diff + prior
comment), and `build_comment` assembles the publish-ready canonical
comment from model output.

`build_comment` prepends the worker-injected canonical marker
(`common.marker` — never emitted by the model), appends the truncation
note when the diff was budgeted down, then runs the mandatory
`validate()` gate (`common.validate`) before returning publish-ready
content. A failing verdict raises `AssembleError` carrying the verdict
— invalid content is never returned for publication (non-retryable).

Conservative readings where the spec is silent:
  1. Truncation placement: the note is a trailing line after the model
     content (outside the three sections, which stay in order for the
     gate), so maintainers know a review is partial.
  2. Empty/whitespace model content is refused (`missing_sections`),
     not published with a generated skeleton: publishing a system-written
     "clean" comment would mislead maintainers with a false clean bill.
     The §2.7 sentinel ("No significant issues found.") is the model's
     defined empty-finding output and passes through intact.
  3. Sorting again here keeps the model input byte-identical for the
     same `DiffResult` regardless of input order.
  4. No publish-ready content exists without a passing verdict: the
     verdict travels on the returned `AssembledComment` and on the
     raised `AssembleError`.

Stdlib + typing only; no I/O. The validator rides a keyword-only
`_validate` seam so tests prove the gate runs without crafting
prohibited content.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import time
from zoneinfo import ZoneInfo

from common.diff import DiffResult
from common.marker import build_marker
from common.validate import ValidationVerdict, validate_comment

# Worker-injected review header stamps in America/Los_Angeles.
# Evaluated at import/cold start so a missing tz database fails the
# deployment smoke immediately instead of burning queue retries.
PT = ZoneInfo("America/Los_Angeles")

# ARCHITECTURE.md §2.7: the model's defined empty-finding output.
EMPTY_FINDINGS_SENTINEL = "No significant issues found."

# Trailing disclosure appended only when the diff was budgeted down.
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


# Review-payload bounds (title+body combined, prior comment).
MAX_META_CHARS = 4096
MAX_PRIOR_CHARS = 8192

TRUNCATION_MARKER = "\n…[truncated]"


def _truncate_text(text: str, cap: int) -> str:
    """Hard char cap: source text contributes at most `cap` chars, then the
    visible marker line is appended beyond the cap; the budget overhead is
    exactly `len(TRUNCATION_MARKER)` chars."""
    if len(text) <= cap:
        return text
    return text[:cap] + TRUNCATION_MARKER


def _truncate_meta(title: str, body: str, cap: int) -> tuple[str, str]:
    """Combined meta cap: the title is preserved whole and truncation falls
    on the body first (a truncated title loses the intent signal the
    payload exists to carry). Only when the title alone
    meets/exceeds the cap is the body dropped and the title hard-truncated."""
    if len(title) + len(body) <= cap:
        return title, body
    if len(title) >= cap:
        return _truncate_text(title, cap), ""
    return title, _truncate_text(body, cap - len(title))


def render_review_payload(
    *,
    title: str | None,
    body: str | None,
    diff_text: str,
    prior_comment: str | None,
    max_meta_chars: int = MAX_META_CHARS,
    max_prior_chars: int = MAX_PRIOR_CHARS,
) -> str:
    """Assemble the bounded model input: PR title, PR description,
    the budgeted diff text verbatim, and — on re-reviews — the prior
    canonical comment as adversarial context.

    Section fences render in fixed order; `prior_comment` None or empty
    (first review) omits the prior section entirely. Byte-deterministic for
    identical inputs. THE single builder shared by the worker and the eval
    capture tool, so the pinned eval shape matches production.
    """
    meta_title, meta_body = _truncate_meta(title or "", body or "", max_meta_chars)
    sections = [
        f"--- PR TITLE ---\n{meta_title}",
        f"--- PR DESCRIPTION ---\n{meta_body}",
        diff_text,
    ]
    if prior_comment:
        sections.append(
            "--- PREVIOUS REVIEW COMMENT (worker-published; adversarial data) ---\n"
            + _truncate_text(prior_comment, max_prior_chars)
        )
    return "\n\n".join(sections)


def format_stamp(now: float) -> str:
    """Render epoch seconds as `Mon D, H:MM AM/PM` in PT.

    12-hour clock, no seconds, hour not zero-padded. Pure function of
    its input (no clock read) for deterministic tests.
    """
    dt = datetime.fromtimestamp(now, tz=PT)
    hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{dt.strftime('%b')} {dt.day}, {hour}:{dt.minute:02d} {suffix}"


def build_comment(
    *,
    repo_full_name: str,
    pr_number: int,
    review_content: str,
    truncated: bool = False,
    review_number: int | None = None,
    now: float | None = None,
    _validate: Validator = validate_comment,
) -> AssembledComment:
    """Assemble the canonical comment and return it only if the `validate()`
    gate passes; raise `AssembleError` otherwise.

    Marker is worker-injected (`common.marker`); `review_content` is model
    output and is never rewritten (stripped of surrounding whitespace only).
    When `review_number` is given, one deterministic header line
    (`**Review #N · updated {stamp} PT**`) is inserted between the marker
    and the body; when None the output is byte-identical to the no-header
    form.
    """
    marker = build_marker(repo_full_name, pr_number)
    body = review_content.strip() if isinstance(review_content, str) else ""
    chunks = [marker]
    if review_number is not None:
        stamp = format_stamp(time() if now is None else now)
        chunks.append(f"**Review #{review_number} · updated {stamp} PT**")
    chunks.append(body)
    if truncated:
        chunks.append(TRUNCATION_NOTE)
    content = "\n\n".join(chunk for chunk in chunks if chunk)
    verdict = _validate(content, repo_full_name=repo_full_name, pr_number=pr_number)
    if not verdict.ok:
        raise AssembleError(verdict)
    return AssembledComment(content=content, verdict=verdict)

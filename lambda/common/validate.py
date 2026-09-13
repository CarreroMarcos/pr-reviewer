"""Publication validation gate (HLD §2.3 item 7, §2.7; FR-020, FR-025).

Validates the worker-assembled canonical comment before any publish call
(`specs/001-pr-reviewer/contracts/canonical-comment.md`). Consumes the Model
I/O contract defined in `prompts/system_prompt.md` (`prompt_version` v1):
the canonical marker is present (worker-injected via `common.marker`, never
emitted by the model), length is bounded, expected sections are present in
order, and prohibited content plus the prompt canary are absent.

Returns a typed `ValidationVerdict` with machine-readable reason codes.
Invalid content is never published (§2.3 item 8: non-retryable, alert).

Reason codes: `missing_marker`, `marker_spoofed`, `not_text`, `over_length`,
`missing_sections`, `too_many_findings`, `credential_like`, `hidden_html`,
`control_directive`, `canary_leaked`, `mention`, `external_media`,
`approval_verdict`.

Pure stdlib, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from common.marker import build_marker

# Mirrors the `prompt_version` header in `prompts/system_prompt.md`; kept in
# sync manually. Any change reruns the pinned model evaluation set (§4.4 item 4).
PROMPT_VERSION = "v1"

# Tripwire embedded verbatim in `prompts/system_prompt.md`: output containing
# it proves prompt leakage into review content — rejected, never published.
CANARY_SUBSTRING = "pr-reviewer-canary-9f3a7c2e1b4d6a8e"

# HLD §2.7: finding count ≤ the `max_findings` configuration parameter
# (default 20). `AppConfig` (common.config) carries only SSM hydration values,
# so the bound travels as an explicit parameter defaulting to 20 here.
DEFAULT_MAX_FINDINGS = 20

# GitHub comment ceiling; the HLD bounds output via the worker-side
# `max_output_tokens` config parameter, and this character bound is the
# defense-in-depth publish-side backstop for the assembled comment.
MAX_COMMENT_CHARS = 65536

_SECTION_PATTERNS = (
    re.compile(r"^##\s+Summary\s*$", re.MULTILINE),
    re.compile(r"^##\s+Findings\s*$", re.MULTILINE),
    re.compile(r"^##\s+Risk Notes\s*$", re.MULTILINE),
)

_FINDING_BULLET_RE = re.compile(r"^\s*[-*]\s+\S", re.MULTILINE)

_CREDENTIAL_RES = (
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"gho_[A-Za-z0-9]{8,}"),
    re.compile(r"AKIA[0-9A-Z]{8,}"),
    re.compile(r"BEGIN [A-Z][A-Z ]*PRIVATE KEY"),
    re.compile(r"xox[bpars]-[A-Za-z0-9-]{6,}"),
    re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*\S+"),
    re.compile(r"(?i)password\s*[:=]\s*\S+"),
)

_HTML_TAG_RES = tuple(
    re.compile(rf"<{tag}\b", re.IGNORECASE)
    for tag in ("script", "iframe", "object", "embed", "form", "link", "meta", "style", "img")
)
_EVENT_HANDLER_RE = re.compile(r"""\son(?:load|click|error|mouseover|focus)\s*=""", re.IGNORECASE)

# Repository content must not establish control-plane state (§5.3): heuristic
# tripwires for instruction-override phrasing, not an exhaustive filter.
_CONTROL_DIRECTIVE_RES = (
    re.compile(r"(?i)ignore\s+(all\s+)?previous\s+instructions"),
    re.compile(r"(?i)disregard\s+.*instructions"),
    re.compile(r"(?i)^\s*system\s*:", re.MULTILINE),
    re.compile(r"(?i)\[system\]"),
    re.compile(r"(?i)you are now (a|an|the)\s"),
    re.compile(r"(?i)new\s+(system\s+)?instructions?\b"),
    re.compile(r"(?i)override\s+(the\s+)?(system|review|policy)\b"),
)

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")

_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\(\s*https?://", re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<img\b", re.IGNORECASE)

# Marker-spoof tripwire (§9: model output is untrusted): the worker injects
# exactly one canonical marker, so any further `pr-reviewer:canonical`
# string is model-emitted spoof. Matching is case-insensitive over
# invisible-char-stripped text, so case/zero-width/whitespace evasions and
# truncated prefixes carrying the core are refused, not laundered.
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\ufeff\u00ad]")
_MARKER_SPOOF_RE = re.compile(r"pr-reviewer\s*:\s*canonical", re.IGNORECASE)

_APPROVAL_PHRASES = (
    "safe to merge",
    "ready to merge",
    "ok to merge",
    "okay to merge",
    "good to merge",
    "approv",
    "lgtm",
    "looks good to me",
    "ship it",
    "merge this",
    "merge when ready",
)


@dataclass(frozen=True)
class ValidationVerdict:
    """Typed gate outcome: `ok` gates publication; `reasons` carries the
    machine-readable rejection codes (empty when accepted)."""

    ok: bool
    reasons: tuple[str, ...] = ()


def _sections_in_order(content: str) -> tuple[int, int] | None:
    """Return the (findings_start, risk_start) offsets when all three section
    headings are present in order, else None."""
    positions = [pattern.search(content) for pattern in _SECTION_PATTERNS]
    if any(match is None for match in positions):
        return None
    spans = [match.span() for match in positions if match is not None]
    if not (spans[0][0] < spans[1][0] < spans[2][0]):
        return None
    return (spans[1][1], spans[2][0])


def validate_comment(
    content: str,
    *,
    repo_full_name: str,
    pr_number: int,
    max_length: int = MAX_COMMENT_CHARS,
    max_findings: int = DEFAULT_MAX_FINDINGS,
) -> ValidationVerdict:
    """Validate a worker-assembled canonical comment for publication."""
    if not isinstance(content, str):
        return ValidationVerdict(False, ("not_text",))

    reasons: list[str] = []
    marker = build_marker(repo_full_name, pr_number)

    if marker not in content:
        reasons.append("missing_marker")

    # Exactly one legitimate occurrence is excused (the worker-injected
    # marker); anything marker-shaped left in the remainder is spoof.
    remainder = content.replace(marker, "", 1)
    if _MARKER_SPOOF_RE.search(_INVISIBLE_RE.sub("", remainder)):
        reasons.append("marker_spoofed")

    if len(content) > max_length:
        reasons.append("over_length")

    section_span = _sections_in_order(content)
    if section_span is None:
        reasons.append("missing_sections")
    else:
        findings_region = content[section_span[0] : section_span[1]]
        if len(_FINDING_BULLET_RE.findall(findings_region)) > max_findings:
            reasons.append("too_many_findings")

    if any(pattern.search(content) for pattern in _CREDENTIAL_RES):
        reasons.append("credential_like")

    without_marker = content.replace(marker, "")
    if (
        any(pattern.search(content) for pattern in _HTML_TAG_RES)
        or _EVENT_HANDLER_RE.search(content)
        or "<!--" in without_marker
        or "-->" in without_marker
    ):
        reasons.append("hidden_html")

    if any(pattern.search(content) for pattern in _CONTROL_DIRECTIVE_RES):
        reasons.append("control_directive")

    if CANARY_SUBSTRING in content:
        reasons.append("canary_leaked")

    # GitHub does not notify @mentions inside code formatting, so mentions in
    # fenced blocks, inline code spans, and email addresses are not mentions.
    text_for_mentions = _EMAIL_RE.sub("", _INLINE_CODE_RE.sub("", _CODE_BLOCK_RE.sub("", content)))
    if _MENTION_RE.search(text_for_mentions):
        reasons.append("mention")

    if _IMAGE_MD_RE.search(content) or _IMG_TAG_RE.search(content):
        reasons.append("external_media")

    lowered = content.lower()
    if any(phrase in lowered for phrase in _APPROVAL_PHRASES):
        reasons.append("approval_verdict")

    return ValidationVerdict(ok=not reasons, reasons=tuple(reasons))

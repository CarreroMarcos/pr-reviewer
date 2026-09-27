"""Accepted-residuals parser (HLD-004 D7).

TOLERANT by contract — the opposite of `common.findings`' fail-closed
validation: the operator-curated `docs/accepted-residuals.md` may carry
titles, prose, and comments, and any malformed line is skipped WITH a
record, never raised, never silently dropped. There is no error type:
nothing here is fatal.

File shape (representative; the file does not exist yet — missing file
behaves as empty, and specialists receive `residuals = []` indefinitely
until it is populated):

    # Accepted residuals
    ## owner/repo#42
    - One accepted-residual line each.
    - Another settled nit.

Skip records (`SkippedLine`: 1-based `line_no`, raw `line`, `reason`)
are DATA for the caller: fan-out turns them into events (emitting events
is T015's duty — this module never imports the events layer). Reasons:
`bad_section` (a `##` header that is not exactly `repo#pr`),
`orphan_bullet` (a bullet with no current valid section, including after
a bad header), `empty_bullet`, `bad_bullet` (marker not followed by
whitespace, e.g. `-item`). Blank lines, `#` titles, `###` sub-headers,
and other prose are formatting, not content, and are ignored.

Pure stdlib, no I/O except the explicit `load_accepted_residuals`
reader, no boto3 import. The file ships in the Lambda bundle: no
test-only code paths, no filesystem writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PathLike = str | Path

_SECTION_RE = re.compile(r"^##[ \t]+([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)#([0-9]+)[ \t\r]*$")
_EXACT_SECTION_RE = re.compile(r"^##(?!#)")
_BULLET_RE = re.compile(r"^[ \t]*[-*+](.*)$")


@dataclass(frozen=True)
class SkippedLine:
    """One malformed line, recorded for the caller to surface as an
    event. `line_no` is 1-based; `line` is the raw line; `reason` is
    `bad_section` | `orphan_bullet` | `empty_bullet` | `bad_bullet`."""

    line_no: int
    line: str
    reason: str


@dataclass(frozen=True)
class ParseResult:
    """Parsed residuals: `repo#pr` → accepted lines, plus every skipped
    line in file order. Fresh containers per parse — callers own them."""

    residuals: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[SkippedLine] = field(default_factory=list)


def parse_accepted_residuals(text: str) -> ParseResult:
    """Parse accepted-residuals markdown into sections and skip records.

    Sections merge on repeat keys; `-`, `*`, `+` bullets (any indent)
    are accepted; anything malformed is recorded, never raised.
    """
    if not isinstance(text, str):
        raise TypeError(f"accepted-residuals text must be str, got {type(text).__name__}")
    residuals: dict[str, list[str]] = {}
    skipped: list[SkippedLine] = []
    current: str | None = None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for line_no, line in enumerate(normalized.split("\n"), start=1):
        if not line.strip():
            continue
        section = _SECTION_RE.match(line)
        if section is not None:
            number = int(section.group(2))
            if number < 1:
                skipped.append(SkippedLine(line_no, line, "bad_section"))
                current = None
            else:
                current = f"{section.group(1)}#{number}"
                residuals.setdefault(current, [])
            continue
        if _EXACT_SECTION_RE.match(line) is not None:
            skipped.append(SkippedLine(line_no, line, "bad_section"))
            current = None
            continue
        bullet = _BULLET_RE.match(line)
        if bullet is None:
            continue
        rest = bullet.group(1)
        if current is None:
            skipped.append(SkippedLine(line_no, line, "orphan_bullet"))
        elif not rest.strip():
            skipped.append(SkippedLine(line_no, line, "empty_bullet"))
        elif rest[0] not in (" ", "\t"):
            skipped.append(SkippedLine(line_no, line, "bad_bullet"))
        else:
            residuals[current].append(rest.strip())
    return ParseResult(residuals=residuals, skipped=skipped)


def load_accepted_residuals(path: PathLike) -> ParseResult:
    """Read + parse the committed residuals file. Missing, unreadable,
    or non-UTF-8 files behave as empty (specialists receive
    `residuals = []`) — never fatal."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ParseResult()
    return parse_accepted_residuals(text)


def residuals_for(result: ParseResult, repo_full_name: str, pr_number: int) -> list[str]:
    """Accepted lines for one PR; `[]` when the section is absent. The
    returned list is a copy — mutating it never affects parsed state."""
    return list(result.residuals.get(f"{repo_full_name}#{pr_number}", []))

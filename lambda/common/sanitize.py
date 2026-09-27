"""Span-preserving markdown sanitization (HLD-004 §5 item 2).

Naive neutralization of `<...>` or `[...]` corrupts generic type
signatures (`List<T>`, `Dict[str, Any]`), JSX tags, and code blocks, so
this sanitizer works in three phases: (1) extract fenced code blocks and
inline backtick spans into indexed `\x00CODE_SPAN_N\x00` placeholders,
(2) neutralize exactly four prose constructs — `![img](url)` →
`[Image: img] (url)`, `[link](url)` → `link (url)`, `<http...>` (bare
angle-bracket autolink) → `` `<http...>` `` — and (3) re-substitute the
original spans byte-identical.

Collision discipline: the `\x00` framing is the anti-collision mechanism
— NUL never occurs in legitimate finding/markdown text (control
characters are stripped at the envelope boundary, HLD §2.1) — so any
`\x00` in input fails closed with `SanitizeError` instead of risking a
forged placeholder hijacking the stash. The stash is per-call; restore
is exact-string replacement, so spans always come back byte-identical.

Ordering (spec-list order): fences, then inline spans, then images,
links, autolinks. Images precede links so `![a](b)` is never eaten as a
link; link/image text tolerates one nested bracket pair so linked images
(`[![b](c)](d)`) still neutralize instead of surviving as live links.
Placeholders carry no bracket/paren/angle characters, so transforms can
never touch them.

Non-goals (untouched by design): tilde fences, indented code blocks,
reference-style links, and bare URLs without angle brackets are not
spec'd constructs and pass through; an unclosed fence runs to end of
text (CommonMark) while an unclosed inline backtick stays literal.

Pure stdlib, no I/O, no boto3 import.
"""

from __future__ import annotations

import re
from typing import Any

_FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,})([^`]*)$")

# Link/image text tolerates one nested bracket pair (see module docstring).
_NESTED_TEXT = r"(?:[^\[\]]|\[[^\[\]]*\])*"
_IMAGE_RE = re.compile(r"!\[(" + _NESTED_TEXT + r")\]\(([^)]*)\)")
_LINK_RE = re.compile(r"\[(" + _NESTED_TEXT + r")\]\(([^)]*)\)")
# Bare angle-bracket autolink: `<http` (any case) + ≥1 non-space,
# non-angle char. `<http>` alone, generics, JSX, and spaced text stay.
_AUTOLINK_RE = re.compile(r"<[Hh][Tt][Tt][Pp]([^<>\s]+)>")


class SanitizeError(ValueError):
    """Typed sanitizer rejection: `field` is always `"text"`, `reason` is
    `bad_type` (non-string input) or `nul_byte` (NUL present — the
    placeholder-collision domain; fail closed, never silently proceed)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid sanitize input: {field}: {reason}")


def _placeholder(index: int) -> str:
    return f"\x00CODE_SPAN_{index}\x00"


def _extract_fenced_blocks(text: str, stash: list[str]) -> str:
    """Stash backtick fenced blocks; return prose with placeholders.

    Opening line: optional indent + run of ≥3 backticks + backtick-free
    info string. Closing line: optional indent + run of ≥ opening length
    and nothing else. Unclosed fences run to end of text. Slicing is by
    exact char offsets, so unstashed prose is byte-preserved.
    """
    lines = text.split("\n")
    starts: list[int] = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1
    chunks: list[str] = []
    cursor = 0
    i = 0
    total = len(lines)
    while i < total:
        opened = _FENCE_OPEN_RE.match(lines[i])
        if opened is None:
            i += 1
            continue
        run_len = len(opened.group(1))
        close_re = re.compile(rf"^[ \t]*`{{{run_len},}}[ \t]*$")
        j = i + 1
        while j < total and close_re.match(lines[j]) is None:
            j += 1
        end = starts[j] + len(lines[j]) if j < total else len(text)
        chunks.append(text[cursor : starts[i]])
        chunks.append(_placeholder(len(stash)))
        stash.append(text[starts[i] : end])
        cursor = end
        i = j + 1 if j < total else total
    chunks.append(text[cursor:])
    return "".join(chunks)


def _extract_inline_spans(text: str, stash: list[str]) -> str:
    """Stash CommonMark inline code spans; return prose with placeholders.

    Maximal backtick runs open; the closer is the next maximal run of
    exactly the same length (so `` ``a `b` `` `` nests correctly). An
    opener with no closer stays literal and rescanning continues after
    its first backtick.
    """
    out: list[str] = []
    i = 0
    total = len(text)
    while i < total:
        if text[i] != "`":
            out.append(text[i])
            i += 1
            continue
        j = i
        while j < total and text[j] == "`":
            j += 1
        run = j - i
        found = -1
        k = j
        while k < total:
            if text[k] != "`":
                k += 1
                continue
            m = k
            while m < total and text[m] == "`":
                m += 1
            if m - k == run:
                found = k
                break
            k = m
        if found == -1:
            out.append("`")
            i += 1
            continue
        out.append(_placeholder(len(stash)))
        stash.append(text[i : found + run])
        i = found + run
    return "".join(out)


def sanitize(text: Any) -> str:
    """Sanitize model-controlled markdown for the publish path.

    Total on strings: always returns a string, never raises for
    well-formed text. Raises `SanitizeError` only for non-string input
    or NUL-containing input (the placeholder-collision domain).
    """
    if not isinstance(text, str):
        raise SanitizeError("text", "bad_type")
    if "\x00" in text:
        raise SanitizeError("text", "nul_byte")
    stash: list[str] = []
    prose = _extract_fenced_blocks(text, stash)
    prose = _extract_inline_spans(prose, stash)
    prose = _IMAGE_RE.sub(r"[Image: \1] (\2)", prose)
    prose = _LINK_RE.sub(r"\1 (\2)", prose)
    prose = _AUTOLINK_RE.sub(lambda match: f"`{match.group(0)}`", prose)
    for index, span in enumerate(stash):
        prose = prose.replace(_placeholder(index), span)
    return prose

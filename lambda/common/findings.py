"""Candidate-finding schema layer (HLD-004 §6).

Two duties, kept separate:

1. Closed-schema PARSE (`parse_candidate_findings`): specialist output
   must be exactly `{"findings": [...]}` with exactly the §6 item keys —
   unknown keys are rejected at BOTH levels (`additionalProperties`), as
   are wrong enums, `title` > 120, and `line_start`/`line_end` < 1. These
   are REJECTIONS: malformed model output never flows downstream.
2. Post-image CLAMP (`clamp_to_post_image`): coordinates are 1-based
   POST-IMAGE lines; values beyond the file length are clamped to it and
   COUNTED (`ClampResult.clamped_n`, per clamped coordinate) — never
   rejected, never silent. The count is data `run_fanout` surfaces as
   `coordinates_clamped_n`; flags never travel inside finding JSON.

Layering: `common.events` carries findings arrays as opaque JSON lists;
this module validates the ITEMS. `candidate_id` (`"{specialty}:{index}"`)
is assigned by `run_fanout` post-wave — specialists never see IDs, so
`candidate_id` is an unknown key in specialist output; this module
defines/validates the ID FORMAT only (`make_candidate_id`,
`parse_candidate_id` — shape, not wave membership, which only
`run_fanout` knows).

Pure stdlib, no I/O, no boto3 import.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FINDING_FIELDS = frozenset(
    {
        "file_path",
        "line_start",
        "line_end",
        "title",
        "description",
        "suggested_fix",
        "severity",
        "category",
    }
)

# Order missing-key reports deterministically (schema-table order).
_REQUIRED_KEYS = (
    "file_path",
    "line_start",
    "line_end",
    "title",
    "description",
    "suggested_fix",
    "severity",
    "category",
)

SEVERITIES = frozenset({"HIGH", "MEDIUM", "LOW"})
CATEGORIES = frozenset({"correctness", "security", "tests"})

TITLE_MAX_CHARS = 120


class FindingsError(ValueError):
    """Typed finding rejection: `field` names the offending field
    (`"payload"` for whole-payload shape errors, `"findings"` for
    item-shape errors, `"candidate_id"` for ID-shape errors),
    `reason` is a machine-readable code (`not_object`, `missing`,
    `bad_fields`, `bad_list`, `bad_item`, `bad_type`, `bad_enum`,
    `bad_length`, `bad_line`, `bad_specialty`, `bad_index`,
    `bad_candidate_id`)."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"invalid finding: {field}: {reason}")


@dataclass(frozen=True)
class ClampResult:
    """Post-image clamp outcome: the (possibly adjusted) findings plus
    the count of clamped COORDINATES (each `line_start`/`line_end` value
    moved counts one). `clamped_n == 0` means nothing moved."""

    findings: list[dict[str, Any]]
    clamped_n: int


def _clean_line(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FindingsError(field, "bad_line")
    return value


def _clean_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise FindingsError("findings", "bad_item")
    for key in _REQUIRED_KEYS:
        if key not in item:
            raise FindingsError(key, "missing")
    if set(item) != FINDING_FIELDS:
        raise FindingsError("findings", "bad_fields")
    for key in ("file_path", "description", "suggested_fix"):
        if not isinstance(item[key], str):
            raise FindingsError(key, "bad_type")
    title = item["title"]
    if not isinstance(title, str):
        raise FindingsError("title", "bad_type")
    if len(title) > TITLE_MAX_CHARS:
        raise FindingsError("title", "bad_length")
    severity = item["severity"]
    if not isinstance(severity, str) or severity not in SEVERITIES:
        raise FindingsError("severity", "bad_enum")
    category = item["category"]
    if not isinstance(category, str) or category not in CATEGORIES:
        raise FindingsError("category", "bad_enum")
    return {
        "file_path": item["file_path"],
        "line_start": _clean_line(item["line_start"], "line_start"),
        "line_end": _clean_line(item["line_end"], "line_end"),
        "title": title,
        "description": item["description"],
        "suggested_fix": item["suggested_fix"],
        "severity": severity,
        "category": category,
    }


def parse_candidate_findings(payload: Any) -> list[dict[str, Any]]:
    """Parse + closed-schema validate specialist output (HLD §6
    Candidate Finding JSON Schema).

    Returns the validated findings as fresh dicts in input order.
    Anything off-schema raises `FindingsError` — unknown keys at either
    level, missing keys, wrong types/enums, overlong titles, lines < 1.
    """
    if not isinstance(payload, dict):
        raise FindingsError("payload", "not_object")
    if "findings" not in payload:
        raise FindingsError("findings", "missing")
    if set(payload) != {"findings"}:
        raise FindingsError("payload", "bad_fields")
    findings = payload["findings"]
    if not isinstance(findings, list):
        raise FindingsError("findings", "bad_list")
    return [_clean_item(item) for item in findings]


def clamp_to_post_image(
    findings: list[dict[str, Any]], file_lengths: dict[str, int]
) -> ClampResult:
    """Clamp out-of-range coordinates to the post-image file length.

    Precondition: items are `parse_candidate_findings`-validated (so
    every coordinate is already ≥ 1 — the lower bound is a parse-time
    rejection, never a clamp). Only the UPPER bound moves: each
    `line_start`/`line_end` above its file's length is set to the
    length and counted once. Files absent from `file_lengths` (and any
    value already ≤ the bound, including below-minimum input that
    bypassed parse) pass through untouched and uncounted. Inputs are
    never mutated — clamped items are copies.

    `file_lengths` maps `file_path` → post-image line count.
    """
    clamped: list[dict[str, Any]] = []
    clamped_n = 0
    for item in findings:
        length = file_lengths.get(item.get("file_path", ""))
        if not isinstance(length, int) or isinstance(length, bool) or length < 1:
            clamped.append(item)
            continue
        adjusted = dict(item)
        for key in ("line_start", "line_end"):
            value = adjusted[key]
            if isinstance(value, int) and not isinstance(value, bool) and value > length:
                adjusted[key] = length
                clamped_n += 1
        clamped.append(adjusted)
    return ClampResult(findings=clamped, clamped_n=clamped_n)


def make_candidate_id(specialty: Any, index: Any) -> str:
    """Build a `"{specialty}:{index}"` ID (HLD §6 Candidate Identity).

    Called by `run_fanout` post-wave only — never by specialists, never
    embedded into prompt-facing structures here.
    """
    if not isinstance(specialty, str) or not specialty:
        raise FindingsError("specialty", "bad_specialty")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise FindingsError("index", "bad_index")
    return f"{specialty}:{index}"


def parse_candidate_id(value: Any) -> tuple[str, int]:
    """Validate ID SHAPE only (`"<non-empty>:<non-negative int>"`, exactly
    one colon) and return `(specialty, index)`. Wave MEMBERSHIP (whether
    `run_fanout` assigned this ID) is checked by `run_fanout`, which owns
    the assigned set — unknown IDs there are a `verification_failed`
    error, never silently dropped."""
    if not isinstance(value, str):
        raise FindingsError("candidate_id", "bad_candidate_id")
    parts = value.split(":")
    if len(parts) != 2 or not parts[0]:
        raise FindingsError("candidate_id", "bad_candidate_id")
    try:
        index = int(parts[1])
    except ValueError:
        raise FindingsError("candidate_id", "bad_candidate_id") from None
    if isinstance(index, bool) or index < 0 or str(index) != parts[1]:
        raise FindingsError("candidate_id", "bad_candidate_id")
    return parts[0], index

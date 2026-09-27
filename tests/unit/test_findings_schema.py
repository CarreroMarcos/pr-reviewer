"""SPR-90 T007: candidate-finding schema contract tests (HLD-004 §6).

Closed-schema parse of specialist output: the top-level `{"findings"}`
object and every item reject unknown keys (`additionalProperties` at
both levels); required fields, severity/category enums, `title` ≤ 120,
`line_start`/`line_end` ≥ 1 (a REJECTION — distinct from post-image
range clamping, which counts but never rejects).

Coordinate space is 1-based POST-IMAGE lines: `clamp_to_post_image`
clamps out-of-range coordinates to the file length and returns the
clamped-coordinate count as data (`ClampResult.clamped_n` — the flag
`run_fanout` surfaces; flags never travel inside finding JSON, and
`candidate_id` is assigned by `run_fanout`, never carried in findings).

RED state: `common.findings` does not exist — collection errors.
"""

import pytest

from common.findings import (
    ClampResult,
    FindingsError,
    clamp_to_post_image,
    make_candidate_id,
    parse_candidate_findings,
    parse_candidate_id,
)

REQUIRED_KEYS = (
    "file_path",
    "line_start",
    "line_end",
    "title",
    "description",
    "suggested_fix",
    "severity",
    "category",
)


def _finding(**overrides):
    finding = {
        "file_path": "src/main.py",
        "line_start": 12,
        "line_end": 14,
        "title": "Missing bound check",
        "description": "d",
        "suggested_fix": "f",
        "severity": "HIGH",
        "category": "correctness",
    }
    finding.update(overrides)
    return finding


def _payload(findings):
    return {"findings": findings}


# --- closed-schema parse --------------------------------------------------------


def test_valid_finding_parses():
    assert parse_candidate_findings(_payload([_finding()])) == [_finding()]


def test_valid_empty_findings():
    assert parse_candidate_findings({"findings": []}) == []


def test_missing_findings_key():
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings({})
    assert (excinfo.value.field, excinfo.value.reason) == ("findings", "missing")


def test_top_level_extra_key_rejected():
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings({"findings": [], "extra": 1})
    assert (excinfo.value.field, excinfo.value.reason) == ("payload", "bad_fields")


def test_non_object_payload_rejected():
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings([_finding()])
    assert (excinfo.value.field, excinfo.value.reason) == ("payload", "not_object")


def test_findings_not_a_list_rejected():
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings({"findings": {"line_start": 1}})
    assert (excinfo.value.field, excinfo.value.reason) == ("findings", "bad_list")


def test_non_object_item_rejected():
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings(_payload(["not-a-finding"]))
    assert (excinfo.value.field, excinfo.value.reason) == ("findings", "bad_item")


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_missing_required_field(key):
    finding = _finding()
    del finding[key]
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings(_payload([finding]))
    assert (excinfo.value.field, excinfo.value.reason) == (key, "missing")


def test_item_extra_key_rejected():
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings(_payload([_finding(diff_hunk="@@ -1 +1 @@")]))
    assert (excinfo.value.field, excinfo.value.reason) == ("findings", "bad_fields")


def test_candidate_id_never_carried_in_specialist_output():
    """Specialists never see IDs: `candidate_id` is an unknown key here."""
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings(_payload([_finding(candidate_id="correctness:0")]))
    assert (excinfo.value.field, excinfo.value.reason) == ("findings", "bad_fields")


# --- enums ----------------------------------------------------------------------


@pytest.mark.parametrize("severity", ["HIGH", "MEDIUM", "LOW"])
def test_valid_severities(severity):
    parsed = parse_candidate_findings(_payload([_finding(severity=severity)]))
    assert parsed[0]["severity"] == severity


@pytest.mark.parametrize("severity", ["CRITICAL", "high", "High", "", 5, None])
def test_invalid_severity_rejected(severity):
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings(_payload([_finding(severity=severity)]))
    assert (excinfo.value.field, excinfo.value.reason) == ("severity", "bad_enum")


@pytest.mark.parametrize("category", ["correctness", "security", "tests"])
def test_valid_categories(category):
    parsed = parse_candidate_findings(_payload([_finding(category=category)]))
    assert parsed[0]["category"] == category


@pytest.mark.parametrize("category", ["style", "Correctness", "", None])
def test_invalid_category_rejected(category):
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings(_payload([_finding(category=category)]))
    assert (excinfo.value.field, excinfo.value.reason) == ("category", "bad_enum")


# --- title length ------------------------------------------------------------------


def test_title_at_120_chars_passes():
    parsed = parse_candidate_findings(_payload([_finding(title="t" * 120)]))
    assert parsed[0]["title"] == "t" * 120


def test_title_over_120_chars_rejected():
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings(_payload([_finding(title="t" * 121)]))
    assert (excinfo.value.field, excinfo.value.reason) == ("title", "bad_length")


# --- line minima (REJECTION, not clamping) --------------------------------------------


@pytest.mark.parametrize("field", ["line_start", "line_end"])
@pytest.mark.parametrize("value", [0, -1, True, "3", 2.5, None])
def test_line_below_minimum_rejected(field, value):
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings(_payload([_finding(**{field: value})]))
    assert (excinfo.value.field, excinfo.value.reason) == (field, "bad_line")


def test_non_string_text_field_rejected():
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_findings(_payload([_finding(file_path=42)]))
    assert (excinfo.value.field, excinfo.value.reason) == ("file_path", "bad_type")


# --- post-image clamp-and-flag (COUNT, never reject) ------------------------------------


def test_clamp_in_range_untouched():
    result = clamp_to_post_image([_finding()], {"src/main.py": 100})
    assert isinstance(result, ClampResult)
    assert result.findings == [_finding()]
    assert result.clamped_n == 0


def test_clamp_out_of_range_coordinates_clamped_and_counted():
    findings = [
        _finding(line_start=999, line_end=999),
        _finding(file_path="other.py", line_start=1, line_end=60),
    ]
    result = clamp_to_post_image(findings, {"src/main.py": 50, "other.py": 50})
    assert result.findings[0]["line_start"] == 50
    assert result.findings[0]["line_end"] == 50
    assert result.findings[1]["line_start"] == 1
    assert result.findings[1]["line_end"] == 50
    # Per-coordinate count: 2 + 1.
    assert result.clamped_n == 3


def test_clamp_unknown_file_passes_through_uncounted():
    result = clamp_to_post_image([_finding(line_start=999)], {"unrelated.py": 10})
    assert result.findings == [_finding(line_start=999)]
    assert result.clamped_n == 0


def test_clamp_does_not_mutate_input():
    findings = [_finding(line_start=999, line_end=999)]
    clamp_to_post_image(findings, {"src/main.py": 50})
    assert findings == [_finding(line_start=999, line_end=999)]


def test_clamp_below_minimum_left_alone():
    """Separation of mechanisms: < 1 is a parse-time REJECTION;
    clamp (a post-parse range operation) only touches the upper bound
    and never counts or repairs below-minimum values."""
    result = clamp_to_post_image([_finding(line_start=0)], {"src/main.py": 50})
    assert result.findings[0]["line_start"] == 0
    assert result.clamped_n == 0


# --- candidate identity (format only; assignment is run_fanout's) ---------------------------


def test_make_candidate_id():
    assert make_candidate_id("correctness", 0) == "correctness:0"
    assert make_candidate_id("security", 2) == "security:2"


@pytest.mark.parametrize(
    ("specialty", "index"),
    [("", 0), ("correctness", -1), ("correctness", True), (42, 0)],
)
def test_make_candidate_id_rejects(specialty, index):
    with pytest.raises(FindingsError):
        make_candidate_id(specialty, index)


def test_parse_candidate_id():
    assert parse_candidate_id("tests:3") == ("tests", 3)


@pytest.mark.parametrize(
    "value",
    ["no-colon", "a:b:c", "tests:-1", "tests:x", ":3", "tests:", "", 42, None],
)
def test_parse_candidate_id_rejects(value):
    with pytest.raises(FindingsError) as excinfo:
        parse_candidate_id(value)
    assert excinfo.value.field == "candidate_id"

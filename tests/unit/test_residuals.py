"""SPR-94 T010a: accepted-residuals parser contract tests (HLD-004 D7).

TOLERANT parser (the opposite of findings.py's fail-closed validation):
`repo#pr`-keyed sections map to their accepted-residual lines; missing
file/section → `[]`; malformed lines are skipped WITH a record the
caller can turn into events (never raised, never silently dropped).
`docs/accepted-residuals.md` does not exist yet, so fixtures below
define the representative shape and missing-file → empty is live
specified behavior.

RED state: `common.residuals` does not exist — collection errors.
"""

import pytest

from common.residuals import (
    ParseResult,
    SkippedLine,
    load_accepted_residuals,
    parse_accepted_residuals,
    residuals_for,
)

FIXTURE_TEXT = """\
# Accepted residuals

<!-- operator-curated; one bullet per accepted finding. -->

## octo-org/hello-world#42
- Accept 6x constant duplication in test fixtures.
- Ignore LOW off-by-one in the legacy parser.

## octo-org/other#7
- Tautological assert in the smoke test is intentional.
"""


def test_parse_two_sections():
    result = parse_accepted_residuals(FIXTURE_TEXT)
    assert isinstance(result, ParseResult)
    assert result.residuals == {
        "octo-org/hello-world#42": [
            "Accept 6x constant duplication in test fixtures.",
            "Ignore LOW off-by-one in the legacy parser.",
        ],
        "octo-org/other#7": ["Tautological assert in the smoke test is intentional."],
    }
    assert result.skipped == []


def test_lookup_hit():
    result = parse_accepted_residuals(FIXTURE_TEXT)
    assert residuals_for(result, "octo-org/hello-world", 42) == [
        "Accept 6x constant duplication in test fixtures.",
        "Ignore LOW off-by-one in the legacy parser.",
    ]


def test_missing_section_lookup_returns_empty():
    result = parse_accepted_residuals(FIXTURE_TEXT)
    assert residuals_for(result, "octo-org/hello-world", 999) == []
    assert residuals_for(result, "nobody/nothing", 1) == []


def test_lookup_returns_a_copy():
    result = parse_accepted_residuals(FIXTURE_TEXT)
    out = residuals_for(result, "octo-org/hello-world", 42)
    out.append("mutant")
    assert residuals_for(result, "octo-org/hello-world", 42) == [
        "Accept 6x constant duplication in test fixtures.",
        "Ignore LOW off-by-one in the legacy parser.",
    ]


def test_missing_file_returns_empty(tmp_path):
    result = load_accepted_residuals(tmp_path / "no-such-file.md")
    assert result.residuals == {}
    assert result.skipped == []
    assert residuals_for(result, "octo-org/hello-world", 42) == []


def test_empty_file_returns_empty(tmp_path):
    path = tmp_path / "empty.md"
    path.write_text("", encoding="utf-8")
    assert load_accepted_residuals(path).residuals == {}
    assert parse_accepted_residuals("").residuals == {}
    assert parse_accepted_residuals("   \n\n").skipped == []


def test_load_reads_real_file(tmp_path):
    path = tmp_path / "accepted-residuals.md"
    path.write_text(FIXTURE_TEXT, encoding="utf-8")
    result = load_accepted_residuals(path)
    assert set(result.residuals) == {"octo-org/hello-world#42", "octo-org/other#7"}


def test_malformed_section_header_skipped_with_record():
    text = "## octo-org/hello-world#42\n- Fine line.\n## not a section header\n- Orphaned.\n"
    result = parse_accepted_residuals(text)
    assert result.residuals == {"octo-org/hello-world#42": ["Fine line."]}
    assert result.skipped == [
        SkippedLine(line_no=3, line="## not a section header", reason="bad_section"),
        SkippedLine(line_no=4, line="- Orphaned.", reason="orphan_bullet"),
    ]


def test_orphan_bullet_before_any_section():
    result = parse_accepted_residuals("- Nowhere to attach.\n## a/b#1\n- Home.\n")
    assert result.residuals == {"a/b#1": ["Home."]}
    assert result.skipped == [
        SkippedLine(line_no=1, line="- Nowhere to attach.", reason="orphan_bullet")
    ]


def test_empty_bullet_skipped_with_record():
    result = parse_accepted_residuals("## a/b#1\n- Kept.\n-\n-   \n")
    assert result.residuals == {"a/b#1": ["Kept."]}
    assert [s.reason for s in result.skipped] == ["empty_bullet", "empty_bullet"]
    assert [s.line_no for s in result.skipped] == [3, 4]


def test_bullet_without_space_skipped_with_record():
    result = parse_accepted_residuals("## a/b#1\n-item\n- Real item.\n")
    assert result.residuals == {"a/b#1": ["Real item."]}
    assert result.skipped == [SkippedLine(line_no=2, line="-item", reason="bad_bullet")]


@pytest.mark.parametrize("marker", ["-", "*", "+"])
def test_all_bullet_markers_accepted(marker):
    result = parse_accepted_residuals(f"## a/b#1\n{marker} Item.\n")
    assert result.residuals == {"a/b#1": ["Item."]}
    assert result.skipped == []


def test_nested_bullet_flattens_to_own_line():
    result = parse_accepted_residuals("## a/b#1\n- Outer.\n  - Inner detail.\n")
    assert result.residuals == {"a/b#1": ["Outer.", "Inner detail."]}


def test_duplicate_sections_merge():
    text = "## a/b#1\n- First.\n## a/b#1\n- Second.\n"
    result = parse_accepted_residuals(text)
    assert result.residuals == {"a/b#1": ["First.", "Second."]}


def test_prose_title_and_comments_ignored():
    text = "# Title\nSome prose.\n<!-- comment -->\n### sub\n## a/b#1\n- Kept.\n"
    result = parse_accepted_residuals(text)
    assert result.residuals == {"a/b#1": ["Kept."]}
    assert result.skipped == []


def test_crlf_tolerated():
    result = parse_accepted_residuals("## a/b#1\r\n- Kept.\r\n")
    assert result.residuals == {"a/b#1": ["Kept."]}
    assert result.skipped == []


def test_non_string_input_raises_type_error():
    with pytest.raises(TypeError):
        parse_accepted_residuals(None)

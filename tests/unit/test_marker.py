"""T007: canonical marker exact-string cases (contracts/canonical-comment.md, HLD §2.8).

The marker is assembled by worker code only; the model never emits it. Exact form:
`<!-- pr-reviewer:canonical:v1:{repo_full_name}#{pr_number} -->`.
"""

import pytest
from common.marker import build_marker


@pytest.mark.parametrize(
    ("repo_full_name", "pr_number", "expected"),
    [
        (
            "CarreroMarcos/pr-reviewer",
            42,
            "<!-- pr-reviewer:canonical:v1:CarreroMarcos/pr-reviewer#42 -->",
        ),
        (
            "octo-org/hello-world",
            1,
            "<!-- pr-reviewer:canonical:v1:octo-org/hello-world#1 -->",
        ),
        (
            "a/b",
            7,
            "<!-- pr-reviewer:canonical:v1:a/b#7 -->",
        ),
        (
            "my-org/my.repo-name_123",
            1000000000,
            "<!-- pr-reviewer:canonical:v1:my-org/my.repo-name_123#1000000000 -->",
        ),
    ],
)
def test_exact_marker_string(repo_full_name, pr_number, expected):
    assert build_marker(repo_full_name, pr_number) == expected


def test_marker_has_no_surrounding_whitespace():
    marker = build_marker("CarreroMarcos/pr-reviewer", 42)
    assert marker == marker.strip()
    assert "\n" not in marker
    assert marker == "<!-- pr-reviewer:canonical:v1:CarreroMarcos/pr-reviewer#42 -->"


def test_marker_is_html_comment_form():
    marker = build_marker("octo-org/hello-world", 9)
    assert marker.startswith("<!-- ")
    assert marker.endswith(" -->")


def test_marker_is_deterministic_pure_function():
    first = build_marker("octo-org/hello-world", 123)
    second = build_marker("octo-org/hello-world", 123)
    assert first == second


def test_distinct_inputs_yield_distinct_markers():
    markers = {
        build_marker("octo-org/hello-world", 1),
        build_marker("octo-org/hello-world", 2),
        build_marker("other-org/hello-world", 1),
    }
    assert len(markers) == 3

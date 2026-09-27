"""SPR-92 T009: span-preserving markdown sanitization tests (HLD-004 §5 item 2).

Fenced blocks + inline backtick spans stash to indexed `\x00CODE_SPAN_N\x00`
placeholders and come back byte-identical; prose neutralizes exactly four
constructs (`![img](url)`, `[link](url)`, `<http...>`); generics, JSX, and
other angle-bracket text survive untouched.

RED state: `common.sanitize` does not exist — collection errors.
"""

import pytest

from common.sanitize import SanitizeError, sanitize


def test_plain_prose_with_generics_and_jsx_untouched():
    text = "Use List<T> and Dict[str, Any>; render <div className=\"x\">; a < b; <3."
    assert sanitize(text) == text


def test_fenced_block_stashed_and_restored_byte_identical():
    code = "```python\n[x](y) ![a](b) <http://evil.test> `raw`\n```"
    assert sanitize(code) == code


def test_fenced_block_protects_while_prose_transforms():
    text = "see [a](b)\n```\n[x](y)\n```\n"
    assert sanitize(text) == "see a (b)\n```\n[x](y)\n```\n"


def test_two_distinct_spans_each_restored():
    text = "`one` and `two`"
    assert sanitize(text) == text


def test_unclosed_fence_runs_to_end_of_text():
    text = "intro [a](b)\n```\n[x](y)\n"
    assert sanitize(text) == "intro a (b)\n```\n[x](y)\n"


def test_inline_span_protects_link_syntax():
    assert sanitize("`[a](b)` stays") == "`[a](b)` stays"


def test_inline_span_with_adversarial_content_byte_identical():
    text = "``code `x` ~ ü — ✓`` after"
    assert sanitize(text) == text


def test_unclosed_inline_backtick_is_literal():
    assert sanitize("a ` b") == "a ` b"


def test_backtick_span_inside_link_text_survives():
    assert sanitize("[a `code` b](u)") == "a `code` b (u)"


def test_image_transform():
    assert sanitize("see ![img](http://x.test/i.png) now") == (
        "see [Image: img] (http://x.test/i.png) now"
    )


def test_image_empty_alt():
    assert sanitize("![](u)") == "[Image: ] (u)"


def test_link_transform():
    assert sanitize("see [link](http://x.test) now") == "see link (http://x.test) now"


def test_link_with_title_keeps_dest_and_title():
    assert sanitize('[a](b "t")') == 'a (b "t")'


@pytest.mark.parametrize("url", ["http://x.test/a", "https://x.test/a"])
def test_bare_autolink_neutralized(url):
    assert sanitize(f"see <{url}> now") == f"see `{url}` now"


def test_autolink_scheme_case_insensitive():
    assert sanitize("<HTTP://x.test>") == "`<HTTP://x.test>`"


def test_angle_bracket_non_links_survive():
    for text in [
        "List<T>",
        "Dict[str, Any]",
        "a < b",
        "<div>",
        '<a href="http://x.test">',
        "<http>",
        "see http://x.test and https://y.test here",
    ]:
        assert sanitize(text) == text


def test_placeholder_looking_text_without_nul_passes_through():
    assert sanitize("literal CODE_SPAN_0 talk") == "literal CODE_SPAN_0 talk"


def test_no_nul_bytes_leak_into_output():
    out = sanitize("see [a](b) and `c` plus\n```\nd\n```\n")
    assert "\x00" not in out


def test_empty_string():
    assert sanitize("") == ""


def test_nul_byte_input_rejected():
    """The `\x00`-framed placeholder scheme is only collision-proof when
    input carries no NUL — fail closed instead of risking a forged
    placeholder hijacking the stash."""
    with pytest.raises(SanitizeError) as excinfo:
        sanitize("evil \x00CODE_SPAN_0\x00 here")
    assert excinfo.value.field == "text"


@pytest.mark.parametrize("value", [None, 42, b"bytes", ["x"]])
def test_non_string_input_rejected(value):
    with pytest.raises(SanitizeError) as excinfo:
        sanitize(value)
    assert excinfo.value.field == "text"

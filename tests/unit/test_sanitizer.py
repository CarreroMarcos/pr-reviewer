"""SPR-92 T009: span-preserving markdown sanitization tests (HLD-004 §5 item 2).

Fenced blocks + inline backtick spans stash to indexed `\x00CODE_SPAN_N\x00`
placeholders and come back byte-identical; prose neutralizes exactly four
constructs (`![img](url)`, `[link](url)`, `<http...>`); generics, JSX, and
other angle-bracket text survive untouched.
"""

import pytest

from common.sanitize import SanitizeError, sanitize
from common.validate import CANARY_SUBSTRING


def test_plain_prose_with_generics_and_jsx_untouched():
    text = 'Use List<T> and Dict[str, Any>; render <div className="x">; a < b; <3.'
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
    assert sanitize(f"see <{url}> now") == f"see `<{url}>` now"


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


# HLD-004 §5 item 5: payload-token redaction (runs after span
# re-substitution over the fully-restored text).


def test_payload_email_redacted():
    out = sanitize("contact attacker@evil.example for details")
    assert out == "contact [redacted:email] for details"
    assert "attacker@evil.example" not in out


def test_canary_inside_fenced_block_redacted():
    code = f"```\nleaked {CANARY_SUBSTRING} here\n```"
    out = sanitize(code)
    assert out == "```\nleaked [redacted:token] here\n```"
    assert CANARY_SUBSTRING not in out


def test_canary_in_prose_redacted():
    out = sanitize(f"see {CANARY_SUBSTRING} now")
    assert out == "see [redacted:token] now"


def test_email_inside_fenced_block_redacted():
    out = sanitize("```\nmail bob@corp.example now\n```")
    assert "[redacted:email]" in out
    assert "bob@corp.example" not in out


def test_hex_inside_inline_code_span_redacted():
    out = sanitize("`token 9f3a7c2e1b4d6a8e` end")
    assert "9f3a7c2e1b4d6a8e" not in out
    assert "[redacted:token]" in out


def test_sixteen_hex_run_redacted():
    out = sanitize("sha 9f3a7c2e1b4d6a8e end")
    assert out == "sha [redacted:token] end"


def test_full_commit_sha_redacted():
    sha = "a" * 40
    out = sanitize(f"see {sha} now")
    assert out == "see [redacted:token] now"


@pytest.mark.parametrize("token", ["deadbee", "abc123def456789", "9f3a7c2e1b4d6a8"])
def test_short_hex_runs_survive(token):
    assert sanitize(f"see {token} now") == f"see {token} now"


def test_generics_and_code_spans_survive_redaction_phase():
    for text in [
        "Use List<T> and Dict[str, Any> here",
        "`const x: List<T> = []` stays",
        "render <div> and `Dict[str, Any]` now",
    ]:
        assert sanitize(text) == text


def test_structure_phases_still_apply_before_redaction():
    assert sanitize("see ![img](http://x.test/i.png) now") == (
        "see [Image: img] (http://x.test/i.png) now"
    )
    assert sanitize("see [link](http://x.test) now") == "see link (http://x.test) now"
    assert sanitize("see <http://x.test/a> now") == "see `<http://x.test/a>` now"


def test_sanitize_idempotent_on_shipped_mix():
    # Gate-54 coverage gap 1: reconcile re-publishes shipped text, so
    # sanitize∘sanitize must be identity over the structure outputs.
    once = sanitize(
        "see ![img](http://x.test/i.png), <http://x.test/a>, "
        f"`tok {CANARY_SUBSTRING}` and a@b.test\n\n```\n9f3a7c2e1b4d6a8e\n```"
    )
    assert sanitize(once) == once


def test_email_inside_inline_code_span_redacted():
    # Gate-54 F5: the fence row pins crossing fenced blocks; this pins
    # inline spans (same phase-4 pass over fully-restored text).
    out = sanitize("`mail bob@corp.example` end")
    assert "bob@corp.example" not in out
    assert "[redacted:email]" in out

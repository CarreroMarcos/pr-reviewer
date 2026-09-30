"""T075: the replay-link footer on the canonical comment.

Contract (spec PR #168): the footer is appended deterministically at
publish — code-side, never model-side; omitted entirely when
`REPLAY_BASE_URL` is unset; exactly-once under update-in-place
re-publish (a body already carrying the mark is never re-appended).
"""

from common import config
from worker_handler import REPLAY_FOOTER_MARK, _with_replay_footer

BODY = "## Summary\n\nLooks good.\n\n## Findings\n\n- one finding (file.py:12)"
BASE = "https://viewer.example.lambda-url.us-west-2.on.aws"
SHA = "a" * 40


def test_footer_appended_when_env_set(monkeypatch):
    monkeypatch.setenv("REPLAY_BASE_URL", BASE + "/")  # trailing slash in env
    out = _with_replay_footer(BODY, pr_number=167, sha=SHA)
    assert out.startswith(BODY)
    assert out.count(REPLAY_FOOTER_MARK) == 1
    # env trailing slash stripped — exactly one `/runs/...` segment shape
    assert f"[{REPLAY_FOOTER_MARK}]({BASE}/runs/167/{SHA}/)" in out
    assert out.rstrip().endswith("checkpoints for this review.")


def test_footer_omitted_when_env_unset(monkeypatch):
    monkeypatch.delenv("REPLAY_BASE_URL", raising=False)
    assert _with_replay_footer(BODY, pr_number=167, sha=SHA) == BODY


def test_footer_exactly_once_on_republish(monkeypatch):
    monkeypatch.setenv("REPLAY_BASE_URL", BASE)
    once = _with_replay_footer(BODY, pr_number=167, sha=SHA)
    twice = _with_replay_footer(once, pr_number=167, sha=SHA)
    assert twice == once


def test_quoted_phrase_without_link_does_not_suppress(monkeypatch):
    """PR #169 r1 LOW: a model-quoted 'Full agent replay' phrase (no link)
    must not read as an existing footer — idempotency anchors to the
    exact footer link, so the footer still lands exactly once."""
    monkeypatch.setenv("REPLAY_BASE_URL", BASE)
    quoted = BODY + "\n\nThe verifier noted the Full agent replay idea.\n"
    out = _with_replay_footer(quoted, pr_number=167, sha=SHA)
    assert f"]({BASE}/runs/167/{SHA}/)" in out
    assert out.count(f"[{REPLAY_FOOTER_MARK}]({BASE}/runs/") == 1


def test_decoy_link_for_different_run_does_not_suppress(monkeypatch):
    """PR #169 r2 LOW: only the EXACT footer for this run suppresses —
    a decoy link (attacker-influenced text) for another pr/sha must not."""
    monkeypatch.setenv("REPLAY_BASE_URL", BASE)
    decoy = (
        BODY
        + "\n\n🔬 [Full agent replay](https://viewer.example.test/runs/999/"
        + "c" * 40
        + "/)\n"
    )
    out = _with_replay_footer(decoy, pr_number=167, sha=SHA)
    assert out.count(f"]({BASE}/runs/167/{SHA}/)") == 1


def test_footer_omitted_for_whitespace_only_env(monkeypatch):
    monkeypatch.setenv("REPLAY_BASE_URL", "   \n")
    assert _with_replay_footer(BODY, pr_number=167, sha=SHA) == BODY


def test_config_replay_base_url_strips_trailing_slashes(monkeypatch):
    monkeypatch.setenv("REPLAY_BASE_URL", BASE + "//")
    assert config.replay_base_url() == BASE
    monkeypatch.delenv("REPLAY_BASE_URL", raising=False)
    assert config.replay_base_url() == ""

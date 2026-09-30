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


def test_config_replay_base_url_strips_trailing_slashes(monkeypatch):
    monkeypatch.setenv("REPLAY_BASE_URL", BASE + "//")
    assert config.replay_base_url() == BASE
    monkeypatch.delenv("REPLAY_BASE_URL", raising=False)
    assert config.replay_base_url() == ""

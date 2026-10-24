"""T075: the replay-link footer on the canonical comment.

Contract (PR #168): the footer is appended deterministically at
publish — code-side, never model-side; omitted entirely when
`REPLAY_BASE_URL` is unset; exactly-once under update-in-place
re-publish (a body already carrying the mark is never re-appended).
"""

import json
from types import SimpleNamespace

from common import config
from common.envelope import Envelope
from worker_handler import (
    REPLAY_FOOTER_MARK,
    _label_fn_for,
    _make_publish,
    _run_label,
    _with_replay_footer,
)

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


def test_footer_label_rendered_after_link(monkeypatch):
    monkeypatch.setenv("REPLAY_BASE_URL", BASE)
    out = _with_replay_footer(BODY, pr_number=167, sha=SHA, label="multi-agent · glm-5.3-flash")
    link = f"[{REPLAY_FOOTER_MARK}]({BASE}/runs/167/{SHA}/)"
    assert f"{link} — multi-agent · glm-5.3-flash · agent DAG" in out
    assert out.count(REPLAY_FOOTER_MARK) == 1


def test_footer_label_does_not_break_exactly_once(monkeypatch):
    """The exactly-once anchor is the link alone: a re-publish of a body
    whose footer already carries a label must not stack a second footer."""
    monkeypatch.setenv("REPLAY_BASE_URL", BASE)
    once = _with_replay_footer(BODY, pr_number=167, sha=SHA, label="single-pass · glm-5.3-flash")
    twice = _with_replay_footer(once, pr_number=167, sha=SHA, label="single-pass · glm-5.3-flash")
    assert twice == once


def test_run_label_single_pass():
    assert _run_label([], "glm-5.3-flash") == "single-pass · glm-5.3-flash"
    assert (
        _run_label([{"type": "concurrency_single_pass"}], "glm-5.3-flash")
        == "single-pass · glm-5.3-flash"
    )


def test_run_label_multi_agent():
    events = [{"type": "agent_started"}, {"type": "review_synthesized"}]
    assert _run_label(events, "glm-5.3-flash") == "multi-agent · glm-5.3-flash"


def test_run_label_degraded_keeps_origin_visible():
    events = [{"type": "agent_started"}, {"type": "degraded_to_single_pass"}]
    assert (
        _run_label(events, "glm-5.3-flash")
        == "multi-agent → degraded to single-pass · glm-5.3-flash"
    )


def test_run_label_degraded_flag_ignored_when_pipeline_single_pass(monkeypatch):
    """The degraded flag only surfaces under a multi-agent pipeline.

    A lone `degraded_to_single_pass` event genuinely classifies as
    multi-agent (the marker is in the fan-out set), so the real
    discriminator never produces this combination — but if the pipeline
    ever did resolve single-pass alongside the marker, the label must
    say single-pass, not leak a degraded qualifier. Forced via a
    stubbed discriminator to pin the branch, not the unreachable input.
    """
    monkeypatch.setattr("worker_handler.archive_mod.resolve_pipeline", lambda events: "single_pass")
    assert (
        _run_label([{"type": "degraded_to_single_pass"}], "glm-5.3-flash")
        == "single-pass · glm-5.3-flash"
    )


def test_label_fn_for_is_late_bound_on_events_and_model():
    """The production wiring seam: the closure reads the event list and
    the credential snapshot when called (publish time), not when built.

    Pins the exact properties a hand-rolled publish test cannot: the
    argument order into `_run_label`, the `glm_model` attribute, and
    that events recorded after wiring still count.
    """
    events: list = []
    creds = SimpleNamespace(
        current=lambda: SimpleNamespace(glm_model="glm-x"),
        refresh_once=lambda: False,
    )
    label_fn = _label_fn_for(events, creds)
    assert label_fn() == "single-pass · glm-x"
    events.append({"type": "agent_started"})
    assert label_fn() == "multi-agent · glm-x"
    events.append({"type": "degraded_to_single_pass"})
    assert label_fn() == "multi-agent → degraded to single-pass · glm-x"


def _publish_fixture(monkeypatch, label_fn):
    """A `_make_publish` callable on the stored-comment PATCH path.

    The table seeds a stored comment id, so publish() takes the PATCH
    fast-path and the transport captures the exact shipped body.
    """
    monkeypatch.setenv("REPLAY_BASE_URL", BASE)
    calls = []

    def transport(method, url, headers, body):
        calls.append({"method": method, "url": url, "body": body})
        return 200, b'{"id": 555}', {}

    creds = SimpleNamespace(
        current=lambda: SimpleNamespace(github_token="t"),  # noqa: S106 (fake fixture)
        refresh_once=lambda: False,
    )

    class Table:
        def get_item(self, pk):
            return {"comment_id": 555}

    envelope = Envelope(
        envelope_version="v1",
        event_type="pull_request",
        action="opened",
        repo_full_name="octo-org/hello-world",
        pr_number=167,
        head_sha=SHA,
        base_sha="0" * 40,
        sender="octo-user",
        delivery_guid="11111111-1111-4111-8111-111111111111",
    )
    publish = _make_publish(
        envelope=envelope,
        creds=creds,
        table=Table(),
        pk="review:octo-org/hello-world#167",
        owner="11111111-1111-4111-8111-111111111111",
        clock=lambda: 1_750_000_000,
        github_transport=transport,
        label_fn=label_fn,
    )
    return publish, calls


def _shipped_body(calls):
    assert len(calls) == 1 and calls[0]["method"] == "PATCH"
    return json.loads(calls[0]["body"].decode("utf-8"))["body"]


def test_publish_wires_label_fn_into_footer(monkeypatch):
    publish, calls = _publish_fixture(monkeypatch, label_fn=lambda: "multi-agent · glm-5.3-flash")
    assert publish(BODY) == 555
    shipped = _shipped_body(calls)
    assert "— multi-agent · glm-5.3-flash · agent DAG" in shipped


def test_publish_without_label_fn_keeps_legacy_footer(monkeypatch):
    publish, calls = _publish_fixture(monkeypatch, label_fn=None)
    assert publish(BODY) == 555
    shipped = _shipped_body(calls)
    assert f"[{REPLAY_FOOTER_MARK}]({BASE}/runs/167/{SHA}/)" in shipped
    assert "glm-5.3-flash" not in shipped


def test_publish_label_failure_degrades_to_unlabeled_footer(monkeypatch):
    def boom():
        raise RuntimeError("simulated label failure")

    publish, calls = _publish_fixture(monkeypatch, label_fn=boom)
    assert publish(BODY) == 555
    shipped = _shipped_body(calls)
    assert f"[{REPLAY_FOOTER_MARK}]({BASE}/runs/167/{SHA}/)" in shipped
    assert "glm-5.3-flash" not in shipped


def test_footer_label_change_does_not_break_exactly_once(monkeypatch):
    """The exactly-once anchor is the link alone, not the labeled line:
    a re-publish whose pipeline/model label changed must still be
    suppressed, never stack a second footer."""
    monkeypatch.setenv("REPLAY_BASE_URL", BASE)
    once = _with_replay_footer(BODY, pr_number=167, sha=SHA, label="single-pass · glm-5.3-flash")
    again = _with_replay_footer(
        once,
        pr_number=167,
        sha=SHA,
        label="multi-agent → degraded to single-pass · other-model",
    )
    assert again == once


def test_footer_omitted_when_env_unset(monkeypatch):
    monkeypatch.delenv("REPLAY_BASE_URL", raising=False)
    assert _with_replay_footer(BODY, pr_number=167, sha=SHA) == BODY


def test_footer_exactly_once_on_republish(monkeypatch):
    monkeypatch.setenv("REPLAY_BASE_URL", BASE)
    once = _with_replay_footer(BODY, pr_number=167, sha=SHA)
    twice = _with_replay_footer(once, pr_number=167, sha=SHA)
    assert twice == once


def test_quoted_phrase_without_link_does_not_suppress(monkeypatch):
    """PR #169: a model-quoted 'Full agent replay' phrase (no link)
    must not read as an existing footer — idempotency anchors to the
    exact footer link, so the footer still lands exactly once."""
    monkeypatch.setenv("REPLAY_BASE_URL", BASE)
    quoted = BODY + "\n\nThe verifier noted the Full agent replay idea.\n"
    out = _with_replay_footer(quoted, pr_number=167, sha=SHA)
    assert f"]({BASE}/runs/167/{SHA}/)" in out
    assert out.count(f"[{REPLAY_FOOTER_MARK}]({BASE}/runs/") == 1


def test_decoy_link_for_different_run_does_not_suppress(monkeypatch):
    """PR #169: only the EXACT footer for this run suppresses —
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

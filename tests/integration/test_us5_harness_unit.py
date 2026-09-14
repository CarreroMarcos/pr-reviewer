"""SPR-59: offline unit coverage for the US5 harness state discipline.

Loads test_us5_acceptance.py as a side module (env fakes satisfy its
opt-in gate) and exercises the disk-only machinery plus the redrive
fallback branch with a fake boto3 — no AWS calls. Runs in the default
offline suite (no "live" in any test name).
"""

import importlib.util
import json
import os
from pathlib import Path

os.environ["ACCEPTANCE_LIVE"] = "1"
os.environ["ACCEPTANCE_PR"] = "1"
os.environ["ACCEPTANCE_US5_PHASE"] = "drill"

import botocore.exceptions
import pytest

_HARNESS_PATH = Path(__file__).resolve().parent / "test_us5_acceptance.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("us5_harness_under_test", _HARNESS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


h = _load_harness()


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """Fresh harness memory with the state file redirected to tmp."""
    h._STATE.clear()
    monkeypatch.setattr(h, "STATE_PATH", str(tmp_path / "us5-state.json"))
    return h


def _read_state_file(harness):
    with open(harness.STATE_PATH) as handle:
        return json.load(handle)


def test_merge_bias_memory_wins_disk_nested_survive(harness):
    """Divergent same-key: memory wins; disk-only nested keys survive."""
    Path(harness.STATE_PATH).write_text(
        json.dumps({"drill": {"recovery_sha": "disk-sha", "t0": 1}, "watcher_only": True})
    )
    h._STATE["state"] = {"drill": {"t0": 2}}
    harness._save_state()
    saved = _read_state_file(harness)
    assert saved["drill"] == {"recovery_sha": "disk-sha", "t0": 2}
    assert saved["watcher_only"] is True


def test_legacy_secret_scrubbed_pointer_survives(harness):
    """Legacy disk key_backup purged; functional key_restore_param kept."""
    for forbidden in harness._FORBIDDEN_STATE_KEYS:
        assert forbidden not in "key_restore_param", (
            f"forbidden entry {forbidden!r} would eat the resume pointer"
        )
    Path(harness.STATE_PATH).write_text(
        json.dumps({"l": {"key_backup": "disk-secret", "sha": "abc"}})
    )
    h._STATE["state"] = {
        "l": {"sha": "abc", "key_restore_param": "/pr-reviewer/glm-api-key-backup"}
    }
    harness._save_state()
    saved = _read_state_file(harness)
    assert "key_backup" not in json.dumps(saved)
    assert saved["l"]["key_restore_param"] == "/pr-reviewer/glm-api-key-backup"


def test_secret_key_fails_loud_before_write(harness, tmp_path):
    """A secret-shaped in-memory key raises before any scrub or write."""
    h._STATE["state"] = {"l": {"key_backup": "sekret-value"}}
    with pytest.raises(AssertionError, match="never the state file"):
        harness._save_state()
    assert not Path(harness.STATE_PATH).exists()
    assert (tmp_path / f"us5-state.json.tmp-{os.getpid()}") not in list(tmp_path.iterdir())


class _FakeSQS:
    def __init__(self, mode):
        self.mode = mode
        self.sent: list[dict] = []

    def start_message_move_task(self, **kwargs):
        if self.mode == "client_error":
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "StartMessageMoveTask"
            )
        raise TypeError("boom-programming-error")

    def receive_message(self, **kwargs):
        return {"Messages": []}

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return {}

    def delete_message(self, **kwargs):
        return {}

    def get_queue_attributes(self, **kwargs):
        return {"Attributes": {"QueueArn": "arn:aws:sqs:us-west-2:1:q"}}


class _FakeBoto:
    def __init__(self, sqs):
        self._sqs = sqs

    def client(self, name, **kwargs):
        assert name == "sqs"
        return self._sqs


def _prime_redrive(harness, monkeypatch, sqs, tmp_path):
    h._STATE.clear()
    state_path = tmp_path / "us5-state.json"
    state_path.write_text(json.dumps({"drill": {"n_head": "sha-x"}}))
    monkeypatch.setattr(h, "STATE_PATH", str(state_path))
    monkeypatch.setattr(h, "boto3", _FakeBoto(sqs))
    calls = {"n": 0}

    def _depth(name):
        calls["n"] += 1
        if name == h.DLQ_QUEUE:
            return 2 if calls["n"] == 1 else 0
        return 0

    monkeypatch.setattr(h, "_queue_depth", _depth)
    monkeypatch.setattr(h, "_queue_url", lambda name: f"https://sqs/x/{name}")
    monkeypatch.setattr(
        h, "_canonical_comments", lambda: [{"id": 7, "updated_at": "t", "body": "b"}]
    )
    monkeypatch.setattr(h, "_state_item", lambda: {"status": "ACTIVE", "head_sha": "sha-x"})
    return state_path


def test_redrive_fallback_on_client_error(harness, monkeypatch, tmp_path):
    """ClientError takes the manual-move fallback, not a drill abort."""
    sqs = _FakeSQS("client_error")
    state_path = _prime_redrive(harness, monkeypatch, sqs, tmp_path)
    h.test_stage_redrive_drain_and_converge()
    saved = json.loads(state_path.read_text())["drill"]
    assert saved["move_task_error"].startswith("ClientError:")
    assert saved["manual_moved"] == 0
    assert saved["redrive"]["moved_manually"] is True


def test_redrive_type_error_propagates(harness, monkeypatch, tmp_path):
    """Programming errors stay loud through the narrowed except."""
    sqs = _FakeSQS("type_error")
    _prime_redrive(harness, monkeypatch, sqs, tmp_path)
    with pytest.raises(TypeError, match="boom-programming-error"):
        h.test_stage_redrive_drain_and_converge()


def test_pointer_round_trip_across_runs(harness):
    """Save -> fresh load still sees key_restore_param (resume works)."""
    h._STATE["state"] = {
        "l": {"sha": "abc", "key_restore_param": "/pr-reviewer/glm-api-key-backup-us5"}
    }
    harness._save_state()
    h._STATE.clear()
    reloaded = harness._load_state()
    assert reloaded["l"]["key_restore_param"] == "/pr-reviewer/glm-api-key-backup-us5"

"""SPR-88 T005: multi-agent env-knob contract tests (HLD-004 §8 checklist 7).

The 12 multi-agent env vars with exact names and defaults; typed parsing
(ints vs the `REASONING_EFFORT` string); request-time resolution (never
cached in module state); and the naming pin — no `MARGIN_S` /
`DEGRADED_BUDGET_MARGIN_S`.

`REASONING_EFFORT` defaults to the literal `"low"`, PROVISIONAL until the
Phase-0 exit ruling (D6/D8 pick the ship config from measured p95 — this
infrastructure default must not pre-decide it).

RED state: `common.config` exposes none of these names — collection
errors on import.
"""

import pytest

from common import config
from common.config import (
    DEFAULT_BUDGET_MARGIN_S,
    DEFAULT_FANOUT_CONCURRENCY,
    DEFAULT_MULTI_AGENT,
    DEFAULT_MULTI_AGENT_PHASE0,
    DEFAULT_MUTEX_LEASE_TTL_S,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_REASONING_MAX_CHARS,
    DEFAULT_SINGLE_PASS_WAIT_FOR_S,
    DEFAULT_SOCKET_READ_TIMEOUT_S,
    DEFAULT_SYNTHESIZER_WAIT_FOR_S,
    DEFAULT_VERIFIER_WAIT_FOR_S,
    DEFAULT_WAVE_WAIT_FOR_S,
    multi_agent_config,
)

ALL_ENV_VARS = (
    "MULTI_AGENT",
    "MULTI_AGENT_PHASE0",
    "FANOUT_CONCURRENCY",
    "MUTEX_LEASE_TTL_S",
    "REASONING_MAX_CHARS",
    "REASONING_EFFORT",
    "WAVE_WAIT_FOR_S",
    "VERIFIER_WAIT_FOR_S",
    "SYNTHESIZER_WAIT_FOR_S",
    "SINGLE_PASS_WAIT_FOR_S",
    "SOCKET_READ_TIMEOUT_S",
    "BUDGET_MARGIN_S",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_when_env_unset():
    cfg = multi_agent_config()
    assert cfg.multi_agent == 0
    assert cfg.multi_agent_phase0 == 1
    assert cfg.fanout_concurrency == 3
    assert cfg.mutex_lease_ttl_s == 900
    assert cfg.reasoning_max_chars == 4000
    assert cfg.reasoning_effort == "low"
    assert cfg.wave_wait_for_s == 300
    assert cfg.verifier_wait_for_s == 240
    assert cfg.synthesizer_wait_for_s == 180
    assert cfg.single_pass_wait_for_s == 240
    assert cfg.socket_read_timeout_s == 240
    assert cfg.budget_margin_s == 60


def test_default_constants_pin_checklist_item_7():
    assert DEFAULT_MULTI_AGENT == 0
    assert DEFAULT_MULTI_AGENT_PHASE0 == 1
    assert DEFAULT_FANOUT_CONCURRENCY == 3
    assert DEFAULT_MUTEX_LEASE_TTL_S == 900
    assert DEFAULT_REASONING_MAX_CHARS == 4000
    assert DEFAULT_REASONING_EFFORT == "low"
    assert DEFAULT_WAVE_WAIT_FOR_S == 300
    assert DEFAULT_VERIFIER_WAIT_FOR_S == 240
    assert DEFAULT_SYNTHESIZER_WAIT_FOR_S == 180
    assert DEFAULT_SINGLE_PASS_WAIT_FOR_S == 240
    assert DEFAULT_SOCKET_READ_TIMEOUT_S == 240
    assert DEFAULT_BUDGET_MARGIN_S == 60


def test_reasoning_effort_default_is_provisional():
    # PROVISIONAL until the Phase-0 exit ruling: D6/D8 pick the ship
    # config from measured p95 — this default must not pre-decide it.
    assert DEFAULT_REASONING_EFFORT == "low"
    assert multi_agent_config().reasoning_effort == "low"


def test_set_values_parsed(monkeypatch):
    monkeypatch.setenv("MULTI_AGENT", "1")
    monkeypatch.setenv("MULTI_AGENT_PHASE0", "1")
    monkeypatch.setenv("FANOUT_CONCURRENCY", "5")
    monkeypatch.setenv("MUTEX_LEASE_TTL_S", "600")
    monkeypatch.setenv("REASONING_MAX_CHARS", "8000")
    monkeypatch.setenv("REASONING_EFFORT", "high")
    monkeypatch.setenv("WAVE_WAIT_FOR_S", "120")
    monkeypatch.setenv("VERIFIER_WAIT_FOR_S", "100")
    monkeypatch.setenv("SYNTHESIZER_WAIT_FOR_S", "90")
    monkeypatch.setenv("SINGLE_PASS_WAIT_FOR_S", "200")
    monkeypatch.setenv("SOCKET_READ_TIMEOUT_S", "100")
    monkeypatch.setenv("BUDGET_MARGIN_S", "30")
    cfg = multi_agent_config()
    assert cfg.multi_agent == 1
    assert cfg.multi_agent_phase0 == 1
    assert cfg.fanout_concurrency == 5
    assert cfg.mutex_lease_ttl_s == 600
    assert cfg.reasoning_max_chars == 8000
    assert cfg.reasoning_effort == "high"
    assert cfg.wave_wait_for_s == 120
    assert cfg.verifier_wait_for_s == 100
    assert cfg.synthesizer_wait_for_s == 90
    assert cfg.single_pass_wait_for_s == 200
    assert cfg.socket_read_timeout_s == 100
    assert cfg.budget_margin_s == 30


@pytest.mark.parametrize(
    ("env", "field", "default"),
    [
        ("MULTI_AGENT", "multi_agent", 0),
        ("MULTI_AGENT_PHASE0", "multi_agent_phase0", 1),
        ("FANOUT_CONCURRENCY", "fanout_concurrency", 3),
        ("MUTEX_LEASE_TTL_S", "mutex_lease_ttl_s", 900),
        ("REASONING_MAX_CHARS", "reasoning_max_chars", 4000),
        ("WAVE_WAIT_FOR_S", "wave_wait_for_s", 300),
        ("VERIFIER_WAIT_FOR_S", "verifier_wait_for_s", 240),
        ("SYNTHESIZER_WAIT_FOR_S", "synthesizer_wait_for_s", 180),
        ("SINGLE_PASS_WAIT_FOR_S", "single_pass_wait_for_s", 240),
        ("SOCKET_READ_TIMEOUT_S", "socket_read_timeout_s", 240),
        ("BUDGET_MARGIN_S", "budget_margin_s", 60),
    ],
)
@pytest.mark.parametrize("raw", ["", "bogus", "12x", "4.5"])
def test_invalid_int_falls_back_to_default(monkeypatch, env, field, default, raw):
    monkeypatch.setenv(env, raw)
    assert getattr(multi_agent_config(), field) == default


def test_empty_effort_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("REASONING_EFFORT", "")
    assert multi_agent_config().reasoning_effort == "low"


def test_resolved_per_read_not_import_time(monkeypatch):
    """Two reads with different env values observe different knobs —
    nothing is cached in module state (and `reset_config_cache`, which
    drops only the SSM provider, can never freeze these)."""
    monkeypatch.setenv("FANOUT_CONCURRENCY", "2")
    assert multi_agent_config().fanout_concurrency == 2
    monkeypatch.setenv("FANOUT_CONCURRENCY", "3")
    assert multi_agent_config().fanout_concurrency == 3


def test_withdrawn_margin_names_absent():
    """Naming pin (checklist item 7): `MARGIN_S` and
    `DEGRADED_BUDGET_MARGIN_S` are withdrawn — only `BUDGET_MARGIN_S`
    (here `DEFAULT_BUDGET_MARGIN_S` / `budget_margin_s`) exists."""
    assert not hasattr(config, "MARGIN_S")
    assert not hasattr(config, "DEGRADED_BUDGET_MARGIN_S")
    assert DEFAULT_BUDGET_MARGIN_S == 60
    assert multi_agent_config().budget_margin_s == 60

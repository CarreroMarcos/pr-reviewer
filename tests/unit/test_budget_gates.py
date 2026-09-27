"""SPR-97 T012: elapsed-budget gate contract tests (HLD-004 D9).

Four cumulative gates over Lambda remaining time: pre-wave covers
wave+verifier+synth+margin; pre-verifier covers verifier+synth+margin
(else degrade NOW); pre-synth covers synth+margin; pre-fallback covers
single-pass+margin. Thresholds are computed FROM the config constants so
config changes re-pin the arithmetic; the 900s/810s/780s envelope
numbers are the only literals (D9/task-text pins).

RED state: `common.fanout` does not exist — collection errors.
"""

import pytest

from common.config import MultiAgentConfig, multi_agent_config
from common.fanout import (
    single_pass_budget_ok,
    synthesizer_budget_ok,
    verifier_budget_ok,
    wave_budget_ok,
)

MS_PER_S = 1000
# D9/task-text pins — the only literals in this file.
GATE1_TOTAL_MS = 780_000
ENVELOPE_FIT_MS = 810_000

GATE_ENV_VARS = (
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


@pytest.fixture()
def cfg():
    return multi_agent_config()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in GATE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _gate1_threshold(cfg):
    return (
        cfg.wave_wait_for_s
        + cfg.verifier_wait_for_s
        + cfg.synthesizer_wait_for_s
        + cfg.budget_margin_s
    ) * MS_PER_S


def _gate2_threshold(cfg):
    return (
        cfg.verifier_wait_for_s + cfg.synthesizer_wait_for_s + cfg.budget_margin_s
    ) * MS_PER_S


def _gate3_threshold(cfg):
    return (cfg.synthesizer_wait_for_s + cfg.budget_margin_s) * MS_PER_S


def _gate4_threshold(cfg):
    return (cfg.single_pass_wait_for_s + cfg.budget_margin_s) * MS_PER_S


# --- per-gate threshold semantics --------------------------------------------------


def test_pre_wave_passes_at_threshold(cfg):
    assert wave_budget_ok(_gate1_threshold(cfg), cfg) is True


def test_pre_wave_degrades_below_threshold(cfg):
    assert wave_budget_ok(_gate1_threshold(cfg) - 1, cfg) is False


def test_pre_wave_passes_above_threshold(cfg):
    assert wave_budget_ok(_gate1_threshold(cfg) + 1, cfg) is True


def test_pre_verifier_passes_at_threshold(cfg):
    assert verifier_budget_ok(_gate2_threshold(cfg), cfg) is True


def test_pre_verifier_degrades_below_threshold(cfg):
    assert verifier_budget_ok(_gate2_threshold(cfg) - 1, cfg) is False


def test_pre_verifier_passes_above_threshold(cfg):
    assert verifier_budget_ok(_gate2_threshold(cfg) + 1, cfg) is True


def test_pre_synth_passes_at_threshold(cfg):
    assert synthesizer_budget_ok(_gate3_threshold(cfg), cfg) is True


def test_pre_synth_degrades_below_threshold(cfg):
    assert synthesizer_budget_ok(_gate3_threshold(cfg) - 1, cfg) is False


def test_pre_synth_passes_above_threshold(cfg):
    assert synthesizer_budget_ok(_gate3_threshold(cfg) + 1, cfg) is True


def test_pre_fallback_passes_at_threshold(cfg):
    assert single_pass_budget_ok(_gate4_threshold(cfg), cfg) is True


def test_pre_fallback_degrades_below_threshold(cfg):
    assert single_pass_budget_ok(_gate4_threshold(cfg) - 1, cfg) is False


def test_pre_fallback_passes_above_threshold(cfg):
    assert single_pass_budget_ok(_gate4_threshold(cfg) + 1, cfg) is True


@pytest.mark.parametrize(
    "gate",
    [wave_budget_ok, verifier_budget_ok, synthesizer_budget_ok, single_pass_budget_ok],
    ids=["wave", "verifier", "synth", "fallback"],
)
def test_zero_remaining_degrades_every_gate(cfg, gate):
    assert gate(0, cfg) is False


# --- cumulative chain -----------------------------------------------------------------


def test_gate1_pass_implies_downstream_gates_pass(cfg):
    """Gate totals nest (780s ≥ 480s ≥ 240s): a run that can afford the
    wave can afford every later stage."""
    remaining = _gate1_threshold(cfg)
    assert wave_budget_ok(remaining, cfg) is True
    assert verifier_budget_ok(remaining, cfg) is True
    assert synthesizer_budget_ok(remaining, cfg) is True
    assert single_pass_budget_ok(remaining, cfg) is True


def test_gate1_fail_can_still_afford_fallback(cfg):
    """The degrade-NOW path: no wave budget, but single-pass fits — the
    run degrades instead of dying silently."""
    remaining = _gate4_threshold(cfg)
    assert wave_budget_ok(remaining, cfg) is False
    assert single_pass_budget_ok(remaining, cfg) is True


# --- thresholds derive from cfg, not hardcoded -------------------------------------------


def test_thresholds_derive_from_cfg():
    custom = MultiAgentConfig(
        multi_agent=0,
        multi_agent_phase0=0,
        fanout_concurrency=3,
        mutex_lease_ttl_s=900,
        reasoning_max_chars=4000,
        reasoning_effort="low",
        wave_wait_for_s=10,
        verifier_wait_for_s=20,
        synthesizer_wait_for_s=30,
        single_pass_wait_for_s=40,
        socket_read_timeout_s=240,
        budget_margin_s=5,
    )
    assert wave_budget_ok(65_000, custom) is True
    assert wave_budget_ok(64_999, custom) is False
    assert verifier_budget_ok(55_000, custom) is True
    assert synthesizer_budget_ok(35_000, custom) is True
    assert single_pass_budget_ok(45_000, custom) is True


# --- envelope pins (D9: 900s cap − ~90s overhead ≈ 810s) --------------------------------------


def test_gate1_total_is_780s(cfg):
    assert _gate1_threshold(cfg) == GATE1_TOTAL_MS


def test_every_gate_total_fits_810s_envelope(cfg):
    for threshold in (
        _gate1_threshold(cfg),
        _gate2_threshold(cfg),
        _gate3_threshold(cfg),
        _gate4_threshold(cfg),
    ):
        assert threshold <= ENVELOPE_FIT_MS

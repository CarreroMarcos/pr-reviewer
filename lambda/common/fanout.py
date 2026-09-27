"""Multi-agent fan-out stage (HLD-004 §5, D9).

This module currently hosts ONLY the elapsed-budget gates (D9
Cumulative Downstream Protection): pure predicates over Lambda
remaining time that decide proceed vs degrade-to-single-pass BEFORE
each stage burns budget. Wave/verifier/synthesizer orchestration
(`run_fanout`) lands in later tickets (T015+) and will call these
gates at each stage boundary — keep this section separable.

Gate semantics (D9): each gate checks ALL remaining downstream stages
to publication, so a stage never starts it cannot finish (the
catastrophic mode is the verifier burning the budget and leaving
nothing to synthesize or fall back with — publishing NO comment).
Thresholds derive from the `MultiAgentConfig` knobs; the 900s worker
cap and ~90s fixed claim/fence/publish/finalize/archive overhead bound
the configured totals (gate-1 total 780s ≤ ~810s envelope).

Pure stdlib, no I/O, no logging, no event emission (T015's duty).
"""

from __future__ import annotations

from common.config import MultiAgentConfig

_MS_PER_S = 1000


def wave_budget_ok(remaining_ms: int, cfg: MultiAgentConfig) -> bool:
    """Pre-wave gate (D9.1): remaining covers wave + verifier +
    synthesizer + margin. False → skip fan-out entirely."""
    return (
        remaining_ms
        >= (
            cfg.wave_wait_for_s
            + cfg.verifier_wait_for_s
            + cfg.synthesizer_wait_for_s
            + cfg.budget_margin_s
        )
        * _MS_PER_S
    )


def verifier_budget_ok(remaining_ms: int, cfg: MultiAgentConfig) -> bool:
    """Pre-verifier gate (D9.2): remaining covers verifier + synthesizer
    + margin. False → degrade to single-pass fallback NOW, while
    single-pass still has budget."""
    return (
        remaining_ms
        >= (cfg.verifier_wait_for_s + cfg.synthesizer_wait_for_s + cfg.budget_margin_s) * _MS_PER_S
    )


def synthesizer_budget_ok(remaining_ms: int, cfg: MultiAgentConfig) -> bool:
    """Pre-synthesizer gate (D9.3): remaining covers synthesizer +
    margin. False → degrade to single-pass fallback."""
    return remaining_ms >= (cfg.synthesizer_wait_for_s + cfg.budget_margin_s) * _MS_PER_S


def single_pass_budget_ok(remaining_ms: int, cfg: MultiAgentConfig) -> bool:
    """Pre-fallback gate (D9.4): remaining covers a single-pass call +
    margin. False → no budget left even for fallback (terminal)."""
    return remaining_ms >= (cfg.single_pass_wait_for_s + cfg.budget_margin_s) * _MS_PER_S

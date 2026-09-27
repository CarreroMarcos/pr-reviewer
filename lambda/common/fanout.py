"""Multi-agent fan-out stage (HLD-004 §5, D1, D9).

Two sections, kept separable:

1. Elapsed-budget gates (D9 Cumulative Downstream Protection): pure
   predicates over Lambda remaining time that decide proceed vs
   degrade-to-single-pass BEFORE each stage burns budget.
2. Wave stage (D1): three specialist coroutines over an explicit
   per-invocation `ThreadPoolExecutor` (runtime-fix pattern verbatim),
   fresh `asyncio.run` per call, events emitted ONLY from the coroutines
   on the loop thread, ≥2-survivor rule with `FanoutDegraded` fallback.

Verifier/synthesizer orchestration (`run_fanout`) lands in later
tickets and will call both sections at each stage boundary.

Pure stdlib, no I/O, no logging. Event emission here is the wave's own
stage events (T015's duty); downstream stages emit theirs.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from common import llm
from common.config import MultiAgentConfig
from common.events import (
    agent_completed,
    agent_failed,
    agent_reasoning,
    agent_started,
    degraded_to_single_pass,
)
from common.findings import FindingsError, parse_candidate_findings
from common.llm import LlmError, ReviewResult

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


class FanoutDegraded(Exception):
    """Wave/verifier/synthesizer stage failure: degrade to single-pass
    fallback. `reason` names the cause (`all_specialists_failed`,
    `insufficient_wave_survivors`, …); `failed_stage` names the stage
    (`wave`, `verifier`, `synthesizer`). Caught INSIDE the review closure
    around `run_fanout` only — never propagates to `run_review` or the
    worker boundary (HLD §5 FanoutDegraded wiring pin)."""

    def __init__(self, reason: str, failed_stage: str) -> None:
        self.reason = reason
        self.failed_stage = failed_stage
        super().__init__(f"fanout degraded: {failed_stage}: {reason}")


@dataclass(frozen=True)
class WaveSurvivor:
    """One specialist that returned parseable findings: its specialty,
    validated findings (never carrying candidate IDs — assigned later by
    `run_fanout`), and the capture-truncated reasoning excerpt (`None`
    when the call returned no reasoning)."""

    specialty: str
    findings: list[dict[str, Any]]
    reasoning_excerpt: str | None


@dataclass(frozen=True)
class _SpecialistOutcome:
    survived: bool
    survivor: WaveSurvivor | None
    error_class: str | None


def _latency_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * _MS_PER_S))


async def _specialist_coro(
    *,
    loop: asyncio.AbstractEventLoop,
    pool: ThreadPoolExecutor,
    fn: Callable[..., ReviewResult],
    run_id: str,
    cfg: MultiAgentConfig,
    specialty: str,
    api_key: str,
    model: str,
    endpoint: str,
    system_prompt: str,
    diff_text: str,
    events: list[dict[str, Any]],
    read_timeout_s: int,
    effort: str,
    allowed_hosts: frozenset[str] | None,
) -> _SpecialistOutcome:
    """One specialist wave leg — runs ENTIRELY on the loop thread except
    the sync provider call it dispatches to the pool. Emits
    `agent_started` pre-dispatch and `agent_reasoning`/`agent_completed`
    (or `agent_failed`) as the executor call returns. Never raises
    except on cancellation (timeout — recorded first) or programming
    faults (bad run_id shape), which stay loud."""
    events.append(agent_started(specialty=specialty, run_id=run_id))
    start = time.monotonic()
    try:
        result = await loop.run_in_executor(
            pool,
            functools.partial(
                fn,
                api_key=api_key,
                model=model,
                endpoint=endpoint,
                system_prompt=system_prompt,
                diff_text=diff_text,
                thinking_enabled=True,
                reasoning_effort=effort,
                read_timeout_s=read_timeout_s,
                allowed_hosts=allowed_hosts,
            ),
        )
    except asyncio.CancelledError:
        # Wave window expired while dispatched: the abandoned pool thread
        # is bounded by the socket read timeout; the loss is recorded and
        # absorbed by the ≥2-survivor rule. Re-raise — cancellation must
        # propagate so the task ends cancelled.
        events.append(
            agent_failed(
                specialty=specialty,
                error_class="timeout",
                latency_ms=_latency_ms(start),
                run_id=run_id,
            )
        )
        raise
    except LlmError as exc:
        # Typed provider failure (429/1302 fail fast here as
        # `rate_limit`; 401 classifies as `http_401`): single attempt,
        # NO in-executor retry — all retry ownership stays with the queue.
        events.append(
            agent_failed(
                specialty=specialty,
                error_class=exc.error_class,
                latency_ms=_latency_ms(start),
                run_id=run_id,
            )
        )
        return _SpecialistOutcome(False, None, exc.error_class)
    except Exception:
        # Untyped escape from the sync call (contract says LlmError
        # only): recorded, never retried, never propagated as a wave
        # crash — the survivor rule absorbs it.
        events.append(
            agent_failed(
                specialty=specialty,
                error_class="unknown",
                latency_ms=_latency_ms(start),
                run_id=run_id,
            )
        )
        return _SpecialistOutcome(False, None, "unknown")
    try:
        findings = parse_candidate_findings(json.loads(result.content))
    except (ValueError, FindingsError):
        # 200 with unusable content (non-JSON or off-schema): the
        # provider answered but nothing parseable came back.
        events.append(
            agent_failed(
                specialty=specialty,
                error_class="invalid_response",
                latency_ms=_latency_ms(start),
                run_id=run_id,
            )
        )
        return _SpecialistOutcome(False, None, "invalid_response")
    # Capture-truncated reasoning excerpt (D6): raw reasoning sliced at
    # REASONING_MAX_CHARS the moment it arrives — never summarized, never
    # a second model call. Absent reasoning emits no event (the replay
    # renders reasoning WHEN PRESENT).
    excerpt = None
    if result.reasoning_content:
        excerpt = result.reasoning_content[: cfg.reasoning_max_chars]
    if excerpt:
        events.append(
            agent_reasoning(specialty=specialty, reasoning_excerpt=excerpt, run_id=run_id)
        )
    events.append(
        agent_completed(
            specialty=specialty,
            findings_n=len(findings),
            latency_ms=_latency_ms(start),
            tokens_in=result.prompt_tokens,
            tokens_out=result.completion_tokens,
            findings=findings,
            run_id=run_id,
        )
    )
    return _SpecialistOutcome(True, WaveSurvivor(specialty, findings, excerpt), None)


async def _wave_async(
    *,
    run_id: str,
    cfg: MultiAgentConfig,
    pool: ThreadPoolExecutor,
    fn: Callable[..., ReviewResult],
    api_key: str,
    model: str,
    endpoint: str,
    system_prompts: Mapping[str, str],
    diff_text: str,
    specialties: tuple[str, ...],
    events: list[dict[str, Any]],
    read_timeout_s: int,
    effort: str,
    allowed_hosts: frozenset[str] | None,
) -> list[WaveSurvivor]:
    loop = asyncio.get_running_loop()
    tasks = [
        loop.create_task(
            _specialist_coro(
                loop=loop,
                pool=pool,
                fn=fn,
                run_id=run_id,
                cfg=cfg,
                specialty=specialty,
                api_key=api_key,
                model=model,
                endpoint=endpoint,
                system_prompt=system_prompts[specialty],
                diff_text=diff_text,
                events=events,
                read_timeout_s=read_timeout_s,
                effort=effort,
                allowed_hosts=allowed_hosts,
            )
        )
        for specialty in specialties
    ]
    # One bounded wait, no short-circuit on early survivors: every task
    # is awaited (done for results, pending via cancel + gather).
    _, pending = await asyncio.wait(
        tasks, timeout=cfg.wave_wait_for_s, return_when=asyncio.ALL_COMPLETED
    )
    if pending:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    survivors: list[WaveSurvivor] = []
    losses: list[str] = []
    for task in tasks:
        if task.cancelled():
            # Window-expired: agent_failed{timeout} was recorded inside
            # the coroutine before it re-raised.
            continue
        outcome = task.result()
        if outcome.survived and outcome.survivor is not None:
            survivors.append(outcome.survivor)
        else:
            losses.append(outcome.error_class or "unknown")
    if len(survivors) >= 2:
        return survivors
    if losses and all(error_class == "http_401" for error_class in losses):
        # Uniform auth failure (D1 Credential Thread Safety): the sync
        # fallback owns the single-threaded creds refresh from SSM. The
        # survivor (if any) does not contradict stale creds — it raced
        # ahead of expiry — so refresh-on-401-evidence is the recovery.
        events.append(
            degraded_to_single_pass(
                reason="all_specialists_failed", failed_stage="wave", run_id=run_id
            )
        )
        raise FanoutDegraded("all_specialists_failed", "wave")
    raise FanoutDegraded("insufficient_wave_survivors", "wave")


def run_wave(
    *,
    run_id: str,
    cfg: MultiAgentConfig,
    api_key: str,
    model: str,
    endpoint: str,
    system_prompts: Mapping[str, str],
    diff_text: str,
    events: list[dict[str, Any]],
    specialties: tuple[str, ...] = ("correctness", "security", "tests"),
    review_fn: Callable[..., ReviewResult] | None = None,
    allowed_hosts: frozenset[str] | None = None,
) -> list[WaveSurvivor]:
    """Run the multi-agent wave: dispatch every specialty's sync
    provider call onto one explicit per-invocation pool, collect
    survivors (D1 runtime-fix pattern verbatim).

    `cfg` is the immutable pre-hydrated snapshot (D1 Credential Thread
    Safety — never re-read, never mutated); `events` is caller-owned and
    appended ONLY from the coroutines on the loop thread, never from
    pool threads and never from this sync entry point. `system_prompts`
    carries one assembled prompt per specialty (missing specialty =
    loud `KeyError`, fail fast before dispatch). The socket budget is
    the `_read_timeout_s()` resolution, resolved ONCE here and passed
    explicitly to every specialist call (HLD §9 item 1).

    Returns ≥2 survivors; otherwise raises `FanoutDegraded` for the
    review closure to catch (single-pass fallback owns recovery).
    """
    for specialty in specialties:
        system_prompts[specialty]
    fn = review_fn if review_fn is not None else llm.review_diff
    read_timeout_s = llm._read_timeout_s()
    # ADV-9 (first wiring PR): env-sourced effort rides stripped.
    effort = cfg.reasoning_effort.strip()
    pool = ThreadPoolExecutor(
        max_workers=cfg.fanout_concurrency,
        thread_name_prefix=f"fanout-{run_id[:8]}",
    )
    try:
        return asyncio.run(
            _wave_async(
                run_id=run_id,
                cfg=cfg,
                pool=pool,
                fn=fn,
                api_key=api_key,
                model=model,
                endpoint=endpoint,
                system_prompts=system_prompts,
                diff_text=diff_text,
                specialties=specialties,
                events=events,
                read_timeout_s=read_timeout_s,
                effort=effort,
                allowed_hosts=allowed_hosts,
            )
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

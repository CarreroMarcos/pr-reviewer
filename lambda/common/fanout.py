"""Multi-agent fan-out stage (HLD-004 §5, D1, D3, D9).

Three sections, kept separable:

1. Elapsed-budget gates (D9 Cumulative Downstream Protection): pure
   predicates over Lambda remaining time that decide proceed vs
   degrade-to-single-pass BEFORE each stage burns budget.
2. Wave stage (D1): three specialist coroutines over an explicit
   per-invocation `ThreadPoolExecutor` (runtime-fix pattern verbatim),
   fresh `asyncio.run` per call, events emitted ONLY from the coroutines
   on the loop thread, ≥2-survivor rule with `FanoutDegraded` fallback.
3. Verifier stage (D3): one falsification leg over the same pool/loop
   machinery, closed-schema validation of the model verdict, policy
   routing (HIGH never killed; kills need cited reasons), post-image
   re-anchor counting onto `verification_done`.
4. Synthesizer stage (D3, §5 item 3): deterministic four-condition
   duplicate merge over verifier survivors (STOPWORDS title similarity,
   highest severity wins, adjacent ordering), exact `## Findings`
   rendering, one comment leg over the same pool/loop machinery.

Sequencer assembly (`run_fanout`) lands in a later ticket and will call
these sections at each stage boundary.

Pure stdlib, no I/O, no logging. Event emission here is each stage's
own events; downstream stages emit theirs.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
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
    review_synthesized,
    synthesizer_failed,
    verification_done,
    verification_failed,
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
    if not survivors and losses and all(error_class == "http_401" for error_class in losses):
        # Uniform auth failure across EVERY specialist — HLD D1's literal
        # antecedent ("when all specialists fail on 401"). Recovery is the
        # sync fallback's lazy SSM refresh; this wave never touches creds.
        # A mixed wave with a survivor is survivor-rule territory below.
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
        if specialty not in system_prompts:
            raise KeyError(specialty)  # loud pre-dispatch failure (docstring pin)
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


# Verifier stage (HLD-004 D3). reroute marker: fixed system provenance
# for kills the policy refuses (HIGH, or reasonless) — the downstream
# synthesizer renders it beside [Requires Verification].
_POLICY_REROUTE_NOTE = "rerouted by policy: kill lacked evidence citation"

_VERIFIED_FIELDS = frozenset(
    {
        "candidate_id",
        "file_path",
        "line_start",
        "line_end",
        "title",
        "description",
        "suggested_fix",
        "severity",
        "category",
        "verification_note",
    }
)
_KILLED_FIELDS = frozenset({"candidate_id", "kill_reason"})
_ESCALATED_FIELDS = frozenset(
    {
        "candidate_id",
        "file_path",
        "line_start",
        "line_end",
        "title",
        "description",
        "suggested_fix",
        "severity",
        "category",
        "escalation_reason",
    }
)
_VERDICT_KEYS = frozenset({"verified", "killed", "escalated"})


class _VerifierFailure(Exception):
    """One verifier leg failed with a known class: the collector emits
    `verification_failed` and raises `FanoutDegraded`. Internal
    control flow only — never crosses the stage boundary."""

    def __init__(self, error_class: str, latency_ms: int) -> None:
        self.error_class = error_class
        self.latency_ms = latency_ms
        super().__init__(f"verifier leg failed: {error_class}")


class _InvalidOutput(Exception):
    """Model verdict violated the closed §6 schema or the echo contract.
    Raised during validation; converted to `_VerifierFailure` with the
    uniform `invalid_response` class at the single conversion point."""

    pass


def _require_str(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise _InvalidOutput(f"bad {key}")
    return value


def _require_line(item: dict[str, Any], key: str) -> int:
    # Integer coordinates, bools excluded. NO minimum: the §6 verifier
    # item schema shows no minimum (unlike the candidate schema) —
    # re-anchoring and the downstream clamp own ranges, not this gate.
    value = item.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise _InvalidOutput(f"bad {key}")
    return value


def _check_verified_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != _VERIFIED_FIELDS:
        raise _InvalidOutput("bad verified item")
    # Severity/category ride through as strings (Gate-6(c): no
    # category-vs-specialty runtime check v1) — enums stay prompt
    # contract, exactly as the §6 verifier schema (plain strings).
    return {
        "candidate_id": _require_str(item, "candidate_id"),
        "file_path": _require_str(item, "file_path"),
        "line_start": _require_line(item, "line_start"),
        "line_end": _require_line(item, "line_end"),
        "title": _require_str(item, "title"),
        "description": _require_str(item, "description"),
        "suggested_fix": _require_str(item, "suggested_fix"),
        "severity": _require_str(item, "severity"),
        "category": _require_str(item, "category"),
        "verification_note": _require_str(item, "verification_note"),
    }


def _check_killed_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != _KILLED_FIELDS:
        raise _InvalidOutput("bad killed item")
    return {
        "candidate_id": _require_str(item, "candidate_id"),
        "kill_reason": _require_str(item, "kill_reason"),
    }


def _check_escalated_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) != _ESCALATED_FIELDS:
        raise _InvalidOutput("bad escalated item")
    return {
        "candidate_id": _require_str(item, "candidate_id"),
        "file_path": _require_str(item, "file_path"),
        "line_start": _require_line(item, "line_start"),
        "line_end": _require_line(item, "line_end"),
        "title": _require_str(item, "title"),
        "description": _require_str(item, "description"),
        "suggested_fix": _require_str(item, "suggested_fix"),
        "severity": _require_str(item, "severity"),
        "category": _require_str(item, "category"),
        "escalation_reason": _require_str(item, "escalation_reason"),
    }


def _validate_verifier_output(
    payload: Any, known_ids: frozenset[str]
) -> dict[str, list[dict[str, Any]]]:
    """Closed-schema + echo validation (§6 Verifier Output pin).

    Top level is exactly {verified, killed, escalated} arrays; every
    item carries exactly its array's properties (unknown fields fail,
    never forward); every `candidate_id` is echoed exactly once across
    all three arrays — unknown, duplicated, or omitted IDs fail.
    Raises `_InvalidOutput` on any violation.
    """
    if not isinstance(payload, dict) or set(payload) != _VERDICT_KEYS:
        raise _InvalidOutput("bad verdict shape")
    checkers = {
        "verified": _check_verified_item,
        "killed": _check_killed_item,
        "escalated": _check_escalated_item,
    }
    validated: dict[str, list[dict[str, Any]]] = {}
    for key in ("verified", "killed", "escalated"):
        items = payload[key]
        if not isinstance(items, list):
            raise _InvalidOutput(f"bad {key}")
        validated[key] = [checkers[key](item) for item in items]
    seen: dict[str, str] = {}
    for key in ("verified", "killed", "escalated"):
        for item in validated[key]:
            cid = item["candidate_id"]
            if cid in seen or cid not in known_ids:
                raise _InvalidOutput("bad candidate_id echo")
            seen[cid] = key
    if set(seen) != set(known_ids):
        # MUST-echo violation (HLD §6): an assigned ID with no verdict
        # is malformed output — never a silent drop.
        raise _InvalidOutput("incomplete candidate_id echo")
    return validated


def _route_verdict(
    validated: dict[str, list[dict[str, Any]]],
    by_id: Mapping[str, dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Apply the D3 kill policy + count re-anchors.

    HIGH-severity kills reroute to escalated (never killed, no
    exceptions); MEDIUM/LOW kills stand ONLY with a non-blank
    kill_reason (whitespace cites nothing; the evidence-citation
    substance is prompt contract) —
    reasonless kills reroute to escalated, never fail. Rerouted items
    are rebuilt from the trusted candidate coordinates (killed items
    carry none) with the model's reason preserved, or the fixed policy
    marker when there is none. Returns (verdict, reanchored_n) where
    reanchored_n counts verified/escalated items whose coordinates
    differ from the assigned candidate's.
    """
    verdict = {
        "verified": list(validated["verified"]),
        "killed": [],
        "escalated": list(validated["escalated"]),
    }
    for item in validated["killed"]:
        cid = item["candidate_id"]
        candidate = by_id[cid]
        cited = item["kill_reason"].strip()
        if candidate.get("severity") == "HIGH" or not cited:
            verdict["escalated"].append(
                {
                    "candidate_id": cid,
                    "file_path": candidate["file_path"],
                    "line_start": candidate["line_start"],
                    "line_end": candidate["line_end"],
                    "title": candidate["title"],
                    "description": candidate["description"],
                    "suggested_fix": candidate["suggested_fix"],
                    "severity": candidate["severity"],
                    "category": candidate["category"],
                    "escalation_reason": cited or _POLICY_REROUTE_NOTE,
                }
            )
        else:
            verdict["killed"].append({"candidate_id": cid, "kill_reason": item["kill_reason"]})
    reanchored_n = 0
    for item in verdict["verified"] + verdict["escalated"]:
        candidate = by_id[item["candidate_id"]]
        if (
            item["file_path"],
            item["line_start"],
            item["line_end"],
        ) != (
            candidate["file_path"],
            candidate["line_start"],
            candidate["line_end"],
        ):
            reanchored_n += 1
    return verdict, reanchored_n


async def _verifier_coro(
    *,
    loop: asyncio.AbstractEventLoop,
    pool: ThreadPoolExecutor,
    fn: Callable[..., ReviewResult],
    run_id: str,
    api_key: str,
    model: str,
    endpoint: str,
    verifier_prompt: str,
    diff_text: str,
    read_timeout_s: int,
    effort: str,
    allowed_hosts: frozenset[str] | None,
) -> tuple[dict[str, Any], int, int]:
    """One falsification leg — same authorship discipline as the wave:
    the sync provider call dispatches to the pool while this coroutine,
    on the loop thread, awaits it. Returns (parsed payload, tokens_in,
    tokens_out). Raises `_VerifierFailure` for typed provider faults
    and malformed model output; cancellation (window expiry) propagates
    for the collector to record."""
    start = time.monotonic()
    try:
        result = await loop.run_in_executor(
            pool,
            functools.partial(
                fn,
                api_key=api_key,
                model=model,
                endpoint=endpoint,
                system_prompt=verifier_prompt,
                diff_text=diff_text,
                thinking_enabled=True,
                reasoning_effort=effort,
                read_timeout_s=read_timeout_s,
                allowed_hosts=allowed_hosts,
            ),
        )
    except asyncio.CancelledError:
        raise
    except LlmError as exc:
        # Single attempt, NO retry (a 429/1302 here fails fast to the
        # fallback path, same as the wave).
        raise _VerifierFailure(exc.error_class, _latency_ms(start)) from None
    except Exception as exc:
        raise _VerifierFailure("unknown", _latency_ms(start)) from exc
    try:
        payload = json.loads(result.content)
    except ValueError as exc:
        raise _VerifierFailure("invalid_response", _latency_ms(start)) from exc
    return payload, result.prompt_tokens, result.completion_tokens


async def _verify_async(
    *,
    run_id: str,
    cfg: MultiAgentConfig,
    pool: ThreadPoolExecutor,
    fn: Callable[..., ReviewResult],
    api_key: str,
    model: str,
    endpoint: str,
    verifier_prompt: str,
    candidates: list[dict[str, Any]],
    diff_text: str,
    wave_survivors: int,
    events: list[dict[str, Any]],
    read_timeout_s: int,
    effort: str,
    allowed_hosts: frozenset[str] | None,
) -> dict[str, list[dict[str, Any]]]:
    start = time.monotonic()
    by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    loop = asyncio.get_running_loop()
    task = loop.create_task(
        _verifier_coro(
            loop=loop,
            pool=pool,
            fn=fn,
            run_id=run_id,
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            verifier_prompt=verifier_prompt,
            diff_text=diff_text,
            read_timeout_s=read_timeout_s,
            effort=effort,
            allowed_hosts=allowed_hosts,
        )
    )
    _, pending = await asyncio.wait(
        {task}, timeout=cfg.verifier_wait_for_s, return_when=asyncio.ALL_COMPLETED
    )
    if pending:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        latency_ms = _latency_ms(start)
        events.append(
            verification_failed(error_class="timeout", latency_ms=latency_ms, run_id=run_id)
        )
        raise FanoutDegraded("timeout", "verifier")
    try:
        payload, tokens_in, tokens_out = task.result()
    except _VerifierFailure as exc:
        events.append(
            verification_failed(
                error_class=exc.error_class, latency_ms=exc.latency_ms, run_id=run_id
            )
        )
        raise FanoutDegraded(exc.error_class, "verifier") from None
    try:
        validated = _validate_verifier_output(payload, frozenset(by_id))
    except _InvalidOutput:
        latency_ms = _latency_ms(start)
        events.append(
            verification_failed(
                error_class="invalid_response", latency_ms=latency_ms, run_id=run_id
            )
        )
        raise FanoutDegraded("invalid_response", "verifier") from None
    verdict, reanchored_n = _route_verdict(validated, by_id)
    events.append(
        verification_done(
            survived_n=len(verdict["verified"]),
            killed_n=len(verdict["killed"]),
            escalated_n=len(verdict["escalated"]),
            wave_survivors=wave_survivors,
            latency_ms=_latency_ms(start),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            verified=verdict["verified"],
            killed=verdict["killed"],
            escalated=verdict["escalated"],
            coordinates_reanchored_n=reanchored_n,
            run_id=run_id,
        )
    )
    return verdict


def run_verifier(
    *,
    run_id: str,
    cfg: MultiAgentConfig,
    api_key: str,
    model: str,
    endpoint: str,
    verifier_prompt: str,
    candidates: list[dict[str, Any]],
    diff_text: str,
    wave_survivors: int,
    events: list[dict[str, Any]],
    review_fn: Callable[..., ReviewResult] | None = None,
    allowed_hosts: frozenset[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Run the falsification stage: one verifier leg over an explicit
    per-invocation pool (max_workers=1), fresh `asyncio.run` per call,
    closed-schema validation of the model verdict, D3 policy routing.

    `candidates` carry assigned `candidate_id`s (run_fanout's
    post-wave assignment — never model-generated); every ID must be
    echoed exactly once. `wave_survivors` is wave data carried onto the
    `verification_done` event. The socket budget is the
    `_read_timeout_s()` resolution, resolved ONCE here and passed
    explicitly (HLD §9 item 1).

    Returns {"verified", "killed", "escalated"}; any leg or validation
    failure emits `verification_failed` and raises `FanoutDegraded`
    for the review closure to catch.
    """
    fn = review_fn if review_fn is not None else llm.review_diff
    read_timeout_s = llm._read_timeout_s()
    # ADV-9 (first wiring PR): env-sourced effort rides stripped.
    effort = cfg.reasoning_effort.strip()
    pool = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=f"fanout-{run_id[:8]}",
    )
    try:
        return asyncio.run(
            _verify_async(
                run_id=run_id,
                cfg=cfg,
                pool=pool,
                fn=fn,
                api_key=api_key,
                model=model,
                endpoint=endpoint,
                verifier_prompt=verifier_prompt,
                candidates=candidates,
                diff_text=diff_text,
                wave_survivors=wave_survivors,
                events=events,
                read_timeout_s=read_timeout_s,
                effort=effort,
                allowed_hosts=allowed_hosts,
            )
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# Synthesizer stage (HLD-004 D3, §5 item 3). Pre-registered title-guard
# contract: duplicate titles match at token Jaccard ≥ 0.6 over
# lowercased tokens with stopwords removed. STOPWORDS is the frozen
# infrastructure set below — conventional English grammar words
# (multi-char only, so single-letter variable names keep their signal),
# deliberately WITHOUT code-signaling words (null, missing, leak,
# injection, unused, race, check, error, fail, ...). Threshold AND set
# are tunable only by Phase-0 data (D8 effort-selection pre-registration
# discipline); the ANTONym-pair caveat (with/without, before/after…
# strip symmetrically per conventional lists) is accepted residual
# noise inside the conjunctive four-condition rule, likewise tunable.
STOPWORDS = frozenset(
    {
        "about",
        "above",
        "across",
        "after",
        "again",
        "against",
        "all",
        "almost",
        "along",
        "already",
        "also",
        "always",
        "among",
        "another",
        "any",
        "anyone",
        "anything",
        "around",
        "because",
        "before",
        "behind",
        "below",
        "between",
        "beyond",
        "both",
        "cannot",
        "could",
        "despite",
        "does",
        "doing",
        "done",
        "down",
        "during",
        "each",
        "either",
        "enough",
        "even",
        "every",
        "everyone",
        "everything",
        "further",
        "having",
        "however",
        "into",
        "itself",
        "moreover",
        "mostly",
        "neither",
        "never",
        "nevertheless",
        "next",
        "nobody",
        "none",
        "nor",
        "not",
        "nothing",
        "now",
        "nowhere",
        "often",
        "otherwise",
        "over",
        "rather",
        "same",
        "several",
        "should",
        "since",
        "some",
        "someone",
        "something",
        "still",
        "such",
        "than",
        "therefore",
        "though",
        "through",
        "toward",
        "towards",
        "under",
        "unless",
        "until",
        "upon",
        "versus",
        "whether",
        "while",
        "within",
        "without",
        "would",
        "an",
        "and",
        "are",
        "was",
        "were",
        "been",
        "being",
        "will",
        "shall",
        "must",
        "might",
        "may",
        "very",
        "much",
        "many",
        "most",
        "more",
        "less",
        "least",
        "only",
        "just",
        "ever",
        "quite",
        "maybe",
        "perhaps",
        "indeed",
        "instead",
        "likewise",
        "anyway",
        "besides",
        "furthermore",
        "hence",
        "thus",
        "accordingly",
        "meanwhile",
        "the",
        "with",
        "from",
        "that",
        "this",
        "these",
        "those",
        "then",
        "there",
        "here",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "what",
        "how",
        "why",
        "them",
        "they",
        "their",
        "theirs",
        "your",
        "yours",
        "our",
        "ours",
        "its",
        "up",
        "out",
        "off",
        "on",
        "in",
        "at",
        "to",
        "of",
        "for",
        "by",
        "as",
        "is",
        "be",
        "am",
        "or",
        "so",
        "too",
        "can",
    }
)

TITLE_SIMILARITY_THRESHOLD = 0.6
_LINE_PROXIMITY = 2

_SEVERITY_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SLOT_RE = re.compile(r"\{\{FINDINGS_SECTION\}\}|\{\{ACCEPTED_RESIDUALS\}\}")


def _title_tokens(title: str) -> frozenset[str]:
    return frozenset(token for token in _TOKEN_RE.findall(title.lower()) if token not in STOPWORDS)


def _title_jaccard(first: str, second: str) -> float:
    left, right = _title_tokens(first), _title_tokens(second)
    union = left | right
    if not union:
        # No positive evidence either way: similarity 0 (never merge on
        # vacuous titles — the guard fails closed toward keeping).
        return 0.0
    return len(left & right) / len(union)


def _norm_path(path: str) -> str:
    cleaned = path.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _duplicates(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """D3 four-condition duplicate predicate: same file AND proximate
    lines AND same category AND similar titles. All four, no shortcuts."""
    return (
        _norm_path(first["file_path"]) == _norm_path(second["file_path"])
        and abs(first["line_start"] - second["line_start"]) <= _LINE_PROXIMITY
        and first["category"] == second["category"]
        and _title_jaccard(first["title"], second["title"]) >= TITLE_SIMILARITY_THRESHOLD
    )


def _severity_rank(severity: Any) -> int:
    # Unknown strings (verifier passthrough, Gate-6(c)) rank 0: they
    # never outrank a known severity; ties keep the anchor.
    return _SEVERITY_RANK.get(severity, 0) if isinstance(severity, str) else 0


def dedupe_findings(
    verified: list[dict[str, Any]], escalated: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Deterministic duplicate merge over verifier survivors (HLD D3).

    Survivors pool in location-major order (path, start, end, category,
    title — fully deterministic regardless of model output order, and
    non-duplicate same-location pairs land ADJACENT as pinned). Each
    item greedily joins the first anchor group it duplicates (anchor
    linkage — no transitive chaining across distant items). Merged
    groups keep the anchor's fields with severity upgraded to the group
    maximum; any-escalated groups stay escalated (the flag never drops
    silently). Merged items carry the kept anchor's fields plus an
    `escalated` bool — nothing else is added or removed.
    """
    flagged = [(dict(item), False) for item in verified]
    flagged += [(dict(item), True) for item in escalated]
    flagged.sort(
        key=lambda pair: (
            _norm_path(pair[0]["file_path"]),
            pair[0]["line_start"],
            pair[0]["line_end"],
            pair[0]["category"],
            pair[0]["title"],
        )
    )
    groups: list[list[dict[str, Any]]] = []
    group_escalated: list[bool] = []
    for item, is_escalated in flagged:
        anchor = None
        for index, group in enumerate(groups):
            if _duplicates(group[0], item):
                anchor = index
                break
        if anchor is None:
            groups.append([item])
            group_escalated.append(is_escalated)
            continue
        group = groups[anchor]
        if _severity_rank(item["severity"]) > _severity_rank(group[0]["severity"]):
            group[0]["severity"] = item["severity"]
        group_escalated[anchor] = group_escalated[anchor] or is_escalated
    merged = []
    for group, is_escalated in zip(groups, group_escalated, strict=True):
        entry = group[0]
        entry["escalated"] = is_escalated
        merged.append(entry)
    return merged


def _one_line(text: Any) -> str:
    # Single-line bullets: the scorer counts wrapped continuations as
    # unparsable, so collapse all whitespace runs (titles, descriptions
    # and fixes are model-controlled free text).
    return re.sub(r"\s+", " ", str(text)).strip()


def render_findings_section(merged: list[dict[str, Any]]) -> str:
    """Render the exact `## Findings` section (HLD D3, §5 item 3).

    One `- [SEVERITY] \\`path:line\\` — title. Description. Fix: …`
    bullet per merged item, backticked location (the scorer REQUIRES
    backticked `path:LINE` — unbackticked locations parse as
    unparsable), escalated items at ORIGINAL severity with the
    `[Requires Verification]` marker. Empty input renders the
    single-pass sentinel sentence (the scorer skips it: zero findings,
    zero unparsable).
    """
    if not merged:
        return "## Findings\n\nNo significant issues found.\n"
    lines = ["## Findings", ""]
    for item in merged:
        marker = "[Requires Verification] " if item.get("escalated") else ""
        lines.append(
            f"- [{item['severity']}] `{item['file_path']}:{item['line_start']}`"
            f" — {marker}{_one_line(item['title'])}. {_one_line(item['description'])}"
            f" Fix: {_one_line(item['suggested_fix'])}."
        )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class SynthResult:
    """Synthesizer outcome: the model-rendered canonical comment, the
    deterministic merged findings JSON (event + eval truth), and the
    duplicate-drop count (`survivors − bullets`)."""

    comment: str
    merged: list[dict[str, Any]]
    dropped_as_duplicate_n: int


class _SynthFailure(Exception):
    """One synthesizer leg failed with a known class: the collector
    emits `synthesizer_failed` and raises `FanoutDegraded`. Internal
    control flow only — never crosses the stage boundary."""

    def __init__(self, error_class: str, latency_ms: int) -> None:
        self.error_class = error_class
        self.latency_ms = latency_ms
        super().__init__(f"synthesizer leg failed: {error_class}")


def _render_residuals(residuals: list[str]) -> str:
    # Gate-6(d): empty residuals substitute as the empty string.
    return "\n".join(f"- {residual}" for residual in residuals)


def _fill_prompt(template: str, section: str, residuals: str) -> str:
    # Single-pass slot substitution (Gate-6(a) payload-literal safety):
    # replacements never re-scan each other, so finding/residual text
    # containing slot-looking markers cannot hijack the template.
    def replace(match: re.Match[str]) -> str:
        if "FINDINGS_SECTION" in match.group(0):
            return section
        return residuals

    return _SLOT_RE.sub(replace, template)


async def _synth_coro(
    *,
    loop: asyncio.AbstractEventLoop,
    pool: ThreadPoolExecutor,
    fn: Callable[..., ReviewResult],
    prompt: str,
    read_timeout_s: int,
    effort: str,
    api_key: str,
    model: str,
    endpoint: str,
    allowed_hosts: frozenset[str] | None,
) -> tuple[str, int, int]:
    """One comment-render leg — same authorship discipline as the wave:
    the sync provider call dispatches to the pool while this coroutine,
    on the loop thread, awaits it. Returns (comment, tokens_in,
    tokens_out). Raises `_SynthFailure`; cancellation propagates for
    the collector to record."""
    start = time.monotonic()
    try:
        result = await loop.run_in_executor(
            pool,
            functools.partial(
                fn,
                api_key=api_key,
                model=model,
                endpoint=endpoint,
                system_prompt=prompt,
                thinking_enabled=True,
                reasoning_effort=effort,
                read_timeout_s=read_timeout_s,
                allowed_hosts=allowed_hosts,
            ),
        )
    except asyncio.CancelledError:
        raise
    except LlmError as exc:
        raise _SynthFailure(exc.error_class, _latency_ms(start)) from None
    except Exception as exc:
        raise _SynthFailure("unknown", _latency_ms(start)) from exc
    return result.content, result.prompt_tokens, result.completion_tokens


async def _synth_async(
    *,
    run_id: str,
    cfg: MultiAgentConfig,
    pool: ThreadPoolExecutor,
    fn: Callable[..., ReviewResult],
    prompt: str,
    merged: list[dict[str, Any]],
    dropped_as_duplicate_n: int,
    read_timeout_s: int,
    effort: str,
    api_key: str,
    model: str,
    endpoint: str,
    allowed_hosts: frozenset[str] | None,
    events: list[dict[str, Any]],
) -> SynthResult:
    start = time.monotonic()
    loop = asyncio.get_running_loop()
    task = loop.create_task(
        _synth_coro(
            loop=loop,
            pool=pool,
            fn=fn,
            prompt=prompt,
            read_timeout_s=read_timeout_s,
            effort=effort,
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            allowed_hosts=allowed_hosts,
        )
    )
    _, pending = await asyncio.wait(
        {task}, timeout=cfg.synthesizer_wait_for_s, return_when=asyncio.ALL_COMPLETED
    )
    if pending:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        latency_ms = _latency_ms(start)
        events.append(
            synthesizer_failed(error_class="timeout", latency_ms=latency_ms, run_id=run_id)
        )
        raise FanoutDegraded("timeout", "synthesizer")
    try:
        comment, tokens_in, tokens_out = task.result()
    except _SynthFailure as exc:
        events.append(
            synthesizer_failed(
                error_class=exc.error_class, latency_ms=exc.latency_ms, run_id=run_id
            )
        )
        raise FanoutDegraded(exc.error_class, "synthesizer") from None
    # Trust boundary (documented): the comment's ## Findings fidelity to
    # `merged` is prompt-contract (verbatim-copy instruction) and
    # eval-measured (D8 Gate 4) — never runtime-rejected. A paraphrasing
    # model degrades eval scores, not this stage: the event always
    # carries the deterministic code truth.
    events.append(
        review_synthesized(
            findings_merged_n=len(merged),
            dropped_as_duplicate_n=dropped_as_duplicate_n,
            latency_ms=_latency_ms(start),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            findings=merged,
            run_id=run_id,
        )
    )
    return SynthResult(
        comment=comment, merged=merged, dropped_as_duplicate_n=dropped_as_duplicate_n
    )


def run_synthesizer(
    *,
    run_id: str,
    cfg: MultiAgentConfig,
    api_key: str,
    model: str,
    endpoint: str,
    synth_prompt: str,
    verified: list[dict[str, Any]],
    escalated: list[dict[str, Any]],
    residuals: list[str] | None = None,
    review_fn: Callable[..., ReviewResult] | None = None,
    allowed_hosts: frozenset[str] | None = None,
    events: list[dict[str, Any]],
) -> SynthResult:
    """Run the synthesis stage: deterministic duplicate merge, exact
    ## Findings rendering into the prompt (verbatim-copy block), one
    comment leg, `review_synthesized` event with code-exact counts.

    `verified`/`escalated` are the verifier's finding-shaped items;
    `residuals` are accepted-residual lines (HLD §5 flow feeds them to
    the synthesizer — raw lines here, never the loader: ADV-17's file
    wiring belongs to the assembly ticket). `synth_prompt` is the
    template carrying `{{FINDINGS_SECTION}}` + `{{ACCEPTED_RESIDUALS}}`
    slots (NO `{{DIFF}}` — the flow feeds the synthesizer no diff; the
    summary derives solely from the findings). Empty survivors still
    run the leg (uniform behavior): merged is empty, the section is the
    sentinel, counts zeroed.

    Returns `SynthResult`; any leg failure emits `synthesizer_failed`
    and raises `FanoutDegraded` for the review closure to catch.
    """
    merged = dedupe_findings(verified, escalated)
    dropped_as_duplicate_n = (len(verified) + len(escalated)) - len(merged)
    prompt = _fill_prompt(
        synth_prompt,
        render_findings_section(merged),
        _render_residuals(residuals or []),
    )
    fn = review_fn if review_fn is not None else llm.review_diff
    read_timeout_s = llm._read_timeout_s()
    # ADV-9 (first wiring PR): env-sourced effort rides stripped.
    effort = cfg.reasoning_effort.strip()
    pool = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=f"fanout-{run_id[:8]}",
    )
    try:
        return asyncio.run(
            _synth_async(
                run_id=run_id,
                cfg=cfg,
                pool=pool,
                fn=fn,
                prompt=prompt,
                merged=merged,
                dropped_as_duplicate_n=dropped_as_duplicate_n,
                read_timeout_s=read_timeout_s,
                effort=effort,
                api_key=api_key,
                model=model,
                endpoint=endpoint,
                allowed_hosts=allowed_hosts,
                events=events,
            )
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

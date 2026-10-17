"""Shared arithmetic for interleaved victim/attacker runs.

One planner serves the declared timing window, the executor's finish check and
the runtime, so the planned physical duration can never differ from the events
the runtime commits. Only the shared-region transition model is covered: a
tenant handover costs one service reset, the same rule the single-shot path
uses. Placements that avoid a handover reset are rejected before this runs.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from core.attack_simulation.circuit_program import CircuitExecutionOperation


@dataclass(frozen=True)
class InterleavedWork:
    duration_us: float
    event_count: int


def circuit_shot_duration_us(operation: CircuitExecutionOperation,
                            reset_duration_us: float, measurement_duration_us: float) -> float:
    """Physical time of one shot, matching DeclaredQpuExecutor._duration."""
    duration = sum(
        item.duration_us if item.kind in {"unitary", "delay"}
        else reset_duration_us if item.kind in {"initialize", "reset"} else 0.0
        for item in operation.instructions
    )
    return duration + measurement_duration_us * len({
        item.time_group for item in operation.instructions if item.kind == "measure"})


def interleaved_work(operation: CircuitExecutionOperation, attacker_tenant_id: str,
                     previous_tenant_id: str | None, reset_duration_us: float,
                     measurement_duration_us: float) -> InterleavedWork:
    """Round = [handover reset] victim, handover reset, attacker shot."""
    victim = operation.interleave
    if victim is None:
        raise ValueError("operation is not interleaved")
    if victim.tenant_id == attacker_tenant_id:
        raise ValueError("victim and attacker tenants must differ")
    rounds = operation.shots
    victim_us = circuit_shot_duration_us(victim.operation, reset_duration_us, measurement_duration_us)
    attacker_us = circuit_shot_duration_us(operation, reset_duration_us, measurement_duration_us)
    entry_resets = int(previous_tenant_id is not None and previous_tenant_id != victim.tenant_id)
    # victim->attacker every round, attacker->victim before every later round.
    resets = entry_resets + rounds + (rounds - 1)
    return InterleavedWork(
        duration_us=rounds * (victim_us + attacker_us) + resets * reset_duration_us,
        event_count=2 * rounds + resets,
    )


def derived_seed(seed: int, round_index: int, role: str) -> int:
    """Deterministic per-round sampling seed that never repeats across roles."""
    digest = hashlib.sha256(f"{seed}:{round_index}:{role}".encode("ascii")).digest()
    return int.from_bytes(digest[:4], "big")

"""Where in a job's shots each externally given kick falls, and the rotation it applies.

Stage 2 of the concurrent-sharing design: pure placement, no state. A kick is placed at instruction
granularity: it is applied just before the first instruction that starts after the kick's time, so
instructions that started at or before the kick have already run. A kick before the first shot's
first instruction (the handover reset) or in the wait between shots goes to the start of the next
shot; a kick during the final measurements goes just before the measurement is sampled; a kick after
the last shot has ended acts on the state that shot leaves behind.

The kick's ``phase_rad`` is used as the rotation axis exactly as given. Turning a hardware carrier
phase into the simulation's logical axis (adding the target's virtual-Z frame) is not done here.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
import math

import numpy as np

from core.attack_simulation.concurrent_windows import DriveSchedule, UnitaryKick
from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.shot_activity import instruction_starts


@dataclass(frozen=True)
class PlacedKick:
    """``kick`` applied before instruction ``position`` of its shot (``len(instructions)``: at the end)."""

    position: int
    kick: UnitaryKick


@dataclass(frozen=True)
class KickPlan:
    """Kicks per shot index, each shot's list in time order; ``trailing`` acts after the last shot."""

    per_shot: tuple[tuple[PlacedKick, ...], ...]
    trailing: tuple[UnitaryKick, ...] = ()

    def of_shot(self, shot_index: int) -> tuple[PlacedKick, ...]:
        return self.per_shot[shot_index] if shot_index < len(self.per_shot) else ()


EMPTY_PLAN = KickPlan(())


def kick_unitary(kick: UnitaryKick) -> np.ndarray:
    """exp(-i theta/2 (cos(phase) X + sin(phase) Y))."""
    half = 0.5 * kick.angle_rad
    off = -1j * math.sin(half)
    return np.array([[math.cos(half), off * np.exp(-1j * kick.phase_rad)],
                     [off * np.exp(1j * kick.phase_rad), math.cos(half)]], dtype=complex)


def place_kicks(kicks: Sequence[UnitaryKick], operation: CircuitExecutionOperation, schedule: DriveSchedule,
                *, window_start_us: float, reset_duration_us: float) -> KickPlan:
    """Assign window-absolute kicks to shots and instruction positions."""
    if any(type(kick) is not UnitaryKick for kick in kicks):
        raise TypeError("kicks must contain UnitaryKick values")
    starts, _ = instruction_starts(operation, reset_duration_us=reset_duration_us)
    keys = [math.inf if start is None else start for start in starts]
    if keys != sorted(keys):
        raise ValueError("instruction start times must not decrease")
    shots, period = schedule.shots, schedule.period_us
    per_shot: list[list[PlacedKick]] = [[] for _ in range(shots)]
    trailing: list[UnitaryKick] = []
    for kick in sorted(kicks, key=lambda k: (k.time_us, k.qubit, k.source_job_id, k.source_qubit)):
        relative = kick.time_us - window_start_us - schedule.offset_us
        if relative < 0.0:
            per_shot[0].append(PlacedKick(0, kick))
            continue
        index = min(shots - 1, int(relative // period)) if period > 0.0 else shots - 1
        local = relative - index * period
        if local >= schedule.shot.duration_us:       # after this shot's measurements: its wait, or the end
            if index + 1 < shots:
                per_shot[index + 1].append(PlacedKick(0, kick))
            else:
                trailing.append(kick)
            continue
        per_shot[index].append(PlacedKick(bisect_right(keys, local), kick))
    return KickPlan(tuple(tuple(shot) for shot in per_shot), tuple(trailing))

"""Where in a job's shots each externally given kick falls, and the rotation it applies.

Stage 2 of the concurrent-sharing design: pure placement, no state. A kick is placed at instruction
granularity: it is applied just before the first instruction that starts after the kick's time, so
instructions that started at or before the kick have already run. A kick before the first shot's
first instruction (the handover reset) or in the wait between shots goes to the start of the next
shot; a kick during the final measurements goes just before the measurement is sampled; a kick after
the last shot has ended acts on the state that shot leaves behind.

A kick's ``phase_rad`` is the hardware carrier phase of its axis. The simulator applies virtual Z as a
real rotation of the state, so the axis it must use is shifted by the target qubit's accumulated
frame at the kick's position: ``axis = phase + frame`` (the sign is to be confirmed against the
pulse-level model). ``place_kicks`` makes that shift, with the same position it places the kick at.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
import math

import numpy as np

from core.attack_simulation.concurrent_windows import DriveSchedule, UnitaryKick
from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.shot_activity import frame_before, instruction_starts


@dataclass(frozen=True)
class PlacedKick:
    """``kick`` applied before instruction ``position`` of its shot (``len(instructions)``: at the end).

    ``axis_rad`` is the rotation axis in the simulator's frame, in the XY plane.
    """

    position: int
    kick: UnitaryKick
    axis_rad: float

    def __post_init__(self) -> None:
        if type(self.position) is not int or self.position < 0:
            raise ValueError("kick position must be a nonnegative index")
        if type(self.kick) is not UnitaryKick:
            raise TypeError("a UnitaryKick is required")
        if type(self.axis_rad) not in (int, float) or not math.isfinite(self.axis_rad):
            raise ValueError("kick axis must be finite")


@dataclass(frozen=True)
class KickPlan:
    """Kicks per shot index, each shot's list in time order; ``trailing`` acts after the last shot."""

    per_shot: tuple[tuple[PlacedKick, ...], ...]
    trailing: tuple[PlacedKick, ...] = ()

    def of_shot(self, shot_index: int) -> tuple[PlacedKick, ...]:
        return self.per_shot[shot_index] if shot_index < len(self.per_shot) else ()


EMPTY_PLAN = KickPlan(())


def kick_unitary(placed: PlacedKick) -> np.ndarray:
    """exp(-i theta/2 (cos(axis) X + sin(axis) Y))."""
    half = 0.5 * placed.kick.angle_rad
    off = -1j * math.sin(half)
    return np.array([[math.cos(half), off * np.exp(-1j * placed.axis_rad)],
                     [off * np.exp(1j * placed.axis_rad), math.cos(half)]], dtype=complex)


def place_kicks(kicks: Sequence[UnitaryKick], operation: CircuitExecutionOperation, schedule: DriveSchedule,
                *, window_start_us: float, reset_duration_us: float,
                layout_qubit_ids: Sequence[int]) -> KickPlan:
    """Assign window-absolute kicks to shots and instruction positions, with their axes in the simulator's frame.

    ``layout_qubit_ids[i]`` is the physical ID of program qubit ``i``. A kick on a qubit outside the
    program has no frame (nothing drives it), so its axis is its phase.
    """
    if any(type(kick) is not UnitaryKick for kick in kicks):
        raise TypeError("kicks must contain UnitaryKick values")
    program_qubit = {physical: index for index, physical in enumerate(layout_qubit_ids)}
    starts, _ = instruction_starts(operation, reset_duration_us=reset_duration_us)
    keys = [math.inf if start is None else start for start in starts]
    if keys != sorted(keys):
        raise ValueError("instruction start times must not decrease")
    end = len(operation.instructions)

    def place(position: int, kick: UnitaryKick) -> PlacedKick:
        index = program_qubit.get(kick.qubit)
        frame = 0.0 if index is None else frame_before(operation, position, index)
        return PlacedKick(position, kick, kick.phase_rad + frame)

    shots, period = schedule.shots, schedule.period_us
    per_shot: list[list[PlacedKick]] = [[] for _ in range(shots)]
    trailing: list[PlacedKick] = []
    for kick in sorted(kicks, key=lambda k: (k.time_us, k.qubit, k.source_job_id, k.source_qubit)):
        relative = kick.time_us - window_start_us - schedule.offset_us
        if relative < 0.0:
            per_shot[0].append(place(0, kick))
            continue
        index = min(shots - 1, int(relative // period)) if period > 0.0 else shots - 1
        local = relative - index * period
        if local >= schedule.shot.duration_us:       # after this shot's measurements: its wait, or the end
            if index + 1 < shots:
                per_shot[index + 1].append(place(0, kick))
            else:
                trailing.append(place(end, kick))
            continue
        per_shot[index].append(place(bisect_right(keys, local), kick))
    return KickPlan(tuple(tuple(shot) for shot in per_shot), tuple(trailing))

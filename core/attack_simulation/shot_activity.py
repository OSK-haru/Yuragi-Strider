"""What a compiled Program does to each qubit, and when, within one shot.

This is the first step of the concurrent-sharing design: a pure reading of a ``CircuitExecutionOperation``
into timed activity records. It reads no state and decides nothing about other jobs. A later step
turns the records of overlapping jobs into effects (for example drive crosstalk), so the record kinds
are generic rather than specific to one effect.

Times are microseconds from the start of the shot. Qubits are the Program's own indices, not
physical IDs. The compiler charges time by a layer rule: gates of one layer run together, the layer's
first instruction carries the layer's length and the other instructions carry 0. Each instruction also
keeps its own duration (``intrinsic_us``), which is how a later instruction of a layer is told apart
from a virtual gate: it starts with the layer's first instruction, not after it.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.native_pulses import PULSE_ANGLES

ACTIVITY_KINDS = ("drive", "frame", "two_qubit", "idle", "reset", "measure")


@dataclass(frozen=True)
class ActivityInterval:
    """One qubit doing one thing over ``[start_us, end_us)``; a ``frame`` shift is instantaneous.

    * ``drive``: a microwave pulse of rotation ``angle_rad`` at hardware carrier phase ``phase_rad``
      (a virtual Z of ``a`` shifts the later pulses of that qubit by ``-a``).
    * ``frame``: a virtual Z; ``angle_rad`` is the shift.
    * ``two_qubit``: a two-qubit gate acting on this qubit.
    * ``idle``: a declared wait. ``reset`` and ``measure``: service operations on the register.
    """

    qubit: int
    kind: str
    start_us: float
    end_us: float
    angle_rad: float = 0.0
    phase_rad: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in ACTIVITY_KINDS:
            raise ValueError("unknown activity kind")
        if type(self.qubit) is not int or self.qubit < 0:
            raise ValueError("activity qubit must be a nonnegative index")
        values = (self.start_us, self.end_us, self.angle_rad, self.phase_rad)
        if not all(math.isfinite(value) for value in values) or self.start_us < 0.0 or self.end_us < self.start_us:
            raise ValueError("activity times must be finite with 0 <= start <= end")
        if self.kind == "frame" and self.end_us != self.start_us:
            raise ValueError("a frame shift is instantaneous")


@dataclass(frozen=True)
class ShotActivity:
    duration_us: float
    intervals: tuple[ActivityInterval, ...]

    def of_qubit(self, qubit: int) -> tuple[ActivityInterval, ...]:
        return tuple(item for item in self.intervals if item.qubit == qubit)


def shot_activity(operation: CircuitExecutionOperation, *, reset_duration_us: float,
                  measurement_duration_us: float) -> ShotActivity:
    """Read one shot of a compiled Program into activity records.

    ``duration_us`` equals the per-shot time the planner charges (reset, gates, measurements); the
    wait between shots belongs to the job, not to the shot.
    """
    if type(operation) is not CircuitExecutionOperation:
        raise TypeError("a compiled Program is required")
    intervals: list[ActivityInterval] = []
    frames: dict[int, float] = {}
    now = 0.0
    carrier_start = 0.0
    carrier_group: int | None = None
    measured_groups: list[int] = []

    for item in operation.instructions:
        if item.kind in {"initialize", "reset"}:
            intervals.extend(ActivityInterval(q, "reset", now, now + reset_duration_us) for q in item.qubits)
            now += reset_duration_us
            carrier_start, carrier_group = now, None
            continue
        if item.kind == "measure":
            if item.time_group not in measured_groups:
                measured_groups.append(item.time_group)
            continue

        # unitary or delay: the layer rule decides where the instruction starts.
        if item.duration_us > 0.0:
            start, carrier_start, carrier_group = now, now, item.time_group
            now += item.duration_us
        elif item.intrinsic_us > 0.0 or item.time_group == carrier_group:
            start = carrier_start      # a later instruction of the layer runs with its first one
        else:
            start = now
        if item.kind == "delay":
            if item.wait_us > 0.0:
                intervals.extend(ActivityInterval(q, "idle", start, start + item.wait_us) for q in item.qubits)
        elif item.natives:
            qubit = item.qubits[0]
            cursor = start
            for step in item.natives:
                if step.name == "rz":
                    intervals.append(ActivityInterval(qubit, "frame", cursor, cursor, angle_rad=step.angle_rad))
                    frames[qubit] = frames.get(qubit, 0.0) + step.angle_rad
                else:
                    intervals.append(ActivityInterval(
                        qubit, "drive", cursor, cursor + step.duration_us,
                        angle_rad=PULSE_ANGLES[step.name], phase_rad=-frames.get(qubit, 0.0)))
                    cursor += step.duration_us
        elif len(item.qubits) == 2 and item.intrinsic_us > 0.0:
            intervals.extend(ActivityInterval(q, "two_qubit", start, start + item.intrinsic_us) for q in item.qubits)

    for group in measured_groups:
        qubits = tuple(q for item in operation.instructions if item.kind == "measure" and item.time_group == group
                       for q in item.qubits)
        intervals.extend(ActivityInterval(q, "measure", now, now + measurement_duration_us) for q in qubits)
        now += measurement_duration_us
    intervals.sort(key=lambda a: (a.start_us, a.qubit, a.kind))
    return ShotActivity(now, tuple(intervals))

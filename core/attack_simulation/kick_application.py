"""Where in a job's shots the leaks from other jobs act, and the rotation each one becomes.

Stage 2 of the concurrent-sharing design: pure placement, no state. A leak (``DriveLeak``) is spread
over time; it is turned into a few instantaneous rotations ("kicks") placed between the target's
instructions, so the runtime only has to apply a rotation before an instruction.

How a leak becomes a kick, in the simulator's frame (virtual Z applied to the state as a real rotation):

* The rotation vector is the Magnus expansion of the leak to second order,
  ``r = int v dt + 1/2 int int_{t1>t2} v(t1) x v(t2) dt2 dt1`` with ``v(t) = rate(t) n(t)`` and ``n(t)`` the
  axis at hardware phase ``arg(rate)`` plus the target's virtual-Z frame at time ``t``; the kick is
  ``exp(-i r.sigma / 2)``. The first term is the leak's net rotation; the second is what a leak whose
  axis turns (a detuned drive, or a drive seen from a turning target) adds beyond it, which for a
  far-detuned drive is its main effect (an AC-Stark-like rotation about the axis perpendicular to the
  leak). It is computed by cutting the leak into short slices. Third order and the cross terms between
  two different sources are neglected (cross terms between sources are of the same order as the
  second-order term of each).
* While the target runs its own gate (``rz``/``sx``/``x`` steps), the leak is carried along by the
  target's rotation. The integral is taken in the gate's interaction picture, ``n(t)`` rotated back by
  everything the gate has done up to ``t``, and the kick goes *before* the gate. A single kick at the
  gate's start with the plain axis would get the effect of a concurrent pi pulse wrong by order one.
* A leak while the target does something else (waits, a two-qubit gate, reset, measurement, or has no
  program on that qubit) is placed before the first instruction that starts after the leak begins,
  with the axis at the frame in force there.
* A leak before the window starts (the gap since the previous job, the handover) or in the wait between
  shots goes to the start of the next shot; one after the last shot has ended acts on the state that
  shot leaves behind.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
import math

import numpy as np

from core.attack_simulation.concurrent_windows import DriveLeak, DriveSchedule
from core.attack_simulation.circuit_program import CircuitExecutionOperation
from core.attack_simulation.shot_activity import instruction_starts

SLICE_ANGLE_RAD = 0.4          # a slice turns the leak axis (relative to the target) by about this much
MAX_SLICES = 128               # the cost knob: more slices are more exact for a leak that turns very fast
SLICE_NODES = 6                # Gauss-Legendre nodes per slice

_SIGMA = (np.array([[0, 1], [1, 0]], dtype=complex), np.array([[0, -1j], [1j, 0]], dtype=complex),
          np.array([[1, 0], [0, -1]], dtype=complex))


@dataclass(frozen=True)
class PlacedKick:
    """A rotation ``exp(-i rotation.sigma / 2)`` of physical ``qubit``, applied before instruction ``position``.

    ``position == len(instructions)`` means at the end of the shot's instructions. ``rotation`` is the
    rotation vector (angle times axis) in the simulator's frame; ``sources`` are the job IDs it comes from.
    """

    position: int
    qubit: int
    rotation: tuple[float, float, float]
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.position) is not int or self.position < 0:
            raise ValueError("kick position must be a nonnegative index")
        if type(self.qubit) is not int or self.qubit < 0:
            raise ValueError("kick qubit must be a physical ID")
        rotation = tuple(float(value) for value in self.rotation)
        if len(rotation) != 3 or not all(math.isfinite(value) for value in rotation):
            raise ValueError("kick rotation must be a finite 3-vector")
        object.__setattr__(self, "rotation", rotation)


@dataclass(frozen=True)
class KickPlan:
    """Kicks per shot index, each shot's list in application order; ``trailing`` acts after the last shot."""

    per_shot: tuple[tuple[PlacedKick, ...], ...]
    trailing: tuple[PlacedKick, ...] = ()

    def of_shot(self, shot_index: int) -> tuple[PlacedKick, ...]:
        return self.per_shot[shot_index] if shot_index < len(self.per_shot) else ()


EMPTY_PLAN = KickPlan(())


def kick_unitary(placed: PlacedKick) -> np.ndarray:
    """exp(-i/2 r.sigma) for the kick's rotation vector r."""
    vector = np.array(placed.rotation)
    angle = float(np.linalg.norm(vector))
    if angle == 0.0:
        return np.eye(2, dtype=complex)
    axis = vector / angle
    generator = sum(component * pauli for component, pauli in zip(axis, _SIGMA))
    return math.cos(angle / 2.0) * np.eye(2, dtype=complex) - 1j * math.sin(angle / 2.0) * generator


def _rotation_z(angle: float) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _rotation_x(angle: float) -> np.ndarray:
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


@lru_cache(maxsize=MAX_SLICES + 2)
def _unit_slices(count: int) -> tuple[np.ndarray, np.ndarray]:
    """Gauss-Legendre nodes and weights on ``count`` equal slices of [0, 1), shape (count, SLICE_NODES)."""
    x, w = np.polynomial.legendre.leggauss(SLICE_NODES)
    left = np.arange(count)[:, None] / count
    return left + 0.5 * (x[None, :] + 1.0) / count, np.tile(0.5 * w / count, (count, 1))


def _slices(begin: float, end: float, count: int) -> tuple[np.ndarray, np.ndarray]:
    """Nodes and weights, shape (count, SLICE_NODES), on ``count`` equal slices of [begin, end)."""
    nodes, weights = _unit_slices(count)
    return begin + (end - begin) * nodes, (end - begin) * weights


def _slice_count(detuning: float, length: float, turning: float = 0.0) -> int:
    """Slices so that the leak axis, seen from the target, turns by about SLICE_ANGLE_RAD in each.

    A leak whose axis does not turn at all (resonant, nothing moving on the target) needs one slice.
    """
    turn = abs(detuning) * length + abs(turning)
    return 1 if turn == 0.0 else min(MAX_SLICES, max(3, math.ceil(turn / SLICE_ANGLE_RAD)))


def _magnus(slice_vectors: np.ndarray) -> np.ndarray:
    """Rotation vector of time-ordered slices: their sum plus 1/2 sum_k V_k x (V_1 + ... + V_{k-1})."""
    total = slice_vectors.sum(axis=0)
    if len(slice_vectors) < 2:
        return total
    earlier = np.cumsum(slice_vectors, axis=0) - slice_vectors
    v, e = slice_vectors, earlier
    second = np.array([np.dot(v[:, 1], e[:, 2]) - np.dot(v[:, 2], e[:, 1]),
                       np.dot(v[:, 2], e[:, 0]) - np.dot(v[:, 0], e[:, 2]),
                       np.dot(v[:, 0], e[:, 1]) - np.dot(v[:, 1], e[:, 0])])
    return total + 0.5 * second


@dataclass(frozen=True)
class _Gate:
    """A single-qubit gate of the target's with native steps, in shot-local time."""

    index: int
    start: float
    end: float
    frame_before: float
    steps: tuple[tuple, ...]            # ("rz", angle) or ("pulse", start, end, angle)


class _Timeline:
    """One program qubit's own evolution inside a shot: its gates, and its frame before each instruction."""

    def __init__(self, operation: CircuitExecutionOperation, starts: Sequence[float | None], qubit: int) -> None:
        frames = [0.0]
        gates: list[_Gate] = []
        frame = 0.0
        for index, item in enumerate(operation.instructions):
            if item.kind == "unitary" and item.qubits == (qubit,):
                if not item.natives:
                    raise ValueError("the virtual-Z frame is unknown: a single-qubit gate has no native steps")
                cursor, steps = starts[index], []
                for step in item.natives:
                    if step.name == "rz":
                        steps.append(("rz", step.angle_rad))
                    else:
                        steps.append(("pulse", cursor, cursor + step.duration_us, step.angle_rad))
                        cursor += step.duration_us
                if cursor > starts[index]:
                    gates.append(_Gate(index, starts[index], cursor, frame, tuple(steps)))
                frame += sum(step.angle_rad for step in item.natives if step.name == "rz")
            frames.append(frame)
        self.frames = frames
        self.gates = gates


def place_kicks(leaks: Sequence[DriveLeak], operation: CircuitExecutionOperation, schedule: DriveSchedule,
                *, window_start_us: float, reset_duration_us: float,
                layout_qubit_ids: Sequence[int]) -> KickPlan:
    """Turn window-absolute leaks into kicks at shot and instruction positions, in the simulator's frame.

    ``layout_qubit_ids[i]`` is the physical ID of program qubit ``i``; a leak on a qubit outside the
    program has no own evolution and no frame. Leaks that start before ``window_start_us`` are the gap
    since the previous job and go to the start of the first shot.
    """
    if any(type(leak) is not DriveLeak for leak in leaks):
        raise TypeError("leaks must contain DriveLeak values")
    program_qubit = {physical: index for index, physical in enumerate(layout_qubit_ids)}
    starts, _ = instruction_starts(operation, reset_duration_us=reset_duration_us)
    keys = [math.inf if start is None else start for start in starts]
    if keys != sorted(keys):
        raise ValueError("instruction start times must not decrease")
    end_position = len(operation.instructions)
    shots, period, duration = schedule.shots, schedule.period_us, schedule.shot.duration_us
    first_shot = window_start_us + schedule.offset_us
    target_shape = schedule.shot.pulse_shape
    timelines: dict[int, _Timeline] = {}

    def timeline(qubit: int) -> _Timeline:
        if qubit not in timelines:
            timelines[qubit] = _Timeline(operation, starts, qubit)
        return timelines[qubit]

    accumulated: dict[tuple[int | None, int, int], list] = {}

    def add(shot: int | None, position: int, leak: DriveLeak, vector: np.ndarray) -> None:
        entry = accumulated.setdefault((shot, position, leak.qubit), [np.zeros(3), set()])
        entry[0] += vector
        entry[1].add(leak.source_job_id)

    def plain(leak: DriveLeak, begin: float, end: float, frame: float) -> np.ndarray:
        """The leak's rotation vector over [begin, end) with the axis at a fixed frame (absolute times)."""
        t, w = _slices(begin, end, _slice_count(leak.detuning_rad_per_us, end - begin))
        v = leak.rate(t) * np.exp(1j * frame)
        return _magnus(np.stack([np.sum(w * v.real, axis=1), np.sum(w * v.imag, axis=1),
                                 np.zeros(len(t))], axis=1))

    def toggled(leak: DriveLeak, gate: _Gate, base: float, begin: float, end: float) -> np.ndarray:
        """The leak over local [begin, end) inside ``gate``, in the gate's interaction picture."""
        rotation, frame, pieces = np.eye(3), gate.frame_before, []
        for step in gate.steps:
            if step[0] == "rz":
                rotation, frame = _rotation_z(step[1]) @ rotation, frame + step[1]
                continue
            _, pulse_start, pulse_end, angle = step
            low, high = max(begin, pulse_start), min(end, pulse_end)
            if high > low:
                local, w = _slices(low, high, _slice_count(leak.detuning_rad_per_us, high - low, angle))
                v = leak.rate(base + local) * np.exp(1j * frame)
                psi = angle * target_shape.cumulative((local - pulse_start) / (pulse_end - pulse_start))
                c, s = np.cos(psi), np.sin(psi)
                pieces.append(np.stack([np.sum(w * v.real, axis=1), np.sum(w * c * v.imag, axis=1),
                                        np.sum(-w * s * v.imag, axis=1)], axis=1) @ rotation)
            rotation = _rotation_x(angle) @ rotation
        return _magnus(np.concatenate(pieces)) if pieces else np.zeros(3)

    for leak in sorted(leaks, key=lambda k: (k.start_us, k.qubit, k.source_job_id, k.source_qubit)):
        program = program_qubit.get(leak.qubit)
        frames = timeline(program).frames if program is not None else None
        if leak.start_us < first_shot:                 # the gap before the window, or the handover
            add(0, 0, leak, plain(leak, leak.start_us, min(leak.end_us, first_shot), 0.0))
        relative, stop = max(leak.start_us - first_shot, 0.0), leak.end_us - first_shot
        guard = 0
        while relative < stop:
            guard += 1
            if guard > 10_000_000:
                raise RuntimeError("leak placement did not advance")
            shot = min(shots - 1, int(relative // period)) if period > 0.0 else shots - 1
            if shot + 1 < shots and (shot + 1) * period <= relative:
                shot += 1
            local = relative - shot * period
            base = first_shot + shot * period
            if local >= duration:                      # after this shot's operations
                if shot + 1 < shots:
                    reach = min(stop, (shot + 1) * period)
                    if reach > relative:
                        add(shot + 1, 0, leak, plain(leak, first_shot + relative, first_shot + reach, 0.0))
                else:
                    frame = 0.0 if frames is None else frames[end_position]
                    add(None, end_position, leak, plain(leak, first_shot + relative, first_shot + stop, frame))
                    reach = stop
                relative = reach if reach > relative else math.nextafter(relative, math.inf)
                continue
            end_local = min(duration, stop - shot * period)
            cursor = local
            for gate in (timeline(program).gates if program is not None else ()):
                if gate.end <= cursor or gate.start >= end_local:
                    continue
                if gate.start > cursor:                # not in a gate of its own: placed between instructions
                    position = bisect_right(keys, cursor)
                    frame = 0.0 if frames is None else frames[position]
                    add(shot, position, leak, plain(leak, base + cursor, base + gate.start, frame))
                    cursor = gate.start
                inside_end = min(end_local, gate.end)
                add(shot, gate.index, leak, toggled(leak, gate, base, cursor, inside_end))
                cursor = inside_end
            if cursor < end_local:
                position = bisect_right(keys, cursor)
                frame = 0.0 if frames is None else frames[position]
                add(shot, position, leak, plain(leak, base + cursor, base + end_local, frame))
            advanced = shot * period + end_local
            relative = advanced if advanced > relative else math.nextafter(relative, math.inf)

    per_shot: list[list[PlacedKick]] = [[] for _ in range(shots)]
    trailing: list[PlacedKick] = []
    for (shot, position, qubit), (vector, sources) in sorted(
            accumulated.items(), key=lambda item: (-1 if item[0][0] is None else item[0][0], item[0][1], item[0][2])):
        placed = PlacedKick(position, qubit, tuple(vector), tuple(sorted(sources)))
        (trailing if shot is None else per_shot[shot]).append(placed)
    return KickPlan(tuple(tuple(shot) for shot in per_shot), tuple(trailing))

"""Execution windows, their overlap, and the drive-crosstalk kicks one window puts on another.

Stage 1 of the concurrent-sharing design: pure functions only. Nothing here schedules jobs, reads or
evolves QPU state, or decides how a kick is applied. The kicks are computed from declared activity
records alone (never from results), so every window's kicks are fixed once the windows are fixed.

Times are microseconds in the window's clock domain. Windows are half-open ``[start, finish)`` on the
event positions' ``time_us``: windows that only touch do not overlap, and tie order adds no duration.
Activity qubits are the Program's own indices; ``physical_qubits[i]`` is the physical ID of index ``i``,
and crosstalk transfers are declared between physical IDs.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
import cmath
import math

from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.shot_activity import ActivityInterval, ShotActivity
from core.experimental_isolation_physics.coherent_crosstalk import DriveCrosstalkTransfer

_SPAN_TOLERANCE_US = 1e-9


@dataclass(frozen=True)
class DriveSchedule:
    """Everything the compiled Program does in one window, as repeated shots.

    ``offset_us`` is the service work before the first shot (tenant handover); consecutive shots are
    ``repetition_delay_us`` apart. Intervals are relative to the window start.
    """

    shot: ShotActivity
    shots: int
    repetition_delay_us: float = 0.0
    offset_us: float = 0.0

    def __post_init__(self) -> None:
        if type(self.shot) is not ShotActivity:
            raise TypeError("a ShotActivity is required")
        if type(self.shots) is not int or self.shots < 1:
            raise ValueError("shots must be a positive integer")
        for name in ("repetition_delay_us", "offset_us"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and nonnegative")

    @property
    def period_us(self) -> float:
        return self.shot.duration_us + self.repetition_delay_us

    @property
    def duration_us(self) -> float:
        return self.offset_us + self.shots * self.shot.duration_us + (self.shots - 1) * self.repetition_delay_us

    def intervals(self, kind: str | None = None) -> Iterator[ActivityInterval]:
        """Every shot's records in time order, shifted to window-relative time; ``kind`` filters."""
        for index in range(self.shots):
            shift = self.offset_us + index * self.period_us
            for item in self.shot.intervals:
                if kind is None or item.kind == kind:
                    yield ActivityInterval(item.qubit, item.kind, item.start_us + shift, item.end_us + shift,
                                           item.angle_rad, item.phase_rad)


@dataclass(frozen=True)
class ExecutionWindow:
    job_id: str
    tenant_id: str
    physical_qubits: tuple[int, ...]
    start: EventPosition
    finish: EventPosition
    schedule: DriveSchedule

    def __post_init__(self) -> None:
        for name in ("job_id", "tenant_id"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a nonempty identifier")
        qubits = tuple(self.physical_qubits)
        if (not qubits or any(type(q) is not int or q < 0 for q in qubits) or len(set(qubits)) != len(qubits)):
            raise ValueError("physical qubits must be distinct nonnegative integers")
        object.__setattr__(self, "physical_qubits", qubits)
        if type(self.start) is not EventPosition or type(self.finish) is not EventPosition:
            raise TypeError("explicit EventPosition values are required")
        if self.start.clock_domain != self.finish.clock_domain:
            raise ValueError("window clock domains differ")
        if self.finish.key <= self.start.key:
            raise ValueError("a window must finish after it starts")
        if type(self.schedule) is not DriveSchedule:
            raise TypeError("a DriveSchedule is required")
        if self.schedule.duration_us > self.span_us + _SPAN_TOLERANCE_US * max(1.0, self.span_us):
            raise ValueError("the schedule does not fit in the window")
        if any(item.qubit >= len(qubits) for item in self.schedule.shot.intervals):
            raise ValueError("the schedule uses a qubit outside the window's register")

    @property
    def span_us(self) -> float:
        return self.finish.time_us - self.start.time_us


@dataclass(frozen=True)
class UnitaryKick:
    """A small rotation of ``qubit`` at ``time_us``, leaked from another job's drive.

    ``phase_rad`` is the hardware carrier phase of the rotation axis (the target's own virtual-Z frame
    is not applied here). ``time_us`` is the start of the leaking pulse inside the target window.
    """

    qubit: int
    time_us: float
    angle_rad: float
    phase_rad: float
    source_job_id: str
    source_qubit: int


def _check_windows(*windows: ExecutionWindow) -> None:
    if any(type(w) is not ExecutionWindow for w in windows):
        raise TypeError("ExecutionWindow values are required")
    if len({w.start.clock_domain for w in windows}) > 1:
        raise ValueError("window clock domains differ")


def overlap_span(first: ExecutionWindow, second: ExecutionWindow) -> tuple[float, float] | None:
    """The shared half-open time ``[start, end)`` of two windows, or None if they only touch or are apart."""
    _check_windows(first, second)
    start = max(first.start.time_us, second.start.time_us)
    end = min(first.finish.time_us, second.finish.time_us)
    return (start, end) if start < end else None


def windows_overlap(first: ExecutionWindow, second: ExecutionWindow) -> bool:
    return overlap_span(first, second) is not None


def shares_qubits(first: ExecutionWindow, second: ExecutionWindow) -> bool:
    return not set(first.physical_qubits).isdisjoint(second.physical_qubits)


def overlap_groups(windows: Sequence[ExecutionWindow]) -> tuple[tuple[int, ...], ...]:
    """Indices of ``windows`` grouped into chains of time-overlapping windows, in start order.

    Overlap is transitive through a chain: A overlaps B and B overlaps C puts all three in one group
    even if A and C never meet, because B's kicks depend on both. A window that overlaps nothing is a
    group of one.
    """
    _check_windows(*windows)
    order = sorted(range(len(windows)), key=lambda i: (windows[i].start.key, i))
    groups: list[list[int]] = []
    reach = -math.inf
    for index in order:
        window = windows[index]
        if groups and window.start.time_us < reach:
            groups[-1].append(index)
            reach = max(reach, window.finish.time_us)
        else:
            groups.append([index])
            reach = window.finish.time_us
    return tuple(tuple(group) for group in groups)


def drive_crosstalk_kicks(source: ExecutionWindow, target: ExecutionWindow,
                          transfers: Sequence[DriveCrosstalkTransfer]) -> tuple[UnitaryKick, ...]:
    """Kicks that ``source``'s drives put on ``target`` through the declared transfers, in time order.

    For a transfer ``c`` from physical qubit ``s`` to ``t``, a drive of ``s`` with angle ``theta`` and
    carrier phase ``phi`` that runs inside the target window kicks ``t`` by ``|c| * theta`` about the
    axis at phase ``arg(c) + phi``. A drive cut by the window edge counts for the part inside, so the
    angle is scaled by that fraction and the kick sits at the start of the part. Virtual Z has no
    drive and leaks nothing. Pairs without a declared transfer, zero coefficients, and windows that do
    not overlap give no kicks. One direction only: call again with the windows swapped for the other.
    """
    _check_windows(source, target)
    if any(type(item) is not DriveCrosstalkTransfer for item in transfers):
        raise TypeError("transfers must contain DriveCrosstalkTransfer values")
    if shares_qubits(source, target):
        raise ValueError("crosstalk is defined between disjoint regions")
    if overlap_span(source, target) is None:
        return ()
    index_of = {physical: index for index, physical in enumerate(source.physical_qubits)}
    by_source: dict[int, list[tuple[int, complex]]] = {}
    for transfer in transfers:
        if (transfer.coefficient != 0.0 and transfer.source in index_of
                and transfer.target in target.physical_qubits):
            by_source.setdefault(index_of[transfer.source], []).append((transfer.target, transfer.coefficient))
    kicks: list[UnitaryKick] = []
    for drive in source.schedule.intervals("drive"):
        for target_qubit, coefficient in by_source.get(drive.qubit, ()):
            begin = max(source.start.time_us + drive.start_us, target.start.time_us)
            end = min(source.start.time_us + drive.end_us, target.finish.time_us)
            if drive.end_us <= drive.start_us or end <= begin:
                continue
            fraction = (end - begin) / (drive.end_us - drive.start_us)
            kicks.append(UnitaryKick(
                target_qubit, begin, abs(coefficient) * drive.angle_rad * fraction,
                cmath.phase(coefficient) + drive.phase_rad, source.job_id,
                source.physical_qubits[drive.qubit]))
    kicks.sort(key=lambda k: (k.time_us, k.qubit, k.source_job_id, k.source_qubit))
    return tuple(kicks)


def kicks_into(target: ExecutionWindow, sources: Sequence[ExecutionWindow],
               transfers: Sequence[DriveCrosstalkTransfer]) -> tuple[UnitaryKick, ...]:
    """All kicks on ``target`` from the other windows, merged in time order.

    A window in ``sources`` that is ``target`` itself is ignored. Sources that share qubits with the
    target are an error, as in ``drive_crosstalk_kicks``.
    """
    merged = [kick for source in sources if source is not target
              for kick in drive_crosstalk_kicks(source, target, transfers)]
    merged.sort(key=lambda k: (k.time_us, k.qubit, k.source_job_id, k.source_qubit))
    return tuple(merged)

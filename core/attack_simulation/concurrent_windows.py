"""Execution windows, their overlap, and the drive leaks one window puts on another.

Stage 1 of the concurrent-sharing design: pure functions only. Nothing here schedules jobs, reads or
evolves QPU state, or decides how a leak is applied. The leaks are computed from declared activity
records alone (never from results), so every window's leaks are fixed once the windows are fixed.

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

import numpy as np

from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.pulse_shape import PulseShape
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
                                           item.angle_rad, item.phase_rad, item.shape)


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
class DriveLeak:
    """The part of one source drive that falls on ``qubit`` during ``[start_us, end_us)``.

    The source drive runs over ``[drive_start_us, drive_end_us)`` with rotation ``drive_angle_rad``, hardware
    carrier phase ``drive_phase_rad`` and envelope ``shape``. Through the declared transfer
    ``coefficient`` (and ``detuning_rad_per_us``) it turns the receiver at the complex rate
    ``rate(t) = coefficient * Omega(t) * exp(i (phase + detuning * t))``: its modulus is the leak's rotation
    rate in rad/us and its argument the hardware phase of the rotation axis at time ``t``.
    """

    qubit: int
    start_us: float
    end_us: float
    source_job_id: str
    source_qubit: int
    coefficient: complex
    detuning_rad_per_us: float
    drive_start_us: float
    drive_end_us: float
    drive_angle_rad: float
    drive_phase_rad: float
    shape: PulseShape

    def rate(self, time_us: np.ndarray) -> np.ndarray:
        time_us = np.asarray(time_us, dtype=float)
        duration = self.drive_end_us - self.drive_start_us
        omega = self.drive_angle_rad / duration * self.shape.density((time_us - self.drive_start_us) / duration)
        return (self.coefficient * omega
                * np.exp(1j * (self.drive_phase_rad + self.detuning_rad_per_us * time_us)))


    def net_rotation(self, begin_us: float, end_us: float) -> complex:
        """A resonant leak's net rotation over ``[begin_us, end_us)`` as one complex number (detuning must be 0).

        Its modulus is the rotation angle and its argument the hardware phase of the axis: the coefficient and
        the drive's phase times the angle and the fraction of the drive's area that lies in the interval.
        """
        if self.detuning_rad_per_us != 0.0:
            raise ValueError("a closed form needs a resonant leak")
        duration = self.drive_end_us - self.drive_start_us
        low, high = self.shape.cumulative(
            np.array([(begin_us - self.drive_start_us) / duration, (end_us - self.drive_start_us) / duration]))
        return complex(self.coefficient * cmath.exp(1j * self.drive_phase_rad) * self.drive_angle_rad * (high - low))


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
    even if A and C never meet, because B's leaks depend on both. A window that overlaps nothing is a
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


def _leaks_onto(source: ExecutionWindow, receivers: Sequence[int], begin_us: float, end_us: float,
                transfers: Sequence[DriveCrosstalkTransfer]) -> tuple[DriveLeak, ...]:
    """Leaks of ``source``'s drives onto the receiving physical qubits during ``[begin_us, end_us)``."""
    if any(type(item) is not DriveCrosstalkTransfer for item in transfers):
        raise TypeError("transfers must contain DriveCrosstalkTransfer values")
    if not set(source.physical_qubits).isdisjoint(receivers):
        raise ValueError("crosstalk is defined between disjoint regions")
    if end_us <= begin_us:
        return ()
    index_of = {physical: index for index, physical in enumerate(source.physical_qubits)}
    by_source: dict[int, list[DriveCrosstalkTransfer]] = {}
    for transfer in transfers:
        if transfer.coefficient != 0.0 and transfer.source in index_of and transfer.target in receivers:
            by_source.setdefault(index_of[transfer.source], []).append(transfer)
    leaks: list[DriveLeak] = []
    for drive in source.schedule.intervals("drive"):
        drive_start, drive_end = source.start.time_us + drive.start_us, source.start.time_us + drive.end_us
        begin, end = max(drive_start, begin_us), min(drive_end, end_us)
        if drive.end_us <= drive.start_us or end <= begin:
            continue
        for transfer in by_source.get(drive.qubit, ()):
            leaks.append(DriveLeak(
                transfer.target, begin, end, source.job_id, source.physical_qubits[drive.qubit],
                transfer.coefficient, transfer.detuning_rad_per_us, drive_start, drive_end,
                drive.angle_rad, drive.phase_rad, drive.shape))
    leaks.sort(key=_leak_order)
    return tuple(leaks)


def _leak_order(leak: DriveLeak) -> tuple[float, int, str, int]:
    return leak.start_us, leak.qubit, leak.source_job_id, leak.source_qubit


def drive_crosstalk_leaks(source: ExecutionWindow, target: ExecutionWindow,
                          transfers: Sequence[DriveCrosstalkTransfer]) -> tuple[DriveLeak, ...]:
    """Leaks of ``source``'s drives onto ``target`` through the declared transfers, in time order.

    A drive cut by the target window's edge leaks only for the part inside. Virtual Z has no drive and
    leaks nothing. Pairs without a declared transfer, zero coefficients, and windows that do not overlap
    give none. One direction only: call again with the windows swapped for the other.
    """
    _check_windows(source, target)
    if shares_qubits(source, target):
        raise ValueError("crosstalk is defined between disjoint regions")
    span = overlap_span(source, target)
    if span is None:
        _leaks_onto(source, (), 0.0, 0.0, transfers)       # still validates the transfers
        return ()
    return _leaks_onto(source, target.physical_qubits, span[0], span[1], transfers)


def leaks_into(target: ExecutionWindow, sources: Sequence[ExecutionWindow],
               transfers: Sequence[DriveCrosstalkTransfer]) -> tuple[DriveLeak, ...]:
    """All leaks onto ``target`` from the other windows, merged in time order.

    A window in ``sources`` that is ``target`` itself is ignored. Sources that share qubits with the
    target are an error, as in ``drive_crosstalk_leaks``.
    """
    merged = [leak for source in sources if source is not target
              for leak in drive_crosstalk_leaks(source, target, transfers)]
    merged.sort(key=_leak_order)
    return tuple(merged)


def gap_before(window: ExecutionWindow, previous: ExecutionWindow | None) -> tuple[float, float]:
    """The gap that precedes ``window`` on its qubits, as ``(start, end)`` for ``gap_leaks``.

    It runs from the finish of the previous job on the same region to the start of ``window``; with no
    previous job it runs from time 0 of the world clock.
    """
    _check_windows(window)
    if previous is None:
        return 0.0, window.start.time_us
    _check_windows(window, previous)
    if previous.finish.time_us > window.start.time_us:
        raise ValueError("the previous job must finish before this one starts")
    return previous.finish.time_us, window.start.time_us


def gap_leaks(sources: Sequence[ExecutionWindow], qubits: Sequence[int], gap_start_us: float,
              gap_end_us: float, transfers: Sequence[DriveCrosstalkTransfer]) -> tuple[DriveLeak, ...]:
    """Leaks onto ``qubits`` while no job of theirs runs, during ``[gap_start_us, gap_end_us)``.

    The qubits keep their state between two jobs and the other windows' drives still leak onto them.
    ``place_kicks`` puts leaks that start before the next job's window ahead of its first shot, where
    they act on the state that job inherits.
    """
    _check_windows(*sources)
    if any(type(q) is not int or q < 0 for q in qubits):
        raise ValueError("qubits must be physical IDs")
    merged = [leak for source in sources
              for leak in _leaks_onto(source, tuple(qubits), gap_start_us, gap_end_us, transfers)]
    merged.sort(key=_leak_order)
    return tuple(merged)

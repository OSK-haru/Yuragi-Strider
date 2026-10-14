"""Select scheduled drive-transfer relations for one physical execution window.

This module is a small planning step. It does not schedule jobs, evolve QPU
state, or decide which quantum correlations must be retained.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Sequence

from core.experimental_isolation_physics.coherent_crosstalk import (
    DriveCrosstalkTransfer,
)


@dataclass(frozen=True)
class TimeWindow:
    """A nonempty half-open device-time interval [start_us, end_us)."""

    start_us: float
    end_us: float

    def __post_init__(self) -> None:
        start = float(self.start_us)
        end = float(self.end_us)
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("time-window endpoints must be finite")
        if start < 0.0 or end <= start:
            raise ValueError("time window must have 0 <= start_us < end_us")
        object.__setattr__(self, "start_us", start)
        object.__setattr__(self, "end_us", end)


@dataclass(frozen=True)
class ScheduledDriveInterval:
    """The time interval during which one source control line is driven."""

    source_qubit_id: int
    window: TimeWindow

    def __post_init__(self) -> None:
        source = int(self.source_qubit_id)
        if source < 0 or source != self.source_qubit_id:
            raise ValueError("source_qubit_id must be a non-negative integer")
        if not isinstance(self.window, TimeWindow):
            raise TypeError("window must be a TimeWindow")
        object.__setattr__(self, "source_qubit_id", source)


def select_active_drive_transfers(
    execution_window: TimeWindow,
    drives: Sequence[ScheduledDriveInterval],
    transfers: Sequence[DriveCrosstalkTransfer],
) -> tuple[DriveCrosstalkTransfer, ...]:
    """Return transfers whose source is actually driven in this device window.

    Transfer coefficients are static in the current internal physics model.
    The returned tuple must preserve the order of ``transfers`` and contain
    each relation at most once, even if its source has multiple drive intervals.
    A zero coefficient has no physical effect and is omitted. Merely queueing
    two jobs next to each other does not make their drive windows overlap.

    The small selection rule below is intentionally left for Huew to implement.
    See ``tests/test_active_drive_relations.py`` for the required cases.
    """

    if not isinstance(execution_window, TimeWindow):
        raise TypeError("execution_window must be a TimeWindow")
    if any(not isinstance(drive, ScheduledDriveInterval) for drive in drives):
        raise TypeError("drives must contain ScheduledDriveInterval values")
    if any(not isinstance(item, DriveCrosstalkTransfer) for item in transfers):
        raise TypeError("transfers must contain DriveCrosstalkTransfer values")

    # TODO(Huew): Select transfers with a nonzero coefficient for which at
    # least one matching source drive overlaps execution_window. Time windows
    # are half-open: [0, 1) and [1, 2) do not overlap. Keep declaration order.

    results = []

    for transfer in transfers:
        if transfer.coefficient == 0.0:
            continue
        for drive in drives:
            if drive.source_qubit_id == transfer.source:
                # Check for overlap of time windows
                if (
                    drive.window.start_us < execution_window.end_us
                    and execution_window.start_us < drive.window.end_us
                ):
                    results.append(transfer)
                    break  # No need to check other drives for this transfer

    return tuple(results)

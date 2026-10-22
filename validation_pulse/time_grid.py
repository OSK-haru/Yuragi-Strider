"""Sample-time grids shared by the pulse validation modules and scripts."""

from __future__ import annotations


def uniform_times(duration_us: float, count: int) -> tuple[float, ...]:
    """``count`` evenly spaced times from 0 to ``duration_us``, both included."""
    if count < 2:
        raise ValueError("uniform_times needs at least two samples")
    return tuple(duration_us * index / (count - 1) for index in range(count))

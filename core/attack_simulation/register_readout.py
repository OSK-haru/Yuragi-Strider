"""Readout-only adapter for inherited registers; never evaluates a Reset.

The registry's Reset protocol does not promise that a zero-count request is
state preserving. Use the exact existing phenomenological model's declared
assignment calibration, or an explicit readout-only method on another model.
Unknown models fail closed rather than acquiring a noiseless channel.
"""

from __future__ import annotations

import math

from core.experimental_isolation_physics.reset_model import (
    PhenomenologicalResetModel, ResetModel,
)


def assigned_one_probability(channel: tuple[float, ...], level: int) -> float:
    """P(read 1) for a subsystem truly in ``level``, from its ``(p10, p01, p02, ...)`` channel (D91)."""
    if level == 0:
        return channel[0]
    if level >= len(channel):
        raise ValueError(f"the readout has no declared assignment for level {level}")
    return 1.0 - channel[level]


def assign_bit(channel: tuple[float, ...], level: int, draw: float) -> int:
    """The bit a readout assigns to a subsystem truly in ``level``, for a uniform ``draw`` in [0, 1).

    Level 0 reads 1 when ``draw < p10``; level ``k >= 1`` reads 0 when ``draw < p0k`` (the same comparisons
    as the two-level readout always made, so its results do not move).
    """
    if level == 0:
        return int(draw < channel[0])
    if level >= len(channel):
        raise ValueError(f"the readout has no declared assignment for level {level}")
    return int(draw >= channel[level])


def register_assignment_errors(
    model: ResetModel, *, subsystem_count: int, targets: tuple[int, ...],
) -> tuple[tuple[float, ...], ...]:
    """Return independent (p10, p01, p02, ...) channels in physical target order.

    Optional model method: register_assignment_errors(*, subsystem_count,
    targets). It must be a readout-only query, with no physical state action.
    Entries past ``p01`` are P(read 0 | true k) for the higher levels a model declares.
    """
    if type(subsystem_count) is not int or not 1 <= subsystem_count <= 10:
        raise ValueError("unsupported readout register width")
    if (not targets or len(set(targets)) != len(targets)
            or any(type(q) is not int or not 0 <= q < subsystem_count for q in targets)):
        raise ValueError("invalid readout targets")
    if type(model) is PhenomenologicalResetModel:
        errors = tuple((model.observed_one_given_zero, model.observed_zero_given_one,
                        *model.observed_zero_given_higher) for _ in targets)
    else:
        method = getattr(model, "register_assignment_errors", None)
        if not callable(method):
            raise ValueError("selected model does not support readout without Reset")
        errors = method(subsystem_count=subsystem_count, targets=targets)
    if not isinstance(errors, (tuple, list)) or len(errors) != len(targets):
        raise ValueError("invalid readout assignment channels")
    validated = []
    for channel in errors:
        if not isinstance(channel, (tuple, list)) or not 2 <= len(channel) <= 4:
            raise ValueError("readout requires one (p10, p01, ...) channel per target")
        pair = tuple(float(value) for value in channel)
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in pair):
            raise ValueError("readout probabilities must be finite and between zero and one")
        validated.append(pair)
    return tuple(validated)

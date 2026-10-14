"""Observation-stage readout mapping shared by the pulse API services."""

from __future__ import annotations

from collections.abc import Mapping

from api.pulse_models import PulseReadoutErrorRequest
from core.gates import apply_local_readout_error


def pulse_readout_response(
    readout_error: PulseReadoutErrorRequest | None,
    probabilities: Mapping[str, float],
    subsystem_count: int,
    local_levels: int,
) -> dict[str, object]:
    """Build true and observed final probabilities without mutating the state."""

    assignment_errors = (
        [(0.0, 0.0)] * subsystem_count
        if readout_error is None
        else readout_error.assignment_errors(subsystem_count)
    )
    true_probabilities = {
        label: float(value) for label, value in sorted(probabilities.items())
    }
    observed_probabilities = apply_local_readout_error(
        true_probabilities,
        subsystem_count,
        local_levels,
        assignment_errors,
    )
    return {
        "enabled": any(p10 > 0.0 or p01 > 0.0 for p10, p01 in assignment_errors),
        "model_id": "independent_affine_binary_readout_v1",
        "assignment_errors": [
            {"p10": p10, "p01": p01} for p10, p01 in assignment_errors
        ],
        "true_probabilities": true_probabilities,
        "observed_probabilities": observed_probabilities,
        "preserves_state_metrics": True,
        "noncomputational_level_policy": "preserve",
    }

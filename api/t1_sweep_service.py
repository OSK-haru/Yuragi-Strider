"""Dedicated one-qubit T1 sweep built on the production gate-aware solver."""

from __future__ import annotations

from typing import Any
from core.circuit_model import CircuitConfig
from core.gates import apply_readout_error
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from core.state_snapshots import SnapshotOptions
from core.t1_analysis import fit_exponential_with_floor


def run_t1_sweep(
    *,
    parameters: Any,
    maximum_delay_us: float,
    point_count: int,
    simulation_backend: str,
    evolution_method: str,
    readout_error: Any | None,
) -> dict[str, object]:
    environment = EnvironmentConfig(
        input_mode="physical",
        device_quality=_required(parameters.device_quality, "device_quality"),
        temperature_mk=_required(parameters.temperature_mk, "temperature_mk"),
        flux_noise_phi0=_required(parameters.flux_noise_phi0, "flux_noise_phi0"),
        qubit_frequency_ghz=_required(parameters.qubit_frequency_ghz, "qubit_frequency_ghz"),
        t1_max_us=_required(parameters.t1_max_us, "t1_max_us"),
        tphi_max_us=_required(parameters.tphi_max_us, "tphi_max_us"),
        ideal_reference=False,
    )
    delays = [
        maximum_delay_us * index / (point_count - 1)
        for index in range(point_count)
    ]
    config = SimulationConfig(
        circuit=CircuitConfig(logical_qubits=1, initial_states=["1"], columns=[]),
        environment=environment,
        duration_us=maximum_delay_us,
        time_steps=max(101, point_count),
        fidelity_threshold=0.0,
        simulation_backend=simulation_backend,
        evolution_method=evolution_method,
        snapshot_options=SnapshotOptions(
            enabled=True,
            uniform_count=0,
            custom_times_us=tuple(delays[1:-1]),
            include_initial=True,
            include_final=True,
            include_column_boundaries=False,
            include_after_circuit=False,
        ),
    )
    result = run_simulation(config)
    states_by_time = {
        round(snapshot.time_us, 12): snapshot.density_matrix
        for snapshot in result.state_snapshots
    }
    assignment_errors = _assignment_errors(readout_error)
    true_values: list[float] = []
    observed_values: list[float] = []
    for delay in delays:
        state = states_by_time.get(round(delay, 12))
        if state is None:
            raise RuntimeError(f"T1 sweep snapshot missing at {delay} us")
        true_p1 = min(1.0, max(0.0, float(state[1][1].real)))
        observed = apply_readout_error(
            {"0": 1.0 - true_p1, "1": true_p1},
            1,
            assignment_errors,
        )["1"]
        true_values.append(true_p1)
        observed_values.append(observed)

    fit = fit_exponential_with_floor(delays, observed_values)
    points = [
        {
            "delay_us": delay,
            "true_p1": true_p1,
            "observed_p1": observed_p1,
            "fitted_observed_p1": fitted,
            "residual": residual,
        }
        for delay, true_p1, observed_p1, fitted, residual in zip(
            delays,
            true_values,
            observed_values,
            fit.fitted,
            fit.residuals,
        )
    ]
    return {
        "schema_version": "t1-sweep-v1",
        "model": "single_exponential_with_observation_floor",
        "points": points,
        "fit": {
            "amplitude": fit.amplitude,
            "observation_floor": fit.floor,
            "t1_us": fit.t1_us,
            "root_mean_square_error": fit.root_mean_square_error,
            "maximum_absolute_error": fit.maximum_absolute_error,
            "r_squared": fit.r_squared,
        },
        "reference": {
            "t1_effective_us": result.derived_parameters.get("t1_effective_us"),
            "gamma_population_relaxation_per_us": result.derived_parameters.get(
                "gamma_population_relaxation_per_us"
            ),
            "temperature_mk": environment.temperature_mk,
        },
        "readout": {
            "enabled": any(p10 > 0.0 or p01 > 0.0 for p10, p01 in assignment_errors),
            "p10": assignment_errors[0][0],
            "p01": assignment_errors[0][1],
        },
        "diagnostics": {
            "simulation_backend": result.diagnostics.get("simulation_backend"),
            "evolution_method": evolution_method,
            "point_count": point_count,
            "maximum_delay_us": maximum_delay_us,
        },
    }


def _assignment_errors(
    readout_error: Any | None,
) -> list[tuple[float, float]]:
    if readout_error is None:
        return [(0.0, 0.0)]
    if len(readout_error.per_qubit) > 1:
        raise ValueError("T1 sweep readout_error.per_qubit accepts exactly one entry")
    selected = readout_error.per_qubit[0] if readout_error.per_qubit else readout_error
    if selected.p10 + selected.p01 >= 1.0:
        raise ValueError("readout assignment span must be positive")
    return [(selected.p10, selected.p01)]


def _required(value: float | None, field_name: str) -> float:
    if value is None:
        raise ValueError(f"{field_name} is required for a T1 sweep")
    return float(value)

"""Bounded API service for the validated experimental qutrit pulse path."""

from __future__ import annotations

import math
from copy import deepcopy
from time import perf_counter

from api.pulse_models import (
    QutritPulseSimulateRequest,
    QutritPulseSimulateResponse,
)
from api.pulse_backend_logging import log_pulse_backend_selection
from api.pulse_readout import pulse_readout_response
from core.capabilities import DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL
from core.cptp_evolution import EXPLICIT_CPTP_EVOLUTION_ID
from core.gates import Matrix, matmul, trace
from core.pulse_contract import (
    PULSE_ANGULAR_FREQUENCY_UNIT,
    PULSE_APPROXIMATION,
    PULSE_FRAME,
    PULSE_RATE_UNIT,
    PULSE_TIME_UNIT,
)
from core.pulse_envelopes import (
    GaussianPulseEnvelope,
    PulseEnvelope,
    SquarePulseEnvelope,
)
from core.pulse_evolution import (
    PhysicalityMetrics,
    TimeDependentCheckpoint,
    physicality_metrics,
    resolve_time_dependent_backend,
)
from core.pulse_qutrit import qutrit_initial_density_matrix
from core.pulse_qutrit_contract import (
    QUTRIT_BASIS_LABELS,
    transmon_anharmonicity_rad_per_us,
)
from core.pulse_qutrit_open_system import (
    QutritDissipationRates,
    evolve_open_qutrit_sequence,
    evolve_cptp_open_qutrit_sequence,
    qutrit_dissipation_rates,
)
from core.pulse_step_policy import (
    PULSE_QUTRIT_MAX_INTERNAL_STEPS,
    recommended_qutrit_step_policy,
)
from core.results import EnvironmentConfig
from core.quasi_static_noise import gaussian_quasi_static_detuning_samples


QUTRIT_API_CONTRACT_VERSION = "pulse-extension-b-v1"
QUTRIT_API_MAX_INTERNAL_STEPS = PULSE_QUTRIT_MAX_INTERNAL_STEPS
QUTRIT_MODEL_DESCRIPTION = (
    "single-transmon three-level rotating-frame RWA experimental model"
)
QUTRIT_MODEL_WARNING = (
    "Experimental educational qutrit model; this is not calibrated "
    "hardware pulse reproduction."
)
QUTRIT_MODEL_LIMITATIONS = (
    "Single qutrit only; multi-qubit and entangling pulses are omitted.",
    "Three-level truncation only; higher transmon levels are omitted.",
    "Rotating-frame RWA only; laboratory-frame carrier dynamics are omitted.",
    "Quasi-static Gaussian detuning is supported; other non-Markovian noise is omitted.",
    "Fixed-step RK4 is not a strict finite-step CPTP integrator.",
    "No transfer-function distortion, crosstalk, or hardware calibration.",
)


def run_qutrit_pulse_request(
    request: QutritPulseSimulateRequest,
) -> dict[str, object]:
    noise = request.quasi_static_noise
    if not noise.enabled:
        return _run_single_qutrit_pulse_request(request)

    started = perf_counter()
    samples = gaussian_quasi_static_detuning_samples(
        noise.sigma_detuning_rad_per_us,
        noise.quadrature_order,
    )
    # Run the largest total detuning first. The step policy tightens
    # monotonically with Hamiltonian scale, so this gives a conservative
    # preflight work estimate before the remaining ensemble members run.
    samples = tuple(sorted(
        samples,
        key=lambda item: abs(request.pulse.detuning_rad_per_us + item[0]),
        reverse=True,
    ))
    responses: list[dict[str, object]] = []
    for index, (offset, _) in enumerate(samples):
        sample_request = request.model_copy(update={
            "pulse": request.pulse.model_copy(update={
                "detuning_rad_per_us": (
                    request.pulse.detuning_rad_per_us + offset
                ),
            }),
            "quasi_static_noise": noise.model_copy(update={"enabled": False}),
        })
        response = _run_single_qutrit_pulse_request(sample_request)
        responses.append(response)
        if index == 0:
            worst_steps = int(
                response["step_policy"]["estimated_internal_step_count"]
            )
            if worst_steps * len(samples) > QUTRIT_API_MAX_INTERNAL_STEPS:
                from api.pulse_service import PulseExecutionLimitError

                raise PulseExecutionLimitError(
                    "Quasi-static ensemble exceeds the internal-step limit: "
                    f"conservative estimate {worst_steps * len(samples)} "
                    f"steps, maximum {QUTRIT_API_MAX_INTERNAL_STEPS}."
                )

    return _ensemble_average_response(
        request,
        responses,
        samples,
        (perf_counter() - started) * 1000.0,
    )


def _run_single_qutrit_pulse_request(
    request: QutritPulseSimulateRequest,
) -> dict[str, object]:
    from api.pulse_service import PulseExecutionLimitError

    started = perf_counter()
    resolved_backend = resolve_time_dependent_backend(request.backend)
    log_pulse_backend_selection(
        model_id=DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
        requested=request.backend,
        resolved=resolved_backend,
    )
    envelope = _build_envelope(request)
    rates = _build_rates(request)
    alpha = transmon_anharmonicity_rad_per_us(
        request.anharmonicity_mhz
    )
    sample_times = _sample_times(
        request.total_simulation_time_us,
        envelope.duration_us,
        request.snapshot_options.uniform_count,
        request.snapshot_options.custom_times_us,
    )
    initial_density_matrix = _request_initial_density_matrix(request)
    pulse_times, idle_times = _segment_sample_times(
        sample_times,
        envelope.duration_us,
        request.total_simulation_time_us,
    )
    policy = recommended_qutrit_step_policy(
        envelope,
        request.pulse.detuning_rad_per_us,
        alpha,
        rates,
        request.total_simulation_time_us,
        maximum_internal_step_count=QUTRIT_API_MAX_INTERNAL_STEPS,
        drag_beta_us=request.pulse.drag_beta_us,
    )
    estimated_steps = (
        _estimated_segment_steps(
            pulse_times, policy.selected_internal_step_cap_us
        )
        + _estimated_segment_steps(
            idle_times, policy.selected_internal_step_cap_us
        )
    )
    if estimated_steps > QUTRIT_API_MAX_INTERNAL_STEPS:
        raise PulseExecutionLimitError(
            "Qutrit pulse request exceeds the internal-step limit: "
            f"estimated {estimated_steps} steps, maximum "
            f"{QUTRIT_API_MAX_INTERNAL_STEPS}."
        )

    if request.evolution_method == "explicit_cptp":
        result, pulse_audit, idle_audit = (
            evolve_cptp_open_qutrit_sequence(
                initial_density_matrix,
                envelope,
                alpha,
                rates,
                request.total_simulation_time_us,
                policy.selected_internal_step_cap_us,
                phase_rad=request.pulse.phase_rad,
                detuning_rad_per_us=request.pulse.detuning_rad_per_us,
                drag_beta_us=request.pulse.drag_beta_us,
                pulse_checkpoint_times_us=pulse_times,
                idle_checkpoint_times_us=idle_times,
                backend=resolved_backend,
            )
        )
    else:
        result = evolve_open_qutrit_sequence(
            initial_density_matrix,
            envelope,
            alpha,
            rates,
            request.total_simulation_time_us,
            policy.selected_internal_step_cap_us,
            phase_rad=request.pulse.phase_rad,
            detuning_rad_per_us=request.pulse.detuning_rad_per_us,
            drag_beta_us=request.pulse.drag_beta_us,
            pulse_checkpoint_times_us=pulse_times,
            idle_checkpoint_times_us=idle_times,
            backend=resolved_backend,
        )
        pulse_audit = None
        idle_audit = None
    paired = _sequence_checkpoints(result, envelope.duration_us)
    trajectory = [
        _trajectory_point(time_us, segment, point)
        for time_us, segment, point in paired
    ]
    pulse_end = _trajectory_point(
        envelope.duration_us,
        "pulse",
        result.pulse_result.checkpoints[-1],
    )
    final = (
        pulse_end
        if result.idle_result is None
        else _trajectory_point(
            request.total_simulation_time_us,
            "idle",
            result.idle_result.checkpoints[-1],
        )
    )
    cleaned = [
        physicality_metrics(point.cleaned_state)
        for _, _, point in paired
    ]
    step_policy = policy.to_dict()
    step_policy["estimated_internal_step_count"] = estimated_steps
    step_policy["within_work_budget"] = True
    response = {
        "contract_version": QUTRIT_API_CONTRACT_VERSION,
        "model": {
            "model_id": DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
            "description": QUTRIT_MODEL_DESCRIPTION,
            "frame": PULSE_FRAME,
            "approximation": PULSE_APPROXIMATION,
            "logical_qubits": 1,
            "state_levels": 3,
            "basis_order": list(QUTRIT_BASIS_LABELS),
            "subsystem_dimensions": [3],
            "experimental": True,
            "hardware_calibrated": False,
            "internal_units": {
                "time": PULSE_TIME_UNIT,
                "angular_frequency": PULSE_ANGULAR_FREQUENCY_UNIT,
                "rate": PULSE_RATE_UNIT,
            },
        },
        "input": {
            "initial_state": request.initial_state,
            "initial_state_source": (
                "density_matrix"
                if request.initial_density_matrix is not None
                else "basis_state"
            ),
            "anharmonicity_mhz": request.anharmonicity_mhz,
            "shape": request.pulse.shape,
            "amplitude_mode": request.pulse.amplitude_mode,
            "target_rotation_angle_rad": (
                request.pulse.target_rotation_angle_rad
            ),
            "peak_amplitude_rad_per_us": (
                envelope.peak_amplitude_rad_per_us
            ),
            "pulse_area_rad": envelope.pulse_area_rad,
            "pulse_duration_us": envelope.duration_us,
            "total_simulation_time_us": request.total_simulation_time_us,
            "idle_duration_us": (
                request.total_simulation_time_us - envelope.duration_us
            ),
            "phase_rad": request.pulse.phase_rad,
            "detuning_rad_per_us": request.pulse.detuning_rad_per_us,
            "drag_beta_us": request.pulse.drag_beta_us,
            "quasi_static_noise_enabled": False,
            "quasi_static_detuning_sigma_rad_per_us": 0.0,
            "quasi_static_quadrature_order": 1,
            "sample_count": len(trajectory),
        },
        "rates": rates.to_dict(),
        "step_policy": step_policy,
        "sample_times_us": [point["time_us"] for point in trajectory],
        "trajectory": trajectory,
        "leakage": {
            "maximum_recorded_leakage_probability": (
                result.leakage.maximum_recorded_leakage_probability
            ),
            "leakage_at_pulse_end": (
                result.leakage.leakage_at_pulse_end
            ),
            "leakage_at_final_time": (
                result.leakage.leakage_at_final_time
            ),
        },
        "pulse_end": pulse_end,
        "final": final,
        "readout": pulse_readout_response(
            request.readout_error,
            {
                "0": float(final["population_0"]),
                "1": float(final["population_1"]),
                "2": float(final["population_2"]),
            },
            1,
            3,
        ),
        "diagnostics": {
            "api_runtime_ms": (perf_counter() - started) * 1000.0,
            "backend": {
                "requested": request.backend,
                "resolved": resolved_backend,
                "fallback_used": (
                    request.backend == "auto" and resolved_backend == "python"
                ),
            },
            "evolution": {
                "requested": request.evolution_method,
                "resolved": request.evolution_method,
                "method_id": (
                    EXPLICIT_CPTP_EVOLUTION_ID
                    if request.evolution_method == "explicit_cptp"
                    else "fixed_step_rk4_v1"
                ),
                "cptp_guaranteed_by_construction": (
                    request.evolution_method == "explicit_cptp"
                ),
                "cleanup_applied": (
                    request.evolution_method == "fixed_step_rk4"
                ),
                "open_pulse_audit": _audit_response(pulse_audit),
                "open_idle_audit": _audit_response(idle_audit),
                "closed_pulse_audit": None,
                "closed_idle_audit": None,
            },
            "open_pulse": result.pulse_result.diagnostics.to_dict(),
            "open_idle": (
                None
                if result.idle_result is None
                else result.idle_result.diagnostics.to_dict()
            ),
            "maximum_cleaned_trace_error": max(
                item.trace_error for item in cleaned
            ),
            "maximum_cleaned_hermiticity_error": max(
                item.hermiticity_error for item in cleaned
            ),
            "minimum_cleaned_eigenvalue": min(
                item.minimum_eigenvalue for item in cleaned
            ),
            "quasi_static_noise": {
                "enabled": False,
                "model_id": "gaussian_quasi_static_detuning_v1",
            },
        },
        "warnings": _warnings(request),
        "limitations": _limitations(request.evolution_method),
    }
    return QutritPulseSimulateResponse.model_validate(response).model_dump()


def _ensemble_average_response(
    request: QutritPulseSimulateRequest,
    responses: list[dict[str, object]],
    samples: tuple[tuple[float, float], ...],
    runtime_ms: float,
) -> dict[str, object]:
    """Average complete density-matrix trajectories over fixed detunings."""

    if not responses:
        raise ValueError("quasi-static ensemble requires at least one sample")
    weights = [weight for _, weight in samples]
    averaged = deepcopy(responses[0])
    trajectories = [response["trajectory"] for response in responses]
    averaged["trajectory"] = [
        _average_response_point(
            [trajectory[index] for trajectory in trajectories],
            weights,
        )
        for index in range(len(trajectories[0]))
    ]
    averaged["pulse_end"] = _average_response_point(
        [response["pulse_end"] for response in responses],
        weights,
    )
    averaged["final"] = _average_response_point(
        [response["final"] for response in responses],
        weights,
    )

    trajectory = averaged["trajectory"]
    pulse_end = averaged["pulse_end"]
    final = averaged["final"]
    averaged["readout"] = pulse_readout_response(
        request.readout_error,
        {
            "0": float(final["population_0"]),
            "1": float(final["population_1"]),
            "2": float(final["population_2"]),
        },
        1,
        3,
    )
    averaged["leakage"] = {
        "maximum_recorded_leakage_probability": max(
            point["leakage_probability"] for point in trajectory
        ),
        "leakage_at_pulse_end": pulse_end["leakage_probability"],
        "leakage_at_final_time": final["leakage_probability"],
    }

    input_summary = averaged["input"]
    input_summary.update({
        "detuning_rad_per_us": request.pulse.detuning_rad_per_us,
        "quasi_static_noise_enabled": True,
        "quasi_static_detuning_sigma_rad_per_us": (
            request.quasi_static_noise.sigma_detuning_rad_per_us
        ),
        "quasi_static_quadrature_order": (
            request.quasi_static_noise.quadrature_order
        ),
    })

    step_policy = averaged["step_policy"]
    step_policy["estimated_internal_step_count"] = sum(
        int(response["step_policy"]["estimated_internal_step_count"])
        for response in responses
    )
    step_policy["within_work_budget"] = (
        step_policy["estimated_internal_step_count"]
        <= QUTRIT_API_MAX_INTERNAL_STEPS
    )

    diagnostics = averaged["diagnostics"]
    diagnostics["api_runtime_ms"] = runtime_ms
    diagnostics["open_pulse"] = _aggregate_evolution_diagnostics(
        [response["diagnostics"]["open_pulse"] for response in responses]
    )
    open_idle_items = [
        response["diagnostics"]["open_idle"] for response in responses
        if response["diagnostics"]["open_idle"] is not None
    ]
    diagnostics["open_idle"] = (
        _aggregate_evolution_diagnostics(open_idle_items)
        if open_idle_items else None
    )
    evolution = diagnostics["evolution"]
    evolution["open_pulse_audit"] = _aggregate_cptp_audits([
        response["diagnostics"]["evolution"]["open_pulse_audit"]
        for response in responses
    ])
    evolution["open_idle_audit"] = _aggregate_cptp_audits([
        response["diagnostics"]["evolution"]["open_idle_audit"]
        for response in responses
    ])
    cleaned_metrics = [
        point["cleaned_physicality"] for point in trajectory
    ]
    diagnostics["maximum_cleaned_trace_error"] = max(
        item["trace_error"] for item in cleaned_metrics
    )
    diagnostics["maximum_cleaned_hermiticity_error"] = max(
        item["hermiticity_error"] for item in cleaned_metrics
    )
    diagnostics["minimum_cleaned_eigenvalue"] = min(
        item["minimum_eigenvalue"] for item in cleaned_metrics
    )
    diagnostics["quasi_static_noise"] = {
        "enabled": True,
        "model_id": "gaussian_quasi_static_detuning_v1",
        "distribution": "delta_omega ~ Normal(0, sigma_omega^2)",
        "ensemble_equation": "rho_bar(t) = E_delta_omega[rho(t; delta_omega)]",
        "quadrature_equation": (
            "rho_bar(t) ~= sum_i (w_i/sqrt(pi)) "
            "rho(t; sqrt(2)*sigma_omega*x_i)"
        ),
        "quadrature_method": "Gauss-Hermite",
        "quadrature_reason": (
            "The Gaussian expectation transforms to an exp(-x^2)-weighted "
            "integral, matching Gauss-Hermite quadrature and avoiding "
            "Monte Carlo shot noise."
        ),
        "sigma_detuning_rad_per_us": (
            request.quasi_static_noise.sigma_detuning_rad_per_us
        ),
        "quadrature_order": request.quasi_static_noise.quadrature_order,
        "samples": [
            {"detuning_offset_rad_per_us": offset, "weight": weight}
            for offset, weight in samples
        ],
        "constant_within_each_shot": True,
        "independent_between_shots": True,
    }
    averaged["warnings"] = [
        *averaged["warnings"],
        "Quasi-static Gaussian detuning is an ensemble model, not a "
        "time-resolved stochastic noise trace.",
    ]
    return QutritPulseSimulateResponse.model_validate(averaged).model_dump()


def _average_response_point(
    points: list[dict[str, object]],
    weights: list[float],
) -> dict[str, object]:
    averaged = deepcopy(points[0])
    state = _weighted_density_matrix(
        [point["density_matrix"] for point in points],
        weights,
    )
    metrics = physicality_metrics(state)
    populations = [float(state[index][index].real) for index in range(3)]
    averaged.update({
        "population_0": populations[0],
        "population_1": populations[1],
        "population_2": populations[2],
        "computational_population": populations[0] + populations[1],
        "leakage_probability": populations[2],
        "population_sum_error": abs(sum(populations) - 1.0),
        "purity": float(trace(matmul(state, state)).real),
        "density_matrix": _matrix_response(state),
        "raw_physicality": {
            "trace_error": max(
                point["raw_physicality"]["trace_error"] for point in points
            ),
            "hermiticity_error": max(
                point["raw_physicality"]["hermiticity_error"] for point in points
            ),
            "minimum_eigenvalue": min(
                point["raw_physicality"]["minimum_eigenvalue"] for point in points
            ),
        },
        "cleaned_physicality": _physicality_response(metrics),
        "cleanup_correction_norm": sum(
            weight * float(point["cleanup_correction_norm"])
            for point, weight in zip(points, weights, strict=True)
        ),
    })
    return averaged


def _weighted_density_matrix(
    matrices: list[list[list[dict[str, float]]]],
    weights: list[float],
) -> Matrix:
    return tuple(
        tuple(
            sum(
                weight * complex(
                    matrix[row][column]["real"],
                    matrix[row][column]["imag"],
                )
                for matrix, weight in zip(matrices, weights, strict=True)
            )
            for column in range(3)
        )
        for row in range(3)
    )


def _aggregate_evolution_diagnostics(
    items: list[dict[str, object]],
) -> dict[str, object]:
    first = deepcopy(items[0])
    for key in (
        "internal_step_count",
        "rhs_evaluation_count",
        "hamiltonian_evaluation_count",
    ):
        first[key] = sum(int(item[key]) for item in items)
    first["minimum_internal_step_us"] = min(
        float(item["minimum_internal_step_us"]) for item in items
    )
    first["maximum_internal_step_us"] = max(
        float(item["maximum_internal_step_us"]) for item in items
    )
    for key in (
        "raw_trace_error",
        "raw_hermiticity_error",
        "cleanup_correction_norm",
        "actual_duration_us",
    ):
        first[key] = max(float(item[key]) for item in items)
    first["raw_minimum_eigenvalue"] = min(
        float(item["raw_minimum_eigenvalue"]) for item in items
    )
    return first


def _aggregate_cptp_audits(items: list[object]) -> dict[str, object] | None:
    audits = [item for item in items if item is not None]
    if not audits:
        return None
    first = deepcopy(audits[0])
    first["map_count"] = sum(int(item["map_count"]) for item in audits)
    first["interval_count"] = sum(
        int(item["interval_count"]) for item in audits
    )
    first["minimum_choi_eigenvalue"] = min(
        float(item["minimum_choi_eigenvalue"]) for item in audits
    )
    first["maximum_trace_preservation_frobenius_error"] = max(
        float(item["maximum_trace_preservation_frobenius_error"])
        for item in audits
    )
    first["maximum_trace_preservation_max_abs_error"] = max(
        float(item["maximum_trace_preservation_max_abs_error"])
        for item in audits
    )
    first["all_maps_cptp"] = all(bool(item["all_maps_cptp"]) for item in audits)
    return first


def _request_initial_density_matrix(
    request: QutritPulseSimulateRequest,
) -> Matrix:
    if request.initial_density_matrix is None:
        return qutrit_initial_density_matrix(request.initial_state)
    return tuple(
        tuple(complex(value.real, value.imag) for value in row)
        for row in request.initial_density_matrix
    )


def _build_envelope(request: QutritPulseSimulateRequest) -> PulseEnvelope:
    pulse = request.pulse
    if pulse.shape == "square":
        assert pulse.pulse_duration_us is not None
        if pulse.amplitude_mode == "target_rotation_angle":
            assert pulse.target_rotation_angle_rad is not None
            return SquarePulseEnvelope.from_target_rotation_angle(
                pulse.target_rotation_angle_rad, pulse.pulse_duration_us
            )
        assert pulse.peak_amplitude_rad_per_us is not None
        return SquarePulseEnvelope(
            pulse.peak_amplitude_rad_per_us, pulse.pulse_duration_us
        )
    assert pulse.sigma_us is not None
    assert pulse.truncation_sigma is not None
    if pulse.amplitude_mode == "target_rotation_angle":
        assert pulse.target_rotation_angle_rad is not None
        return GaussianPulseEnvelope.from_target_rotation_angle(
            pulse.target_rotation_angle_rad,
            pulse.sigma_us,
            pulse.truncation_sigma,
        )
    assert pulse.peak_amplitude_rad_per_us is not None
    return GaussianPulseEnvelope(
        pulse.peak_amplitude_rad_per_us,
        pulse.sigma_us,
        pulse.truncation_sigma,
    )


def _build_rates(
    request: QutritPulseSimulateRequest,
) -> QutritDissipationRates:
    environment = request.environment
    if environment.input_mode == "direct_rates":
        return QutritDissipationRates(
            "direct_rates",
            environment.gamma_10_down_per_us,
            environment.gamma_01_up_per_us,
            environment.gamma_21_down_per_us,
            environment.gamma_12_up_per_us,
            environment.gamma_phi_adjacent_per_us,
        )
    return qutrit_dissipation_rates(
        EnvironmentConfig(
            input_mode="physical",
            device_quality=environment.device_quality,
            temperature_mk=environment.temperature_mk,
            flux_noise_phi0=environment.flux_noise_phi0,
            qubit_frequency_ghz=environment.qubit_frequency_ghz,
            t1_max_us=(
                environment.per_qubit[0].t1_max_us
                if environment.per_qubit else environment.t1_max_us
            ),
            tphi_max_us=(
                environment.per_qubit[0].tphi_max_us
                if environment.per_qubit else environment.tphi_max_us
            ),
            ideal_reference=environment.ideal_reference,
        ),
        request.anharmonicity_mhz,
    )


def _sample_times(
    total: float, pulse: float, count: int, custom: list[float]
) -> list[float]:
    values = {0.0, float(pulse), float(total), *map(float, custom)}
    if count >= 2:
        values.update(total * index / (count - 1) for index in range(count))
    return sorted(values)


def _segment_sample_times(
    times: list[float], pulse: float, total: float
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    tolerance = 1e-14
    pulse_times = tuple(
        min(value, pulse) for value in times
        if value <= pulse + tolerance
    )
    if total <= pulse:
        return pulse_times, ()
    idle_duration = total - pulse
    idle_times = tuple(
        min(idle_duration, max(0.0, value - pulse))
        for value in times
        if value >= pulse - tolerance
    )
    return pulse_times, idle_times


def _estimated_segment_steps(
    boundaries: tuple[float, ...], max_step: float
) -> int:
    total = 0
    previous = 0.0
    for boundary in boundaries:
        interval = boundary - previous
        if interval > 1e-15:
            total += math.ceil(interval / max_step)
        previous = boundary
    return total


def _sequence_checkpoints(result, pulse_duration: float):
    paired = [
        (point.time_us, "pulse", point)
        for point in result.pulse_result.checkpoints
    ]
    if result.idle_result is not None:
        paired.extend(
            (pulse_duration + point.time_us, "idle", point)
            for point in result.idle_result.checkpoints[1:]
        )
    return paired


def _trajectory_point(
    time_us: float, segment: str, point: TimeDependentCheckpoint
) -> dict[str, object]:
    state = point.cleaned_state
    populations = [float(state[index][index].real) for index in range(3)]
    cleaned = physicality_metrics(state)
    return {
        "time_us": time_us,
        "segment": segment,
        "population_0": populations[0],
        "population_1": populations[1],
        "population_2": populations[2],
        "computational_population": populations[0] + populations[1],
        "leakage_probability": populations[2],
        "population_sum_error": abs(sum(populations) - 1.0),
        "purity": float(trace(matmul(state, state)).real),
        "density_matrix": _matrix_response(state),
        "raw_physicality": _physicality_response(point.raw_physicality),
        "cleaned_physicality": _physicality_response(cleaned),
        "cleanup_correction_norm": point.cleanup_correction_norm,
    }


def _warnings(request: QutritPulseSimulateRequest) -> list[str]:
    warnings = [QUTRIT_MODEL_WARNING]
    if request.environment.input_mode == "direct_rates":
        warnings.append(
            "Direct-rate mode bypasses the educational physical-input mapping."
        )
    elif request.environment.ideal_reference:
        warnings.append(
            "ideal_reference disables environmental rates for the physical "
            "input profile."
        )
    if request.evolution_method == "explicit_cptp":
        warnings.append(
            "Explicit CPTP mode uses a midpoint piecewise-constant "
            "Hamiltonian approximation."
        )
    return warnings


def _audit_response(audit) -> dict[str, object] | None:
    return None if audit is None else audit.to_dict()


def _limitations(evolution_method: str) -> list[str]:
    limitations = [
        item
        for item in QUTRIT_MODEL_LIMITATIONS
        if "Fixed-step RK4" not in item
    ]
    if evolution_method == "fixed_step_rk4":
        limitations.append(
            "Fixed-step RK4 is not a strict finite-step CPTP integrator."
        )
    else:
        limitations.append(
            "Explicit CPTP evolution is CPTP for the frozen interval maps, "
            "while time dependence is approximated at interval midpoints."
        )
    return limitations


def _physicality_response(metrics: PhysicalityMetrics) -> dict[str, float]:
    return {
        "trace_error": metrics.trace_error,
        "hermiticity_error": metrics.hermiticity_error,
        "minimum_eigenvalue": metrics.minimum_eigenvalue,
    }


def _matrix_response(matrix: Matrix) -> list[list[dict[str, float]]]:
    return [
        [
            {"real": float(value.real), "imag": float(value.imag)}
            for value in row
        ]
        for row in matrix
    ]

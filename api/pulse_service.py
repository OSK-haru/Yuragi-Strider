"""Application service for the frozen Pulse Baseline A API contract."""

from __future__ import annotations

import math
from time import perf_counter

from api.pulse_models import (
    CoupledTransmonNetworkPulseSimulateRequest,
    PulseApiRequest,
    QutritPulseSimulateRequest,
    PulseSimulateRequest,
    PulseSimulateResponse,
)
from api.pulse_backend_logging import log_pulse_backend_selection
from api.pulse_qutrit_service import run_qutrit_pulse_request
from api.pulse_readout import pulse_readout_response
from api.pulse_transmon_network_service import run_coupled_transmon_network_request
from core.capabilities import DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL
from core.cptp_evolution import EXPLICIT_CPTP_EVOLUTION_ID
from core.gates import Matrix, initial_density_matrix, matmul, trace
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
from core.pulse_open_system import (
    DIRECT_RATES_INPUT_MODE,
    OpenPulseSequenceResult,
    PulseDissipationRates,
    evolve_open_pulse_sequence,
    evolve_cptp_open_pulse_sequence,
    pulse_dissipation_rates,
)
from core.pulse_step_policy import (
    PULSE_BASELINE_A_EPSILON_D,
    PULSE_BASELINE_A_EPSILON_H,
    PULSE_BASELINE_A_SAMPLES_PER_SIGMA,
    pulse_step_controls,
    recommended_max_step_us,
)
from core.results import EnvironmentConfig


PULSE_API_CONTRACT_VERSION = "pulse-baseline-a-v1"
PULSE_API_STEP_POLICY_ID = "pulse_baseline_a_step_policy_v1"
PULSE_API_MAX_INTERNAL_STEPS = 200_000
PULSE_MODEL_DESCRIPTION = (
    "rotating-frame RWA control-envelope experimental model"
)
PULSE_MODEL_WARNING = (
    "Experimental educational two-level model; this is not calibrated "
    "hardware pulse reproduction."
)
PULSE_MODEL_LIMITATIONS = (
    "Two-level state space only; qutrit leakage is not represented.",
    "Rotating-frame RWA only; laboratory-frame carrier dynamics are omitted.",
    "Markovian Lindblad environment only; non-Markovian noise is omitted.",
    "No DRAG, transfer-function distortion, crosstalk, or multi-qubit pulses.",
    "The fixed-step RK4 path applies density-matrix cleanup after each step.",
)


class PulseExecutionLimitError(ValueError):
    """Raised when a valid request exceeds the frozen API work budget."""


def run_pulse_api_request(request: PulseApiRequest) -> dict[str, object]:
    """Dispatch one validated pulse request without changing either contract."""

    if isinstance(request, QutritPulseSimulateRequest):
        return run_qutrit_pulse_request(request)
    if isinstance(request, CoupledTransmonNetworkPulseSimulateRequest):
        return run_coupled_transmon_network_request(request)
    return run_pulse_request(request)


def run_pulse_request(request: PulseSimulateRequest) -> dict[str, object]:
    """Execute one bounded Baseline A request and return its frozen response."""

    started = perf_counter()
    resolved_backend = resolve_time_dependent_backend(request.backend)
    log_pulse_backend_selection(
        model_id=DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
        requested=request.backend,
        resolved=resolved_backend,
    )
    envelope = _build_envelope(request)
    rates = _build_rates(request)
    sample_times = _sample_times(
        request.total_simulation_time_us,
        envelope.duration_us,
        request.snapshot_options.uniform_count,
        request.snapshot_options.custom_times_us,
    )
    pulse_times, idle_times = _segment_sample_times(
        sample_times,
        envelope.duration_us,
        request.total_simulation_time_us,
    )
    max_step_us = recommended_max_step_us(
        envelope,
        request.pulse.detuning_rad_per_us,
        rates,
    )
    estimated_steps = 2 * _estimated_sequence_steps(
        pulse_times,
        idle_times,
        max_step_us,
    )
    if estimated_steps > PULSE_API_MAX_INTERNAL_STEPS:
        raise PulseExecutionLimitError(
            "Pulse request exceeds the Baseline A internal-step limit: "
            f"estimated {estimated_steps} steps, maximum "
            f"{PULSE_API_MAX_INTERNAL_STEPS}."
        )

    initial = initial_density_matrix([request.initial_state])
    zero_rates = PulseDissipationRates(
        DIRECT_RATES_INPUT_MODE,
        0.0,
        0.0,
        0.0,
    )
    if request.evolution_method == "explicit_cptp":
        open_result, open_pulse_audit, open_idle_audit = (
            evolve_cptp_open_pulse_sequence(
                initial,
                envelope,
                rates,
                request.total_simulation_time_us,
                max_step_us,
                phase_rad=request.pulse.phase_rad,
                detuning_rad_per_us=request.pulse.detuning_rad_per_us,
                pulse_checkpoint_times_us=pulse_times,
                idle_checkpoint_times_us=idle_times,
                backend=resolved_backend,
            )
        )
        closed_result, closed_pulse_audit, closed_idle_audit = (
            evolve_cptp_open_pulse_sequence(
                initial,
                envelope,
                zero_rates,
                request.total_simulation_time_us,
                max_step_us,
                phase_rad=request.pulse.phase_rad,
                detuning_rad_per_us=request.pulse.detuning_rad_per_us,
                pulse_checkpoint_times_us=pulse_times,
                idle_checkpoint_times_us=idle_times,
                backend=resolved_backend,
            )
        )
    else:
        open_result = evolve_open_pulse_sequence(
            initial,
            envelope,
            rates,
            request.total_simulation_time_us,
            max_step_us,
            phase_rad=request.pulse.phase_rad,
            detuning_rad_per_us=request.pulse.detuning_rad_per_us,
            pulse_checkpoint_times_us=pulse_times,
            idle_checkpoint_times_us=idle_times,
            backend=resolved_backend,
        )
        closed_result = evolve_open_pulse_sequence(
            initial,
            envelope,
            zero_rates,
            request.total_simulation_time_us,
            max_step_us,
            phase_rad=request.pulse.phase_rad,
            detuning_rad_per_us=request.pulse.detuning_rad_per_us,
            pulse_checkpoint_times_us=pulse_times,
            idle_checkpoint_times_us=idle_times,
            backend=resolved_backend,
        )
        open_pulse_audit = None
        open_idle_audit = None
        closed_pulse_audit = None
        closed_idle_audit = None

    paired = _paired_checkpoints(open_result, closed_result)
    trajectory = [
        _trajectory_point(time_us, segment, open_point, closed_point)
        for time_us, segment, open_point, closed_point in paired
    ]
    pulse_end = _state_response(
        envelope.duration_us,
        "pulse",
        open_result.pulse_result.checkpoints[-1],
        closed_result.pulse_result.checkpoints[-1],
    )
    if open_result.idle_result is None:
        final = pulse_end
    else:
        assert closed_result.idle_result is not None
        final = _state_response(
            request.total_simulation_time_us,
            "idle",
            open_result.idle_result.checkpoints[-1],
            closed_result.idle_result.checkpoints[-1],
        )

    cleaned_metrics = [
        physicality_metrics(point.cleaned_state)
        for _, _, point, _ in paired
    ]
    controls = pulse_step_controls(
        envelope,
        request.pulse.detuning_rad_per_us,
        rates,
        max_step_us,
    )
    response = {
        "contract_version": PULSE_API_CONTRACT_VERSION,
        "model": {
            "model_id": DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
            "description": PULSE_MODEL_DESCRIPTION,
            "frame": PULSE_FRAME,
            "approximation": PULSE_APPROXIMATION,
            "logical_qubits": 1,
            "state_levels": 2,
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
            "total_simulation_time_us": (
                request.total_simulation_time_us
            ),
            "idle_duration_us": (
                request.total_simulation_time_us - envelope.duration_us
            ),
            "phase_rad": request.pulse.phase_rad,
            "detuning_rad_per_us": request.pulse.detuning_rad_per_us,
            "sample_count": len(sample_times),
        },
        "rates": _rates_response(rates),
        "step_policy": {
            "policy_id": PULSE_API_STEP_POLICY_ID,
            "epsilon_h": PULSE_BASELINE_A_EPSILON_H,
            "epsilon_d": PULSE_BASELINE_A_EPSILON_D,
            "samples_per_sigma": PULSE_BASELINE_A_SAMPLES_PER_SIGMA,
            **controls.to_dict(),
            "estimated_internal_steps": estimated_steps,
            "maximum_allowed_internal_steps": (
                PULSE_API_MAX_INTERNAL_STEPS
            ),
        },
        "sample_times_us": sample_times,
        "trajectory": trajectory,
        "pulse_end": pulse_end,
        "final": final,
        "readout": pulse_readout_response(
            request.readout_error,
            {
                "0": float(final["open_population_0"]),
                "1": float(final["open_population_1"]),
            },
            1,
            2,
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
                "open_pulse_audit": _audit_response(open_pulse_audit),
                "open_idle_audit": _audit_response(open_idle_audit),
                "closed_pulse_audit": _audit_response(
                    closed_pulse_audit
                ),
                "closed_idle_audit": _audit_response(closed_idle_audit),
            },
            "open_pulse": open_result.pulse_result.diagnostics.to_dict(),
            "open_idle": (
                None
                if open_result.idle_result is None
                else open_result.idle_result.diagnostics.to_dict()
            ),
            "closed_pulse": closed_result.pulse_result.diagnostics.to_dict(),
            "closed_idle": (
                None
                if closed_result.idle_result is None
                else closed_result.idle_result.diagnostics.to_dict()
            ),
            "maximum_cleaned_trace_error": max(
                metric.trace_error for metric in cleaned_metrics
            ),
            "maximum_cleaned_hermiticity_error": max(
                metric.hermiticity_error for metric in cleaned_metrics
            ),
            "minimum_cleaned_eigenvalue": min(
                metric.minimum_eigenvalue for metric in cleaned_metrics
            ),
        },
        "warnings": _warnings(request, open_result),
        "limitations": _limitations(request.evolution_method),
    }
    return PulseSimulateResponse.model_validate(response).model_dump()


def _build_envelope(request: PulseSimulateRequest) -> PulseEnvelope:
    pulse = request.pulse
    if pulse.shape == "square":
        assert pulse.pulse_duration_us is not None
        if pulse.amplitude_mode == "target_rotation_angle":
            assert pulse.target_rotation_angle_rad is not None
            return SquarePulseEnvelope.from_target_rotation_angle(
                pulse.target_rotation_angle_rad,
                pulse.pulse_duration_us,
            )
        assert pulse.peak_amplitude_rad_per_us is not None
        return SquarePulseEnvelope(
            pulse.peak_amplitude_rad_per_us,
            pulse.pulse_duration_us,
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


def _build_rates(request: PulseSimulateRequest) -> PulseDissipationRates:
    environment = request.environment
    if environment.input_mode == "direct_rates":
        return PulseDissipationRates(
            DIRECT_RATES_INPUT_MODE,
            environment.gamma_down_per_us,
            environment.gamma_up_per_us,
            environment.gamma_phi_per_us,
        )
    return pulse_dissipation_rates(EnvironmentConfig(
        input_mode="physical",
        device_quality=environment.device_quality,
        temperature_mk=environment.temperature_mk,
        flux_noise_phi0=environment.flux_noise_phi0,
        qubit_frequency_ghz=environment.qubit_frequency_ghz,
        t1_max_us=environment.t1_max_us,
        tphi_max_us=environment.tphi_max_us,
        ideal_reference=environment.ideal_reference,
    ))


def _sample_times(
    total_duration_us: float,
    pulse_duration_us: float,
    uniform_count: int,
    custom_times_us: list[float],
) -> list[float]:
    values = {
        0.0,
        float(pulse_duration_us),
        float(total_duration_us),
        *(float(value) for value in custom_times_us),
    }
    if uniform_count >= 2:
        values.update(
            total_duration_us * index / (uniform_count - 1)
            for index in range(uniform_count)
        )
    return sorted(values)


def _segment_sample_times(
    global_times: list[float],
    pulse_duration_us: float,
    total_duration_us: float,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    tolerance = 1e-14
    pulse_times = tuple(
        min(time_us, pulse_duration_us)
        for time_us in global_times
        if time_us <= pulse_duration_us + tolerance
    )
    if total_duration_us <= pulse_duration_us:
        return pulse_times, ()
    idle_duration = total_duration_us - pulse_duration_us
    idle_times = tuple(
        min(idle_duration, max(0.0, time_us - pulse_duration_us))
        for time_us in global_times
        if time_us >= pulse_duration_us - tolerance
    )
    return pulse_times, idle_times


def _estimated_sequence_steps(
    pulse_times: tuple[float, ...],
    idle_times: tuple[float, ...],
    max_step_us: float,
) -> int:
    return (
        _estimated_segment_steps(pulse_times, max_step_us)
        + _estimated_segment_steps(idle_times, max_step_us)
    )


def _estimated_segment_steps(
    boundaries: tuple[float, ...],
    max_step_us: float,
) -> int:
    total = 0
    previous = 0.0
    for boundary in boundaries:
        interval = boundary - previous
        if interval > 1e-15:
            total += math.ceil(interval / max_step_us)
        previous = boundary
    return total


def _paired_checkpoints(
    open_result: OpenPulseSequenceResult,
    closed_result: OpenPulseSequenceResult,
) -> list[
    tuple[
        float,
        str,
        TimeDependentCheckpoint,
        TimeDependentCheckpoint,
    ]
]:
    paired = [
        (
            open_point.time_us,
            "pulse",
            open_point,
            closed_point,
        )
        for open_point, closed_point in zip(
            open_result.pulse_result.checkpoints,
            closed_result.pulse_result.checkpoints,
            strict=True,
        )
    ]
    if open_result.idle_result is None:
        return paired
    assert closed_result.idle_result is not None
    paired.extend(
        (
            open_result.pulse_duration_us + open_point.time_us,
            "idle",
            open_point,
            closed_point,
        )
        for open_point, closed_point in zip(
            open_result.idle_result.checkpoints[1:],
            closed_result.idle_result.checkpoints[1:],
            strict=True,
        )
    )
    return paired


def _trajectory_point(
    time_us: float,
    segment: str,
    open_point: TimeDependentCheckpoint,
    closed_point: TimeDependentCheckpoint,
) -> dict[str, object]:
    open_state = open_point.cleaned_state
    closed_state = closed_point.cleaned_state
    return {
        "time_us": time_us,
        "segment": segment,
        "open_population_0": _probability(open_state[0][0].real),
        "open_population_1": _probability(open_state[1][1].real),
        "closed_population_0": _probability(closed_state[0][0].real),
        "closed_population_1": _probability(closed_state[1][1].real),
        "fidelity_to_closed": _state_overlap(open_state, closed_state),
        "purity": _purity(open_state),
        "raw_physicality": _physicality_response(
            open_point.raw_physicality
        ),
        "cleaned_physicality": _physicality_response(
            physicality_metrics(open_state)
        ),
        "cleanup_correction_norm": open_point.cleanup_correction_norm,
    }


def _state_response(
    time_us: float,
    segment: str,
    open_point: TimeDependentCheckpoint,
    closed_point: TimeDependentCheckpoint,
) -> dict[str, object]:
    return {
        **_trajectory_point(
            time_us,
            segment,
            open_point,
            closed_point,
        ),
        "open_density_matrix": _matrix_response(open_point.cleaned_state),
        "closed_density_matrix": _matrix_response(
            closed_point.cleaned_state
        ),
    }


def _rates_response(rates: PulseDissipationRates) -> dict[str, object]:
    population_rate = rates.gamma_population_relaxation_per_us
    transverse_rate = 0.5 * population_rate + rates.gamma_phi_per_us
    return {
        "input_mode": rates.input_mode,
        "gamma_down_per_us": rates.gamma_down_per_us,
        "gamma_up_per_us": rates.gamma_up_per_us,
        "gamma_population_relaxation_per_us": population_rate,
        "gamma_phi_per_us": rates.gamma_phi_per_us,
        "n_th": rates.n_th,
        "t1_effective_us": _inverse_or_none(population_rate),
        "tphi_effective_us": _inverse_or_none(rates.gamma_phi_per_us),
        "t2_effective_us": _inverse_or_none(transverse_rate),
    }


def _warnings(
    request: PulseSimulateRequest,
    result: OpenPulseSequenceResult,
) -> list[str]:
    warnings = [PULSE_MODEL_WARNING]
    if request.environment.input_mode == "direct_rates":
        warnings.append(
            "Direct-rate mode bypasses the educational physical-input "
            "mapping."
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
    diagnostics = [result.pulse_result.diagnostics]
    if result.idle_result is not None:
        diagnostics.append(result.idle_result.diagnostics)
    if max(item.cleanup_correction_norm for item in diagnostics) > 1e-10:
        warnings.append(
            "Density-matrix cleanup exceeded 1e-10; inspect raw "
            "physicality diagnostics."
        )
    return warnings


def _audit_response(audit) -> dict[str, object] | None:
    return None if audit is None else audit.to_dict()


def _limitations(evolution_method: str) -> list[str]:
    limitations = [
        item
        for item in PULSE_MODEL_LIMITATIONS
        if "fixed-step RK4" not in item
    ]
    if evolution_method == "fixed_step_rk4":
        limitations.append(
            "The fixed-step RK4 path applies density-matrix cleanup after "
            "each step."
        )
    else:
        limitations.append(
            "Explicit CPTP evolution is CPTP for the frozen interval maps, "
            "while time dependence is approximated at interval midpoints."
        )
    return limitations


def _state_overlap(state: Matrix, reference: Matrix) -> float:
    return _probability(trace(matmul(state, reference)).real)


def _purity(state: Matrix) -> float:
    return _probability(trace(matmul(state, state)).real)


def _probability(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def _inverse_or_none(value: float) -> float | None:
    return None if value <= 0.0 else 1.0 / value


def _physicality_response(
    metrics: PhysicalityMetrics,
) -> dict[str, float]:
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

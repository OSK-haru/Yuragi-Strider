"""B-4 validation for Gaussian DRAG control in the qutrit model."""

from __future__ import annotations

import math
import time
from typing import Any

from core.gates import Matrix
from core.pulse_envelopes import GaussianPulseEnvelope
from core.pulse_qutrit import (
    ClosedQutritSequenceResult,
    QutritPulseHamiltonian,
    evolve_closed_qutrit_sequence,
    qutrit_initial_density_matrix,
)
from core.pulse_qutrit_contract import (
    transmon_anharmonicity_rad_per_us,
)
from core.pulse_qutrit_open_system import (
    QutritDissipationRates,
    evolve_open_qutrit_sequence,
)
from core.pulse_step_policy import recommended_qutrit_step_policy
from validation_pulse.time_grid import uniform_times


SELECTED_DRAG_BETA_US = 0.001
BETA_SWEEP_US = (
    -0.003,
    -0.002,
    -0.001,
    0.0,
    0.0005,
    0.001,
    0.0015,
    0.002,
    0.003,
)
REFINEMENT_FACTORS = (4.0, 2.0, 1.0, 0.5)
REFERENCE_FACTOR = 0.25
STATE_ERROR_TOLERANCE = 2e-7
RAW_TRACE_TOLERANCE = 1e-10
RAW_HERMITICITY_TOLERANCE = 1e-10
RAW_MINIMUM_EIGENVALUE_TOLERANCE = -1e-9
CLEANUP_CORRECTION_TOLERANCE = 1e-10
DERIVATIVE_ABSOLUTE_TOLERANCE = 1e-5
DERIVATIVE_RELATIVE_TOLERANCE = 1e-9
PRIMARY_MAX_END_LEAKAGE_RATIO = 0.2
PRIMARY_MIN_TARGET_FIDELITY = 0.9
SECONDARY_MIN_TARGET_FIDELITY = 0.98
SECONDARY_MAX_PHASE_ERROR_RAD = 0.08
SECONDARY_MIN_COMPUTATIONAL_POPULATION = 0.99


def run_qutrit_drag_validation(
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the fixed B-4 derivative, sweep, and convergence cases."""

    rows: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    started = time.perf_counter()

    derivative = _derivative_validation(rows)
    boundary = _boundary_validation(rows)
    sign = _quadrature_sign_validation(rows)
    beta_zero = _beta_zero_validation(rows)
    sweep, sweep_rows = _beta_sweep_validation()
    rows.extend(sweep_rows)
    primary, primary_rows = _primary_pi_improvement()
    rows.extend(primary_rows)
    secondary = _secondary_pi_over_two_quality(sweep_rows)
    dissipative, dissipative_rows = _dissipative_compatibility()
    rows.extend(dissipative_rows)
    convergence, convergence_rows = _drag_convergence()
    rows.extend(convergence_rows)
    cases.extend((
        derivative,
        boundary,
        sign,
        beta_zero,
        sweep,
        primary,
        secondary,
        dissipative,
        convergence,
    ))

    overall_pass = all(bool(case["pass"]) for case in cases)
    return {
        "validation_id": "pulse_b4_qutrit_drag_v1",
        "overall_pass": overall_pass,
        "model_id": "driven_transmon_qutrit_rwa_experimental_v1",
        "contract_version": "pulse-extension-b-v1",
        "capability_status": "contract_only",
        "frame": "rotating",
        "approximation": "RWA",
        "basis_order": ["0", "1", "2"],
        "subsystem_dimensions": [3],
        "drag_convention": {
            "omega_x": "Omega(t)",
            "omega_y": "drag_beta_us * dOmega(t)/dt",
            "phase_rotation": (
                "in-phase axis rotated by phase_rad; positive quadrature "
                "is +90 degrees from that axis"
            ),
            "drag_beta_unit": "us",
            "selected_fixture_beta_us": SELECTED_DRAG_BETA_US,
        },
        "boundary_rule": {
            "inside_support": (
                "analytic Gaussian and derivative on inclusive [0, T]"
            ),
            "outside_support": "both drives are zero",
            "cutoff": "hard truncated Gaussian; no smoothing added",
        },
        "solver": "fixed-step RK4 with B-3 qutrit step policy",
        "cleanup_policy": (
            "raw metrics before cleanup; no PSD projection"
        ),
        "tolerances": {
            "state_error": STATE_ERROR_TOLERANCE,
            "raw_trace": RAW_TRACE_TOLERANCE,
            "raw_hermiticity": RAW_HERMITICITY_TOLERANCE,
            "raw_minimum_eigenvalue": (
                RAW_MINIMUM_EIGENVALUE_TOLERANCE
            ),
            "cleanup_correction": CLEANUP_CORRECTION_TOLERANCE,
            "derivative_absolute": DERIVATIVE_ABSOLUTE_TOLERANCE,
            "derivative_relative": DERIVATIVE_RELATIVE_TOLERANCE,
            "primary_max_end_leakage_ratio": (
                PRIMARY_MAX_END_LEAKAGE_RATIO
            ),
            "primary_min_target_fidelity": (
                PRIMARY_MIN_TARGET_FIDELITY
            ),
            "secondary_min_target_fidelity": (
                SECONDARY_MIN_TARGET_FIDELITY
            ),
            "secondary_max_phase_error_rad": (
                SECONDARY_MAX_PHASE_ERROR_RAD
            ),
            "secondary_min_computational_population": (
                SECONDARY_MIN_COMPUTATIONAL_POPULATION
            ),
        },
        "cases": cases,
        "runtime_ms": (time.perf_counter() - started) * 1000.0,
        "scope_and_limitations": {
            "proves": [
                "analytic Gaussian derivative and DRAG sign convention",
                "beta zero exactly preserves non-DRAG qutrit evolution",
                "one fixed positive-beta pi pulse lowers leakage and raises target fidelity",
                "one fixed pi/2 condition improves leakage without hiding fidelity or phase error",
                "DRAG works with the existing qutrit dissipative path",
                "DRAG on/off trajectories converge under the extended B-3 policy",
            ],
            "does_not_prove": [
                "a universal optimal beta",
                "hardware-calibrated DRAG performance",
                "smooth-edge pulse behavior",
                "strict finite-step CPTP propagation",
                "QuTiP qutrit agreement",
                "public qutrit API readiness",
            ],
        },
    }, rows


def _derivative_validation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    envelope = _gaussian(math.pi / 2.0)
    delta = 1e-8
    normalized_points = (-2.5, -1.0, -0.25, 0.25, 1.0, 2.5)
    errors = []
    relative_errors = []
    for normalized in normalized_points:
        time_us = envelope.center_us + normalized * envelope.sigma_us
        finite_difference = (
            envelope.amplitude_rad_per_us(time_us + delta)
            - envelope.amplitude_rad_per_us(time_us - delta)
        ) / (2.0 * delta)
        analytic = envelope.derivative_rad_per_us2(time_us)
        error = abs(analytic - finite_difference)
        relative_error = error / max(abs(analytic), 1.0)
        errors.append(error)
        relative_errors.append(relative_error)
        rows.append({
            "row_type": "derivative",
            "case": "analytic_derivative",
            "normalized_time": normalized,
            "time_us": time_us,
            "analytic_derivative_rad_per_us2": analytic,
            "finite_difference_rad_per_us2": finite_difference,
            "absolute_error": error,
            "relative_error": relative_error,
        })
    maximum_error = max(errors)
    maximum_relative_error = max(relative_errors)
    return {
        "name": "analytic_gaussian_derivative",
        "maximum_absolute_error": maximum_error,
        "maximum_relative_error": maximum_relative_error,
        "finite_difference_delta_us": delta,
        "pass": (
            maximum_error <= DERIVATIVE_ABSOLUTE_TOLERANCE
            and maximum_relative_error <= DERIVATIVE_RELATIVE_TOLERANCE
        ),
    }


def _boundary_validation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    envelope = _gaussian(math.pi / 2.0)
    start_amplitude = envelope.amplitude_rad_per_us(0.0)
    end_amplitude = envelope.amplitude_rad_per_us(envelope.duration_us)
    start_derivative = envelope.derivative_rad_per_us2(0.0)
    end_derivative = envelope.derivative_rad_per_us2(
        envelope.duration_us
    )
    outside_before = envelope.derivative_rad_per_us2(-1e-12)
    outside_after = envelope.derivative_rad_per_us2(
        envelope.duration_us + 1e-12
    )
    rows.append({
        "row_type": "boundary",
        "case": "truncated_gaussian_boundary",
        "start_amplitude_rad_per_us": start_amplitude,
        "end_amplitude_rad_per_us": end_amplitude,
        "start_derivative_rad_per_us2": start_derivative,
        "end_derivative_rad_per_us2": end_derivative,
        "start_drag_quadrature_rad_per_us": (
            SELECTED_DRAG_BETA_US * start_derivative
        ),
        "end_drag_quadrature_rad_per_us": (
            SELECTED_DRAG_BETA_US * end_derivative
        ),
        "outside_before_derivative_rad_per_us2": outside_before,
        "outside_after_derivative_rad_per_us2": outside_after,
    })
    passed = (
        start_amplitude > 0.0
        and math.isclose(start_amplitude, end_amplitude)
        and start_derivative > 0.0
        and math.isclose(start_derivative, -end_derivative)
        and outside_before == 0.0
        and outside_after == 0.0
    )
    return {
        "name": "truncated_gaussian_boundary",
        "start_amplitude_rad_per_us": start_amplitude,
        "end_amplitude_rad_per_us": end_amplitude,
        "start_derivative_rad_per_us2": start_derivative,
        "end_derivative_rad_per_us2": end_derivative,
        "pass": passed,
    }


def _quadrature_sign_validation(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    envelope = _gaussian(math.pi / 2.0)
    time_us = envelope.center_us - envelope.sigma_us
    positive = QutritPulseHamiltonian(
        envelope,
        _alpha(),
        drag_beta_us=SELECTED_DRAG_BETA_US,
    ).evaluate(time_us)
    negative = QutritPulseHamiltonian(
        envelope,
        _alpha(),
        drag_beta_us=-SELECTED_DRAG_BETA_US,
    ).evaluate(time_us)
    positive_omega_y = -2.0 * positive[0][1].imag
    negative_omega_y = -2.0 * negative[0][1].imag
    rows.append({
        "row_type": "sign",
        "case": "positive_negative_beta_sign",
        "time_us": time_us,
        "positive_beta_omega_y_rad_per_us": positive_omega_y,
        "negative_beta_omega_y_rad_per_us": negative_omega_y,
    })
    passed = (
        positive_omega_y > 0.0
        and negative_omega_y < 0.0
        and math.isclose(positive_omega_y, -negative_omega_y)
    )
    return {
        "name": "positive_negative_beta_sign",
        "positive_beta_omega_y_rad_per_us": positive_omega_y,
        "negative_beta_omega_y_rad_per_us": negative_omega_y,
        "pass": passed,
    }


def _beta_zero_validation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    envelope = _gaussian(math.pi / 2.0)
    policy = _policy(envelope, 0.0, _zero_rates())
    initial = qutrit_initial_density_matrix("0")
    implicit = evolve_closed_qutrit_sequence(
        initial,
        envelope,
        _alpha(),
        envelope.duration_us,
        policy.selected_internal_step_cap_us,
    )
    explicit = evolve_closed_qutrit_sequence(
        initial,
        envelope,
        _alpha(),
        envelope.duration_us,
        policy.selected_internal_step_cap_us,
        drag_beta_us=0.0,
    )
    state_error = _matrix_max_error(
        implicit.final_state,
        explicit.final_state,
    )
    rows.append({
        "row_type": "compatibility",
        "case": "beta_zero_exact_compatibility",
        "matrix_max_element_error": state_error,
    })
    return {
        "name": "beta_zero_exact_compatibility",
        "matrix_max_element_error": state_error,
        "diagnostics_equal": (
            implicit.pulse_result.diagnostics
            == explicit.pulse_result.diagnostics
        ),
        "pass": (
            state_error == 0.0
            and implicit.pulse_result.diagnostics
            == explicit.pulse_result.diagnostics
        ),
    }


def _beta_sweep_validation(
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    envelope = _gaussian(math.pi / 2.0)
    rows = [
        _closed_metrics(envelope, math.pi / 2.0, beta, "beta_sweep")
        for beta in BETA_SWEEP_US
    ]
    baseline = _row_for_beta(rows, 0.0)
    selected = _row_for_beta(rows, SELECTED_DRAG_BETA_US)
    negative = _row_for_beta(rows, -SELECTED_DRAG_BETA_US)
    best_leakage = min(rows, key=lambda row: row["end_leakage"])
    passed = (
        selected["end_leakage"] < baseline["end_leakage"]
        and selected["target_fidelity"] > baseline["target_fidelity"]
        and selected["phase_error_rad"] < baseline["phase_error_rad"]
        and negative["end_leakage"] > selected["end_leakage"]
    )
    return {
        "name": "positive_zero_negative_beta_sweep",
        "beta_values_us": list(BETA_SWEEP_US),
        "selected_beta_us": SELECTED_DRAG_BETA_US,
        "minimum_end_leakage_beta_us": best_leakage["drag_beta_us"],
        "minimum_end_leakage": best_leakage["end_leakage"],
        "selected_end_leakage": selected["end_leakage"],
        "baseline_end_leakage": baseline["end_leakage"],
        "negative_beta_end_leakage": negative["end_leakage"],
        "pass": passed,
    }, rows


def _primary_pi_improvement(
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    envelope = _gaussian(math.pi)
    baseline = _closed_metrics(envelope, math.pi, 0.0, "primary_pi")
    selected = _closed_metrics(
        envelope,
        math.pi,
        SELECTED_DRAG_BETA_US,
        "primary_pi",
    )
    ratio = selected["end_leakage"] / baseline["end_leakage"]
    passed = (
        ratio <= PRIMARY_MAX_END_LEAKAGE_RATIO
        and selected["target_fidelity"] >= PRIMARY_MIN_TARGET_FIDELITY
        and selected["target_fidelity"] > baseline["target_fidelity"]
    )
    return {
        "name": "fixed_pi_leakage_and_fidelity_improvement",
        "anharmonicity_mhz": -100.0,
        "sigma_us": envelope.sigma_us,
        "selected_beta_us": SELECTED_DRAG_BETA_US,
        "baseline": _quality_summary(baseline),
        "drag": _quality_summary(selected),
        "end_leakage_ratio": ratio,
        "pass": passed,
    }, [baseline, selected]


def _secondary_pi_over_two_quality(
    sweep_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    baseline = _row_for_beta(sweep_rows, 0.0)
    selected = _row_for_beta(sweep_rows, SELECTED_DRAG_BETA_US)
    passed = (
        selected["end_leakage"] < baseline["end_leakage"]
        and selected["target_fidelity"]
        >= SECONDARY_MIN_TARGET_FIDELITY
        and selected["phase_error_rad"]
        <= SECONDARY_MAX_PHASE_ERROR_RAD
        and selected["computational_population"]
        >= SECONDARY_MIN_COMPUTATIONAL_POPULATION
    )
    return {
        "name": "pi_over_two_fidelity_phase_guard",
        "baseline": _quality_summary(baseline),
        "drag": _quality_summary(selected),
        "pass": passed,
    }


def _dissipative_compatibility(
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    envelope = _gaussian(math.pi / 2.0)
    rates = QutritDissipationRates(
        "direct_rates",
        0.8,
        0.2,
        1.4,
        0.1,
        0.5,
    )
    baseline = _open_metrics(
        envelope,
        math.pi / 2.0,
        0.0,
        rates,
        "dissipative_drag",
    )
    selected = _open_metrics(
        envelope,
        math.pi / 2.0,
        SELECTED_DRAG_BETA_US,
        rates,
        "dissipative_drag",
    )
    passed = (
        selected["end_leakage"] < baseline["end_leakage"]
        and selected["target_fidelity"] > baseline["target_fidelity"]
        and _physicality_pass(selected)
    )
    return {
        "name": "dissipative_drag_compatibility",
        "rates": rates.to_dict(),
        "baseline": _quality_summary(baseline),
        "drag": _quality_summary(selected),
        "drag_physicality": _physicality_summary(selected),
        "pass": passed,
    }, [baseline, selected]


def _drag_convergence(
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    envelope = _gaussian(math.pi / 2.0)
    rows: list[dict[str, Any]] = []
    mode_reports = []
    for beta, label in (
        (0.0, "drag_off"),
        (SELECTED_DRAG_BETA_US, "drag_on"),
    ):
        policy = _policy(envelope, beta, _zero_rates())
        reference = _run_closed(
            envelope,
            beta,
            policy.selected_internal_step_cap_us * REFERENCE_FACTOR,
        )
        reference_state = reference.final_state
        reference_leakage = (
            reference.leakage.maximum_recorded_leakage_probability
        )
        mode_rows = []
        for factor in REFINEMENT_FACTORS:
            result, runtime_ms = _timed_closed(
                envelope,
                beta,
                policy.selected_internal_step_cap_us * factor,
            )
            diagnostics = result.pulse_result.diagnostics
            row = {
                "row_type": "convergence",
                "case": label,
                "drag_beta_us": beta,
                "step_factor": factor,
                "requested_internal_step_cap_us": (
                    policy.selected_internal_step_cap_us * factor
                ),
                "selected_internal_step_cap_us": (
                    policy.selected_internal_step_cap_us
                ),
                "step_limit_reason": policy.step_limit_reason,
                "matrix_max_element_error": _matrix_max_error(
                    result.final_state,
                    reference_state,
                ),
                "maximum_leakage_error": abs(
                    result.leakage.maximum_recorded_leakage_probability
                    - reference_leakage
                ),
                "runtime_ms": runtime_ms,
                **_diagnostics_dict(diagnostics),
            }
            rows.append(row)
            mode_rows.append(row)
        errors = [
            row["matrix_max_element_error"] for row in mode_rows
        ]
        policy_row = next(
            row for row in mode_rows if row["step_factor"] == 1.0
        )
        convergence_pass = _nonincreasing(errors)
        policy_pass = (
            policy_row["matrix_max_element_error"]
            <= STATE_ERROR_TOLERANCE
            and _physicality_pass(policy_row)
        )
        mode_reports.append({
            "mode": label,
            "drag_beta_us": beta,
            "policy": policy.to_dict(),
            "matrix_errors": errors,
            "observed_orders": [
                _observed_order(errors[index], errors[index + 1])
                for index in range(len(errors) - 1)
            ],
            "convergence_pass": convergence_pass,
            "policy_pass": policy_pass,
        })
    return {
        "name": "drag_on_off_refinement",
        "refinement_factors": list(REFINEMENT_FACTORS),
        "reference_factor": REFERENCE_FACTOR,
        "modes": mode_reports,
        "hard_cutoff_note": (
            "Endpoint amplitude and derivative are nonzero; asymptotic "
            "order is reported rather than assumed."
        ),
        "pass": all(
            mode["convergence_pass"] and mode["policy_pass"]
            for mode in mode_reports
        ),
    }, rows


def _closed_metrics(
    envelope: GaussianPulseEnvelope,
    target_angle_rad: float,
    beta_us: float,
    case: str,
) -> dict[str, Any]:
    policy = _policy(envelope, beta_us, _zero_rates())
    result, runtime_ms = _timed_closed(
        envelope,
        beta_us,
        policy.selected_internal_step_cap_us,
    )
    return _quality_row(
        case,
        target_angle_rad,
        beta_us,
        policy.to_dict(),
        result,
        runtime_ms,
    )


def _open_metrics(
    envelope: GaussianPulseEnvelope,
    target_angle_rad: float,
    beta_us: float,
    rates: QutritDissipationRates,
    case: str,
) -> dict[str, Any]:
    policy = _policy(envelope, beta_us, rates)
    started = time.perf_counter()
    result = evolve_open_qutrit_sequence(
        qutrit_initial_density_matrix("0"),
        envelope,
        _alpha(),
        rates,
        envelope.duration_us,
        policy.selected_internal_step_cap_us,
        drag_beta_us=beta_us,
        pulse_checkpoint_times_us=uniform_times(
            envelope.duration_us,
            81,
        ),
    )
    runtime_ms = (time.perf_counter() - started) * 1000.0
    return _quality_row(
        case,
        target_angle_rad,
        beta_us,
        policy.to_dict(),
        result,
        runtime_ms,
    )


def _quality_row(
    case: str,
    target_angle_rad: float,
    beta_us: float,
    policy: dict[str, Any],
    result: Any,
    runtime_ms: float,
) -> dict[str, Any]:
    state = result.final_state
    computational_population = float(
        state[0][0].real + state[1][1].real
    )
    target_fidelity = _target_fidelity(state, target_angle_rad)
    conditional_fidelity = (
        target_fidelity / computational_population
        if computational_population > 0.0
        else 0.0
    )
    phase_error = _phase_error_rad(state, target_angle_rad)
    return {
        "row_type": "quality",
        "case": case,
        "drag_beta_us": beta_us,
        "maximum_recorded_leakage": (
            result.leakage.maximum_recorded_leakage_probability
        ),
        "end_leakage": result.leakage.leakage_at_final_time,
        "target_fidelity": target_fidelity,
        "conditional_computational_fidelity": conditional_fidelity,
        "phase_error_rad": phase_error,
        "computational_population": computational_population,
        "runtime_ms": runtime_ms,
        "selected_internal_step_cap_us": (
            policy["selected_internal_step_cap_us"]
        ),
        "step_limit_reason": policy["step_limit_reason"],
        "maximum_drive_magnitude_rad_per_us": (
            policy["maximum_drive_magnitude_rad_per_us"]
        ),
        **_diagnostics_dict(result.pulse_result.diagnostics),
    }


def _run_closed(
    envelope: GaussianPulseEnvelope,
    beta_us: float,
    max_step_us: float,
) -> ClosedQutritSequenceResult:
    return evolve_closed_qutrit_sequence(
        qutrit_initial_density_matrix("0"),
        envelope,
        _alpha(),
        envelope.duration_us,
        max_step_us,
        drag_beta_us=beta_us,
        pulse_checkpoint_times_us=uniform_times(
            envelope.duration_us,
            81,
        ),
    )


def _timed_closed(
    envelope: GaussianPulseEnvelope,
    beta_us: float,
    max_step_us: float,
) -> tuple[ClosedQutritSequenceResult, float]:
    started = time.perf_counter()
    result = _run_closed(envelope, beta_us, max_step_us)
    return result, (time.perf_counter() - started) * 1000.0


def _policy(
    envelope: GaussianPulseEnvelope,
    beta_us: float,
    rates: QutritDissipationRates,
):
    return recommended_qutrit_step_policy(
        envelope,
        0.0,
        _alpha(),
        rates,
        envelope.duration_us,
        drag_beta_us=beta_us,
    )


def _gaussian(angle_rad: float) -> GaussianPulseEnvelope:
    return GaussianPulseEnvelope.from_target_rotation_angle(
        angle_rad,
        sigma_us=0.002,
        truncation_sigma=4.0,
    )


def _alpha() -> float:
    return transmon_anharmonicity_rad_per_us(-100.0)


def _zero_rates() -> QutritDissipationRates:
    return QutritDissipationRates("direct_rates", 0.0, 0.0, 0.0, 0.0, 0.0)


def _target_fidelity(state: Matrix, angle_rad: float) -> float:
    target = (
        complex(math.cos(angle_rad / 2.0), 0.0),
        complex(0.0, -math.sin(angle_rad / 2.0)),
        0.0 + 0.0j,
    )
    value = sum(
        target[row].conjugate() * state[row][column] * target[column]
        for row in range(3)
        for column in range(3)
    )
    return float(value.real)


def _phase_error_rad(state: Matrix, angle_rad: float) -> float | None:
    expected_coherence = (
        math.cos(angle_rad / 2.0) * math.sin(angle_rad / 2.0)
    )
    if abs(expected_coherence) <= 1e-12 or abs(state[0][1]) <= 1e-12:
        return None
    actual_phase = math.atan2(state[0][1].imag, state[0][1].real)
    return abs(_wrap_phase(actual_phase - math.pi / 2.0))


def _wrap_phase(value: float) -> float:
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def _diagnostics_dict(diagnostics: Any) -> dict[str, float | int]:
    return {
        "actual_internal_step_count": diagnostics.internal_step_count,
        "actual_internal_step_min_us": (
            diagnostics.minimum_internal_step_us
        ),
        "actual_internal_step_max_us": (
            diagnostics.maximum_internal_step_us
        ),
        "raw_trace_error": diagnostics.raw_trace_error,
        "raw_hermiticity_error": diagnostics.raw_hermiticity_error,
        "raw_minimum_eigenvalue": diagnostics.raw_minimum_eigenvalue,
        "cleanup_correction_norm": diagnostics.cleanup_correction_norm,
    }


def _quality_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "drag_beta_us": row["drag_beta_us"],
        "maximum_recorded_leakage": row["maximum_recorded_leakage"],
        "end_leakage": row["end_leakage"],
        "target_fidelity": row["target_fidelity"],
        "conditional_computational_fidelity": (
            row["conditional_computational_fidelity"]
        ),
        "phase_error_rad": row["phase_error_rad"],
        "computational_population": row["computational_population"],
    }


def _physicality_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "raw_trace_error": row["raw_trace_error"],
        "raw_hermiticity_error": row["raw_hermiticity_error"],
        "raw_minimum_eigenvalue": row["raw_minimum_eigenvalue"],
        "cleanup_correction_norm": row["cleanup_correction_norm"],
    }


def _physicality_pass(row: dict[str, Any]) -> bool:
    return (
        row["raw_trace_error"] <= RAW_TRACE_TOLERANCE
        and row["raw_hermiticity_error"] <= RAW_HERMITICITY_TOLERANCE
        and row["raw_minimum_eigenvalue"]
        >= RAW_MINIMUM_EIGENVALUE_TOLERANCE
        and row["cleanup_correction_norm"]
        <= CLEANUP_CORRECTION_TOLERANCE
    )


def _row_for_beta(
    rows: list[dict[str, Any]],
    beta_us: float,
) -> dict[str, Any]:
    return next(
        row for row in rows
        if math.isclose(
            row["drag_beta_us"],
            beta_us,
            rel_tol=0.0,
            abs_tol=1e-15,
        )
    )


def _matrix_max_error(actual: Matrix, expected: Matrix) -> float:
    return max(
        abs(actual[row][column] - expected[row][column])
        for row in range(3)
        for column in range(3)
    )


def _nonincreasing(values: list[float]) -> bool:
    return all(
        finer <= max(coarser * 1.05, 5e-14)
        for coarser, finer in zip(values, values[1:])
    )


def _observed_order(coarse: float, fine: float) -> float | None:
    if coarse <= 0.0 or fine <= 0.0:
        return None
    return math.log(coarse / fine, 2.0)

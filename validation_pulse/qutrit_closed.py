"""Reproducible closed-qutrit validation cases for Pulse Extension B-1."""

from __future__ import annotations

import cmath
import math
from time import perf_counter

from core.gates import Matrix, density_from_ket
from core.pulse_envelopes import (
    GaussianPulseEnvelope,
    SquarePulseEnvelope,
    TwoLevelPulseHamiltonian,
)
from core.pulse_evolution import evolve_time_dependent_segment
from core.pulse_qutrit import (
    ClosedQutritSequenceResult,
    evolve_closed_qutrit_sequence,
    qutrit_initial_density_matrix,
    qutrit_populations,
)
from core.pulse_qutrit_contract import (
    transmon_anharmonicity_rad_per_us,
)
from validation_pulse.time_grid import uniform_times


COHERENCE_TOLERANCE = 2e-7
WEAK_TWO_LEVEL_BLOCK_TOLERANCE = 2e-3
WEAK_LEAKAGE_TOLERANCE = 2e-5
PHYSICALITY_TOLERANCE = 1e-10
POPULATION_TOLERANCE = 2e-7
MINIMUM_DEMONSTRATED_LEAKAGE = 1e-4
MINIMUM_IDLE_COHERENCE_CHANGE = 1e-4


def run_closed_qutrit_validation() -> tuple[dict[str, object], list[dict[str, object]]]:
    """Run the fixed B-1 validation set and return report plus CSV rows."""

    started = perf_counter()
    rows: list[dict[str, object]] = []

    zero_drive = _zero_drive_case(rows)
    free_coherence = _free_coherence_case(rows)
    weak_two_level = _weak_two_level_case(rows)
    alpha_sweep = _alpha_sweep_case(rows)
    closed_idle = _closed_idle_case(rows)
    cases = (
        zero_drive,
        free_coherence,
        weak_two_level,
        alpha_sweep,
        closed_idle,
    )
    overall_pass = all(bool(case["pass"]) for case in cases)

    return {
        "validation": "PULSE-B1-CLOSED-QUTRIT",
        "model_id": "driven_transmon_qutrit_rwa_experimental_v1",
        "contract_version": "pulse-extension-b-v1",
        "capability_status": "contract_only",
        "frame": "rotating",
        "approximation": "RWA",
        "basis_order": ["0", "1", "2"],
        "subsystem_dimensions": [3],
        "solver": "fixed-step classical RK4 with post-step cleanup",
        "tolerances": {
            "coherence_max_element_error": COHERENCE_TOLERANCE,
            "weak_two_level_block_error": WEAK_TWO_LEVEL_BLOCK_TOLERANCE,
            "weak_leakage": WEAK_LEAKAGE_TOLERANCE,
            "raw_physicality": PHYSICALITY_TOLERANCE,
            "population": POPULATION_TOLERANCE,
            "minimum_demonstrated_leakage": MINIMUM_DEMONSTRATED_LEAKAGE,
            "minimum_idle_coherence_change": (
                MINIMUM_IDLE_COHERENCE_CHANGE
            ),
        },
        "cases": list(cases),
        "runtime_ms": (perf_counter() - started) * 1000.0,
        "overall_pass": overall_pass,
        "scope_and_limitations": {
            "proves": [
                "the generic time-dependent solver accepts 3x3 density matrices",
                "zero-drive qutrit populations and analytic free coherence evolution",
                "a fixed weak selective pulse remains near the two-level result",
                "nonzero leakage is represented by rho_22",
                "a larger absolute anharmonicity lowers recorded leakage in one fixed case",
                "closed free-idle evolution preserves populations",
                "raw physicality is acceptable at the declared fine validation steps",
            ],
            "does_not_prove": [
                "qutrit dissipation or finite-temperature behavior",
                "a production-safe qutrit step policy",
                "DRAG behavior",
                "QuTiP qutrit agreement",
                "hardware-calibrated leakage prediction",
                "that checkpoint-sampled maximum leakage captures every between-checkpoint extremum",
            ],
        },
    }, rows


def _zero_drive_case(rows: list[dict[str, object]]) -> dict[str, object]:
    envelope = SquarePulseEnvelope(0.0, 0.005)
    result = evolve_closed_qutrit_sequence(
        qutrit_initial_density_matrix("2"),
        envelope,
        transmon_anharmonicity_rad_per_us(-250.0),
        total_simulation_time_us=0.01,
        max_step_us=1e-5,
        pulse_checkpoint_times_us=(0.0, 0.005),
        idle_checkpoint_times_us=(0.0, 0.005),
    )
    _append_rows(rows, "zero_drive_basis_2", result)
    maximum_population_error = max(
        max(
            abs(point.population_0),
            abs(point.population_1),
            abs(point.population_2 - 1.0),
        )
        for point in result.trajectory
    )
    physicality = _physicality_summary(result)
    passed = (
        maximum_population_error <= POPULATION_TOLERANCE
        and _physicality_passes(physicality)
    )
    return {
        "name": "zero_drive_basis_2",
        "maximum_population_error": maximum_population_error,
        "physicality": physicality,
        "pass": passed,
    }


def _free_coherence_case(
    rows: list[dict[str, object]],
) -> dict[str, object]:
    inverse_sqrt_two = 1.0 / math.sqrt(2.0)
    initial = density_from_ket(
        (inverse_sqrt_two, 0.0 + 0.0j, inverse_sqrt_two)
    )
    detuning = 0.4
    anharmonicity = transmon_anharmonicity_rad_per_us(-250.0)
    duration = 0.002
    envelope = SquarePulseEnvelope(0.0, duration)
    result = evolve_closed_qutrit_sequence(
        initial,
        envelope,
        anharmonicity,
        total_simulation_time_us=duration,
        max_step_us=2e-6,
        detuning_rad_per_us=detuning,
        pulse_checkpoint_times_us=uniform_times(duration, 21),
    )
    _append_rows(rows, "free_coherence_0_2", result)
    energy_2 = -2.0 * detuning + anharmonicity
    expected_02 = initial[0][2] * cmath.exp(1j * energy_2 * duration)
    coherence_error = abs(result.final_state[0][2] - expected_02)
    population_error = max(
        abs(result.final_state[index][index] - initial[index][index])
        for index in range(3)
    )
    physicality = _physicality_summary(result)
    return {
        "name": "free_coherence_0_2",
        "detuning_rad_per_us": detuning,
        "anharmonicity_mhz": -250.0,
        "coherence_02_error": coherence_error,
        "maximum_population_error": population_error,
        "physicality": physicality,
        "pass": (
            coherence_error <= COHERENCE_TOLERANCE
            and population_error <= POPULATION_TOLERANCE
            and _physicality_passes(physicality)
        ),
    }


def _weak_two_level_case(
    rows: list[dict[str, object]],
) -> dict[str, object]:
    envelope = GaussianPulseEnvelope.from_target_rotation_angle(
        math.pi / 2.0,
        sigma_us=0.05,
        truncation_sigma=4.0,
    )
    times = uniform_times(envelope.duration_us, 41)
    qutrit = evolve_closed_qutrit_sequence(
        qutrit_initial_density_matrix("0"),
        envelope,
        transmon_anharmonicity_rad_per_us(-250.0),
        total_simulation_time_us=envelope.duration_us,
        max_step_us=2e-5,
        pulse_checkpoint_times_us=times,
    )
    two_level = evolve_time_dependent_segment(
        ((1.0 + 0.0j, 0.0 + 0.0j), (0.0 + 0.0j, 0.0 + 0.0j)),
        TwoLevelPulseHamiltonian(envelope),
        (),
        duration_us=envelope.duration_us,
        max_step_us=0.001,
        checkpoint_times_us=times,
    )
    _append_rows(rows, "weak_selective_pi_over_2", qutrit)
    block_error = _top_left_block_error(qutrit.final_state, two_level.state)
    leakage = qutrit.leakage.leakage_at_final_time
    physicality = _physicality_summary(qutrit)
    return {
        "name": "weak_selective_pi_over_2",
        "anharmonicity_mhz": -250.0,
        "sigma_us": envelope.sigma_us,
        "qutrit_to_two_level_block_error": block_error,
        "final_leakage_probability": leakage,
        "physicality": physicality,
        "pass": (
            block_error <= WEAK_TWO_LEVEL_BLOCK_TOLERANCE
            and leakage <= WEAK_LEAKAGE_TOLERANCE
            and _physicality_passes(physicality)
        ),
    }


def _alpha_sweep_case(
    rows: list[dict[str, object]],
) -> dict[str, object]:
    envelope = GaussianPulseEnvelope.from_target_rotation_angle(
        math.pi,
        sigma_us=0.002,
        truncation_sigma=4.0,
    )
    times = uniform_times(envelope.duration_us, 81)
    by_alpha: dict[str, dict[str, object]] = {}
    results = {}
    for alpha_mhz in (-100.0, -300.0):
        result = evolve_closed_qutrit_sequence(
            qutrit_initial_density_matrix("0"),
            envelope,
            transmon_anharmonicity_rad_per_us(alpha_mhz),
            total_simulation_time_us=envelope.duration_us,
            max_step_us=2e-6,
            pulse_checkpoint_times_us=times,
        )
        results[alpha_mhz] = result
        _append_rows(rows, f"alpha_{int(alpha_mhz)}_mhz", result)
        by_alpha[str(alpha_mhz)] = {
            "maximum_recorded_leakage_probability": (
                result.leakage.maximum_recorded_leakage_probability
            ),
            "leakage_at_pulse_end": result.leakage.leakage_at_pulse_end,
            "physicality": _physicality_summary(result),
        }

    leakage_100 = results[-100.0].leakage.maximum_recorded_leakage_probability
    leakage_300 = results[-300.0].leakage.maximum_recorded_leakage_probability
    passed = (
        leakage_100 > leakage_300
        and leakage_100 >= MINIMUM_DEMONSTRATED_LEAKAGE
        and all(
            _physicality_passes(_physicality_summary(result))
            for result in results.values()
        )
    )
    return {
        "name": "fixed_gaussian_anharmonicity_comparison",
        "sigma_us": envelope.sigma_us,
        "target_rotation_angle_rad": math.pi,
        "by_anharmonicity_mhz": by_alpha,
        "observed_larger_abs_alpha_has_lower_recorded_leakage": (
            leakage_300 < leakage_100
        ),
        "pass": passed,
    }


def _closed_idle_case(
    rows: list[dict[str, object]],
) -> dict[str, object]:
    envelope = GaussianPulseEnvelope.from_target_rotation_angle(
        math.pi / 2.0,
        sigma_us=0.004,
        truncation_sigma=4.0,
    )
    pulse_duration = envelope.duration_us
    idle_duration = 0.003
    result = evolve_closed_qutrit_sequence(
        qutrit_initial_density_matrix("0"),
        envelope,
        transmon_anharmonicity_rad_per_us(-200.0),
        total_simulation_time_us=pulse_duration + idle_duration,
        max_step_us=2e-6,
        detuning_rad_per_us=0.5,
        pulse_checkpoint_times_us=uniform_times(pulse_duration, 41),
        idle_checkpoint_times_us=uniform_times(idle_duration, 11),
    )
    _append_rows(rows, "closed_pulse_then_free_idle", result)
    pulse = qutrit_populations(result.pulse_end_state, pulse_duration, "pulse")
    final = qutrit_populations(
        result.final_state,
        pulse_duration + idle_duration,
        "idle",
    )
    population_change = max(
        abs(getattr(pulse, name) - getattr(final, name))
        for name in ("population_0", "population_1", "population_2")
    )
    coherence_change = abs(
        result.final_state[0][1] - result.pulse_end_state[0][1]
    )
    physicality = _physicality_summary(result)
    return {
        "name": "closed_pulse_then_free_idle",
        "detuning_rad_per_us": 0.5,
        "idle_duration_us": idle_duration,
        "maximum_population_change_during_idle": population_change,
        "coherence_01_change_during_idle": coherence_change,
        "physicality": physicality,
        "pass": (
            population_change <= POPULATION_TOLERANCE
            and coherence_change >= MINIMUM_IDLE_COHERENCE_CHANGE
            and _physicality_passes(physicality)
        ),
    }


def _append_rows(
    rows: list[dict[str, object]],
    case_name: str,
    result: ClosedQutritSequenceResult,
) -> None:
    for point in result.trajectory:
        rows.append({
            "case": case_name,
            "time_us": point.time_us,
            "segment": point.segment,
            "population_0": point.population_0,
            "population_1": point.population_1,
            "population_2": point.population_2,
            "computational_population": point.computational_population,
            "population_sum_error": point.population_sum_error,
        })


def _physicality_summary(
    result: ClosedQutritSequenceResult,
) -> dict[str, float | int]:
    diagnostics = [result.pulse_result.diagnostics]
    if result.idle_result is not None:
        diagnostics.append(result.idle_result.diagnostics)
    return {
        "maximum_raw_trace_error": max(
            item.raw_trace_error for item in diagnostics
        ),
        "maximum_raw_hermiticity_error": max(
            item.raw_hermiticity_error for item in diagnostics
        ),
        "minimum_raw_eigenvalue": min(
            item.raw_minimum_eigenvalue for item in diagnostics
        ),
        "maximum_cleanup_correction_norm": max(
            item.cleanup_correction_norm for item in diagnostics
        ),
        "maximum_population_sum_error": max(
            point.population_sum_error for point in result.trajectory
        ),
        "internal_step_count": sum(
            item.internal_step_count for item in diagnostics
        ),
    }


def _physicality_passes(metrics: dict[str, float | int]) -> bool:
    return (
        metrics["maximum_raw_trace_error"] <= PHYSICALITY_TOLERANCE
        and metrics["maximum_raw_hermiticity_error"]
        <= PHYSICALITY_TOLERANCE
        and metrics["minimum_raw_eigenvalue"] >= -PHYSICALITY_TOLERANCE
        and metrics["maximum_cleanup_correction_norm"]
        <= PHYSICALITY_TOLERANCE
        and metrics["maximum_population_sum_error"]
        <= PHYSICALITY_TOLERANCE
    )


def _top_left_block_error(
    qutrit_state: Matrix,
    two_level_state: Matrix,
) -> float:
    return max(
        abs(qutrit_state[row][column] - two_level_state[row][column])
        for row in range(2)
        for column in range(2)
    )

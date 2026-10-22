"""Independent QuTiP comparison for the Pulse Extension B qutrit path."""

from __future__ import annotations

import math
import platform
from dataclasses import dataclass
from typing import Any

import numpy as np

from core.gates import Matrix
from core.pulse_envelopes import GaussianPulseEnvelope
from core.pulse_qutrit import (
    QutritPulseHamiltonian,
    qutrit_initial_density_matrix,
)
from core.pulse_qutrit_contract import (
    QUTRIT_BASIS_LABELS,
    qutrit_rotating_frame_hamiltonian,
    transmon_anharmonicity_rad_per_us,
)
from core.pulse_qutrit_open_system import (
    QutritDissipationRates,
    evolve_open_qutrit_sequence,
    qutrit_collapse_operator_matrices,
)
from core.pulse_step_policy import recommended_qutrit_step_policy
from validation_pulse.qutip_adapter import (
    DEFAULT_OPTIONS,
    QUTIP_AVAILABLE,
    compare_density_matrices,
    qutip,
    run_qutip_constant_segment,
    run_qutip_time_dependent_segment,
)
from validation_pulse.time_grid import uniform_times


QUTRIT_QUTIP_TOLERANCE = 5e-7
QUTRIT_QUTIP_SUBSYSTEM_DIMENSIONS = (3,)
QUTRIT_QUTIP_CHECKPOINT_COUNT = 17


@dataclass(frozen=True)
class QutritQutipCase:
    name: str
    initial_state: str
    target_angle_rad: float
    sigma_us: float
    anharmonicity_mhz: float
    detuning_rad_per_us: float = 0.0
    phase_rad: float = 0.0
    drag_beta_us: float = 0.0
    idle_duration_us: float = 0.0
    rates: QutritDissipationRates = QutritDissipationRates(
        "direct_rates", 0.0, 0.0, 0.0, 0.0, 0.0
    )


def qutrit_qutip_cases() -> tuple[QutritQutipCase, ...]:
    zero = QutritDissipationRates(
        "direct_rates", 0.0, 0.0, 0.0, 0.0, 0.0
    )
    return (
        QutritQutipCase(
            "closed_gaussian_qutrit_pulse", "0", math.pi / 2, 0.004, -100.0
        ),
        QutritQutipCase(
            "detuned_leakage_trajectory",
            "0",
            math.pi,
            0.004,
            -100.0,
            detuning_rad_per_us=0.4 * math.pi,
        ),
        QutritQutipCase(
            "transition_specific_qutrit_dissipation",
            "2",
            0.0,
            0.004,
            -100.0,
            rates=QutritDissipationRates(
                "direct_rates", 0.7, 0.03, 1.1, 0.05, 0.0
            ),
        ),
        QutritQutipCase(
            "finite_temperature_excitation",
            "0",
            0.0,
            0.004,
            -100.0,
            rates=QutritDissipationRates(
                "direct_rates", 0.5, 0.12, 0.9, 0.18, 0.0
            ),
        ),
        QutritQutipCase(
            "pure_number_noise_dephasing",
            "0",
            math.pi / 2,
            0.004,
            -100.0,
            rates=QutritDissipationRates(
                "direct_rates", 0.0, 0.0, 0.0, 0.0, 0.4
            ),
        ),
        QutritQutipCase(
            "pulse_followed_by_idle",
            "0",
            math.pi / 2,
            0.004,
            -100.0,
            detuning_rad_per_us=0.2 * math.pi,
            idle_duration_us=0.02,
            rates=QutritDissipationRates(
                "direct_rates", 0.3, 0.04, 0.6, 0.06, 0.1
            ),
        ),
        QutritQutipCase(
            "drag_beta_zero",
            "0",
            math.pi / 2,
            0.002,
            -100.0,
            drag_beta_us=0.0,
            rates=zero,
        ),
        QutritQutipCase(
            "drag_nonzero_both_quadratures",
            "0",
            math.pi / 2,
            0.002,
            -100.0,
            phase_rad=0.35,
            drag_beta_us=0.001,
            rates=QutritDissipationRates(
                "direct_rates", 0.2, 0.02, 0.4, 0.03, 0.08
            ),
        ),
    )


def run_qutrit_qutip_comparison() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not QUTIP_AVAILABLE:
        raise RuntimeError("QuTiP is unavailable in this validation environment")
    reports = []
    rows: list[dict[str, Any]] = []
    for case in qutrit_qutip_cases():
        report, case_rows = _run_case(case)
        reports.append(report)
        rows.extend(case_rows)
    maxima = {
        key: max(row[key] for row in rows)
        for key in (
            "max_element_difference",
            "frobenius_difference",
            "trace_distance",
            "population_0_error",
            "population_1_error",
            "population_2_error",
            "leakage_error",
            "purity_difference",
        )
    }
    return {
        "validation_id": "pulse_b_qutip_qutrit",
        "pass": all(report["pass"] for report in reports),
        "tolerance": QUTRIT_QUTIP_TOLERANCE,
        "basis_order": list(QUTRIT_BASIS_LABELS),
        "subsystem_dimensions": list(QUTRIT_QUTIP_SUBSYSTEM_DIMENSIONS),
        "matrix_shape": [3, 3],
        "comparison_rule": "identical rho0, H(t), collapse matrices, and times",
        "solver_options": dict(DEFAULT_OPTIONS),
        "software_versions": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "qutip": qutip.__version__,
        },
        "cases": reports,
        "maximum_errors": maxima,
        "scope_limitations": [
            "This validates shared equations and numerical implementation.",
            "It does not validate physical-input mapping or real hardware.",
        ],
    }, rows


def _run_case(
    case: QutritQutipCase,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    envelope = GaussianPulseEnvelope.from_target_rotation_angle(
        case.target_angle_rad,
        case.sigma_us,
        4.0,
    )
    alpha = transmon_anharmonicity_rad_per_us(case.anharmonicity_mhz)
    total_duration = envelope.duration_us + case.idle_duration_us
    policy = recommended_qutrit_step_policy(
        envelope,
        case.detuning_rad_per_us,
        alpha,
        case.rates,
        total_duration,
        drag_beta_us=case.drag_beta_us,
    )
    pulse_times = uniform_times(
        envelope.duration_us, QUTRIT_QUTIP_CHECKPOINT_COUNT
    )
    idle_times = (
        uniform_times(case.idle_duration_us, QUTRIT_QUTIP_CHECKPOINT_COUNT)
        if case.idle_duration_us > 0.0
        else ()
    )
    initial = qutrit_initial_density_matrix(case.initial_state)
    quanta = evolve_open_qutrit_sequence(
        initial,
        envelope,
        alpha,
        case.rates,
        total_duration,
        policy.selected_internal_step_cap_us,
        phase_rad=case.phase_rad,
        detuning_rad_per_us=case.detuning_rad_per_us,
        drag_beta_us=case.drag_beta_us,
        pulse_checkpoint_times_us=pulse_times,
        idle_checkpoint_times_us=idle_times,
    )
    collapse_ops = qutrit_collapse_operator_matrices(case.rates)
    recorded_pulse_times = tuple(
        point.time_us for point in quanta.pulse_result.checkpoints
    )
    qutip_pulse = run_qutip_time_dependent_segment(
        initial,
        QutritPulseHamiltonian(
            envelope,
            alpha,
            case.phase_rad,
            case.detuning_rad_per_us,
            case.drag_beta_us,
        ),
        collapse_ops,
        None,
        envelope.duration_us,
        recorded_pulse_times,
        max_step_us=policy.selected_internal_step_cap_us / 2.0,
        subsystem_dimensions=QUTRIT_QUTIP_SUBSYSTEM_DIMENSIONS,
    )
    rows = _comparison_rows(
        case.name,
        "pulse",
        recorded_pulse_times,
        [point.cleaned_state for point in quanta.pulse_result.checkpoints],
        qutip_pulse,
    )
    if case.idle_duration_us > 0.0:
        assert quanta.idle_result is not None
        recorded_idle_times = tuple(
            point.time_us for point in quanta.idle_result.checkpoints
        )
        qutip_idle = run_qutip_constant_segment(
            qutip_pulse[-1],
            qutrit_rotating_frame_hamiltonian(
                case.detuning_rad_per_us, alpha, 0.0, 0.0
            ),
            collapse_ops,
            None,
            case.idle_duration_us,
            recorded_idle_times,
            max_step_us=policy.selected_internal_step_cap_us / 2.0,
            subsystem_dimensions=QUTRIT_QUTIP_SUBSYSTEM_DIMENSIONS,
        )
        rows.extend(_comparison_rows(
            case.name,
            "idle",
            tuple(
                envelope.duration_us + point.time_us
                for point in quanta.idle_result.checkpoints
            ),
            [point.cleaned_state for point in quanta.idle_result.checkpoints],
            qutip_idle,
        ))
    maximum_error = max(row["max_element_difference"] for row in rows)
    return {
        "name": case.name,
        "initial_state": case.initial_state,
        "anharmonicity_mhz": case.anharmonicity_mhz,
        "drag_beta_us": case.drag_beta_us,
        "rates": case.rates.to_dict(),
        "step_policy": policy.to_dict(),
        "checkpoint_count": len(rows),
        "maximum_density_matrix_element_error": maximum_error,
        "pass": maximum_error <= QUTRIT_QUTIP_TOLERANCE,
    }, rows


def _comparison_rows(
    case_name: str,
    segment: str,
    times: tuple[float, ...],
    quanta_states: list[Matrix],
    qutip_states: list[Matrix],
) -> list[dict[str, Any]]:
    rows = []
    for time_us, quanta_state, qutip_state in zip(
        times, quanta_states, qutip_states, strict=True
    ):
        metrics = compare_density_matrices(quanta_state, qutip_state)
        diagonal = [
            abs(quanta_state[index][index].real - qutip_state[index][index].real)
            for index in range(3)
        ]
        rows.append({
            "case": case_name,
            "segment": segment,
            "time_us": time_us,
            **metrics,
            "population_0_error": diagonal[0],
            "population_1_error": diagonal[1],
            "population_2_error": diagonal[2],
            "leakage_error": diagonal[2],
        })
    return rows

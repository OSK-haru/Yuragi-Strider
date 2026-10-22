"""Reproducible qutrit open-system validation for Pulse Extension B-2."""

from __future__ import annotations

import math
from time import perf_counter
from types import SimpleNamespace

from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT
from core.gates import Matrix, density_from_ket
from core.pulse_envelopes import SquarePulseEnvelope
from core.pulse_qutrit import qutrit_initial_density_matrix
from core.pulse_qutrit_contract import (
    transmon_anharmonicity_rad_per_us,
)
from core.pulse_qutrit_open_system import (
    OpenQutritSequenceResult,
    QutritDissipationRates,
    evolve_open_qutrit_sequence,
    qutrit_dissipation_rates,
    qutrit_gibbs_populations,
)
from validation_pulse.time_grid import uniform_times


RATE_TOLERANCE = 2e-14
POPULATION_TOLERANCE = 5e-7
COHERENCE_TOLERANCE = 5e-7
MODE_MATCH_TOLERANCE = 1e-14
PHYSICALITY_TOLERANCE = 1e-10
MINIMUM_VISIBLE_EFFECT = 1e-4


def run_qutrit_dissipation_validation(
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Run the fixed B-2 analytic validation set."""

    started = perf_counter()
    rows: list[dict[str, object]] = []
    rate_conventions = _rate_convention_case()
    cascade = _cascade_case(rows)
    dephasing = _pure_dephasing_case(rows)
    outflow = _population_outflow_case(rows)
    gibbs = _gibbs_case(rows)
    drive_idle = _drive_idle_case(rows)
    mode_match = _mode_match_case()
    cases = (
        rate_conventions,
        cascade,
        dephasing,
        outflow,
        gibbs,
        drive_idle,
        mode_match,
    )
    overall_pass = all(bool(case["pass"]) for case in cases)
    for row in rows:
        row["result"] = "pass" if overall_pass else "fail"

    return {
        "validation": "PULSE-B2-QUTRIT-DISSIPATION",
        "model_id": "driven_transmon_qutrit_rwa_experimental_v1",
        "contract_version": "pulse-extension-b-v1",
        "capability_status": "contract_only",
        "frame": "rotating",
        "approximation": "RWA",
        "basis_order": ["0", "1", "2"],
        "subsystem_dimensions": [3],
        "collapse_operator_convention": {
            "gamma_10_down": "sqrt(gamma_10_down) |0><1|",
            "gamma_01_up": "sqrt(gamma_01_up) |1><0|",
            "gamma_21_down": "sqrt(gamma_21_down) |1><2|",
            "gamma_12_up": "sqrt(gamma_12_up) |2><1|",
            "pure_dephasing": "sqrt(2 gamma_phi_adjacent) n",
        },
        "physical_profile_convention": {
            "gamma_21_zero_temperature": (
                "2 * gamma_10_zero_temperature"
            ),
            "meaning": (
                "educational harmonic-matrix-element approximation"
            ),
        },
        "tolerances": {
            "rate": RATE_TOLERANCE,
            "population": POPULATION_TOLERANCE,
            "coherence": COHERENCE_TOLERANCE,
            "mode_match": MODE_MATCH_TOLERANCE,
            "raw_physicality": PHYSICALITY_TOLERANCE,
            "minimum_visible_segment_effect": MINIMUM_VISIBLE_EFFECT,
        },
        "cases": list(cases),
        "overall_pass": overall_pass,
        "runtime_ms": (perf_counter() - started) * 1000.0,
        "scope_and_limitations": {
            "proves": [
                "zero-temperature qutrit upward rates vanish",
                "both adjacent transitions satisfy detailed balance",
                "zero-temperature two-step cascade matches its analytic solution",
                "number-operator dephasing gives adjacent rates gamma and 0-2 rate 4 gamma",
                "population outflow gives the documented coherence decay",
                "no-drive long-time populations approach the three-level Gibbs state",
                "dissipation acts during both pulse and post-pulse idle",
                "equivalent physical and direct rates produce the same trajectory",
                "raw physicality is acceptable at the declared validation steps",
            ],
            "does_not_prove": [
                "a production-safe qutrit step policy",
                "strict finite-step CPTP behavior",
                "DRAG behavior",
                "QuTiP qutrit agreement",
                "public qutrit API readiness",
                "hardware-calibrated transition rates",
            ],
        },
    }, rows


def _rate_convention_case() -> dict[str, object]:
    zero = qutrit_dissipation_rates(
        _physical_environment(temperature_mk=0.0),
        -250.0,
    )
    finite_temperature_mk = 120.0
    finite = qutrit_dissipation_rates(
        _physical_environment(temperature_mk=finite_temperature_mk),
        -250.0,
    )
    assert finite.transition_01_frequency_ghz is not None
    assert finite.transition_12_frequency_ghz is not None
    expected_ratio_01 = _boltzmann_factor(
        finite_temperature_mk,
        finite.transition_01_frequency_ghz,
    )
    expected_ratio_12 = _boltzmann_factor(
        finite_temperature_mk,
        finite.transition_12_frequency_ghz,
    )
    ratio_01 = finite.gamma_01_up_per_us / finite.gamma_10_down_per_us
    ratio_12 = finite.gamma_12_up_per_us / finite.gamma_21_down_per_us
    ratio_error_01 = abs(ratio_01 - expected_ratio_01)
    ratio_error_12 = abs(ratio_12 - expected_ratio_12)
    harmonic_error = abs(
        zero.gamma_21_zero_temperature_per_us
        - 2.0 * zero.gamma_10_zero_temperature_per_us
    )
    return {
        "name": "zero_temperature_and_detailed_balance",
        "zero_temperature_rates": zero.to_dict(),
        "finite_temperature_rates": finite.to_dict(),
        "detailed_balance_ratio_01": ratio_01,
        "detailed_balance_ratio_12": ratio_12,
        "detailed_balance_error_01": ratio_error_01,
        "detailed_balance_error_12": ratio_error_12,
        "gamma_21_harmonic_factor_error": harmonic_error,
        "pass": (
            zero.gamma_01_up_per_us == 0.0
            and zero.gamma_12_up_per_us == 0.0
            and ratio_error_01 <= RATE_TOLERANCE
            and ratio_error_12 <= RATE_TOLERANCE
            and harmonic_error <= RATE_TOLERANCE
        ),
    }


def _cascade_case(
    rows: list[dict[str, object]],
) -> dict[str, object]:
    gamma_10 = 0.3
    gamma_21 = 0.8
    duration = 3.0
    result = evolve_open_qutrit_sequence(
        qutrit_initial_density_matrix("2"),
        SquarePulseEnvelope(0.0, duration),
        -1.0,
        _direct_rates(
            gamma_10_down_per_us=gamma_10,
            gamma_21_down_per_us=gamma_21,
        ),
        duration,
        0.002,
        pulse_checkpoint_times_us=uniform_times(duration, 61),
    )
    maximum_error = 0.0
    for checkpoint in result.pulse_result.checkpoints:
        time_us = checkpoint.time_us
        expected_2 = math.exp(-gamma_21 * time_us)
        expected_1 = gamma_21 / (gamma_10 - gamma_21) * (
            math.exp(-gamma_21 * time_us)
            - math.exp(-gamma_10 * time_us)
        )
        expected = (1.0 - expected_1 - expected_2, expected_1, expected_2)
        maximum_error = max(
            maximum_error,
            max(
                abs(checkpoint.cleaned_state[level][level].real - expected[level])
                for level in range(3)
            ),
        )
        _append_row(
            rows,
            "zero_temperature_cascade",
            "pulse",
            time_us,
            checkpoint.cleaned_state,
            expected_populations=expected,
            raw_trace_error=checkpoint.raw_physicality.trace_error,
            raw_hermiticity_error=(
                checkpoint.raw_physicality.hermiticity_error
            ),
            raw_minimum_eigenvalue=(
                checkpoint.raw_physicality.minimum_eigenvalue
            ),
            cleanup_correction_norm=checkpoint.cleanup_correction_norm,
        )
    physicality = _physicality_summary(result)
    return {
        "name": "zero_temperature_cascade",
        "gamma_10_down_per_us": gamma_10,
        "gamma_21_down_per_us": gamma_21,
        "maximum_population_error": maximum_error,
        "physicality": physicality,
        "pass": (
            maximum_error <= POPULATION_TOLERANCE
            and _physicality_passes(physicality)
        ),
    }


def _pure_dephasing_case(
    rows: list[dict[str, object]],
) -> dict[str, object]:
    inverse_sqrt_three = 1.0 / math.sqrt(3.0)
    initial = density_from_ket((
        inverse_sqrt_three,
        inverse_sqrt_three,
        inverse_sqrt_three,
    ))
    gamma_phi = 0.7
    duration = 0.5
    result = evolve_open_qutrit_sequence(
        initial,
        SquarePulseEnvelope(0.0, duration),
        -1.0,
        _direct_rates(gamma_phi_adjacent_per_us=gamma_phi),
        duration,
        0.001,
        pulse_checkpoint_times_us=uniform_times(duration, 51),
    )
    errors = {"rho_01": 0.0, "rho_12": 0.0, "rho_02": 0.0}
    for checkpoint in result.pulse_result.checkpoints:
        time_us = checkpoint.time_us
        expected_by_pair = {
            (0, 1): abs(initial[0][1]) * math.exp(-gamma_phi * time_us),
            (1, 2): abs(initial[1][2]) * math.exp(-gamma_phi * time_us),
            (0, 2): abs(initial[0][2]) * math.exp(
                -4.0 * gamma_phi * time_us
            ),
        }
        for pair, name in (
            ((0, 1), "rho_01"),
            ((1, 2), "rho_12"),
            ((0, 2), "rho_02"),
        ):
            errors[name] = max(
                errors[name],
                abs(
                    abs(checkpoint.cleaned_state[pair[0]][pair[1]])
                    - expected_by_pair[pair]
                ),
            )
        _append_row(
            rows,
            "pure_dephasing_one_one_four",
            "pulse",
            time_us,
            checkpoint.cleaned_state,
            expected_coherence_01=expected_by_pair[(0, 1)],
            expected_coherence_12=expected_by_pair[(1, 2)],
            expected_coherence_02=expected_by_pair[(0, 2)],
            raw_trace_error=checkpoint.raw_physicality.trace_error,
            raw_hermiticity_error=(
                checkpoint.raw_physicality.hermiticity_error
            ),
            raw_minimum_eigenvalue=(
                checkpoint.raw_physicality.minimum_eigenvalue
            ),
            cleanup_correction_norm=checkpoint.cleanup_correction_norm,
        )
    physicality = _physicality_summary(result)
    return {
        "name": "pure_dephasing_one_one_four",
        "gamma_phi_adjacent_per_us": gamma_phi,
        "maximum_coherence_errors": errors,
        "physicality": physicality,
        "pass": (
            max(errors.values()) <= COHERENCE_TOLERANCE
            and _physicality_passes(physicality)
        ),
    }


def _population_outflow_case(
    rows: list[dict[str, object]],
) -> dict[str, object]:
    inverse_sqrt_two = 1.0 / math.sqrt(2.0)
    initial = density_from_ket((
        inverse_sqrt_two,
        inverse_sqrt_two,
        0.0 + 0.0j,
    ))
    rates = _direct_rates(
        gamma_10_down_per_us=0.2,
        gamma_01_up_per_us=0.1,
        gamma_21_down_per_us=0.4,
        gamma_12_up_per_us=0.3,
    )
    decay_rate = rates.population_induced_coherence_decay_per_us(0, 1)
    duration = 0.7
    result = evolve_open_qutrit_sequence(
        initial,
        SquarePulseEnvelope(0.0, duration),
        -1.0,
        rates,
        duration,
        0.001,
        pulse_checkpoint_times_us=uniform_times(duration, 51),
    )
    maximum_error = 0.0
    for checkpoint in result.pulse_result.checkpoints:
        expected = (
            abs(initial[0][1])
            * math.exp(-decay_rate * checkpoint.time_us)
        )
        maximum_error = max(
            maximum_error,
            abs(abs(checkpoint.cleaned_state[0][1]) - expected),
        )
        _append_row(
            rows,
            "population_outflow_coherence",
            "pulse",
            checkpoint.time_us,
            checkpoint.cleaned_state,
            expected_coherence_01=expected,
            raw_trace_error=checkpoint.raw_physicality.trace_error,
            raw_hermiticity_error=(
                checkpoint.raw_physicality.hermiticity_error
            ),
            raw_minimum_eigenvalue=(
                checkpoint.raw_physicality.minimum_eigenvalue
            ),
            cleanup_correction_norm=checkpoint.cleanup_correction_norm,
        )
    physicality = _physicality_summary(result)
    return {
        "name": "population_outflow_coherence",
        "predicted_decay_rate_per_us": decay_rate,
        "maximum_coherence_error": maximum_error,
        "physicality": physicality,
        "pass": (
            maximum_error <= COHERENCE_TOLERANCE
            and _physicality_passes(physicality)
        ),
    }


def _gibbs_case(
    rows: list[dict[str, object]],
) -> dict[str, object]:
    temperature_mk = 300.0
    rates = qutrit_dissipation_rates(
        _physical_environment(
            temperature_mk=temperature_mk,
            device_quality=0.0,
        ),
        -250.0,
    )
    assert rates.transition_01_frequency_ghz is not None
    assert rates.transition_12_frequency_ghz is not None
    expected = qutrit_gibbs_populations(
        temperature_mk,
        rates.transition_01_frequency_ghz,
        rates.transition_12_frequency_ghz,
    )
    pulse_duration = 0.01
    total_duration = 12.0
    result = evolve_open_qutrit_sequence(
        qutrit_initial_density_matrix("2"),
        SquarePulseEnvelope(0.0, pulse_duration),
        transmon_anharmonicity_rad_per_us(-250.0),
        rates,
        total_duration,
        0.005,
        pulse_checkpoint_times_us=(0.0, pulse_duration),
        idle_checkpoint_times_us=uniform_times(
            total_duration - pulse_duration,
            101,
        ),
    )
    _append_result_rows(
        rows,
        "three_level_gibbs",
        result,
        expected_populations=expected,
    )
    final_populations = tuple(
        result.final_state[level][level].real for level in range(3)
    )
    maximum_error = max(
        abs(actual - target)
        for actual, target in zip(
            final_populations,
            expected,
            strict=True,
        )
    )
    physicality = _physicality_summary(result)
    return {
        "name": "three_level_gibbs",
        "temperature_mk": temperature_mk,
        "rates": rates.to_dict(),
        "expected_populations": list(expected),
        "final_populations": list(final_populations),
        "maximum_population_error": maximum_error,
        "physicality": physicality,
        "pass": (
            maximum_error <= POPULATION_TOLERANCE
            and _physicality_passes(physicality)
        ),
    }


def _drive_idle_case(
    rows: list[dict[str, object]],
) -> dict[str, object]:
    rates = _direct_rates(
        gamma_10_down_per_us=0.4,
        gamma_01_up_per_us=0.2,
        gamma_21_down_per_us=0.8,
        gamma_12_up_per_us=0.1,
    )
    pulse_duration = 0.2
    result = evolve_open_qutrit_sequence(
        qutrit_initial_density_matrix("0"),
        SquarePulseEnvelope.from_target_rotation_angle(
            math.pi / 2.0,
            pulse_duration,
        ),
        -1.0,
        rates,
        1.0,
        0.001,
        pulse_checkpoint_times_us=uniform_times(pulse_duration, 41),
        idle_checkpoint_times_us=uniform_times(0.8, 81),
    )
    _append_result_rows(rows, "dissipative_pulse_and_idle", result)
    initial = qutrit_initial_density_matrix("0")
    pulse_change = _matrix_max_error(result.pulse_end_state, initial)
    idle_change = _matrix_max_error(
        result.final_state,
        result.pulse_end_state,
    )
    physicality = _physicality_summary(result)
    return {
        "name": "dissipative_pulse_and_idle",
        "pulse_state_change": pulse_change,
        "idle_state_change": idle_change,
        "physicality": physicality,
        "pass": (
            pulse_change >= MINIMUM_VISIBLE_EFFECT
            and idle_change >= MINIMUM_VISIBLE_EFFECT
            and _physicality_passes(physicality)
        ),
    }


def _mode_match_case() -> dict[str, object]:
    physical_rates = qutrit_dissipation_rates(
        _physical_environment(temperature_mk=100.0),
        -250.0,
    )
    direct_rates = _direct_rates(
        gamma_10_down_per_us=physical_rates.gamma_10_down_per_us,
        gamma_01_up_per_us=physical_rates.gamma_01_up_per_us,
        gamma_21_down_per_us=physical_rates.gamma_21_down_per_us,
        gamma_12_up_per_us=physical_rates.gamma_12_up_per_us,
        gamma_phi_adjacent_per_us=(
            physical_rates.gamma_phi_adjacent_per_us
        ),
    )
    envelope = SquarePulseEnvelope(0.0, 0.05)
    initial = qutrit_initial_density_matrix("2")
    alpha = transmon_anharmonicity_rad_per_us(-250.0)
    physical = evolve_open_qutrit_sequence(
        initial,
        envelope,
        alpha,
        physical_rates,
        0.5,
        0.001,
    )
    direct = evolve_open_qutrit_sequence(
        initial,
        envelope,
        alpha,
        direct_rates,
        0.5,
        0.001,
    )
    pulse_error = _matrix_max_error(
        physical.pulse_end_state,
        direct.pulse_end_state,
    )
    final_error = _matrix_max_error(
        physical.final_state,
        direct.final_state,
    )
    return {
        "name": "physical_direct_rate_equivalence",
        "physical_rates": physical_rates.to_dict(),
        "direct_rates": direct_rates.to_dict(),
        "pulse_end_max_element_error": pulse_error,
        "final_max_element_error": final_error,
        "pass": max(pulse_error, final_error) <= MODE_MATCH_TOLERANCE,
    }


def _append_result_rows(
    rows: list[dict[str, object]],
    case_name: str,
    result: OpenQutritSequenceResult,
    *,
    expected_populations: tuple[float, float, float] | None = None,
) -> None:
    for segment, offset, evolution in (
        ("pulse", 0.0, result.pulse_result),
        ("idle", result.pulse_duration_us, result.idle_result),
    ):
        if evolution is None:
            continue
        for checkpoint in evolution.checkpoints:
            if segment == "idle" and checkpoint.time_us == 0.0:
                continue
            _append_row(
                rows,
                case_name,
                segment,
                offset + checkpoint.time_us,
                checkpoint.cleaned_state,
                expected_populations=expected_populations,
                raw_trace_error=checkpoint.raw_physicality.trace_error,
                raw_hermiticity_error=(
                    checkpoint.raw_physicality.hermiticity_error
                ),
                raw_minimum_eigenvalue=(
                    checkpoint.raw_physicality.minimum_eigenvalue
                ),
                cleanup_correction_norm=checkpoint.cleanup_correction_norm,
            )


def _append_row(
    rows: list[dict[str, object]],
    case_name: str,
    segment: str,
    time_us: float,
    state: Matrix,
    *,
    expected_populations: tuple[float, float, float] | None = None,
    expected_coherence_01: float | None = None,
    expected_coherence_12: float | None = None,
    expected_coherence_02: float | None = None,
    raw_trace_error: float = 0.0,
    raw_hermiticity_error: float = 0.0,
    raw_minimum_eigenvalue: float = 0.0,
    cleanup_correction_norm: float = 0.0,
) -> None:
    populations = tuple(state[index][index].real for index in range(3))
    expected = expected_populations or (None, None, None)
    rows.append({
        "case": case_name,
        "time_us": time_us,
        "segment": segment,
        "population_0": populations[0],
        "population_1": populations[1],
        "population_2": populations[2],
        "coherence_01_abs": abs(state[0][1]),
        "coherence_12_abs": abs(state[1][2]),
        "coherence_02_abs": abs(state[0][2]),
        "expected_population_0": expected[0],
        "expected_population_1": expected[1],
        "expected_population_2": expected[2],
        "expected_coherence_01_abs": expected_coherence_01,
        "expected_coherence_12_abs": expected_coherence_12,
        "expected_coherence_02_abs": expected_coherence_02,
        "population_sum_error": abs(sum(populations) - 1.0),
        "raw_trace_error": raw_trace_error,
        "raw_hermiticity_error": raw_hermiticity_error,
        "raw_minimum_eigenvalue": raw_minimum_eigenvalue,
        "cleanup_correction_norm": cleanup_correction_norm,
        "result": "pass",
    })


def _physicality_summary(
    result: OpenQutritSequenceResult,
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
    )


def _direct_rates(
    *,
    gamma_10_down_per_us: float = 0.0,
    gamma_01_up_per_us: float = 0.0,
    gamma_21_down_per_us: float = 0.0,
    gamma_12_up_per_us: float = 0.0,
    gamma_phi_adjacent_per_us: float = 0.0,
) -> QutritDissipationRates:
    return QutritDissipationRates(
        "direct_rates",
        gamma_10_down_per_us,
        gamma_01_up_per_us,
        gamma_21_down_per_us,
        gamma_12_up_per_us,
        gamma_phi_adjacent_per_us,
    )


def _physical_environment(
    *,
    temperature_mk: float,
    device_quality: float = 0.8,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_mode="physical",
        device_quality=device_quality,
        temperature_mk=temperature_mk,
        flux_noise_phi0=1e-6,
        qubit_frequency_ghz=5.0,
        t1_max_us=100.0,
        tphi_max_us=100.0,
        ideal_reference=False,
    )


def _boltzmann_factor(
    temperature_mk: float,
    frequency_ghz: float,
) -> float:
    return math.exp(
        -PLANCK_CONSTANT * frequency_ghz * 1e9
        / (BOLTZMANN_CONSTANT * temperature_mk * 1e-3)
    )


def _matrix_max_error(actual: Matrix, expected: Matrix) -> float:
    return max(
        abs(actual[row][column] - expected[row][column])
        for row in range(len(expected))
        for column in range(len(expected))
    )

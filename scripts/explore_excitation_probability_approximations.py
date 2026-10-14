"""Explore fast target P(1) estimators under residual ZZ and local noise.

Absolute tolerance is 0.01 probability (one percentage point). Parameters
are synthetic stress values, not hardware calibration. The conditional-block
candidate is valid only when the spectator has no transverse drive or exchange
coupling; such inputs must use a coupled solver instead. No runtime mode is
enabled by this experiment.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
from statistics import median
import sys
from time import perf_counter
from typing import Callable

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.experimental_isolation_physics import (  # noqa: E402
    ExtendedTransmonNetworkHamiltonian,
    ResidualZZCoupling,
)
from core.pulse_envelopes import SquarePulseEnvelope  # noqa: E402
from core.pulse_qutrit_open_system import QutritDissipationRates  # noqa: E402
from core.pulse_transmon_network import (  # noqa: E402
    CoupledTransmonNetworkHamiltonian,
    ScheduledTransmonDrive,
    network_collapse_operator_matrices,
)


TARGET_ZERO = np.array(((1.0, 0.0), (0.0, 0.0)), dtype=np.complex128)
SPECTATOR_STATES = {
    "zero": np.array(((1.0, 0.0), (0.0, 0.0)), dtype=np.complex128),
    "one": np.array(((0.0, 0.0), (0.0, 1.0)), dtype=np.complex128),
    "plus": np.full((2, 2), 0.5, dtype=np.complex128),
    "mixed_30pct_one": np.diag((0.7, 0.3)).astype(np.complex128),
}
TOLERANCE = 0.01


@dataclass(frozen=True)
class Case:
    zz_rad_per_us: float
    target_drive_rad_per_us: float
    spectator_drive_rad_per_us: float
    spectator_state: str
    noise_scale: float
    pulse_us: float

    @property
    def conditional_blocks_supported(self) -> bool:
        return self.spectator_drive_rad_per_us == 0.0

    def to_dict(self) -> dict[str, float | str | bool]:
        return {
            "zz_rad_per_us": self.zz_rad_per_us,
            "target_drive_rad_per_us": self.target_drive_rad_per_us,
            "spectator_drive_rad_per_us": self.spectator_drive_rad_per_us,
            "spectator_state": self.spectator_state,
            "noise_scale": self.noise_scale,
            "pulse_us": self.pulse_us,
            "conditional_blocks_supported": self.conditional_blocks_supported,
        }


@dataclass(frozen=True)
class PreparedCase:
    case: Case
    segments: tuple[tuple[float, float, float, float, np.ndarray], ...]
    jumps: tuple[np.ndarray, ...]
    relaxation: np.ndarray
    target_rates: tuple[float, float, float]
    spectator_rates: tuple[float, float, float]
    initial_joint: np.ndarray
    initial_mean_field: np.ndarray
    initial_blocks: np.ndarray


def _rates(case: Case) -> tuple[QutritDissipationRates, QutritDissipationRates]:
    values = []
    for down, up, phi in ((0.20, 0.01, 0.05), (0.35, 0.02, 0.08)):
        values.append(QutritDissipationRates(
            input_mode="direct_rates",
            gamma_10_down_per_us=down * case.noise_scale,
            gamma_01_up_per_us=up * case.noise_scale,
            gamma_21_down_per_us=0.0,
            gamma_12_up_per_us=0.0,
            gamma_phi_adjacent_per_us=phi * case.noise_scale,
        ))
    return values[0], values[1]


def prepare_case(case: Case) -> PreparedCase:
    if case.spectator_state not in SPECTATOR_STATES:
        raise ValueError("unknown spectator state")
    if any(not math.isfinite(value) or value < 0.0 for value in (
        case.zz_rad_per_us,
        case.target_drive_rad_per_us,
        case.spectator_drive_rad_per_us,
        case.noise_scale,
        case.pulse_us,
    )) or case.pulse_us <= 0.0:
        raise ValueError("case parameters must be finite, non-negative, and pulse_us positive")
    pre_us = 0.04
    post_us = 0.06
    drives = tuple(
        ScheduledTransmonDrive(
            target=index,
            start_time_us=pre_us,
            envelope=SquarePulseEnvelope(amplitude, case.pulse_us),
        )
        for index, amplitude in enumerate((case.target_drive_rad_per_us, case.spectator_drive_rad_per_us))
        if amplitude > 0.0
    )
    base = CoupledTransmonNetworkHamiltonian(
        anharmonicities_rad_per_us=(0.0, 0.0),
        detunings_rad_per_us=(0.0, 0.0),
        couplings=(),
        drives=drives,
        local_dimension=2,
    )
    coupled = ExtendedTransmonNetworkHamiltonian(
        base,
        residual_zz=(ResidualZZCoupling(0, 1, case.zz_rad_per_us),) if case.zz_rad_per_us else (),
    )
    boundaries = (0.0, pre_us, pre_us + case.pulse_us, pre_us + case.pulse_us + post_us)
    segments = []
    for start, end in zip(boundaries[:-1], boundaries[1:], strict=True):
        hamiltonian = coupled.for_segment(start, end).evaluate_array((start + end) / 2.0)
        pulse_on = start >= pre_us and end <= pre_us + case.pulse_us
        segments.append((
            start,
            end,
            case.target_drive_rad_per_us if pulse_on else 0.0,
            case.spectator_drive_rad_per_us if pulse_on else 0.0,
            hamiltonian,
        ))
    rates = _rates(case)
    jumps = tuple(np.asarray(operator, dtype=np.complex128) for operator in network_collapse_operator_matrices(rates, 2))
    relaxation = sum((jump.conj().T @ jump for jump in jumps), np.zeros((4, 4), dtype=np.complex128))
    spectator = SPECTATOR_STATES[case.spectator_state]
    p_one_b = float(np.real(spectator[1, 1]))
    initial_mean_field = np.array((0.0, 0.0, 0.0, p_one_b, float(spectator[0, 1].real), float(spectator[0, 1].imag)), dtype=np.float64)
    initial_blocks = np.array((1.0 - p_one_b, 0.0, 0.0, 0.0, p_one_b, 0.0, 0.0, 0.0), dtype=np.float64)
    return PreparedCase(
        case=case,
        segments=tuple(segments),
        jumps=jumps,
        relaxation=relaxation,
        target_rates=(rates[0].gamma_10_down_per_us, rates[0].gamma_01_up_per_us, rates[0].gamma_phi_adjacent_per_us),
        spectator_rates=(rates[1].gamma_10_down_per_us, rates[1].gamma_01_up_per_us, rates[1].gamma_phi_adjacent_per_us),
        initial_joint=np.kron(TARGET_ZERO, spectator),
        initial_mean_field=initial_mean_field,
        initial_blocks=initial_blocks,
    )


def _rk4(value: np.ndarray, step: float, derivative: Callable[[np.ndarray], np.ndarray]) -> np.ndarray:
    k1 = derivative(value)
    k2 = derivative(value + step * k1 / 2.0)
    k3 = derivative(value + step * k2 / 2.0)
    k4 = derivative(value + step * k3)
    return value + step * (k1 + 2.0 * k2 + 2.0 * k3 + k4) / 6.0


def _exact_rhs(rho: np.ndarray, hamiltonian: np.ndarray, prepared: PreparedCase) -> np.ndarray:
    relaxation = prepared.relaxation
    result = -1j * (hamiltonian @ rho - rho @ hamiltonian)
    result -= 0.5 * (relaxation @ rho + rho @ relaxation)
    for jump in prepared.jumps:
        result += jump @ rho @ jump.conj().T
    return result


def _single_rhs(
    state: np.ndarray,
    drive: float,
    detuning: float,
    rates: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Local Lindblad RHS for (P1, Re rho01, Im rho01)."""
    down, up, phi = rates
    population, coherence_re, coherence_im = state
    gamma_two = 0.5 * (down + up) + phi
    return (
        drive * coherence_im - down * population + up * (1.0 - population),
        -detuning * coherence_im - gamma_two * coherence_re,
        0.5 * drive * (1.0 - 2.0 * population) + detuning * coherence_re - gamma_two * coherence_im,
    )


def _mean_field_rhs(state: np.ndarray, drive_a: float, drive_b: float, prepared: PreparedCase) -> np.ndarray:
    zz = prepared.case.zz_rad_per_us
    first = _single_rhs(state[:3], drive_a, zz * state[3], prepared.target_rates)
    second = _single_rhs(state[3:], drive_b, zz * state[0], prepared.spectator_rates)
    return np.array((*first, *second), dtype=np.float64)


def _conditional_blocks_rhs(state: np.ndarray, drive_a: float, prepared: PreparedCase) -> np.ndarray:
    """Evolve the two unnormalized target blocks conditioned on spectator 0/1.

    Each block is (weight, P1, Re rho01, Im rho01). The spectator's
    off-diagonal blocks cannot feed these when it has no transverse drive or
    exchange edge; its T1/up jumps only transfer weight between the blocks.
    """
    down_a, up_a, phi_a = prepared.target_rates
    down_b, up_b, _ = prepared.spectator_rates
    gamma_two = 0.5 * (down_a + up_a) + phi_a
    blocks = state.reshape((2, 4))
    derivative = np.zeros((2, 4), dtype=np.float64)
    for spectator_level in (0, 1):
        weight, population, coherence_re, coherence_im = blocks[spectator_level]
        detuning = prepared.case.zz_rad_per_us * spectator_level
        derivative[spectator_level, 1] = drive_a * coherence_im - down_a * population + up_a * (weight - population)
        derivative[spectator_level, 2] = -detuning * coherence_im - gamma_two * coherence_re
        derivative[spectator_level, 3] = 0.5 * drive_a * (weight - 2.0 * population) + detuning * coherence_re - gamma_two * coherence_im
    derivative[0] += down_b * blocks[1] - up_b * blocks[0]
    derivative[1] += up_b * blocks[0] - down_b * blocks[1]
    return derivative.reshape(-1)


def solve_case(prepared: PreparedCase, method: str, max_step_us: float = 0.004) -> tuple[float, float]:
    if not math.isfinite(max_step_us) or max_step_us <= 0.0:
        raise ValueError("max_step_us must be positive and finite")
    if method == "conditional_blocks" and not prepared.case.conditional_blocks_supported:
        raise ValueError("conditional blocks require an undriven spectator")
    if method == "exact":
        state = prepared.initial_joint.copy()
    elif method == "mean_field":
        state = prepared.initial_mean_field.copy()
    elif method == "conditional_blocks":
        state = prepared.initial_blocks.copy()
    else:
        raise ValueError("unknown method")
    at_pulse_end = 0.0
    for segment_index, (start, end, drive_a, drive_b, hamiltonian) in enumerate(prepared.segments):
        steps = math.ceil((end - start) / max_step_us)
        step = (end - start) / steps
        if method == "exact":
            derivative = lambda value: _exact_rhs(value, hamiltonian, prepared)
        elif method == "mean_field":
            derivative = lambda value: _mean_field_rhs(value, drive_a, drive_b, prepared)
        else:
            derivative = lambda value: _conditional_blocks_rhs(value, drive_a, prepared)
        for _ in range(steps):
            state = _rk4(state, step, derivative)
        if segment_index == 1:
            at_pulse_end = _target_p_one(state, method)
    return at_pulse_end, _target_p_one(state, method)


def _target_p_one(state: np.ndarray, method: str) -> float:
    if method == "exact":
        return float(np.real(state[2, 2] + state[3, 3]))
    if method == "mean_field":
        return float(state[0])
    return float(state[1] + state[5])


def _case_grid() -> tuple[Case, ...]:
    cases = []
    for zz in (0.0, 2.0, 8.0):
        for target_drive in (6.0, 20.0):
            for spectator_state in SPECTATOR_STATES:
                for noise_scale in (0.0, 1.0):
                    for pulse_us in (0.06, 0.12):
                        cases.append(Case(zz, target_drive, 0.0, spectator_state, noise_scale, pulse_us))
    for zz in (2.0, 8.0):
        for target_drive in (6.0, 20.0):
            for spectator_drive in (8.0, 20.0):
                for spectator_state in ("zero", "plus"):
                    for pulse_us in (0.06, 0.12):
                        cases.append(Case(zz, target_drive, spectator_drive, spectator_state, 1.0, pulse_us))
    return tuple(cases)


def _holdout_cases(*, seed: int = 20260930, count: int = 96) -> tuple[Case, ...]:
    rng = np.random.default_rng(seed)
    names = tuple(SPECTATOR_STATES)
    return tuple(Case(
        zz_rad_per_us=float(rng.uniform(0.0, 12.0)),
        target_drive_rad_per_us=float(rng.uniform(2.0, 24.0)),
        spectator_drive_rad_per_us=0.0 if index % 2 == 0 else float(rng.uniform(2.0, 24.0)),
        spectator_state=names[int(rng.integers(len(names)))],
        noise_scale=float(rng.uniform(0.0, 4.0)),
        pulse_us=float(rng.uniform(0.02, 0.16)),
    ) for index in range(count))


def _stress_cases() -> tuple[Case, ...]:
    return tuple(
        Case(
            zz_rad_per_us=zz,
            target_drive_rad_per_us=target_drive,
            spectator_drive_rad_per_us=spectator_drive,
            spectator_state=state,
            noise_scale=noise,
            pulse_us=phase_product / zz,
        )
        for zz in (8.0, 20.0)
        for target_drive in (20.0, 40.0)
        for spectator_drive in (8.0, 20.0, 40.0)
        for state in ("zero", "plus")
        for noise in (0.0, 1.0)
        for phase_product in (0.5, 0.7)
    )


def _summarize(rows: list[dict[str, object]], method: str) -> dict[str, object]:
    applicable = [row for row in rows if row[method] is not None]
    errors = [max(abs(a - b) for a, b in zip(row["exact"], row[method], strict=True)) for row in applicable]
    worst_index = int(np.argmax(errors))
    return {
        "applicable_cases": len(applicable),
        "within_one_percentage_point_cases": sum(error <= TOLERANCE for error in errors),
        "maximum_absolute_excitation_error": max(errors),
        "median_absolute_excitation_error": median(errors),
        "worst_case": applicable[worst_index]["case"],
        "worst_case_exact_p_one_at_pulse_end_and_final": applicable[worst_index]["exact"],
        "worst_case_candidate_p_one_at_pulse_end_and_final": applicable[worst_index][method],
        "aggregate_solver_seconds": sum(row[method + "_seconds"] for row in applicable),
        "median_solver_seconds_per_case": median(row[method + "_seconds"] for row in applicable),
        "exact_to_candidate_aggregate_speed_ratio": sum(row["exact_seconds"] for row in applicable) / sum(row[method + "_seconds"] for row in applicable),
    }


def _evaluate_cases(cases: tuple[Case, ...], max_step_us: float, timing_repetitions: int) -> list[dict[str, object]]:
    if timing_repetitions < 1:
        raise ValueError("timing_repetitions must be positive")
    rows = []
    for case in cases:
        prepared = prepare_case(case)
        row: dict[str, object] = {"case": case.to_dict()}
        for method in ("exact", "mean_field", "conditional_blocks"):
            if method == "conditional_blocks" and not case.conditional_blocks_supported:
                row[method] = None
                continue
            durations = []
            for _ in range(timing_repetitions):
                started = perf_counter()
                row[method] = solve_case(prepared, method, max_step_us)
                durations.append(perf_counter() - started)
            row[method + "_seconds"] = median(durations)
        rows.append(row)
    return rows


def _screen_summary(rows: list[dict[str, object]], threshold: float) -> dict[str, object]:
    """Evaluate an empirical screen, not an upper bound on approximation error."""
    accepted = [
        row for row in rows
        if not row["case"]["conditional_blocks_supported"]
        and row["case"]["zz_rad_per_us"] * row["case"]["pulse_us"] <= threshold
    ]
    if not accepted:
        return {"phase_product_threshold": threshold, "accepted_driven_cases": 0}
    errors = [
        max(abs(a - b) for a, b in zip(row["exact"], row["mean_field"], strict=True))
        for row in accepted
    ]
    return {
        "phase_product_threshold": threshold,
        "accepted_driven_cases": len(accepted),
        "accepted_cases_above_one_percentage_point": sum(error > TOLERANCE for error in errors),
        "maximum_accepted_error": max(errors),
    }


def run_experiment(*, max_step_us: float = 0.004, include_cases: bool = False, timing_repetitions: int = 3) -> dict[str, object]:
    rows = _evaluate_cases(_case_grid(), max_step_us, timing_repetitions)
    holdout_rows = _evaluate_cases(_holdout_cases(), max_step_us, timing_repetitions)
    stress_rows = _evaluate_cases(_stress_cases(), max_step_us, 1)
    undriven = [row for row in rows if row["case"]["conditional_blocks_supported"]]
    driven = [row for row in rows if not row["case"]["conditional_blocks_supported"]]
    holdout_undriven = [row for row in holdout_rows if row["case"]["conditional_blocks_supported"]]
    holdout_driven = [row for row in holdout_rows if not row["case"]["conditional_blocks_supported"]]
    result: dict[str, object] = {
        "metric": "absolute_excitation_probability_error_at_pulse_end_or_final",
        "tolerance": TOLERANCE,
        "cases_total": len(rows),
        "conditional_domain": {
            "cases": len(undriven),
            "mean_field": _summarize(undriven, "mean_field"),
            "conditional_blocks": _summarize(undriven, "conditional_blocks"),
        },
        "spectator_driven_domain": {
            "cases": len(driven),
            "mean_field": _summarize(driven, "mean_field"),
            "conditional_blocks": "unsupported; use a coupled solver",
        },
        "holdout": {
            "seed": 20260930,
            "cases": len(holdout_rows),
            "undriven_conditional_blocks": _summarize(holdout_undriven, "conditional_blocks"),
            "driven_mean_field": _summarize(holdout_driven, "mean_field"),
            "driven_mean_field_phase_screens": [
                _screen_summary(holdout_rows, threshold) for threshold in (0.25, 0.50, 0.75)
            ],
        },
        "boundary_stress": {
            "cases": len(stress_rows),
            "driven_mean_field": _summarize(stress_rows, "mean_field"),
            "driven_mean_field_phase_screens": [
                _screen_summary(stress_rows, threshold) for threshold in (0.50, 0.75)
            ],
        },
        "timing_scope": "solver only, prepared Hamiltonians/jumps excluded; Python process, median of repeated runs per case",
        "timing_repetitions": timing_repetitions,
    }
    if include_cases:
        result["cases"] = rows
        result["holdout_cases"] = holdout_rows
        result["boundary_stress_cases"] = stress_rows
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-step-us", type=float, default=0.004)
    parser.add_argument("--include-cases", action="store_true")
    parser.add_argument("--timing-repetitions", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(run_experiment(max_step_us=args.max_step_us, include_cases=args.include_cases, timing_repetitions=args.timing_repetitions), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

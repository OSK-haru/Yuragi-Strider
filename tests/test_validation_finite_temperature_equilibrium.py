"""Direct-rate and physical-input checks for finite-temperature equilibrium."""

from __future__ import annotations

import math
import unittest

from core.backend_boundary import PYTHON_DENSE_BACKEND
from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT
from core.gates import (
    SIGMA_MINUS,
    SIGMA_PLUS,
    Z,
    initial_density_matrix,
    multi_qubit_physical_collapse_operators,
    prepare_collapse_operators,
    scale,
    trace,
    zero_hamiltonian,
)
from core.physical_environment import compute_environment_rates
from core.results import EnvironmentConfig
from core.simulator import _KernelStats, _evolve_stable


DIRECT_RATE_CASES = (
    {"name": "V5-1", "gamma_down_per_us": 0.012, "gamma_up_per_us": 0.002},
    {"name": "V5-2", "gamma_down_per_us": 0.020, "gamma_up_per_us": 0.010},
    {"name": "V5-3", "gamma_down_per_us": 0.051, "gamma_up_per_us": 0.049},
)
PHYSICAL_INPUT_CASES = (
    {"name": "P5-1", "temperature_mk": 50.0},
    {"name": "P5-2", "temperature_mk": 100.0},
    {"name": "P5-3", "temperature_mk": 200.0},
)
INITIAL_STATES = ("0", "1", "mixed")
SAMPLE_MULTIPLIERS = (0.0, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 10.0, 12.0)
NORMAL_INTERNAL_STEP_US = 0.5
MEDIUM_INTERNAL_STEP_US = 0.25
FINE_INTERNAL_STEP_US = 0.125

MAX_ABS_ERROR_P1 = 1e-6
RMSE_P1 = 1e-7
FINAL_EQUILIBRIUM_ERROR = 1e-5
MAX_RELATIVE_FIT_ERROR = 1e-4
MAX_PAIRWISE_FINAL_DIFFERENCE = 1e-5
MAX_TRACE_ERROR = 1e-10
MAX_HERMITICITY_ERROR = 1e-10
MINIMUM_EIGENVALUE = -1e-10
MAX_STEP_REFINEMENT_DIFFERENCE = 1e-7
DETAILED_BALANCE_TOLERANCE = 1e-10


def equilibrium_p1(gamma_down_per_us: float, gamma_up_per_us: float) -> float:
    return gamma_up_per_us / (gamma_down_per_us + gamma_up_per_us)


def case_times_us(gamma_down_per_us: float, gamma_up_per_us: float) -> tuple[float, ...]:
    t1_effective_us = 1.0 / (gamma_down_per_us + gamma_up_per_us)
    return tuple(multiplier * t1_effective_us for multiplier in SAMPLE_MULTIPLIERS)


def run_population_case(
    gamma_down_per_us: float,
    gamma_up_per_us: float,
    initial_state: str,
    times_us: tuple[float, ...],
    *,
    gamma_phi_per_us: float = 0.0,
    integration_step_us: float = NORMAL_INTERNAL_STEP_US,
) -> list[dict[str, float | str]]:
    """Evolve one initial population through the production Lindblad solver."""

    collapse_ops = prepare_collapse_operators(
        multi_qubit_physical_collapse_operators(
            1,
            gamma_down_per_us,
            gamma_up_per_us,
            gamma_phi_per_us,
        )
    )
    state = _initial_density(initial_state)
    hamiltonian = zero_hamiltonian(2)
    kernel_stats = _KernelStats(PYTHON_DENSE_BACKEND)
    max_rate = gamma_down_per_us + gamma_up_per_us + gamma_phi_per_us
    rows: list[dict[str, float | str]] = []

    for index, requested_time_us in enumerate(times_us):
        if index > 0:
            duration_us = requested_time_us - times_us[index - 1]
            segment_count = max(1, math.ceil(duration_us / integration_step_us))
            segment_duration_us = duration_us / segment_count
            for _ in range(segment_count):
                state = _evolve_stable(
                    state,
                    hamiltonian,
                    collapse_ops,
                    segment_duration_us,
                    max_rate,
                    kernel_stats,
                    blocked_by_sampling=False,
                )
        rows.append(_snapshot_metrics(
            state,
            requested_time_us,
            initial_state,
            gamma_down_per_us,
            gamma_up_per_us,
        ))
    return rows


def summarize_case(
    rows: list[dict[str, float | str]],
    gamma_down_per_us: float,
    gamma_up_per_us: float,
) -> dict[str, float]:
    p1_eq = equilibrium_p1(gamma_down_per_us, gamma_up_per_us)
    fit_equilibrium, fit_rate = _fit_population_parameters(rows)
    p1_errors = [float(row["absolute_error_p1"]) for row in rows]
    p0_errors = [float(row["absolute_error_p0"]) for row in rows]
    return {
        "max_abs_error_p1": max(p1_errors),
        "rmse_p1": math.sqrt(sum(error * error for error in p1_errors) / len(rows)),
        "max_abs_error_p0": max(p0_errors),
        "final_equilibrium_error_p1": abs(float(rows[-1]["simulated_p1"]) - p1_eq),
        "max_trace_error": max(float(row["trace_error"]) for row in rows),
        "max_hermiticity_error": max(float(row["hermiticity_error"]) for row in rows),
        "minimum_density_eigenvalue": min(float(row["minimum_eigenvalue"]) for row in rows),
        "fitted_equilibrium_p1": fit_equilibrium,
        "fitted_population_relaxation_rate_per_us": fit_rate,
        "relative_equilibrium_fit_error": abs(fit_equilibrium - p1_eq) / max(p1_eq, 1e-12),
        "relative_rate_fit_error": abs(fit_rate - (gamma_down_per_us + gamma_up_per_us))
        / (gamma_down_per_us + gamma_up_per_us),
    }


def validate_case(rows: list[dict[str, float | str]], summary: dict[str, float]) -> None:
    assert all(row["requested_time_us"] == row["time_us"] for row in rows)
    assert all(
        math.isfinite(float(value))
        for row in rows
        for key, value in row.items()
        if key != "initial_state"
    )
    assert all(0.0 <= float(row["simulated_p0"]) <= 1.0 for row in rows)
    assert all(0.0 <= float(row["simulated_p1"]) <= 1.0 for row in rows)
    assert all(abs(float(row["simulated_p0"]) + float(row["simulated_p1"]) - 1.0) <= MAX_TRACE_ERROR for row in rows)
    assert summary["max_abs_error_p1"] <= MAX_ABS_ERROR_P1
    assert summary["rmse_p1"] <= RMSE_P1
    assert summary["max_abs_error_p0"] <= MAX_ABS_ERROR_P1
    assert summary["final_equilibrium_error_p1"] <= FINAL_EQUILIBRIUM_ERROR
    assert summary["max_trace_error"] <= MAX_TRACE_ERROR
    assert summary["max_hermiticity_error"] <= MAX_HERMITICITY_ERROR
    assert summary["minimum_density_eigenvalue"] >= MINIMUM_EIGENVALUE
    assert summary["relative_equilibrium_fit_error"] <= MAX_RELATIVE_FIT_ERROR
    assert summary["relative_rate_fit_error"] <= MAX_RELATIVE_FIT_ERROR


def physical_environment(temperature_mk: float) -> EnvironmentConfig:
    return EnvironmentConfig(
        t1_zero_temperature_us=100.0,
        tphi_zero_temperature_us=100.0,
        temperature_mk=temperature_mk,
        qubit_frequency_ghz=5.0,
        ideal_reference=False,
    )


class FiniteTemperatureEquilibriumTest(unittest.TestCase):
    def test_direct_rate_transients_and_equilibrium_for_all_initial_states(self) -> None:
        for case in DIRECT_RATE_CASES:
            down = float(case["gamma_down_per_us"])
            up = float(case["gamma_up_per_us"])
            times = case_times_us(down, up)
            for initial_state in INITIAL_STATES:
                with self.subTest(case=case["name"], initial_state=initial_state):
                    rows = run_population_case(down, up, initial_state, times)
                    validate_case(rows, summarize_case(rows, down, up))
                    _assert_monotonic_toward_equilibrium(self, rows, equilibrium_p1(down, up))

    def test_initial_populations_reach_the_same_equilibrium(self) -> None:
        for case in DIRECT_RATE_CASES:
            down = float(case["gamma_down_per_us"])
            up = float(case["gamma_up_per_us"])
            finals = [
                float(run_population_case(down, up, state, case_times_us(down, up))[-1]["simulated_p1"])
                for state in INITIAL_STATES
            ]
            with self.subTest(case=case["name"]):
                self.assertLessEqual(max(finals) - min(finals), MAX_PAIRWISE_FINAL_DIFFERENCE)

    def test_direct_rate_collapse_operators_are_individual_up_and_down_rates(self) -> None:
        down, up = 0.020, 0.010
        operators = multi_qubit_physical_collapse_operators(1, down, up, 0.0)

        self.assertEqual(
            operators,
            [scale(math.sqrt(down), SIGMA_MINUS), scale(math.sqrt(up), SIGMA_PLUS)],
        )
        self.assertNotIn(scale(math.sqrt(down + up), SIGMA_MINUS), operators)
        self.assertNotIn(scale(math.sqrt(down + up), SIGMA_PLUS), operators)
        self.assertNotIn(scale(math.sqrt((down + up) / 2.0), Z), operators)

    def test_physical_input_rates_and_equilibrium_match_gibbs_ratio(self) -> None:
        for case in PHYSICAL_INPUT_CASES:
            environment = physical_environment(float(case["temperature_mk"]))
            rates = compute_environment_rates(environment)
            beta_delta_e = PLANCK_CONSTANT * 5.0e9 / (BOLTZMANN_CONSTANT * environment.temperature_mk * 1e-3)
            boltzmann_ratio = math.exp(-beta_delta_e)
            p1_gibbs = boltzmann_ratio / (1.0 + boltzmann_ratio)
            p1_rates = equilibrium_p1(rates.gamma_down_per_us, rates.gamma_up_per_us)
            rows = run_population_case(
                rates.gamma_down_per_us,
                rates.gamma_up_per_us,
                "1",
                case_times_us(rates.gamma_down_per_us, rates.gamma_up_per_us),
                gamma_phi_per_us=rates.gamma_phi_per_us,
            )
            summary = summarize_case(rows, rates.gamma_down_per_us, rates.gamma_up_per_us)
            with self.subTest(case=case["name"]):
                validate_case(rows, summary)
                self.assertAlmostEqual(rates.gamma_up_per_us / rates.gamma_down_per_us, boltzmann_ratio, delta=DETAILED_BALANCE_TOLERANCE)
                self.assertAlmostEqual(p1_rates, p1_gibbs, delta=DETAILED_BALANCE_TOLERANCE)
                self.assertAlmostEqual(float(rows[-1]["simulated_p1"]), p1_gibbs, delta=FINAL_EQUILIBRIUM_ERROR)

    def test_refinement_is_stable_for_direct_and_physical_cases(self) -> None:
        direct = DIRECT_RATE_CASES[1]
        direct_audit = _refinement_difference(
            float(direct["gamma_down_per_us"]),
            float(direct["gamma_up_per_us"]),
            "1",
        )
        rates = compute_environment_rates(physical_environment(100.0))
        physical_audit = _refinement_difference(
            rates.gamma_down_per_us,
            rates.gamma_up_per_us,
            "1",
            gamma_phi_per_us=rates.gamma_phi_per_us,
        )
        self.assertLessEqual(direct_audit, MAX_STEP_REFINEMENT_DIFFERENCE)
        self.assertLessEqual(physical_audit, MAX_STEP_REFINEMENT_DIFFERENCE)

    def test_repeated_direct_rate_evaluation_is_deterministic(self) -> None:
        down, up = 0.020, 0.010
        times = case_times_us(down, up)
        first = run_population_case(down, up, "1", times)
        second = run_population_case(down, up, "1", times)

        self.assertEqual(first, second)


def _initial_density(initial_state: str):
    if initial_state in {"0", "1"}:
        return initial_density_matrix([initial_state])
    if initial_state == "mixed":
        return [[0.5 + 0.0j, 0.0j], [0.0j, 0.5 + 0.0j]]
    raise ValueError(f"unsupported initial state: {initial_state}")


def _snapshot_metrics(state, time_us: float, initial_state: str, down: float, up: float) -> dict[str, float | str]:
    p1_initial = 1.0 if initial_state == "1" else 0.0 if initial_state == "0" else 0.5
    total_rate = down + up
    p1_eq = equilibrium_p1(down, up)
    analytic_p1 = p1_eq + (p1_initial - p1_eq) * math.exp(-total_rate * time_us)
    simulated_p0 = float(state[0][0].real)
    simulated_p1 = float(state[1][1].real)
    eigenvalues = _two_by_two_eigenvalues(state)
    return {
        "time_us": time_us,
        "requested_time_us": time_us,
        "t_over_t1_effective": total_rate * time_us,
        "initial_state": initial_state,
        "simulated_p0": simulated_p0,
        "simulated_p1": simulated_p1,
        "analytic_p0": 1.0 - analytic_p1,
        "analytic_p1": analytic_p1,
        "absolute_error_p0": abs(simulated_p0 - (1.0 - analytic_p1)),
        "absolute_error_p1": abs(simulated_p1 - analytic_p1),
        "relative_error_p1": abs(simulated_p1 - analytic_p1) / analytic_p1 if analytic_p1 > 1e-14 else 0.0,
        "rho01_abs": abs(state[0][1]),
        "rho10_abs": abs(state[1][0]),
        "trace_error": abs(trace(state) - 1.0),
        "hermiticity_error": max(abs(state[row][column] - state[column][row].conjugate()) for row in range(2) for column in range(2)),
        "minimum_eigenvalue": min(eigenvalues),
        "purity": float(sum(abs(state[row][column]) ** 2 for row in range(2) for column in range(2))),
    }


def _fit_population_parameters(rows: list[dict[str, float | str]]) -> tuple[float, float]:
    first, second, third = rows[:3]
    y0, y1, y2 = (float(row["simulated_p1"]) for row in (first, second, third))
    dt = float(second["time_us"]) - float(first["time_us"])
    ratio = (y2 - y1) / (y1 - y0)
    fitted_rate = -math.log(ratio) / dt
    fitted_equilibrium = (y1 - ratio * y0) / (1.0 - ratio)
    return fitted_equilibrium, fitted_rate


def _refinement_difference(down: float, up: float, initial_state: str, *, gamma_phi_per_us: float = 0.0) -> float:
    times = case_times_us(down, up)
    medium = run_population_case(down, up, initial_state, times, gamma_phi_per_us=gamma_phi_per_us, integration_step_us=MEDIUM_INTERNAL_STEP_US)
    fine = run_population_case(down, up, initial_state, times, gamma_phi_per_us=gamma_phi_per_us, integration_step_us=FINE_INTERNAL_STEP_US)
    return max(
        max(abs(float(left[key]) - float(right[key])) for key in ("simulated_p0", "simulated_p1", "rho01_abs", "rho10_abs"))
        for left, right in zip(medium, fine)
    )


def _assert_monotonic_toward_equilibrium(test: unittest.TestCase, rows, p1_eq: float) -> None:
    values = [float(row["simulated_p1"]) for row in rows]
    if values[0] > p1_eq + 1e-12:
        test.assertTrue(all(left >= right - 1e-12 for left, right in zip(values, values[1:])))
    elif values[0] < p1_eq - 1e-12:
        test.assertTrue(all(left <= right + 1e-12 for left, right in zip(values, values[1:])))


def _two_by_two_eigenvalues(matrix) -> tuple[float, float]:
    center = 0.5 * (matrix[0][0].real + matrix[1][1].real)
    radius = math.sqrt((0.5 * (matrix[0][0].real - matrix[1][1].real)) ** 2 + abs(matrix[0][1]) ** 2)
    return center - radius, center + radius


if __name__ == "__main__":
    unittest.main()

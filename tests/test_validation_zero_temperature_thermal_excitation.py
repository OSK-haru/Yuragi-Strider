from __future__ import annotations

import math
import unittest

from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT
from core.gates import (
    SIGMA_MINUS,
    multi_qubit_environment_collapse_operators,
)
from core.physical_environment import compute_environment_rates
from core.results import EnvironmentConfig


RATE_TOLERANCE = 1e-12
DETAILED_BALANCE_ABS_TOLERANCE = 1e-12
DETAILED_BALANCE_REL_TOLERANCE = 1e-10


class ZeroTemperatureThermalExcitationTest(unittest.TestCase):
    def test_exact_zero_temperature_has_no_thermal_excitation(self) -> None:
        rates = compute_environment_rates(_environment(temperature_mk=0.0))
        gamma0 = rates.gamma0_per_us

        self.assertEqual(rates.n_th, 0.0)
        self.assertEqual(rates.gamma_up_per_us, 0.0)
        self.assertEqual(rates.gamma_down_per_us, gamma0)
        self.assertAlmostEqual(rates.t1_effective_us, 1.0 / gamma0)
        _assert_finite_nonnegative(self, rates)

    def test_zero_temperature_is_independent_of_frequency(self) -> None:
        for frequency_ghz in (1.0, 5.0, 10.0):
            with self.subTest(frequency_ghz=frequency_ghz):
                rates = compute_environment_rates(
                    _environment(temperature_mk=0.0, frequency_ghz=frequency_ghz)
                )
                self.assertEqual(rates.n_th, 0.0)
                self.assertEqual(rates.gamma_up_per_us, 0.0)
                self.assertEqual(
                    rates.gamma_down_per_us,
                    rates.gamma0_per_us,
                )

    def test_very_low_positive_temperature_is_safe_and_approaches_zero(self) -> None:
        values = []
        for temperature_mk in (1e-9, 1e-6, 0.001):
            with self.subTest(temperature_mk=temperature_mk):
                rates = compute_environment_rates(
                    _environment(temperature_mk=temperature_mk)
                )
                _assert_finite_nonnegative(self, rates)
                values.append(rates.n_th)

        self.assertEqual(values, sorted(values))
        self.assertLessEqual(values[-1], values[0] + 1e-12)

    def test_temperature_monotonicity(self) -> None:
        rates = [
            compute_environment_rates(_environment(temperature_mk=temperature_mk))
            for temperature_mk in (0.0, 1.0, 10.0, 20.0, 100.0, 1000.0)
        ]

        self.assertEqual([rate.n_th for rate in rates], sorted(rate.n_th for rate in rates))
        self.assertEqual(
            [rate.gamma_up_per_us for rate in rates],
            sorted(rate.gamma_up_per_us for rate in rates),
        )
        self.assertEqual(
            [rate.gamma_down_per_us for rate in rates],
            sorted(rate.gamma_down_per_us for rate in rates),
        )

    def test_frequency_monotonicity_at_fixed_positive_temperature(self) -> None:
        rates = [
            compute_environment_rates(
                _environment(temperature_mk=100.0, frequency_ghz=frequency_ghz)
            )
            for frequency_ghz in (1.0, 3.0, 5.0, 10.0)
        ]

        self.assertEqual(
            [rate.n_th for rate in rates],
            sorted((rate.n_th for rate in rates), reverse=True),
        )
        self.assertEqual(
            [rate.gamma_up_per_us for rate in rates],
            sorted((rate.gamma_up_per_us for rate in rates), reverse=True),
        )

    def test_detailed_balance_matches_independent_analytic_formula(self) -> None:
        for temperature_mk, frequency_ghz in (
            (20.0, 10.0),
            (50.0, 5.0),
            (100.0, 1.0),
            (200.0, 5.0),
        ):
            with self.subTest(temperature_mk=temperature_mk, frequency_ghz=frequency_ghz):
                rates = compute_environment_rates(
                    _environment(
                        temperature_mk=temperature_mk,
                        frequency_ghz=frequency_ghz,
                    )
                )
                expected_n_th = analytic_thermal_occupation(
                    temperature_mk,
                    frequency_ghz,
                )
                expected_ratio = math.exp(
                    -PLANCK_CONSTANT * frequency_ghz * 1e9
                    / (BOLTZMANN_CONSTANT * temperature_mk * 1e-3)
                )
                actual_ratio = rates.gamma_up_per_us / rates.gamma_down_per_us

                self.assertAlmostEqual(rates.n_th, expected_n_th, delta=1e-12)
                self.assertLessEqual(
                    abs(actual_ratio - expected_ratio),
                    DETAILED_BALANCE_ABS_TOLERANCE
                    + DETAILED_BALANCE_REL_TOLERANCE * abs(expected_ratio),
                )
                self.assertAlmostEqual(
                    rates.gamma_up_per_us / rates.gamma_down_per_us,
                    rates.n_th / (rates.n_th + 1.0),
                    delta=1e-12,
                )

    def test_zero_temperature_collapse_operators_contain_relaxation_only(self) -> None:
        rates = compute_environment_rates(_environment(temperature_mk=0.0))
        operators = multi_qubit_environment_collapse_operators(1, rates)

        self.assertGreater(rates.gamma_down_per_us, 0.0)
        self.assertEqual(rates.gamma_up_per_us, 0.0)
        self.assertGreaterEqual(len(operators), 1)
        expected_relaxation = tuple(
            tuple(
                math.sqrt(rates.gamma_down_per_us) * value
                for value in row
            )
            for row in SIGMA_MINUS
        )
        self.assertEqual(operators[0], expected_relaxation)
        self.assertEqual(len(operators), 2)  # relaxation plus pure dephasing

    def test_ideal_reference_is_distinct_from_physical_zero_temperature(self) -> None:
        physical_zero = compute_environment_rates(_environment(temperature_mk=0.0))
        ideal = compute_environment_rates(
            _environment(temperature_mk=0.0, ideal_reference=True)
        )

        self.assertGreater(physical_zero.gamma_down_per_us, 0.0)
        self.assertEqual(physical_zero.gamma_up_per_us, 0.0)
        self.assertEqual(ideal.n_th, 0.0)
        self.assertEqual(ideal.gamma_down_per_us, 0.0)
        self.assertEqual(ideal.gamma_up_per_us, 0.0)
        self.assertEqual(ideal.gamma_phi_per_us, 0.0)

    def test_large_exponent_is_safe_and_repeated_results_are_deterministic(self) -> None:
        environment = _environment(temperature_mk=1e-12, frequency_ghz=10.0)
        first = compute_environment_rates(environment)
        second = compute_environment_rates(environment)

        self.assertEqual(first, second)
        _assert_finite_nonnegative(self, first)
        self.assertEqual(first.n_th, 0.0)

    def test_t1_uses_total_upward_and_downward_rate(self) -> None:
        rates = compute_environment_rates(
            _environment(temperature_mk=200.0, frequency_ghz=5.0)
        )
        expected = 1.0 / (rates.gamma_down_per_us + rates.gamma_up_per_us)

        self.assertEqual(
            rates.gamma_population_relaxation_per_us,
            rates.gamma_down_per_us + rates.gamma_up_per_us,
        )
        self.assertAlmostEqual(rates.t1_effective_us, expected, delta=RATE_TOLERANCE)


def analytic_thermal_occupation(temperature_mk: float, frequency_ghz: float) -> float:
    temperature_k = temperature_mk * 1e-3
    frequency_hz = frequency_ghz * 1e9
    exponent = PLANCK_CONSTANT * frequency_hz / (BOLTZMANN_CONSTANT * temperature_k)
    if exponent > 700.0:
        return 0.0
    return 1.0 / math.expm1(exponent)


def _environment(
    *,
    temperature_mk: float,
    frequency_ghz: float = 5.0,
    ideal_reference: bool = False,
) -> EnvironmentConfig:
    return EnvironmentConfig(
        t1_zero_temperature_us=100.0,
        tphi_zero_temperature_us=100.0,
        temperature_mk=temperature_mk,
        qubit_frequency_ghz=frequency_ghz,
        ideal_reference=ideal_reference,
    )


def _assert_finite_nonnegative(test: unittest.TestCase, rates) -> None:
    values = [
        rates.n_th,
        rates.gamma_down_per_us,
        rates.gamma_up_per_us,
        rates.gamma_phi_per_us,
        rates.t1_zero_temperature_us,
        rates.tphi_zero_temperature_us,
        rates.t1_effective_us,
        rates.t2_effective_us,
    ]
    for value in values:
        test.assertTrue(math.isfinite(value), msg=f"non-finite rate value: {value}")
        test.assertGreaterEqual(value, 0.0)


if __name__ == "__main__":
    unittest.main()

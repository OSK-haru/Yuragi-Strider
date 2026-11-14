from __future__ import annotations

import math
import unittest

from core.physical_environment import (
    compute_environment_rates,
    environment_rates_to_derived_parameters,
)
from core.results import EnvironmentConfig


class RateVariableNamingRefactorTest(unittest.TestCase):
    def test_zero_temperature_canonical_rates(self) -> None:
        rates = compute_environment_rates(_environment(temperature_mk=0.0))

        self.assertEqual(rates.gamma_up_per_us, 0.0)
        self.assertEqual(rates.gamma_down_per_us, rates.gamma0_per_us)
        self.assertEqual(
            rates.gamma_population_relaxation_per_us,
            rates.gamma0_per_us,
        )
        self.assertAlmostEqual(
            rates.t1_effective_us,
            1.0 / rates.gamma_population_relaxation_per_us,
        )
        self.assertAlmostEqual(rates.t1_effective_us, rates.t1_zero_temperature_us)

    def test_finite_temperature_population_rate_is_not_downward_rate(self) -> None:
        rates = compute_environment_rates(_environment(temperature_mk=200.0))

        self.assertGreater(rates.gamma_up_per_us, 0.0)
        self.assertAlmostEqual(
            rates.gamma_population_relaxation_per_us,
            rates.gamma_down_per_us + rates.gamma_up_per_us,
        )
        self.assertGreater(
            rates.gamma_population_relaxation_per_us,
            rates.gamma_down_per_us,
        )
        self.assertAlmostEqual(
            rates.t1_effective_us,
            1.0 / rates.gamma_population_relaxation_per_us,
        )

    def test_ideal_reference_has_zero_rates_and_infinite_times(self) -> None:
        rates = compute_environment_rates(
            _environment(temperature_mk=0.0, ideal_reference=True)
        )

        self.assertEqual(rates.gamma0_per_us, 0.0)
        self.assertEqual(rates.gamma_population_relaxation_per_us, 0.0)
        self.assertEqual(rates.gamma_down_per_us, 0.0)
        self.assertEqual(rates.gamma_up_per_us, 0.0)
        self.assertTrue(math.isinf(rates.t1_effective_us))
        self.assertTrue(math.isinf(rates.tphi_us))

    def test_legacy_aliases_are_gone(self) -> None:
        rates = compute_environment_rates(_environment(temperature_mk=200.0))
        derived = environment_rates_to_derived_parameters(rates)

        for name in ("gamma1_per_us", "gammaphi_per_us", "t1_us", "t2_us", "t1_base_us", "tphi_base_us"):
            self.assertNotIn(name, derived)
        self.assertFalse(hasattr(rates, "gamma1_per_us"))


def _environment(*, temperature_mk: float, ideal_reference: bool = False) -> EnvironmentConfig:
    return EnvironmentConfig(
        t1_zero_temperature_us=100.0,
        tphi_us=100.0,
        temperature_mk=temperature_mk,
        qubit_frequency_ghz=5.0,
        ideal_reference=ideal_reference,
    )


if __name__ == "__main__":
    unittest.main()

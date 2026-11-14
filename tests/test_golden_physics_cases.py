import unittest

from tests.physics_test_helpers import (
    make_bell_config,
    make_ideal_environment,
    make_one_qubit_h_config,
    make_physical_environment,
    run_and_reconstruct,
)


class GoldenPhysicsCasesTest(unittest.TestCase):
    def test_one_qubit_h_ideal_golden_case(self) -> None:
        result, _ = run_and_reconstruct(
            make_one_qubit_h_config(make_ideal_environment())
        )

        self.assertAlmostEqual(result.output_probabilities["0"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["1"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.purity[-1], 1.0, delta=1e-10)

    def test_two_qubit_bell_ideal_golden_case(self) -> None:
        result, _ = run_and_reconstruct(make_bell_config(make_ideal_environment()))

        self.assertAlmostEqual(result.output_probabilities["00"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["11"], 0.5, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["01"], 0.0, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["10"], 0.0, delta=1e-10)
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.purity[-1], 1.0, delta=1e-10)

    def test_bell_with_finite_profile_rates_decoheres_at_zero_temperature_and_flux(self) -> None:
        result, _ = run_and_reconstruct(
            make_bell_config(
                make_physical_environment(t1_zero_temperature_us=20.0, tphi_zero_temperature_us=20.0, temperature_mk=0.0),
                duration_us=50.0,
                time_steps=101,
            )
        )

        self.assertGreater(result.derived_parameters["gamma_down_per_us"], 0.0)
        self.assertGreater(result.derived_parameters["gamma_phi_per_us"], 0.0)
        self.assertLess(result.fidelity[-1], 1.0)
        self.assertLess(result.purity[-1], 1.0)

    def test_hot_bell_case_has_excitation_and_lower_purity_than_ideal(self) -> None:
        ideal = run_and_reconstruct(make_bell_config(make_ideal_environment()))[0]
        hot = run_and_reconstruct(
            make_bell_config(
                make_physical_environment(t1_zero_temperature_us=20.0, tphi_zero_temperature_us=20.0, temperature_mk=500.0),
                duration_us=50.0,
                time_steps=101,
            )
        )[0]

        self.assertGreater(hot.derived_parameters["gamma_up_per_us"], 0.0)
        self.assertLess(hot.purity[-1], ideal.purity[-1])


if __name__ == "__main__":
    unittest.main()

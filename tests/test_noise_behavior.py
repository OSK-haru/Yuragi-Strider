import unittest

from tests.physics_test_helpers import (
    FIDELITY_PURITY_TOL,
    coherence_abs,
    make_initial_state_config,
    make_one_qubit_h_config,
    make_physical_environment,
    run_and_reconstruct,
)


class NoiseBehaviorTest(unittest.TestCase):
    def test_pure_dephasing_reduces_plus_state_coherence_and_purity(self) -> None:
        environment = make_physical_environment(t1_zero_temperature_us=1e9, tphi_zero_temperature_us=10.0, temperature_mk=0.0)
        result, density = run_and_reconstruct(
            make_initial_state_config(
                "+",
                environment=environment,
                duration_us=50.0,
                time_steps=101,
            )
        )

        self.assertLess(coherence_abs(density), 0.5)
        self.assertLess(result.purity[-1], 1.0 - FIDELITY_PURITY_TOL)

    def test_relaxation_from_one_increases_ground_population(self) -> None:
        environment = make_physical_environment(t1_zero_temperature_us=20.0, tphi_zero_temperature_us=999999999.9999999, temperature_mk=0.0)
        result, _ = run_and_reconstruct(
            make_initial_state_config(
                "1",
                environment=environment,
                duration_us=50.0,
                time_steps=101,
            )
        )

        self.assertGreater(result.output_probabilities["0"], 0.5)
        self.assertLess(result.output_probabilities["1"], 0.5)

    def test_finite_temperature_adds_excitation_and_mixing(self) -> None:
        environment = make_physical_environment(
            t1_zero_temperature_us=20.0,
            tphi_zero_temperature_us=999999999.9999999,
            temperature_mk=500.0,
        )
        result, _ = run_and_reconstruct(
            make_initial_state_config(
                "0",
                environment=environment,
                duration_us=50.0,
                time_steps=101,
            )
        )

        self.assertGreater(result.derived_parameters["gamma_up_per_us"], 0.0)
        self.assertGreater(result.output_probabilities["1"], 0.0)
        self.assertLess(result.purity[-1], 1.0)

    def test_higher_noise_does_not_improve_fidelity_or_effective_time(self) -> None:
        low_noise = run_and_reconstruct(
            make_one_qubit_h_config(
                make_physical_environment(
                    t1_zero_temperature_us=100.0,
                    tphi_zero_temperature_us=66.66666666666667,
                    temperature_mk=10.0,
                ),
                duration_us=30.0,
                time_steps=101,
            )
        )[0]
        high_noise = run_and_reconstruct(
            make_one_qubit_h_config(
                make_physical_environment(
                    t1_zero_temperature_us=1.0,
                    tphi_zero_temperature_us=0.9523809523809523,
                    temperature_mk=100.0,
                ),
                duration_us=30.0,
                time_steps=101,
            )
        )[0]

        self.assertLessEqual(
            high_noise.fidelity[-1],
            low_noise.fidelity[-1] + FIDELITY_PURITY_TOL,
        )
        self.assertLessEqual(
            high_noise.effective_operation_time_us,
            low_noise.effective_operation_time_us + FIDELITY_PURITY_TOL,
        )


if __name__ == "__main__":
    unittest.main()

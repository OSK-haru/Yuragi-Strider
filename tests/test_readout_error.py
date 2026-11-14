import unittest

from api.main import SimulateRequest, build_config_from_simulate_request
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gates import apply_readout_error, output_probabilities
from core.results import ReadoutErrorConfig, SimulationConfig
from core.simulator import run_simulation


class ReadoutErrorTest(unittest.TestCase):
    """Affine two-point readout model applied to observation probabilities."""

    def test_zero_error_is_the_identity(self) -> None:
        probabilities = {"00": 0.5, "01": 0.2, "10": 0.2, "11": 0.1}

        observed = apply_readout_error(probabilities, 2, [(0.0, 0.0), (0.0, 0.0)])

        for label, value in probabilities.items():
            self.assertAlmostEqual(observed[label], value, delta=1e-15)

    def test_single_qubit_matches_the_analytic_affine_relation(self) -> None:
        # P(obs 1) = p10 * (1 - P1) + (1 - p01) * P1
        #          = 0.01 * 0.7 + 0.95 * 0.3 = 0.292
        observed = apply_readout_error({"0": 0.7, "1": 0.3}, 1, [(0.01, 0.05)])

        self.assertAlmostEqual(observed["1"], 0.292, delta=1e-12)
        self.assertAlmostEqual(observed["0"], 0.708, delta=1e-12)

    def test_probabilities_sum_to_one_under_arbitrary_errors(self) -> None:
        probabilities = {
            format(index, "03b"): value
            for index, value in enumerate(
                [0.31, 0.04, 0.17, 0.09, 0.11, 0.02, 0.20, 0.06]
            )
        }

        observed = apply_readout_error(
            probabilities,
            3,
            [(0.02, 0.07), (0.005, 0.013), (0.03, 0.02)],
        )

        self.assertAlmostEqual(sum(observed.values()), 1.0, delta=1e-12)

    def test_errors_act_on_the_intended_qubit_only(self) -> None:
        # Qubit 0 is the most significant bit, matching the basis-label convention.
        # Starting from |01>, a p10 error on qubit 0 must leak into |11>.
        observed = apply_readout_error(
            {"00": 0.0, "01": 1.0, "10": 0.0, "11": 0.0},
            2,
            [(0.1, 0.0), (0.0, 0.0)],
        )

        self.assertAlmostEqual(observed["01"], 0.9, delta=1e-12)
        self.assertAlmostEqual(observed["11"], 0.1, delta=1e-12)
        self.assertAlmostEqual(observed["00"], 0.0, delta=1e-12)
        self.assertAlmostEqual(observed["10"], 0.0, delta=1e-12)

    def test_state_probabilities_are_not_mutated(self) -> None:
        # Readout error is an observation-stage effect: the density matrix and
        # anything derived from it must be untouched.
        rho = (
            (0.7 + 0j, 0.2 + 0j),
            (0.2 + 0j, 0.3 + 0j),
        )
        true_probabilities = output_probabilities(rho, 1)

        apply_readout_error(true_probabilities, 1, [(0.04, 0.09)])

        self.assertEqual(true_probabilities, output_probabilities(rho, 1))

    def test_rejects_a_non_positive_assignment_span(self) -> None:
        with self.assertRaises(ValueError):
            apply_readout_error({"0": 1.0, "1": 0.0}, 1, [(0.6, 0.5)])

    def test_rejects_errors_outside_the_unit_interval(self) -> None:
        with self.assertRaises(ValueError):
            apply_readout_error({"0": 1.0, "1": 0.0}, 1, [(-0.01, 0.02)])

    def test_rejects_a_qubit_count_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            apply_readout_error({"00": 1.0}, 2, [(0.01, 0.02)])


class ReadoutErrorConfigTest(unittest.TestCase):
    def test_default_configuration_is_disabled(self) -> None:
        self.assertFalse(ReadoutErrorConfig().is_enabled)

    def test_uniform_errors_expand_to_every_qubit(self) -> None:
        config = ReadoutErrorConfig(p10=0.01, p01=0.02)

        self.assertEqual(config.assignment_errors(3), [(0.01, 0.02)] * 3)

    def test_per_qubit_errors_are_kept_in_order(self) -> None:
        config = ReadoutErrorConfig(
            per_qubit=[{"p10": 0.01, "p01": 0.02}, {"p10": 0.005, "p01": 0.013}]
        )

        self.assertEqual(config.assignment_errors(2), [(0.01, 0.02), (0.005, 0.013)])

    def test_per_qubit_length_must_match_the_circuit(self) -> None:
        config = ReadoutErrorConfig(per_qubit=[{"p10": 0.01, "p01": 0.02}])

        with self.assertRaises(ValueError):
            config.assignment_errors(2)

    def test_round_trips_through_a_dictionary(self) -> None:
        config = ReadoutErrorConfig(p10=0.01, p01=0.02)

        restored = ReadoutErrorConfig.from_dict(config.to_dict())

        self.assertEqual(restored.assignment_errors(1), config.assignment_errors(1))

    def test_rejects_a_non_positive_assignment_span(self) -> None:
        with self.assertRaises(ValueError):
            ReadoutErrorConfig(p10=0.6, p01=0.5)


class ReadoutErrorSimulationTest(unittest.TestCase):
    """End-to-end behaviour through the gate-aware density-matrix path."""

    def test_absent_configuration_reproduces_the_baseline(self) -> None:
        baseline = run_simulation(_bell_config())
        explicit_zero = run_simulation(_bell_config({"p10": 0.0, "p01": 0.0}))

        self.assertEqual(
            explicit_zero.output_probabilities,
            baseline.output_probabilities,
        )

    def test_readout_error_moves_weight_onto_the_misread_outcomes(self) -> None:
        baseline = run_simulation(_bell_config())
        observed = run_simulation(_bell_config({"p10": 0.01, "p01": 0.02}))

        # A Bell state concentrates on 00 and 11; misreads leak into 01 and 10.
        self.assertLess(observed.output_probabilities["00"], baseline.output_probabilities["00"])
        self.assertLess(observed.output_probabilities["11"], baseline.output_probabilities["11"])
        self.assertGreater(observed.output_probabilities["01"], baseline.output_probabilities["01"])
        self.assertGreater(observed.output_probabilities["10"], baseline.output_probabilities["10"])

    def test_observed_probabilities_still_sum_to_one(self) -> None:
        observed = run_simulation(_bell_config({"p10": 0.01, "p01": 0.02}))

        self.assertAlmostEqual(sum(observed.output_probabilities.values()), 1.0, delta=1e-12)

    def test_state_metrics_are_unaffected(self) -> None:
        # Readout error describes the apparatus, not the state.
        baseline = run_simulation(_bell_config())
        observed = run_simulation(_bell_config({"p10": 0.01, "p01": 0.02}))

        self.assertAlmostEqual(observed.fidelity[-1], baseline.fidelity[-1], delta=1e-15)
        self.assertAlmostEqual(observed.purity[-1], baseline.purity[-1], delta=1e-15)

    def test_measurement_counts_follow_the_observed_distribution(self) -> None:
        baseline = run_simulation(_bell_config())
        observed = run_simulation(_bell_config({"p10": 0.05, "p01": 0.05}))

        self.assertNotEqual(observed.measurement_counts, baseline.measurement_counts)


class ReadoutErrorApiTest(unittest.TestCase):
    def test_request_without_readout_error_leaves_the_config_unset(self) -> None:
        config = build_config_from_simulate_request(SimulateRequest(**_api_payload()))

        self.assertIsNone(config.readout_error)

    def test_uniform_request_reaches_the_simulation_config(self) -> None:
        config = build_config_from_simulate_request(
            SimulateRequest(**_api_payload(), readout_error={"p10": 0.01, "p01": 0.02})
        )

        self.assertEqual(config.readout_error.assignment_errors(2), [(0.01, 0.02)] * 2)

    def test_per_qubit_request_reaches_the_simulation_config(self) -> None:
        config = build_config_from_simulate_request(
            SimulateRequest(
                **_api_payload(),
                readout_error={
                    "per_qubit": [
                        {"p10": 0.01, "p01": 0.02},
                        {"p10": 0.005, "p01": 0.013},
                    ]
                },
            )
        )

        self.assertEqual(
            config.readout_error.assignment_errors(2),
            [(0.01, 0.02), (0.005, 0.013)],
        )

    def test_non_positive_assignment_span_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_config_from_simulate_request(
                SimulateRequest(**_api_payload(), readout_error={"p10": 0.6, "p01": 0.5})
            )


def _api_payload() -> dict[str, object]:
    return {
        "circuit_preset": "bell",
        "simulation_backend": "python_dense",
        "parameters": {
            "temperature_mk": 28.0,
            "qubit_frequency_ghz": 5.0,
            "t1_zero_temperature_us": 15.848931924611133,
            "tphi_us": 13.68512385279464,
            "duration_us": 2.0,
            "time_steps": 11,
            "fidelity_threshold": 0.9,
        },
    }


def _bell_config(readout_error: dict[str, float] | None = None) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(type="H", targets=[0], controls=[], params={})
                    ],
                ),
                GateColumn(
                    step=1,
                    gates=[
                        GateOperation(
                            type="CNOT", targets=[1], controls=[0], params={}
                        )
                    ],
                ),
            ],
        ),
        readout_error=readout_error,
    )


if __name__ == "__main__":
    unittest.main()

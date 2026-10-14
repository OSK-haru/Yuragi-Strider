import unittest

from pydantic import ValidationError

from api.main import SimulateRequest, build_config_from_simulate_request, simulate
from core.circuit_model import CircuitConfig
from core.physical_environment import compute_environment_rates_per_qubit
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation


def _environment(per_qubit=None) -> EnvironmentConfig:
    return EnvironmentConfig(
        input_mode="physical",
        device_quality=1.0,
        temperature_mk=0.0,
        flux_noise_phi0=0.0,
        qubit_frequency_ghz=5.0,
        t1_max_us=100.0,
        tphi_max_us=100.0,
        per_qubit=per_qubit or [],
    )


def _payload() -> dict[str, object]:
    return {
        "simulation_backend": "python_dense",
        "input_mode": "physical",
        "circuit_config": {
            "logical_qubits": 2,
            "initial_states": [1, 1],
            "columns": [],
        },
        "parameters": {
            "device_quality": 1.0,
            "temperature_mk": 0.0,
            "flux_noise_phi0": 0.0,
            "qubit_frequency_ghz": 5.0,
            "t1_max_us": 100.0,
            "tphi_max_us": 100.0,
            "duration_us": 10.0,
            "time_steps": 21,
            "fidelity_threshold": 0.9,
            "per_qubit": [
                {"t1_max_us": 10.0, "tphi_max_us": 1000.0},
                {"t1_max_us": 100.0, "tphi_max_us": 1000.0},
            ],
        },
    }


class PerQubitEnvironmentTest(unittest.TestCase):
    def test_shared_values_expand_without_changing_rates(self) -> None:
        rates = compute_environment_rates_per_qubit(_environment(), 3)

        self.assertEqual(len(rates), 3)
        self.assertEqual(rates[0], rates[1])
        self.assertEqual(rates[1], rates[2])

    def test_local_t1_and_tphi_create_distinct_local_rates(self) -> None:
        rates = compute_environment_rates_per_qubit(
            _environment([
                {"t1_max_us": 10.0, "tphi_max_us": 20.0},
                {"t1_max_us": 100.0, "tphi_max_us": 200.0},
            ]),
            2,
        )

        self.assertGreater(rates[0].gamma_down_per_us, rates[1].gamma_down_per_us)
        self.assertGreater(rates[0].gamma_phi_per_us, rates[1].gamma_phi_per_us)
        self.assertAlmostEqual(rates[0].t1_effective_us, 10.0)
        self.assertAlmostEqual(rates[1].t1_effective_us, 100.0)

    def test_idle_network_applies_t1_to_the_intended_qubit(self) -> None:
        result = run_simulation(SimulationConfig(
            circuit=CircuitConfig(logical_qubits=2, initial_states=["1", "1"], columns=[]),
            environment=_environment([
                {"t1_max_us": 10.0, "tphi_max_us": 1000.0},
                {"t1_max_us": 100.0, "tphi_max_us": 1000.0},
            ]),
            duration_us=10.0,
            time_steps=21,
        ))

        probabilities = result.output_probabilities
        q0_p1 = probabilities["10"] + probabilities["11"]
        q1_p1 = probabilities["01"] + probabilities["11"]
        self.assertLess(q0_p1, q1_p1)
        self.assertAlmostEqual(q0_p1, 0.367879, delta=2e-5)
        self.assertAlmostEqual(q1_p1, 0.904837, delta=2e-5)


class PerQubitCalibrationApiTest(unittest.TestCase):
    def test_request_reaches_environment_and_response_metadata(self) -> None:
        request = SimulateRequest.model_validate(_payload())
        config = build_config_from_simulate_request(request)

        self.assertEqual(config.environment.per_qubit[0]["t1_max_us"], 10.0)
        response = simulate(request)
        self.assertEqual(len(response["rates"]["per_qubit"]), 2)
        self.assertAlmostEqual(
            response["rates"]["per_qubit"][0]["t1_effective_us"], 10.0
        )

    def test_per_qubit_coherence_count_must_match_circuit(self) -> None:
        payload = _payload()
        payload["parameters"]["per_qubit"] = [
            {"t1_max_us": 10.0, "tphi_max_us": 20.0}
        ]

        with self.assertRaises(ValidationError):
            SimulateRequest.model_validate(payload)

    def test_per_qubit_readout_count_must_match_circuit(self) -> None:
        payload = _payload()
        payload["readout_error"] = {
            "per_qubit": [{"p10": 0.01, "p01": 0.02}]
        }

        with self.assertRaises(ValidationError):
            SimulateRequest.model_validate(payload)

    def test_per_qubit_readout_changes_each_observation_channel(self) -> None:
        payload = _payload()
        payload["circuit_config"]["initial_states"] = [0, 0]
        payload["readout_error"] = {
            "per_qubit": [
                {"p10": 0.1, "p01": 0.0},
                {"p10": 0.2, "p01": 0.0},
            ]
        }

        response = simulate(SimulateRequest.model_validate(payload))

        self.assertAlmostEqual(response["output_probabilities"]["00"], 0.72, places=6)
        self.assertAlmostEqual(response["output_probabilities"]["01"], 0.18, places=6)
        self.assertAlmostEqual(response["output_probabilities"]["10"], 0.08, places=6)
        self.assertAlmostEqual(response["output_probabilities"]["11"], 0.02, places=6)


if __name__ == "__main__":
    unittest.main()

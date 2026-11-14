import unittest

from pydantic import ValidationError

from api.main import SimulateRequest, simulate


class ApiSimulateQubitCountsTest(unittest.TestCase):
    def _base_request(self) -> dict[str, object]:
        return {
            "simulation_backend": "python_dense",
            "gate_duration_defaults": {
                "H": 0.02,
                "X": 0.02,
                "Z": 0.0,
                "CNOT": 0.2,
                "MEASURE": 0.0,
            },
            "parameters": {
                "t1_zero_temperature_us": 39.810717055349734,
                "tphi_zero_temperature_us": 33.20178309308936,
                "readout_resonator_frequency_ghz": 7.0,
                "readout_resonator_kappa_mhz": 1.0,
                "readout_resonator_chi_mhz": 0.5,
                "temperature_mk": 15.0,
                "qubit_frequency_ghz": 5.0,
                "duration_us": 2.0,
                "time_steps": 101,
                "fidelity_threshold": 0.9,
            },
        }

    def _response_keys(self, response: dict[str, object]) -> None:
        for key in (
            "circuit",
            "parameters",
            "diagnostics",
            "summary",
            "timeline",
            "physical_timeline",
            "circuit_probes",
            "output_probabilities",
            "measurement",
            "run",
            "warnings",
            "issues",
        ):
            self.assertIn(key, response)

    def _simulate(self, payload: dict[str, object]) -> dict[str, object]:
        request = SimulateRequest(**payload)
        return simulate(request)

    def test_two_qubit_custom_circuit_returns_full_response(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 2,
            "initial_states": [0, 0],
            "columns": [
                {
                    "step": 0,
                    "gates": [
                        {
                            "type": "H",
                            "targets": [0],
                            "controls": [],
                            "params": {},
                        }
                    ],
                },
                {
                    "step": 1,
                    "gates": [
                        {
                            "type": "CNOT",
                            "targets": [1],
                            "controls": [0],
                            "params": {},
                        }
                    ],
                },
            ],
        }

        body = self._simulate(payload)
        self._response_keys(body)
        self.assertTrue(body["output_probabilities"])
        self.assertEqual(body["run"]["status"], "Completed")

    def test_three_qubit_valid_circuit_returns_full_response(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 3,
            "initial_states": [0, 0, 0],
            "columns": [
                {
                    "step": 0,
                    "gates": [
                        {
                            "type": "H",
                            "targets": [0],
                            "controls": [],
                            "params": {},
                        }
                    ],
                }
            ],
        }

        body = self._simulate(payload)
        self._response_keys(body)
        self.assertEqual(body["run"]["status"], "Completed")
        self.assertEqual(len(body["timeline"]), 101)
        self.assertEqual(len(body["output_probabilities"]), 8)
        self.assertEqual(sorted(body["output_probabilities"].keys()), [
            "000",
            "001",
            "010",
            "011",
            "100",
            "101",
            "110",
            "111",
        ])
        self.assertFalse(body["issues"])
        self.assertFalse(body["warnings"])

    def test_four_qubit_valid_circuit_returns_full_response(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 4,
            "initial_states": [0, 0, 0, 0],
            "columns": [
                {
                    "step": 0,
                    "gates": [
                        {
                            "type": "X",
                            "targets": [3],
                            "controls": [],
                            "params": {},
                        }
                    ],
                }
            ],
        }

        body = self._simulate(payload)
        self._response_keys(body)
        self.assertEqual(body["run"]["status"], "Completed")
        self.assertEqual(len(body["timeline"]), 101)
        self.assertEqual(len(body["output_probabilities"]), 16)
        self.assertEqual(sorted(body["output_probabilities"].keys()), [
            "0000",
            "0001",
            "0010",
            "0011",
            "0100",
            "0101",
            "0110",
            "0111",
            "1000",
            "1001",
            "1010",
            "1011",
            "1100",
            "1101",
            "1110",
            "1111",
        ])
        self.assertFalse(body["issues"])
        self.assertFalse(body["warnings"])

    def test_five_qubit_valid_circuit_is_accepted_by_request_schema(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 5,
            "initial_states": [0, 0, 0, 0, 0],
            "columns": [],
        }

        request = SimulateRequest(**payload)
        self.assertEqual(request.circuit_config.logical_qubits, 5)

    def test_eight_qubit_valid_circuit_is_accepted_by_request_schema(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 8,
            "initial_states": [0] * 8,
            "columns": [],
        }

        request = SimulateRequest(**payload)
        self.assertEqual(request.circuit_config.logical_qubits, 8)

    def test_invalid_three_qubit_initial_state_length_is_rejected(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 3,
            "initial_states": [0, 0],
            "columns": [],
        }

        with self.assertRaises(ValidationError):
            SimulateRequest(**payload)

    def test_invalid_four_qubit_target_index_is_rejected(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 4,
            "initial_states": [0, 0, 0, 0],
            "columns": [
                {
                    "step": 0,
                    "gates": [
                        {
                            "type": "H",
                            "targets": [4],
                            "controls": [],
                            "params": {},
                        }
                    ],
                }
            ],
        }

        with self.assertRaises(ValidationError):
            SimulateRequest(**payload)

    def test_invalid_cnot_same_control_and_target_is_rejected(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 3,
            "initial_states": [0, 0, 0],
            "columns": [
                {
                    "step": 0,
                    "gates": [
                        {
                            "type": "CNOT",
                            "targets": [1],
                            "controls": [1],
                            "params": {},
                        }
                    ],
                }
            ],
        }

        with self.assertRaises(ValidationError):
            SimulateRequest(**payload)


if __name__ == "__main__":
    unittest.main()

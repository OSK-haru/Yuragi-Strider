import unittest

from pydantic import ValidationError

from api.main import (
    MAX_API_CIRCUIT_COLUMNS,
    SimulateRequest,
    build_config_from_simulate_request,
    simulate,
)


class ApiSimulateCircuitConfigTest(unittest.TestCase):
    def _base_request(self) -> dict[str, object]:
        return {
            "simulation_backend": "python_dense",
            "parameters": {
                "t1_zero_temperature_us": 39.810717055349734,
                "tphi_us": 33.20178309308936,
                "temperature_mk": 15.0,
                "qubit_frequency_ghz": 5.0,
                "duration_us": 2.0,
                "time_steps": 11,
                "fidelity_threshold": 0.9,
            },
            "gate_duration_defaults": {
                "H": 0.02,
                "X": 0.02,
                "Z": 0.0,
                "CNOT": 0.2,
                "MEASURE": 0.0,
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

    def test_preset_only_post_works(self) -> None:
        payload = self._base_request()
        payload["circuit_preset"] = "bell"

        request = SimulateRequest(**payload)

        config = build_config_from_simulate_request(request)

        self.assertEqual(config.circuit.logical_qubits, 2)
        response = simulate(request)
        self._response_keys(response)
        self.assertEqual(response["parameters"]["t1_zero_temperature_us"],
                         payload["parameters"]["t1_zero_temperature_us"])

    def test_circuit_config_only_post_works(self) -> None:
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

        request = SimulateRequest(**payload)
        response = simulate(request)

        self._response_keys(response)
        self.assertEqual(response["diagnostics"]["simulation_backend"], "python_dense")

    def test_independent_cnot_operations_in_same_column_work(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 4,
            "initial_states": [0, 0, 0, 0],
            "columns": [
                {
                    "step": 0,
                    "gates": [
                        {
                            "type": "CNOT",
                            "targets": [1],
                            "controls": [0],
                            "params": {},
                        },
                        {
                            "type": "CNOT",
                            "targets": [3],
                            "controls": [2],
                            "params": {},
                        },
                    ],
                }
            ],
        }

        request = SimulateRequest(**payload)
        response = simulate(request)

        self._response_keys(response)
        self.assertEqual(response["circuit"]["qubit_count"], 4)
        cnot_markers = [
            gate
            for gate in response["circuit"]["columns"][0]["gates"]
            if gate["label"] == "CNOT"
        ]
        self.assertEqual(len(cnot_markers), 4)

    def test_custom_non_bell_simple_circuit_works(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 1,
            "initial_states": [0],
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

        request = SimulateRequest(**payload)
        response = simulate(request)

        self._response_keys(response)

    def test_empty_circuit_works(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 2,
            "initial_states": [0, 0],
            "columns": [],
        }

        request = SimulateRequest(**payload)
        response = simulate(request)

        self._response_keys(response)

    def test_neither_circuit_config_nor_circuit_preset_is_rejected(self) -> None:
        payload = self._base_request()

        with self.assertRaises(ValidationError):
            SimulateRequest(**payload)

    def test_initial_states_are_accepted_as_numbers(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 2,
            "initial_states": [0, 0],
            "columns": [],
        }

        request = SimulateRequest(**payload)

        self.assertEqual(request.circuit_config.initial_states, [0, 0])

    def test_invalid_cnot_is_rejected(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 2,
            "initial_states": [0, 0],
            "columns": [
                {
                    "step": 0,
                    "gates": [
                        {
                            "type": "CNOT",
                            "targets": [0],
                            "controls": [0],
                            "params": {},
                        }
                    ],
                }
            ],
        }

        with self.assertRaises(ValidationError):
            SimulateRequest(**payload)

    def test_invalid_qubit_index_is_rejected(self) -> None:
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
                            "targets": [2],
                            "controls": [],
                            "params": {},
                        }
                    ],
                }
            ],
        }

        with self.assertRaises(ValidationError):
            SimulateRequest(**payload)

    def test_negative_gate_duration_is_rejected(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 1,
            "initial_states": [0],
            "columns": [
                {
                    "step": 0,
                    "gates": [
                        {
                            "type": "H",
                            "targets": [0],
                            "controls": [],
                            "params": {"duration_us": -0.1},
                        }
                    ],
                }
            ],
        }

        with self.assertRaises(ValidationError):
            SimulateRequest(**payload)

    def test_circuit_exceeding_column_budget_is_rejected(self) -> None:
        payload = self._base_request()
        payload["circuit_config"] = {
            "logical_qubits": 1,
            "initial_states": [0],
            "columns": [
                {"step": index, "gates": []}
                for index in range(MAX_API_CIRCUIT_COLUMNS + 1)
            ],
        }

        with self.assertRaises(ValidationError):
            SimulateRequest(**payload)


if __name__ == "__main__":
    unittest.main()

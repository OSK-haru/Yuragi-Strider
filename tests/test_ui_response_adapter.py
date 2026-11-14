import unittest

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.errors import ValidationIssue
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult
from core.simulator import run_simulation
from core.ui_response import simulation_result_to_ui_response


class UiResponseAdapterTests(unittest.TestCase):
    def test_adapter_returns_all_top_level_keys(self):
        result = run_simulation(SimulationConfig())

        response = simulation_result_to_ui_response(result)

        self.assertEqual(
            {
                "circuit",
                "parameters",
                "rates",
                "diagnostics",
                "summary",
                "timeline",
                "physical_timeline",
                "circuit_probes",
                "output_probabilities",
                "measurement",
                "state_snapshots",
                "state_transfer",
                "run",
                "warnings",
                "issues",
            },
            set(response.keys()),
        )

    def test_one_qubit_h_maps_summary_timeline_and_probabilities(self):
        result = run_simulation(SimulationConfig())

        response = simulation_result_to_ui_response(result)

        self.assertIsNotNone(response["summary"]["final_fidelity"])
        self.assertIsNotNone(response["summary"]["final_purity"])
        self.assertEqual(len(result.times), len(response["timeline"]))
        self.assertEqual(result.output_probabilities, response["output_probabilities"])

    def test_diagnostics_include_model_evolution_and_backend(self):
        result = run_simulation(SimulationConfig())

        diagnostics = simulation_result_to_ui_response(result)["diagnostics"]

        self.assertIn("simulation_model", diagnostics)
        self.assertIn("evolution_mode", diagnostics)
        self.assertEqual("python_dense", diagnostics["simulation_backend"])

    def test_physical_parameters_include_temperature_k_and_mk(self):
        config = SimulationConfig(
            environment=EnvironmentConfig(
                t1_zero_temperature_us=50.11872336272722,
                tphi_us=0.00999800513571278,
                temperature_mk=15.0,
                qubit_frequency_ghz=5.0,
            )
        )
        result = run_simulation(config)

        parameters = simulation_result_to_ui_response(result)["parameters"]

        self.assertAlmostEqual(0.015, parameters["temperature_k"])
        self.assertAlmostEqual(15.0, parameters["temperature_mk"])
        self.assertAlmostEqual(50.11872336272722, parameters["t1_zero_temperature_us"])
        self.assertNotIn("normalized_temperature", parameters)

    def test_physical_rates_expose_canonical_fields_without_legacy_alias(self):
        config = SimulationConfig(
            environment=EnvironmentConfig(
                t1_zero_temperature_us=50.11872336272722,
                tphi_us=0.00999800513571278,
                temperature_mk=15.0,
                qubit_frequency_ghz=5.0,
            )
        )

        rates = simulation_result_to_ui_response(run_simulation(config))["rates"]

        self.assertIsNotNone(rates["gamma_down_per_us"])
        self.assertIsNotNone(rates["gamma_up_per_us"])
        self.assertIsNotNone(rates["gamma_population_relaxation_per_us"])
        self.assertAlmostEqual(
            rates["gamma_down_per_us"] + rates["gamma_up_per_us"],
            rates["gamma_population_relaxation_per_us"],
        )
        self.assertNotIn("gamma1_per_us", rates)
        self.assertAlmostEqual(rates["tphi_us"], 0.00999800513571278)

    def test_infinite_times_are_reported_as_null(self):
        result = run_simulation(SimulationConfig(environment=EnvironmentConfig(tphi_us=float("inf"))))

        parameters = simulation_result_to_ui_response(result)["parameters"]

        self.assertIsNone(parameters["tphi_us"])
        self.assertIsNotNone(parameters["t1_zero_temperature_us"])

    def test_cnot_maps_to_control_and_target_entries(self):
        circuit = CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(
                            type="CNOT",
                            controls=[0],
                            targets=[1],
                            params={"duration_us": 0.2},
                        )
                    ],
                )
            ],
        )
        result = run_simulation(SimulationConfig(circuit=circuit))

        gates = simulation_result_to_ui_response(result)["circuit"]["columns"][0]["gates"]

        self.assertIn("control", [gate["kind"] for gate in gates])
        self.assertIn("target", [gate["kind"] for gate in gates])

    def test_auto_decomposed_cz_exposes_compiled_preview_and_source_map(self):
        circuit = CircuitConfig(
            logical_qubits=2,
            initial_states=["+", "+"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(
                            type="CZ",
                            controls=[0],
                            targets=[1],
                            params={"duration_us": 0.2},
                        )
                    ],
                )
            ],
        )
        result = run_simulation(SimulationConfig(
            circuit=circuit,
            duration_us=1.0,
            compilation_mode="auto_decompose",
            native_gate_durations_us={"H": 0.02, "CNOT": 0.2},
        ))

        compilation = simulation_result_to_ui_response(result)["run"]["compilation"]

        self.assertEqual("auto_decompose", compilation["mode"])
        self.assertEqual(1, compilation["logical_gate_count"])
        self.assertEqual(3, compilation["compiled_gate_count"])
        self.assertEqual(
            ["H", "CNOT", "H"],
            [
                column["gates"][0]["type"]
                for column in compilation["compiled_circuit"]["columns"]
            ],
        )
        self.assertEqual(
            "cz_to_h_cnot_h_v1",
            compilation["source_map"][0]["rule_id"],
        )

    def test_swap_maps_two_symmetric_targets_and_auditable_cnot_directions(self):
        circuit = CircuitConfig(
            logical_qubits=2,
            initial_states=["1", "0"],
            columns=[GateColumn(
                step=0,
                gates=[GateOperation(
                    type="SWAP",
                    targets=[0, 1],
                    controls=[],
                    params={"duration_us": 0.2},
                )],
            )],
        )
        result = run_simulation(SimulationConfig(
            circuit=circuit,
            duration_us=0.6,
            compilation_mode="auto_decompose",
            native_gate_durations_us={"CNOT": 0.2},
        ))

        response = simulation_result_to_ui_response(result)
        swap_entries = response["circuit"]["columns"][0]["gates"]
        operations = response["run"]["compilation"]["source_map"][0][
            "compiled_operations"
        ]

        self.assertEqual([entry["qubits"] for entry in swap_entries], [[0], [1]])
        self.assertEqual(
            [(item["controls"], item["targets"]) for item in operations],
            [([0], [1]), ([1], [0]), ([0], [1])],
        )

    def test_ccx_maps_two_controls_and_exposes_full_decomposition(self):
        circuit = CircuitConfig(
            logical_qubits=3,
            initial_states=["0", "0", "0"],
            columns=[GateColumn(
                step=0,
                gates=[GateOperation(
                    type="CCX",
                    targets=[2],
                    controls=[0, 1],
                    params={"duration_us": 0.4},
                )],
            )],
        )
        result = run_simulation(SimulationConfig(
            circuit=circuit,
            duration_us=1.34,
            compilation_mode="auto_decompose",
            native_gate_durations_us={"H": 0.02, "RZ": 0.02, "CNOT": 0.2},
        ))

        response = simulation_result_to_ui_response(result)
        entries = response["circuit"]["columns"][0]["gates"]
        compilation = response["run"]["compilation"]

        self.assertEqual([entry["kind"] for entry in entries], ["control", "control", "target"])
        self.assertEqual(compilation["compiled_gate_count"], 15)
        self.assertEqual(compilation["compiled_depth"], 13)
        self.assertEqual(
            compilation["source_map"][0]["rule_id"],
            "ccx_to_h_cnot_rz_v1",
        )

    def test_timeline_uses_shortest_length_and_warns(self):
        result = SimulationResult(
            config=SimulationConfig(),
            times=[0.0, 1.0, 2.0],
            fidelity=[1.0, 0.9],
            purity=[1.0, 0.95, 0.9],
            effective_operation_time_us=None,
        )

        response = simulation_result_to_ui_response(result)

        self.assertEqual(2, len(response["timeline"]))
        self.assertTrue(any("shortest length" in warning for warning in response["warnings"]))

    def test_issues_are_json_safe(self):
        result = SimulationResult(
            config=SimulationConfig(),
            times=[],
            fidelity=[],
            purity=[],
            effective_operation_time_us=None,
            issues=[
                ValidationIssue(
                    level="error",
                    code="TEST",
                    message="Test issue",
                    detail="detail",
                    suggestion="suggestion",
                )
            ],
        )

        response = simulation_result_to_ui_response(result)

        self.assertEqual("Completed with issues", response["run"]["status"])
        self.assertEqual("TEST", response["issues"][0]["code"])


if __name__ == "__main__":
    unittest.main()

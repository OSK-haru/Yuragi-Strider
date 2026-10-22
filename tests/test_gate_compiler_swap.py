import unittest

import numpy as np

from api.main import SimulateRequest, build_config_from_simulate_request
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gate_compiler import AUTO_DECOMPOSE, LOGICAL_DIRECT, compile_gate_aware_circuit
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from tests.physics_test_helpers import circuit_unitary, make_ideal_environment


class SwapGateCompilerTests(unittest.TestCase):
    def test_swap_decomposition_matches_direct_unitary_and_reverses_middle_control(self):
        direct = compile_gate_aware_circuit(_swap_circuit(), LOGICAL_DIRECT)
        compiled = compile_gate_aware_circuit(_swap_circuit(), AUTO_DECOMPOSE)

        self.assertLess(
            float(np.max(np.abs(
                np.asarray(circuit_unitary(direct.circuit))
                - np.asarray(circuit_unitary(compiled.circuit))
            ))),
            1e-12,
        )
        operations = [column.gates[0] for column in compiled.circuit.columns]
        self.assertEqual([gate.type for gate in operations], ["CNOT"] * 3)
        self.assertEqual(
            [(gate.controls[0], gate.targets[0]) for gate in operations],
            [(0, 1), (1, 0), (0, 1)],
        )
        self.assertEqual(
            compiled.diagnostics["decomposition_rules_used"],
            ["swap_to_three_cnot_v1"],
        )
        source_operations = compiled.diagnostics["source_map"][0]["compiled_operations"]
        self.assertEqual(
            [
                (operation["controls"][0], operation["targets"][0])
                for operation in source_operations
            ],
            [(0, 1), (1, 0), (0, 1)],
        )

    def test_zero_noise_direct_and_decomposed_swap_match(self):
        direct = run_simulation(_simulation_config(LOGICAL_DIRECT, make_ideal_environment()))
        compiled = run_simulation(_simulation_config(AUTO_DECOMPOSE, make_ideal_environment()))

        self.assertFalse(direct.issues)
        self.assertFalse(compiled.issues)
        self.assertAlmostEqual(direct.output_probabilities["01"], 1.0, delta=1e-10)
        self.assertAlmostEqual(compiled.output_probabilities["01"], 1.0, delta=1e-10)
        self.assertAlmostEqual(direct.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(compiled.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(direct.diagnostics["compiled_duration_us"], 0.2)
        self.assertAlmostEqual(compiled.diagnostics["compiled_duration_us"], 0.6)

    def test_finite_noise_exposes_three_cnot_cost(self):
        environment = EnvironmentConfig(
            input_mode="physical",
            device_quality=1.0,
            temperature_mk=15.0,
            flux_noise_phi0=0.0,
            qubit_frequency_ghz=5.0,
            t1_max_us=1.0,
            tphi_max_us=1.0,
        )
        direct = run_simulation(_simulation_config(LOGICAL_DIRECT, environment))
        compiled = run_simulation(_simulation_config(AUTO_DECOMPOSE, environment))

        self.assertGreater(
            compiled.diagnostics["total_gate_duration_us"],
            direct.diagnostics["total_gate_duration_us"],
        )
        self.assertNotAlmostEqual(direct.fidelity[-1], compiled.fidelity[-1], delta=1e-6)

    def test_disjoint_cz_and_swap_share_three_compiled_layers(self):
        circuit = CircuitConfig(
            logical_qubits=4,
            initial_states=["0", "0", "1", "0"],
            columns=[GateColumn(0, [
                GateOperation("CZ", [1], controls=[0], params={"duration_us": 0.2}),
                GateOperation("SWAP", [2, 3], params={"duration_us": 0.2}),
            ])],
        )
        compiled = compile_gate_aware_circuit(circuit, AUTO_DECOMPOSE)

        self.assertEqual(compiled.diagnostics["compiled_depth"], 3)
        self.assertEqual(compiled.diagnostics["compiled_gate_count"], 6)
        self.assertEqual(
            compiled.diagnostics["decomposition_rules_used"],
            ["cz_to_h_cnot_h_v1", "swap_to_three_cnot_v1"],
        )
        self.assertEqual([len(column.gates) for column in compiled.circuit.columns], [2, 2, 2])


class SwapGateCompilerApiTests(unittest.TestCase):
    def test_api_accepts_canonical_swap_targets(self):
        request = SimulateRequest(**{
            "simulation_backend": "python_dense",
            "compilation_mode": "auto_decompose",
            "input_mode": "normalized",
            "circuit_config": {
                "logical_qubits": 2,
                "initial_states": [1, 0],
                "columns": [{
                    "step": 0,
                    "gates": [{"type": "SWAP", "targets": [0, 1], "controls": []}],
                }],
            },
            "parameters": {
                "normalized_temperature": 0.0,
                "normalized_magnetic_field": 0.0,
                "noise_level": 0.0,
                "duration_us": 0.6,
                "time_steps": 21,
                "fidelity_threshold": 0.9,
            },
        })
        config = build_config_from_simulate_request(request)
        self.assertEqual(config.circuit.columns[0].gates[0].targets, [0, 1])
        self.assertEqual(config.circuit.columns[0].gates[0].controls, [])
        self.assertAlmostEqual(config.circuit.columns[0].gates[0].params["duration_us"], 0.2)


def _swap_circuit() -> CircuitConfig:
    return CircuitConfig(
        logical_qubits=2,
        initial_states=["1", "0"],
        columns=[GateColumn(0, [
            GateOperation("SWAP", [0, 1], params={"duration_us": 0.2})
        ])],
    )


def _simulation_config(mode: str, environment: EnvironmentConfig):
    return SimulationConfig(
        circuit=_swap_circuit(),
        environment=environment,
        duration_us=0.6,
        time_steps=41,
        fidelity_threshold=0.9,
        compilation_mode=mode,
        native_gate_durations_us={"CNOT": 0.2},
    )


if __name__ == "__main__":
    unittest.main()

import unittest

import numpy as np

from api.main import SimulateRequest, build_config_from_simulate_request
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gate_compiler import AUTO_DECOMPOSE, LOGICAL_DIRECT, compile_gate_aware_circuit
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from tests.physics_test_helpers import circuit_unitary, make_ideal_environment


class CZGateCompilerTests(unittest.TestCase):
    def test_cz_decomposition_matches_direct_unitary(self) -> None:
        circuit = _cz_circuit()
        direct = compile_gate_aware_circuit(circuit, LOGICAL_DIRECT)
        compiled = compile_gate_aware_circuit(circuit, AUTO_DECOMPOSE)

        direct_unitary = circuit_unitary(direct.circuit)
        compiled_unitary = circuit_unitary(compiled.circuit)
        self.assertLess(
            float(np.max(np.abs(
                np.asarray(direct_unitary) - np.asarray(compiled_unitary)
            ))),
            1e-12,
        )
        self.assertEqual(
            [column.gates[0].type for column in compiled.circuit.columns],
            ["H", "CNOT", "H"],
        )
        self.assertEqual(compiled.diagnostics["compiled_depth"], 3)
        self.assertEqual(compiled.diagnostics["decomposition_rules_used"], [
            "cz_to_h_cnot_h_v1"
        ])
        self.assertEqual(
            compiled.diagnostics["source_map"][0]["source_gate"],
            "CZ",
        )

    def test_zero_noise_direct_and_decomposed_cz_match(self) -> None:
        direct = run_simulation(_simulation_config(LOGICAL_DIRECT, make_ideal_environment()))
        compiled = run_simulation(_simulation_config(AUTO_DECOMPOSE, make_ideal_environment()))

        self.assertFalse(direct.issues)
        self.assertFalse(compiled.issues)
        for basis in ("00", "01", "10", "11"):
            self.assertAlmostEqual(
                direct.output_probabilities[basis],
                compiled.output_probabilities[basis],
                delta=1e-10,
            )
        self.assertAlmostEqual(direct.fidelity[-1], 1.0, delta=1e-10)
        self.assertAlmostEqual(compiled.fidelity[-1], 1.0, delta=1e-10)
        self.assertEqual(direct.diagnostics["compiled_depth"], 1)
        self.assertEqual(compiled.diagnostics["compiled_depth"], 3)
        self.assertAlmostEqual(direct.diagnostics["compiled_duration_us"], 0.2)
        self.assertAlmostEqual(compiled.diagnostics["compiled_duration_us"], 0.24)

    def test_finite_noise_exposes_decomposition_cost(self) -> None:
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
        self.assertNotAlmostEqual(
            direct.fidelity[-1], compiled.fidelity[-1], delta=1e-6
        )
        self.assertGreater(
            compiled.diagnostics["total_gate_duration_us"],
            direct.diagnostics["total_gate_duration_us"],
        )

    def test_compilation_settings_round_trip(self) -> None:
        config = _simulation_config(AUTO_DECOMPOSE, make_ideal_environment())
        restored = SimulationConfig.from_dict(config.to_dict())
        self.assertEqual(restored.compilation_mode, AUTO_DECOMPOSE)
        self.assertEqual(restored.native_gate_durations_us["H"], 0.02)


class CZGateCompilerApiTests(unittest.TestCase):
    def test_api_accepts_cz_auto_decomposition(self) -> None:
        request = SimulateRequest(**{
            "simulation_backend": "python_dense",
            "compilation_mode": "auto_decompose",
            "input_mode": "normalized",
            "circuit_config": {
                "logical_qubits": 2,
                "initial_states": [0, 0],
                "columns": [{
                    "step": 0,
                    "gates": [{
                        "type": "CZ",
                        "controls": [0],
                        "targets": [1],
                    }],
                }],
            },
            "parameters": {
                "normalized_temperature": 0.0,
                "normalized_magnetic_field": 0.0,
                "noise_level": 0.0,
                "duration_us": 0.24,
                "time_steps": 21,
                "fidelity_threshold": 0.9,
            },
        })
        config = build_config_from_simulate_request(request)
        self.assertEqual(config.compilation_mode, AUTO_DECOMPOSE)
        self.assertEqual(config.circuit.columns[0].gates[0].type, "CZ")
        self.assertAlmostEqual(config.circuit.columns[0].gates[0].params["duration_us"], 0.2)


def _cz_circuit() -> CircuitConfig:
    return CircuitConfig(
        logical_qubits=2,
        initial_states=["+", "+"],
        columns=[GateColumn(0, [GateOperation(
            "CZ", [1], controls=[0], params={"duration_us": 0.2}
        )])],
    )


def _simulation_config(mode: str, environment: EnvironmentConfig):
    return SimulationConfig(
        circuit=_cz_circuit(),
        environment=environment,
        duration_us=0.24,
        time_steps=31,
        fidelity_threshold=0.9,
        compilation_mode=mode,
        native_gate_durations_us={"H": 0.02, "CNOT": 0.2},
    )


if __name__ == "__main__":
    unittest.main()

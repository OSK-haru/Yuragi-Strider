import math
import unittest

import numpy as np
from pydantic import ValidationError

from api.main import SimulateRequest, build_config_from_simulate_request
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gate_compiler import AUTO_DECOMPOSE, LOGICAL_DIRECT, compile_gate_aware_circuit
from core.gates import (
    apply_unitary_to_density,
)
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from tests.physics_test_helpers import circuit_unitary, make_ideal_environment


class CcxGateCompilerTests(unittest.TestCase):
    def test_ccx_decomposition_matches_truth_table_and_density_channel(self):
        direct = compile_gate_aware_circuit(_ccx_circuit(), LOGICAL_DIRECT)
        compiled = compile_gate_aware_circuit(_ccx_circuit(), AUTO_DECOMPOSE)
        direct_unitary = np.asarray(circuit_unitary(direct.circuit))
        compiled_unitary = np.asarray(circuit_unitary(compiled.circuit))

        phase = np.trace(direct_unitary.conj().T @ compiled_unitary) / 8.0
        self.assertAlmostEqual(abs(phase), 1.0, delta=1e-12)
        self.assertLess(
            float(np.max(np.abs(compiled_unitary - phase * direct_unitary))),
            1e-12,
        )
        self.assertAlmostEqual(phase.real, math.cos(math.pi / 8.0), delta=1e-12)
        self.assertAlmostEqual(phase.imag, -math.sin(math.pi / 8.0), delta=1e-12)

        for basis_index in range(8):
            expected_index = basis_index
            bits = list(f"{basis_index:03b}")
            if bits[0] == "1" and bits[1] == "1":
                bits[2] = "0" if bits[2] == "1" else "1"
                expected_index = int("".join(bits), 2)
            self.assertEqual(
                int(np.argmax(np.abs(direct_unitary[:, basis_index]))),
                expected_index,
            )
            self.assertEqual(
                int(np.argmax(np.abs(compiled_unitary[:, basis_index]))),
                expected_index,
            )

        rho = _test_density_matrix()
        direct_rho = np.asarray(apply_unitary_to_density(
            _as_matrix(rho), circuit_unitary(direct.circuit),
        ))
        compiled_rho = np.asarray(apply_unitary_to_density(
            _as_matrix(rho), circuit_unitary(compiled.circuit),
        ))
        self.assertLess(float(np.max(np.abs(direct_rho - compiled_rho))), 1e-12)

    def test_ccx_source_map_preserves_15_operations_and_signed_quarter_turns(self):
        compiled = compile_gate_aware_circuit(_ccx_circuit(), AUTO_DECOMPOSE)
        source = compiled.diagnostics["source_map"][0]
        operations = source["compiled_operations"]

        self.assertEqual(source["rule_id"], "ccx_to_h_cnot_rz_v1")
        self.assertEqual(compiled.diagnostics["compiled_depth"], 13)
        self.assertEqual(compiled.diagnostics["compiled_gate_count"], 15)
        self.assertEqual(
            {gate: sum(item["gate"] == gate for item in operations) for gate in ("H", "CNOT", "RZ")},
            {"H": 2, "CNOT": 6, "RZ": 7},
        )
        self.assertEqual(
            [
                (item["controls"][0], item["targets"][0])
                for item in operations
                if item["gate"] == "CNOT"
            ],
            [(1, 2), (0, 2), (1, 2), (0, 2), (0, 1), (0, 1)],
        )
        rz_angles = [
            item["params"]["theta_rad"]
            for item in operations
            if item["gate"] == "RZ"
        ]
        self.assertEqual(sum(angle > 0 for angle in rz_angles), 4)
        self.assertEqual(sum(angle < 0 for angle in rz_angles), 3)
        self.assertTrue(all(abs(abs(angle) - math.pi / 4.0) < 1e-12 for angle in rz_angles))
        self.assertAlmostEqual(compiled.diagnostics["compiled_duration_us"], 1.34)

    def test_finite_noise_exposes_ccx_decomposition_cost(self):
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

        self.assertFalse(direct.issues)
        self.assertFalse(compiled.issues)
        self.assertAlmostEqual(direct.diagnostics["compiled_duration_us"], 0.4)
        self.assertAlmostEqual(compiled.diagnostics["compiled_duration_us"], 1.34)
        self.assertGreater(
            compiled.diagnostics["total_gate_duration_us"],
            direct.diagnostics["total_gate_duration_us"],
        )
        self.assertNotAlmostEqual(direct.fidelity[-1], compiled.fidelity[-1], delta=1e-6)

    def test_ccx_auto_decomposition_runs_through_explicit_cptp(self):
        config = _simulation_config(AUTO_DECOMPOSE, make_ideal_environment())
        config.evolution_method = "explicit_cptp"
        result = run_simulation(config)

        self.assertFalse(result.issues)
        self.assertEqual(result.diagnostics["compiled_depth"], 13)
        self.assertTrue(result.diagnostics["cptp_all_maps_passed_audit"])
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-11)


class CcxGateCompilerApiTests(unittest.TestCase):
    def test_api_accepts_two_ccx_controls(self):
        request = SimulateRequest(**_api_payload([0, 1], [2]))
        config = build_config_from_simulate_request(request)
        gate = config.circuit.columns[0].gates[0]
        self.assertEqual(gate.type, "CCX")
        self.assertEqual(gate.controls, [0, 1])
        self.assertEqual(gate.targets, [2])
        self.assertAlmostEqual(gate.params["duration_us"], 0.4)

    def test_api_rejects_repeated_ccx_qubits(self):
        with self.assertRaises(ValidationError):
            SimulateRequest(**_api_payload([0, 2], [2]))


def _ccx_circuit() -> CircuitConfig:
    return CircuitConfig(
        logical_qubits=3,
        initial_states=["+", "1", "0"],
        columns=[GateColumn(0, [GateOperation(
            "CCX",
            [2],
            controls=[0, 1],
            params={"duration_us": 0.4},
        )])],
    )


def _test_density_matrix() -> np.ndarray:
    ket = np.asarray(
        [1.0, 0.2j, -0.3, 0.4j, 0.1, -0.2j, 0.5, 0.7j],
        dtype=np.complex128,
    )
    ket /= np.linalg.norm(ket)
    return np.outer(ket, ket.conj())


def _as_matrix(array: np.ndarray):
    return tuple(tuple(complex(value) for value in row) for row in array)


def _simulation_config(mode: str, environment: EnvironmentConfig):
    return SimulationConfig(
        circuit=_ccx_circuit(),
        environment=environment,
        duration_us=1.34,
        time_steps=41,
        fidelity_threshold=0.9,
        compilation_mode=mode,
        native_gate_durations_us={"H": 0.02, "RZ": 0.02, "CNOT": 0.2},
    )


def _api_payload(controls: list[int], targets: list[int]):
    return {
        "simulation_backend": "python_dense",
        "compilation_mode": "auto_decompose",
        "input_mode": "normalized",
        "circuit_config": {
            "logical_qubits": 3,
            "initial_states": [0, 0, 0],
            "columns": [{
                "step": 0,
                "gates": [{"type": "CCX", "controls": controls, "targets": targets}],
            }],
        },
        "parameters": {
            "normalized_temperature": 0.0,
            "normalized_magnetic_field": 0.0,
            "noise_level": 0.0,
            "duration_us": 1.34,
            "time_steps": 21,
            "fidelity_threshold": 0.9,
        },
    }


if __name__ == "__main__":
    unittest.main()

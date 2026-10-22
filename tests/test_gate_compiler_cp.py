import math
import unittest

import numpy as np
from pydantic import ValidationError

from api.main import SimulateRequest, build_config_from_simulate_request
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gate_compiler import AUTO_DECOMPOSE, LOGICAL_DIRECT, compile_gate_aware_circuit
from core.gates import (
    apply_unitary_to_density,
    gate_unitary,
)
from core.evolution_methods import EXPLICIT_CPTP
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from tests.physics_test_helpers import circuit_unitary, make_ideal_environment


class ControlledPhaseCompilerTests(unittest.TestCase):
    def test_rz_uses_requested_angle(self):
        theta = -0.73
        unitary = np.asarray(gate_unitary(
            GateOperation("RZ", [0], params={"theta_rad": theta, "duration_us": 0.02}),
            1,
        ))
        expected = np.diag([np.exp(-0.5j * theta), np.exp(0.5j * theta)])
        self.assertLess(float(np.max(np.abs(unitary - expected))), 1e-12)

    def test_cp_parameter_propagation_and_density_action(self):
        for theta in (0.0, 0.37, -1.2, math.pi):
            with self.subTest(theta=theta):
                direct = compile_gate_aware_circuit(_cp_circuit(theta), LOGICAL_DIRECT)
                compiled = compile_gate_aware_circuit(_cp_circuit(theta), AUTO_DECOMPOSE)
                direct_unitary = np.asarray(circuit_unitary(direct.circuit))
                compiled_unitary = np.asarray(circuit_unitary(compiled.circuit))

                phase = np.trace(direct_unitary.conj().T @ compiled_unitary) / 4.0
                self.assertAlmostEqual(abs(phase), 1.0, delta=1e-12)
                self.assertLess(
                    float(np.max(np.abs(compiled_unitary - phase * direct_unitary))),
                    1e-12,
                )

                rho = _test_density_matrix()
                direct_rho = np.asarray(apply_unitary_to_density(
                    tuple(tuple(value for value in row) for row in rho),
                    circuit_unitary(direct.circuit),
                ))
                compiled_rho = np.asarray(apply_unitary_to_density(
                    tuple(tuple(value for value in row) for row in rho),
                    circuit_unitary(compiled.circuit),
                ))
                self.assertLess(float(np.max(np.abs(direct_rho - compiled_rho))), 1e-12)

                source = compiled.diagnostics["source_map"][0]
                operations = source["compiled_operations"]
                self.assertEqual(source["rule_id"], "cp_to_rz_cnot_v1")
                self.assertEqual(
                    [(item["compiled_column"], item["gate"]) for item in operations],
                    [(0, "RZ"), (0, "RZ"), (1, "CNOT"), (2, "RZ"), (3, "CNOT")],
                )
                rz_angles = [
                    item["params"]["theta_rad"]
                    for item in operations
                    if item["gate"] == "RZ"
                ]
                np.testing.assert_allclose(
                    rz_angles,
                    [theta / 2.0, theta / 2.0, -theta / 2.0],
                    atol=0.0,
                    rtol=0.0,
                )

    def test_finite_noise_exposes_cp_decomposition_cost(self):
        environment = EnvironmentConfig(
            input_mode="physical",
            device_quality=1.0,
            temperature_mk=15.0,
            flux_noise_phi0=0.0,
            qubit_frequency_ghz=5.0,
            t1_max_us=1.0,
            tphi_max_us=1.0,
        )
        direct = run_simulation(_simulation_config(LOGICAL_DIRECT, environment, 0.83))
        compiled = run_simulation(_simulation_config(AUTO_DECOMPOSE, environment, 0.83))

        self.assertFalse(direct.issues)
        self.assertFalse(compiled.issues)
        self.assertAlmostEqual(direct.diagnostics["compiled_duration_us"], 0.2)
        self.assertAlmostEqual(compiled.diagnostics["compiled_duration_us"], 0.44)
        self.assertGreater(
            compiled.diagnostics["total_gate_duration_us"],
            direct.diagnostics["total_gate_duration_us"],
        )
        self.assertNotAlmostEqual(direct.fidelity[-1], compiled.fidelity[-1], delta=1e-6)

    def test_cp_auto_decomposition_runs_through_explicit_cptp(self):
        config = _simulation_config(AUTO_DECOMPOSE, make_ideal_environment(), -0.91)
        config.evolution_method = EXPLICIT_CPTP
        result = run_simulation(config)

        self.assertFalse(result.issues)
        self.assertEqual(
            result.diagnostics["evolution_method_resolved"],
            "explicit_cptp",
        )
        self.assertEqual(
            result.diagnostics["decomposition_rules_used"],
            ["cp_to_rz_cnot_v1"],
        )
        self.assertTrue(result.diagnostics["cptp_all_maps_passed_audit"])


class ControlledPhaseApiTests(unittest.TestCase):
    def test_api_preserves_cp_theta_rad(self):
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
                        "type": "CP",
                        "controls": [0],
                        "targets": [1],
                        "params": {"theta_rad": -0.625},
                    }],
                }],
            },
            "parameters": {
                "normalized_temperature": 0.0,
                "normalized_magnetic_field": 0.0,
                "noise_level": 0.0,
                "duration_us": 0.44,
                "time_steps": 21,
                "fidelity_threshold": 0.9,
            },
        })
        config = build_config_from_simulate_request(request)
        gate = config.circuit.columns[0].gates[0]
        self.assertAlmostEqual(gate.params["theta_rad"], -0.625)
        self.assertAlmostEqual(gate.params["duration_us"], 0.2)

    def test_api_rejects_non_finite_cp_theta(self):
        with self.assertRaises(ValidationError):
            SimulateRequest(**{
                "simulation_backend": "python_dense",
                "input_mode": "normalized",
                "circuit_config": {
                    "logical_qubits": 2,
                    "initial_states": [0, 0],
                    "columns": [{
                        "step": 0,
                        "gates": [{
                            "type": "CP",
                            "controls": [0],
                            "targets": [1],
                            "params": {"theta_rad": float("nan")},
                        }],
                    }],
                },
                "parameters": {
                    "normalized_temperature": 0.0,
                    "normalized_magnetic_field": 0.0,
                    "noise_level": 0.0,
                    "duration_us": 1.0,
                    "time_steps": 21,
                    "fidelity_threshold": 0.9,
                },
            })


def _cp_circuit(theta: float) -> CircuitConfig:
    return CircuitConfig(
        logical_qubits=2,
        initial_states=["+", "+"],
        columns=[GateColumn(0, [GateOperation(
            "CP",
            [1],
            controls=[0],
            params={"duration_us": 0.2, "theta_rad": theta},
        )])],
    )


def _test_density_matrix() -> np.ndarray:
    ket = np.asarray([1.0, 1.0j, -0.5, 0.3j], dtype=np.complex128)
    ket /= np.linalg.norm(ket)
    return np.outer(ket, ket.conj())


def _simulation_config(mode: str, environment: EnvironmentConfig, theta: float):
    return SimulationConfig(
        circuit=_cp_circuit(theta),
        environment=environment,
        duration_us=0.6,
        time_steps=41,
        fidelity_threshold=0.9,
        compilation_mode=mode,
        native_gate_durations_us={"RZ": 0.02, "CNOT": 0.2},
    )


if __name__ == "__main__":
    unittest.main()

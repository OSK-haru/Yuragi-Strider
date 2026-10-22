import math
import unittest

import numpy as np

from api.main import SimulateRequest, build_config_from_simulate_request
from core.backend_boundary import (
    PYTHON_DENSE_BACKEND,
    RUST_DENSE_PREVIEW_BACKEND,
)
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gate_compiler import (
    AUTO_DECOMPOSE,
    LOGICAL_DIRECT,
    compile_gate_aware_circuit,
)
from core.gates import (
    X,
    Y,
    apply_unitary_to_density,
    gate_unitary,
    matmul,
)
from core.results import SimulationConfig
from core.simulator import run_simulation
from tests.physics_test_helpers import circuit_unitary, make_ideal_environment


class ParameterizedRotationCompilerTests(unittest.TestCase):
    def test_direct_rx_and_ry_match_standard_definitions(self) -> None:
        theta = -0.73
        cosine = math.cos(theta / 2.0)
        sine = math.sin(theta / 2.0)
        expected = {
            "RX": np.asarray([
                [cosine, -1.0j * sine],
                [-1.0j * sine, cosine],
            ]),
            "RY": np.asarray([
                [cosine, -sine],
                [sine, cosine],
            ]),
        }

        for gate_type, expected_unitary in expected.items():
            with self.subTest(gate_type=gate_type):
                actual = np.asarray(gate_unitary(
                    GateOperation(
                        gate_type,
                        [0],
                        params={"theta_rad": theta, "duration_us": 0.02},
                    ),
                    1,
                ))
                self.assertLess(
                    float(np.max(np.abs(actual - expected_unitary))),
                    1e-12,
                )

    def test_pi_rotations_match_pauli_density_actions_and_square_to_identity(self) -> None:
        rho = _test_density_matrix()
        for gate_type, pauli in (("RX", X), ("RY", Y)):
            with self.subTest(gate_type=gate_type):
                rotation = gate_unitary(
                    GateOperation(gate_type, [0], params={"theta_rad": math.pi}),
                    1,
                )
                rotated = np.asarray(apply_unitary_to_density(_as_matrix(rho), rotation))
                pauli_rotated = np.asarray(apply_unitary_to_density(_as_matrix(rho), pauli))
                twice = np.asarray(matmul(rotation, rotation))

                self.assertLess(float(np.max(np.abs(rotated - pauli_rotated))), 1e-12)
                self.assertLess(
                    float(np.max(np.abs(twice + np.identity(2)))),
                    1e-12,
                )

    def test_auto_decomposition_matches_direct_unitary_and_density_action(self) -> None:
        for gate_type in ("RX", "RY"):
            for theta in (0.0, 0.37, -1.2, math.pi):
                with self.subTest(gate_type=gate_type, theta=theta):
                    direct = compile_gate_aware_circuit(
                        _rotation_circuit(gate_type, theta),
                        LOGICAL_DIRECT,
                    )
                    compiled = compile_gate_aware_circuit(
                        _rotation_circuit(gate_type, theta),
                        AUTO_DECOMPOSE,
                    )
                    direct_unitary = np.asarray(circuit_unitary(direct.circuit))
                    compiled_unitary = np.asarray(circuit_unitary(compiled.circuit))

                    self.assertLess(
                        float(np.max(np.abs(compiled_unitary - direct_unitary))),
                        1e-12,
                    )
                    direct_rho = np.asarray(apply_unitary_to_density(
                        _as_matrix(_test_density_matrix()),
                        circuit_unitary(direct.circuit),
                    ))
                    compiled_rho = np.asarray(apply_unitary_to_density(
                        _as_matrix(_test_density_matrix()),
                        circuit_unitary(compiled.circuit),
                    ))
                    self.assertLess(
                        float(np.max(np.abs(compiled_rho - direct_rho))),
                        1e-12,
                    )

                    source = compiled.diagnostics["source_map"][0]
                    operations = source["compiled_operations"]
                    if gate_type == "RX":
                        self.assertEqual(source["rule_id"], "rx_to_h_rz_h_v1")
                        self.assertEqual(
                            [item["gate"] for item in operations],
                            ["H", "RZ", "H"],
                        )
                        self.assertEqual(
                            [item["params"]["theta_rad"] for item in operations
                             if item["gate"] == "RZ"],
                            [theta],
                        )
                        self.assertAlmostEqual(
                            compiled.diagnostics["compiled_duration_us"],
                            0.06,
                        )
                    else:
                        self.assertEqual(
                            source["rule_id"],
                            "ry_to_rz_h_rz_h_rz_v1",
                        )
                        self.assertEqual(
                            [item["gate"] for item in operations],
                            ["RZ", "H", "RZ", "H", "RZ"],
                        )
                        np.testing.assert_allclose(
                            [item["params"]["theta_rad"] for item in operations
                             if item["gate"] == "RZ"],
                            [-math.pi / 2.0, theta, math.pi / 2.0],
                            atol=0.0,
                            rtol=0.0,
                        )
                        self.assertAlmostEqual(
                            compiled.diagnostics["compiled_duration_us"],
                            0.10,
                        )

    def test_auto_decomposed_rotations_run_in_cptp_and_rust_backends(self) -> None:
        python_result = run_simulation(_simulation_config(PYTHON_DENSE_BACKEND))
        rust_result = run_simulation(_simulation_config(RUST_DENSE_PREVIEW_BACKEND))

        for result in (python_result, rust_result):
            self.assertFalse(result.issues)
            self.assertEqual(
                result.diagnostics["decomposition_rules_used"],
                ["rx_to_h_rz_h_v1", "ry_to_rz_h_rz_h_rz_v1"],
            )
            self.assertTrue(result.diagnostics["cptp_all_maps_passed_audit"])
            self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)

        for state in python_result.output_probabilities:
            self.assertAlmostEqual(
                rust_result.output_probabilities[state],
                python_result.output_probabilities[state],
                delta=1e-10,
            )

    def test_rotation_populations_are_present_in_after_circuit_snapshots(self) -> None:
        expectations = {
            "RX": (math.pi / 2.0, [0.5, 0.5]),
            "RY": (math.pi, [0.0, 1.0]),
        }
        for gate_type, (theta, expected_diagonal) in expectations.items():
            with self.subTest(gate_type=gate_type):
                result = run_simulation(SimulationConfig(
                    circuit=_rotation_circuit(gate_type, theta),
                    environment=make_ideal_environment(),
                    duration_us=0.04,
                    time_steps=11,
                    fidelity_threshold=0.9,
                    compilation_mode=LOGICAL_DIRECT,
                    snapshot_options={
                        "enabled": True,
                        "uniform_count": 0,
                        "include_initial": True,
                        "include_final": True,
                        "include_column_boundaries": True,
                        "include_after_circuit": True,
                    },
                ))
                after_circuit = next(
                    snapshot for snapshot in result.state_snapshots
                    if snapshot.kind == "after_circuit"
                )
                actual_diagonal = [
                    after_circuit.density_matrix[index][index].real
                    for index in range(2)
                ]
                np.testing.assert_allclose(
                    actual_diagonal,
                    expected_diagonal,
                    atol=1e-11,
                    rtol=0.0,
                )

    def test_runtime_unitary_cache_distinguishes_rotation_angles(self) -> None:
        circuit = CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(0, [GateOperation(
                    "RX", [0], params={"duration_us": 0.02, "theta_rad": 0.31},
                )]),
                GateColumn(1, [GateOperation(
                    "RX", [0], params={"duration_us": 0.02, "theta_rad": -0.31},
                )]),
            ],
        )
        result = run_simulation(SimulationConfig(
            circuit=circuit,
            environment=make_ideal_environment(),
            duration_us=0.04,
            time_steps=11,
            fidelity_threshold=0.9,
            compilation_mode=LOGICAL_DIRECT,
        ))

        self.assertFalse(result.issues)
        self.assertAlmostEqual(result.output_probabilities["0"], 1.0, delta=1e-10)
        self.assertAlmostEqual(result.output_probabilities["1"], 0.0, delta=1e-10)


class ParameterizedRotationApiTests(unittest.TestCase):
    def test_api_preserves_angles_and_uses_rotation_duration_defaults(self) -> None:
        request = SimulateRequest(**{
            "simulation_backend": "python_dense",
            "compilation_mode": "auto_decompose",
            "input_mode": "normalized",
            "circuit_config": {
                "logical_qubits": 2,
                "initial_states": [0, 0],
                "columns": [
                    {"step": 0, "gates": [{
                        "type": "RX",
                        "targets": [0],
                        "params": {"theta_rad": -0.625},
                    }]},
                    {"step": 1, "gates": [{
                        "type": "RY",
                        "targets": [1],
                        "params": {"theta_rad": 0.875},
                    }]},
                ],
            },
            "parameters": {
                "normalized_temperature": 0.0,
                "normalized_magnetic_field": 0.0,
                "noise_level": 0.0,
                "duration_us": 0.16,
                "time_steps": 21,
                "fidelity_threshold": 0.9,
            },
        })
        config = build_config_from_simulate_request(request)
        rx_gate = config.circuit.columns[0].gates[0]
        ry_gate = config.circuit.columns[1].gates[0]

        self.assertAlmostEqual(rx_gate.params["theta_rad"], -0.625)
        self.assertAlmostEqual(ry_gate.params["theta_rad"], 0.875)
        self.assertAlmostEqual(rx_gate.params["duration_us"], 0.02)
        self.assertAlmostEqual(ry_gate.params["duration_us"], 0.02)


def _rotation_circuit(gate_type: str, theta: float) -> CircuitConfig:
    return CircuitConfig(
        logical_qubits=1,
        initial_states=["0"],
        columns=[GateColumn(0, [GateOperation(
            gate_type,
            [0],
            params={"duration_us": 0.02, "theta_rad": theta},
        )])],
    )


def _simulation_config(backend: str) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(0, [GateOperation(
                    "RX", [0], params={"duration_us": 0.02, "theta_rad": 0.41},
                )]),
                GateColumn(1, [GateOperation(
                    "RY", [0], params={"duration_us": 0.02, "theta_rad": -0.83},
                )]),
            ],
        ),
        environment=make_ideal_environment(),
        duration_us=0.16,
        time_steps=41,
        fidelity_threshold=0.9,
        simulation_backend=backend,
        evolution_method="explicit_cptp",
        compilation_mode=AUTO_DECOMPOSE,
        native_gate_durations_us={"H": 0.02, "RZ": 0.02},
    )


def _test_density_matrix() -> np.ndarray:
    ket = np.asarray([1.0, 0.3 + 0.7j], dtype=np.complex128)
    ket /= np.linalg.norm(ket)
    return np.outer(ket, ket.conj())


def _as_matrix(array: np.ndarray):
    return tuple(tuple(complex(value) for value in row) for row in array)


if __name__ == "__main__":
    unittest.main()

import types
import unittest
from unittest.mock import patch

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import ReadoutErrorConfig
from core.rust_circuit_runtime import (
    apply_readout_rust,
    execute_ideal_circuit_rust,
    execute_post_circuit_gates_rust,
)


class RustCircuitRuntimeBoundaryTest(unittest.TestCase):
    def test_ideal_circuit_and_readout_are_sent_in_one_coarse_call(self) -> None:
        captured = {}

        def execute(*args):
            captured["args"] = args
            return [0.5, 0.0, 0.0, 0.5], [0.45, 0.05, 0.05, 0.45]

        module = types.SimpleNamespace(execute_ideal_circuit_v1=execute)
        circuit = CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            classical_bits=1,
            columns=[
                GateColumn(0, [GateOperation("H", [0])]),
                GateColumn(1, [GateOperation("CNOT", [1], controls=[0])]),
            ],
        )
        readout = ReadoutErrorConfig(per_qubit=[{"p10": 0.1, "p01": 0.05}, {"p10": 0.2, "p01": 0.1}])
        with patch("core.rust_circuit_runtime.importlib.import_module", return_value=module):
            ideal, observed = execute_ideal_circuit_rust(circuit, readout)

        self.assertEqual(captured["args"][0:3], (2, 1, ["0", "0"]))
        self.assertEqual(len(captured["args"][3]), 2)
        self.assertEqual(captured["args"][3][0][0], "H")
        self.assertEqual(captured["args"][3][1][1:3], ([1], [0]))
        self.assertEqual(captured["args"][4], [(0.1, 0.05), (0.2, 0.1)])
        self.assertEqual(ideal["00"], 0.5)
        self.assertEqual(observed["11"], 0.45)

    def test_density_path_readout_uses_single_rust_call(self) -> None:
        captured = {}

        def apply(probabilities, n_qubits, assignments):
            captured.update(probabilities=probabilities, n=n_qubits, assignments=assignments)
            return probabilities

        module = types.SimpleNamespace(apply_binary_readout_v1=apply)
        probabilities = {"00": 0.5, "01": 0.0, "10": 0.0, "11": 0.5}
        with patch("core.rust_circuit_runtime.importlib.import_module", return_value=module):
            actual = apply_readout_rust(probabilities, 2, [(0.1, 0.2), (0.3, 0.4)])

        self.assertEqual(actual, probabilities)
        self.assertEqual(captured["n"], 2)
        self.assertEqual(captured["assignments"], [(0.1, 0.2), (0.3, 0.4)])

    def test_post_circuit_operations_cross_the_boundary_as_one_batch(self) -> None:
        captured = {}

        def execute(*args):
            captured["args"] = args
            flat = [0.0] * 32
            for offset in (0, 6, 24, 30):
                flat[offset] = 0.5
            return flat

        module = types.SimpleNamespace(execute_ideal_density_circuit_v1=execute)
        circuit = CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(0, [GateOperation("H", [0])]),
                GateColumn(1, [GateOperation("CNOT", [1], controls=[0])]),
            ],
        )
        with patch("core.rust_circuit_runtime.importlib.import_module", return_value=module):
            result = execute_post_circuit_gates_rust(circuit)

        self.assertEqual(captured["args"][0:2], (2, ["0", "0"]))
        self.assertEqual(len(captured["args"][2]), 2)
        self.assertEqual(result[0][0].real, 0.5)
        self.assertEqual(result[0][3].real, 0.5)

    def test_message_gate_keeps_its_animation_parameter_contract(self) -> None:
        captured = {}

        def execute(*args):
            captured["args"] = args
            return [1.0, 0.0], [1.0, 0.0]

        module = types.SimpleNamespace(execute_ideal_circuit_v1=execute)
        circuit = CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[GateColumn(0, [GateOperation("MESSAGE", [0])])],
        )
        with patch("core.rust_circuit_runtime.importlib.import_module", return_value=module):
            execute_ideal_circuit_rust(circuit, None)
        self.assertEqual(captured["args"][3][0][3], 0.0)


if __name__ == "__main__":
    unittest.main()

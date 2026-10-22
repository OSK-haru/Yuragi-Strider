import math
import unittest

import numpy as np
from pydantic import ValidationError

from api.main import SimulateRequest, build_config_from_simulate_request
from core.capabilities import GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL, SUPPORTED_GATES
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gate_compiler import AUTO_DECOMPOSE, LOGICAL_DIRECT, compile_gate_aware_circuit
from core.gates import (
    INVOLUTION_TOLERANCE,
    expand_phase_oracle,
    gate_duration_us,
)
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from core.statevector import execute_statevector_branches
from core.validation import validate_simulation_config
from tests.physics_test_helpers import circuit_unitary


class PhaseOracleMatrixTests(unittest.TestCase):
    def test_marks_exactly_the_requested_basis_states(self):
        for n_qubits, targets, marked in (
            (3, [0, 1, 2], 5), (4, [3, 1, 0], 6), (5, [4, 2, 0], 5),
        ):
            matrix = np.asarray(expand_phase_oracle(targets, n_qubits, marked))
            self.assertLess(
                float(np.max(np.abs(matrix - np.diag(np.diag(matrix))))), 1e-15,
                "the oracle must stay diagonal",
            )
            bits = format(marked, f"0{len(targets)}b")
            expected = {
                index for index in range(2 ** n_qubits)
                if all(
                    format(index, f"0{n_qubits}b")[qubit] == bits[position]
                    for position, qubit in enumerate(targets)
                )
            }
            negatives = {
                index for index, value in enumerate(np.diag(matrix).real) if value < 0
            }
            self.assertEqual(negatives, expected)

    def test_is_hermitian_and_involutory(self):
        # This is what puts the oracle on the gate-aware involution branch.
        for n_qubits in (1, 2, 3, 4, 5):
            matrix = np.asarray(
                expand_phase_oracle(list(range(n_qubits)), n_qubits, 1)
            )
            identity = np.eye(2 ** n_qubits)
            self.assertLessEqual(
                float(np.max(np.abs(matrix.conj().T - matrix))), INVOLUTION_TOLERANCE,
            )
            self.assertLessEqual(
                float(np.max(np.abs(matrix @ matrix - identity))), INVOLUTION_TOLERANCE,
            )

    def test_rejects_bad_registers_and_marked_values(self):
        with self.assertRaisesRegex(ValueError, "must be different"):
            expand_phase_oracle([1, 1], 2, 0)
        with self.assertRaisesRegex(ValueError, "at least one target"):
            expand_phase_oracle([], 2, 0)
        with self.assertRaisesRegex(ValueError, "outside range"):
            expand_phase_oracle([0, 2], 2, 0)
        with self.assertRaisesRegex(ValueError, "outside the register range"):
            expand_phase_oracle([0, 1], 2, 4)

    def test_default_duration_scales_with_the_register(self):
        for register_size in range(1, 6):
            gate = GateOperation("ORACLE", list(range(register_size)))
            self.assertAlmostEqual(gate_duration_us(gate), 0.2 * register_size)


class PhaseOracleCompilerTests(unittest.TestCase):
    def test_decomposition_matches_the_direct_unitary_up_to_global_phase(self):
        for n_qubits, targets, marked in (
            (1, [0], 0), (1, [0], 1),
            (2, [0, 1], 2), (3, [0, 1, 2], 5), (3, [2, 0, 1], 0),
            (4, [0, 1, 2, 3], 11), (5, [0, 1, 2, 3, 4], 22), (5, [4, 2, 0], 5),
        ):
            circuit = _oracle_circuit(n_qubits, targets, marked)
            direct = np.asarray(circuit_unitary(
                compile_gate_aware_circuit(circuit, LOGICAL_DIRECT).circuit
            ))
            compiled = np.asarray(circuit_unitary(
                compile_gate_aware_circuit(circuit, AUTO_DECOMPOSE).circuit
            ))
            phase = np.trace(direct.conj().T @ compiled) / 2 ** n_qubits
            self.assertAlmostEqual(abs(phase), 1.0, delta=1e-12)
            self.assertLess(float(np.max(np.abs(compiled - phase * direct))), 1e-12)

    def test_gray_code_ordering_keeps_the_cnot_count_at_two_to_the_m(self):
        # A parity ladder per subset would cost O(m 2^m) CNOTs; Gray-code
        # ordering changes one element per step, so the count stays at 2^m - 2.
        for register_size in (2, 3, 4, 5):
            targets = list(range(register_size))
            compiled = compile_gate_aware_circuit(
                _oracle_circuit(register_size, targets, 2 ** register_size - 1),
                AUTO_DECOMPOSE,
            )
            operations = compiled.diagnostics["source_map"][0]["compiled_operations"]
            counts = {
                gate: sum(1 for item in operations if item["gate"] == gate)
                for gate in ("CNOT", "RZ", "X")
            }
            self.assertEqual(counts["CNOT"], 2 ** register_size - 2)
            self.assertEqual(counts["RZ"], 2 ** register_size - 1)
            # marking the all-ones state needs no X conjugation at all
            self.assertEqual(counts["X"], 0)

    def test_decomposition_only_emits_native_gates_and_records_one_rule(self):
        compiled = compile_gate_aware_circuit(
            _oracle_circuit(3, [0, 1, 2], 5), AUTO_DECOMPOSE,
        )
        source = compiled.diagnostics["source_map"][0]
        self.assertEqual(source["rule_id"], "oracle_to_x_mcz_graycode_v1")
        self.assertEqual(source["source_gate"], "ORACLE")
        self.assertEqual(
            {item["gate"] for item in source["compiled_operations"]},
            {"X", "CNOT", "RZ"},
        )

    def test_marking_a_zero_bit_adds_the_x_conjugation_pair(self):
        compiled = compile_gate_aware_circuit(
            _oracle_circuit(3, [0, 1, 2], 5), AUTO_DECOMPOSE,
        )
        operations = compiled.diagnostics["source_map"][0]["compiled_operations"]
        # |101> has one zero bit, so exactly one qubit is flipped twice.
        flips = [item["targets"][0] for item in operations if item["gate"] == "X"]
        self.assertEqual(flips, [1, 1])


class GroverSearchTests(unittest.TestCase):
    def test_success_probability_follows_the_analytic_curve(self):
        # A Grover iteration is  D . O_m  with  D = -(H^n . O_0 . H^n), so the
        # whole algorithm needs only H and the oracle.
        for n_qubits, marked in ((2, 3), (3, 5), (4, 11), (5, 22)):
            dimension = 2 ** n_qubits
            theta = math.asin(1.0 / math.sqrt(dimension))
            label = format(marked, f"0{n_qubits}b")
            for iterations in range(0, int(math.pi / (4 * theta)) + 3):
                probabilities = execute_statevector_branches(
                    _grover_circuit(n_qubits, marked, iterations)
                ).output_probabilities
                self.assertAlmostEqual(
                    probabilities.get(label, 0.0),
                    math.sin((2 * iterations + 1) * theta) ** 2,
                    delta=1e-12,
                    msg=f"n={n_qubits} k={iterations}",
                )

    def test_five_qubit_search_peaks_at_the_optimal_iteration(self):
        marked = 22
        label = format(marked, "05b")
        peaks = [
            execute_statevector_branches(
                _grover_circuit(5, marked, iterations)
            ).output_probabilities.get(label, 0.0)
            for iterations in range(0, 8)
        ]
        self.assertEqual(peaks.index(max(peaks)), 4)
        self.assertGreater(max(peaks), 0.999)
        # over-rotating past the optimum has to fall back off again
        self.assertLess(peaks[7], peaks[4])

    def test_grover_runs_through_the_gate_aware_cptp_path(self):
        config = SimulationConfig(
            circuit=_grover_circuit(3, 5, 2),
            environment=EnvironmentConfig(
                input_mode="physical", ideal_reference=True, device_quality=1.0,
                temperature_mk=0.0, flux_noise_phi0=0.0,
            ),
            duration_us=2.0,
            time_steps=11,
            fidelity_threshold=0.9,
            model=GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
            compilation_mode=LOGICAL_DIRECT,
        )
        config.evolution_method = "explicit_cptp"
        result = run_simulation(config)
        self.assertFalse(result.issues)
        self.assertTrue(result.diagnostics["cptp_all_maps_passed_audit"])
        self.assertAlmostEqual(result.output_probabilities["101"], 0.9453125, delta=1e-9)

    def test_decomposing_grover_costs_more_exposure_than_direct_execution(self):
        circuit = _grover_circuit(5, 22, 1)
        direct = compile_gate_aware_circuit(circuit, LOGICAL_DIRECT).diagnostics
        compiled = compile_gate_aware_circuit(circuit, AUTO_DECOMPOSE).diagnostics
        self.assertEqual(
            compiled["decomposition_rules_used"], ["oracle_to_x_mcz_graycode_v1"],
        )
        self.assertGreater(compiled["compiled_depth"], direct["compiled_depth"])
        self.assertGreater(
            compiled["compiled_duration_us"], direct["compiled_duration_us"],
        )


class PhaseOracleValidationTests(unittest.TestCase):
    def test_oracle_is_declared_in_the_core_capabilities(self):
        self.assertIn("ORACLE", SUPPORTED_GATES)

    def test_out_of_range_marked_index_is_reported(self):
        config = SimulationConfig(
            circuit=_oracle_circuit(3, [0, 1], 7),
            environment=EnvironmentConfig(input_mode="physical"),
            duration_us=1.0, time_steps=11, fidelity_threshold=0.9,
        )
        codes = {issue.code for issue in validate_simulation_config(config)}
        self.assertIn("ORACLE_MARKED_INDEX_OUT_OF_RANGE", codes)

    def test_controls_are_reported(self):
        config = SimulationConfig(
            circuit=CircuitConfig(
                logical_qubits=3,
                initial_states=["0"] * 3,
                columns=[GateColumn(0, [GateOperation(
                    "ORACLE", [0, 1], controls=[2], params={"marked_index": 1.0},
                )])],
            ),
            environment=EnvironmentConfig(input_mode="physical"),
            duration_us=1.0, time_steps=11, fidelity_threshold=0.9,
        )
        codes = {issue.code for issue in validate_simulation_config(config)}
        self.assertIn("ORACLE_REJECTS_CONTROLS", codes)


class PhaseOracleApiTests(unittest.TestCase):
    def test_api_accepts_an_oracle_over_arbitrary_qubits(self):
        request = SimulateRequest(**_api_payload([3, 0, 2], 5))
        config = build_config_from_simulate_request(request)
        gate = config.circuit.columns[0].gates[0]
        self.assertEqual(gate.type, "ORACLE")
        self.assertEqual(gate.targets, [3, 0, 2])
        self.assertEqual(gate.controls, [])
        self.assertAlmostEqual(gate.params["marked_index"], 5.0)
        self.assertAlmostEqual(gate.params["duration_us"], 0.6)

    def test_api_rejects_an_out_of_range_marked_index(self):
        with self.assertRaises(ValidationError):
            SimulateRequest(**_api_payload([0, 1], 4))

    def test_api_rejects_a_fractional_marked_index(self):
        with self.assertRaises(ValidationError):
            SimulateRequest(**_api_payload([0, 1], 1.5))


def _oracle_circuit(n_qubits, targets, marked):
    return CircuitConfig(
        logical_qubits=n_qubits,
        initial_states=["0"] * n_qubits,
        columns=[GateColumn(0, [GateOperation(
            "ORACLE", list(targets), params={"marked_index": float(marked)},
        )])],
    )


def _grover_circuit(n_qubits, marked_index, iterations):
    register = list(range(n_qubits))
    columns: list[GateColumn] = []

    def hadamards():
        columns.append(GateColumn(
            len(columns), [GateOperation("H", [q]) for q in register],
        ))

    def oracle(index):
        columns.append(GateColumn(len(columns), [GateOperation(
            "ORACLE", register, params={"marked_index": float(index)},
        )]))

    hadamards()
    for _ in range(iterations):
        oracle(marked_index)
        hadamards()
        oracle(0)
        hadamards()
    return CircuitConfig(
        logical_qubits=n_qubits, initial_states=["0"] * n_qubits, columns=columns,
    )


def _api_payload(targets, marked):
    return {
        "simulation_backend": "python_dense",
        "compilation_mode": "auto_decompose",
        "input_mode": "normalized",
        "circuit_config": {
            "logical_qubits": 4,
            "initial_states": [0, 0, 0, 0],
            "columns": [{
                "step": 0,
                "gates": [{
                    "type": "ORACLE",
                    "targets": list(targets),
                    "controls": [],
                    "params": {"marked_index": marked},
                }],
            }],
        },
        "parameters": {
            "normalized_temperature": 0.0,
            "normalized_magnetic_field": 0.0,
            "noise_level": 0.0,
            "duration_us": 1.0,
            "time_steps": 11,
            "fidelity_threshold": 0.9,
        },
    }


if __name__ == "__main__":
    unittest.main()

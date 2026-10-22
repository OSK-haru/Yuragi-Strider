import math
import unittest

import numpy as np
from pydantic import ValidationError

from api.main import SimulateRequest, build_config_from_simulate_request
from core.capabilities import GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL, SUPPORTED_GATES
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.gate_compiler import AUTO_DECOMPOSE, LOGICAL_DIRECT, compile_gate_aware_circuit
from core.gates import (
    apply_gate_operation,
    apply_unitary_to_density,
    initial_density_matrix,
    reduced_density_matrix,
    effective_hamiltonian_from_unitary,
    expand_qft,
    gate_duration_us,
    unitary_from_hamiltonian,
)
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from core.statevector import execute_statevector_branches
from core.validation import validate_simulation_config
from tests.physics_test_helpers import circuit_unitary


class QftUnitaryTests(unittest.TestCase):
    def test_full_register_matches_the_textbook_matrix(self):
        for register_size in range(1, 6):
            dimension = 2 ** register_size
            indices = np.arange(dimension)
            expected = np.exp(
                2.0j * np.pi * np.outer(indices, indices) / dimension
            ) / np.sqrt(dimension)
            actual = np.asarray(expand_qft(list(range(register_size)), register_size))
            self.assertLess(float(np.max(np.abs(actual - expected))), 1e-12)

    def test_arbitrary_subset_and_ordering_leave_other_qubits_untouched(self):
        for n_qubits, targets in ((3, [2, 0]), (4, [3, 1, 0]), (5, [4, 2, 1, 3])):
            actual = np.asarray(expand_qft(targets, n_qubits))
            self.assertLess(
                float(np.max(np.abs(
                    actual.conj().T @ actual - np.eye(2 ** n_qubits)
                ))),
                1e-12,
            )
            self.assertLess(
                float(np.max(np.abs(actual - _reference_qft(targets, n_qubits)))),
                1e-12,
            )

    def test_target_order_selects_the_most_significant_register_bit(self):
        forward = np.asarray(expand_qft([0, 1], 2))
        reversed_register = np.asarray(expand_qft([1, 0], 2))
        self.assertGreater(float(np.max(np.abs(forward - reversed_register))), 0.1)
        self.assertLess(
            float(np.max(np.abs(reversed_register - _reference_qft([1, 0], 2)))),
            1e-12,
        )

    def test_register_order_matches_a_little_endian_simulator_on_reversed_wires(self):
        # This project labels basis states with q0 as the most significant bit,
        # so targets[0] is the register MSB. Quirk and Qiskit put q0 in the
        # least significant position instead. The two agree exactly once the
        # wire list is reversed; pinning that here keeps the convention honest
        # when results are cross-checked against an external simulator.
        for n_qubits, targets in (
            (2, [0, 1]), (3, [0, 1, 2]), (4, [0, 1, 2, 3]), (4, [3, 1, 0]),
        ):
            self.assertLess(
                float(np.max(np.abs(
                    np.asarray(expand_qft(targets, n_qubits))
                    - _little_endian_reference_qft(list(reversed(targets)), n_qubits)
                ))),
                1e-12,
            )

    def test_reversing_the_register_is_a_different_gate(self):
        # Guards against "fixing" an endianness mismatch by assuming the gate is
        # symmetric under reversing targets. It is not.
        forward = np.asarray(expand_qft([0, 1, 2], 3))
        reversed_register = np.asarray(expand_qft([2, 1, 0], 3))
        self.assertGreater(float(np.max(np.abs(forward - reversed_register))), 0.1)

    def test_repeated_and_out_of_range_targets_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be different"):
            expand_qft([1, 1], 2)
        with self.assertRaisesRegex(ValueError, "at least one target"):
            expand_qft([], 2)
        with self.assertRaisesRegex(ValueError, "outside range"):
            expand_qft([0, 2], 2)

    def test_default_duration_scales_with_the_spanned_register(self):
        for register_size in range(1, 6):
            gate = GateOperation("QFT", list(range(register_size)))
            self.assertAlmostEqual(gate_duration_us(gate), 0.2 * register_size)
        explicit = GateOperation("QFT", [0, 1], params={"duration_us": 0.9})
        self.assertAlmostEqual(gate_duration_us(explicit), 0.9)


class QftEffectiveHamiltonianTests(unittest.TestCase):
    def test_degenerate_qft_spectrum_still_yields_a_hermitian_generator(self):
        # QFT eigenvalues are only the four fourth roots of unity, so every
        # register above two qubits has degenerate eigenspaces.
        for register_size in range(1, 6):
            unitary = expand_qft(list(range(register_size)), register_size)
            duration = 0.2 * register_size
            hamiltonian = np.asarray(
                effective_hamiltonian_from_unitary(unitary, duration)
            )
            self.assertLess(
                float(np.max(np.abs(hamiltonian - hamiltonian.conj().T))),
                1e-9,
            )
            reconstructed = np.asarray(
                unitary_from_hamiltonian(_as_matrix(hamiltonian), duration)
            )
            self.assertLess(
                float(np.max(np.abs(reconstructed - np.asarray(unitary)))),
                1e-9,
            )


class QftGateCompilerTests(unittest.TestCase):
    def test_decomposition_matches_the_direct_unitary_up_to_global_phase(self):
        for n_qubits, targets in ((1, [0]), (2, [0, 1]), (3, [0, 1, 2]), (4, [3, 1, 0])):
            circuit = _qft_circuit(n_qubits, targets)
            direct = np.asarray(circuit_unitary(
                compile_gate_aware_circuit(circuit, LOGICAL_DIRECT).circuit
            ))
            compiled = np.asarray(circuit_unitary(
                compile_gate_aware_circuit(circuit, AUTO_DECOMPOSE).circuit
            ))
            phase = np.trace(direct.conj().T @ compiled) / 2 ** n_qubits
            self.assertAlmostEqual(abs(phase), 1.0, delta=1e-12)
            self.assertLess(
                float(np.max(np.abs(compiled - phase * direct))),
                1e-12,
            )

    def test_density_channel_is_identical_despite_the_global_phase(self):
        circuit = _qft_circuit(3, [0, 1, 2])
        rho = _test_density_matrix(3)
        direct = np.asarray(apply_unitary_to_density(
            rho, circuit_unitary(compile_gate_aware_circuit(circuit, LOGICAL_DIRECT).circuit)
        ))
        compiled = np.asarray(apply_unitary_to_density(
            rho, circuit_unitary(compile_gate_aware_circuit(circuit, AUTO_DECOMPOSE).circuit)
        ))
        self.assertLess(float(np.max(np.abs(direct - compiled))), 1e-12)

    def test_decomposition_only_emits_native_gates_and_records_one_rule(self):
        compiled = compile_gate_aware_circuit(_qft_circuit(3, [0, 1, 2]), AUTO_DECOMPOSE)
        source = compiled.diagnostics["source_map"][0]
        operations = source["compiled_operations"]

        self.assertEqual(source["rule_id"], "qft_to_h_cp_swap_v1")
        self.assertEqual(compiled.diagnostics["decomposition_rules_used"], ["qft_to_h_cp_swap_v1"])
        self.assertEqual(source["source_gate"], "QFT")
        self.assertEqual(source["targets"], [0, 1, 2])
        self.assertEqual({item["gate"] for item in operations}, {"H", "RZ", "CNOT"})
        # 3 register H, 3 CP -> 3 RZ each, 3 bit-reversal CNOT.
        self.assertEqual(
            {gate: sum(item["gate"] == gate for item in operations) for gate in ("H", "CNOT", "RZ")},
            {"H": 3, "CNOT": 9, "RZ": 9},
        )
        self.assertEqual(compiled.diagnostics["compiled_depth"], 18)
        self.assertEqual(compiled.diagnostics["compiled_gate_count"], 21)
        self.assertAlmostEqual(compiled.diagnostics["compiled_duration_us"], 1.98)

    def test_controlled_phase_angles_halve_along_the_ladder(self):
        compiled = compile_gate_aware_circuit(_qft_circuit(4, [0, 1, 2, 3]), AUTO_DECOMPOSE)
        operations = compiled.diagnostics["source_map"][0]["compiled_operations"]
        # Each CP(theta) contributes RZ angles +theta/2, +theta/2, -theta/2.
        positive_angles = sorted(
            round(item["params"]["theta_rad"], 12)
            for item in operations
            if item["gate"] == "RZ" and item["params"]["theta_rad"] > 0
        )
        expected = sorted(
            round(math.pi / float(2 ** offset) / 2.0, 12)
            for offset in (1, 2, 3, 1, 2, 1)
            for _ in range(2)
        )
        self.assertEqual(positive_angles, expected)

    def test_disjoint_qft_registers_share_compiled_layers(self):
        circuit = CircuitConfig(
            logical_qubits=4,
            initial_states=["0", "0", "0", "0"],
            columns=[GateColumn(0, [
                GateOperation("QFT", [0, 1]),
                GateOperation("QFT", [2, 3]),
            ])],
        )
        compiled = compile_gate_aware_circuit(circuit, AUTO_DECOMPOSE)
        # Two independent 2-qubit QFTs run in parallel, so the depth matches one.
        self.assertEqual(compiled.diagnostics["compiled_depth"], 9)
        self.assertEqual(compiled.diagnostics["compiled_gate_count"], 20)

    def test_single_qubit_qft_is_a_bare_hadamard(self):
        compiled = compile_gate_aware_circuit(_qft_circuit(1, [0]), AUTO_DECOMPOSE)
        operations = compiled.diagnostics["source_map"][0]["compiled_operations"]
        self.assertEqual([item["gate"] for item in operations], ["H"])


class QftExecutionTests(unittest.TestCase):
    def test_basis_state_maps_to_a_uniform_distribution(self):
        for mode in (LOGICAL_DIRECT, AUTO_DECOMPOSE):
            config = _simulation_config(3, [0, 1, 2], mode)
            config.evolution_method = "explicit_cptp"
            result = run_simulation(config)
            self.assertFalse(result.issues)
            self.assertTrue(result.diagnostics["cptp_all_maps_passed_audit"])
            self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-10)
            for probability in result.output_probabilities.values():
                self.assertAlmostEqual(probability, 0.125, delta=1e-10)

    def test_decomposition_costs_more_exposure_time_than_direct_execution(self):
        direct = run_simulation(_simulation_config(3, [0, 1, 2], LOGICAL_DIRECT))
        compiled = run_simulation(_simulation_config(3, [0, 1, 2], AUTO_DECOMPOSE))
        self.assertAlmostEqual(direct.diagnostics["compiled_duration_us"], 0.6)
        self.assertAlmostEqual(compiled.diagnostics["compiled_duration_us"], 1.98)
        self.assertGreater(
            compiled.diagnostics["total_gate_duration_us"],
            direct.diagnostics["total_gate_duration_us"],
        )

    def test_statevector_backend_matches_the_dense_unitary(self):
        n_qubits, targets = 4, [3, 0, 2]
        circuit = CircuitConfig(
            logical_qubits=n_qubits,
            initial_states=["1", "0", "1", "0"],
            columns=[GateColumn(0, [GateOperation("QFT", targets)])],
        )
        result = execute_statevector_branches(circuit)

        initial = np.zeros(2 ** n_qubits, dtype=np.complex128)
        initial[int("1010", 2)] = 1.0
        expected = np.abs(np.asarray(expand_qft(targets, n_qubits)) @ initial) ** 2
        for index, probability in enumerate(expected):
            label = format(index, f"0{n_qubits}b")
            self.assertAlmostEqual(
                result.output_probabilities.get(label, 0.0),
                float(probability),
                delta=1e-12,
            )

    def test_reduced_bloch_vectors_match_an_external_simulator(self):
        # H(q0), CNOT(q0->q1), then a 4-qubit QFT. The expected numbers were
        # read off Quirk's Bloch tooltip for the same circuit (its QFT on wires
        # 0-3 is our reversed register) and independently reproduced with
        # Qiskit's partial_trace. Pins the reduced-state path as well as QFT.
        expected = [
            (0.6913, 0.4619, 0.0000),
            (0.1464, 0.3536, 0.0000),
            (0.5000, -0.5000, 0.0000),
            (0.0000, -0.1871, 0.1250),
        ]
        rho = initial_density_matrix(["0"] * 4)
        for gate in (
            GateOperation("H", [0]),
            GateOperation("CNOT", [1], controls=[0]),
            GateOperation("QFT", [3, 2, 1, 0]),
        ):
            rho = apply_gate_operation(rho, gate, 4)

        for qubit, expected_vector in enumerate(expected):
            reduced = np.asarray(reduced_density_matrix(rho, 4, qubit))
            actual = tuple(
                float(np.trace(reduced @ pauli).real)
                for pauli in (
                    np.array([[0, 1], [1, 0]], dtype=complex),
                    np.array([[0, -1j], [1j, 0]], dtype=complex),
                    np.array([[1, 0], [0, -1]], dtype=complex),
                )
            )
            for axis, (got, want) in enumerate(zip(actual, expected_vector)):
                self.assertAlmostEqual(got, want, delta=1e-4, msg=f"q{qubit} axis {axis}")

    def test_register_order_moves_where_the_entanglement_lands(self):
        # Reversing the register is not cosmetic: with j in {0, 12} the two most
        # significant output wires see the same phase and factor out into pure
        # states, so |r| = 1 there. That is correct, not a lost CNOT.
        def bloch_lengths(targets):
            rho = initial_density_matrix(["0"] * 4)
            for gate in (
                GateOperation("H", [0]),
                GateOperation("CNOT", [1], controls=[0]),
                GateOperation("QFT", list(targets)),
            ):
                rho = apply_gate_operation(rho, gate, 4)
            lengths = []
            for qubit in range(4):
                reduced = np.asarray(reduced_density_matrix(rho, 4, qubit))
                lengths.append(float(np.sqrt(max(
                    0.0, 2.0 * float(np.trace(reduced @ reduced).real) - 1.0,
                ))))
            return lengths

        msb_first = bloch_lengths([0, 1, 2, 3])
        lsb_first = bloch_lengths([3, 2, 1, 0])
        for got, want in zip(msb_first, [1.0, 1.0, 0.7071, 0.7071]):
            self.assertAlmostEqual(got, want, delta=1e-4)
        for got, want in zip(lsb_first, [0.8315, 0.3827, 0.7071, 0.2250]):
            self.assertAlmostEqual(got, want, delta=1e-4)

    def test_repeated_qft_targets_are_reported_as_a_validation_issue(self):
        config = _simulation_config(3, [0, 1], LOGICAL_DIRECT)
        config.circuit.columns[0].gates[0].targets = [1, 1]
        codes = {issue.code for issue in validate_simulation_config(config)}
        self.assertIn("QFT_TARGETS_MUST_DIFFER", codes)

    def test_qft_is_declared_in_the_core_capabilities(self):
        self.assertIn("QFT", SUPPORTED_GATES)


class QftApiTests(unittest.TestCase):
    def test_api_accepts_a_register_spanning_arbitrary_qubits(self):
        request = SimulateRequest(**_api_payload([3, 0, 2]))
        config = build_config_from_simulate_request(request)
        gate = config.circuit.columns[0].gates[0]
        self.assertEqual(gate.type, "QFT")
        self.assertEqual(gate.targets, [3, 0, 2])
        self.assertEqual(gate.controls, [])
        self.assertAlmostEqual(gate.params["duration_us"], 0.6)

    def test_api_rejects_repeated_targets_and_controls(self):
        with self.assertRaises(ValidationError):
            SimulateRequest(**_api_payload([0, 0, 2]))
        with self.assertRaises(ValidationError):
            SimulateRequest(**_api_payload([0, 1], controls=[2]))


def _little_endian_reference_qft(wires, n_qubits):
    """QFT with ``wires[0]`` as the LEAST significant bit of the register.

    This is the Quirk / Qiskit operand convention. Matrix storage still uses
    this project's q0-first basis labels; only the bit significance inside the
    register differs.
    """

    register_size = len(wires)
    register_dimension = 2 ** register_size
    dimension = 2 ** n_qubits
    expected = np.zeros((dimension, dimension), dtype=np.complex128)
    for column in range(dimension):
        bits = format(column, f"0{n_qubits}b")
        source_value = sum(
            int(bits[wire]) << position for position, wire in enumerate(wires)
        )
        for output_value in range(register_dimension):
            output_bits = list(bits)
            for position, wire in enumerate(wires):
                output_bits[wire] = str((output_value >> position) & 1)
            expected[int("".join(output_bits), 2), column] = np.exp(
                2.0j * np.pi * source_value * output_value / register_dimension
            ) / np.sqrt(register_dimension)
    return expected


def _reference_qft(targets, n_qubits):
    register_size = len(targets)
    register_dimension = 2 ** register_size
    dimension = 2 ** n_qubits
    expected = np.zeros((dimension, dimension), dtype=np.complex128)
    for column in range(dimension):
        bits = format(column, f"0{n_qubits}b")
        source_value = int("".join(bits[target] for target in targets), 2)
        for output_value in range(register_dimension):
            output_bits = list(bits)
            value_bits = format(output_value, f"0{register_size}b")
            for position, target in enumerate(targets):
                output_bits[target] = value_bits[position]
            expected[int("".join(output_bits), 2), column] = np.exp(
                2.0j * np.pi * source_value * output_value / register_dimension
            ) / np.sqrt(register_dimension)
    return expected


def _qft_circuit(n_qubits, targets):
    return CircuitConfig(
        logical_qubits=n_qubits,
        initial_states=["0"] * n_qubits,
        columns=[GateColumn(0, [GateOperation("QFT", list(targets))])],
    )


def _test_density_matrix(n_qubits):
    generator = np.random.default_rng(20260807)
    ket = generator.normal(size=2 ** n_qubits) + 1j * generator.normal(size=2 ** n_qubits)
    ket /= np.linalg.norm(ket)
    return _as_matrix(np.outer(ket, ket.conj()))


def _as_matrix(array):
    return tuple(tuple(complex(value) for value in row) for row in array)


def _simulation_config(n_qubits, targets, mode):
    return SimulationConfig(
        circuit=_qft_circuit(n_qubits, targets),
        environment=EnvironmentConfig(
            input_mode="physical",
            ideal_reference=True,
            device_quality=1.0,
            temperature_mk=0.0,
            flux_noise_phi0=0.0,
        ),
        duration_us=1.98,
        time_steps=21,
        fidelity_threshold=0.9,
        model=GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
        compilation_mode=mode,
    )


def _api_payload(targets, controls=None):
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
                    "type": "QFT",
                    "targets": list(targets),
                    "controls": list(controls or []),
                }],
            }],
        },
        "parameters": {
            "normalized_temperature": 0.0,
            "normalized_magnetic_field": 0.0,
            "noise_level": 0.0,
            "duration_us": 1.98,
            "time_steps": 21,
            "fidelity_threshold": 0.9,
        },
    }


if __name__ == "__main__":
    unittest.main()

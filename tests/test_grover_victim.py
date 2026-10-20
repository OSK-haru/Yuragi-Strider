"""The routed Grover circuit uses only coupled pairs and finds the marked item with the ideal probability."""

import math
import unittest

import numpy as np

from core.attack_simulation.grover_victim import (
    DATA_QUBITS, grover_circuit, logical_grover, route, to_editor_circuit,
)

REGION = (2, 3, 4, 5, 16, 23)
EDGES = ((2, 3), (3, 4), (4, 5), (3, 16), (16, 23))        # the product's default region: a tree on 3


def simulate(ops, qubits):
    """Statevector of the ops on |0...0>; qubit 0 is the most significant bit."""
    state = np.zeros(2 ** qubits, dtype=complex)
    state[0] = 1.0
    state = state.reshape([2] * qubits)
    h = np.array([[1, 1], [1, -1]]) / math.sqrt(2)
    x = np.array([[0, 1], [1, 0]])

    def ry(theta):
        return np.array([[math.cos(theta / 2), -math.sin(theta / 2)], [math.sin(theta / 2), math.cos(theta / 2)]])

    def one(matrix, q):
        nonlocal state
        state = np.moveaxis(np.tensordot(matrix, state, axes=([1], [q])), 0, q)

    for op in ops:
        if op[0] == "H":
            one(h, op[1])
        elif op[0] == "X":
            one(x, op[1])
        elif op[0] == "RY":
            one(ry(op[2]), op[1])
        else:
            control, target = op[1], op[2]
            view = np.moveaxis(state, (control, target), (0, 1))
            if op[0] == "CNOT":
                view[1] = np.flip(view[1], axis=0).copy()
            else:
                view[1, 1] = -view[1, 1]
            state = np.moveaxis(view, (0, 1), (control, target))
    return state.reshape(-1)


def data_distribution(state, qubits, holders):
    probabilities = (np.abs(state) ** 2).reshape([2] * qubits)
    other = [q for q in range(qubits) if q not in holders]
    marginal = probabilities.sum(axis=tuple(other)) if other else probabilities
    order = sorted(holders)
    permutation = [order.index(h) for h in holders]
    return np.transpose(marginal, permutation)


class GroverVictimTests(unittest.TestCase):
    def test_every_two_qubit_gate_sits_on_a_coupled_pair(self):
        coupled = {(REGION.index(a), REGION.index(b)) for a, b in EDGES}
        coupled |= {(b, a) for a, b in coupled}
        document, _ = grover_circuit("1011", 2, REGION, EDGES)
        for column in document["columns"]:
            for gate in column["gates"]:
                if gate["type"] in {"CNOT", "CZ"}:
                    self.assertIn((gate["controls"][0], gate["targets"][0]), coupled)

    def test_ideal_success_matches_grover_for_one_and_two_iterations(self):
        for marked in ("1011", "0000", "1111"):
            for iterations, ideal in ((1, 0.4725), (2, 0.9084)):
                with self.subTest(marked=marked, iterations=iterations):
                    routed, where = route(logical_grover(marked, iterations), REGION, EDGES)
                    state = simulate(routed, len(REGION))
                    distribution = data_distribution(state, len(REGION), where[:DATA_QUBITS])
                    index = tuple(int(bit) for bit in marked)
                    self.assertAlmostEqual(float(distribution[index]), ideal, places=3)

    def test_ancillas_return_to_zero(self):
        routed, where = route(logical_grover("1011", 2), REGION, EDGES)
        state = simulate(routed, len(REGION))
        probabilities = (np.abs(state) ** 2).reshape([2] * len(REGION))
        ancilla = [where[4], where[5]]
        for q in ancilla:
            self.assertAlmostEqual(float(np.take(probabilities, 0, axis=q).sum()), 1.0, places=9)

    def test_two_iterations_fit_the_editor_and_job_limits(self):
        document, expected = grover_circuit("1011", 2, REGION, EDGES)
        self.assertEqual(expected, "1011")
        self.assertLessEqual(len(document["columns"]), 200)
        self.assertEqual(document["classical_bits"], DATA_QUBITS)
        gates = [g for c in document["columns"] for g in c["gates"]]
        self.assertTrue({g["type"] for g in gates} <= {"H", "X", "RY", "CNOT", "CZ", "MEASURE"})

    def test_a_disconnected_region_is_rejected(self):
        with self.assertRaises(ValueError):
            route(logical_grover("1011", 1), REGION, [(2, 3)])
        with self.assertRaises(ValueError):
            grover_circuit("1011", 1, REGION[:5], EDGES)

    def test_circuit_document_is_deterministic(self):
        self.assertEqual(grover_circuit("1011", 2, REGION, EDGES), grover_circuit("1011", 2, REGION, EDGES))
        ops, _ = route(logical_grover("1011", 1), REGION, EDGES)
        self.assertEqual(to_editor_circuit(ops, 6, [0, 1, 2, 3])["logical_qubits"], 6)


if __name__ == "__main__":
    unittest.main()

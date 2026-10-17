"""The attack_client circuit builder packs gates like a transpiler and measures once at the end."""

import json
import random
import unittest

from attack_client.circuit import Circuit
from core.attack_simulation.circuit_program import LocalCircuitCompiler, NATIVE_GATES
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest


def columns(circuit):
    return [[(g["type"], tuple(g["targets"]), tuple(g.get("controls", []))) for g in c["gates"]]
            for c in circuit.to_dict()["columns"]]


class CircuitBuilderTests(unittest.TestCase):
    def test_gates_on_different_qubits_share_a_column(self):
        cols = columns(Circuit(3).x(0).h(1).z(2).measure_all())
        self.assertEqual(len(cols), 2)
        self.assertEqual(sorted(cols[0]), [("H", (1,), ()), ("X", (0,), ()), ("Z", (2,), ())])

    def test_gates_on_the_same_qubit_stay_in_call_order(self):
        cols = columns(Circuit(1).x(0).h(0).z(0).measure(0, 0))
        self.assertEqual([c[0][0] for c in cols[:3]], ["X", "H", "Z"])

    def test_a_two_qubit_gate_waits_for_both_of_its_qubits(self):
        cols = columns(Circuit(3).x(0).h(1).x(1).cnot(0, 1).z(2).measure_all())
        position = {gate: i for i, column in enumerate(cols) for gate in column}
        self.assertGreater(position[("CNOT", (1,), (0,))], position[("X", (1,), ())])
        self.assertGreater(position[("CNOT", (1,), (0,))], position[("X", (0,), ())])
        self.assertEqual(position[("Z", (2,), ())], 0)

    def test_measurements_land_in_one_final_column(self):
        cols = columns(Circuit(4).x(0).measure_all())
        self.assertEqual(len(cols[-1]), 4)
        self.assertTrue(all(kind == "MEASURE" for kind, _, _ in cols[-1]))
        self.assertTrue(all(kind != "MEASURE" for column in cols[:-1] for kind, _, _ in column))

    def test_a_measured_qubit_cannot_be_used_again(self):
        with self.assertRaises(ValueError):
            Circuit(2).measure(0, 0).x(0)
        Circuit(2).measure(0, 0).x(1)  # another qubit is fine; the gate still runs before the measurement

    def test_per_qubit_order_is_preserved_for_random_circuits(self):
        rng = random.Random(7)
        for _ in range(50):
            circuit, expected = Circuit(4), {q: [] for q in range(4)}
            for index in range(rng.randrange(1, 40)):
                if rng.random() < 0.3:
                    a, b = rng.sample(range(4), 2)
                    circuit.cnot(a, b)
                    for q in (a, b):
                        expected[q].append(("CNOT", index))
                else:
                    q = rng.randrange(4)
                    circuit.rx(q, 0.1 * index)
                    expected[q].append(("RX", index))
            seen = {q: [] for q in range(4)}
            for column in circuit.to_dict()["columns"]:
                for gate in column["gates"]:
                    for q in list(gate["targets"]) + list(gate.get("controls", [])):
                        seen[q].append(gate["type"])
            for q in range(4):
                self.assertEqual(seen[q], [kind for kind, _ in expected[q]])

    def test_packing_shortens_the_planned_time_but_not_the_gate_count(self):
        compiler = LocalCircuitCompiler(supported_gates=NATIVE_GATES)
        serial = Circuit(4)
        for q in range(4):
            serial.x(q)
        text = json.dumps(serial.measure_all().to_dict())
        operation = compiler.compile(ProgramSubmitRequest(CircuitProgram.from_editor_json(text), "d", 1, "k"),
                                     sampling_seed=1)
        durations = [i.duration_us for i in operation.instructions if i.kind == "unitary"]
        self.assertEqual(len(durations), 4)
        self.assertEqual(sum(1 for d in durations if d > 0), 1)  # one layer carries the time of all four
        measures = [i for i in operation.instructions if i.kind == "measure"]
        self.assertEqual(len({i.time_group for i in measures}), 1)


if __name__ == "__main__":
    unittest.main()

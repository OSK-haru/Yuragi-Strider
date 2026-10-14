"""Register IR, physical layout, correlated state, and bounded terminal E2E."""

import json
import unittest

import numpy as np

from core.attack_simulation.circuit_program import LocalCircuitCompiler, NATIVE_GATES
from core.attack_simulation.job_service import JobNotFoundError
from core.attack_simulation.jobs import JobCommand, JobStatus
from core.attack_simulation.program_request import CircuitProgram, ProgramRequestError, ProgramSubmitRequest
from core.attack_simulation.resource_binding import PhysicalQubitLayout, PhysicalQubitRef, ResetResourceBinding
from core.circuit_model import GateOperation
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel, ResetModelRequest
from core.experimental_isolation_physics.imperfect_reset import apply_local_imperfect_reset
from core.gates import apply_unitary_to_density, density_from_ket, gate_unitary, initial_density_matrix
from core.register_operations import apply_operand_operator, freeze_register
from tests.test_common_command_pipeline import pipeline


def program_json(n, columns=(), *, initial=None, measurements=None, classical_bits=None):
    measurements = [(q, q) for q in range(n)] if measurements is None else measurements
    columns = list(columns) + [[{"type": "MEASURE", "targets": [q], "classical_targets": [c]} for q, c in measurements]]
    return json.dumps({"logical_qubits": n, "initial_states": [0] * n if initial is None else initial,
                       "classical_bits": n if classical_bits is None else classical_bits,
                       "columns": [{"step": i, "gates": gates} for i, gates in enumerate(columns)]})


def submit(terminal, text, *, shots=1, request_id="sample"):
    terminal.workspace.put_circuit("register.json", text)
    return terminal.execute(f"program submit register.json --device qpu-1 --shots {shots} --request-id {request_id}")


class LocalProgramRuntimeTests(unittest.TestCase):
    def test_unsupported_isolated_register_is_rejected_during_assembly(self):
        with self.assertRaisesRegex(ValueError, "one-qubit local region"):
            pipeline(physical_qubit_ids=(0, 1), placement_policy="isolated_physical_resource")

    def test_bell_measurements_are_correlated_not_independent_marginals(self):
        store, runtime, _, (a, v) = pipeline(physical_qubit_ids=(8, 19), supported_gates=NATIVE_GATES)
        text = program_json(2, [[{"type": "H", "targets": [0]}],
                                [{"type": "CNOT", "controls": [0], "targets": [1]}]])
        job = submit(a, text, shots=24)
        self.assertIs(job.status, JobStatus.QUEUED)
        self.assertEqual(a.execute("device show qpu-1").max_qubits, 2)
        self.assertEqual(store.read().revision, 0)
        runtime.run_next()
        result = a.execute(f"job result {job.job_id}").result
        self.assertEqual(sum(result["counts"].values()), 24)
        self.assertTrue(set(result["counts"]) <= {"00", "11"})
        self.assertAlmostEqual(store.read().device_time_us, 24 * (1.0 + 0.02 + 0.20 + 1.0))
        state = np.asarray(store.read().density_matrix)
        self.assertAlmostEqual(float(state[0, 0].real + state[3, 3].real), 1.0)
        with self.assertRaises(JobNotFoundError):
            v.execute(f"job result {job.job_id}")
        victim = submit(v, text, shots=1)
        self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
        self.assertEqual(sum(v.execute(f"job result {victim.job_id}").result["counts"].values()), 1)

    def test_noncontiguous_layout_and_classical_wiring_preserve_spectator_coherence(self):
        layout = PhysicalQubitLayout("qpu-1", (91, 5))
        initial = initial_density_matrix(["0", "+", "0"])
        store, runtime, _, (a, _) = pipeline(physical_qubit_ids=(5, 17, 91), initial_density=initial,
                                            program_layouts={"a": layout}, supported_gates=NATIVE_GATES)
        text = program_json(2, [[{"type": "X", "targets": [0]}]],
                            measurements=[(0, 2), (1, 0)], classical_bits=4)
        job = submit(a, text)
        runtime.run_next()
        self.assertEqual(a.execute(f"job result {job.job_id}").result["counts"], {"0010": 1})
        expected = initial_density_matrix(["0", "+", "1"])
        np.testing.assert_allclose(store.read().density_matrix, expected, atol=1e-12)
        self.assertEqual(runtime.scheduler.get(job.job_id).command.layout, layout)
        self.assertEqual(a.execute("device show qpu-1").max_qubits, 2)
        with self.assertRaises(ProgramRequestError):
            submit(a, program_json(3), request_id="too-wide")

    def test_partial_measurement_collapses_shared_entanglement_without_discarding_other_qubits(self):
        ket = [0j] * 8
        ket[0] = ket[7] = 2 ** -0.5
        store, runtime, _, (a, _) = pipeline(
            physical_qubit_ids=(3, 7, 11), reset_success=0.0, initial_density=density_from_ket(tuple(ket)),
            program_layouts={"a": PhysicalQubitLayout("qpu-1", (11,))},
        )
        job = submit(a, program_json(1))
        runtime.run_next()
        counts = a.execute(f"job result {job.job_id}").result["counts"]
        bit = 1 if counts["1"] else 0
        expected = np.zeros((8, 8), complex)
        expected[7 * bit, 7 * bit] = 1
        np.testing.assert_allclose(store.read().density_matrix, expected, atol=1e-12)
        self.assertEqual(store.read().physical_qubit_ids, (3, 7, 11))

    def test_shared_register_state_survives_job_and_tenant_changes_when_resets_fail(self):
        store, runtime, _, (a, v) = pipeline(physical_qubit_ids=(5, 9), reset_success=0.0)
        first = submit(v, program_json(2, [[{"type": "X", "targets": [0]}]]))
        runtime.run_next()
        second = submit(a, program_json(2))
        runtime.run_next()
        self.assertEqual(v.execute(f"job result {first.job_id}").result["counts"], {"10": 1})
        self.assertEqual(a.execute(f"job result {second.job_id}").result["counts"], {"10": 1})
        self.assertEqual([e.event_kind for e in store.events()], ["program_shot", "service_reset", "program_shot"])

    def test_common_programs_respect_the_one_qubit_reset_preset_binding(self):
        for isolated in (False, True):
            binding = ResetResourceBinding(PhysicalQubitRef("qpu-1", 5), PhysicalQubitRef("qpu-1", 6 if isolated else 5))
            store, runtime, _, (a, v) = pipeline(
                physical_qubit_ids=(5,), reset_success=0.0, resource_binding=binding,
                placement_policy="isolated_physical_resource" if isolated else "guaranteed_same_physical_qubit",
            )
            submit(v, program_json(1, [[{"type": "X", "targets": [0]}]]))
            runtime.run_next()
            job = submit(a, program_json(1))
            self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
            counts = a.execute(f"job result {job.job_id}").result["counts"]
            self.assertEqual(counts, {"0": 1, "1": 0} if isolated else {"0": 0, "1": 1})
            self.assertEqual(store.read().physical_qubit_ids, (6,) if isolated else (5,))

    def test_region_work_and_layout_fail_before_queue_or_state_changes(self):
        store, runtime, _, (a, _) = pipeline(physical_qubit_ids=(1, 4), max_execution_work=1)
        with self.assertRaises(ProgramRequestError):
            submit(a, program_json(2))
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.events(), ())
        request = ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(2)), "qpu-1", 1, "direct")
        op = LocalCircuitCompiler().compile(request, sampling_seed=1)
        with self.assertRaises(ValueError):
            runtime.submit(JobCommand("tenant-a", op, "invalid-layout", layout=PhysicalQubitLayout("qpu-1", (1, 99))))
        self.assertEqual(store.read().revision, 0)

    def test_psd_checks_reject_both_diagonal_and_correlated_invalid_states(self):
        store, _, _, _ = pipeline(physical_qubit_ids=(0, 1))
        diagonal = np.diag([-0.1, 1.1, 0.0, 0.0]).astype(complex)
        correlated = np.zeros((4, 4), complex)
        correlated[0, 0] = correlated[1, 1] = 0.5
        correlated[0, 1] = correlated[1, 0] = 0.7
        for state in (diagonal, correlated):
            with self.subTest(state=state), self.assertRaises(ValueError):
                store.validate_state(freeze_register(state))

    def test_eight_qubits_execute_without_full_register_gate_copies(self):
        store, runtime, _, (a, _) = pipeline(physical_qubit_ids=tuple(range(100, 108)), supported_gates=NATIVE_GATES)
        job = submit(a, program_json(8, [[{"type": "X", "targets": [q]} for q in range(8)]]))
        instructions = runtime.scheduler.get(job.job_id).command.operation.instructions
        self.assertTrue(all(len(i.unitary) == 2 for i in instructions if i.unitary is not None))
        self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
        self.assertEqual(a.execute(f"job result {job.job_id}").result["counts"], {"11111111": 1})
        self.assertEqual(len(store.read().density_matrix), 256)
        self.assertAlmostEqual(store.read().device_time_us, 2.02)

    def test_physical_region_can_be_larger_than_the_eight_qubit_input_limit(self):
        store, runtime, _, (a, _) = pipeline(physical_qubit_ids=tuple(range(9)), max_local_qubits=9)
        job = submit(a, program_json(8, [[{"type": "X", "targets": [q]} for q in range(8)]]))
        self.assertEqual(a.execute("device show qpu-1").max_qubits, 8)
        self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
        self.assertEqual(a.execute(f"job result {job.job_id}").result["counts"], {"11111111": 1})
        state = np.asarray(store.read().density_matrix)
        self.assertAlmostEqual(state[510, 510].real, 1.0)  # Eight 1s, then untouched spectator 0.
        self.assertEqual(len(store.read().physical_qubit_ids), 9)

    def test_unsupported_or_ambiguous_programs_are_rejected_before_admission(self):
        _, runtime, _, (a, _) = pipeline(physical_qubit_ids=(0, 1), supported_gates=NATIVE_GATES)
        invalid = [
            program_json(2, [[{"type": "H", "targets": [0], "controls": [1]}]]),
            program_json(2, measurements=[(0, 0), (1, 0)]),
            program_json(2, [[{"type": "MEASURE", "targets": [0], "classical_targets": [0]}], [{"type": "X", "targets": [1]}]]),
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ProgramRequestError):
                submit(a, text)
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())


class RegisterKernelTests(unittest.TestCase):
    def test_operand_kernels_match_expanded_core_gates_on_complex_correlated_states(self):
        rng = np.random.default_rng(83)
        matrix = rng.normal(size=(16, 16)) + 1j * rng.normal(size=(16, 16))
        state = matrix @ matrix.conj().T
        state /= np.trace(state)
        gates = [GateOperation("Y", [2]), GateOperation("S", [1]),
                 GateOperation("RZ", [3], params={"theta_rad": 0.72}),
                 GateOperation("CNOT", [1], [3, 0], {"control_state": 1}),
                 GateOperation("CP", [0], [3], {"theta_rad": 0.32}),
                 GateOperation("CCX", [2], [3, 1]), GateOperation("SWAP", [3, 0]),
                 GateOperation("QFT", [3, 1]), GateOperation("ORACLE", [2, 0], params={"marked_index": 2})]
        for gate in gates:
            operands = tuple(gate.controls + gate.targets)
            indices = {q: i for i, q in enumerate(operands)}
            local = GateOperation(gate.type, [indices[q] for q in gate.targets], [indices[q] for q in gate.controls], gate.params)
            with self.subTest(gate=gate.type):
                result = apply_operand_operator(state, gate_unitary(local, len(operands)), operands)
                expected = apply_unitary_to_density(freeze_register(state), gate_unitary(gate, 4))
                np.testing.assert_allclose(result, expected, atol=1e-12)

    def test_register_reset_matches_existing_cptp_primitive_and_leaves_spectators(self):
        spec = ImperfectResetSpec(0.37, (0.92, 0.08))
        model = PhenomenologicalResetModel(spec)
        rng = np.random.default_rng(39)
        factor = rng.normal(size=(8, 8)) + 1j * rng.normal(size=(8, 8))
        correlated = factor @ factor.conj().T
        correlated /= np.trace(correlated)
        for state in (initial_density_matrix(["+", "-", "1"]), freeze_register(correlated)):
            actual = model.run_register(ResetModelRequest(state, reset_count=2, victim_measurement=False, subsystem_count=3, targets=(2, 0)))
            expected = state
            for _ in range(2):
                for q in (2, 0):
                    expected = apply_local_imperfect_reset(expected, 3, q, spec)
            np.testing.assert_allclose(actual.post_reset_state, expected, atol=1e-12)
            self.assertEqual(len(actual.assignment_errors), 2)

    def test_unitary_storage_budget_and_terminal_measurement_contract(self):
        compiler = LocalCircuitCompiler(max_unitary_elements=4)
        request = ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(2, [[{"type": "CNOT", "controls": [0], "targets": [1]}]])), "qpu-1", 1, "wide")
        with self.assertRaises(ProgramRequestError):
            compiler.compile(request, sampling_seed=1)


if __name__ == "__main__":
    unittest.main()

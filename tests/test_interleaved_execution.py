"""Interleaved victim/attacker runs: carry-over physics, controls and timing."""

from dataclasses import replace
import unittest

from core.attack_simulation.circuit_program import (
    CircuitExecutionOperation, InterleavedVictim, LocalCircuitCompiler, NATIVE_GATES,
)
from core.attack_simulation.interleaved_execution import interleaved_work
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.jobs import JobCommand, JobStatus
from core.attack_simulation.program_request import (
    INTERLEAVE_TRAINING_OPERATION_ID, INTERLEAVE_VICTIM_OPERATION_ID, CircuitProgram,
    ProgramRequestError, ProgramSubmitRequest, STATE_INHERIT_OPERATION_ID,
)
from core.attack_simulation.qpu_state import QpuStateStore, local_zero_snapshot
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import ScenarioMode, ScenarioPolicy, ScenarioSession, ScenarioTemplate
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel, ResetModelRegistry
from tests.test_common_command_pipeline import OPERATIONS
from tests.test_local_program_runtime import program_json

ALL = OPERATIONS + (STATE_INHERIT_OPERATION_ID, INTERLEAVE_VICTIM_OPERATION_ID, INTERLEAVE_TRAINING_OPERATION_ID)
ATTACKER, VICTIM = "tenant-a", "tenant-v"
LAYOUT = PhysicalQubitLayout("qpu-1", (3,))


def build(reset_success, *, reset_count=1, grants=ALL, supported=ALL):
    model = PhenomenologicalResetModel(ImperfectResetSpec(reset_success))
    config = SharedQpuRuntimeConfig(
        reset_model_id=model.model_id, reset_model_configuration_id=model.configuration_id,
        reset_count=reset_count,
    )
    manifest = ScenarioEnvironment(ScenarioSession(
        template=ScenarioTemplate(
            "interleave_test", "1", attacker_config={"operation_grants": list(grants)},
            victim_config={"operation_grants": list(ALL)},
            qpu_service_config={
                "execution_regime": "nisq_physical", "supported_operations": list(supported),
                "runtime_execution": config.to_manifest_config(),
                "circuit_execution": {
                    "compiler_id": "local_sampling_v1", "supported_gates": list(NATIVE_GATES),
                    "max_shots": 512, "max_instruction_executions": 65536, "max_program_qubits": 8,
                    "supported_initializations": ["prepare", "inherit"]},
            }),
        policy=ScenarioPolicy(ScenarioMode.FREE, allowed_operations=tuple(supported)),
    )).establish()
    store = QpuStateStore(local_zero_snapshot(qpu_id="qpu-1", physical_qubit_ids=(3,), device_config_version="t"))
    registry = ResetModelRegistry()
    registry.register(model)
    runtime = SharedQpuRuntime(state_store=store, reset_models=registry, config=config)
    runtime.configure_job_authorization(JobAuthorizationPolicy.from_manifest(
        manifest, attacker_tenant_id=ATTACKER, victim_tenant_id=VICTIM))
    return store, runtime, LocalCircuitCompiler.from_manifest(manifest), manifest


def operation(compiler, bit, rounds, *, reference="victim", seed=11):
    victim = compiler.compile(ProgramSubmitRequest(
        CircuitProgram.from_editor_json(program_json(1, initial=[bit])), "qpu-1", 1, "v"),
        sampling_seed=seed + 1, physical_qubit_ids=(3,))
    attacker = compiler.compile(ProgramSubmitRequest(
        CircuitProgram.from_editor_json(program_json(1)), "qpu-1", rounds, "a", "inherit"),
        sampling_seed=seed, physical_qubit_ids=(3,))
    return replace(attacker, interleave=InterleavedVictim(reference, VICTIM, victim, LAYOUT))


def run(runtime, op, key="job"):
    runtime.submit(JobCommand(tenant_id=ATTACKER, operation=op, idempotency_key=key, layout=LAYOUT))
    done = runtime.run_next()
    assert done.status is JobStatus.SUCCEEDED, done
    return done.result["counts"]


def one_frequency(counts):
    return counts.get("1", 0) / sum(counts.values())


class InterleavedExecutionTests(unittest.TestCase):
    def test_imperfect_reset_leaks_the_victim_bit_every_round(self):
        rounds = 300
        _, runtime0, compiler0, _ = build(0.90)
        _, runtime1, compiler1, _ = build(0.90)
        zero = one_frequency(run(runtime0, operation(compiler0, 0, rounds)))
        one = one_frequency(run(runtime1, operation(compiler1, 1, rounds)))
        # A failed reset (probability 1 - 0.90) leaves the victim bit behind.
        self.assertAlmostEqual(one - zero, 0.10, delta=0.05)
        self.assertLess(zero, 0.03)

    def test_perfect_reset_is_the_control_with_no_dependence_on_the_victim_bit(self):
        rounds = 300
        _, runtime0, compiler0, _ = build(1.0)
        _, runtime1, compiler1, _ = build(1.0)
        zero = one_frequency(run(runtime0, operation(compiler0, 0, rounds)))
        one = one_frequency(run(runtime1, operation(compiler1, 1, rounds)))
        self.assertEqual((zero, one), (0.0, 0.0))

    def test_a_single_long_job_without_interleaving_only_leaks_in_its_first_shot(self):
        _, runtime, compiler, _ = build(0.90)
        victim_first = compiler.compile(ProgramSubmitRequest(
            CircuitProgram.from_editor_json(program_json(1, initial=[1])), "qpu-1", 1, "v"),
            sampling_seed=3, physical_qubit_ids=(3,))
        runtime.submit(JobCommand(tenant_id=VICTIM, operation=victim_first, idempotency_key="v", layout=LAYOUT))
        self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
        plain = replace(operation(compiler, 1, 300), interleave=None)
        counts = run(runtime, plain)
        self.assertLess(one_frequency(counts), 0.1)

    def test_counts_cover_only_the_attacker_shots(self):
        _, runtime, compiler, _ = build(0.90)
        counts = run(runtime, operation(compiler, 1, 5))
        self.assertEqual(sum(counts.values()), 5)

    def test_planned_duration_and_event_count_equal_the_committed_physics(self):
        store, runtime, compiler, _ = build(0.90)
        op = operation(compiler, 1, 7)
        config = runtime.config
        work = interleaved_work(op, ATTACKER, None, config.reset_duration_us,
                                config.program_measurement_duration_us)
        run(runtime, op)
        self.assertAlmostEqual(store.read().device_time_us, work.duration_us)
        self.assertEqual(len(store.events()), work.event_count)

    def test_a_previous_attacker_owner_adds_one_entry_reset(self):
        _, runtime, compiler, _ = build(0.90)
        op = operation(compiler, 1, 3)
        config = runtime.config
        args = (config.reset_duration_us, config.program_measurement_duration_us)
        fresh = interleaved_work(op, ATTACKER, None, *args)
        after_attacker = interleaved_work(op, ATTACKER, ATTACKER, *args)
        after_victim = interleaved_work(op, ATTACKER, VICTIM, *args)
        self.assertEqual(after_victim, fresh)
        self.assertEqual(after_attacker.event_count, fresh.event_count + 1)

    def test_interleaving_requires_its_own_grant(self):
        _, runtime, compiler, _ = build(0.90, grants=ALL[:-2] + (INTERLEAVE_TRAINING_OPERATION_ID,))
        with self.assertRaises(PermissionError):
            run(runtime, operation(compiler, 1, 2))
        counts = run(runtime, operation(compiler, 1, 2, reference="training:1"), "t")
        self.assertEqual(sum(counts.values()), 2)

    def test_interleave_reference_and_initialization_are_closed_sets(self):
        program = CircuitProgram.from_editor_json(program_json(1))
        with self.assertRaises(ProgramRequestError):
            ProgramSubmitRequest(program, "qpu-1", 4, "k", "inherit", interleave="anything")
        with self.assertRaises(ProgramRequestError):
            ProgramSubmitRequest(program, "qpu-1", 4, "k", "prepare", interleave="victim")


if __name__ == "__main__":
    unittest.main()

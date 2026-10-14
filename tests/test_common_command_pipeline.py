"""Real authenticated terminal -> compiler -> queue -> state -> result flow."""

from dataclasses import replace
import json
import unittest

from api.attack_terminal import TerminalSession, TerminalSyntaxError, TerminalWorkspace, parse_terminal_command
from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.job_service import JobApplicationService, JobNotFoundError, JobResultNotReadyError, PrincipalGrant
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.program_request import ProgramRequestError
from core.attack_simulation.qpu_state import QpuStateStore, local_zero_snapshot
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import ScenarioMode, ScenarioPolicy, ScenarioSession, ScenarioTemplate
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.attack_simulation.virtual_credentials import CredentialedJobConnection, VirtualCredentialAuthority
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel, ResetModelRegistry
from core.experimental_isolation_physics.reset_model import ResetModelResult
from core.gates import density_from_ket


OPERATIONS = ("device.info.read", "program.submit", "job.status.read", "job.result.read", "job.cancel")


def circuit_json(*gate_types: str, initial_state=0, qubits=1) -> str:
    gates = [{"type": kind, "targets": [0]} for kind in gate_types]
    gates.append({"type": "MEASURE", "targets": [0], "classical_targets": [0]})
    return json.dumps({
        "logical_qubits": qubits, "classical_bits": 1,
        "initial_states": [initial_state] * qubits,
        "columns": [{"step": index, "gates": [gate]} for index, gate in enumerate(gates)],
    })


def pipeline(*, reset_success=1.0, initial_bit=0, attacker_operations=OPERATIONS,
             model=None, execution_regime="nisq_physical", physical_qubit_ids=(0,),
             supported_gates=("I", "X", "H", "MEASURE"), program_layouts=None,
             initial_density=None, max_execution_work=536870912, max_instruction_executions=128,
             max_local_qubits=8, resource_binding=None, placement_policy="guaranteed_same_physical_qubit",
             topology_binding=None):
    if model is None:
        model = PhenomenologicalResetModel(ImperfectResetSpec(success_probability=reset_success))
    runtime_config = SharedQpuRuntimeConfig(
        reset_model_id=model.model_id,
        reset_model_configuration_id=model.configuration_id,
        max_execution_work=max_execution_work,
        max_local_qubits=max_local_qubits,
        resource_binding=resource_binding,
        placement_policy=placement_policy,
    )
    session = ScenarioSession(
        template=ScenarioTemplate(
            "common_commands_test", "1",
            attacker_config={"operation_grants": list(attacker_operations)},
            victim_config={"operation_grants": list(OPERATIONS)},
            qpu_service_config={
                "execution_regime": execution_regime,
                "supported_operations": list(OPERATIONS),
                "runtime_execution": runtime_config.to_manifest_config(),
                **({"topology": topology_binding.topology.to_config()} if topology_binding is not None else {}),
                "circuit_execution": {
                    "compiler_id": "local_sampling_v1",
                    "max_program_qubits": 8,
                    "supported_gates": list(supported_gates),
                    "max_shots": 32, "max_instruction_executions": max_instruction_executions,
                },
            },
        ),
        policy=ScenarioPolicy(ScenarioMode.FREE, allowed_operations=OPERATIONS),
    )
    manifest = ScenarioEnvironment(session).establish()
    compiler = LocalCircuitCompiler.from_manifest(manifest)
    snapshot = local_zero_snapshot(qpu_id="qpu-1", physical_qubit_ids=physical_qubit_ids, device_config_version="test-v1")
    if initial_density is not None:
        snapshot = replace(snapshot, density_matrix=initial_density)
    if initial_bit:
        snapshot = replace(snapshot, density_matrix=density_from_ket((0j, 1+0j)))
    store = QpuStateStore(snapshot, region_binding=topology_binding)
    registry = ResetModelRegistry()
    registry.register(model)
    runtime = SharedQpuRuntime(
        state_store=store, reset_models=registry, config=runtime_config,
    )
    runtime.configure_job_authorization(JobAuthorizationPolicy.from_manifest(
        manifest, attacker_tenant_id="tenant-a", victim_tenant_id="tenant-v",
    ))
    service = JobApplicationService(
        runtime=runtime, program_compiler=compiler, scenario_manifest_id=manifest.manifest_id,
        program_layouts=program_layouts,
        principals=(PrincipalGrant("a", "tenant-a", frozenset(OPERATIONS)),
                    PrincipalGrant("v", "tenant-v", frozenset(OPERATIONS))),
    )
    authority = VirtualCredentialAuthority()
    terminals = []
    for principal in ("a", "v"):
        connection = CredentialedJobConnection(
            authority=authority, service=service, principal_id=principal,
            preconfigured_credential=authority.issue(principal),
        )
        workspace = TerminalWorkspace()
        workspace.put_circuit("circuits/one.json", circuit_json("X"))
        terminals.append(TerminalSession(connection, workspace))
    return store, runtime, authority, terminals


class CommonCommandPipelineTests(unittest.TestCase):
    def test_physical_compiler_does_not_claim_ftqc_support(self):
        with self.assertRaises(ProgramRequestError):
            pipeline(execution_regime="fault_tolerant_logical")

    def test_submission_is_async_idempotent_and_scoped_for_both_actors(self):
        store, runtime, authority, (attacker, victim) = pipeline()
        self.assertEqual(attacker.execute("device list")[0].device_id, "qpu-1")
        self.assertEqual(victim.execute("device show qpu-1").max_qubits, 1)
        command = "program submit circuits/one.json --shots 8 --device qpu-1 --request-id once"
        job = attacker.execute(command)
        self.assertIs(job.status, JobStatus.QUEUED)
        self.assertEqual(attacker.execute(command).job_id, job.job_id)
        attacker.workspace.put_circuit("circuits/one.json", circuit_json())
        with self.assertRaises(ValueError):
            attacker.execute(command)  # Changed program with the same request ID.
        attacker.workspace.put_circuit("circuits/one.json", circuit_json("X"))
        self.assertEqual(runtime.scheduler.queued_job_ids(), (job.job_id,))
        with self.assertRaises(JobResultNotReadyError):
            attacker.execute(f"job result {job.job_id}")
        self.assertEqual(store.read().revision, 0)
        self.assertIs(attacker.execute(f"job status {job.job_id}").status, JobStatus.QUEUED)
        runtime.run_next()  # Service supervisor, never a terminal command.
        result = attacker.execute(f"job result {job.job_id}")
        self.assertEqual(result.result, {"shots": 8, "counts": {"0": 0, "1": 8}})
        self.assertEqual(store.read().density_matrix[1][1].real, 1.0)
        self.assertEqual(len(store.events()), 8)
        self.assertGreater(store.read().device_time_us, 0)
        with self.assertRaises(JobNotFoundError):
            victim.execute(f"job result {job.job_id}")
        second = victim.execute(command)
        self.assertNotEqual(second.job_id, job.job_id)
        runtime.run_next()
        self.assertEqual(victim.execute(f"job result {second.job_id}").result["counts"], {"0": 0, "1": 8})
        authority.close()
        with self.assertRaises(PermissionError):
            attacker.execute(f"job status {job.job_id}")

    def test_cancellation_and_invalid_requests_do_not_touch_qpu_state(self):
        store, runtime, _, (terminal, _) = pipeline()
        job = terminal.execute("program submit circuits/one.json --device qpu-1 --shots 4")
        self.assertIs(terminal.execute(f"job cancel {job.job_id}").status, JobStatus.CANCELLED)
        self.assertIsNone(runtime.run_next())
        self.assertIsNone(terminal.execute(f"job result {job.job_id}").result)
        terminal.workspace.put_circuit("unsupported.json", circuit_json("Y"))
        terminal.workspace.put_circuit("wide.json", circuit_json("X", qubits=2))
        for command in (
            "program submit unsupported.json --device qpu-1 --shots 1",
            "program submit wide.json --device qpu-1 --shots 1",
            "program submit circuits/one.json --device wrong --shots 1",
            "program submit circuits/one.json --device qpu-1 --shots 33",
        ):
            with self.subTest(command=command), self.assertRaises(ProgramRequestError):
                terminal.execute(command)
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)
        self.assertEqual(store.events(), ())

    def test_capability_denial_precedes_job_creation(self):
        store, runtime, _, (terminal, _) = pipeline(attacker_operations=("job.status.read",))
        for command in ("device list", "program submit circuits/one.json --device qpu-1 --shots 1"):
            with self.subTest(command=command), self.assertRaises(PermissionError):
                terminal.execute(command)
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)

    def test_initialization_uses_physical_reset_and_preserves_its_imperfection(self):
        for success, expected in ((1.0, {"0": 4, "1": 0}), (0.0, {"0": 0, "1": 4})):
            with self.subTest(reset_success=success):
                _, runtime, _, (terminal, _) = pipeline(reset_success=success, initial_bit=1)
                terminal.workspace.put_circuit("measure.json", circuit_json())
                job = terminal.execute("program submit measure.json --device qpu-1 --shots 4")
                runtime.run_next()
                self.assertEqual(terminal.execute(f"job result {job.job_id}").result["counts"], expected)

    def test_option_grammar_and_workspace_are_bounded(self):
        command = parse_terminal_command('program   submit "circuit one.json" --shots 32 --request-id retry --device qpu-1')
        self.assertEqual(command.program_path, "circuit one.json")
        self.assertEqual(command.shots, 32)
        for line in (
            "program submit one.json --shots 1",
            "program submit one.json --device qpu-1 --shots 1 --shots 2",
            "program submit one.json --device qpu-1 --shots 1 --tenant victim",
            "program submit ../secret.json --device qpu-1 --shots 1",
            "program submit one.json --device qpu-1 --shots 1;dir",
            "program submit one.json --device qpu-1 --shots 0",
        ):
            with self.subTest(line=line), self.assertRaises(TerminalSyntaxError):
                parse_terminal_command(line)
        workspace = TerminalWorkspace()
        workspace.put_circuit("one.json", circuit_json())
        with self.assertRaises(FileNotFoundError):
            TerminalWorkspace().read_circuit("one.json")
        for index in range(15):
            workspace.put_circuit(f"{index}.json", circuit_json())
        with self.assertRaises(ValueError):
            workspace.put_circuit("overflow.json", circuit_json())

    def test_readout_error_is_not_a_change_to_the_true_post_measurement_state(self):
        model = PhenomenologicalResetModel(
            ImperfectResetSpec(success_probability=1.0), observed_zero_given_one=1.0,
        )
        store, runtime, _, (terminal, _) = pipeline(model=model)
        job = terminal.execute("program submit circuits/one.json --device qpu-1 --shots 4")
        runtime.run_next()
        self.assertEqual(terminal.execute(f"job result {job.job_id}").result["counts"], {"0": 4, "1": 0})
        self.assertEqual(store.read().density_matrix, density_from_ket((0j, 1+0j)))

    def test_invalid_reset_state_cannot_be_hidden_by_final_measurement(self):
        for successful_shots in (0, 1):
            with self.subTest(successful_shots=successful_shots):
                store, runtime, _, (terminal, _) = pipeline(model=_InvalidLaterResetModel(successful_shots))
                job = terminal.execute("program submit circuits/one.json --device qpu-1 --shots 4")
                completed = runtime.run_next()
                self.assertIs(completed.status, JobStatus.INTERNAL_ERROR)
                self.assertEqual(store.read().revision, successful_shots)
                self.assertEqual(len(store.events()), successful_shots)
                if successful_shots:
                    self.assertEqual(store.read().density_matrix, density_from_ket((0j, 1+0j)))
                result = terminal.execute(f"job result {job.job_id}")
                self.assertIsNone(result.result)
                self.assertEqual(result.error_code, "INTERNAL_EXECUTION_ERROR")


class _InvalidLaterResetModel:
    model_id = "invalid_later_reset"
    model_version = "1"
    fidelity_level = "test_double"
    provenance = "test"
    configuration_id = "invalid-later-reset-v1"
    model_mode = "state_transition_with_readout"

    def __init__(self, successful_shots):
        self.remaining = successful_shots

    def run(self, request):
        valid = self.remaining > 0
        self.remaining -= 1
        return ResetModelResult(
            post_reset_state=density_from_ket((1+0j, 0j)) if valid else ((2+0j, 0j), (0j, 0j)),
            attacker_one_probability=0.0,
            observed_one_given_true_zero=0.0, observed_zero_given_true_one=0.0,
        )


if __name__ == "__main__":
    unittest.main()

"""Bounded, actor-neutral preparation/inheritance and readout regressions."""

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

import numpy as np

from api.attack_terminal import TerminalSession, TerminalSyntaxError, TerminalWorkspace, parse_terminal_command
from core.attack_simulation.circuit_program import CircuitExecutionOperation, CircuitInstruction, LocalCircuitCompiler, NATIVE_GATES
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.job_service import JobApplicationService, PrincipalGrant
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.program_request import CircuitProgram, ProgramRequestError, ProgramSubmitRequest, STATE_INHERIT_OPERATION_ID
from core.attack_simulation.qpu_state import QpuStateStore, local_zero_snapshot
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import ScenarioMode, ScenarioPolicy, ScenarioSession, ScenarioTemplate
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel, ResetModelRegistry
from core.experimental_isolation_physics.reset_model import RegisterResetResult
from core.gates import density_from_ket, initial_density_matrix
from tests.test_common_command_pipeline import OPERATIONS, _InvalidLaterResetModel
from tests.test_local_program_runtime import program_json


ALL_OPERATIONS = OPERATIONS + (STATE_INHERIT_OPERATION_ID,)


def manifest_with_profile(modes=("prepare", "inherit"), topology_binding=None, runtime_config=None):
    if runtime_config is None:
        model = PhenomenologicalResetModel(ImperfectResetSpec(1.0))
        runtime_config = SharedQpuRuntimeConfig(
            reset_model_id=model.model_id,
            reset_model_configuration_id=model.configuration_id,
        )
    config = {"compiler_id": "local_sampling_v1", "supported_gates": list(NATIVE_GATES),
              "max_shots": 32, "max_instruction_executions": 128, "max_program_qubits": 8}
    if modes is not None:
        config["supported_initializations"] = modes
    service_config = {"execution_regime": "nisq_physical", "supported_operations": list(ALL_OPERATIONS),
                      "runtime_execution": runtime_config.to_manifest_config(),
                      "circuit_execution": config}
    if topology_binding is not None:
        service_config["topology"] = topology_binding.topology.to_config()
    return ScenarioEnvironment(ScenarioSession(
        template=ScenarioTemplate(
            "initialization_test", "1",
            attacker_config={"operation_grants": list(ALL_OPERATIONS)},
            victim_config={"operation_grants": list(ALL_OPERATIONS)},
            qpu_service_config=service_config,
        ), policy=ScenarioPolicy(ScenarioMode.FREE, allowed_operations=ALL_OPERATIONS),
    )).establish()


def build(*, initial=None, ids=(3, 7, 11), layouts=None, model=None, modes=("prepare", "inherit"), topology_binding=None):
    model = model or PhenomenologicalResetModel(ImperfectResetSpec(1.0))
    runtime_config = SharedQpuRuntimeConfig(
        reset_model_id=model.model_id,
        reset_model_configuration_id=model.configuration_id,
    )
    manifest = manifest_with_profile(modes, topology_binding, runtime_config)
    snapshot = local_zero_snapshot(qpu_id="qpu-1", physical_qubit_ids=ids, device_config_version="test-v1")
    if initial is not None:
        snapshot = replace(snapshot, density_matrix=initial)
    store = QpuStateStore(snapshot, region_binding=topology_binding)
    registry = ResetModelRegistry()
    registry.register(model)
    runtime = SharedQpuRuntime(state_store=store, reset_models=registry, config=runtime_config)
    runtime.configure_job_authorization(JobAuthorizationPolicy.from_manifest(
        manifest, attacker_tenant_id="tenant-a", victim_tenant_id="tenant-v",
    ))
    service = JobApplicationService(
        runtime=runtime, program_compiler=LocalCircuitCompiler.from_manifest(manifest),
        scenario_manifest_id=manifest.manifest_id, program_layouts=layouts,
        principals=tuple(PrincipalGrant(p, f"tenant-{p}", frozenset(ALL_OPERATIONS)) for p in ("a", "v")),
    )
    return store, runtime, service


def request(*, text=None, mode="inherit", shots=1, key="one"):
    return ProgramSubmitRequest(CircuitProgram.from_editor_json(text or program_json(2)), "qpu-1", shots, key, mode)


def deny(runtime, boundary, role):
    policy = runtime.job_authorization
    if boundary == "actor":
        grants = dict(policy.actor_grants)
        actor_role = next(key for key in grants if key.role_id == role)
        grants[actor_role] = grants[actor_role] - {STATE_INHERIT_OPERATION_ID}
        policy = replace(policy, actor_grants=grants)
    else:
        field = f"{boundary}_operations"
        policy = replace(policy, **{field: getattr(policy, field) - {STATE_INHERIT_OPERATION_ID}})
    # Trusted test injection of a changed policy between service boundaries.
    runtime._job_authorization = policy


class ProgramInitializationTests(unittest.TestCase):
    def test_default_payload_manifest_and_preparation_are_compatible(self):
        default = request(mode="prepare", text=program_json(2, initial=["-", 1]), shots=4)
        implicit = ProgramSubmitRequest(default.program, default.device_id, default.shots, default.idempotency_key)
        self.assertEqual(default, implicit)
        self.assertEqual(set(implicit.to_application_payload()), {"device_id", "shots", "circuit_config"})
        manifest = manifest_with_profile(None)
        self.assertNotIn("supported_initializations", manifest.qpu_service_config["circuit_execution"])
        compiler = LocalCircuitCompiler.from_manifest(manifest)
        self.assertEqual(compiler.supported_initializations, ("prepare",))
        self.assertEqual(compiler.compile(default, sampling_seed=1), compiler.compile(implicit, sampling_seed=1))
        self.assertEqual([i.kind for i in compiler.compile(default, sampling_seed=1).instructions],
                         ["initialize", "unitary", "unitary", "unitary", "measure", "measure"])
        store, runtime, service = build(ids=(3, 11), initial=initial_density_matrix(["1", "1"]), modes=None)
        # Reusing an identical entry state is exact; this check counts the Reset applied to every shot.
        with patch("core.attack_simulation.shared_qpu_runtime._find_reusable", return_value=None), \
                patch.object(runtime.reset_models, "run_register", wraps=runtime.reset_models.run_register) as resets:
            job = service.submit_program("a", request(mode="prepare", shots=4))
            self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
            self.assertEqual(resets.call_count, 4)
        self.assertEqual(service.get_result("a", job.job_id).result["counts"], {"00": 4})
        self.assertAlmostEqual(store.read().device_time_us, 8.0)
        self.assertEqual(service.get_device("a", "qpu-1").initialization_policy,
                         "imperfect_reset_then_declared_preparation")

    def test_inherit_is_envelope_only_and_omits_all_declared_preparation(self):
        req = request(text=program_json(2, initial=["-", "+"]))
        compiler = LocalCircuitCompiler(supported_gates=("MEASURE",), supported_initializations=("inherit",))
        op = compiler.compile(req, sampling_seed=1)
        self.assertEqual(op.initialization, "inherit")
        self.assertEqual([i.kind for i in op.instructions], ["measure", "measure"])
        self.assertEqual(req.to_application_payload()["initialization"], "inherit")
        self.assertEqual(req.to_application_payload()["circuit_config"], req.program.to_circuit_dict())
        editor = json.loads(program_json(2))
        editor["initialization"] = "inherit"
        with self.assertRaises(ProgramRequestError):
            CircuitProgram.from_editor_json(json.dumps(editor))
        with self.assertRaises(ProgramRequestError):
            LocalCircuitCompiler().compile(req, sampling_seed=1)

    def test_noncontiguous_inheritance_retains_spectator_correlations_and_shot_collapse(self):
        ket = [0j] * 8
        ket[0] = ket[7] = 2 ** -0.5
        for principal in ("a", "v"):
            with self.subTest(principal=principal):
                store, runtime, service = build(initial=density_from_ket(tuple(ket)), layouts={
                    principal: PhysicalQubitLayout("qpu-1", (11, 3)),
                })
                with patch.object(runtime.reset_models, "run_register", side_effect=AssertionError("extra Reset")):
                    job = service.submit_program(principal, request(text=program_json(2, initial=["-", 1]), shots=8))
                    self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
                    # A second job by the same tenant inherits the first collapse.
                    later = service.submit_program(principal, request(shots=3, key="later"))
                    self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
                counts = service.get_result(principal, job.job_id).result["counts"]
                self.assertIn(counts, ({"00": 8}, {"11": 8}))
                label = next(iter(counts))
                self.assertEqual(service.get_result(principal, later.job_id).result["counts"], {label: 3})
                expected = np.zeros((8, 8), complex)
                expected[0 if label == "00" else 7, 0 if label == "00" else 7] = 1
                np.testing.assert_allclose(store.read().density_matrix, expected, atol=1e-12)
                self.assertEqual(store.read().physical_qubit_ids, (3, 7, 11))
                self.assertAlmostEqual(store.read().device_time_us, 11.0)

    def test_inherited_coherence_can_be_uncomputed_without_touching_spectator(self):
        ket = tuple(0.5 if i in (0, 2, 5, 7) else 0j for i in range(8))
        store, runtime, service = build(initial=density_from_ket(ket), layouts={
            "a": PhysicalQubitLayout("qpu-1", (11, 3)),
        })
        text = program_json(2, [[{"type": "CNOT", "controls": [0], "targets": [1]}],
                                [{"type": "H", "targets": [0]}]])
        job = service.submit_program("a", request(text=text))
        self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
        self.assertEqual(service.get_result("a", job.job_id).result["counts"], {"00": 1})
        np.testing.assert_allclose(store.read().density_matrix, initial_density_matrix(["0", "+", "0"]), atol=1e-12)

    def test_inherit_keeps_readout_error_separate_from_true_state(self):
        model = PhenomenologicalResetModel(ImperfectResetSpec(1.0), observed_one_given_zero=1.0,
                                          observed_zero_given_one=1.0)
        store, runtime, service = build(model=model, ids=(3, 11), initial=initial_density_matrix(["0", "1"]))
        with patch.object(runtime.reset_models, "run_register", side_effect=AssertionError("extra Reset")):
            job = service.submit_program("a", request(shots=5))
            self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
        self.assertEqual(service.get_result("a", job.job_id).result["counts"], {"10": 5})
        np.testing.assert_allclose(store.read().density_matrix, initial_density_matrix(["0", "1"]))

    def test_handover_reset_still_applies_once_to_new_allocation(self):
        store, runtime, service = build(ids=(3, 11))
        service.submit_program("v", request(mode="prepare", text=program_json(2, initial=[1, 1])))
        runtime.run_next()
        with patch.object(runtime.reset_models, "run_register", wraps=runtime.reset_models.run_register) as resets:
            job = service.submit_program("a", request(shots=3))
            self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
            self.assertEqual(resets.call_count, 1)
            self.assertEqual(resets.call_args.args[1].reset_count, 1)
            self.assertTrue(resets.call_args.args[1].victim_measurement)
        self.assertEqual(service.get_result("a", job.job_id).result["counts"], {"00": 3})
        self.assertEqual([event.event_kind for event in store.events()],
                         ["program_shot", "service_reset", "program_shot", "program_shot", "program_shot"])

    def test_grant_intersections_are_rechecked_at_every_boundary_for_both_roles(self):
        for principal, role in (("a", "attacker"), ("v", "victim")):
            for boundary in ("service", "actor", "scenario"):
                for phase in ("prepare", "admit", "submit", "execute"):
                    with self.subTest(role=role, boundary=boundary, phase=phase):
                        store, runtime, service = build()
                        command = service._prepare_program(principal, request())
                        if phase == "execute":
                            runtime.submit(command)
                        deny(runtime, boundary, role)
                        if phase == "execute":
                            self.assertIs(runtime.run_next().status, JobStatus.INTERNAL_ERROR)
                        else:
                            with self.assertRaises(PermissionError):
                                if phase == "prepare":
                                    service._prepare_program(principal, request())
                                elif phase == "admit":
                                    service._admit_program(principal, command)
                                else:
                                    runtime.submit(command)
                        self.assertEqual(store.read().revision, 0)
                        self.assertEqual(runtime.scheduler.queued_job_ids(), ())

    def test_principal_grant_is_required_at_prepare_and_admit(self):
        for principal in ("a", "v"):
            store, runtime, service = build()
            command = service._prepare_program(principal, request())
            grant = service._principals[principal]
            service._principals[principal] = replace(grant, operations=grant.operations - {STATE_INHERIT_OPERATION_ID})
            for action in (lambda: service._prepare_program(principal, request()),
                           lambda: service._admit_program(principal, command)):
                with self.assertRaises(PermissionError):
                    action()
            self.assertEqual(store.read().revision, 0)
            self.assertEqual(runtime.scheduler.queued_job_ids(), ())

    def test_no_policy_and_default_compiler_fail_closed_for_inheritance(self):
        store, runtime, service = build(modes=None)
        with self.assertRaises(ProgramRequestError):
            service.submit_program("a", request())
        compiler = LocalCircuitCompiler(supported_initializations=("inherit",))
        command = replace(service._prepare_program("a", request(mode="prepare")),
                          operation=compiler.compile(request(), sampling_seed=1))
        with self.assertRaises(ProgramRequestError):
            service._admit_program("a", command)
        runtime._job_authorization = None
        with self.assertRaises(PermissionError):
            runtime.submit(command)
        self.assertEqual(store.read().revision, 0)

    def test_same_id_different_mode_conflicts_but_default_and_explicit_prepare_replay(self):
        _, runtime, service = build()
        self.assertEqual(service.get_device("a", "qpu-1").initialization_policy,
                         "explicit_preparation_or_inherited_physical_register")
        req = request(mode="prepare")
        first = service.submit_program("a", req)
        implicit = ProgramSubmitRequest(req.program, req.device_id, req.shots, req.idempotency_key)
        self.assertEqual(service.submit_program("a", implicit).job_id, first.job_id)
        with self.assertRaisesRegex(ProgramRequestError, "different command"):
            service.submit_program("a", replace(req, initialization="inherit"))
        self.assertEqual(runtime.scheduler.queued_job_ids(), (first.job_id,))

    def test_malformed_modes_profiles_and_ir_are_rejected(self):
        for mode in (None, 1, True, [], {}, "", "INHERIT", " inherit"):
            with self.subTest(mode=mode), self.assertRaises(ProgramRequestError):
                request(mode=mode)
        for modes in ([], "inherit", ["inherit", "inherit"], ["unknown"], [None], [{}], True):
            with self.subTest(modes=modes):
                with self.assertRaises(ValueError):
                    LocalCircuitCompiler(supported_initializations=modes)
                with self.assertRaises(ValueError):
                    LocalCircuitCompiler.from_manifest(manifest_with_profile(modes))
        measurement = CircuitInstruction("measure", 1.0, qubits=(0,), classical_targets=(0,))
        initialization = CircuitInstruction("initialize", 0.0)
        for instructions, mode in (((measurement,), "prepare"), ((initialization, measurement), "inherit"),
                                   ((initialization, initialization, measurement), "prepare"), ((measurement,), "bad")):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                CircuitExecutionOperation(instructions, 1, 1, initialization=mode)

    def test_unknown_readout_model_is_rejected_without_evaluating_reset(self):
        model = _InvalidLaterResetModel(1)
        store, runtime, service = build(ids=(3,), model=model)
        with patch.object(model, "run", side_effect=AssertionError("extra Reset")):
            with self.assertRaisesRegex(ProgramRequestError, "readout without Reset"):
                service.submit_program("a", request(text=program_json(1)))
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)

    def test_explicit_readout_only_model_method_uses_physical_target_order(self):
        class ReadoutModel(_InvalidLaterResetModel):
            def run_register(self, req):
                return RegisterResetResult(req.input_state, ((0.0, 0.0),) * len(req.targets))

            def register_assignment_errors(self, *, subsystem_count, targets):
                self.last_query = (subsystem_count, targets)
                return tuple((1.0, 1.0) if q == 2 else (0.0, 0.0) for q in targets)

        model = ReadoutModel(1)
        _, runtime, service = build(model=model, layouts={"a": PhysicalQubitLayout("qpu-1", (11, 3))})
        with patch.object(model, "run_register", side_effect=AssertionError("extra Reset")):
            job = service.submit_program("a", request())
            self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
        self.assertEqual(model.last_query, (3, (2, 0)))
        self.assertEqual(service.get_result("a", job.job_id).result["counts"], {"10": 1})

    def test_malformed_readout_only_channels_fail_before_acceptance(self):
        class ReadoutModel(_InvalidLaterResetModel):
            def register_assignment_errors(self, **kwargs):
                return self.errors

        for errors in ((), ((-0.1, 0.0),), ((0.0, float("nan")),), ((0.0,),), "noiseless"):
            with self.subTest(errors=errors):
                model = ReadoutModel(1)
                model.errors = errors
                store, runtime, service = build(ids=(3,), model=model)
                with self.assertRaises(ProgramRequestError):
                    service.submit_program("a", request(text=program_json(1)))
                self.assertEqual(runtime.scheduler.queued_job_ids(), ())
                self.assertEqual(store.read().revision, 0)

    def test_explicit_ir_reset_remains_a_physical_action_in_inherit_mode(self):
        store, runtime, service = build(ids=(3, 11), initial=initial_density_matrix(["1", "1"]))
        command = service._prepare_program("a", request())
        operation = replace(command.operation, instructions=(
            CircuitInstruction("reset", 0.0, qubits=(0,)), *command.operation.instructions,
        ))
        with patch.object(runtime.reset_models, "run_register", wraps=runtime.reset_models.run_register) as resets:
            job = runtime.submit(replace(command, operation=operation))
            self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
            self.assertEqual(resets.call_count, 1)
            self.assertEqual(resets.call_args.args[1].targets, (0,))
        self.assertEqual(service.get_result("a", job.job_id).result["counts"], {"01": 1})
        self.assertAlmostEqual(store.read().device_time_us, 2.0)

    def test_supervisor_checks_topology_presence_and_exact_metadata(self):
        from core.attack_simulation.qpu_topology import QpuTopology, RetainedRegionBinding, TopologyLimits
        topology = QpuTopology("qpu-1", "test-v1", (3, 7, 11, 99), TopologyLimits(4, 4, 4),
                               gate_edges=((3, 11),))
        binding = RetainedRegionBinding(topology, (3, 7, 11), 3)
        _, runtime, service = build(topology_binding=binding)
        manifest = manifest_with_profile(topology_binding=binding)
        service.validate_session_binding(manifest, runtime.run_next)

        def validate_changed(config, *, tested_runtime=runtime):
            # Model a supervisor assembling a valid manifest against the wrong
            # retained binding; preserve content-address validation itself.
            changed = replace(manifest, qpu_service_config=config, manifest_id="")
            assembled = JobApplicationService(
                runtime=tested_runtime, program_compiler=LocalCircuitCompiler.from_manifest(changed),
                scenario_manifest_id=changed.manifest_id,
                principals=tuple(PrincipalGrant(p, f"tenant-{p}", frozenset(ALL_OPERATIONS)) for p in ("a", "v")),
            )
            assembled.validate_session_binding(changed, tested_runtime.run_next)

        config = dict(manifest.qpu_service_config)
        raw = dict(config["topology"])
        raw["topology_version"] = "corrupted"
        config["topology"] = raw
        with self.assertRaisesRegex(ValueError, "topology differs"):
            validate_changed(config)
        config.pop("topology")
        with self.assertRaisesRegex(ValueError, "requires topology"):
            validate_changed(config)
        _, unbound_runtime, _ = build()
        unbound_manifest = manifest_with_profile()
        config = dict(unbound_manifest.qpu_service_config)
        config["topology"] = topology.to_config()
        with self.assertRaisesRegex(ValueError, "requires a retained region binding"):
            validate_changed(config, tested_runtime=unbound_runtime)

    def test_terminal_option_whitespace_dispatch_and_mode_conflict(self):
        _, runtime, service = build()

        class Client:
            def submit_program(self, req):
                return service.submit_program("a", req)

        workspace = TerminalWorkspace()
        workspace.put_circuit("circuit two.json", program_json(2))
        terminal = TerminalSession(Client(), workspace)
        line = '  program\t submit "circuit two.json" --initialization inherit --shots 2 --device qpu-1  '
        self.assertEqual(parse_terminal_command(line).initialization, "inherit")
        first = terminal.execute(line, request_id="transport")
        self.assertEqual(terminal.execute(line, request_id="transport").job_id, first.job_id)
        self.assertEqual(runtime.scheduler.get(first.job_id).command.operation.initialization, "inherit")
        with self.assertRaises(ProgramRequestError):
            terminal.execute(line.replace("inherit", "prepare"), request_id="transport")
        for suffix in (" --initialization inherit", " --initialization", "\nhelp", "\rhelp"):
            with self.assertRaises(TerminalSyntaxError):
                parse_terminal_command(line + suffix)
        with self.assertRaises(TerminalSyntaxError):
            parse_terminal_command(line.replace("inherit", "unknown"))


if __name__ == "__main__":
    unittest.main()

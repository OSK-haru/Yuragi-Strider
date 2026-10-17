"""The delay instruction: an identity gate with a declared wait, relaxed by the idle model."""

import json
import math
import unittest

from attack_client.circuit import Circuit
from core.attack_simulation.circuit_program import MAX_DELAY_US, LocalCircuitCompiler, NATIVE_GATES
from core.attack_simulation.declared_service_timing import compiled_job_duration
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.job_service import JobApplicationService, PrincipalGrant
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.program_request import CircuitProgram, ProgramRequestError, ProgramSubmitRequest
from core.attack_simulation.qpu_state import QpuStateStore, local_zero_snapshot
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel, ResetModelRegistry
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from core.gates import initial_density_matrix
from tests.test_program_initialization import ALL_OPERATIONS, manifest_with_profile

T1_US = 1.0


def build(*, idle=True, ids=(3,), initial=("1",)):
    model = PhenomenologicalResetModel(ImperfectResetSpec(1.0))
    config = SharedQpuRuntimeConfig(reset_model_id=model.model_id,
                                    reset_model_configuration_id=model.configuration_id)
    manifest = manifest_with_profile(runtime_config=config)
    snapshot = local_zero_snapshot(qpu_id="qpu-1", physical_qubit_ids=ids, device_config_version="test-v1")
    snapshot = type(snapshot)(**{**snapshot.__dict__, "density_matrix": initial_density_matrix(list(initial))})
    store = QpuStateStore(snapshot)
    registry = ResetModelRegistry()
    registry.register(model)
    idle_model = IndependentIdleModel(tuple(IdleQubitSpec(T1_US, math.inf, 0.0) for _ in ids), 8) if idle else None
    runtime = SharedQpuRuntime(state_store=store, reset_models=registry, config=config, idle_model=idle_model)
    runtime.configure_job_authorization(JobAuthorizationPolicy.from_manifest(
        manifest, attacker_tenant_id="tenant-a", victim_tenant_id="tenant-v"))
    service = JobApplicationService(
        runtime=runtime, program_compiler=LocalCircuitCompiler.from_manifest(manifest),
        scenario_manifest_id=manifest.manifest_id,
        principals=tuple(PrincipalGrant(p, f"tenant-{p}", frozenset(ALL_OPERATIONS)) for p in ("a", "v")),
    )
    return store, runtime, service, config


def submit(service, circuit, shots=1):
    program = CircuitProgram.from_editor_json(json.dumps(circuit.to_dict()))
    return service.submit_program("a", ProgramSubmitRequest(program, "qpu-1", shots, "k", "inherit"))


class DelayInstructionTests(unittest.TestCase):
    def test_the_identity_gate_with_a_duration_is_the_delay(self):
        self.assertIn("I", NATIVE_GATES)
        gate = Circuit(1).delay(0, 2.5).to_dict()["columns"][0]["gates"][0]
        self.assertEqual(gate, {"type": "I", "targets": [0], "params": {"duration_us": 2.5}})

    def test_a_long_wait_relaxes_the_excited_qubit_and_costs_that_time(self):
        store, runtime, service, config = build()
        circuit = Circuit(1).delay(0, 10 * T1_US).measure(0, 0)
        job = submit(service, circuit)
        record = runtime.run_next()
        self.assertIs(record.status, JobStatus.SUCCEEDED)
        counts = service.get_result("a", job.job_id).result["counts"]
        self.assertEqual(counts, {"0": 1, "1": 0})            # exp(-10) of |1> remains
        self.assertAlmostEqual(store.read().device_time_us, 10 * T1_US + 1.0)  # wait + measurement
        self.assertAlmostEqual(compiled_job_duration(record, config, None), store.read().device_time_us)

    def test_a_zero_wait_changes_nothing(self):
        store, runtime, service, _ = build()
        job = submit(service, Circuit(1).delay(0, 0.0).measure(0, 0))
        runtime.run_next()
        self.assertEqual(service.get_result("a", job.job_id).result["counts"], {"0": 0, "1": 1})
        self.assertAlmostEqual(store.read().device_time_us, 1.0)

    def test_only_the_named_qubit_idles(self):
        store, runtime, service, _ = build(ids=(3, 7), initial=("1", "1"))
        job = submit(service, Circuit(2).delay(0, 10 * T1_US).measure(0, 0).measure(1, 1))
        runtime.run_next()
        # Qubit 0 relaxed to 0; qubit 1 was not delayed and keeps |1>.
        self.assertEqual(service.get_result("a", job.job_id).result["counts"], {"01": 1})

    def test_parallel_columns_take_the_longest_wait(self):
        store, runtime, service, _ = build(ids=(3, 7), initial=("1", "1"))
        circuit = Circuit(2)
        circuit._columns.append([
            {"type": "I", "targets": [0], "params": {"duration_us": 4.0}},
            {"type": "I", "targets": [1], "params": {"duration_us": 6.0}},
        ])
        circuit.measure(0, 0).measure(1, 1)
        submit(service, circuit)
        runtime.run_next()
        self.assertAlmostEqual(store.read().device_time_us, 6.0 + 2.0)

    def test_a_delay_without_an_idle_model_is_rejected_before_it_runs(self):
        _, runtime, service, _ = build(idle=False)
        with self.assertRaisesRegex(ValueError, "idle model"):
            submit(service, Circuit(1).delay(0, 1.0).measure(0, 0))

    def test_a_bad_wait_is_rejected(self):
        _, _, service, _ = build()
        for bad in (-1.0, MAX_DELAY_US * 2):
            with self.assertRaises((ProgramRequestError, ValueError)):
                submit(service, Circuit(1).delay(0, bad).measure(0, 0))


if __name__ == "__main__":
    unittest.main()

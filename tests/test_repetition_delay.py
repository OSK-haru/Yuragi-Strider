"""The declared wait between consecutive shots of one Program."""

import math
import unittest

import numpy as np

from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.declared_service_timing import compiled_job_duration
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.job_service import JobApplicationService, PrincipalGrant
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.qpu_state import QpuStateStore, local_zero_snapshot
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel, ResetModelRegistry
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from core.gates import initial_density_matrix
from tests.test_program_initialization import ALL_OPERATIONS, manifest_with_profile, request
from tests.test_local_program_runtime import program_json

T1_US = 1.0


def build(delay_us, *, idle=True):
    model = PhenomenologicalResetModel(ImperfectResetSpec(1.0))
    config = SharedQpuRuntimeConfig(
        reset_model_id=model.model_id, reset_model_configuration_id=model.configuration_id,
        repetition_delay_us=delay_us,
    )
    manifest = manifest_with_profile(runtime_config=config)
    snapshot = local_zero_snapshot(qpu_id="qpu-1", physical_qubit_ids=(3,), device_config_version="test-v1")
    snapshot = type(snapshot)(**{**snapshot.__dict__, "density_matrix": initial_density_matrix(["1"])})
    store = QpuStateStore(snapshot)
    registry = ResetModelRegistry()
    registry.register(model)
    idle_model = IndependentIdleModel((IdleQubitSpec(T1_US, math.inf, 0.0),), 8) if idle else None
    runtime = SharedQpuRuntime(state_store=store, reset_models=registry, config=config, idle_model=idle_model)
    runtime.configure_job_authorization(JobAuthorizationPolicy.from_manifest(
        manifest, attacker_tenant_id="tenant-a", victim_tenant_id="tenant-v"))
    service = JobApplicationService(
        runtime=runtime, program_compiler=LocalCircuitCompiler.from_manifest(manifest),
        scenario_manifest_id=manifest.manifest_id,
        principals=tuple(PrincipalGrant(p, f"tenant-{p}", frozenset(ALL_OPERATIONS)) for p in ("a", "v")),
    )
    return store, runtime, service, config


def run(delay_us, shots=8):
    store, runtime, service, config = build(delay_us)
    job = service.submit_program("a", request(text=program_json(1), shots=shots))
    record = runtime.run_next()
    assert record.status is JobStatus.SUCCEEDED
    return store, runtime, service.get_result("a", job.job_id).result["counts"], record, config


class RepetitionDelayTests(unittest.TestCase):
    def test_the_manifest_value_is_explicit_and_validated(self):
        model = PhenomenologicalResetModel(ImperfectResetSpec(1.0))
        base = dict(reset_model_id=model.model_id, reset_model_configuration_id=model.configuration_id)
        self.assertEqual(SharedQpuRuntimeConfig(**base).to_manifest_config()["repetition_delay_us"], 0.0)
        for bad in (-1.0, math.inf, math.nan):
            with self.assertRaises(ValueError):
                SharedQpuRuntimeConfig(**base, repetition_delay_us=bad)

    def test_a_positive_delay_needs_an_explicit_idle_model(self):
        with self.assertRaisesRegex(ValueError, "idle model"):
            build(5.0, idle=False)
        build(0.0, idle=False)

    def test_no_delay_keeps_the_inherited_state_and_the_previous_timing(self):
        store, _, counts, _, _ = run(0.0)
        self.assertEqual(counts, {"0": 0, "1": 8})
        self.assertAlmostEqual(store.read().device_time_us, 8 * 1.0)

    def test_the_delay_relaxes_the_inherited_state_between_shots_only(self):
        store, _, counts, _, _ = run(8.0 * T1_US)
        # The first shot sees the prepared |1>; later shots follow 8*T1 of relaxation.
        self.assertGreaterEqual(counts.get("1", 0), 1)
        self.assertLess(counts.get("1", 0), 4)
        # 8 measurements and 7 gaps; no wait before the first shot or after the last.
        self.assertAlmostEqual(store.read().device_time_us, 8 * 1.0 + 7 * 8.0)

    def test_the_planned_duration_matches_the_executed_events(self):
        store, runtime, _, record, config = run(3.0, shots=5)
        planned = compiled_job_duration(record, config, None)
        self.assertAlmostEqual(planned, store.read().device_time_us)
        self.assertAlmostEqual(planned, 5 * 1.0 + 4 * 3.0)

    def test_a_single_shot_has_no_gap(self):
        store, _, counts, _, _ = run(50.0, shots=1)
        self.assertEqual(counts, {"0": 0, "1": 1})
        self.assertAlmostEqual(store.read().device_time_us, 1.0)


if __name__ == "__main__":
    unittest.main()

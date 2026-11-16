"""A qubit with a third level through the single-region runtime: reset, gates, idle time and readout (D91)."""

import math
import unittest
from dataclasses import replace

import numpy as np

from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.qpu_state import (
    InactiveRegionState, QpuStateSnapshot, QpuStateStore, local_zero_snapshot,
)
from core.attack_simulation.register_readout import assign_bit, register_assignment_errors
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel, ResetModelRegistry
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from core.experimental_isolation_physics.reset_model import (
    PerQubitPhenomenologicalResetModel, ResetModelRequest,
)
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.job_service import JobApplicationService, PrincipalGrant
from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.register_operations import freeze_register
from tests.test_local_program_runtime import program_json
from tests.test_program_initialization import ALL_OPERATIONS, manifest_with_profile, request

IDS = (3, 7)
DIMS = (3, 2)


def leaked_state(level0=2, level1=0):
    state = np.zeros((6, 6), dtype=complex)
    index = level0 * 2 + level1
    state[index, index] = 1.0
    return freeze_register(state)


def build(*, initial, t1=math.inf, read_two_as_zero=0.0, success=1.0):
    model = PhenomenologicalResetModel(ImperfectResetSpec(success), observed_zero_given_higher=(read_two_as_zero,))
    config = SharedQpuRuntimeConfig(reset_model_id=model.model_id, reset_model_configuration_id=model.configuration_id)
    manifest = manifest_with_profile(("prepare", "inherit"), None, config)
    snapshot = replace(local_zero_snapshot(qpu_id="qpu-1", physical_qubit_ids=IDS, device_config_version="test-v1",
                                           local_dimension=DIMS), density_matrix=initial)
    store = QpuStateStore(snapshot)
    registry = ResetModelRegistry()
    registry.register(model)
    idle = IndependentIdleModel((IdleQubitSpec(t1, math.inf, 0.0, levels=3), IdleQubitSpec(t1, math.inf, 0.0)), 2)
    runtime = SharedQpuRuntime(state_store=store, reset_models=registry, config=config, idle_model=idle)
    runtime.configure_job_authorization(JobAuthorizationPolicy.from_manifest(
        manifest, attacker_tenant_id="tenant-a", victim_tenant_id="tenant-v"))
    service = JobApplicationService(
        runtime=runtime, program_compiler=LocalCircuitCompiler.from_manifest(manifest),
        scenario_manifest_id=manifest.manifest_id,
        principals=tuple(PrincipalGrant(p, f"tenant-{p}", frozenset(ALL_OPERATIONS)) for p in ("a", "v")))
    return store, runtime, service


def run(service, runtime, columns, *, mode="prepare", shots=1, measure=((0, 0), (1, 1))):
    text = program_json(2, list(columns), measurements=list(measure), classical_bits=2)
    job = service.submit_program("a", request(text=text, mode=mode, shots=shots))
    record = runtime.run_next()
    assert record.status is JobStatus.SUCCEEDED, record
    return {bits: n for bits, n in service.get_result("a", job.job_id).result["counts"].items() if n}


def populations(store):
    return np.diag(np.asarray(store.read().density_matrix)).real.reshape(DIMS)


class LeakedLevelTests(unittest.TestCase):
    def test_the_initializing_reset_does_not_return_a_leaked_qubit(self):
        """Xu et al.: the measurement-based reset leaves |2> in place, and the readout calls it 1."""
        store, runtime, service = build(initial=leaked_state(2, 1))
        self.assertEqual(run(service, runtime, []), {"10": 1})
        self.assertAlmostEqual(populations(store)[2, 0], 1.0)
        # The same qubit in |1> is reset as usual.
        store, runtime, service = build(initial=leaked_state(1, 1))
        self.assertEqual(run(service, runtime, []), {"00": 1})

    def test_a_gate_on_the_qubit_levels_leaves_a_leaked_state_alone(self):
        store, runtime, service = build(initial=leaked_state(2, 0))
        self.assertEqual(run(service, runtime, [[{"type": "X", "targets": [0]}, {"type": "X", "targets": [1]}]]),
                         {"11": 1})
        self.assertAlmostEqual(populations(store)[2, 1], 1.0)
        store, runtime, service = build(initial=leaked_state(2, 0))
        self.assertEqual(run(service, runtime, [[{"type": "H", "targets": [0]}],
                                                 [{"type": "CNOT", "controls": [0], "targets": [1]}]]), {"10": 1})

    def test_the_readout_of_a_leaked_level_uses_its_declared_assignment(self):
        _, runtime, service = build(initial=leaked_state(2, 0), read_two_as_zero=1.0)
        self.assertEqual(run(service, runtime, []), {"00": 1})
        # Every shot's reset leaves |2> in place, so each shot reads it again: P(read 1) = 1 - p02 = 0.7.
        _, runtime, service = build(initial=leaked_state(2, 0), read_two_as_zero=0.3)
        counts = run(service, runtime, [], shots=32)
        self.assertEqual(set(counts) | {"10", "00"}, {"10", "00"})
        self.assertAlmostEqual(counts.get("10", 0) / 32, 0.7, delta=0.25)
        self.assertGreater(counts.get("00", 0), 0)

    def test_a_leaked_qubit_decays_down_the_ladder_while_it_idles(self):
        """From |2>: P(2) = e^{-2 t/T1}, P(1) = 2 (e^{-t/T1} - e^{-2 t/T1})."""
        t1, wait = 40.0, 13.0
        store, runtime, service = build(initial=leaked_state(2, 0), t1=t1)
        run(service, runtime, [[{"type": "I", "targets": [0], "params": {"duration_us": wait}}]], mode="inherit",
            measure=((1, 1),))
        levels = populations(store).sum(axis=1)
        e1, e2 = math.exp(-wait / t1), math.exp(-2.0 * wait / t1)
        # The readout of qubit 1 takes the measurement time, during which qubit 0 keeps relaxing.
        elapsed = wait + runtime._active_service_durations()[1]
        e1, e2 = math.exp(-elapsed / t1), math.exp(-2.0 * elapsed / t1)
        np.testing.assert_allclose(levels, [1 - 2 * e1 + e2, 2 * (e1 - e2), e2], atol=1e-12)

    def test_an_undeclared_readout_of_a_leaked_level_is_refused(self):
        model = PhenomenologicalResetModel(ImperfectResetSpec(1.0))
        with self.assertRaisesRegex(ValueError, "no default"):
            model.run_register(ResetModelRequest(leaked_state(), subsystem_count=2, targets=(0,), dims=DIMS))
        self.assertEqual(assign_bit((0.1, 0.2), 1, 0.15), 0)
        self.assertEqual(assign_bit((0.1, 0.2), 1, 0.25), 1)
        self.assertEqual(assign_bit((0.1, 0.2, 0.7), 2, 0.5), 0)
        with self.assertRaises(ValueError):
            assign_bit((0.1, 0.2), 2, 0.5)


class DeclarationTests(unittest.TestCase):
    def test_two_level_configuration_ids_are_unchanged(self):
        plain = PhenomenologicalResetModel(ImperfectResetSpec(0.9), 0.01, 0.02)
        self.assertEqual(plain.configuration_id,
                         replace(plain, observed_zero_given_higher=()).configuration_id)
        self.assertNotEqual(plain.configuration_id,
                            replace(plain, observed_zero_given_higher=(0.1,)).configuration_id)
        per_qubit = PerQubitPhenomenologicalResetModel((3, 7), (ImperfectResetSpec(0.9),) * 2,
                                                      ((0.01, 0.02), (0.01, 0.02)))
        leaked = PerQubitPhenomenologicalResetModel((3, 7), (ImperfectResetSpec(0.9),) * 2,
                                                    ((0.01, 0.02, 0.3), (0.01, 0.02)))
        self.assertNotEqual(per_qubit.configuration_id, leaked.configuration_id)
        self.assertEqual(register_assignment_errors(leaked, subsystem_count=2, targets=(0,)), ((0.01, 0.02, 0.3),))

    def test_the_state_store_keeps_each_regions_levels(self):
        snapshot = local_zero_snapshot(qpu_id="q", physical_qubit_ids=(1, 2), device_config_version="v",
                                       local_dimension=(3, 2))
        self.assertEqual((snapshot.dims, snapshot.dimension), ((3, 2), 6))
        self.assertEqual(local_zero_snapshot(qpu_id="q", physical_qubit_ids=(1, 2), device_config_version="v",
                                             local_dimension=(3, 3)).local_dimension, 3)
        for bad in ((3,), (5, 2), (1, 2)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                local_zero_snapshot(qpu_id="q", physical_qubit_ids=(1, 2), device_config_version="v",
                                    local_dimension=bad)
        with self.assertRaises(ValueError):
            QpuStateSnapshot("shared_qpu_state_v1", "q", 0, 0.0, (1, 2), (3, 2), np.eye(4) / 4.0, "v")
        parked = InactiveRegionState((5,), freeze_register(np.diag([0, 0, 1.0]).astype(complex)), None, 0.0, 3)
        self.assertEqual(parked.dims, (3,))
        with self.assertRaises(ValueError):
            InactiveRegionState((5,), freeze_register(np.eye(2) / 2.0), None, 0.0, 3)
        with self.assertRaises(ValueError):
            local_zero_snapshot(qpu_id="q", physical_qubit_ids=tuple(range(6)), device_config_version="v",
                                local_dimension=4)

    def test_the_idle_model_must_have_the_regions_levels(self):
        model = PhenomenologicalResetModel(ImperfectResetSpec(1.0))
        registry = ResetModelRegistry()
        registry.register(model)
        store = QpuStateStore(local_zero_snapshot(qpu_id="q", physical_qubit_ids=IDS, device_config_version="v",
                                                  local_dimension=DIMS))
        config = SharedQpuRuntimeConfig(reset_model_id=model.model_id, reset_model_configuration_id=model.configuration_id)
        two_level = IndependentIdleModel((IdleQubitSpec(math.inf, math.inf, 0.0),) * 2, 2)
        with self.assertRaisesRegex(ValueError, "levels"):
            SharedQpuRuntime(state_store=store, reset_models=registry, config=config, idle_model=two_level)


if __name__ == "__main__":
    unittest.main()

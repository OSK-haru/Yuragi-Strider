"""A disjoint register remains authoritative while another tenant is active."""

import unittest
import math
from unittest.mock import patch

from core.attack_simulation.qpu_state import (
    InactiveRegionState, QpuStateStore, QpuStateTransition, local_zero_snapshot,
)
from core.attack_simulation.qpu_topology import QpuTopology, RetainedRegionBinding, TopologyLimits
from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.declared_execution import DeclaredQpuExecutor, JobExecutionWindow
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.jobs import JobCommand, JobStatus
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntime, SharedQpuRuntimeConfig
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, IndependentIdleModel
from core.experimental_isolation_physics.imperfect_reset import ImperfectResetSpec
from core.experimental_isolation_physics.reset_model import PhenomenologicalResetModel, ResetModelRegistry
from tests.test_local_program_runtime import program_json


def regional_store() -> QpuStateStore:
    topology = QpuTopology("qpu-1", "two-regions", (0, 1, 2, 3), TopologyLimits(4, 4, 4),
                           gate_edges=((0, 1), (2, 3)))
    first = RetainedRegionBinding(topology, (0, 1), 2)
    second = RetainedRegionBinding(topology, (2, 3), 2)
    zero = local_zero_snapshot(qpu_id="qpu-1", physical_qubit_ids=(0, 1),
                               device_config_version="v1")
    parked = local_zero_snapshot(qpu_id="qpu-1", physical_qubit_ids=(2, 3),
                                 device_config_version="v1")
    return QpuStateStore(zero, region_binding=first, alternate_region_binding=second,
                         inactive_region=InactiveRegionState((2, 3), parked.density_matrix, None, 0.0))


def transition(store: QpuStateStore, event_id: str, kind: str, state, owner,
               target=None, duration=0.0):
    return store.commit(store.read().revision, QpuStateTransition(
        event_id, kind, "job-1", state, duration, owner,
        next_physical_qubit_ids=target,
    ))


class RegionalStateBankTests(unittest.TestCase):
    def test_declared_execution_switches_without_changing_prior_physics(self):
        store = regional_store()
        model = PhenomenologicalResetModel(ImperfectResetSpec(1.0, (1.0, 0.0)))
        registry = ResetModelRegistry()
        registry.register(model)
        idle = IndependentIdleModel((IdleQubitSpec(float("inf"), float("inf"), 0.0),) * 2, 2)
        runtime = SharedQpuRuntime(
            state_store=store, reset_models=registry,
            config=SharedQpuRuntimeConfig(model.model_id, model.configuration_id,
                placement_policy=PlacementPolicy.TENANT_DEDICATED_REGION,
                max_local_qubits=2),
            tenant_regions={"victim": (0, 1), "attacker": (2, 3)},
            regional_reset_models={(0, 1): registry, (2, 3): registry},
            regional_idle_models={(0, 1): idle, (2, 3): idle},
            regional_service_durations={(0, 1): (1.0, 1.0), (2, 3): (1.0, 1.0)},
        )
        executor = DeclaredQpuExecutor(runtime=runtime, clock_domain="world", max_jobs=4,
                                       max_events_per_job=8, max_unresolved=8, idle_model=idle)
        compiler = LocalCircuitCompiler(max_program_qubits=2)
        start = 1.0
        for index, (tenant, region) in enumerate((("victim", (0, 1)), ("attacker", (2, 3)))):
            request = ProgramSubmitRequest(
                CircuitProgram.from_editor_json(program_json(2, [[{"type": "X", "targets": [0]}]])),
                "qpu-1", 1, f"request-{index}",
            )
            operation = compiler.compile(request, sampling_seed=1)
            job = runtime.submit(JobCommand(tenant, operation, request.idempotency_key,
                                            layout=PhysicalQubitLayout("qpu-1", region)))
            duration = executor._duration(job, store.read())
            window = JobExecutionWindow(
                EventPosition("world", start - 0.5, 1), EventPosition("world", start, 2),
                EventPosition("world", start + duration, 3),
                EventPosition("world", start + duration + 1.0, 4),
            )
            executor.register_job(job.job_id, window)
            executor.frontier.set_target(EventPosition("world", 100, 99))
            executor.frontier.seal_ingress(EventPosition("world", 100, 99))
            prepared = executor.prepare_next()
            executor.commit(prepared)
            start += duration + 2.0
        self.assertEqual(store.read().physical_qubit_ids, (2, 3))
        self.assertAlmostEqual(store.inactive_region().density_matrix[2][2].real, 1.0)
        self.assertEqual(runtime.scheduler.get(job.job_id).status, JobStatus.SUCCEEDED)

    def test_common_fifo_programs_switch_between_two_preserved_registers(self):
        store = regional_store()
        model = PhenomenologicalResetModel(ImperfectResetSpec(1.0, (1.0, 0.0)))
        registry = ResetModelRegistry()
        registry.register(model)
        idle = IndependentIdleModel((IdleQubitSpec(10.0, float("inf"), 0.0),) * 2, 2)
        quiet_idle = IndependentIdleModel((IdleQubitSpec(float("inf"), float("inf"), 0.0),) * 2, 2)
        regions = {"victim": (0, 1), "attacker": (2, 3)}
        runtime = SharedQpuRuntime(
            state_store=store, reset_models=registry,
            config=SharedQpuRuntimeConfig(
                model.model_id, model.configuration_id,
                placement_policy=PlacementPolicy.TENANT_DEDICATED_REGION,
                max_local_qubits=2,
            ),
            tenant_regions=regions,
            regional_reset_models={(0, 1): registry, (2, 3): registry},
            regional_idle_models={(0, 1): idle, (2, 3): quiet_idle},
            regional_service_durations={(0, 1): (1.0, 1.0), (2, 3): (1.0, 1.0)},
        )
        compiler = LocalCircuitCompiler(max_program_qubits=2)
        restored_probabilities = []
        for tenant, region in (("victim", (0, 1)), ("attacker", (2, 3)), ("victim", (0, 1))):
            request = ProgramSubmitRequest(
                CircuitProgram.from_editor_json(program_json(2, [[{"type": "X", "targets": [0]}]])),
                "qpu-1", 1, f"request-{tenant}-{store.read().revision}",
            )
            operation = compiler.compile(request, sampling_seed=1)
            command = JobCommand(tenant, operation, request.idempotency_key,
                                 layout=PhysicalQubitLayout("qpu-1", region))
            job = runtime.submit(command)
            if tenant == "victim" and store.read().physical_qubit_ids == (2, 3):
                parked = store.inactive_region()
                expected = math.exp(-(store.read().device_time_us - parked.updated_at_us) / 10.0)
                original = runtime._execute_circuit
                def capture(next_job, next_operation):
                    restored_probabilities.append(store.read().density_matrix[2][2].real)
                    return original(next_job, next_operation)
                with patch.object(runtime, "_execute_circuit", side_effect=capture):
                    self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
                self.assertAlmostEqual(restored_probabilities[0], expected, places=10)
            else:
                self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
            self.assertEqual(runtime.scheduler.get(job.job_id).result["counts"], {"10": 1})
            if tenant == "attacker":
                self.assertEqual(store.inactive_region().physical_qubit_ids, (0, 1))
                self.assertAlmostEqual(store.inactive_region().density_matrix[2][2].real, 1.0)
        self.assertEqual([event.event_kind for event in store.events()].count("tenant_region_assignment"), 2)

    def test_switch_keeps_inactive_density_and_owner(self):
        store = regional_store()
        excited = ((0j, 0j, 0j, 0j), (0j, 0j, 0j, 0j),
                   (0j, 0j, 1 + 0j, 0j), (0j, 0j, 0j, 0j))
        transition(store, "event-1", "program_shot", excited, "victim", duration=1.0)
        transition(store, "event-2", "tenant_region_assignment",
                   store.inactive_region().density_matrix, None, target=(2, 3))
        self.assertEqual(store.read().physical_qubit_ids, (2, 3))
        self.assertEqual(store.inactive_region().density_matrix, excited)
        self.assertEqual(store.inactive_region().owner_tenant_id, "victim")
        self.assertEqual(store.inactive_region().updated_at_us, 1.0)
        detached = store.clone_detached()
        transition(detached, "event-3", "tenant_region_assignment",
                   detached.inactive_region().density_matrix, "victim", target=(0, 1))
        self.assertEqual(detached.read().density_matrix, excited)
        self.assertEqual(store.read().physical_qubit_ids, (2, 3))

    def test_prepared_batch_switch_and_rollback_restore_both_regions(self):
        store = regional_store()
        before, parked = store.read(), store.inactive_region()
        scratch = store.clone_detached()
        transition(scratch, "event-1", "tenant_region_assignment",
                   parked.density_matrix, None, target=(2, 3))
        token = store.prepare_batch(before, scratch.read(), scratch.events(), 2,
                                    final_inactive_region=scratch.inactive_region())
        store.commit_prepared(token)
        self.assertEqual(store.read().physical_qubit_ids, (2, 3))
        store._rollback_prepared_commit(token)
        self.assertIs(store.read(), before)
        self.assertIs(store.inactive_region(), parked)
        self.assertEqual(store.events(), ())

    def test_rejects_overlap_and_unrelated_region(self):
        store = regional_store()
        with self.assertRaisesRegex(ValueError, "other retained region"):
            transition(store, "event-1", "tenant_region_assignment",
                       store.read().density_matrix, None, target=(4, 5))
        self.assertEqual(store.read().revision, 0)
        with self.assertRaisesRegex(ValueError, "frozen bindings"):
            transition(store, "event-2", "isolated_resource_assignment",
                       store.read().density_matrix, None, target=(4, 5))

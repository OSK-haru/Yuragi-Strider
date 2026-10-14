"""Pure FIFO plans, executor duration parity, and finite-coordinate failures."""

from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
import json
import unittest

from core.attack_simulation.circuit_program import CircuitExecutionOperation, CircuitInstruction, LocalCircuitCompiler
from core.attack_simulation.declared_execution import DeclaredQpuExecutor, JobExecutionWindow
from core.attack_simulation.declared_service_timing import DeclaredServiceTiming, compiled_job_duration
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.jobs import JobCommand, JobRecord, JobStatus, VictimPrepareOperation
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntimeConfig


def config(*, reset=3.0, measurement=5.0):
    return SharedQpuRuntimeConfig(
        reset_model_id="declared-test", reset_model_configuration_id="declared-test-v1",
        reset_duration_us=reset, program_measurement_duration_us=measurement,
    )


def job(*, tenant="tenant-a", shots=2, inherit=False, explicit_reset=False,
        unitary_duration=7.0, measurement_groups=(10, 10), physical_ids=(91, 5)):
    width = len(physical_ids)
    instructions = [] if inherit else [
        CircuitInstruction("initialize", 100.0, qubits=tuple(range(width)))
    ]
    if explicit_reset:
        instructions.append(CircuitInstruction("reset", 200.0, qubits=(width - 1,)))
    instructions.append(CircuitInstruction("unitary", unitary_duration,
                                           ((0j, 1 + 0j), (1 + 0j, 0j)), (0,)))
    instructions.extend(CircuitInstruction("measure", 300.0, qubits=(q,),
                                           classical_targets=(q,), time_group=group)
                        for q, group in enumerate(measurement_groups))
    operation = CircuitExecutionOperation(tuple(instructions), shots, 17, width, width,
                                           "inherit" if inherit else "prepare")
    command = JobCommand(tenant, operation, "timing-test", layout=PhysicalQubitLayout("qpu-test", physical_ids))
    return JobRecord("job-test", 1, command, JobStatus.QUEUED, (JobStatus.QUEUED,))


def pos(time=0, order=0, domain="world"):
    return EventPosition(domain, time, order)


class DeclaredServiceTimingTests(unittest.TestCase):
    def test_timing_is_immutable_and_has_no_implicit_fields(self):
        timing = DeclaredServiceTiming("world", 2, 4, 9)
        self.assertEqual((timing.processing_delay_us, timing.publication_delay_us, timing.denial_delay_us),
                         (2.0, 4.0, 9.0))
        with self.assertRaises(FrozenInstanceError):
            timing.processing_delay_us = 8
        with self.assertRaises(TypeError):
            DeclaredServiceTiming("world", 0, 0)

    def test_invalid_declared_assumptions_are_rejected(self):
        for field in ("processing_delay_us", "publication_delay_us", "denial_delay_us"):
            for value in (-1, float("nan"), float("inf"), -float("inf"), True, "1", None, 10**400):
                with self.subTest(field=field, value=repr(value)):
                    values = dict(clock_domain="world", processing_delay_us=0,
                                  publication_delay_us=0, denial_delay_us=0)
                    values[field] = value
                    with self.assertRaises(ValueError):
                        DeclaredServiceTiming(**values)
        for domain in ("", "x" * 129, "世界", None):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                DeclaredServiceTiming(domain, 0, 0, 0)

    def test_compiled_duration_matches_executor_for_program_combinations(self):
        runtime_config = config()
        for inherit in (False, True):
            for explicit_reset in (False, True):
                for groups in ((10, 10), (10, 11)):
                    for previous_tenant in (None, "tenant-a", "tenant-b"):
                        with self.subTest(inherit=inherit, reset=explicit_reset,
                                          groups=groups, previous=previous_tenant):
                            record = job(inherit=inherit, explicit_reset=explicit_reset,
                                         measurement_groups=groups)
                            expected = 2 * (7 + 3 * (int(not inherit) + int(explicit_reset))
                                            + 5 * len(set(groups)))
                            expected += 3 if previous_tenant == "tenant-b" else 0
                            actual = compiled_job_duration(record, runtime_config, previous_tenant)
                            self.assertEqual(actual, expected)
                            # Execute the existing pure duration method without constructing owners.
                            adapter = SimpleNamespace(runtime=SimpleNamespace(config=runtime_config))
                            snapshot = SimpleNamespace(owner_tenant_id=previous_tenant)
                            self.assertEqual(actual, DeclaredQpuExecutor._duration(adapter, record, snapshot))

    def test_handover_is_not_multiplied_by_shots_or_reset_count(self):
        runtime_config = replace(config(), reset_count=0)
        record = job(shots=4, inherit=True)
        self.assertEqual(compiled_job_duration(record, runtime_config, "tenant-b"), 4 * (7 + 5) + 3)

    def test_real_compiler_parallel_columns_are_not_charged_per_qubit(self):
        program = CircuitProgram.from_editor_json(json.dumps({
            "logical_qubits": 2, "classical_bits": 2, "initial_states": [1, 1],
            "columns": [
                {"step": 0, "gates": [{"type": "H", "targets": [q]} for q in range(2)]},
                {"step": 1, "gates": [{"type": "MEASURE", "targets": [q],
                                       "classical_targets": [q]} for q in range(2)]},
            ],
        }))
        request = ProgramSubmitRequest(program, "qpu-test", 3, "parallel")
        operation = LocalCircuitCompiler().compile(request, sampling_seed=17)
        record = job()
        record = replace(record, command=replace(record.command, operation=operation))
        # Preparation Xs and column Hs each occupy one compiled duration slot.
        self.assertEqual([i.duration_us for i in operation.instructions if i.kind == "unitary"],
                         [0.02, 0.0, 0.02, 0.0])
        self.assertAlmostEqual(compiled_job_duration(record, config(), "tenant-b"),
                               3 * (3 + 0.02 + 0.02 + 5) + 3)

    def test_eight_qubit_program_preserves_noncontiguous_layout(self):
        record = job(physical_ids=(91, 5, 77, 13, 6, 200, 4, 30), measurement_groups=(10,) * 8)
        timing = DeclaredServiceTiming("world", 0, 0, 0)
        window = timing.plan_job_window(record, config(), previous_finish=None,
                                       previous_tenant_id=None, arrival=pos(order=456))
        self.assertEqual(window.finish.time_us, 2 * (3 + 7 + 5))
        self.assertEqual(record.command.layout.physical_qubit_ids, (91, 5, 77, 13, 6, 200, 4, 30))

    def test_processing_then_physics_then_publication_are_distinct(self):
        timing = DeclaredServiceTiming("world", 2, 4, 900)
        window = timing.plan_job_window(job(), config(), previous_finish=None,
                                       previous_tenant_id=None, arrival=pos(10, 77))
        self.assertIs(type(window), JobExecutionWindow)
        self.assertEqual(window.arrival, pos(10, 77))
        self.assertEqual((window.start.time_us, window.finish.time_us, window.release.time_us), (12, 42, 46))
        self.assertEqual((window.start.order, window.finish.order, window.release.order), (0, 0, 0))
        with self.assertRaises(FrozenInstanceError):
            window.start = pos(99)

    def test_fifo_ties_advance_after_both_arbitrary_arrival_and_previous_finish(self):
        timing = DeclaredServiceTiming("world", 0, 0, 0)
        for arrival_order, finish_order in ((0, 0), (99, 4), (4, 99), (10**30, 10**30 + 1)):
            with self.subTest(arrival=arrival_order, finish=finish_order):
                arrival, previous = pos(10, arrival_order), pos(10, finish_order)
                window = timing.plan_job_window(job(), config(), previous_finish=previous,
                                               previous_tenant_id="tenant-a", arrival=arrival)
                self.assertEqual(window.start, pos(10, max(arrival_order, finish_order) + 1))
                self.assertGreater(window.start.key, previous.key)
                self.assertGreater(window.start.key, arrival.key)

    def test_queue_busy_and_idle_and_processing_tie_choose_correct_start(self):
        timing = DeclaredServiceTiming("world", 2, 0, 0)
        for arrival, previous, expected in (
            (pos(2, 90), pos(10, 7), pos(10, 8)),
            (pos(20, 90), pos(10, 7), pos(22, 0)),
            (pos(8, 90), pos(10, 7), pos(10, 8)),
            (pos(8, 90), pos(0, 9999), pos(10, 0)),
        ):
            with self.subTest(arrival=arrival, previous=previous):
                window = timing.plan_job_window(job(), config(), previous_finish=previous,
                                               previous_tenant_id="tenant-a", arrival=arrival)
                self.assertEqual(window.start, expected)

    def test_zero_physical_duration_has_strict_causal_keys_without_fake_wait(self):
        timing = DeclaredServiceTiming("world", 0, 0, 0)
        record = job(inherit=True, unitary_duration=0)
        window = timing.plan_job_window(record, config(reset=0, measurement=0),
                                       previous_finish=pos(10, 99), previous_tenant_id="tenant-b",
                                       arrival=pos(10, 12))
        self.assertEqual((window.start, window.finish, window.release),
                         (pos(10, 100), pos(10, 101), pos(10, 102)))

    def test_publication_does_not_block_successor_and_replay_is_pure(self):
        timing = DeclaredServiceTiming("world", 0, 100, 0)
        record, runtime_config, arrival = job(), config(), pos(0, 800)
        before = (record, runtime_config, arrival, timing)
        first = timing.plan_job_window(record, runtime_config, previous_finish=None,
                                      previous_tenant_id=None, arrival=arrival)
        second = timing.plan_job_window(record, runtime_config, previous_finish=first.finish,
                                       previous_tenant_id="tenant-a", arrival=pos(1, 900))
        self.assertEqual(second.start.time_us, first.finish.time_us)
        self.assertGreater(second.start.key, first.finish.key)
        self.assertLess(second.start.time_us, first.release.time_us)
        self.assertEqual(first, timing.plan_job_window(record, runtime_config, previous_finish=None,
                                                     previous_tenant_id=None, arrival=arrival))
        self.assertEqual(before, (record, runtime_config, arrival, timing))

    def test_mixed_domains_and_non_positions_are_rejected(self):
        timing = DeclaredServiceTiming("world", 0, 0, 0)
        for arrival, previous, error in (
            (pos(domain="other"), None, ValueError),
            (pos(), pos(domain="other"), ValueError),
            (None, None, TypeError),
            (pos(), 0, TypeError),
        ):
            with self.subTest(arrival=arrival, previous=previous), self.assertRaises(error):
                timing.plan_job_window(job(), config(), previous_finish=previous,
                                       previous_tenant_id=None, arrival=arrival)

    def test_unsupported_operations_configs_and_placement_are_rejected(self):
        record = job()
        legacy = replace(record, command=replace(record.command, operation=VictimPrepareOperation(0)))
        for candidate, runtime_config, previous, error in (
            (legacy, config(), None, TypeError),
            (record.command, config(), None, TypeError),
            (record, None, None, TypeError),
            (record, config(), "", ValueError),
            (record, config(), 7, ValueError),
            (record, replace(config(), placement_policy=PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE), None, ValueError),
        ):
            with self.subTest(candidate=type(candidate), previous=previous), self.assertRaises(error):
                compiled_job_duration(candidate, runtime_config, previous)

    def test_finite_inputs_that_overflow_compiled_duration_are_rejected(self):
        for record, runtime_config, previous in (
            (job(unitary_duration=1e308, explicit_reset=True), config(reset=1e308), None),
            (job(measurement_groups=(10, 11)), config(measurement=1e308), None),
            (job(unitary_duration=1e308), config(), None),
            (job(shots=1, inherit=True, unitary_duration=1e308), config(reset=1e308), "tenant-b"),
        ):
            with self.subTest(record=record), self.assertRaisesRegex(ValueError, "compiled job duration"):
                compiled_job_duration(record, runtime_config, previous)

    def test_position_addition_overflow_is_rejected_at_each_stage(self):
        for timing, record, runtime_config, arrival, previous in (
            (DeclaredServiceTiming("world", 1e308, 0, 0), job(), config(), pos(1e308), None),
            (DeclaredServiceTiming("world", 0, 0, 0), job(shots=1, inherit=True, unitary_duration=1e308),
             config(), pos(1e308), None),
            (DeclaredServiceTiming("world", 0, 1e308, 0), job(unitary_duration=0),
             config(reset=0, measurement=0), pos(), pos(1e308)),
        ):
            with self.subTest(timing=timing), self.assertRaises(ValueError):
                timing.plan_job_window(record, runtime_config, previous_finish=previous,
                                       previous_tenant_id=None, arrival=arrival)

    def test_positive_duration_cannot_silently_round_to_zero_at_large_time(self):
        timing = DeclaredServiceTiming("world", 1, 0, 0)
        with self.assertRaisesRegex(ValueError, "not representable"):
            timing.plan_job_window(job(), config(), previous_finish=None,
                                   previous_tenant_id=None, arrival=pos(1e308))

    def test_planning_context_is_keyword_only(self):
        timing = DeclaredServiceTiming("world", 0, 0, 0)
        with self.assertRaises(TypeError):
            timing.plan_job_window(job(), config(), None, None, pos())

    def test_denial_release_uses_only_denial_delay_and_advances_zero_delay(self):
        arrival = pos(10, 12345)
        self.assertEqual(DeclaredServiceTiming("world", 700, 800, 2).denial_release(arrival), pos(12, 0))
        self.assertEqual(DeclaredServiceTiming("world", 700, 800, 0).denial_release(arrival), pos(10, 12346))
        self.assertEqual(arrival, pos(10, 12345))

    def test_denial_release_rejects_foreign_invalid_and_overflowing_positions(self):
        timing = DeclaredServiceTiming("world", 0, 0, 1e308)
        for arrival, error in ((pos(domain="other"), ValueError), (None, TypeError),
                               (pos(1e308), ValueError)):
            with self.subTest(arrival=arrival), self.assertRaises(error):
                timing.denial_release(arrival)


if __name__ == "__main__":
    unittest.main()

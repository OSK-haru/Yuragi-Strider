"""Capabilities and shared-lock publication for declared execution stores."""

from copy import copy, deepcopy
from dataclasses import FrozenInstanceError, replace
from threading import Event, Lock, RLock, Thread
import unittest
from unittest.mock import patch

from core.attack_simulation.jobs import (
    FifoJobScheduler, JobCommand, JobRetentionExpiredError, JobStatus, VictimPrepareOperation,
)
from core.attack_simulation.qpu_state import (
    QpuStateStore, QpuStateTransition, RevisionConflictError,
    single_qubit_zero_snapshot,
)


def _store() -> QpuStateStore:
    return QpuStateStore(single_qubit_zero_snapshot(
        qpu_id="qpu-1", physical_qubit_id=0, device_config_version="test-v1",
    ))


def _batch(store: QpuStateStore, job_id: str = "job-1"):
    expected = store.read()
    detached = QpuStateStore(expected, region_binding=store.region_binding)
    detached.commit(expected.revision, QpuStateTransition(
        "idle-1", "idle_evolution", job_id, expected.density_matrix, 2.0,
        expected.owner_tenant_id,
    ))
    detached.commit(expected.revision + 1, QpuStateTransition(
        "shot-1", "program_shot", job_id,
        ((0.5 + 0j, 0.5 + 0j), (0.5 + 0j, 0.5 + 0j)), 3.0, "victim",
        scenario_manifest_id="scenario-1",
    ))
    return expected, detached.read(), detached.events()


def _submitted(scheduler: FifoJobScheduler, key: str = "first"):
    return scheduler.submit(JobCommand("victim", VictimPrepareOperation(1), key))


def _running(scheduler: FifoJobScheduler):
    submitted = _submitted(scheduler)
    scheduler.allocate_next()
    scheduler.mark_running(submitted.job_id)
    return submitted.job_id


class PublicSchedulerResultQueryTests(unittest.TestCase):
    def test_tenant_query_returns_detached_successful_results_only(self):
        scheduler = FifoJobScheduler()
        first = _submitted(scheduler, "first")
        second = scheduler.submit(JobCommand("attacker", VictimPrepareOperation(1), "second"))
        scheduler.allocate_next()
        scheduler.mark_running(first.job_id)
        scheduler.mark_succeeded(first.job_id, {"counts": {"0": 3}})
        self.assertEqual(scheduler.completed_job_results_for_tenant("attacker"), ())
        records = scheduler.completed_job_results_for_tenant("victim")
        self.assertEqual(tuple(record.job_id for record in records), (first.job_id,))
        records[0].result["counts"]["0"] = 99
        self.assertEqual(scheduler.get(first.job_id).result, {"counts": {"0": 3}})
        self.assertEqual(scheduler.get(second.job_id).status, JobStatus.QUEUED)


class TransactionBindingTests(unittest.TestCase):
    def test_common_rlock_binds_once_and_rejects_invalid_locks(self):
        shared = RLock()
        for authority in (_store(), FifoJobScheduler()):
            with self.subTest(authority=type(authority).__name__):
                for invalid in (None, Lock(), object()):
                    with self.assertRaises(TypeError):
                        authority.bind_transaction_lock(invalid)
                authority.bind_transaction_lock(shared)
                self.assertIs(authority._lock, shared)
                with self.assertRaises(RuntimeError):
                    authority.bind_transaction_lock(shared)
                with self.assertRaises(RuntimeError):
                    authority.bind_transaction_lock(RLock())

    def test_used_authorities_cannot_bind_even_with_empty_queue(self):
        store = _store()
        expected, final, events = _batch(store)
        store.commit_prepared(store.prepare_batch(expected, final, events, 2))
        with self.assertRaises(RuntimeError):
            store.bind_transaction_lock(RLock())
        restored = QpuStateStore(final)
        with self.assertRaises(RuntimeError):
            restored.bind_transaction_lock(RLock())
        scheduler = FifoJobScheduler()
        scheduler.cancel(_submitted(scheduler).job_id)
        with self.assertRaises(RuntimeError):
            scheduler.bind_transaction_lock(RLock())


class PreparedQpuBatchTests(unittest.TestCase):
    def test_prepare_is_detached_and_commit_never_validates_density(self):
        store = _store()
        expected, final, events = _batch(store)
        token = store.prepare_batch(expected, final, events, max_events=2)
        self.assertIs(store.read(), expected)
        self.assertEqual(store.events(), ())
        self.assertIsNot(token.final_snapshot, final)
        self.assertIsNot(token.events[0], events[0])
        with self.assertRaises(FrozenInstanceError):
            token.final_snapshot = expected
        with patch("core.attack_simulation.qpu_state._validate_density_matrix",
                   side_effect=AssertionError("numerical work during commit")):
            store.validate_prepared(token)
            self.assertEqual(store.commit_prepared(token), final)
        self.assertEqual(store.events(), events)
        with self.assertRaises(ValueError):
            store.commit_prepared(token)

    def test_foreign_forged_and_copied_tokens_are_rejected(self):
        store = _store()
        expected, final, events = _batch(store)
        token = store.prepare_batch(expected, final, events, 2)
        other = _store()
        for forged in (copy(token), deepcopy(token), replace(token), object()):
            with self.subTest(forged=type(forged).__name__):
                with self.assertRaises(ValueError):
                    store.validate_prepared(forged)
                with self.assertRaises(ValueError):
                    store.commit_prepared(forged)
        with self.assertRaises(ValueError):
            other.validate_prepared(token)
        self.assertIs(store.read(), expected)
        self.assertEqual(store.events(), ())

    def test_exact_expected_identity_and_stale_tokens(self):
        store = _store()
        expected, final, events = _batch(store)
        with self.assertRaises(RevisionConflictError):
            store.prepare_batch(replace(expected), final, events, 2)
        token = store.prepare_batch(expected, final, events, 2)
        store.commit(0, QpuStateTransition(
            "other", "program_shot", "other-job", expected.density_matrix, 0.0, None,
        ))
        before = store.read(), store.events()
        with self.assertRaises(RevisionConflictError):
            store.validate_prepared(token)
        with self.assertRaises(RevisionConflictError):
            store.commit_prepared(token)
        self.assertEqual((store.read(), store.events()), before)

    def test_event_ids_cannot_be_reused_from_previous_history(self):
        store = _store()
        expected, final, events = _batch(store)
        store.commit_prepared(store.prepare_batch(expected, final, events, 2))
        before = store.read(), store.events()
        expected, final, events = _batch(store)
        with self.assertRaisesRegex(ValueError, "already committed"):
            store.prepare_batch(expected, final, events, 2)
        self.assertEqual((store.read(), store.events()), before)

    def test_bounded_tuple_required_before_numerical_validation(self):
        store = _store()
        expected, final, events = _batch(store)
        for supplied, cap in ((events, 1), (list(events), 2), (iter(events), 2),
                              (events, 0), (events, True), (events, 2.5)):
            with self.subTest(cap=cap, supplied=type(supplied).__name__):
                with patch("core.attack_simulation.qpu_state._validate_density_matrix",
                           side_effect=AssertionError("oversize candidate validated")):
                    with self.assertRaises(ValueError):
                        store.prepare_batch(expected, final, supplied, cap)
        self.assertEqual(store.events(), ())

    def test_rejects_invalid_chains_and_metadata_without_mutation(self):
        store = _store()
        expected, final, events = _batch(store)
        mutations = (
            {"revision_before": 1}, {"revision_after": 4},
            {"revision_before": False}, {"device_time_start_us": 1.0},
            {"device_time_end_us": -1.0}, {"device_time_end_us": float("inf")},
            {"device_time_end_us": float("nan")}, {"owner_before": "other"},
            {"physical_qubit_ids_before": (1,)}, {"physical_qubit_ids_after": (1,)},
            {"physical_qubit_ids_after": [0]}, {"event_kind": ""},
            {"job_id": ""}, {"scenario_manifest_id": []},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(ValueError):
                    store.prepare_batch(expected, final,
                                        (replace(events[0], **mutation), events[1]), 2)
        with self.assertRaises(ValueError):
            store.prepare_batch(expected, final,
                                (events[0], replace(events[1], event_id=events[0].event_id)), 2)
        for mutation in (
            {"qpu_id": "different"}, {"schema_version": "different"},
            {"device_config_version": "different"}, {"physical_qubit_ids": (1,)},
            {"revision": 3}, {"device_time_us": 6.0}, {"owner_tenant_id": None},
            {"last_event_id": "different"}, {"last_job_id": "different"},
        ):
            with self.subTest(final_mutation=mutation):
                with self.assertRaises(ValueError):
                    store.prepare_batch(expected, replace(final, **mutation), events, 2)
        self.assertIs(store.read(), expected)
        self.assertEqual(store.events(), ())

    def test_invalid_density_is_revalidated_and_empty_batch_cannot_change_state(self):
        store = _store()
        expected, final, events = _batch(store)
        # Frozen dataclasses are not an issuance boundary: reject even a
        # supervisor-supplied instance whose constructor was bypassed.
        object.__setattr__(final, "density_matrix", ((1 + 0j, 2 + 0j), (2 + 0j, 0j)))
        with self.assertRaises(ValueError):
            store.prepare_batch(expected, final, events, 2)
        with self.assertRaises(ValueError):
            store.prepare_batch(expected, replace(expected, density_matrix=((0j, 0j), (0j, 1 + 0j))), (), 1)
        token = store.prepare_batch(expected, expected, (), 1)
        self.assertIs(store.commit_prepared(token), expected)
        with self.assertRaises(ValueError):
            store.commit_prepared(token)

    def test_numerical_validation_does_not_hold_transaction_lock(self):
        store = _store()
        store.bind_transaction_lock(RLock())
        expected, final, events = _batch(store)
        entered, release, read_done = Event(), Event(), Event()
        errors = []
        import core.attack_simulation.qpu_state as module
        original = module._validate_density_matrix

        def held_validation(*args):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("validation release timed out")
            return original(*args)

        def prepare():
            try:
                store.prepare_batch(expected, final, events, 2)
            except Exception as error:
                errors.append(error)

        def read():
            store.read()
            read_done.set()

        with patch.object(module, "_validate_density_matrix", held_validation):
            writer = Thread(target=prepare)
            writer.start()
            reader = Thread(target=read)
            try:
                self.assertTrue(entered.wait(2))
                reader.start()
                self.assertTrue(read_done.wait(1), "reader blocked behind numerical validation")
            finally:
                release.set()
                writer.join(2)
                if reader.ident is not None:
                    reader.join(2)
        self.assertFalse(writer.is_alive())
        self.assertEqual(errors, [])
        self.assertIs(store.read(), expected)


class JobRetentionTests(unittest.TestCase):
    def test_forgotten_terminal_job_reports_expiry_and_its_key_cannot_rerun(self):
        scheduler = FifoJobScheduler()
        job_id = _running(scheduler)
        scheduler.mark_succeeded(job_id, {"prepared": True})

        scheduler._forget_terminal(job_id)

        with self.assertRaises(JobRetentionExpiredError):
            scheduler.get(job_id)
        with self.assertRaises(JobRetentionExpiredError):
            _submitted(scheduler)  # Same idempotency key: refused, never re-queued.
        self.assertEqual(scheduler.queued_job_ids(), ())
        with self.assertRaises(KeyError) as unknown:
            scheduler.get("job-never-existed")
        self.assertNotIsInstance(unknown.exception, JobRetentionExpiredError)

    def test_only_terminal_jobs_without_open_journals_can_be_forgotten(self):
        scheduler = FifoJobScheduler()
        queued = _submitted(scheduler, "queued").job_id
        with self.assertRaisesRegex(ValueError, "terminal"):
            scheduler._forget_terminal(queued)

        scheduler = FifoJobScheduler()
        running = _running(scheduler)
        token = scheduler.prepare_success(running, {"prepared": True})
        with self.assertRaisesRegex(ValueError, "terminal"):
            scheduler._forget_terminal(running)
        scheduler.commit_prepared_success(token)
        # The latest success can still be rolled back until another mutation.
        with self.assertRaisesRegex(ValueError, "rollback journal"):
            scheduler._forget_terminal(running)
        _submitted(scheduler, "next")
        scheduler._forget_terminal(running)


class EventRetentionTests(unittest.TestCase):
    def test_forgetting_old_events_keeps_the_latest_batch_rollback_exact(self):
        store = _store()
        expected, final, events = _batch(store, "job-old")
        store.commit_prepared(store.prepare_batch(expected, final, events, max_events=4))
        current = store.read()
        detached = QpuStateStore(current, region_binding=store.region_binding)
        detached.commit(current.revision, QpuStateTransition(
            "shot-2", "program_shot", "job-new", current.density_matrix, 1.0, "victim"))
        latest = store.prepare_batch(current, detached.read(), detached.events(), max_events=4)
        store.commit_prepared(latest)

        with self.assertRaisesRegex(ValueError, "rollback"):
            store._forget_job_events("job-new")
        self.assertEqual(store._forget_job_events("job-old"), 2)
        self.assertEqual([e.job_id for e in store.events()], ["job-new"])
        self.assertTrue(store._rollback_prepared_commit(latest))
        self.assertEqual(store.events(), ())
        self.assertIs(store.read(), current)


class PreparedJobSuccessTests(unittest.TestCase):
    def test_running_success_detaches_input_and_return_without_commit_copy(self):
        scheduler = FifoJobScheduler()
        job_id = _running(scheduler)
        second = _submitted(scheduler, "second")
        source = {"samples": [1], "summary": {"count": 1}}
        token = scheduler.prepare_success(job_id, source)
        source["samples"].append(0)
        source["summary"]["count"] = 99
        self.assertEqual(scheduler.get(job_id).status, JobStatus.RUNNING)
        with patch("core.attack_simulation.jobs.deepcopy",
                   side_effect=AssertionError("copy during commit")):
            scheduler.validate_prepared(token)
            returned = scheduler.commit_prepared_success(token)
        self.assertEqual(returned.status, JobStatus.SUCCEEDED)
        self.assertEqual(returned.status_history[-3:],
                         (JobStatus.ALLOCATED, JobStatus.RUNNING, JobStatus.SUCCEEDED))
        self.assertEqual(returned.result, {"samples": [1], "summary": {"count": 1}})
        returned.result["samples"].append(5)
        self.assertEqual(scheduler.get(job_id).result["samples"], [1])
        self.assertEqual(scheduler.queued_job_ids(), (second.job_id,))
        with self.assertRaises(ValueError):
            scheduler.commit_prepared_success(token)

    def test_requires_running_and_rejects_foreign_copied_and_stale_tokens(self):
        scheduler = FifoJobScheduler()
        first = _submitted(scheduler)
        with self.assertRaises(ValueError):
            scheduler.prepare_success(first.job_id, {})
        scheduler.allocate_next()
        with self.assertRaises(ValueError):
            scheduler.prepare_success(first.job_id, {})
        scheduler.mark_running(first.job_id)
        token = scheduler.prepare_success(first.job_id, {})
        for forged in (copy(token), deepcopy(token), replace(token), object()):
            with self.subTest(forged=type(forged).__name__):
                with self.assertRaises(ValueError):
                    scheduler.validate_prepared(forged)
                with self.assertRaises(ValueError):
                    scheduler.commit_prepared_success(forged)
        with self.assertRaises(ValueError):
            FifoJobScheduler().validate_prepared(token)
        scheduler.mark_internal_error(first.job_id, "EXPLICIT_RECOVERY")
        before = scheduler.get(first.job_id)
        with self.assertRaises(ValueError):
            scheduler.validate_prepared(token)
        with self.assertRaises(ValueError):
            scheduler.commit_prepared_success(token)
        self.assertEqual(scheduler.get(first.job_id), before)

    def test_exact_command_record_and_transition_counter_are_checked(self):
        scheduler = FifoJobScheduler()
        job_id = _running(scheduler)
        token = scheduler.prepare_success(job_id, {})
        job = scheduler._jobs[job_id]
        original_command = job.command
        job.command = replace(original_command)
        with self.assertRaises(ValueError):
            scheduler.validate_prepared(token)
        job.command = original_command
        scheduler._jobs[job_id] = replace(job)
        with self.assertRaises(ValueError):
            scheduler.validate_prepared(token)
        scheduler._jobs[job_id] = job
        job.transition_version += 1
        with self.assertRaises(ValueError):
            scheduler.validate_prepared(token)

    def test_result_copy_failure_leaves_running_job_untouched(self):
        scheduler = FifoJobScheduler()
        job_id = _running(scheduler)
        before = scheduler.get(job_id)
        with patch("core.attack_simulation.jobs.deepcopy", side_effect=ValueError("copy failed")):
            with self.assertRaisesRegex(ValueError, "copy failed"):
                scheduler.prepare_success(job_id, {"samples": [1]})
        self.assertEqual(scheduler.get(job_id), before)

    def test_result_copy_does_not_block_readers_and_rechecks_job_after_copy(self):
        scheduler = FifoJobScheduler()
        scheduler.bind_transaction_lock(RLock())
        job_id = _running(scheduler)
        entered, release, read_done = Event(), Event(), Event()
        errors = []

        def held_copy(value):
            entered.set()
            if not release.wait(2):
                raise RuntimeError("copy release timed out")
            return deepcopy(value)

        def prepare():
            try:
                scheduler.prepare_success(job_id, {"samples": [1]})
            except Exception as error:
                errors.append(error)

        def read_and_recover():
            scheduler.get(job_id)
            scheduler.mark_internal_error(job_id, "EXPLICIT_RECOVERY")
            read_done.set()

        with patch("core.attack_simulation.jobs.deepcopy", held_copy):
            writer = Thread(target=prepare)
            writer.start()
            reader = Thread(target=read_and_recover)
            try:
                self.assertTrue(entered.wait(2))
                reader.start()
                self.assertTrue(read_done.wait(1), "reader blocked behind result copying")
            finally:
                release.set()
                writer.join(2)
                if reader.ident is not None:
                    reader.join(2)
        self.assertFalse(writer.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertEqual(scheduler.get(job_id).status, JobStatus.INTERNAL_ERROR)
        self.assertIsNone(scheduler.get(job_id).result)


class CoordinatedPublicationTests(unittest.TestCase):
    def test_reader_cannot_observe_half_published_state_and_job(self):
        shared = RLock()
        store, scheduler = _store(), FifoJobScheduler()
        store.bind_transaction_lock(shared)
        scheduler.bind_transaction_lock(shared)
        job_id = _running(scheduler)
        expected, final, events = _batch(store, job_id)
        batch = store.prepare_batch(expected, final, events, 2)
        success = scheduler.prepare_success(job_id, {"revision": final.revision})
        started, done = Event(), Event()
        observed = []

        def read():
            started.set()
            with shared:
                observed.append((store.read(), scheduler.get(job_id)))
            done.set()

        reader = Thread(target=read)
        try:
            with shared:
                store.validate_prepared(batch)
                scheduler.validate_prepared(success)
                store.commit_prepared(batch)
                reader.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(done.wait(0.05))
                scheduler.commit_prepared_success(success)
            self.assertTrue(done.wait(1))
        finally:
            if reader.ident is not None:
                reader.join(2)
        self.assertEqual(observed[0][0], final)
        self.assertEqual(observed[0][1].status, JobStatus.SUCCEEDED)

    def test_prevalidate_all_participants_before_any_mutation(self):
        shared = RLock()
        store, scheduler = _store(), FifoJobScheduler()
        store.bind_transaction_lock(shared)
        scheduler.bind_transaction_lock(shared)
        job_id = _running(scheduler)
        expected, final, events = _batch(store, job_id)
        batch = store.prepare_batch(expected, final, events, 2)
        success = scheduler.prepare_success(job_id, {})
        scheduler.mark_internal_error(job_id, "EXPLICIT_RECOVERY")
        with shared:
            store.validate_prepared(batch)
            with self.assertRaises(ValueError):
                scheduler.validate_prepared(success)
        self.assertIs(store.read(), expected)
        self.assertEqual(store.events(), ())


class DeclaredStartRollbackTests(unittest.TestCase):
    def test_owned_queued_head_noop_and_successful_start_can_retry(self):
        scheduler = FifoJobScheduler()
        shared = RLock()
        scheduler.bind_transaction_lock(shared)
        first = _submitted(scheduler)
        second = _submitted(scheduler, "second")
        before = scheduler.get(first.job_id)
        before_queue = scheduler.queued_job_ids()
        before_version = scheduler._jobs[first.job_id].transition_version
        self.assertFalse(scheduler._rollback_declared_start(first.job_id))
        for foreign in (second.job_id, "foreign", object()):
            with self.assertRaises(ValueError):
                scheduler._rollback_declared_start(foreign)
        with self.assertRaises(ValueError):
            FifoJobScheduler()._rollback_declared_start(first.job_id)
        with self.assertRaises(ValueError):
            scheduler._start_declared(second.job_id)
        with shared:
            started = scheduler._start_declared(first.job_id)
            self.assertEqual(started.status, JobStatus.RUNNING)
            self.assertEqual(scheduler.queued_job_ids(), (second.job_id,))
            self.assertTrue(scheduler._rollback_declared_start(first.job_id))
        self.assertEqual(scheduler.get(first.job_id), before)
        self.assertEqual(scheduler.queued_job_ids(), before_queue)
        self.assertEqual(scheduler._jobs[first.job_id].transition_version, before_version)
        self.assertFalse(scheduler._rollback_declared_start(first.job_id))
        self.assertEqual(scheduler._start_declared(first.job_id).status, JobStatus.RUNNING)

    def test_allocate_and_running_faults_before_and_after_mutation_compensate(self):
        class InjectedStartFailure(BaseException):
            pass

        for method, after in (("allocate_next", False), ("allocate_next", True),
                              ("mark_running", False), ("mark_running", True)):
            for queue_size in (1, 3):
                with self.subTest(method=method, after=after, queue_size=queue_size):
                    scheduler = FifoJobScheduler()
                    shared = RLock()
                    scheduler.bind_transaction_lock(shared)
                    first = _submitted(scheduler)
                    for index in range(queue_size - 1):
                        _submitted(scheduler, f"tail-{index}")
                    before = scheduler.get(first.job_id)
                    before_queue = scheduler.queued_job_ids()
                    before_version = scheduler._jobs[first.job_id].transition_version
                    normal = getattr(scheduler, method)

                    def fail(*args):
                        if after:
                            normal(*args)
                        raise InjectedStartFailure("START fault")

                    with patch.object(scheduler, method, side_effect=fail), \
                         patch("core.attack_simulation.jobs.deepcopy",
                               side_effect=AssertionError("unexpected START result copy")):
                        with shared:
                            with self.assertRaises(InjectedStartFailure):
                                try:
                                    scheduler._start_declared(first.job_id)
                                except BaseException:
                                    self.assertEqual(scheduler._rollback_declared_start(first.job_id),
                                                     method == "mark_running" or after)
                                    raise
                    self.assertEqual(scheduler.get(first.job_id), before)
                    self.assertEqual(scheduler.queued_job_ids(), before_queue)
                    self.assertEqual(scheduler._jobs[first.job_id].transition_version, before_version)
                    self.assertEqual(scheduler._start_declared(first.job_id).status, JobStatus.RUNNING)

    def test_start_failure_and_idle_batch_rollback_leave_both_owners_unchanged(self):
        shared = RLock()
        store, scheduler = _store(), FifoJobScheduler()
        store.bind_transaction_lock(shared)
        scheduler.bind_transaction_lock(shared)
        first = _submitted(scheduler)
        expected, final, events = _batch(store, first.job_id)
        batch = store.prepare_batch(expected, final, events, 2)
        normal = scheduler.mark_running

        def fail(job_id):
            normal(job_id)
            raise RuntimeError("after RUNNING")

        with patch.object(scheduler, "mark_running", side_effect=fail):
            with shared:
                with self.assertRaisesRegex(RuntimeError, "after RUNNING"):
                    try:
                        store.commit_prepared(batch)
                        scheduler._start_declared(first.job_id)
                    except BaseException:
                        self.assertTrue(scheduler._rollback_declared_start(first.job_id))
                        self.assertTrue(store._rollback_prepared_commit(batch))
                        raise
        self.assertIs(store.read(), expected)
        self.assertEqual(store.events(), ())
        self.assertEqual(scheduler.get(first.job_id), first)
        self.assertEqual(scheduler.queued_job_ids(), (first.job_id,))
        store.validate_prepared(batch)

    def test_parent_start_then_idle_order_compensates_every_failure_boundary(self):
        for boundary in ("before-journal", "after-running", "before-idle", "after-idle"):
            with self.subTest(boundary=boundary):
                shared = RLock()
                store, scheduler = _store(), FifoJobScheduler()
                store.bind_transaction_lock(shared)
                scheduler.bind_transaction_lock(shared)
                first = _submitted(scheduler)
                expected, final, events = _batch(store, first.job_id)
                batch = store.prepare_batch(expected, final, events, 2)
                if boundary == "before-journal":
                    target, method = scheduler, "_start_declared"
                elif boundary == "after-running":
                    target, method = scheduler, "mark_running"
                else:
                    target, method = store, "commit_prepared"
                normal = getattr(target, method)

                def fail(argument):
                    if boundary in ("after-running", "after-idle"):
                        normal(argument)
                    raise RuntimeError("START publication fault")

                with patch.object(target, method, side_effect=fail):
                    with shared:
                        with self.assertRaisesRegex(RuntimeError, "START publication fault"):
                            try:
                                scheduler._start_declared(first.job_id)
                                store.commit_prepared(batch)
                            except BaseException:
                                self.assertEqual(scheduler._rollback_declared_start(first.job_id),
                                                 boundary != "before-journal")
                                self.assertEqual(store._rollback_prepared_commit(batch),
                                                 boundary == "after-idle")
                                raise
                self.assertIs(store.read(), expected)
                self.assertEqual(store.events(), ())
                self.assertEqual(scheduler.get(first.job_id), first)
                self.assertEqual(scheduler.queued_job_ids(), (first.job_id,))
                store.validate_prepared(batch)

    def test_intervening_scheduler_mutation_or_new_start_retires_checkpoint(self):
        for mutation in ("submit", "cancel", "allocate", "legacy-success", "prepared-success", "next-start"):
            with self.subTest(mutation=mutation):
                scheduler = FifoJobScheduler()
                first = _submitted(scheduler)
                second = _submitted(scheduler, "second")
                scheduler._start_declared(first.job_id)
                if mutation == "submit":
                    _submitted(scheduler, "third")
                elif mutation == "cancel":
                    scheduler.cancel(second.job_id)
                elif mutation == "allocate":
                    scheduler.allocate_next()
                elif mutation == "legacy-success":
                    scheduler.mark_succeeded(first.job_id, {})
                elif mutation == "prepared-success":
                    scheduler.commit_prepared_success(scheduler.prepare_success(first.job_id, {}))
                else:
                    scheduler._start_declared(second.job_id)
                before = scheduler.get(first.job_id), scheduler.queued_job_ids()
                with self.assertRaises(ValueError):
                    scheduler._rollback_declared_start(first.job_id)
                self.assertEqual((scheduler.get(first.job_id), scheduler.queued_job_ids()), before)

    def test_exact_job_queue_and_status_generation_are_checked(self):
        for mutation in ("record", "command", "queue-head", "queue-version", "version", "history", "result", "error"):
            with self.subTest(mutation=mutation):
                scheduler = FifoJobScheduler()
                first = _submitted(scheduler)
                _submitted(scheduler, "second")
                _submitted(scheduler, "third")
                scheduler._start_declared(first.job_id)
                job = scheduler._jobs[first.job_id]
                if mutation == "record":
                    scheduler._jobs[first.job_id] = replace(job)
                elif mutation == "command":
                    job.command = replace(job.command)
                elif mutation == "queue-head":
                    scheduler._queue.rotate(1)
                elif mutation == "queue-version":
                    scheduler._queue_version += 1
                elif mutation == "version":
                    job.transition_version += 1
                elif mutation == "history":
                    job.status_history[-1] = JobStatus.ALLOCATED
                elif mutation == "result":
                    job.result = {}
                else:
                    job.error_code = "changed"
                before = scheduler.get(first.job_id), scheduler.queued_job_ids()
                with self.assertRaises(ValueError):
                    scheduler._rollback_declared_start(first.job_id)
                self.assertEqual((scheduler.get(first.job_id), scheduler.queued_job_ids()), before)


class PreparedRollbackTests(unittest.TestCase):
    def test_fault_before_or_after_normal_owner_mutation_can_retry(self):
        class InjectedFailure(BaseException):
            pass

        for owner, after in (("store", False), ("store", True),
                             ("scheduler", False), ("scheduler", True)):
            with self.subTest(owner=owner, after=after):
                shared = RLock()
                store, scheduler = _store(), FifoJobScheduler()
                store.bind_transaction_lock(shared)
                scheduler.bind_transaction_lock(shared)
                job_id = _running(scheduler)
                # Existing history must survive suffix-only compensation.
                store.commit(0, QpuStateTransition(
                    "prefix", "program_shot", "previous-job",
                    store.read().density_matrix, 1.0, None,
                ))
                prestate_result = {"before": [42]}
                scheduler._jobs[job_id].result = prestate_result
                scheduler._jobs[job_id].error_code = "prior-supervisor-note"
                before_job = scheduler.get(job_id)
                before_events = store.events()
                expected, final, events = _batch(store, job_id)
                batch = store.prepare_batch(expected, final, events, 2)
                success = scheduler.prepare_success(job_id, {"samples": [1]})
                target = store if owner == "store" else scheduler
                method = "commit_prepared" if owner == "store" else "commit_prepared_success"
                normal_commit = getattr(target, method)
                returned = []

                def fail(token):
                    if after:
                        returned.append(normal_commit(token))
                    raise InjectedFailure("owner publication fault")

                with patch.object(target, method, side_effect=fail), \
                     patch("core.attack_simulation.qpu_state._validate_density_matrix",
                           side_effect=AssertionError("numerical validation under gate")), \
                     patch("core.attack_simulation.jobs.deepcopy",
                           side_effect=AssertionError("copy under gate")):
                    with shared:
                        store.validate_prepared(batch)
                        scheduler.validate_prepared(success)
                        with self.assertRaises(InjectedFailure):
                            try:
                                store.commit_prepared(batch)
                                scheduler.commit_prepared_success(success)
                            except BaseException:
                                self.assertEqual(scheduler._rollback_prepared_success(success),
                                                 owner == "scheduler" and after)
                                self.assertEqual(store._rollback_prepared_commit(batch),
                                                 owner == "scheduler" or after)
                                raise
                        store.validate_prepared(batch)
                        scheduler.validate_prepared(success)
                self.assertIs(store.read(), expected)
                self.assertEqual(store.events(), before_events)
                self.assertEqual(store._event_ids, {"prefix"})
                self.assertEqual(scheduler.get(job_id), before_job)
                self.assertIs(scheduler._jobs[job_id].result, prestate_result)
                # Rollback is consumed; exact pending tokens now return False.
                self.assertFalse(store._rollback_prepared_commit(batch))
                self.assertFalse(scheduler._rollback_prepared_success(success))
                if owner == "scheduler" and after:
                    returned[0].result["samples"].append(99)
                with shared:
                    store.commit_prepared(batch)
                    scheduler.commit_prepared_success(success)
                self.assertEqual(store.read(), final)
                self.assertEqual(store.events(), before_events + events)
                self.assertEqual(scheduler.get(job_id).result, {"samples": [1]})

    def test_rollback_rejects_forged_foreign_and_never_issued_tokens(self):
        store, scheduler = _store(), FifoJobScheduler()
        job_id = _running(scheduler)
        expected, final, events = _batch(store, job_id)
        batch = store.prepare_batch(expected, final, events, 2)
        success = scheduler.prepare_success(job_id, {})
        for committed in (False, True):
            if committed:
                store.commit_prepared(batch)
                scheduler.commit_prepared_success(success)
            before = store.read(), store.events(), scheduler.get(job_id)
            for token in (copy(batch), deepcopy(batch), replace(batch), object()):
                with self.assertRaises(ValueError):
                    store._rollback_prepared_commit(token)
            for token in (copy(success), deepcopy(success), replace(success), object()):
                with self.assertRaises(ValueError):
                    scheduler._rollback_prepared_success(token)
            with self.assertRaises(ValueError):
                _store()._rollback_prepared_commit(batch)
            with self.assertRaises(ValueError):
                FifoJobScheduler()._rollback_prepared_success(success)
            self.assertEqual((store.read(), store.events(), scheduler.get(job_id)), before)

    def test_qpu_rollback_rejects_intervening_legacy_or_prepared_commit(self):
        for intervening in ("legacy", "prepared", "pending-stale"):
            with self.subTest(intervening=intervening):
                store = _store()
                expected, final, events = _batch(store)
                batch = store.prepare_batch(expected, final, events, 2)
                if intervening != "pending-stale":
                    store.commit_prepared(batch)
                if intervening == "prepared":
                    current = store.read()
                    later = store.prepare_batch(current, current, (), 1)
                    store.commit_prepared(later)
                else:
                    current = store.read()
                    store.commit(current.revision, QpuStateTransition(
                        "intervening", "program_shot", "other-job", current.density_matrix,
                        0.0, current.owner_tenant_id,
                    ))
                before = store.read(), store.events()
                with self.assertRaises((ValueError, RevisionConflictError)):
                    store._rollback_prepared_commit(batch)
                self.assertEqual((store.read(), store.events()), before)

    def test_scheduler_rollback_rejects_intervening_mutation_and_stale_pending(self):
        for intervening in ("submit", "legacy", "prepared", "pending-stale"):
            with self.subTest(intervening=intervening):
                scheduler = FifoJobScheduler()
                job_id = _running(scheduler)
                token = scheduler.prepare_success(job_id, {"samples": [1]})
                second = _submitted(scheduler, "second")
                if intervening != "pending-stale":
                    scheduler.commit_prepared_success(token)
                if intervening == "submit":
                    _submitted(scheduler, "third")
                elif intervening == "pending-stale":
                    scheduler.mark_internal_error(job_id, "EXPLICIT_RECOVERY")
                else:
                    scheduler.allocate_next()
                    scheduler.mark_running(second.job_id)
                    if intervening == "legacy":
                        scheduler.mark_succeeded(second.job_id, {})
                    else:
                        later = scheduler.prepare_success(second.job_id, {})
                        scheduler.commit_prepared_success(later)
                before = scheduler.get(job_id), scheduler.queued_job_ids()
                with self.assertRaises(ValueError):
                    scheduler._rollback_prepared_success(token)
                self.assertEqual((scheduler.get(job_id), scheduler.queued_job_ids()), before)

    def test_qpu_exact_snapshot_and_suffix_and_job_generation_checked(self):
        for mutation in ("snapshot", "suffix"):
            with self.subTest(mutation=mutation):
                store = _store()
                expected, final, events = _batch(store)
                token = store.prepare_batch(expected, final, events, 2)
                store.commit_prepared(token)
                if mutation == "snapshot":
                    store._snapshot = replace(store.read())
                else:
                    store._events[-1] = replace(store._events[-1])
                before = store.read(), store.events()
                with self.assertRaises(RevisionConflictError):
                    store._rollback_prepared_commit(token)
                self.assertEqual((store.read(), store.events()), before)
        for mutation in ("record", "command", "status", "version", "history", "result", "error"):
            with self.subTest(mutation=mutation):
                scheduler = FifoJobScheduler()
                job_id = _running(scheduler)
                token = scheduler.prepare_success(job_id, {})
                scheduler.commit_prepared_success(token)
                job = scheduler._jobs[job_id]
                if mutation == "record":
                    scheduler._jobs[job_id] = replace(job)
                elif mutation == "command":
                    job.command = replace(job.command)
                elif mutation == "status":
                    job.status = JobStatus.RUNNING
                elif mutation == "version":
                    job.transition_version += 1
                elif mutation == "history":
                    job.status_history.append(JobStatus.SUCCEEDED)
                elif mutation == "result":
                    job.result = {}
                else:
                    job.error_code = "unexpected"
                before = scheduler.get(job_id)
                with self.assertRaises(ValueError):
                    scheduler._rollback_prepared_success(token)
                self.assertEqual(scheduler.get(job_id), before)


if __name__ == "__main__":
    unittest.main()

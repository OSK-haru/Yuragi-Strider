"""Host barriers in the on-demand path, without a live/world clock mapping."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from threading import Event
import unittest
from unittest.mock import patch

from core.attack_simulation import job_service
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.request_receipts import ReceiptOutcome
from tests.test_attack_sessions import circuit_json, session_fixture


TIMEOUT = 5


@contextmanager
def worker_pool():
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        yield pool
    finally:
        # Futures are drained with explicit timeouts below. Avoid the executor
        # context manager's unbounded join if a regression blocks a worker.
        pool.shutdown(wait=False, cancel_futures=True)


class HostDelayInvarianceTests(unittest.TestCase):
    def fixture(self, timestamp):
        fixture = session_fixture()
        session = fixture[5]
        session._service._sampling_salt = bytes(32)  # Matching private test entropy.
        session._receipts._clock = lambda: timestamp  # Host coordinates only.
        return fixture

    def request(self, key):
        return ProgramSubmitRequest(
            CircuitProgram.from_editor_json(circuit_json()), "qpu-1", 4, key,
        )

    def submit(self, fixture, actor, key, **kwargs):
        return fixture[5].request(
            fixture[4][actor], lambda client, workspace: client.submit_program(self.request(key)),
            operation_id="program.submit", **kwargs,
        )

    def result(self, fixture, actor, job):
        view = fixture[5].request(
            fixture[4][actor], lambda client, workspace: client.get_result(job.job_id),
            operation_id="job.result.read",
        )
        # Exact allowlists cover nested public data, not just an absent attribute.
        self.assertEqual(set(asdict(view)), {"job_id", "status", "result", "error_code"})
        self.assertEqual(set(view.result), {"shots", "counts"})
        self.assertEqual(sum(view.result["counts"].values()), 4)
        self.assertIs(view.status, JobStatus.SUCCEEDED)
        self.assertIsNone(view.error_code)
        return view

    def assert_equivalent(self, baseline, delayed, expected, actual, actors):
        self.assertEqual(baseline[0].establish(), delayed[0].establish())
        self.assertEqual(baseline[2].config, delayed[2].config)
        for left, right, actor in zip(expected, actual, actors):
            self.assertEqual(set(asdict(right)), {"job_id", "status", "error_code"})
            self.assertEqual(self.result(baseline, actor, left).result,
                             self.result(delayed, actor, right).result)
            left_job = baseline[2].scheduler.get(left.job_id)
            right_job = delayed[2].scheduler.get(right.job_id)
            self.assertEqual(left_job.acceptance_sequence, right_job.acceptance_sequence)
            self.assertEqual(left_job.status_history, right_job.status_history)
            self.assertIs(right_job.status, JobStatus.SUCCEEDED)
            self.assertEqual(left_job.command.operation.sampling_seed,
                             right_job.command.operation.sampling_seed)

        def trace(fixture, jobs):
            # Random IDs identify ownership; compare their positions in the input
            # sequence rather than discarding which job produced each event.
            positions = {job.job_id: index for index, job in enumerate(jobs)}
            events = fixture[1].events()
            self.assertEqual(len({event.event_id for event in events}), len(events))
            return [(event.event_kind, positions[event.job_id], event.revision_before,
                     event.revision_after, event.device_time_start_us, event.device_time_end_us,
                     event.owner_before, event.owner_after, event.physical_qubit_ids_before,
                     event.physical_qubit_ids_after) for event in events]

        self.assertEqual(trace(baseline, expected), trace(delayed, actual))
        left, right = baseline[1].read(), delayed[1].read()
        self.assertEqual(left.density_matrix, right.density_matrix)
        self.assertEqual(left.device_time_us, right.device_time_us)
        self.assertEqual(left.revision, right.revision)
        self.assertEqual(left.owner_tenant_id, right.owner_tenant_id)
        self.assertGreater(right.device_time_us, 0)
        self.assertEqual(delayed[2].scheduler.queued_job_ids(), ())

    def test_compile_wait_retains_two_actor_ingress_and_physical_trace(self):
        baseline, delayed = self.fixture(42), self.fixture(9_000_000_000)
        expected = [self.submit(baseline, actor, key)
                    for actor, key in (("a", "first"), ("v", "second"))]
        for _ in expected:
            baseline[5].run_next()
        entered, release = Event(), Event()
        session = delayed[5]
        compiler = session._service._program_compiler
        compile_program = compiler.compile

        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(TIMEOUT):
                raise RuntimeError("compile barrier timed out")
            return compile_program(*args, **kwargs)

        async def scenario():
            first = asyncio.create_task(session.request_async(
                delayed[4]["a"], lambda c, w: c.submit_program(self.request("first")),
                operation_id="program.submit"))
            second = None
            attempted = asyncio.Event()
            dispatch_ready = session.dispatch_ready

            def dispatch(credential, receipt):
                ready = dispatch_ready(credential, receipt)
                if receipt.sequence == 2:
                    attempted.set()
                return ready

            try:
                self.assertTrue(await asyncio.wait_for(
                    asyncio.to_thread(entered.wait, TIMEOUT), TIMEOUT + 1))
                receipt = session.receive(delayed[4]["v"], "program.submit")
                with patch.object(session, "dispatch_ready", side_effect=dispatch):
                    second = asyncio.create_task(session.request_async(
                        delayed[4]["v"], lambda c, w: c.submit_program(self.request("second")),
                        receipt=receipt))
                    await asyncio.wait_for(attempted.wait(), TIMEOUT)
                    self.assertFalse(dispatch_ready(delayed[4]["v"], receipt).done())
                    self.assertEqual([r.outcome for r in session.receipt_records()], [None, None])
                    self.assertEqual(delayed[2].scheduler.queued_job_ids(), ())
                    self.assertEqual(delayed[1].read().revision, 0)
                    self.assertEqual(delayed[1].read().device_time_us, 0)
                    self.assertEqual(delayed[1].events(), ())
            finally:
                release.set()
                tasks = [task for task in (first, second) if task is not None]
                actual = await asyncio.wait_for(asyncio.gather(*tasks), TIMEOUT)
            return actual

        with patch.object(type(compiler), "compile", side_effect=paused):
            actual = asyncio.run(scenario())
        jobs = [delayed[2].scheduler.get(view.job_id) for view in actual]
        self.assertEqual([job.ingress_receipt.sequence for job in jobs], [1, 2])
        self.assertEqual([job.acceptance_sequence for job in jobs], [1, 2])
        self.assertEqual([job.ingress_receipt.principal_id for job in jobs], ["a", "v"])
        self.assertEqual([job.ingress_receipt.received_at_ns for job in jobs], [9_000_000_000] * 2)
        self.assertEqual([job.ingress_receipt.clock_domain for job in jobs], ["host_monotonic_ns"] * 2)
        self.assertEqual(delayed[2].scheduler.queued_job_ids(), tuple(view.job_id for view in actual))
        for _ in actual:
            delayed[5].run_next()
        self.assert_equivalent(baseline, delayed, expected, actual, ("a", "v"))
        self.assertEqual([event.event_kind for event in delayed[1].events()],
                         ["program_shot"] * 4 + ["service_reset"] + ["program_shot"] * 4)

    def test_result_projection_wait_allows_controls_admission_execution_and_close(self):
        baseline, delayed = self.fixture(42), self.fixture(9_000_000_000)
        expected_first, actual_first = [self.submit(f, "a", "first") for f in (baseline, delayed)]
        for fixture in (baseline, delayed):
            fixture[5].run_next()
        expected_cancel, actual_cancel = [self.submit(f, "a", "cancel") for f in (baseline, delayed)]

        def controls(fixture, first, cancellable):
            session, credential = fixture[5], fixture[4]["a"]
            devices, device, status = session.request(credential,
                lambda c, w: (c.list_devices(), c.get_device("qpu-1"), c.get_status(first.job_id)),
                operation_id="device.list")
            self.assertEqual(devices, (device,))
            self.assertIs(status.status, JobStatus.SUCCEEDED)
            cancelled = session.request(credential, lambda c, w: c.cancel(cancellable.job_id),
                                        operation_id="job.cancel")
            self.assertIs(cancelled.status, JobStatus.CANCELLED)
            contents = session.request(credential,
                lambda c, w: (w.put_circuit("projection.json", circuit_json()),
                              w.read_circuit("projection.json"))[1],
                operation_id="workspace.circuit.put")
            self.assertEqual(contents, CircuitProgram.from_editor_json(circuit_json()))
            return devices, device, status.status, cancelled.status, contents

        expected_view = self.result(baseline, "a", expected_first)
        expected_controls = controls(baseline, expected_first, expected_cancel)
        expected_second = self.submit(baseline, "v", "second")
        baseline[5].run_next()
        entered, release = Event(), Event()
        project = job_service._project_result
        snapshot, events = delayed[1].read(), delayed[1].events()

        def paused(job):
            entered.set()
            # The control futures time out before this barrier can expire and
            # accidentally make a lock-holding implementation appear usable.
            if not release.wait(4 * TIMEOUT):
                raise RuntimeError("result projection barrier timed out")
            return project(job)

        with patch.object(job_service, "_project_result", side_effect=paused), \
                worker_pool() as pool:
            waiting = pool.submit(self.result, delayed, "a", actual_first)
            try:
                self.assertTrue(entered.wait(TIMEOUT))
                actual_controls = pool.submit(controls, delayed, actual_first, actual_cancel).result(TIMEOUT)
                self.assertEqual(actual_controls, expected_controls)
                self.assertEqual(delayed[1].read(), snapshot)
                self.assertEqual(delayed[1].events(), events)
                receipt = pool.submit(delayed[5].receive, delayed[4]["v"], "program.submit").result(TIMEOUT)
                self.assertIsNone(delayed[5].receipt_records()[-1].outcome)
                actual_second = pool.submit(self.submit, delayed, "v", "second", receipt=receipt).result(TIMEOUT)
                self.assertIs(delayed[2].scheduler.get(actual_second.job_id).ingress_receipt, receipt)
                completed = pool.submit(delayed[5].run_next).result(TIMEOUT)
                self.assertEqual(completed.job_id, actual_second.job_id)
                self.assertIs(completed.status, JobStatus.SUCCEEDED)
                # Physical work has completed while the first result remains
                # unavailable; the read barrier is not a QPU/control barrier.
                self.assertFalse(waiting.done())
                self.assertFalse(release.is_set())
                post_execution, post_events = delayed[1].read(), delayed[1].events()
            finally:
                release.set()
            actual_view = waiting.result(TIMEOUT)
        self.assertEqual(actual_view.result, expected_view.result)
        self.assertEqual(delayed[1].read(), post_execution)
        self.assertEqual(delayed[1].events(), post_events)
        self.assertEqual(delayed[2].scheduler.get(actual_cancel.job_id).status_history,
                         baseline[2].scheduler.get(expected_cancel.job_id).status_history)
        self.assert_equivalent(baseline, delayed, [expected_first, expected_second],
                               [actual_first, actual_second], ("a", "v"))

        # Close must revoke promptly, drain the in-flight read, and reject its
        # stale result without undoing the already committed physical events.
        entered, release, revoked = Event(), Event(), Event()
        close_authority = delayed[3].close

        def revoke():
            close_authority()
            revoked.set()

        with patch.object(job_service, "_project_result", side_effect=paused), \
                patch.object(delayed[3], "close", side_effect=revoke), worker_pool() as pool:
            waiting = pool.submit(self.result, delayed, "a", actual_first)
            closing = None
            try:
                self.assertTrue(entered.wait(TIMEOUT))
                closing = pool.submit(delayed[5].close)
                self.assertTrue(revoked.wait(TIMEOUT))
                self.assertFalse(closing.done())
                self.assertFalse(waiting.done())
                with self.assertRaises(PermissionError):
                    delayed[5].receive(delayed[4]["a"], "device.list")
                self.assertEqual(delayed[1].read(), post_execution)
                self.assertEqual(delayed[1].events(), post_events)
            finally:
                release.set()
                with self.assertRaises(PermissionError):
                    waiting.result(TIMEOUT)
                if closing is not None:
                    closing.result(TIMEOUT)
        self.assertEqual(delayed[1].read(), post_execution)
        self.assertEqual(delayed[1].events(), post_events)

    def test_cancelled_async_compile_wait_and_inflight_retry_execute_only_once(self):
        baseline, delayed = self.fixture(42), self.fixture(9_000_000_000)
        expected = self.submit(baseline, "a", "retry")
        baseline[5].run_next()
        entered, release = Event(), Event()
        session = delayed[5]
        compiler = session._service._program_compiler
        compile_program = compiler.compile
        workers = []

        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(TIMEOUT):
                raise RuntimeError("cancel/retry compile barrier timed out")
            return compile_program(*args, **kwargs)

        with worker_pool() as pool:
            async def worker(*args, **kwargs):
                future = pool.submit(*args, **kwargs)
                workers.append(future)  # Drain the actual worker, not just its compiler.
                return await asyncio.wrap_future(future)

            async def scenario():
                attempted = asyncio.Event()
                dispatch_ready = session.dispatch_ready

                def dispatch(credential, receipt):
                    ready = dispatch_ready(credential, receipt)
                    if receipt.sequence == 2:
                        attempted.set()
                    return ready

                async def submit():
                    return await session.request_async(delayed[4]["a"],
                        lambda c, w: c.submit_program(self.request("retry")),
                        operation_id="program.submit", worker=worker)

                first = asyncio.create_task(submit())
                retry = None
                try:
                    self.assertTrue(await asyncio.wait_for(
                        asyncio.to_thread(entered.wait, TIMEOUT), TIMEOUT + 1))
                    first.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(first, TIMEOUT)
                    self.assertIsNone(session.receipt_records()[0].outcome)
                    with patch.object(session, "dispatch_ready", side_effect=dispatch):
                        retry = asyncio.create_task(submit())
                        await asyncio.wait_for(attempted.wait(), TIMEOUT)
                        records = session.receipt_records()
                        self.assertEqual([r.outcome for r in records], [None, None])
                        self.assertFalse(dispatch_ready(delayed[4]["a"], records[1].receipt).done())
                        self.assertEqual(len(workers), 1)
                        self.assertEqual(delayed[1].events(), ())
                finally:
                    release.set()
                    if not first.done():
                        await asyncio.wait_for(first, TIMEOUT)
                    if retry is not None:
                        replay = await asyncio.wait_for(retry, TIMEOUT)
                return replay

            with patch.object(type(compiler), "compile", side_effect=paused):
                replay = asyncio.run(scenario())
            originals = [future.result(TIMEOUT) for future in workers]
        self.assertEqual([view.job_id for view in originals], [replay.job_id] * 2)
        records = session.receipt_records()
        self.assertEqual([r.outcome for r in records], [ReceiptOutcome.COMPLETED] * 2)
        self.assertEqual([r.job_id for r in records], [replay.job_id] * 2)
        self.assertIs(delayed[2].scheduler.get(replay.job_id).ingress_receipt, records[0].receipt)
        self.assertEqual(delayed[2].scheduler.queued_job_ids(), (replay.job_id,))
        self.assertEqual(delayed[1].read().revision, 0)
        session.run_next()
        snapshot, events = delayed[1].read(), delayed[1].events()
        self.assertIsNone(session.run_next())
        self.assertEqual(delayed[1].read(), snapshot)
        self.assertEqual(delayed[1].events(), events)
        self.assertEqual([event.event_kind for event in events], ["program_shot"] * 4)
        self.assert_equivalent(baseline, delayed, [expected], [replay], ("a",))


if __name__ == "__main__":
    unittest.main()

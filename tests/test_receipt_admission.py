"""Ingress order without filling a host worker pool with admission waiters."""

import asyncio
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from fastapi import FastAPI

from api.attack_http import create_attack_router, run_in_threadpool
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.request_receipts import ReceiptOutcome
from tests.test_attack_http_adapter import _request
from tests.test_attack_sessions import circuit_json, session_fixture


class ReceiptAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.environment, self.store, self.runtime, self.authority, self.tokens, self.session, self.registry = session_fixture()
        self.app = FastAPI()
        self.app.include_router(create_attack_router(self.registry))
        self.base = "/api/attack/sessions/world"
        self.payload = {"device_id": "qpu-1", "shots": 1, "circuit_config": json.loads(circuit_json())}

    async def submit(self, key, *, actor="a", payload=None):
        return await _request(self.app, "POST", self.base + "/programs", credential=self.tokens[actor],
                              payload=self.payload if payload is None else payload,
                              headers=[(b"idempotency-key", key.encode())])

    def test_host_receipt_is_reserved_before_control_lock_wait(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.session._lock:
                waiting = pool.submit(self.session.receive, self.tokens["a"], "program.submit")
                receipt = waiting.result(timeout=2)
                self.assertEqual(self.session.receipt_records()[0].receipt, receipt)
                self.assertTrue(self.session.dispatch_ready(self.tokens["a"], receipt).done())
        self.session.abandon_receipt(receipt)
        self.assertEqual(self.runtime.scheduler.queued_job_ids(), ())

    def test_receipt_fifo_survives_pre_worker_delay_with_one_worker_slot(self):
        self.session._receipts._clock = lambda: 42  # Equal host arrivals, private sequence breaks ties.
        baseline = session_fixture()
        baseline[5]._receipts._clock = lambda: 42
        for session in (self.session, baseline[5]):
            session._service._sampling_salt = bytes(32)  # Same private physics entropy.
        request = CircuitProgram.from_editor_json(circuit_json())
        expected = [baseline[5].request(baseline[4][principal],
                    lambda c, w, key=key: c.submit_program(ProgramSubmitRequest(request, "qpu-1", 1, key)))
                    for principal, key in (("a", "first"), ("v", "second"))]
        async def scenario():
            first_dispatch, release, second_received = asyncio.Event(), asyncio.Event(), asyncio.Event()
            slots = asyncio.Semaphore(1)
            workers = []
            receive = self.session.receive

            def ingress(*args, **kwargs):
                receipt = receive(*args, **kwargs)
                if receipt.sequence == 2:
                    second_received.set()
                return receipt

            async def dispatch(*args, **kwargs):
                receipt = kwargs["receipt"]
                workers.append(receipt.sequence)
                if receipt.sequence == 1:
                    first_dispatch.set()
                    await release.wait()
                async with slots:
                    return await run_in_threadpool(*args, **kwargs)

            with patch.object(self.session, "receive", side_effect=ingress), \
                    patch("api.attack_http.run_in_threadpool", side_effect=dispatch):
                first = asyncio.create_task(self.submit("first"))
                await asyncio.wait_for(first_dispatch.wait(), 2)
                second = asyncio.create_task(self.submit("second", actor="v"))
                try:
                    await asyncio.wait_for(second_received.wait(), 2)
                    status, _ = await _request(self.app, "GET", self.base + "/devices", credential=self.tokens["a"])
                    self.assertEqual(status, 200)
                    self.assertEqual(workers, [1, 3])  # No waiting second worker occupies the slot.
                    self.assertEqual(self.runtime.scheduler.queued_job_ids(), ())
                    self.assertEqual(self.store.read().device_time_us, 0)
                finally:
                    release.set()
                left, right = await asyncio.wait_for(asyncio.gather(first, second), 3)
            self.assertEqual((left[0], right[0]), (202, 202))
            ids = (left[1]["job_id"], right[1]["job_id"])
            self.assertEqual(self.runtime.scheduler.queued_job_ids(), ids)
            records = [self.runtime.scheduler.get(job_id) for job_id in ids]
            self.assertEqual([job.acceptance_sequence for job in records], [1, 2])
            self.assertEqual([job.ingress_receipt.sequence for job in records], [1, 2])
            self.assertEqual([job.ingress_receipt.received_at_ns for job in records], [42, 42])
            self.assertTrue(all("ingress_receipt" not in view[1] for view in (left, right)))
            for session in (baseline[5], self.session):
                session.run_next()
                session.run_next()
            self.assertEqual([baseline[2].scheduler.get(job.job_id).result for job in expected],
                             [self.runtime.scheduler.get(job_id).result for job_id in ids])
            self.assertEqual(baseline[1].read().density_matrix, self.store.read().density_matrix)
            self.assertEqual(baseline[1].read().device_time_us, self.store.read().device_time_us)
        asyncio.run(scenario())

    def test_compile_releases_control_for_status_terminal_and_queued_cancel(self):
        old = asyncio.run(self.submit("old"))[1]["job_id"]
        entered, release = Event(), Event()
        compiler = self.session._service._program_compiler
        compile_program = compiler.compile

        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test compiler barrier timed out")
            return compile_program(*args, **kwargs)

        async def scenario():
            pending = asyncio.create_task(self.submit("new", actor="v"))
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3))
                for method, path, payload in (
                    ("GET", "/jobs/" + old, None),
                    ("POST", "/terminal", {"line": "job status " + old}),
                    ("POST", "/terminal", {"line": "device list"}),
                    ("POST", "/jobs/" + old + "/cancel", None),
                ):
                    response = await asyncio.wait_for(_request(self.app, method, self.base + path,
                        credential=self.tokens["a"], payload=payload), 2)
                    self.assertEqual(response[0], 200)
                self.assertEqual(self.runtime.scheduler.get(old).status.value, "cancelled")
                self.assertEqual(self.store.read().revision, 0)
            finally:
                release.set()
            self.assertEqual((await asyncio.wait_for(pending, 3))[0], 202)
        with patch.object(type(compiler), "compile", side_effect=paused):
            asyncio.run(scenario())

    def test_failed_or_undispatched_head_unblocks_next_without_job_effects(self):
        async def scenario():
            for failure in ("invalid", "dispatch_failure", "cancel_wait"):
                with self.subTest(failure=failure):
                    entered, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()
                    dispatches = 0
                    receive = self.session.receive

                    def ingress(*args, **kwargs):
                        receipt = receive(*args, **kwargs)
                        if receipt.principal_id == "v":
                            received.set()
                        return receipt

                    async def dispatch(*args, **kwargs):
                        nonlocal dispatches
                        dispatches += 1
                        if dispatches == 1:
                            entered.set()
                            await release.wait()
                            if failure == "dispatch_failure":
                                raise RuntimeError("test undispatched worker failure")
                        return await run_in_threadpool(*args, **kwargs)

                    with patch.object(self.session, "receive", side_effect=ingress), \
                            patch("api.attack_http.run_in_threadpool", side_effect=dispatch):
                        first = asyncio.create_task(self.submit(failure, payload={**self.payload, "device_id": "absent"}))
                        await asyncio.wait_for(entered.wait(), 2)
                        second = asyncio.create_task(self.submit(failure + "-valid", actor="v"))
                        try:
                            await asyncio.wait_for(received.wait(), 2)
                            if failure == "cancel_wait":
                                first.cancel()
                                with self.assertRaises(asyncio.CancelledError):
                                    await first
                        finally:
                            release.set()
                        if failure != "cancel_wait":
                            self.assertEqual((await first)[0], 422 if failure == "invalid" else 500)
                        self.assertEqual((await asyncio.wait_for(second, 3))[0], 202)
            self.assertEqual(len(self.runtime.scheduler.queued_job_ids()), 3)
            self.assertEqual(self.store.read().revision, 0)
        asyncio.run(scenario())

    def test_close_wakes_queued_ingress_and_drains_pure_compilation_without_admission(self):
        entered, release, revoked = Event(), Event(), Event()
        compiler = self.session._service._program_compiler
        compile_program, close_authority = compiler.compile, self.authority.close

        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test compiler barrier timed out")
            return compile_program(*args, **kwargs)

        def revoke():
            close_authority()
            revoked.set()

        async def scenario():
            second_received = asyncio.Event()
            receive = self.session.receive
            def ingress(*args, **kwargs):
                receipt = receive(*args, **kwargs)
                if receipt.sequence == 2:
                    second_received.set()
                return receipt
            with patch.object(self.session, "receive", side_effect=ingress):
                first = asyncio.create_task(self.submit("first"))
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3))
                second = asyncio.create_task(self.submit("second", actor="v"))
                closing = None
                try:
                    await asyncio.wait_for(second_received.wait(), 2)
                    closing = asyncio.create_task(asyncio.to_thread(self.registry.discard, "world"))
                    self.assertTrue(await asyncio.wait_for(asyncio.to_thread(revoked.wait, 2), 3))
                    self.assertEqual((await asyncio.wait_for(second, 2))[0], 401)
                    self.assertFalse(closing.done())
                finally:
                    release.set()
                self.assertEqual((await asyncio.wait_for(first, 3))[0], 401)
                await asyncio.wait_for(closing, 3)
            self.assertEqual(self.runtime.scheduler.queued_job_ids(), ())
            self.assertEqual(self.store.read().revision, 0)
            self.assertTrue(self.environment.established)
            self.assertTrue(all(record.outcome is ReceiptOutcome.ABANDONED for record in self.session.receipt_records()))
        with patch.object(type(compiler), "compile", side_effect=paused), \
                patch.object(self.authority, "close", side_effect=revoke):
            asyncio.run(scenario())

    def test_false_read_receipt_and_escaped_client_cannot_bypass_admission(self):
        request = ProgramSubmitRequest(CircuitProgram.from_editor_json(circuit_json()), "qpu-1", 1, "new")
        with self.assertRaises(PermissionError):
            self.session.request(self.tokens["a"], lambda c, w: c.submit_program(request), operation_id="device.list")
        escaped = []
        self.session.request(self.tokens["a"], lambda c, w: escaped.append(c), operation_id="device.list")
        with self.assertRaises(ValueError):
            escaped[0].submit_program(request)
        self.assertEqual(self.runtime.scheduler.queued_job_ids(), ())

    def test_pending_cap_cancelled_waiter_admission_cap_and_replay_share_one_boundary(self):
        fixture = session_fixture(max_jobs=1, max_pending_receipts=2)
        session, credentials, runtime = fixture[5], fixture[4], fixture[2]
        request = CircuitProgram.from_editor_json(circuit_json())

        async def scenario():
            head = session.receive(credentials["a"], "program.submit")
            second = session.receive(credentials["v"], "program.submit")
            retired = session.dispatch_ready(credentials["v"], second)
            with self.assertRaises(PermissionError):
                await session.request_async(credentials["a"], lambda c, w: c.list_devices(), receipt=second)
            self.assertIsNone(session.receipt_records()[-1].outcome)
            waiting = asyncio.create_task(session.request_async(credentials["v"],
                lambda c, w: c.submit_program(ProgramSubmitRequest(request, "qpu-1", 1, "second")), receipt=second))
            await asyncio.sleep(0)  # Hand off once; no time-based race or polling loop.
            from api.attack_sessions import SessionCapacityError
            with self.assertRaises(SessionCapacityError):
                session.receive(credentials["v"], "program.submit")
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertTrue(retired.done())
            self.assertEqual(runtime.scheduler.queued_job_ids(), ())
            accepted = await session.request_async(credentials["a"],
                lambda c, w: c.submit_program(ProgramSubmitRequest(request, "qpu-1", 1, "first")), receipt=head)
            with self.assertRaises(SessionCapacityError):
                await session.request_async(credentials["v"],
                    lambda c, w: c.submit_program(ProgramSubmitRequest(request, "qpu-1", 1, "third")),
                    operation_id="program.submit")
            replay = await session.request_async(credentials["a"],
                lambda c, w: c.submit_program(ProgramSubmitRequest(request, "qpu-1", 1, "first")),
                operation_id="program.submit")
            self.assertEqual(accepted.job_id, replay.job_id)
            self.assertIs(runtime.scheduler.get(accepted.job_id).ingress_receipt, head)
            self.assertEqual(runtime.scheduler.queued_job_ids(), (accepted.job_id,))
            self.assertEqual(fixture[1].read().revision, 0)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

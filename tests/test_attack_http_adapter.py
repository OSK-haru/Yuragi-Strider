"""Exercise the opt-in ASGI router without httpx, sockets or a server."""

import asyncio
import json
import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import FastAPI

from api.attack_http import create_attack_router
from core.attack_simulation.request_receipts import ReceiptOutcome
from tests.test_attack_sessions import circuit_json, session_fixture
from tests.test_local_program_runtime import program_json


async def _request(app, method, path, *, credential=None, payload=None, headers=(), raw=None, chunks=None):
    body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")
    messages = []
    incoming = list(chunks) if chunks is not None else [body]

    async def receive():
        if incoming:
            return {"type": "http.request", "body": incoming.pop(0), "more_body": bool(incoming)}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    request_headers = [(b"content-type", b"application/json"), *headers]
    if credential is not None:
        request_headers.append((b"authorization", f"Bearer {credential}".encode()))
    await asyncio.wait_for(app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": request_headers, "client": ("testclient", 1), "server": ("testserver", 80)}, receive, send), timeout=10)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    encoded = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, json.loads(encoded) if encoded else None


class AttackHttpAdapterTests(unittest.TestCase):
    def test_initialization_default_replay_denial_and_invalid_modes(self):
        first = self.submit("prepare-default")
        self.assertEqual(first, self.submit("prepare-default", initialization="prepare"))
        self.assertEqual(self.submit("denied", initialization="inherit"),
                         (403, {"detail": {"code": "OPERATION_DENIED"}}))
        for mode in (None, True, 1, "INHERIT", "", []):
            with self.subTest(mode=mode):
                self.assertEqual(self.submit("invalid", initialization=mode)[0], 422)
        self.assertEqual(self.runtime.scheduler.queued_job_ids(), (first[1]["job_id"],))
        self.assertEqual(self.store.read().revision, 0)

    def test_structured_and_terminal_inherit_share_common_auth_and_idempotency(self):
        from core.attack_simulation.program_request import STATE_INHERIT_OPERATION_ID
        operations = ("device.info.read", "program.submit", "job.status.read", "job.result.read", "job.cancel",
                      STATE_INHERIT_OPERATION_ID)
        _, store, runtime, _, tokens, session, registry = session_fixture(
            attacker_operations=operations, initializations=("prepare", "inherit"),
        )
        app = FastAPI()
        app.include_router(create_attack_router(registry))
        text = program_json(2)
        for actor in ("a", "v"):
            key = "inherit-" + actor
            def call(path, payload):
                return asyncio.run(_request(app, "POST", self.base + path, credential=tokens[actor],
                                           payload=payload, headers=[(b"idempotency-key", key.encode())]))
            payload = {"device_id": "qpu-1", "shots": 2, "circuit_config": json.loads(text),
                       "initialization": "inherit"}
            status, job = call("/programs", payload)
            self.assertEqual(status, 202)
            self.assertEqual(call("/workspace/circuits", {"name": "two.json", "circuit_json": text})[0], 204)
            self.assertEqual(call("/terminal", {"line":
                "program submit two.json --device qpu-1 --shots 2 --initialization inherit"}), (200, job))
            changed = dict(payload, initialization="prepare")
            self.assertEqual(call("/programs", changed)[0], 422)
            with patch.object(runtime.reset_models, "run_register", wraps=runtime.reset_models.run_register) as resets:
                self.assertEqual(session.run_next().status.value, "succeeded")
                # Only cross-tenant service handover, never implicit shot initialization.
                self.assertEqual(resets.call_count, int(actor == "v"))
        self.assertEqual(store.read().physical_qubit_ids, (0, 1))

    def test_receipt_precedes_worker_wait_and_program_conversion_and_stays_private(self):
        from api.attack_http import CircuitProgram, run_in_threadpool
        observations = []
        convert = CircuitProgram.from_editor_json
        async def dispatch(*args, **kwargs):
            pending = self.session.receipt_records()
            observations.append(("worker", len(pending), pending[-1].outcome))
            self.assertEqual(self.runtime.scheduler.queued_job_ids(), ())
            return await run_in_threadpool(*args, **kwargs)
        def conversion(text):
            pending = self.session.receipt_records()
            observations.append(("conversion", len(pending), pending[-1].outcome))
            return convert(text)
        with patch("api.attack_http.run_in_threadpool", side_effect=dispatch), \
                patch.object(CircuitProgram, "from_editor_json", side_effect=conversion):
            status, job = self.submit()
        self.assertEqual(status, 202)
        self.assertEqual(observations, [("worker", 1, None), ("conversion", 1, None)])
        record = self.session.receipt_records()[0]
        self.assertEqual(record.outcome, ReceiptOutcome.COMPLETED)
        self.assertEqual(record.job_id, job["job_id"])
        self.assertEqual(record.receipt.operation_id, "program.submit")
        self.assertTrue(record.receipt.received_at_ns >= 0)
        self.assertEqual(set(job), {"job_id", "status", "error_code"})
        self.assertEqual(self.store.read().device_time_us, 0)

    def test_retry_receipts_are_distinct_but_share_one_admitted_job(self):
        first = self.submit("same")
        replay = self.submit("same")
        self.assertEqual(first, replay)
        records = self.session.receipt_records()
        self.assertEqual([record.receipt.sequence for record in records], [1, 2])
        self.assertEqual([record.job_id for record in records], [first[1]["job_id"]] * 2)
        self.assertEqual(len(self.runtime.scheduler.queued_job_ids()), 1)

    def test_worker_failure_and_dispatch_cancellation_release_pending_receipt(self):
        async def fail(*args, **kwargs):
            raise RuntimeError("PRIVATE_WORKER_FAILURE")
        with patch("api.attack_http.run_in_threadpool", side_effect=fail):
            self.assertEqual(self.request("GET", "/devices"),
                             (500, {"detail": {"code": "INTERNAL_ERROR"}}))
        self.assertEqual(self.session.receipt_records()[-1].outcome, ReceiptOutcome.ABANDONED)

        async def cancelled_dispatch():
            entered = asyncio.Event()
            async def pause(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            with patch("api.attack_http.run_in_threadpool", side_effect=pause):
                task = asyncio.create_task(_request(self.app, "GET", self.base + "/devices",
                                                   credential=self.tokens["a"]))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=2)
                finally:
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
        asyncio.run(cancelled_dispatch())
        self.assertEqual(self.session.receipt_records()[-1].outcome, ReceiptOutcome.ABANDONED)
        self.assertEqual(self.request("GET", "/devices")[0], 200)
        self.assertEqual(self.runtime.scheduler.queued_job_ids(), ())

    def test_receipt_capacity_and_close_during_dispatch_do_not_run_jobs(self):
        from api.attack_http import run_in_threadpool
        _, _, runtime, _, credentials, session, registry = session_fixture(max_pending_receipts=1)
        app = FastAPI()
        app.include_router(create_attack_router(registry))
        pending = session.receive(credentials["a"], "device.list")
        limited = asyncio.run(_request(app, "GET", self.base + "/devices", credential=credentials["a"]))
        self.assertEqual(limited, (429, {"detail": {"code": "HOST_CAPACITY_REACHED"}}))
        session.abandon_receipt(pending)
        async def closing(*args, **kwargs):
            session.close()
            return await run_in_threadpool(*args, **kwargs)
        with patch("api.attack_http.run_in_threadpool", side_effect=closing):
            response = asyncio.run(_request(app, "GET", self.base + "/devices", credential=credentials["a"]))
        self.assertEqual(response, (401, {"detail": {"code": "UNAUTHENTICATED"}}))
        self.assertEqual(session.receipt_records()[-1].outcome, ReceiptOutcome.ABANDONED)
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())

    def test_cancelling_http_wait_does_not_abandon_a_running_worker_or_duplicate_job(self):
        from threading import Event
        entered, release, completed = Event(), Event(), Event()
        compile_program = self.session._service._program_compiler.compile
        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test compiler barrier timed out")
            try:
                return compile_program(*args, **kwargs)
            finally:
                completed.set()
        async def scenario():
            payload = {"device_id": "qpu-1", "shots": 1, "circuit_config": json.loads(circuit_json())}
            task = asyncio.create_task(_request(self.app, "POST", self.base + "/programs",
                                               credential=self.tokens["a"], payload=payload,
                                               headers=[(b"idempotency-key", b"cancelled-wait")]))
            try:
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), timeout=3))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=2)
                # Cancellation stops waiting, not the running worker/operation.
                self.assertIsNone(self.session.receipt_records()[-1].outcome)
            finally:
                release.set()
            self.assertTrue(await asyncio.wait_for(asyncio.to_thread(completed.wait, 2), timeout=3))
            # A replay after the original worker commits returns that one job.
            response = await _request(self.app, "POST", self.base + "/programs", credential=self.tokens["a"],
                                      payload=payload, headers=[(b"idempotency-key", b"cancelled-wait")])
            self.assertEqual(response[0], 202)
        with patch.object(type(self.session._service._program_compiler), "compile", side_effect=paused):
            asyncio.run(scenario())
        records = self.session.receipt_records()
        self.assertEqual([record.outcome for record in records], [ReceiptOutcome.COMPLETED] * 2)
        self.assertEqual(records[0].job_id, records[1].job_id)
        self.assertEqual(len(self.runtime.scheduler.queued_job_ids()), 1)
        self.assertEqual(self.store.read().revision, 0)

    def test_terminal_transport_key_replays_and_explicit_command_id_has_priority(self):
        self.request("POST", "/workspace/circuits", payload={"name": "bell.json", "circuit_json": circuit_json()})
        line = "program   submit bell.json --device qpu-1 --shots 1"
        def send(key, command=line, actor="a"):
            return self.request("POST", "/terminal", actor=actor,
                                payload={"line": command}, headers=[(b"idempotency-key", key.encode())])
        first = send("transport-first")
        self.assertEqual(first[0], 200)
        self.assertEqual(send("transport-first"), first)
        second = send("transport-second")
        self.assertNotEqual(second[1]["job_id"], first[1]["job_id"])
        explicit = send("unused-fallback", line + " --request-id explicit")
        self.assertEqual(send("another-fallback", line + " --request-id explicit"), explicit)
        self.assertEqual(len(self.runtime.scheduler.queued_job_ids()), 3)
        # The shared compiler still checks changes under the same identity.
        self.assertEqual(send("transport-first", line.replace("--shots 1", "--shots 2"))[0], 422)
        self.assertEqual(send("transport-first", "job status " + first[1]["job_id"])[0], 200)
        for key in ("", "bad key", "x" * 129):
            self.assertEqual(send(key)[0], 422)
        duplicate = self.request("POST", "/terminal", payload={"line": line},
            headers=[(b"idempotency-key", b"a"), (b"idempotency-key", b"b")])
        self.assertEqual(duplicate[0], 422)
        self.assertEqual(self.store.read().revision, 0)

    def test_public_job_ids_do_not_expose_global_acceptance_order(self):
        _, first = self.submit("own-first")
        self.submit("other-first", actor="v")
        self.submit("other-second", actor="v")
        _, second = self.submit("own-second")
        for view in (first, second):
            self.assertEqual(UUID(view["job_id"].removeprefix("job-")).version, 4)
            self.assertNotIn("acceptance_sequence", view)
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(self.runtime.scheduler.get(first["job_id"]).acceptance_sequence, 1)
        self.assertEqual(self.runtime.scheduler.get(second["job_id"]).acceptance_sequence, 4)
        self.assertEqual(self.submit("own-first")[1]["job_id"], first["job_id"])

    def test_workspace_name_limit_matches_shared_utf8_boundary(self):
        for name in ("a" * 129, "a" * 256, "é" * 128):
            with self.subTest(name_length=len(name)):
                response = self.request("POST", "/workspace/circuits", payload={
                    "name": name, "circuit_json": circuit_json()})
                self.assertEqual(response[0], 204)
        for name in ("a" * 257, "é" * 129):
            response = self.request("POST", "/workspace/circuits", payload={
                "name": name, "circuit_json": circuit_json()})
            self.assertEqual(response[0], 422)

    def setUp(self):
        self.environment, self.store, self.runtime, self.authority, self.tokens, self.session, self.registry = session_fixture()
        self.app = FastAPI()
        self.app.include_router(create_attack_router(self.registry))
        self.base = "/api/attack/sessions/world"

    def request(self, method, path, *, actor="a", **kwargs):
        return asyncio.run(_request(self.app, method, self.base + path,
            credential=self.tokens.get(actor), **kwargs))

    def submit(self, key="request", *, actor="a", **overrides):
        payload = {"device_id": "qpu-1", "shots": 8, "circuit_config": json.loads(circuit_json()), **overrides}
        return self.request("POST", "/programs", actor=actor, payload=payload, headers=[(b"idempotency-key", key.encode())])

    def test_async_structured_and_terminal_paths_share_job_and_public_projection(self):
        status, devices = self.request("GET", "/devices")
        self.assertEqual(status, 200)
        self.assertEqual(devices[0]["max_qubits"], 2)
        status, job = self.submit()
        self.assertEqual(status, 202)
        self.assertEqual(job["status"], "queued")
        self.assertEqual(self.request("GET", f"/jobs/{job['job_id']}/result"),
            (409, {"detail": {"code": "RESULT_NOT_READY"}}))
        self.assertEqual(self.store.read().revision, 0)
        status, _ = self.request("POST", "/workspace/circuits", payload={"name": "bell.json", "circuit_json": circuit_json()})
        self.assertEqual(status, 204)
        status, replay = self.request("POST", "/terminal", payload={
            "line": "program   submit bell.json --shots 8 --device qpu-1 --request-id request"})
        self.assertEqual(status, 200)
        self.assertEqual(replay["job_id"], job["job_id"])
        self.assertEqual(self.runtime.scheduler.queued_job_ids(), (job["job_id"],))
        self.session.run_next()
        status, result = self.request("GET", f"/jobs/{job['job_id']}/result")
        self.assertEqual(status, 200)
        self.assertEqual(set(result), {"job_id", "status", "result", "error_code"})
        self.assertEqual(set(result["result"]), {"shots", "counts"})
        self.assertTrue(set(result["result"]["counts"]) <= {"00", "11"})
        self.assertEqual(sum(result["result"]["counts"].values()), 8)
        self.assertEqual(self.request("POST", "/terminal", payload={"line": f"job result {job['job_id']}"}), (200, result))

    def test_identity_and_other_actor_jobs_cannot_be_observed_or_cancelled(self):
        _, job = self.submit()
        for suffix, method in (("", "GET"), ("/result", "GET"), ("/cancel", "POST")):
            foreign = self.request(method, f"/jobs/{job['job_id']}{suffix}", actor="v")
            absent = self.request(method, f"/jobs/absent{suffix}", actor="v")
            self.assertEqual(foreign, absent)
            self.assertEqual(foreign[0], 404)
        for field in ("principal_id", "tenant_id", "sampling_seed", "layout", "role"):
            status, data = self.submit("bad", **{field: "DO_NOT_ECHO"})
            self.assertEqual(status, 422)
            self.assertNotIn("DO_NOT_ECHO", json.dumps(data))
        self.assertEqual(self.runtime.scheduler.queued_job_ids(), (job["job_id"],))
        self.assertEqual(self.store.read().revision, 0)

    def test_authentication_unknown_session_and_disposal_are_indistinguishable(self):
        self.assertEqual(self.request("GET", "/devices", actor="missing")[0], 401)
        invalid = asyncio.run(_request(self.app, "GET", self.base + "/devices", credential="invalid"))
        unknown = asyncio.run(_request(self.app, "GET", "/api/attack/sessions/unknown/devices", credential=self.tokens["a"]))
        self.assertEqual(invalid, unknown)
        _, job = self.submit()
        self.registry.discard("world")
        self.assertEqual(self.request("GET", f"/jobs/{job['job_id']}")[0], 401)

    def test_cancelled_queue_does_not_execute_and_body_is_not_identity_input(self):
        _, job = self.submit()
        self.assertEqual(self.request("POST", f"/jobs/{job['job_id']}/cancel", payload={"tenant_id": "tv"})[0], 422)
        self.assertEqual(self.request("POST", f"/jobs/{job['job_id']}/cancel")[1]["status"], "cancelled")
        self.assertIsNone(self.session.run_next())
        self.assertEqual(self.store.read().revision, 0)
        self.assertIsNone(self.request("GET", f"/jobs/{job['job_id']}/result")[1]["result"])

    def test_terminal_has_no_supervisor_shell_or_foreign_workspace_access(self):
        self.request("POST", "/workspace/circuits", payload={"name": "bell.json", "circuit_json": circuit_json()})
        foreign = self.request("POST", "/terminal", actor="v", payload={"line": "program submit bell.json --device qpu-1 --shots 1"})
        self.assertEqual(foreign[0], 404)
        for line in ("run_next", "python secret.py", "job status ../secret", "program submit ../secret --device qpu-1 --shots 1"):
            self.assertEqual(self.request("POST", "/terminal", payload={"line": line})[0], 422)
        for path in ("/run-next", "/establish", "/assumptions", "/credentials"):
            self.assertEqual(self.request("POST", path)[0], 404)
        self.assertEqual(self.runtime.scheduler.queued_job_ids(), ())

    def test_bytes_strict_types_and_duplicate_auth_are_rejected_before_admission(self):
        for value in (True, "8", 8.0):
            self.assertEqual(self.submit(shots=value)[0], 422)
        for raw in (b"{", b'{"line": NaN}', b'{"line":"help","line":"device list"}'):
            self.assertEqual(self.request("POST", "/terminal", raw=raw)[0], 422)
        response = self.request("POST", "/terminal", chunks=[b" " * 40000, b" " * 40000])
        self.assertEqual(response[0], 413)
        response = self.request("GET", "/devices", headers=[(b"authorization", b"Bearer invalid")])
        self.assertEqual(response[0], 401)
        self.assertEqual(self.runtime.scheduler.queued_job_ids(), ())

    def test_host_capacity_replays_and_busy_are_not_simulated_failures(self):
        _, _, runtime, _, tokens, session, registry = session_fixture(max_jobs=1)
        app = FastAPI()
        app.include_router(create_attack_router(registry))
        payload = {"device_id": "qpu-1", "shots": 1, "circuit_config": json.loads(circuit_json())}
        call = lambda key: asyncio.run(_request(app, "POST", self.base + "/programs", credential=tokens["a"],
            payload=payload, headers=[(b"idempotency-key", key.encode())]))
        first = call("same")
        self.assertEqual(call("same"), first)
        self.assertEqual(call("new")[0], 429)
        self.assertEqual(len(runtime.scheduler.queued_job_ids()), 1)
        # Numeric work owns the executor lock, not the participant control lock.
        session._execution_lock.acquire()
        try:
            response = asyncio.run(_request(app, "GET", self.base + "/devices", credential=tokens["a"]))
            self.assertEqual(response[0], 200)
        finally:
            session._execution_lock.release()

    def test_operation_grants_are_not_invented_by_http(self):
        _, store, runtime, _, tokens, _, registry = session_fixture(attacker_operations=("job.status.read",))
        app = FastAPI()
        app.include_router(create_attack_router(registry))
        response = asyncio.run(_request(app, "GET", self.base + "/devices", credential=tokens["a"]))
        self.assertEqual(response, (403, {"detail": {"code": "OPERATION_DENIED"}}))
        self.assertEqual(runtime.scheduler.queued_job_ids(), ())
        self.assertEqual(store.read().revision, 0)

    def test_internal_exceptions_do_not_echo_private_values(self):
        with patch.object(self.session, "request", side_effect=RuntimeError("PRIVATE_INTERNAL_DETAIL")):
            response = self.request("GET", "/devices")
        self.assertEqual(response, (500, {"detail": {"code": "INTERNAL_ERROR"}}))


if __name__ == "__main__":
    unittest.main()

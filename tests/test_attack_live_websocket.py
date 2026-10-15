"""Opt-in attack WebSocket adapter exercised as ASGI messages only."""

import asyncio
import json
from threading import Event
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from starlette.websockets import WebSocketDisconnect

from api.attack_live_websocket import (
    MAX_WEBSOCKET_MESSAGE_BYTES, PROTOCOL, PausedWorldWebSocketRegistry,
    create_live_attack_websocket_router,
)
from api.attack_paused_world import PausedWorldController, VirtualHostClock
from core.attack_simulation.program_request import CircuitProgram
from api.attack_sessions import WebOwnerContext
from core.attack_simulation.live_time_policy import LiveTimePolicy
from tests.test_attack_live_world import request, setup
from tests.test_local_program_runtime import program_json


ORIGIN = "https://simulator.example"
POLICY = {"mode": "explicit_wait", "max_wait_us": 1_000_000.0,
          "max_steps_per_wait": 10_000}


class DirectWebSocket:
    """A tiny in-process ASGI message bridge; it opens no socket or server."""

    def __init__(self, app, path, *, origin=ORIGIN, owner=None, protocols=(PROTOCOL,)):
        self.app = app
        self.path = path
        self.owner = owner
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        headers = [(b"origin", origin.encode())] if origin is not None else []
        raw_path = path.encode()
        self.scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "scheme": "ws", "path": path, "raw_path": raw_path,
            "query_string": b"", "headers": headers, "client": ("asgi-test", 1),
            "server": ("asgi-test", 80), "subprotocols": list(protocols),
            "state": {"test_owner": owner},
        }
        self.task = None
        self.fail_reply = False

    async def _receive(self):
        return await self.incoming.get()

    async def _send(self, message):
        if (self.fail_reply and message["type"] == "websocket.send"
                and json.loads(message.get("text", "{}")).get("type") == "reply"):
            raise WebSocketDisconnect(code=1006)
        await self.outgoing.put(message)

    async def start(self):
        self.task = asyncio.create_task(self.app(self.scope, self._receive, self._send))
        await self.incoming.put({"type": "websocket.connect"})
        return await asyncio.wait_for(self.outgoing.get(), 2)

    async def send(self, value):
        raw = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
        await self.incoming.put({"type": "websocket.receive", "text": raw})

    async def next(self):
        return await asyncio.wait_for(self.outgoing.get(), 5)

    async def next_json(self):
        message = await self.next()
        if message["type"] == "websocket.send":
            return json.loads(message.get("text", message.get("bytes", b"{}")))
        return message

    async def stop(self):
        if self.task is not None and not self.task.done():
            await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(self.task, 2)


class LiveWebSocketTests(unittest.TestCase):
    def test_discard_fences_active_wait_before_releasing_registry_slot(self):
        async def scenario():
            socket, _ = await self._connect()
            entered, release = Event(), Event()
            original = self.executor.prepare_next
            def slow_prepare():
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test preparation was not released")
                return original()
            with patch.object(self.executor, "prepare_next", side_effect=slow_prepare):
                await socket.send({"type": "command", "command_id": "closing-wait",
                                   "operation": "wait.duration", "payload": {"duration_us": 5000}})
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                discard = asyncio.create_task(self.registry.discard("w1"))
                try:
                    await asyncio.wait_for(asyncio.to_thread(
                        self.controller._close_requested.wait, 2), 3)
                    self.assertIsNotNone(self.registry._get("w1"))
                    self.assertFalse(discard.done())
                finally:
                    release.set()
                self.assertEqual((await socket.next_json())["code"], "WORLD_UNAVAILABLE")
                await asyncio.wait_for(discard, 3)
                self.assertIsNone(self.registry._get("w1"))
        asyncio.run(scenario())

    def test_other_socket_wait_cannot_interleave_issued_time_and_capture(self):
        async def scenario():
            first, _ = await self._connect()
            second, _ = await self._connect()
            entered, release = Event(), Event()
            original_time = self.controller.request_world_time_us
            def paused_time(receipt):
                value = original_time(receipt)
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test timestamp read was not released")
                return value
            original_to_thread = asyncio.to_thread
            with patch.object(self.controller, "request_world_time_us", side_effect=paused_time):
                await first.send({"type": "command", "command_id": "timestamp-status",
                                  "operation": "job.status.read", "payload": {"job_id": "job"}})
                self.assertTrue(await original_to_thread(entered.wait, 2))
                await second.send({"type": "command", "command_id": "timestamp-wait",
                                   "operation": "wait.duration", "payload": {"duration_us": 10}})
                await asyncio.sleep(0.05)
                self.assertEqual(self.controller._clock.value, 1_000_000_000)
                release.set()
                self.assertEqual((await first.next_json())["type"], "accepted")
                self.assertEqual((await second.next_json())["type"], "wait.completed")
            handle = next(iter(self.registry._get("w1").handles.values()))
            self.assertEqual(handle.issued_at_world_us,
                             self.controller.request_world_time_us(handle.receipt))
        asyncio.run(scenario())

    def test_contending_socket_does_not_consume_worker_while_wait_is_active(self):
        async def scenario():
            first, _ = await self._connect()
            second, _ = await self._connect()
            entered, release = Event(), Event()
            original = self.controller.wait
            calls = []
            def slow_wait(*args, **kwargs):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test wait was not released")
                return original(*args, **kwargs)
            original_to_thread = asyncio.to_thread
            async def observed_to_thread(func, *args, **kwargs):
                calls.append(getattr(func, "__name__", type(func).__name__))
                return await original_to_thread(func, *args, **kwargs)
            with patch.object(self.controller, "wait", side_effect=slow_wait), patch(
                    "api.attack_live_websocket.asyncio.to_thread", side_effect=observed_to_thread):
                await first.send({"type": "command", "command_id": "slow-wait",
                                  "operation": "wait.duration", "payload": {"duration_us": 10}})
                self.assertTrue(await original_to_thread(entered.wait, 2))
                await second.send({"type": "command", "command_id": "other-status",
                                   "operation": "job.status.read", "payload": {"job_id": "job"}})
                await asyncio.sleep(0.05)
                self.assertNotIn("status", calls)
                release.set()
                self.assertEqual((await first.next_json())["type"], "wait.completed")
                self.assertEqual((await second.next_json())["type"], "accepted")
        asyncio.run(scenario())

    def setUp(self):
        self.owner = WebOwnerContext()
        clock = VirtualHostClock()
        self.args, self.host, self.runner, self.executor, self.world = setup(
            owner_context=self.owner, host_clock_source=clock, live_time_policy=POLICY)
        self.controller = PausedWorldController.from_manifest(self.world, clock)
        self.registry = PausedWorldWebSocketRegistry(max_worlds=2,
            max_connections_per_world=4, max_retained_requests=64)
        self.registry.register("w1", self.controller)

        def resolve_owner(websocket):
            return websocket.scope["state"]["test_owner"]

        app = FastAPI()
        app.include_router(create_live_attack_websocket_router(
            self.registry, resolve_owner=resolve_owner, allowed_origins=(ORIGIN,)))
        self.app = app
        self.sockets = []

    def tearDown(self):
        asyncio.run(self._cleanup())

    async def _cleanup(self):
        for socket in self.sockets:
            await socket.stop()
        if self.registry._get("w1") is not None:
            await self.registry.discard("w1")

    async def _connect(self, *, credential=None, owner=None, world_id="w1", origin=ORIGIN):
        socket = DirectWebSocket(self.app, f"/api/attack/live/{world_id}/socket",
                                 origin=origin, owner=self.owner if owner is None else owner)
        self.sockets.append(socket)
        first = await socket.start()
        if first["type"] != "websocket.accept":
            return socket, first
        await socket.send({"type": "authenticate", "credential": credential or self.args[4]["a"]})
        response = await socket.next()
        if response["type"] == "websocket.send":
            response = json.loads(response.get("text", response.get("bytes", b"{}")))
        return socket, response

    def test_submit_wait_delivers_released_reply_without_internal_timestamps(self):
        async def scenario():
            socket, ready = await self._connect()
            self.assertEqual((ready["type"], ready["world_time_us"]), ("ready", 0.0))
            # The attacker's ordinary device information, so the editor can match it.
            self.assertEqual(ready["max_program_qubits"], 2)
            self.assertIs(ready["victim_activity"], False)  # Not enabled in this scenario.
            payload = {"device_id": "qpu-1", "shots": 2,
                       "circuit_config": json.loads(program_json(2)), "initialization": "prepare"}
            await socket.send({"type": "command", "command_id": "submit-1",
                               "operation": "program.submit", "payload": payload})
            accepted = await socket.next_json()
            self.assertEqual(accepted["type"], "accepted")
            self.assertEqual(self.controller.now(), 0.0)  # Capture does not advance D60 time.
            self.assertEqual(len(self.world._operations), 1)
            wait_frame = {"type": "command", "command_id": "wait-1",
                "operation": "wait.request", "payload": {"request_id": accepted["request_id"]}}
            await socket.send(wait_frame)
            original = CircuitProgram.from_editor_json.__func__
            parsed = []
            def observe_parse(cls, text):
                parsed.append(text)
                return original(cls, text)
            with patch.object(CircuitProgram, "from_editor_json", classmethod(observe_parse)):
                completed = await socket.next_json()
                reply = await socket.next_json()
            self.assertEqual(len(parsed), 1)  # Parse occurs during declared arrival, after capture.
            self.assertEqual(completed["type"], "wait.completed")
            self.assertGreater(completed["world_time_us"], 0.0)
            self.assertEqual(reply["type"], "reply")
            self.assertEqual(reply["request_id"], accepted["request_id"])
            self.assertIsNone(reply["error_code"])
            settled_at = self.controller.now()
            await socket.send(wait_frame)
            self.assertEqual(await socket.next_json(), completed)
            self.assertEqual(await socket.next_json(), reply)
            self.assertEqual(self.controller.now(), settled_at)  # Lost-response retry is idempotent.
            serialized = json.dumps([ready, accepted, completed, reply])
            for private in ("receipt", "host_ns", "tenant", "trace", "sequence", "device_time"):
                self.assertNotIn(private, serialized)
        asyncio.run(scenario())

    def test_idempotent_command_replay_does_not_submit_twice(self):
        async def scenario():
            socket, ready = await self._connect()
            self.assertEqual(ready["type"], "ready")
            payload = {"device_id": "qpu-1", "shots": 2,
                       "circuit_config": json.loads(program_json(2)), "initialization": "prepare"}
            frame = {"type": "command", "command_id": "same-submit",
                     "operation": "program.submit", "payload": payload}
            await socket.send(frame)
            first = await socket.next_json()
            await socket.send(frame)
            replay = await socket.next_json()
            self.assertEqual(replay, first)
            self.assertEqual(len(self.world._operations), 1)
            changed = {**frame, "payload": {**payload, "shots": 3}}
            await socket.send(changed)
            self.assertEqual((await socket.next_json())["code"], "IDEMPOTENCY_CONFLICT")
        asyncio.run(scenario())

    def test_pending_list_is_principal_scoped_public_and_time_neutral(self):
        async def scenario():
            attacker, ready = await self._connect()
            self.assertEqual(ready["world_time_us"], 0.0)
            payload = {"device_id": "qpu-1", "shots": 2,
                       "circuit_config": json.loads(program_json(2)), "initialization": "prepare"}
            await attacker.send({"type": "command", "command_id": "pending-submit",
                                 "operation": "program.submit", "payload": payload})
            submit = await attacker.next_json()
            await attacker.send({"type": "command", "command_id": "pending-status",
                                 "operation": "job.status.read", "payload": {"job_id": "own-job"}})
            status = await attacker.next_json()
            self.assertEqual(status["type"], "accepted")
            submit_time = submit["issued_at_world_us"]
            status_time = status["issued_at_world_us"]
            resumed, ready = await self._connect()
            self.assertEqual(ready["world_time_us"], 0.0)
            command = {"type": "command", "command_id": "pending-list-1",
                       "operation": "request.pending.list", "payload": {}}
            await resumed.send(command)
            listed = await resumed.next_json()
            self.assertEqual(listed, {"type": "pending.list", "command_id": "pending-list-1",
                "requests": [
                    {"request_id": submit["request_id"], "kind": "submit", "job_id": None,
                     "issued_at_world_us": submit_time},
                    {"request_id": status["request_id"], "kind": "status", "job_id": "own-job",
                     "issued_at_world_us": status_time},
                ]})
            self.assertEqual(self.controller.now(), 0.0)
            victim, _ = await self._connect(credential=self.args[4]["v"])
            await victim.send({**command, "command_id": "victim-list"})
            self.assertEqual((await victim.next_json())["requests"], [])
            await resumed.send({**command, "command_id": "invalid-list", "payload": {"principal": "v"}})
            self.assertEqual((await resumed.next_json())["code"], "INVALID_REQUEST")
            for index, request_id in enumerate((submit["request_id"], status["request_id"])):
                await resumed.send({"type": "command", "command_id": f"pending-wait-{index}",
                                    "operation": "wait.request", "payload": {"request_id": request_id}})
                self.assertEqual((await resumed.next_json())["type"], "wait.completed")
                self.assertEqual((await resumed.next_json())["request_id"], request_id)
            await resumed.send({**command, "command_id": "pending-list-2"})
            self.assertEqual((await resumed.next_json())["requests"], [])
            serialized = json.dumps(listed)
            for private in ("receipt", "host_ns", "tenant", "trace", "sequence", "credential"):
                self.assertNotIn(private, serialized)
        asyncio.run(scenario())

    def test_reply_returns_to_the_issuing_connection_while_it_is_open(self):
        # The page and a script of one owner each hold a connection. A wait on
        # the page must not take the script's reply; once the script closes,
        # its uncollected reply is available to the page (reload and resume).
        async def scenario():
            page, _ = await self._connect()
            script, _ = await self._connect()
            payload = {"device_id": "qpu-1", "shots": 2,
                       "circuit_config": json.loads(program_json(2)), "initialization": "prepare"}
            await script.send({"type": "command", "command_id": "script-submit",
                               "operation": "program.submit", "payload": payload})
            request_id = (await script.next_json())["request_id"]
            await page.send({"type": "command", "command_id": "page-wait",
                             "operation": "wait.duration", "payload": {"duration_us": 1_000_000}})
            self.assertEqual((await page.next_json())["type"], "wait.completed")
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(page.outgoing.get(), 0.3)
            await script.send({"type": "command", "command_id": "script-wait",
                               "operation": "wait.duration", "payload": {"duration_us": 1}})
            self.assertEqual((await script.next_json())["type"], "wait.completed")
            self.assertEqual((await script.next_json())["request_id"], request_id)

            await script.send({"type": "command", "command_id": "orphan-submit",
                               "operation": "program.submit", "payload": payload})
            orphan_id = (await script.next_json())["request_id"]
            await script.stop()
            await page.send({"type": "command", "command_id": "page-wait-2",
                             "operation": "wait.duration", "payload": {"duration_us": 1_000_000}})
            self.assertEqual((await page.next_json())["type"], "wait.completed")
            self.assertEqual((await page.next_json())["request_id"], orphan_id)
        asyncio.run(scenario())

    def test_failed_reply_transport_remains_pending_for_reloaded_socket(self):
        async def scenario():
            old, _ = await self._connect()
            await old.send({"type": "command", "command_id": "lost-submit",
                            "operation": "program.submit", "payload": {
                                "device_id": "qpu-1", "shots": 2,
                                "circuit_config": json.loads(program_json(2)),
                                "initialization": "prepare"}})
            request_id = (await old.next_json())["request_id"]
            old.fail_reply = True
            await old.send({"type": "command", "command_id": "lost-wait",
                            "operation": "wait.request", "payload": {"request_id": request_id}})
            self.assertEqual((await old.next_json())["type"], "wait.completed")
            await asyncio.wait_for(old.task, 2)

            resumed, _ = await self._connect()
            await resumed.send({"type": "command", "command_id": "list-after-loss",
                                "operation": "request.pending.list", "payload": {}})
            listed = await resumed.next_json()
            self.assertEqual([item["request_id"] for item in listed["requests"]], [request_id])
            await resumed.send({"type": "command", "command_id": "recover-lost",
                                "operation": "wait.request", "payload": {"request_id": request_id}})
            self.assertEqual((await resumed.next_json())["type"], "wait.completed")
            self.assertEqual((await resumed.next_json())["request_id"], request_id)
        asyncio.run(scenario())

    def test_pending_capacity_never_silently_evicts_an_unfinished_request(self):
        async def scenario():
            self.registry._get("w1").max_requests = 1
            socket, _ = await self._connect()
            payload = {"job_id": "own-job"}
            await socket.send({"type": "command", "command_id": "first-status",
                               "operation": "job.status.read", "payload": payload})
            first = await socket.next_json()
            self.assertEqual(first["type"], "accepted")
            await socket.send({"type": "command", "command_id": "second-status",
                               "operation": "job.status.read", "payload": payload})
            self.assertEqual((await socket.next_json())["code"], "HOST_CAPACITY_REACHED")
            await socket.send({"type": "command", "command_id": "capacity-list",
                               "operation": "request.pending.list", "payload": {}})
            self.assertEqual([item["request_id"] for item in (await socket.next_json())["requests"]],
                             [first["request_id"]])
            await socket.send({"type": "command", "command_id": "capacity-wait",
                               "operation": "wait.request", "payload": {"request_id": first["request_id"]}})
            self.assertEqual((await socket.next_json())["type"], "wait.completed")
            self.assertEqual((await socket.next_json())["request_id"], first["request_id"])
            await socket.send({"type": "command", "command_id": "third-status",
                               "operation": "job.status.read", "payload": payload})
            self.assertEqual((await socket.next_json())["type"], "accepted")
        asyncio.run(scenario())

    def test_service_denial_is_released_after_arrival_without_parsing_the_program(self):
        async def scenario():
            owner = WebOwnerContext()
            clock = VirtualHostClock()
            args, host, runner, executor, world = setup(owner_context=owner,
                host_clock_source=clock, live_time_policy=POLICY, attacker_operations=())
            controller = PausedWorldController.from_manifest(world, clock)
            registry = PausedWorldWebSocketRegistry(max_worlds=1)
            registry.register("w-denied", controller)
            app = FastAPI()
            app.include_router(create_live_attack_websocket_router(registry,
                resolve_owner=lambda ws: ws.scope["state"]["test_owner"],
                allowed_origins=(ORIGIN,)))
            socket = DirectWebSocket(app, "/api/attack/live/w-denied/socket", owner=owner)
            self.sockets.append(socket)
            await socket.start()
            await socket.send({"type": "authenticate", "credential": args[4]["a"]})
            self.assertEqual((await socket.next_json())["type"], "ready")
            payload = {"device_id": "qpu-1", "shots": 2,
                       "circuit_config": {"not": "a valid circuit"}, "initialization": "prepare"}
            await socket.send({"type": "command", "command_id": "denied-submit",
                "operation": "program.submit", "payload": payload})
            accepted = await socket.next_json()
            self.assertEqual(accepted["type"], "accepted")
            from core.attack_simulation.program_request import CircuitProgram
            from unittest.mock import patch
            await socket.send({"type": "command", "command_id": "denied-wait",
                "operation": "wait.request", "payload": {"request_id": accepted["request_id"]}})
            with patch.object(CircuitProgram, "from_editor_json",
                              side_effect=AssertionError("denied circuit was parsed")):
                self.assertEqual((await socket.next_json())["type"], "wait.completed")
                reply = await socket.next_json()
            self.assertEqual(reply["error_code"], "OPERATION_DENIED")
            await registry.discard("w-denied")
            world.close()
        asyncio.run(scenario())

    def test_owner_credential_and_absent_world_fail_with_identical_handshake(self):
        async def scenario():
            attempts = [
                await self._connect(owner=WebOwnerContext()),
                await self._connect(credential="vqc_" + "x" * 43),
                await self._connect(world_id="absent"),
            ]
            observed = [(response.get("code"), response.get("reason"))
                        for _, response in attempts]
            self.assertEqual(len(set(observed)), 1)
            self.assertEqual(observed[0], (4401, "AUTHENTICATION_FAILED"))
        asyncio.run(scenario())

    def test_origin_mismatch_is_rejected_before_accept(self):
        async def scenario():
            socket, first = await self._connect(origin="https://evil.example")
            self.assertEqual((first.get("type"), first.get("code")), ("websocket.close", 4403))
            self.assertTrue(socket.task.done())
        asyncio.run(scenario())

    def test_connection_limit_is_atomic_for_simultaneous_authentication(self):
        async def scenario():
            self.registry._get("w1").max_connections = 1
            results = await asyncio.gather(self._connect(), self._connect())
            messages = [response for _, response in results]
            self.assertEqual(sum(message.get("type") == "ready" for message in messages), 1)
            self.assertEqual(sum(message.get("code") == "CONNECTION_LIMIT" for message in messages), 1)
        asyncio.run(scenario())

    def test_other_principals_request_is_indistinguishable_from_unknown(self):
        async def scenario():
            attacker, ready = await self._connect()
            self.assertEqual(ready["type"], "ready")
            payload = {"device_id": "qpu-1", "shots": 2,
                       "circuit_config": json.loads(program_json(2)), "initialization": "prepare"}
            await attacker.send({"type": "command", "command_id": "submit-2",
                "operation": "program.submit", "payload": payload})
            ack = await attacker.next_json()
            victim, ready = await self._connect(credential=self.args[4]["v"])
            self.assertEqual(ready["type"], "ready")
            for request_id in (ack["request_id"], "not-a-request"):
                await victim.send({"type": "command", "command_id": "wait-" + request_id,
                    "operation": "wait.request", "payload": {"request_id": request_id}})
                self.assertEqual(await victim.next_json(), {
                    "type": "error", "code": "REQUEST_NOT_FOUND", "command_id": "wait-" + request_id})
            entry = self.registry._get("w1")
            entry.max_requests = 1
            for index in range(4):
                command_id = f"unknown-{index}"
                await victim.send({"type": "command", "command_id": command_id,
                    "operation": "wait.request", "payload": {"request_id": f"missing-{index}"}})
                self.assertEqual(await victim.next_json(), {
                    "type": "error", "code": "REQUEST_NOT_FOUND", "command_id": command_id})
                self.assertLessEqual(len(entry.idempotency), 1)
            self.assertEqual(self.controller.now(), 0.0)
        asyncio.run(scenario())

    def test_oversize_and_malformed_json_are_rejected(self):
        async def scenario():
            bad_json, _ = await self._connect()
            await bad_json.send('{"type":')
            self.assertEqual((await bad_json.next_json())["code"], "INVALID_JSON")
            self.assertEqual((await bad_json.next())["code"], 4400)

            duplicate, _ = await self._connect()
            await duplicate.send('{"type":"authenticate","type":"command"}')
            self.assertEqual((await duplicate.next_json())["code"], "INVALID_JSON")
            self.assertEqual((await duplicate.next())["code"], 4400)

            large, _ = await self._connect()
            await large.send(" " * (MAX_WEBSOCKET_MESSAGE_BYTES + 1))
            self.assertEqual((await large.next_json())["code"], "MESSAGE_TOO_LARGE")
            self.assertEqual((await large.next())["code"], 4409)
            self.assertEqual(self.world._operations, {})
        asyncio.run(scenario())

    def test_host_clock_world_is_rejected_and_discard_closes_connected_socket(self):
        async def scenario():
            from tests.test_attack_live_world import setup as setup_live
            args, host, runner, executor, host_world = setup_live()
            # Registry accepts only the explicit_wait controller, not a host_clock world.
            self.assertEqual(host_world.live_time_policy.mode, "host_clock")
            socket, _ = await self._connect()
            await self.registry.discard("w1")
            close = await socket.next()
            self.assertEqual((close.get("type"), close.get("code")), ("websocket.close", 4404))
            self.assertIsNone(self.registry._get("w1"))
            with self.assertRaises(ValueError):
                # Controller construction itself refuses host_clock; there is no silent fallback.
                PausedWorldController(host_world, VirtualHostClock(), LiveTimePolicy("host_clock"))
            host_world.close()
        asyncio.run(scenario())

    def test_connection_count_is_bounded_per_world(self):
        async def scenario():
            self.registry._max_connections = 1
            self.registry._get("w1").max_connections = 1
            first, ready = await self._connect()
            self.assertEqual(ready["type"], "ready")
            second, accepted = await self._connect()
            self.assertEqual(accepted["code"], "CONNECTION_LIMIT")
            self.assertEqual((await second.next())["code"], 4429)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

"""Live HTTP adapter over ASGI with a test owner resolver; no sockets or real auth."""

import asyncio
import json
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from api.attack_live_driver import LiveDriverSettings, LiveWorldDriver
from api.attack_live_http import LiveWorldRegistry, create_live_attack_router
from api.attack_sessions import WebOwnerContext
from core.attack_simulation.program_request import CircuitProgram
from core.attack_simulation.jobs import JobStatus
from tests.test_attack_http_adapter import _request
from tests.test_attack_sessions import OPERATIONS
from tests.test_attack_live_world import setup
from tests.test_attack_world import at
from tests.test_local_program_runtime import program_json

BASE = "/api/attack/live/w1"


def program(shots=2):
    return {"device_id": "qpu-1", "shots": shots, "circuit_config": json.loads(program_json(2))}


class LiveHttpTests(unittest.TestCase):
    def fixture(self, *, driver=False, owned=True, resolver_owner="same", **kwargs):
        owner = WebOwnerContext() if owned else None
        args, host, runner, executor, world = setup(owner_context=owner, **kwargs)
        live_driver = None
        if driver:
            live_driver = LiveWorldDriver(world, LiveDriverSettings(
                idle_interval_s=0.005, max_transitions_per_tick=64, stop_timeout_s=5.0))
        registry = LiveWorldRegistry(max_worlds=2)
        registry.register("w1", world, live_driver)
        if resolver_owner == "same":
            resolver = (lambda request: owner) if owned else None
        elif resolver_owner == "other":
            resolver = lambda request: WebOwnerContext()
        else:
            resolver = None
        app = FastAPI()
        app.include_router(create_live_attack_router(registry, resolve_owner=resolver))
        if live_driver is not None:
            live_driver.start()
            self.addCleanup(live_driver.stop)
        return args, host, executor, world, registry, app, live_driver

    def call(self, app, method, path, credential, **kwargs):
        return asyncio.run(_request(app, method, path, credential=credential, **kwargs))

    def reply(self, app, credential, request_id):
        return self.call(app, "GET", f"{BASE}/replies/{request_id}", credential)

    def test_submit_status_and_result_replies_are_released_after_pump_without_times(self):
        args, host, executor, world, _, app, _ = self.fixture()
        a = args[4]["a"]
        at(host, 2)
        status, body = self.call(app, "POST", BASE + "/programs", a, payload=program())
        self.assertEqual(status, 202)
        self.assertEqual(set(body), {"request_id"})
        self.assertEqual(len(world._operations), 1)  # Captured on receipt, before any pump.
        self.assertEqual(self.reply(app, a, body["request_id"]),
                         (409, {"detail": {"code": "REPLY_NOT_READY"}}))
        at(host, 1000)
        world.pump(max_transitions=64)
        code, submitted = self.reply(app, a, body["request_id"])
        self.assertEqual(code, 200)
        self.assertIsNone(submitted["error_code"])
        job_id = submitted["value"]["job_id"]
        # Replay of a released reply is served from the bounded exact cache.
        self.assertEqual(self.reply(app, a, body["request_id"]), (200, submitted))

        at(host, 1001)
        status_req = self.call(app, "POST", f"{BASE}/jobs/{job_id}/status", a)[1]["request_id"]
        result_req = self.call(app, "POST", f"{BASE}/jobs/{job_id}/result", a)[1]["request_id"]
        at(host, 3000)
        world.pump(max_transitions=64)
        code, observed = self.reply(app, a, status_req)
        self.assertEqual((code, observed["value"]["status"]), (200, JobStatus.SUCCEEDED.value))
        code, result = self.reply(app, a, result_req)
        self.assertEqual(code, 200)
        self.assertEqual(sum(result["value"]["result"]["counts"].values()), 2)
        text = json.dumps([submitted, observed, result])
        for leaked in ("time", "_ns", "receipt", "tenant", "sequence", "position"):
            self.assertNotIn(leaked, text)

    def test_wrong_owner_absent_world_and_bad_credential_look_the_same_and_capture_nothing(self):
        args, host, executor, world, _, app, _ = self.fixture(resolver_owner="other")
        unauth = (401, {"detail": {"code": "UNAUTHENTICATED"}})
        self.assertEqual(self.call(app, "POST", BASE + "/programs", args[4]["a"], payload=program()), unauth)
        self.assertEqual(self.call(app, "POST", "/api/attack/live/absent/programs", args[4]["a"],
                                   payload=program()), unauth)
        args2, _, _, _, _, app2, _ = self.fixture()
        self.assertEqual(self.call(app2, "POST", BASE + "/programs", "vqc_" + "x" * 43,
                                   payload=program()), unauth)
        self.assertEqual(world._operations, {})

    def test_owned_world_without_resolver_is_unavailable(self):
        args, host, executor, world, _, app, _ = self.fixture(resolver_owner=None)
        self.assertEqual(self.call(app, "POST", BASE + "/programs", args[4]["a"], payload=program())[0], 401)
        self.assertEqual(world._operations, {})

    def test_another_principals_request_id_is_indistinguishable_from_unknown(self):
        args, host, executor, world, _, app, _ = self.fixture()
        at(host, 2)
        request_id = self.call(app, "POST", BASE + "/programs", args[4]["a"], payload=program())[1]["request_id"]
        at(host, 1000)
        world.pump(max_transitions=64)
        missing = (404, {"detail": {"code": "REQUEST_NOT_FOUND"}})
        self.assertEqual(self.reply(app, args[4]["v"], request_id), missing)
        self.assertEqual(self.reply(app, args[4]["v"], "0" * 32), missing)
        self.assertEqual(self.reply(app, args[4]["a"], request_id)[0], 200)

    def test_idempotency_key_returns_same_request_and_rejects_changed_payload(self):
        args, host, executor, world, _, app, _ = self.fixture()
        at(host, 2)
        headers = [(b"idempotency-key", b"same-key")]
        first = self.call(app, "POST", BASE + "/programs", args[4]["a"], payload=program(), headers=headers)
        again = self.call(app, "POST", BASE + "/programs", args[4]["a"], payload=program(), headers=headers)
        self.assertEqual(first, again)
        self.assertEqual(len(world._operations), 1)
        self.assertEqual(self.call(app, "POST", BASE + "/programs", args[4]["a"], payload=program(shots=3),
                                   headers=headers), (422, {"detail": {"code": "IDEMPOTENCY_CONFLICT"}}))
        self.assertEqual(len(world._operations), 1)

    def test_malformed_and_oversized_bodies_are_rejected_before_capture(self):
        args, host, executor, world, _, app, _ = self.fixture()
        invalid = (422, {"detail": {"code": "INVALID_REQUEST"}})
        self.assertEqual(self.call(app, "POST", BASE + "/programs", args[4]["a"],
                                   payload={**program(), "tenant_id": "victim"}), invalid)
        self.assertEqual(self.call(app, "POST", BASE + "/jobs/j/status", args[4]["a"], raw=b"{}"), invalid)
        self.assertEqual(self.call(app, "POST", BASE + "/programs", args[4]["a"],
                                   raw=b" " * (64 * 1024 + 1))[0], 413)
        self.assertEqual(world._operations, {})

    def test_circuit_parsing_happens_at_arrival_not_before_receipt_sample(self):
        args, host, executor, world, _, app, _ = self.fixture()
        original = CircuitProgram.from_editor_json.__func__
        parsed_at = []
        def slow_parse(cls, text):
            parsed_at.append(host.value)
            host.value += 50_000_000  # 50 ms of host parsing must not move the arrival.
            return original(cls, text)
        at(host, 2)
        with patch.object(CircuitProgram, "from_editor_json", classmethod(slow_parse)):
            request_id = self.call(app, "POST", BASE + "/programs", args[4]["a"],
                                   payload=program())[1]["request_id"]
            self.assertEqual(parsed_at, [])  # Captured before any Program parsing.
            (op,) = world._operations.values()
            arrival = op.reservation.position
            at(host, 1000)
            world.pump(max_transitions=64)
        self.assertEqual(len(parsed_at), 1)
        code, body = self.reply(app, args[4]["a"], request_id)
        self.assertEqual(code, 200)
        self.assertEqual(world._arrivals[body["value"]["job_id"]], arrival)

    def test_invalid_circuit_is_captured_then_a_declared_denial(self):
        args, host, executor, world, _, app, _ = self.fixture()
        at(host, 2)
        request_id = self.call(app, "POST", BASE + "/programs", args[4]["a"],
                               payload={**program(), "circuit_config": {"bad": 1}})[1]["request_id"]
        self.assertEqual(len(world._operations), 1)
        at(host, 1000)
        world.pump(max_transitions=64)
        self.assertEqual(self.reply(app, args[4]["a"], request_id),
                         (200, {"value": {"reason": "unknown circuit configuration field"},
                                 "error_code": "INVALID_PROGRAM"}))
        self.assertEqual(executor.runtime.scheduler.queued_job_ids(), ())

    def test_ungranted_principal_gets_denial_not_content_validation(self):
        args, host, executor, world, _, app, _ = self.fixture(attacker_operations=())
        at(host, 2)
        invalid = self.call(app, "POST", BASE + "/programs", args[4]["a"],
                            payload={**program(), "circuit_config": {"bad": 1}})[1]["request_id"]
        valid = self.call(app, "POST", BASE + "/programs", args[4]["a"], payload=program())[1]["request_id"]
        at(host, 1000)
        with patch.object(CircuitProgram, "from_editor_json",
                          side_effect=AssertionError("unauthorized circuit was parsed")):
            world.pump(max_transitions=64)
        denied = (200, {"value": None, "error_code": "OPERATION_DENIED"})
        self.assertEqual(self.reply(app, args[4]["a"], invalid), denied)
        self.assertEqual(self.reply(app, args[4]["a"], valid), denied)

    def test_inherit_grant_is_checked_before_circuit_parsing(self):
        args, host, executor, world, _, app, _ = self.fixture(
            initializations=("prepare", "inherit"), attacker_operations=OPERATIONS)
        at(host, 2)
        request_id = self.call(app, "POST", BASE + "/programs", args[4]["a"],
            payload={**program(), "initialization": "inherit"})[1]["request_id"]
        at(host, 1000)
        with patch.object(CircuitProgram, "from_editor_json",
                          side_effect=AssertionError("ungranted inherited circuit was parsed")):
            world.pump(max_transitions=64)
        self.assertEqual(self.reply(app, args[4]["a"], request_id),
                         (200, {"value": None, "error_code": "OPERATION_DENIED"}))

    def test_service_limit_violation_is_a_released_declared_denial(self):
        args, host, executor, world, _, app, _ = self.fixture()
        at(host, 2)
        request_id = self.call(app, "POST", BASE + "/programs", args[4]["a"],
                               payload=program(shots=33))[1]["request_id"]
        at(host, 1000)
        world.pump(max_transitions=64)
        self.assertEqual(self.reply(app, args[4]["a"], request_id),
                         (200, {"value": {"reason": "shots exceed the service limit: 33 > 32"},
                                "error_code": "INVALID_PROGRAM"}))

    def test_background_driver_releases_reply_without_manual_pump(self):
        args, host, executor, world, _, app, driver = self.fixture(driver=True)
        at(host, 2)
        request_id = self.call(app, "POST", BASE + "/programs", args[4]["a"], payload=program())[1]["request_id"]
        at(host, 1000)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            code, body = self.reply(app, args[4]["a"], request_id)
            if code == 200:
                break
            time.sleep(0.005)
        self.assertEqual(code, 200)
        self.assertIs(executor.runtime.scheduler.get(body["value"]["job_id"]).status, JobStatus.SUCCEEDED)

    def test_discard_stops_driver_closes_world_and_removes_route_target(self):
        args, host, executor, world, registry, app, driver = self.fixture(driver=True)
        at(host, 2)
        request_id = self.call(app, "POST", BASE + "/programs", args[4]["a"], payload=program())[1]["request_id"]
        self.assertTrue(registry.discard("w1"))
        self.assertTrue(world._closed)
        self.assertFalse(driver._thread.is_alive())
        self.assertEqual(self.reply(app, args[4]["a"], request_id)[0], 401)
        with self.assertRaises(LookupError):
            registry.discard("w1")

    def test_registry_is_bounded_and_rejects_duplicates(self):
        with self.assertRaises(ValueError):
            LiveWorldRegistry(max_worlds=0)
        args, host, executor, world, registry, app, _ = self.fixture()
        with self.assertRaises(ValueError):
            registry.register("w2", world)
        with self.assertRaises(ValueError):
            registry.register("bad id", setup()[4])


if __name__ == "__main__":
    unittest.main()

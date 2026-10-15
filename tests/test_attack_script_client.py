"""attack_client runs only in the browser script runner; no local-Python access path exists."""

import json
import sys
import types
import unittest
from unittest.mock import patch

from attack_client import AttackError, AttackSession, Circuit


class FakeBridge:
    """Stands in for the Worker's _attack_bridge: JSON in, JSON out, like the real one."""

    def __init__(self):
        self.calls = []
        self.results = {}
        self.pending = {}
        self.count = 0

    def call(self, method, arguments):
        args = json.loads(arguments)
        self.calls.append((method, args))
        if method == "open":
            return json.dumps({"value": {"environment_id": "env-1", "world_time_us": 5}})
        if method == "answer":
            return json.dumps({"value": {"attempt": 1, "attempts_remaining": 2, "passed": False}})
        if method == "close":
            return json.dumps({"value": {}})
        if method == "issue":
            self.count += 1
            request_id = f"req-{self.count}"
            self.pending[request_id] = (args["operation"], {**args["payload"], "_n": self.count})
            return json.dumps({"value": {"request_id": request_id}})
        if method == "reply":
            operation, payload = self.pending.pop(args["request_id"])
            return self.respond(operation, payload)
        return self.respond(args["operation"], args["payload"])

    def respond(self, operation, payload):
        if operation == "program.submit":
            if payload.get("interleave") and payload.get("shots", 0) > 32:
                return json.dumps({"value": {"value": {"reason": "observation budget"},
                                             "error_code": "INVALID_PROGRAM", "world_time_us": 9}})
            return json.dumps({"value": {"value": {"job_id": f"job-{payload.get('_n', self.count)}", "status": "queued"},
                                         "error_code": None, "world_time_us": 7}})
        if operation == "job.victim_activity.read":
            polls = self.results.setdefault(("activity", payload["job_id"]), 0)
            self.results[("activity", payload["job_id"])] = polls + 1
            if polls == 0:
                return json.dumps({"value": {"value": None, "error_code": "RESULT_NOT_READY"}})
            if payload["job_id"] == "job-off":
                return json.dumps({"value": {"value": None, "error_code": "OPERATION_DENIED"}})
            return json.dumps({"value": {"value": {"job_id": payload["job_id"], "reference": "victim",
                                                   "victim_prepared_bit": 1, "shots_total": 1, "shots_shown": 1,
                                                   "shots": [{"index": 0, "steps": []}]}, "error_code": None}})
        polls = self.results.setdefault(payload["job_id"], 0)
        self.results[payload["job_id"]] = polls + 1
        if polls == 0:
            return json.dumps({"value": {"value": None, "error_code": "RESULT_NOT_READY"}})
        return json.dumps({"value": {"value": {"status": "succeeded",
                                               "result": {"shots": 4, "counts": {"0": 1, "1": 3}}},
                                     "error_code": None}})

    def sleep(self, milliseconds):
        return False


def browser(bridge):
    module = types.ModuleType("_attack_bridge")
    module.call, module.sleep = bridge.call, bridge.sleep
    return patch.multiple(sys, platform="emscripten"), patch.dict(sys.modules, {"_attack_bridge": module})


class BrowserOnlySdkTests(unittest.TestCase):
    def test_outside_the_browser_runner_a_session_cannot_be_opened(self):
        with self.assertRaises(AttackError) as raised:
            AttackSession()
        self.assertEqual(raised.exception.code, "NO_BRIDGE")
        with self.assertRaises(TypeError):
            AttackSession("http://127.0.0.1:8001", "env", "token", "credential")  # The old local form.

    def test_run_result_and_answer_go_through_the_bridge(self):
        bridge = FakeBridge()
        platform, modules = browser(bridge)
        with platform, modules, AttackSession() as session:
            self.assertEqual((session.environment_id, session.world_time_us), ("env-1", 5))
            job = session.run(Circuit(1).measure(0, 0), shots=4, interleave="ref:1")
            result = job.result(timeout=5, poll=0)
            outcome = session.answer(message="guess")
        method, args = bridge.calls[1]
        self.assertEqual((method, args["operation"]), ("issue", "program.submit"))
        self.assertEqual((args["payload"]["interleave"], args["payload"]["initialization"]), ("training:1", "inherit"))
        self.assertEqual((result.counts, result.ones(0)), ({"0": 1, "1": 3}, 3))
        self.assertEqual(outcome, {"attempt": 1, "attempts_remaining": 2, "passed": False})
        self.assertEqual(("answer", {"message": "guess"}), bridge.calls[-2])
        self.assertEqual(bridge.calls[-1][0], "close")

    def test_victim_activity_waits_for_the_result_and_reports_a_denial(self):
        platform, modules = browser(FakeBridge())
        with platform, modules, AttackSession() as session:
            job = session.run(Circuit(1).measure(0, 0), shots=4)
            activity = job.victim_activity(timeout=5, poll=0)
            self.assertEqual((activity["reference"], activity["victim_prepared_bit"]), ("victim", 1))
            job._job_id = "job-off"  # The service answers OPERATION_DENIED when the command is off.
            with self.assertRaises(AttackError) as raised:
                job.victim_activity(timeout=5, poll=0)
            self.assertEqual(raised.exception.code, "OPERATION_DENIED")

    def test_runs_are_sent_before_any_submit_reply_is_awaited(self):
        bridge = FakeBridge()
        platform, modules = browser(bridge)
        with platform, modules, AttackSession() as session:
            jobs = [session.run(Circuit(1).measure(0, 0), shots=4) for _ in range(3)]
            self.assertEqual([m for m, _ in bridge.calls[1:]], ["issue"] * 3)
            self.assertEqual([job.job_id for job in jobs], ["job-1", "job-2", "job-3"])
            self.assertEqual(bridge.calls[4][0], "reply")

    def test_a_refused_program_raises_with_the_service_reason(self):
        platform, modules = browser(FakeBridge())
        with platform, modules, AttackSession() as session:
            job = session.run(Circuit(1).measure(0, 0), shots=64, interleave="other")  # Not refused yet.
            with self.assertRaises(AttackError) as raised:
                job.result(timeout=5, poll=0)
            with self.assertRaises(ValueError):
                session.run(Circuit(1).measure(0, 0), interleave="custom-victim")
            with self.assertRaises(ValueError):
                session.answer(0.1, "ge", message="both")
        self.assertEqual(raised.exception.code, "INVALID_PROGRAM")
        self.assertIsNotNone(job._submit_request_id)  # The refusal does not invent a job ID.
        self.assertIn("observation budget", str(raised.exception))


class NoLocalAccessPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_server_offers_no_script_token_and_ignores_bearer_headers(self):
        from fastapi import FastAPI
        from api.attack_product_runtime import build_attack_simulator_runtime
        from api.attack_web_ownership import WebOwnershipSettings
        from tests.test_attack_web_ownership import DirectHttp

        runtime = build_attack_simulator_runtime(WebOwnershipSettings.local_development())
        app = FastAPI()
        app.include_router(runtime.router)
        http = DirectHttp(app)
        status, headers, body = await http.request(
            "POST", "/api/attack/environments",
            body={"mode": "free", "idle_noise_mode": "calibration_values", "settings": {}},
            content_type="application/json")
        self.assertEqual(status, 201, body)
        environment_id, cookie = body["environment_id"], headers["set-cookie"].split(";", 1)[0]
        status, _, _ = await http.request("POST", f"/api/attack/environments/{environment_id}/script-token",
                                          cookie=cookie)
        self.assertIn(status, (404, 405))
        await http.request("DELETE", f"/api/attack/environments/{environment_id}", cookie=cookie)


if __name__ == "__main__":
    unittest.main()

"""A Python script drives the real service over WebSocket/HTTP with a script token."""

from dataclasses import replace
import socket
import threading
import time
import unittest

from fastapi import FastAPI
import requests
import uvicorn

from api.attack_product_runtime import build_attack_simulator_runtime
from api.attack_web_ownership import WebOwnershipSettings
from attack_client import AttackError, AttackSession, Circuit

CSRF = {"X-Attack-Simulator-Request": "1"}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ServerFixture:
    def __init__(self, script_access=True):
        self.port = free_port()
        settings = replace(WebOwnershipSettings.local_development(api_port=self.port), script_access=script_access)
        self.runtime = build_attack_simulator_runtime(settings)
        app = FastAPI()
        app.include_router(self.runtime.router)
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        self.base = f"http://127.0.0.1:{self.port}"
        self.http = requests.Session()
        self.headers = {"Origin": self.base, **CSRF}

    def create(self, body):
        response = self.http.post(f"{self.base}/api/attack/environments", json=body, headers=self.headers)
        assert response.status_code == 201, response.text
        return response.json()

    def token(self, environment_id):
        response = self.http.post(f"{self.base}/api/attack/environments/{environment_id}/script-token",
                                  headers=self.headers)
        return response

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


class ScriptClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ServerFixture()

    @classmethod
    def tearDownClass(cls):
        cls.fixture.close()

    def test_a_script_runs_a_circuit_with_a_token_and_collects_counts(self):
        f = self.fixture
        created = f.create({"mode": "free", "idle_noise_mode": "calibration_values", "settings": {}})
        token = f.token(created["environment_id"]).json()["script_token"]
        with AttackSession(f.base, created["environment_id"], token, created["attacker_credential"]) as session:
            # Several jobs are submitted first and collected afterwards.
            jobs = [session.run(Circuit(2).x(0).measure(0, 0), shots=16, initialization="prepare") for _ in range(2)]
            results = [job.result(timeout=60) for job in jobs]
        for result in results:
            self.assertEqual(sum(result.counts.values()), 16)
            self.assertGreater(result.probability_of_one(0), 0.6)  # X then measure: mostly 1
        f.http.delete(f"{f.base}/api/attack/environments/{created['environment_id']}", headers=f.headers)

    def test_the_token_is_per_environment_and_the_wrong_one_is_refused(self):
        f = self.fixture
        one = f.create({"mode": "free", "idle_noise_mode": "calibration_values", "settings": {}})
        two = f.create({"mode": "free", "idle_noise_mode": "calibration_values", "settings": {}})
        token_one = f.token(one["environment_id"]).json()["script_token"]
        with self.assertRaises(AttackError):
            AttackSession(f.base, two["environment_id"], token_one, two["attacker_credential"])
        with self.assertRaises(AttackError):
            AttackSession(f.base, one["environment_id"], "x" * 43, one["attacker_credential"])
        # Re-issuing replaces the earlier token.
        token_new = f.token(one["environment_id"]).json()["script_token"]
        with self.assertRaises(AttackError):
            AttackSession(f.base, one["environment_id"], token_one, one["attacker_credential"])
        AttackSession(f.base, one["environment_id"], token_new, one["attacker_credential"]).close()
        # Issuing needs the owner cookie and the CSRF header: a stranger gets nothing.
        stranger = requests.post(f"{f.base}/api/attack/environments/{one['environment_id']}/script-token",
                                 headers={"Origin": f.base, **CSRF})
        self.assertEqual(stranger.status_code, 404)
        for item in (one, two):
            f.http.delete(f"{f.base}/api/attack/environments/{item['environment_id']}", headers=f.headers)

    def test_the_challenge_can_be_answered_by_script_and_returns_only_aggregates(self):
        f = self.fixture
        created = f.create({"mode": "challenge", "preset_id": "reset-leakage", "settings": {}})
        token = f.token(created["environment_id"]).json()["script_token"]
        with AttackSession(f.base, created["environment_id"], token, created["attacker_credential"]) as session:
            outcome = session.answer(0.1, "ge")
        self.assertEqual(set(outcome), {"attempt", "attempts_remaining", "correct", "total", "required", "passed"})
        self.assertEqual(outcome["attempts_remaining"], 2)
        f.http.delete(f"{f.base}/api/attack/environments/{created['environment_id']}", headers=f.headers)

    def test_the_message_challenge_takes_a_message_and_returns_only_pass_or_fail(self):
        f = self.fixture
        created = f.create({"mode": "challenge", "preset_id": "reset-leakage-message", "settings": {}})
        token = f.token(created["environment_id"]).json()["script_token"]
        with AttackSession(f.base, created["environment_id"], token, created["attacker_credential"]) as session:
            with self.assertRaises(ValueError):
                session.answer(0.1, "ge", message="x")  # Never sent: one kind of answer per call.
            outcome = session.answer(message="not the message")
            with self.assertRaises(AttackError):
                session.answer(0.1, "ge")  # Wrong shape for this challenge: refused, no attempt used.
            second = session.answer(message="still not")
        self.assertEqual(outcome, {"attempt": 1, "attempts_remaining": 2, "passed": False})
        self.assertEqual(second["attempt"], 2)
        f.http.delete(f"{f.base}/api/attack/environments/{created['environment_id']}", headers=f.headers)


class SampleAttackTests(unittest.TestCase):
    def test_the_sample_attack_runs_end_to_end_on_the_challenge(self):
        from examples.reset_leakage_attack import run_attack
        f = ServerFixture()
        try:
            created = f.create({"mode": "challenge", "preset_id": "reset-leakage", "settings": {}})
            token = f.token(created["environment_id"]).json()["script_token"]
            with AttackSession(f.base, created["environment_id"], token, created["attacker_credential"]) as session:
                report = run_attack(session, runs=3, submit=True)
            self.assertEqual(len(report["ref0"]), 3)
            self.assertGreater(sum(report["ref1"]), sum(report["ref0"]))
            self.assertIn(report["guess"], (0, 1))
            self.assertEqual(report["submission"]["total"], 20)
        finally:
            f.close()

    def test_the_abstract_command_is_refused_in_free_mode_unless_enabled(self):
        f = ServerFixture()
        try:
            off = f.create({"mode": "free", "idle_noise_mode": "calibration_values", "settings": {}})
            token = f.token(off["environment_id"]).json()["script_token"]
            with AttackSession(f.base, off["environment_id"], token, off["attacker_credential"]) as session:
                with self.assertRaises(AttackError):
                    session.run(Circuit(1).measure(0, 0), shots=4, interleave="ref:0")
                # An ordinary job still works.
                self.assertEqual(sum(session.run(Circuit(1).measure(0, 0), shots=4).result().counts.values()), 4)
            on = f.create({"mode": "free", "idle_noise_mode": "calibration_values", "settings": {
                "qpu_service.abstract_commands.interleaved_observation": True}})
            token = f.token(on["environment_id"]).json()["script_token"]
            with AttackSession(f.base, on["environment_id"], token, on["attacker_credential"]) as session:
                counts = session.run(Circuit(1).measure(0, 0), shots=4, interleave="ref:1").result().counts
                self.assertEqual(sum(counts.values()), 4)
        finally:
            f.close()


class ScriptAccessDisabledTests(unittest.TestCase):
    def test_without_script_access_no_token_is_issued_and_bearer_is_refused(self):
        f = ServerFixture(script_access=False)
        try:
            created = f.create({"mode": "free", "idle_noise_mode": "calibration_values", "settings": {}})
            self.assertEqual(f.token(created["environment_id"]).status_code, 404)
            with self.assertRaises(AttackError):
                AttackSession(f.base, created["environment_id"], "y" * 43, created["attacker_credential"])
            response = requests.post(f"{f.base}/api/attack/environments/{created['environment_id']}/submission",
                                     json={"threshold": 0.1, "direction": "ge"},
                                     headers={"Authorization": "Bearer " + "y" * 43})
            self.assertEqual(response.status_code, 404)
        finally:
            f.close()

    def test_public_and_generic_settings_never_enable_script_access(self):
        public = WebOwnershipSettings.public(allowed_origins=("https://demo.example.dev",), allowed_hosts=("api.example.dev",))
        self.assertFalse(public.script_access)
        self.assertFalse(WebOwnershipSettings(allowed_origins=("http://x.example",), allowed_hosts=("x.example",)).script_access)
        self.assertTrue(WebOwnershipSettings.local_development().script_access)
        self.assertTrue(WebOwnershipSettings.desktop("127.0.0.1", 8765).script_access)


if __name__ == "__main__":
    unittest.main()

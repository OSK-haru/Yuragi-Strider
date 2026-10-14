"""The internet-facing profile: explicit opt-in, HTTPS only, small and rate limited."""

import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from api.attack_web_ownership import (
    PUBLIC_ABSOLUTE_TIMEOUT_SECONDS, PUBLIC_MAX_ENVIRONMENTS_TOTAL, WebOwnershipSettings,
)
from api.main import _worker_count, configure_attack_simulator
from tests.test_attack_web_ownership import DirectHttp

ORIGIN = "https://demo.example.workers.dev"
HOST = "api.example.onrender.com"
# Behind the Worker proxy the request Host is the API host while the browser Origin
# is the Worker's, and the ownership check requires both host names to be allowed.
HOSTS = (HOST, "demo.example.workers.dev")
PUBLIC_ENV = {
    "ATTACK_SIMULATOR_PROFILE": "public",
    "ATTACK_SIMULATOR_ALLOWED_ORIGINS": ORIGIN,
    "ATTACK_SIMULATOR_ALLOWED_HOSTS": ",".join(HOSTS),
}


def public_settings(**overrides):
    return WebOwnershipSettings.public(allowed_origins=(ORIGIN,), allowed_hosts=HOSTS, **overrides)


class PublicSettingsTests(unittest.TestCase):
    def test_defaults_are_small_and_https_only(self):
        settings = public_settings()
        self.assertEqual(settings.max_environments_per_browser, 1)
        self.assertEqual(settings.max_environments_total, PUBLIC_MAX_ENVIRONMENTS_TOTAL)
        self.assertEqual(settings.absolute_timeout_seconds, PUBLIC_ABSOLUTE_TIMEOUT_SECONDS)
        self.assertGreater(settings.creates_per_minute, 0)
        self.assertGreater(settings.max_concurrent_evaluations, 0)

    def test_unsafe_requests_are_rejected_before_any_state_exists(self):
        with self.assertRaises(ValueError):
            WebOwnershipSettings.public(allowed_origins=("http://demo.example.com",), allowed_hosts=(HOST,))
        with self.assertRaises(ValueError):
            WebOwnershipSettings.public(allowed_origins=(), allowed_hosts=(HOST,))
        with self.assertRaises(ValueError):
            public_settings(max_environments_total=9)
        with self.assertRaises(ValueError):
            public_settings(absolute_timeout_seconds=24 * 3600)

    def test_environment_selects_public_only_when_named_explicitly(self):
        with patch.dict(os.environ, PUBLIC_ENV, clear=False):
            settings = WebOwnershipSettings.from_environment()
        self.assertEqual(settings.allowed_origins, (ORIGIN,))
        self.assertEqual(settings.max_environments_per_browser, 1)
        # Setting origins alone never publishes anything.
        env = {key: value for key, value in PUBLIC_ENV.items() if key != "ATTACK_SIMULATOR_PROFILE"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ATTACK_SIMULATOR_PROFILE", None)
            application = FastAPI()
            self.assertFalse(configure_attack_simulator(profile="generic", target_app=application))
        with patch.dict(os.environ, {**PUBLIC_ENV, "ATTACK_SIMULATOR_ALLOWED_ORIGINS": "http://x.example"}):
            with self.assertRaises(ValueError):
                WebOwnershipSettings.from_environment()


class PublicMountTests(unittest.TestCase):
    def test_public_mounts_with_checked_settings(self):
        application = FastAPI()
        self.assertTrue(configure_attack_simulator(
            profile="public", ownership_settings=public_settings(), target_app=application))
        self.assertEqual(application.state.attack_simulator_profile, "public")

    def test_public_refuses_weak_or_missing_settings(self):
        weak = WebOwnershipSettings(allowed_origins=(ORIGIN,), allowed_hosts=HOSTS)
        for settings in (weak, None):
            application = FastAPI()
            with patch.dict(os.environ, {"ATTACK_SIMULATOR_PROFILE": "generic"}):
                self.assertFalse(configure_attack_simulator(
                    profile="public", ownership_settings=settings, target_app=application))
            self.assertFalse(application.state.attack_simulator_enabled)

    def test_public_needs_exactly_one_worker(self):
        for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
            with patch.dict(os.environ, {name: "2"}):
                self.assertEqual(_worker_count(), 2)
                application = FastAPI()
                self.assertFalse(configure_attack_simulator(
                    profile="public", ownership_settings=public_settings(), target_app=application))
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "many"}):
            self.assertGreater(_worker_count(), 1)
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "1"}):
            self.assertEqual(_worker_count(), 1)


class PublicAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_environment_creation_is_rate_limited_globally(self):
        application = FastAPI()
        settings = public_settings(max_environments_total=8)  # Capacity must not hide the rate limit.
        self.assertTrue(configure_attack_simulator(
            profile="public", ownership_settings=settings, target_app=application))
        http = DirectHttp(application)
        request = {"mode": "free", "idle_noise_mode": "calibration_values", "settings": {}}
        statuses = []
        for _ in range(settings.creates_per_minute + 2):
            status, headers, body = await http.request(
                "POST", "/api/attack/environments", origin=ORIGIN, host=HOST,
                body=request, content_type="application/json")
            statuses.append((status, body.get("detail", {}).get("code") if isinstance(body, dict) else None))
            headers_seen = headers
        self.assertEqual(statuses[:settings.creates_per_minute], [(201, None)] * settings.creates_per_minute)
        self.assertIn((429, "CREATE_RATE_LIMIT"), statuses)
        self.assertEqual(headers_seen.get("retry-after"), "30")
        # Capacity and per-browser limits answer first or alongside; the rate limit is never skipped.
        self.assertTrue(all(code in {None, "CREATE_RATE_LIMIT", "ENVIRONMENT_LIMIT", "ENVIRONMENT_CAPACITY"}
                            for _, code in statuses))
        runtime = application.state.attack_simulator_runtime
        for environment_id in list(runtime.owner_manager._records):
            await runtime.owner_manager._discard_registered(environment_id)

    async def test_only_one_evaluation_runs_at_a_time(self):
        application = FastAPI()
        configure_attack_simulator(profile="public", ownership_settings=public_settings(), target_app=application)
        manager = application.state.attack_simulator_runtime.owner_manager
        http = DirectHttp(application)
        status, headers, body = await http.request(
            "POST", "/api/attack/environments", origin=ORIGIN, host=HOST,
            body={"mode": "challenge", "preset_id": "reset-leakage", "settings": {}},
            content_type="application/json")
        self.assertEqual(status, 201, body)
        cookie = headers["set-cookie"].split(";", 1)[0]
        path = f"/api/attack/environments/{body['environment_id']}/submission"
        manager._evaluations_running = 1  # Another evaluation is already running.
        status, _, busy = await http.request(
            "POST", path, origin=ORIGIN, host=HOST, cookie=cookie,
            body={"threshold": 0.1, "direction": "ge"}, content_type="application/json")
        self.assertEqual((status, busy["detail"]["code"]), (503, "EVALUATION_BUSY"))
        manager._evaluations_running = 0
        status, _, ok = await http.request(
            "POST", path, origin=ORIGIN, host=HOST, cookie=cookie,
            body={"threshold": 0.1, "direction": "ge"}, content_type="application/json")
        self.assertEqual(status, 200, ok)
        self.assertEqual(manager._evaluations_running, 0)
        await manager._discard_registered(body["environment_id"])


if __name__ == "__main__":
    unittest.main()

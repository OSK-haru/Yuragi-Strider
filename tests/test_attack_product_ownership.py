"""ASGI lifecycle integration from D62 product assembly through D61 owner."""

import asyncio
import json
from http.cookies import SimpleCookie
import time
import unittest
from unittest.mock import patch

from api.attack_live_websocket import (
    PausedWorldWebSocketRegistry, create_live_attack_websocket_router,
)
from api.attack_product_factory import ProductEnvironmentFactory
from api.attack_web_ownership import (
    BrowserEnvironmentManager, WebOwnershipSettings, create_browser_environment_router,
)
from core.attack_simulation.scenario_policy import ScenarioMode
from core.attack_simulation.virtual_credentials import VirtualCredentialAuthority
from tests.test_attack_product_factory import HOST_LIMITS, prepared_environment
from tests.test_attack_web_ownership import DirectHttp, DirectWebSocket, ORIGIN


class ProductOwnershipIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.settings = WebOwnershipSettings(
            allowed_origins=(ORIGIN,), allowed_hosts=("127.0.0.1",),
            max_environments_per_browser=1, max_environments_total=2,
            idle_timeout_seconds=10, absolute_timeout_seconds=20,
        )
        self.registry = PausedWorldWebSocketRegistry(max_worlds=2)
        self.product_factory = ProductEnvironmentFactory(HOST_LIMITS)
        self.now = 100.0
        self.clock = lambda: self.now
        self.sockets = []

    def _app(self, mode, *, factory_override=None):
        selected = prepared_environment(mode)
        build = factory_override or (lambda owner: self.product_factory.assemble(selected, owner))
        manager = BrowserEnvironmentManager(
            self.registry, environment_factory=build, settings=self.settings, clock=self.clock,
        )
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(create_browser_environment_router(manager))
        app.include_router(create_live_attack_websocket_router(
            self.registry, resolve_owner=manager.resolve_owner,
            allowed_origins=self.settings.allowed_origins,
        ))
        return app, manager

    @staticmethod
    def _cookie(headers):
        parsed = SimpleCookie()
        parsed.load(headers["set-cookie"])
        morsel = next(iter(parsed.values()))
        return f"{morsel.key}={morsel.value}"

    async def _cleanup(self):
        for socket in self.sockets:
            await socket.stop()
        for environment_id in tuple(self.registry._entries):
            await self.registry.discard(environment_id)

    def tearDown(self):
        asyncio.run(self._cleanup())

    def test_free_and_challenge_create_connect_submit_wait_reply_and_delete(self):
        async def scenario():
            for mode in (ScenarioMode.FREE, ScenarioMode.CHALLENGE):
                app, manager = self._app(mode)
                http = DirectHttp(app)
                issued = {}
                original_issue = VirtualCredentialAuthority.issue

                def capture_issue(authority, principal_id):
                    credential = original_issue(authority, principal_id)
                    issued[principal_id] = credential
                    return credential

                with patch.object(VirtualCredentialAuthority, "issue", capture_issue):
                    status, headers, body = await http.request("POST", "/api/attack/environments")
                self.assertEqual(status, 201)
                self.assertEqual(headers["cache-control"], "no-store")
                self.assertEqual(set(body), {"environment_id", "attacker_credential"})
                self.assertIn(body["attacker_credential"], issued.values())
                victim_credentials = set(issued.values()) - {body["attacker_credential"]}
                self.assertTrue(victim_credentials)
                response_text = json.dumps([body, headers], sort_keys=True)
                self.assertTrue(all(secret not in response_text for secret in victim_credentials))

                environment_id = body["environment_id"]
                socket = DirectWebSocket(
                    app, f"/api/attack/live/{environment_id}/socket", self._cookie(headers),
                )
                self.sockets.append(socket)
                self.assertEqual((await socket.start())["type"], "websocket.accept")
                await socket.send_text({
                    "type": "authenticate", "credential": body["attacker_credential"],
                })
                self.assertEqual(json.loads((await socket.next())["text"])["type"], "ready")

                await socket.send_text({
                    "type": "command", "command_id": f"submit-{mode.value}",
                    "operation": "program.submit", "payload": {
                        "device_id": "qpu-1", "shots": 2,
                        "circuit_config": {
                            "logical_qubits": 2, "classical_bits": 2,
                            "initial_states": [0, 0],
                            "columns": [
                                {"step": 0, "gates": [{"type": "H", "targets": [0]}]},
                                {"step": 1, "gates": [{"type": "MEASURE", "targets": [0],
                                                       "classical_targets": [0]}]},
                            ],
                        },
                        "initialization": "prepare",
                    },
                })
                accepted = json.loads((await socket.next())["text"])
                self.assertEqual(accepted["type"], "accepted")
                self.assertEqual(manager._registry._get(environment_id).controller.now(), 0.0)

                await socket.send_text({
                    "type": "command", "command_id": f"wait-{mode.value}",
                    "operation": "wait.duration", "payload": {"duration_us": 10.0},
                })
                wait_done = json.loads((await socket.next())["text"])
                reply = json.loads((await socket.next())["text"])
                self.assertEqual(wait_done["type"], "wait.completed")
                self.assertEqual(reply["type"], "reply")
                self.assertEqual(reply["request_id"], accepted["request_id"])
                self.assertTrue(all(secret not in json.dumps([wait_done, reply])
                                    for secret in victim_credentials))

                deleted = await http.request(
                    "DELETE", f"/api/attack/environments/{environment_id}",
                    cookie=self._cookie(headers),
                )
                self.assertEqual(deleted[0], 204)
                self.assertEqual((await socket.next())["type"], "websocket.close")
                self.assertIsNone(manager._registry._get(environment_id))
                self.assertNotIn(environment_id, manager._records)
                self.assertEqual(manager._global_reservations, 0)
        asyncio.run(scenario())

    def test_reload_recovers_owned_world_without_advancing_time(self):
        async def scenario():
            app, manager = self._app(ScenarioMode.FREE)
            http = DirectHttp(app)
            created = await http.request("POST", "/api/attack/environments")
            self.assertEqual(created[0], 201)
            environment_id = created[2]["environment_id"]
            cookie = self._cookie(created[1])
            original_credential = created[2]["attacker_credential"]
            world = self.registry._get(environment_id).controller

            owned = await http.request("POST", "/api/attack/environments/owned", cookie=cookie)
            self.assertEqual(owned[0], 200)
            self.assertEqual(owned[1]["cache-control"], "no-store")
            self.assertEqual(len(owned[2]["environments"]), 1)
            summary = owned[2]["environments"][0]
            self.assertEqual(set(summary), {"environment_id", "mode", "created_at"})
            self.assertEqual((summary["environment_id"], summary["mode"]), (environment_id, "free"))
            self.assertNotIn(original_credential, json.dumps(owned))

            original_socket = DirectWebSocket(app, f"/api/attack/live/{environment_id}/socket", cookie)
            self.sockets.append(original_socket)
            self.assertEqual((await original_socket.start())["type"], "websocket.accept")
            await original_socket.send_text({"type": "authenticate", "credential": original_credential})
            self.assertEqual(json.loads((await original_socket.next())["text"])["type"], "ready")
            await original_socket.send_text({"type": "command", "command_id": "before-reload",
                                             "operation": "job.status.read", "payload": {"job_id": "unknown-job"}})
            before_reload = json.loads((await original_socket.next())["text"])
            self.assertEqual(before_reload["type"], "accepted")

            foreign = await http.request("POST", "/api/attack/environments/owned")
            self.assertEqual(foreign[2], {"environments": []})
            other = await http.request("POST", "/api/attack/environments")
            self.assertEqual(other[0], 201)
            other_cookie = self._cookie(other[1])
            other_list = await http.request("POST", "/api/attack/environments/owned", cookie=other_cookie)
            self.assertEqual([item["environment_id"] for item in other_list[2]["environments"]],
                             [other[2]["environment_id"]])
            self.assertNotIn(environment_id, json.dumps(other_list))
            self.assertEqual((await http.request(
                "POST", f"/api/attack/environments/{environment_id}/attacker-credential",
                cookie=other_cookie))[0], 404)
            missing = "0" * 32
            for target in (environment_id, missing):
                denied = await http.request("POST", f"/api/attack/environments/{target}/attacker-credential")
                self.assertEqual((denied[0], denied[2]["detail"]["code"]),
                                 (404, "ENVIRONMENT_NOT_FOUND"))

            no_csrf = await http.request("POST", f"/api/attack/environments/{environment_id}/attacker-credential",
                                         cookie=cookie, csrf=False)
            self.assertEqual(no_csrf[0], 403)
            wrong_origin = await http.request("POST", "/api/attack/environments/owned",
                                              cookie=cookie, origin="https://foreign.example")
            self.assertEqual(wrong_origin[0], 403)
            recovered = await http.request("POST", f"/api/attack/environments/{environment_id}/attacker-credential",
                                           cookie=cookie)
            self.assertEqual(recovered[0], 200)
            self.assertEqual(recovered[1]["cache-control"], "no-store")
            self.assertEqual(recovered[2], {"attacker_credential": original_credential})
            self.assertEqual(world.now(), 0.0)
            self.assertFalse(original_socket.task.done(), "credential recovery must not close the old tab")

            socket = DirectWebSocket(app, f"/api/attack/live/{environment_id}/socket", cookie)
            self.sockets.append(socket)
            self.assertEqual((await socket.start())["type"], "websocket.accept")
            await socket.send_text({"type": "authenticate", "credential": recovered[2]["attacker_credential"]})
            self.assertEqual(json.loads((await socket.next())["text"])["world_time_us"], 0.0)
            await socket.send_text({"type": "command", "command_id": "recover-pending",
                                    "operation": "request.pending.list", "payload": {}})
            self.assertEqual(json.loads((await socket.next())["text"]), {
                "type": "pending.list", "command_id": "recover-pending",
                "requests": [{"request_id": before_reload["request_id"], "kind": "status",
                              "job_id": "unknown-job", "issued_at_world_us": 0.0}],
            })
            await socket.send_text({"type": "command", "command_id": "recover-wait",
                                    "operation": "wait.request", "payload": {"request_id": before_reload["request_id"]}})
            self.assertEqual(json.loads((await socket.next())["text"])["type"], "wait.completed")
            self.assertEqual(json.loads((await socket.next())["text"])["request_id"], before_reload["request_id"])
            resumed_at = world.now()
            await socket.send_text({"type": "command", "command_id": "reload-submit", "operation": "program.submit",
                                    "payload": {"device_id": "qpu-1", "shots": 1, "circuit_config": {
                                        "logical_qubits": 2, "classical_bits": 2,
                                        "initial_states": [0, 0],
                                        "columns": [{"step": 0, "gates": [{"type": "MEASURE", "targets": [0],
                                                                              "classical_targets": [0]}]}]},
                                        "initialization": "prepare"}})
            accepted = json.loads((await socket.next())["text"])
            self.assertEqual(accepted["type"], "accepted")
            self.assertEqual(world.now(), resumed_at)
            await socket.send_text({"type": "command", "command_id": "reload-wait", "operation": "wait.duration",
                                    "payload": {"duration_us": 10.0}})
            self.assertEqual(json.loads((await socket.next())["text"])["type"], "wait.completed")
            reply = json.loads((await socket.next())["text"])
            self.assertEqual(reply["request_id"], accepted["request_id"])

            deleted = await http.request("DELETE", f"/api/attack/environments/{environment_id}", cookie=cookie)
            self.assertEqual(deleted[0], 204)
            self.assertEqual((await socket.next())["type"], "websocket.close")
            self.assertEqual((await original_socket.next())["type"], "websocket.close")
            self.assertEqual((await http.request("POST", "/api/attack/environments/owned", cookie=cookie))[2],
                             {"environments": []})
            self.assertEqual((await http.request("POST", f"/api/attack/environments/{environment_id}/attacker-credential",
                                                 cookie=cookie))[0], 404)
        asyncio.run(scenario())

    def test_reload_excludes_expired_environment(self):
        async def scenario():
            app, manager = self._app(ScenarioMode.FREE)
            http = DirectHttp(app)
            created = await http.request("POST", "/api/attack/environments")
            environment_id = created[2]["environment_id"]
            cookie = self._cookie(created[1])
            self.now += self.settings.idle_timeout_seconds
            self.assertEqual((await http.request("POST", "/api/attack/environments/owned", cookie=cookie))[2],
                             {"environments": []})
            self.assertEqual((await http.request("POST", f"/api/attack/environments/{environment_id}/attacker-credential",
                                                 cookie=cookie))[0], 404)
            self.assertNotIn(environment_id, manager._records)
        asyncio.run(scenario())

    def test_browser_and_global_limits_reject_without_replacing_existing_products(self):
        async def scenario():
            app, manager = self._app(ScenarioMode.FREE)
            http = DirectHttp(app)
            first = await http.request("POST", "/api/attack/environments")
            first_id = first[2]["environment_id"]
            first_cookie = self._cookie(first[1])

            browser_limited = await http.request("POST", "/api/attack/environments",
                                                  cookie=first_cookie)
            self.assertEqual((browser_limited[0], browser_limited[2]["detail"]["code"]),
                             (429, "ENVIRONMENT_LIMIT"))
            second = await http.request("POST", "/api/attack/environments")
            self.assertEqual(second[0], 201)
            total_limited = await http.request("POST", "/api/attack/environments")
            self.assertEqual((total_limited[0], total_limited[2]["detail"]["code"]),
                             (503, "ENVIRONMENT_CAPACITY"))
            self.assertEqual(set(manager._records), {first_id, second[2]["environment_id"]})
            self.assertIsNotNone(self.registry._get(first_id))
        asyncio.run(scenario())

    def test_factory_failure_releases_reservation_and_does_not_register_partial_world(self):
        async def scenario():
            selected = prepared_environment(ScenarioMode.FREE)
            calls = 0

            def build(owner):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("injected build failure")
                return self.product_factory.assemble(selected, owner)

            app, manager = self._app(ScenarioMode.FREE, factory_override=build)
            http = DirectHttp(app)
            failed = await http.request("POST", "/api/attack/environments")
            self.assertEqual((failed[0], failed[2]["detail"]["code"]),
                             (503, "ENVIRONMENT_UNAVAILABLE"))
            self.assertEqual((manager._records, manager._reservations,
                              manager._global_reservations, self.registry._entries),
                             ({}, {}, 0, {}))
            retry = await http.request("POST", "/api/attack/environments")
            self.assertEqual(retry[0], 201)
            self.assertEqual(len(manager._records), 1)
        asyncio.run(scenario())

    def test_idle_and_absolute_expiry_close_sockets_and_match_missing_environment(self):
        async def scenario():
            self.settings = WebOwnershipSettings(
                allowed_origins=(ORIGIN,), allowed_hosts=("127.0.0.1",),
                max_environments_per_browser=1, max_environments_total=2,
                idle_timeout_seconds=1, absolute_timeout_seconds=2,
            )
            self.clock = time.monotonic
            for expiry_kind in ("idle", "absolute"):
                app, manager = self._app(ScenarioMode.FREE)
                http = DirectHttp(app)
                status, headers, body = await http.request("POST", "/api/attack/environments")
                self.assertEqual(status, 201)
                environment_id = body["environment_id"]
                socket = DirectWebSocket(
                    app, f"/api/attack/live/{environment_id}/socket", self._cookie(headers),
                )
                self.sockets.append(socket)
                await socket.start()
                await socket.send_text({
                    "type": "authenticate", "credential": body["attacker_credential"],
                })
                self.assertEqual(json.loads((await socket.next())["text"])["type"], "ready")
                recovered = await http.request(
                    "POST", f"/api/attack/environments/{environment_id}/attacker-credential",
                    cookie=self._cookie(headers),
                )
                self.assertEqual(recovered[0], 200)
                second = DirectWebSocket(
                    app, f"/api/attack/live/{environment_id}/socket", self._cookie(headers),
                )
                self.sockets.append(second)
                await second.start()
                await second.send_text({
                    "type": "authenticate", "credential": recovered[2]["attacker_credential"],
                })
                self.assertEqual(json.loads((await second.next())["text"])["type"], "ready")

                if expiry_kind == "absolute":
                    await asyncio.sleep(0.55)
                    await socket.send_text({
                        "type": "command", "command_id": "touch-before-absolute-expiry",
                        "operation": "job.status.read", "payload": {"job_id": "absent-job"},
                    })
                    self.assertEqual(json.loads((await socket.next())["text"])["type"], "accepted")
                closed = await asyncio.wait_for(socket.next(), timeout=2.5)
                self.assertEqual((closed["type"], closed["code"]), ("websocket.close", 4404))
                closed_second = await asyncio.wait_for(second.next(), timeout=2.5)
                self.assertEqual((closed_second["type"], closed_second["code"]), ("websocket.close", 4404))
                async def cleanup_finished():
                    while environment_id in manager._records or self.registry._get(environment_id) is not None:
                        await asyncio.sleep(0.001)
                await asyncio.wait_for(cleanup_finished(), timeout=2.5)
                self.assertNotIn(environment_id, manager._records)
                self.assertIsNone(self.registry._get(environment_id))
                removed = await http.request(
                    "DELETE", f"/api/attack/environments/{environment_id}",
                    cookie=self._cookie(headers),
                )
                absent = await http.request("DELETE", "/api/attack/environments/" + "0" * 32)
                self.assertEqual((removed[0], removed[2]), (absent[0], absent[2]))
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

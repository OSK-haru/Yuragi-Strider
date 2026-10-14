"""Browser ownership and environment lifecycle adapters, exercised via ASGI only."""

import asyncio
import hashlib
import json
from dataclasses import replace
from http.cookies import SimpleCookie
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from api.attack_live_websocket import (
    PausedWorldWebSocketRegistry, create_live_attack_websocket_router,
)
from api.attack_paused_world import PausedWorldController, VirtualHostClock
from api.attack_sessions import WebOwnerContext
from api.attack_web_ownership import (
    BrowserEnvironmentManager, COOKIE_PATH, CSRF_HEADER, CSRF_HEADER_VALUE,
    DEFAULT_ABSOLUTE_TIMEOUT_SECONDS, DEFAULT_IDLE_TIMEOUT_SECONDS,
    RequestOriginHostRejected, WebOwnershipSettings,
    create_browser_environment_router,
)
from api.attack_live_world import LiveWorkloadWorld
from core.attack_simulation.live_time_policy import parse_live_time_policy
from tests.test_attack_live_world import setup


ORIGIN = "http://127.0.0.1:8001"
HOST = "127.0.0.1:8001"
POLICY = {"mode": "explicit_wait", "max_wait_us": 100_000.0, "max_steps_per_wait": 100}


class DirectHttp:
    def __init__(self, app):
        self.app = app

    async def request(self, method, path, *, origin=ORIGIN, host=HOST, cookie="", csrf=True,
                      body=b"", content_type=None):
        headers = [(b"origin", origin.encode()), (b"host", host.encode())]
        if type(body) is dict:
            body = json.dumps(body, separators=(",", ":")).encode("utf-8")
        if type(body) is not bytes:
            raise TypeError("ASGI test request body must be bytes or a JSON object")
        if content_type is not None:
            headers.append((b"content-type", content_type.encode()))
        if body or method.upper() == "POST":
            headers.append((b"content-length", str(len(body)).encode()))
        if cookie:
            headers.append((b"cookie", cookie.encode()))
        if csrf:
            headers.append((CSRF_HEADER.encode(), CSRF_HEADER_VALUE.encode()))
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": method, "scheme": "http", "path": path,
            "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": headers, "client": ("asgi-test", 1), "server": ("127.0.0.1", 8001),
            "state": {},
        }
        sent = []
        body_sent = False

        async def receive():
            nonlocal body_sent
            if body_sent:
                return {"type": "http.disconnect"}
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        await self.app(scope, receive, send)
        start = next(item for item in sent if item["type"] == "http.response.start")
        raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        response_headers = {key.decode().lower(): value.decode() for key, value in start["headers"]}
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = raw
        return start["status"], response_headers, parsed


class DirectWebSocket:
    def __init__(self, app, path, cookie, *, origin=ORIGIN, host=HOST):
        self.app, self.path = app, path
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.scope = {
            "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "scheme": "ws", "path": path, "raw_path": path.encode(),
            "path_params": {"world_id": path.split("/")[-2]}, "query_string": b"",
            "headers": [(b"origin", origin.encode()), (b"host", host.encode()),
                        (b"cookie", cookie.encode())],
            "client": ("asgi-test", 1), "server": ("127.0.0.1", 8001),
            "subprotocols": ["quanyu.attack.v1"], "state": {},
        }
        self.task = None

    async def receive(self):
        return await self.incoming.get()

    async def send(self, message):
        await self.outgoing.put(message)

    async def start(self):
        self.task = asyncio.create_task(self.app(self.scope, self.receive, self.send))
        await self.incoming.put({"type": "websocket.connect"})
        return await asyncio.wait_for(self.outgoing.get(), 2)

    async def next(self):
        return await asyncio.wait_for(self.outgoing.get(), 2)

    async def send_text(self, value):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(value)})

    async def stop(self):
        if self.task is not None and not self.task.done():
            await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(self.task, 2)


class BrowserEnvironmentOwnershipTests(unittest.TestCase):
    def test_discard_keeps_global_slot_until_controller_is_closed(self):
        async def scenario():
            self.manager.settings = replace(self.manager.settings, max_environments_total=1)
            status, headers, body = await self._create()
            self.assertEqual(status, 201)
            environment_id = body["environment_id"]
            name, token, _ = self._cookie_parts(headers)
            entered, release = Event(), Event()
            original_close = self.controllers[0].close
            def slow_close():
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test close was not released")
                original_close()
            with patch.object(self.controllers[0], "close", side_effect=slow_close):
                deleting = asyncio.create_task(self.http.request(
                    "DELETE", f"/api/attack/environments/{environment_id}",
                    cookie=f"{name}={token}"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertIn(environment_id, self.manager._records)
                    self.assertIsNotNone(self.registry._get(environment_id))
                    self.assertEqual((await self._create())[0], 503)
                    self.assertEqual((await self._create(cookie=f"{name}={token}"))[0], 429)
                finally:
                    release.set()
                self.assertEqual((await deleting)[0], 204)
            self.assertNotIn(environment_id, self.manager._records)
            self.assertIsNone(self.registry._get(environment_id))
            self.assertEqual((await self._create())[0], 201)
        asyncio.run(scenario())

    def test_expiry_keeps_slot_until_world_drain_finishes(self):
        async def scenario():
            status, _, body = await self._create()
            self.assertEqual(status, 201)
            environment_id = body["environment_id"]
            entered, release = Event(), Event()
            original_close = self.controllers[0].close
            def slow_close():
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test expiry close was not released")
                original_close()
            with patch.object(self.controllers[0], "close", side_effect=slow_close):
                self.now += DEFAULT_IDLE_TIMEOUT_SECONDS
                expiring = asyncio.create_task(self.manager._expire_if_due(environment_id))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    self.assertTrue(self.manager._records[environment_id].closing)
                    self.assertIsNotNone(self.registry._get(environment_id))
                finally:
                    release.set()
                await expiring
            self.assertNotIn(environment_id, self.manager._records)
            self.assertIsNone(self.registry._get(environment_id))
        asyncio.run(scenario())

    def setUp(self):
        self.now = 100.0
        self.clock = lambda: self.now
        self.registry = PausedWorldWebSocketRegistry(max_worlds=8)
        self.controllers = []
        self.owner_resolutions = []

        def make_controller(owner):
            controller = self._fake_controller(owner)
            self.controllers.append(controller)
            return controller

        self.settings = WebOwnershipSettings(allowed_origins=(ORIGIN,), allowed_hosts=("127.0.0.1",))
        self.manager = BrowserEnvironmentManager(self.registry, environment_factory=make_controller,
                                                 settings=self.settings, clock=self.clock)
        self.app = FastAPI()
        self.app.include_router(create_browser_environment_router(self.manager))
        self.app.include_router(create_live_attack_websocket_router(
            self.registry, resolve_owner=self._resolve_ws_owner,
            allowed_origins=self.settings.allowed_origins))
        self.http = DirectHttp(self.app)
        self.sockets = []

    def _resolve_ws_owner(self, websocket):
        context = self.manager.resolve_owner(websocket)
        self.owner_resolutions.append(context)
        return context

    def _fake_controller(self, owner):
        # Ownership/lifecycle tests should not compile a simulator world. The
        # real owner->session binding remains covered by test_attack_live_world.
        del owner
        host_clock = VirtualHostClock()
        world = LiveWorkloadWorld.__new__(LiveWorkloadWorld)
        world._time_policy = parse_live_time_policy(POLICY)
        world._runner = SimpleNamespace(_clock=SimpleNamespace(_host_ns=host_clock),
                                        close=lambda: None)
        world._ingress = SimpleNamespace(close=lambda: None)
        world.authorize_wait = lambda credential, owner_context=None: (_ for _ in ()).throw(
            PermissionError("test virtual credential is separate"))
        return PausedWorldController(world, host_clock, world.live_time_policy)

    def tearDown(self):
        asyncio.run(self._cleanup())

    async def _cleanup(self):
        for socket in self.sockets:
            await socket.stop()
        for environment_id in tuple(self.registry._entries):
            await self.registry.discard(environment_id)

    async def _create(self, *, cookie="", origin=ORIGIN, host=HOST, csrf=True):
        return await self.http.request("POST", "/api/attack/environments", cookie=cookie,
                                       origin=origin, host=host, csrf=csrf)

    @staticmethod
    def _cookie_parts(headers):
        parsed = SimpleCookie()
        parsed.load(headers["set-cookie"])
        morsel = next(iter(parsed.values()))
        return morsel.key, morsel.value, morsel

    def test_cookie_is_environment_scoped_hashed_and_has_required_attributes(self):
        async def scenario():
            status, headers, body = await self._create()
            self.assertEqual(status, 201)
            environment_id = body["environment_id"]
            name, token, morsel = self._cookie_parts(headers)
            self.assertEqual(name, self.manager.cookie_name(environment_id))
            self.assertIn(environment_id, name)
            self.assertEqual(morsel["path"], COOKIE_PATH)
            self.assertTrue(morsel["httponly"])
            self.assertEqual(morsel["samesite"].lower(), "strict")
            self.assertFalse(morsel["secure"])
            self.assertEqual(int(morsel["max-age"]), DEFAULT_ABSOLUTE_TIMEOUT_SECONDS)
            record = self.manager._records[environment_id]
            self.assertEqual(record.token_hash.hex(), hashlib.sha256(token.encode()).hexdigest())
            self.assertNotIn(token, repr(record))
            self.assertIs(type(record.context), WebOwnerContext)
            self.assertEqual(self.manager.settings.idle_timeout_seconds, DEFAULT_IDLE_TIMEOUT_SECONDS)
        asyncio.run(scenario())

    def test_https_cookie_is_secure_and_cookie_names_are_startup_scoped(self):
        async def scenario():
            settings = WebOwnershipSettings(allowed_origins=("https://sim.example",),
                allowed_hosts=("sim.example",), startup_nonce="a1b2c3d4e5f60718")
            manager = BrowserEnvironmentManager(self.registry,
                environment_factory=lambda owner: self._make_controller(owner), settings=settings,
                clock=self.clock)
            app = FastAPI()
            app.include_router(create_browser_environment_router(manager))
            status, headers, body = await DirectHttp(app).request("POST", "/api/attack/environments",
                origin="https://sim.example", host="sim.example")
            self.assertEqual(status, 201)
            name, _, morsel = self._cookie_parts(headers)
            self.assertTrue(morsel["secure"])
            self.assertIn("a1b2c3d4e5f60718", name)
        asyncio.run(scenario())

    def _make_controller(self, owner):
        controller = self._fake_controller(owner)
        self.controllers.append(controller)
        return controller

    def test_default_browser_limit_and_local_development_limit_four(self):
        async def scenario():
            status, headers, body = await self._create()
            self.assertEqual(status, 201)
            name, token, _ = self._cookie_parts(headers)
            cookie = f"{name}={token}"
            denied = await self._create(cookie=cookie)
            self.assertEqual((denied[0], denied[2]["detail"]["code"]), (429, "ENVIRONMENT_LIMIT"))

            local = WebOwnershipSettings.local_development()
            self.assertEqual(local.max_environments_per_browser, 4)
            local_manager = BrowserEnvironmentManager(self.registry,
                environment_factory=lambda owner: self._make_controller(owner), settings=local,
                clock=self.clock)
            local_app = FastAPI()
            local_app.include_router(create_browser_environment_router(local_manager))
            local_http = DirectHttp(local_app)
            cookie_pairs = []
            local_origin = "http://localhost:5173"
            for _ in range(4):
                result = await local_http.request("POST", "/api/attack/environments",
                    origin=local_origin, host=HOST, cookie="; ".join(cookie_pairs))
                self.assertEqual(result[0], 201)
                env_cookie, token_value, _ = self._cookie_parts(result[1])
                cookie_pairs.append(f"{env_cookie}={token_value}")
            fifth = await local_http.request("POST", "/api/attack/environments",
                origin=local_origin, host=HOST, cookie="; ".join(cookie_pairs))
            self.assertEqual((fifth[0], fifth[2]["detail"]["code"]), (429, "ENVIRONMENT_LIMIT"))
        asyncio.run(scenario())

    def test_server_wide_limit_is_bounded_and_returns_capacity_response(self):
        async def scenario():
            settings = WebOwnershipSettings(allowed_origins=(ORIGIN,), allowed_hosts=("127.0.0.1",),
                                            max_environments_total=2)
            manager = BrowserEnvironmentManager(self.registry,
                environment_factory=lambda owner: self._make_controller(owner), settings=settings,
                clock=self.clock)
            app = FastAPI()
            app.include_router(create_browser_environment_router(manager))
            direct = DirectHttp(app)
            first = await direct.request("POST", "/api/attack/environments")
            second = await direct.request("POST", "/api/attack/environments")
            self.assertEqual((first[0], second[0]), (201, 201))
            third = await direct.request("POST", "/api/attack/environments")
            self.assertEqual((third[0], third[2]["detail"]["code"]), (503, "ENVIRONMENT_CAPACITY"))
            self.assertLessEqual(len(manager._records), 2)
        asyncio.run(scenario())

    def test_expired_absolute_and_idle_cookies_are_rejected_and_resources_discarded(self):
        async def scenario():
            settings = WebOwnershipSettings(allowed_origins=(ORIGIN,), allowed_hosts=("127.0.0.1",),
                idle_timeout_seconds=5, absolute_timeout_seconds=20)
            manager = BrowserEnvironmentManager(self.registry,
                environment_factory=lambda owner: self._make_controller(owner), settings=settings,
                clock=self.clock)
            app = FastAPI()
            app.include_router(create_browser_environment_router(manager))
            direct = DirectHttp(app)
            status, headers, body = await direct.request("POST", "/api/attack/environments")
            env_id = body["environment_id"]
            name, token, _ = self._cookie_parts(headers)
            cookie = f"{name}={token}"
            self.now += 5
            rejected = await direct.request("DELETE", f"/api/attack/environments/{env_id}", cookie=cookie)
            self.assertEqual((rejected[0], rejected[2]), (404, {"detail": {"code": "ENVIRONMENT_NOT_FOUND"}}))
            self.assertIsNone(self.registry._get(env_id))

            status, headers, body = await direct.request("POST", "/api/attack/environments")
            env_id2 = body["environment_id"]
            name2, token2, _ = self._cookie_parts(headers)
            for _ in range(4):
                self.now += 4
                self.assertIsNotNone(manager._resolve_cookie(env_id2, {name2: token2})[0])
            self.now += 4  # Activity cannot extend the absolute lifetime.
            self.assertIsNone(manager._resolve_cookie(env_id2, {name2: token2})[0])
            await manager._dispose_many([env_id2])
            self.assertIsNone(self.registry._get(env_id2))
        asyncio.run(scenario())

    def test_valid_destroy_expires_cookie_and_later_matches_unknown_environment(self):
        async def scenario():
            status, headers, body = await self._create()
            env_id = body["environment_id"]
            name, token, _ = self._cookie_parts(headers)
            cookie = f"{name}={token}"
            removed = await self.http.request("DELETE", f"/api/attack/environments/{env_id}", cookie=cookie)
            self.assertEqual(removed[0], 204)
            self.assertIn("max-age=0", removed[1]["set-cookie"].lower())
            self.assertIsNone(self.registry._get(env_id))
            after = await self.http.request("DELETE", f"/api/attack/environments/{env_id}", cookie=cookie)
            unknown = await self.http.request("DELETE", "/api/attack/environments/invalid-id")
            self.assertEqual((after[0], after[2]), (unknown[0], unknown[2]))
        asyncio.run(scenario())

    def test_invalid_identifier_cookie_other_origin_and_host_are_rejected(self):
        async def scenario():
            status, headers, body = await self._create()
            name, token, _ = self._cookie_parts(headers)
            cookie = f"{name}={token}"
            missing = await self.http.request("DELETE", "/api/attack/environments/0" , cookie=cookie)
            unknown = await self.http.request("DELETE", "/api/attack/environments/" + "0" * 32)
            self.assertEqual((missing[0], missing[2]), (unknown[0], unknown[2]))
            invalid_token = (token[:-1] + ("A" if token[-1] != "A" else "B"))
            invalid = await self.http.request("DELETE", f"/api/attack/environments/{body['environment_id']}",
                                              cookie=f"{name}={invalid_token}")
            self.assertEqual((invalid[0], invalid[2]), (unknown[0], unknown[2]))
            bad_origin = await self._create(origin="http://evil.example")
            bad_host = await self._create(host="rebind.example")
            self.assertEqual((bad_origin[0], bad_host[0]), (403, 403))
            no_csrf = await self._create(csrf=False)
            self.assertEqual((no_csrf[0], no_csrf[2]["detail"]["code"]), (403, "CSRF_HEADER_REQUIRED"))
        asyncio.run(scenario())

    def test_cookie_owner_is_the_context_bound_to_the_real_attack_session(self):
        async def scenario():
            registry = PausedWorldWebSocketRegistry(max_worlds=2)
            clock = VirtualHostClock()
            credentials = {}

            def real_factory(owner):
                args, _, _, _, world = setup(owner_context=owner, victim_workload=False,
                    live_time_policy=POLICY, host_clock_source=clock)
                credentials["attacker"] = args[4]["a"]
                return PausedWorldController.from_manifest(world, clock)

            manager = BrowserEnvironmentManager(registry, environment_factory=real_factory,
                settings=self.settings, clock=self.clock)
            app = FastAPI()
            app.include_router(create_browser_environment_router(manager))
            app.include_router(create_live_attack_websocket_router(
                registry, resolve_owner=manager.resolve_owner, allowed_origins=self.settings.allowed_origins))
            status, headers, body = await DirectHttp(app).request("POST", "/api/attack/environments")
            self.assertEqual(status, 201)
            name, token, _ = self._cookie_parts(headers)
            socket = DirectWebSocket(app, f"/api/attack/live/{body['environment_id']}/socket",
                                     f"{name}={token}")
            self.sockets.append(socket)
            self.assertEqual((await socket.start())["type"], "websocket.accept")
            await socket.send_text({"type": "authenticate", "credential": credentials["attacker"]})
            ready = await socket.next()
            self.assertEqual(json.loads(ready["text"])["type"], "ready")
            await registry.discard(body["environment_id"])
        asyncio.run(scenario())

    def test_http_owner_resolver_requires_origin_host_and_csrf_for_mutations(self):
        async def scenario():
            status, headers, body = await self._create()
            name, token, _ = self._cookie_parts(headers)
            base_scope = {
                "type": "http", "method": "POST", "scheme": "http", "path": "/api/attack/live/x/jobs",
                "headers": [(b"origin", ORIGIN.encode()), (b"host", HOST.encode()),
                            (b"cookie", f"{name}={token}".encode())],
                "path_params": {"world_id": body["environment_id"]},
            }
            request = Request(dict(base_scope, headers=base_scope["headers"] + [
                (CSRF_HEADER.encode(), CSRF_HEADER_VALUE.encode())]))
            resolved = self.manager.resolve_http_owner(request)
            self.assertIs(resolved, self.manager._records[body["environment_id"]].context)
            no_header = Request(base_scope)
            with self.assertRaises(HTTPException) as caught:
                self.manager.resolve_http_owner(no_header)
            self.assertEqual(getattr(caught.exception, "status_code", None), 403)
            wrong_origin = Request(dict(base_scope, headers=[
                (b"origin", b"http://evil.example"), (b"host", HOST.encode()),
                (b"cookie", f"{name}={token}".encode()),
                (CSRF_HEADER.encode(), CSRF_HEADER_VALUE.encode()),
            ]))
            with self.assertRaises(RequestOriginHostRejected):
                self.manager.resolve_http_owner(wrong_origin)
        asyncio.run(scenario())

    def test_websocket_owner_resolver_checks_origin_host_and_refreshes_idle_timeout(self):
        async def scenario():
            status, headers, body = await self._create()
            env_id = body["environment_id"]
            name, token, _ = self._cookie_parts(headers)
            cookie = f"{name}={token}"
            path = f"/api/attack/live/{env_id}/socket"
            bad_origin = DirectWebSocket(self.app, path, cookie, origin="http://evil.example")
            self.sockets.append(bad_origin)
            rejected = await bad_origin.start()
            self.assertEqual((rejected["type"], rejected["code"]), ("websocket.close", 4403))
            bad_host = DirectWebSocket(self.app, path, cookie, host="attacker.example")
            self.sockets.append(bad_host)
            await bad_host.start()
            await bad_host.send_text({"type": "authenticate", "credential": "ignored"})
            rejected = await bad_host.next()
            self.assertEqual(rejected["code"], 4401)

            good = DirectWebSocket(self.app, path, cookie)
            self.sockets.append(good)
            accepted = await good.start()
            self.assertEqual(accepted["type"], "websocket.accept")
            await good.send_text({"type": "authenticate", "credential": "not-a-virtual-credential"})
            rejected = await good.next()
            self.assertEqual(rejected["code"], 4401)
            self.assertTrue(self.owner_resolutions)
            self.assertTrue(all(item is self.manager._records[env_id].context
                                for item in self.owner_resolutions))
        asyncio.run(scenario())

    def test_settings_are_explicit_and_do_not_infer_publication_from_allowed_origins(self):
        from unittest.mock import patch
        with patch.dict("os.environ", {"ALLOWED_ORIGINS": "https://public.example"}, clear=True):
            settings = WebOwnershipSettings.from_environment()
        self.assertEqual(settings.max_environments_per_browser, 1)
        self.assertEqual(settings.max_environments_total, 8)
        self.assertEqual(settings.allowed_origins, ())
        self.assertEqual(WebOwnershipSettings.desktop("127.0.0.1", 9000).max_environments_per_browser, 4)
        self.assertNotEqual(WebOwnershipSettings.desktop("127.0.0.1", 9000).startup_nonce,
                            WebOwnershipSettings.desktop("127.0.0.1", 9000).startup_nonce)

    def test_main_app_keeps_environment_lifecycle_routes_unmounted(self):
        from api.main import app
        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertNotIn("/api/attack/environments", paths)
        self.assertNotIn("/api/attack/environments/{environment_id}", paths)


if __name__ == "__main__":
    unittest.main()

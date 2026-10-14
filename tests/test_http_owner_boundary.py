"""ASGI scope bridge with a test resolver, not actual site authentication."""

import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from api.attack_http import create_attack_router
from api.attack_sessions import AttackSessionRegistry, WebOwnerContext
from tests.test_attack_http_adapter import _request
from tests.test_attack_sessions import circuit_json, session_fixture


class HttpOwnerBoundaryTests(unittest.TestCase):
    def fixture(self, *, owner=None, resolver=None, operations=None):
        args = session_fixture(**({"attacker_operations": operations} if operations is not None else {}))
        owner = WebOwnerContext() if owner is None else owner
        registry = AttackSessionRegistry(max_sessions=1)
        registry.register("owned", args[5], owner_context=owner)
        app = FastAPI()
        app.include_router(create_attack_router(registry, resolve_owner=resolver))
        return args, owner, app

    def get(self, app, token, name="owned"):
        return asyncio.run(_request(app, "GET", "/api/attack/sessions/" + name + "/devices", credential=token))

    def test_owned_route_requires_real_owner_even_with_valid_virtual_bearer(self):
        args, _, app = self.fixture()
        self.assertEqual(self.get(app, args[4]["a"]), (401, {"detail": {"code": "UNAUTHENTICATED"}}))
        self.assertEqual(args[5].receipt_records(), ())

    def test_wrong_owner_and_absent_environment_have_the_same_response(self):
        args, _, app = self.fixture(resolver=lambda request: WebOwnerContext())
        first = self.get(app, args[4]["a"])
        self.assertEqual(first, (401, {"detail": {"code": "UNAUTHENTICATED"}}))
        self.assertEqual(first, self.get(app, args[4]["a"], "absent"))
        self.assertEqual(args[5].receipt_records(), ())

    def test_owner_resolution_does_not_grant_actor_permissions(self):
        owner = WebOwnerContext()
        args, _, app = self.fixture(owner=owner, resolver=lambda request: owner, operations=())
        self.assertEqual(self.get(app, args[4]["a"]), (403, {"detail": {"code": "OPERATION_DENIED"}}))
        self.assertEqual(self.get(app, args[4]["v"])[0], 200)

    def test_authenticated_owner_reconnects_without_reset_or_new_environment(self):
        owner = WebOwnerContext()
        args, _, app = self.fixture(owner=owner, resolver=lambda request: owner)
        path = "/api/attack/sessions/owned/programs"
        payload = {"device_id": "qpu-1", "shots": 2, "circuit_config": json.loads(circuit_json())}

        def submit():
            return asyncio.run(_request(app, "POST", path, credential=args[4]["a"], payload=payload,
                                       headers=[(b"idempotency-key", b"same-reconnect")]))

        first, replay = submit(), submit()
        self.assertEqual(first, replay)
        self.assertEqual(first[0], 202)
        self.assertEqual(len(args[2].scheduler.queued_job_ids()), 1)
        self.assertEqual(args[1].events(), ())
        self.assertEqual(set(first[1]), {"job_id", "status", "error_code"})

    def test_resolver_error_and_untyped_result_do_not_leak_context(self):
        for resolver, expected in ((lambda request: "not-an-owner", 401),
                                   (lambda request: None, 401)):
            args, _, app = self.fixture(resolver=resolver)
            self.assertEqual(self.get(app, args[4]["a"])[0], expected)

        def broken(request):
            raise RuntimeError("PRIVATE_OWNER_RESOLVER_DETAILS")

        args, _, app = self.fixture(resolver=broken)
        self.assertEqual(self.get(app, args[4]["a"]), (500, {"detail": {"code": "INTERNAL_ERROR"}}))

    def test_owner_bound_during_body_read_rejects_previously_unowned_request(self):
        from api import attack_http
        args = session_fixture()
        owner = WebOwnerContext()
        owned = AttackSessionRegistry(max_sessions=1)
        app = FastAPI()
        app.include_router(create_attack_router(args[6]))
        read = attack_http._body

        async def bind_during_body(request, model):
            owned.register("owned", args[5], owner_context=owner)
            return await read(request, model)

        with patch.object(attack_http, "_body", side_effect=bind_during_body):
            result = asyncio.run(_request(app, "POST", "/api/attack/sessions/world/programs",
                credential=args[4]["a"], payload={"device_id": "qpu-1", "shots": 1,
                                                "circuit_config": json.loads(circuit_json())}))
        self.assertEqual(result, (401, {"detail": {"code": "UNAUTHENTICATED"}}))
        self.assertEqual(args[5].receipt_records(), ())
        self.assertEqual(args[2].scheduler.queued_job_ids(), ())
        self.assertIs(owned.authenticated("owned", args[4]["a"], owner_context=owner), args[5])


if __name__ == "__main__":
    unittest.main()

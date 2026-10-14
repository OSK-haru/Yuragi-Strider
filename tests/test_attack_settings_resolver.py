"""Untrusted settings catalog/resolution and D63 browser flow via direct ASGI."""

import asyncio
from dataclasses import replace
import json
import unittest

from fastapi import FastAPI

from api.attack_live_websocket import (
    PausedWorldWebSocketRegistry, create_live_attack_websocket_router,
)
from api.attack_product_factory import ProductEnvironmentFactory
from api.attack_device_snapshot import load_ibm_kingston_snapshot
from api.attack_product_profiles import product_free_scenarios, standard_snapshot_initial_values
from api.attack_settings_resolver import (
    ChallengeDefinition, FreeScenarioDefinition, ScenarioSettingsResolver, SettingField,
    create_settings_catalog_router,
)
from api.attack_web_ownership import (
    BrowserEnvironmentManager, WebOwnershipSettings, create_browser_environment_router,
)
from core.attack_simulation.scenario_policy import ScenarioMode, ScenarioPreset
from tests.test_attack_product_factory import (
    HOST_LIMITS, OPS, prepared_environment,
)
from tests.test_attack_web_ownership import DirectHttp, DirectWebSocket, ORIGIN, HOST


RESET_DURATION = "qpu_service.runtime_execution.reset_duration_us"
TEMPERATURE = "qpu_service.idle_dynamics.physical_inputs.temperature_mk"
SECRET = "DO_NOT_LEAK_HIDDEN_PRESET_SENTINEL"


def _field(path, *, value_type="number", minimum=0.0, maximum=10.0, unit="us"):
    return SettingField(path, path.rsplit(".", 1)[-1], value_type,
                        "Explicit server profile for resolver tests.",
                        "Test-only public setting definition.", minimum, maximum,
                        unit=unit)


def _resolver():
    calibration = prepared_environment(ScenarioMode.FREE).session
    physical = prepared_environment(
        ScenarioMode.FREE, idle_input_mode="physical_parameters",
    ).session
    free = {
        "calibration_values": FreeScenarioDefinition(
            calibration.template, calibration.assumptions,
            (_field(RESET_DURATION),), OPS,
        ),
        "physical_parameters": FreeScenarioDefinition(
            physical.template, physical.assumptions,
            (_field(RESET_DURATION), _field(TEMPERATURE, maximum=100.0, unit="mK")), OPS,
        ),
    }
    challenge_environment = prepared_environment(ScenarioMode.CHALLENGE)
    session = challenge_environment.session
    victim_config = dict(session.template.victim_config)
    victim_config["internal_secret"] = SECRET
    template = replace(session.template, victim_config=victim_config)
    policy = replace(
        session.policy,
        locked_fields=session.policy.locked_fields + ("victim.internal_secret",),
        hidden_fields=("victim.internal_secret",),
        editable_fields=("qpu_service.runtime_execution.victim_operation_duration_us",),
    )
    preset = ScenarioPreset(template, policy, session.assumptions)
    challenge = ChallengeDefinition("reset-lab-test", "Reset lab (test only)",
                                    "Synthetic server-owned test preset.", preset,
                                    (_field("qpu_service.runtime_execution.victim_operation_duration_us"),))
    return ScenarioSettingsResolver(free_scenarios=free, challenges=(challenge,))


class SettingsResolverTests(unittest.TestCase):
    def setUp(self):
        self.resolver = _resolver()

    def test_catalog_has_modes_bounds_d62_basis_and_no_hidden_preset_value(self):
        catalog = self.resolver.catalog()
        self.assertEqual(catalog["schema_version"], "attack-settings-catalog-v1")
        self.assertEqual([item["id"] for item in catalog["modes"]], ["free", "challenge"])
        self.assertEqual([item["id"] for item in catalog["free_mode"]["idle_noise_modes"]],
                         ["calibration_values", "physical_parameters"])
        self.assertIn("2026-08-03", catalog["free_mode"]["standard_initial_values"]["values"]["t1_us"]["basis"])
        self.assertNotIn(SECRET, json.dumps(catalog))
        free_field = catalog["free_mode"]["idle_noise_modes"][0]["fields"][0]
        self.assertEqual((free_field["minimum"], free_field["maximum"]), (0.0, 10.0))
        placeholder = next(item for item in catalog["challenges"] if item["id"] == "reset-leakage")
        self.assertFalse(placeholder["available"])

    def test_server_catalog_cannot_mark_authority_or_provenance_as_editable(self):
        for path in (
            "attacker.operation_grants", "victim.tenant_id",
            "qpu_service.credentials.attacker", "qpu_service.idle_dynamics.basis",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                _field(path)

    def test_same_origin_browser_catalog_get_without_origin_is_guarded(self):
        app = FastAPI()
        settings = WebOwnershipSettings(allowed_origins=(ORIGIN,), allowed_hosts=("127.0.0.1",))
        app.include_router(create_settings_catalog_router(self.resolver, settings))

        async def request(extra_headers, host=HOST):
            async def browser_app(scope, receive, send):
                scope = {**scope, "headers": [
                    (key, value) for key, value in scope["headers"]
                    if key not in (b"origin", b"host")
                ] + [(b"host", host.encode()), *extra_headers]}
                await app(scope, receive, send)
            return await DirectHttp(browser_app).request("GET", "/api/attack/settings/catalog")

        valid = [(b"sec-fetch-site", b"same-origin"), (b"referer", f"{ORIGIN}/attack-simulator".encode())]
        result = asyncio.run(request(valid))
        self.assertEqual(result[0], 200)
        self.assertEqual(asyncio.run(request(valid[:1]))[0], 200)  # no-referrer policy
        cases = [[], valid[1:],
                 [(b"sec-fetch-site", b"cross-site"), valid[1]],
                 [valid[0], (b"referer", b"https://other.invalid/page")],
                 [*valid, valid[1]],
                 [valid[0], (b"referer", b"http://127.0.0.1:bad/page")],
                 [*valid, (b"origin", b"https://other.invalid")]]
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(asyncio.run(request(headers))[0], 403)
        self.assertEqual(asyncio.run(request(valid, "other.invalid"))[0], 403)

    def test_free_override_is_frozen_into_manifest_with_server_provenance(self):
        payload = {
            "mode": "free", "idle_noise_mode": "calibration_values",
            "settings": {RESET_DURATION: 2.75},
        }
        env = self.resolver.resolve(json.dumps(payload).encode())
        manifest = env.establish()
        self.assertEqual(manifest.resolve_path(RESET_DURATION), 2.75)
        self.assertEqual(len(manifest.overrides), 1)
        self.assertEqual(manifest.overrides[0].path, RESET_DURATION)
        self.assertNotEqual(manifest.overrides[0].rationale, "")

    def test_challenge_fixed_field_is_rejected_without_revealing_preset(self):
        payload = {"mode": "challenge", "preset_id": "reset-lab-test",
                   "settings": {RESET_DURATION: 3.0}}
        with self.assertRaises(ValueError):
            self.resolver.resolve(json.dumps(payload).encode())

    def test_challenge_editable_field_is_limited_to_preset_allowlist(self):
        path = "qpu_service.runtime_execution.victim_operation_duration_us"
        payload = {"mode": "challenge", "preset_id": "reset-lab-test",
                   "settings": {path: 4.0}}
        manifest = self.resolver.resolve(json.dumps(payload).encode()).establish()
        self.assertEqual(manifest.resolve_path(path), 4.0)
        self.assertEqual(manifest.overrides[0].path, path)

    def test_challenge_region_cannot_be_overridden_by_browser(self):
        payload = {"mode": "challenge", "preset_id": "reset-lab-test", "settings": {},
                   "regions": {"shared_physical_qubit_ids": [1]}}
        with self.assertRaises(ValueError):
            self.resolver.resolve(json.dumps(payload).encode())

    def test_product_resolver_freezes_selected_connected_region_and_qubit_noise(self):
        snapshot = load_ibm_kingston_snapshot()
        resolver = ScenarioSettingsResolver(free_scenarios=product_free_scenarios(snapshot),
                                            standard_initial_values=standard_snapshot_initial_values(snapshot))
        catalog = resolver.catalog()
        mode = next(item for item in catalog["free_mode"]["idle_noise_modes"]
                    if item["id"] == "calibration_values")
        metadata = mode["region_selection"]
        self.assertEqual(len(mode["fields"]), len(set(item["path"] for item in mode["fields"])))
        self.assertEqual(len(metadata["qubit_noise_fields"]["primary"]), 3)
        physical_mode = next(item for item in catalog["free_mode"]["idle_noise_modes"]
                             if item["id"] == "physical_parameters")
        self.assertNotIn("qubit_noise_fields", physical_mode["region_selection"])
        selected = metadata["alternate_region_initial"]
        qid = selected[0]
        baseline = catalog["free_mode"]["standard_initial_values"]["qubits"][str(qid)]["T1_us"]
        path = f"qpu_service.idle_dynamics.qubits.{qid}.t1_us"
        environment = resolver.resolve(json.dumps({
            "mode": "free", "idle_noise_mode": "calibration_values",
            "settings": {path: baseline + 1.25},
            "regions": {"shared_physical_qubit_ids": selected},
        }).encode())
        manifest = environment.establish()
        self.assertEqual(manifest.qpu_service_config["retained_region"]["physical_qubit_ids"],
                         tuple(sorted(selected)))
        self.assertEqual(manifest.qpu_service_config["idle_dynamics"]["qubits"][str(qid)]["t1_us"], baseline + 1.25)
        self.assertEqual(set(manifest.qpu_service_config["idle_dynamics"]["qubits"]),
                         {str(item) for item in selected})
        from api.attack_sessions import WebOwnerContext
        product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, WebOwnerContext())
        try:
            self.assertEqual(product.controller._world._executor.runtime.state_store.region_binding.physical_qubit_ids,
                             tuple(sorted(selected)))
        finally:
            product.close()

    def test_product_resolver_rejects_invalid_regions_and_applies_dedicated_qubit_noise(self):
        snapshot = load_ibm_kingston_snapshot()
        resolver = ScenarioSettingsResolver(free_scenarios=product_free_scenarios(snapshot),
                                            standard_initial_values=standard_snapshot_initial_values(snapshot))
        catalog = resolver.catalog()
        standard = catalog["free_mode"]["standard_initial_values"]
        metadata = next(item for item in catalog["free_mode"]["idle_noise_modes"]
                        if item["id"] == "calibration_values")["region_selection"]
        victim, attacker = metadata["active_region_initial"], metadata["alternate_region_initial"]
        edges = set(standard["cz_calibrations"])
        eligible = metadata["eligible_qubit_ids"]
        left, right = next((a, b) for a in eligible for b in eligible
                           if a < b and f"{a}:{b}" not in edges and f"{b}:{a}" not in edges)
        unusable = next(int(qid) for qid, item in standard["qubits"].items()
                        if not item["usable_for_placement"])
        invalid_regions = (
            {"shared_physical_qubit_ids": [left, right]},
            {"shared_physical_qubit_ids": [*victim, eligible[-1]]},
            {"shared_physical_qubit_ids": [*victim[:-1], unusable]},
        )
        for regions in invalid_regions:
            with self.subTest(regions=regions), self.assertRaises(ValueError):
                resolver.resolve(json.dumps({"mode": "free", "idle_noise_mode": "calibration_values",
                    "settings": {}, "regions": regions}).encode())

        with self.assertRaises(ValueError):
            resolver.resolve(json.dumps({"mode": "free", "idle_noise_mode": "calibration_values",
                "settings": {"qpu_service.runtime_execution.placement_policy": "tenant_dedicated_region"},
                "regions": {"victim_physical_qubit_ids": victim,
                            "attacker_physical_qubit_ids": victim}}).encode())

        qid = attacker[0]
        baseline = standard["qubits"][str(qid)]["T2_us"]
        path = f"qpu_service.dedicated_region.idle_dynamics.qubits.{qid}.t2_us"
        environment = resolver.resolve(json.dumps({
            "mode": "free", "idle_noise_mode": "calibration_values",
            "settings": {"qpu_service.runtime_execution.placement_policy": "tenant_dedicated_region",
                         path: baseline + 2.0},
            "regions": {"victim_physical_qubit_ids": victim,
                        "attacker_physical_qubit_ids": attacker},
        }).encode())
        manifest = environment.establish()
        alternate = manifest.qpu_service_config["dedicated_region"]
        self.assertEqual(alternate["physical_qubit_ids"], tuple(sorted(attacker)))
        self.assertEqual(alternate["idle_dynamics"]["qubits"][str(qid)]["t2_us"], baseline + 2.0)

    def test_unavailable_reset_frame_cannot_create_environment(self):
        payload = {"mode": "challenge", "preset_id": "reset-leakage", "settings": {}}
        with self.assertRaises(ValueError):
            self.resolver.resolve(json.dumps(payload).encode())

    def test_unknown_range_type_and_mixed_idle_mode_fields_are_rejected(self):
        cases = (
            {"mode": "free", "idle_noise_mode": "calibration_values",
             "settings": {"unknown.path": 1}},
            {"mode": "free", "idle_noise_mode": "calibration_values",
             "settings": {RESET_DURATION: 11.0}},
            {"mode": "free", "idle_noise_mode": "calibration_values",
             "settings": {RESET_DURATION: True}},
            {"mode": "free", "idle_noise_mode": "calibration_values",
             "settings": {TEMPERATURE: 20.0}},
            {"mode": "free", "idle_noise_mode": "calibration_values",
             "settings": {RESET_DURATION: float("nan")}},
            {"mode": "free", "idle_noise_mode": "calibration_values",
             "settings": {RESET_DURATION: 2.0}, "preset_id": "reset-lab-test"},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                raw = json.dumps(payload, allow_nan=True).encode()
                with self.assertRaises((ValueError, TypeError)):
                    self.resolver.resolve(raw)

    def test_duplicate_keys_and_oversized_input_are_rejected(self):
        with self.assertRaises(ValueError):
            self.resolver.resolve(b'{"mode":"free","mode":"challenge"}')
        with self.assertRaises(ValueError):
            self.resolver.resolve(b" " * (64 * 1024 + 1))

    def test_catalog_create_connect_submit_wait_reply_delete_for_both_modes(self):
        async def scenario():
            settings = WebOwnershipSettings(
                allowed_origins=(ORIGIN,), allowed_hosts=("127.0.0.1",),
                max_environments_total=2,
            )
            registry = PausedWorldWebSocketRegistry(max_worlds=2)
            product_factory = ProductEnvironmentFactory(HOST_LIMITS)
            manager = BrowserEnvironmentManager(
                registry, request_environment_factory=self.resolver.bind_product_factory(product_factory),
                settings=settings,
            )
            app = FastAPI()
            app.include_router(create_settings_catalog_router(self.resolver, settings))
            app.include_router(create_browser_environment_router(manager))
            app.include_router(create_live_attack_websocket_router(
                registry, resolve_owner=manager.resolve_owner,
                allowed_origins=settings.allowed_origins,
            ))
            http = DirectHttp(app)
            sockets = []
            try:
                catalog_status, _, catalog = await http.request("GET", "/api/attack/settings/catalog")
                self.assertEqual(catalog_status, 200)
                for suffix, payload in (
                    ("free", {"mode": "free", "idle_noise_mode": "calibration_values",
                              "settings": {RESET_DURATION: 2.5}}),
                    ("challenge", {"mode": "challenge", "preset_id": "reset-lab-test",
                                   "settings": {"qpu_service.runtime_execution.victim_operation_duration_us": 4.0}}),
                ):
                    self.assertIn("modes", catalog)
                    status, headers, body = await http.request(
                        "POST", "/api/attack/environments", body=payload,
                        content_type="application/json",
                    )
                    self.assertEqual(status, 201)
                    self.assertEqual(set(body), {"environment_id", "attacker_credential"})
                    environment_id = body["environment_id"]
                    cookie = http_cookie(headers)
                    socket = DirectWebSocket(
                        app, f"/api/attack/live/{environment_id}/socket", cookie,
                    )
                    sockets.append(socket)
                    self.assertEqual((await socket.start())["type"], "websocket.accept")
                    await socket.send_text({
                        "type": "authenticate", "credential": body["attacker_credential"],
                    })
                    self.assertEqual(json.loads((await socket.next())["text"])["type"], "ready")
                    await socket.send_text({
                        "type": "command", "command_id": f"submit-{suffix}",
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
                            }, "initialization": "prepare",
                        },
                    })
                    accepted = json.loads((await socket.next())["text"])
                    self.assertEqual(accepted["type"], "accepted")
                    await socket.send_text({
                        "type": "command", "command_id": f"wait-{suffix}",
                        "operation": "wait.duration", "payload": {"duration_us": 10.0},
                    })
                    self.assertEqual(json.loads((await socket.next())["text"])["type"], "wait.completed")
                    self.assertEqual(json.loads((await socket.next())["text"])["type"], "reply")
                    deleted = await http.request("DELETE", f"/api/attack/environments/{environment_id}",
                                                 cookie=cookie)
                    self.assertEqual(deleted[0], 204)
                    self.assertEqual((await socket.next())["type"], "websocket.close")
            finally:
                for socket in sockets:
                    await socket.stop()
                for environment_id in tuple(registry._entries):
                    await registry.discard(environment_id)

        asyncio.run(scenario())

    def test_http_rejects_bad_schema_and_oversize_with_public_codes(self):
        async def scenario():
            settings = WebOwnershipSettings(allowed_origins=(ORIGIN,), allowed_hosts=("127.0.0.1",))
            registry = PausedWorldWebSocketRegistry(max_worlds=2)
            manager = BrowserEnvironmentManager(
                registry, request_environment_factory=self.resolver.bind_product_factory(
                    ProductEnvironmentFactory(HOST_LIMITS)), settings=settings,
            )
            app = FastAPI()
            app.include_router(create_browser_environment_router(manager))
            http = DirectHttp(app)
            bad = await http.request("POST", "/api/attack/environments",
                body={"mode": "free", "idle_noise_mode": "calibration_values",
                      "settings": {"secret.private": 1}}, content_type="application/json")
            self.assertEqual((bad[0], bad[2]["detail"]["code"]), (422, "INVALID_SETTINGS"))
            malformed = await http.request("POST", "/api/attack/environments",
                body=b'{"mode":', content_type="application/json")
            self.assertEqual((malformed[0], malformed[2]["detail"]["code"]),
                             (422, "INVALID_SETTINGS"))
            fixed = await http.request("POST", "/api/attack/environments",
                body={"mode": "challenge", "preset_id": "reset-lab-test",
                      "settings": {RESET_DURATION: 5.0}}, content_type="application/json")
            self.assertEqual((fixed[0], fixed[2]["detail"]["code"]),
                             (422, "INVALID_SETTINGS"))
            self.assertNotIn(SECRET, json.dumps(fixed[2]))
            too_large = await http.request("POST", "/api/attack/environments",
                body=b" " * (64 * 1024 + 1), content_type="application/json")
            self.assertEqual((too_large[0], too_large[2]["detail"]["code"]),
                             (413, "SETTINGS_TOO_LARGE"))
            self.assertEqual(len(manager._records), 0)
            self.assertEqual(manager._global_reservations, 0)
        asyncio.run(scenario())


def http_cookie(headers):
    from http.cookies import SimpleCookie
    parsed = SimpleCookie()
    parsed.load(headers["set-cookie"])
    morsel = next(iter(parsed.values()))
    return f"{morsel.key}={morsel.value}"

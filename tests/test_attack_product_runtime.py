"""Mounted product assembly and browser-to-live-world path via direct ASGI."""

import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI

from api.attack_product_runtime import build_attack_simulator_runtime
from api.attack_product_profiles import product_free_scenarios
from api.attack_settings_resolver import ChallengeDefinition, ScenarioSettingsResolver
from api.attack_device_snapshot import deterministic_connected_region, load_ibm_kingston_snapshot
from api.attack_web_ownership import WebOwnershipSettings
from api.main import configure_attack_simulator
from core.attack_simulation.scenario_policy import ScenarioMode, ScenarioPolicy, ScenarioPreset
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from tests.test_attack_web_ownership import DirectHttp, DirectWebSocket


class ProductRuntimeProfileTests(unittest.TestCase):
    def test_challenge_preset_can_lock_the_product_placement_choice(self):
        free = product_free_scenarios()["calibration_values"]
        path = "qpu_service.runtime_execution.placement_policy"
        preset = ScenarioPreset(
            free.template,
            ScenarioPolicy(ScenarioMode.CHALLENGE, locked_fields=(path,),
                           allowed_operations=free.allowed_operations),
            free.assumptions,
        )
        resolver = ScenarioSettingsResolver(
            free_scenarios={"calibration_values": free},
            challenges=(ChallengeDefinition("placement-locked-test", "Placement test",
                                            "Test-only fixed product placement.", preset),),
        )
        self.assertEqual(next(item for item in resolver.catalog()["challenges"]
                              if item["id"] == "placement-locked-test")["fields"], [])
        with self.assertRaises(ValueError):
            resolver.resolve(json.dumps({
                "mode": "challenge", "preset_id": "placement-locked-test",
                "settings": {path: "tenant_dedicated_region"},
            }).encode())
        frozen = resolver.resolve(json.dumps({
            "mode": "challenge", "preset_id": "placement-locked-test", "settings": {},
        }).encode()).establish()
        self.assertEqual(frozen.qpu_service_config["runtime_execution"]["placement_policy"],
                         "guaranteed_same_physical_qubit")

    def test_catalog_profiles_use_snapshot_region_and_keep_idle_modes_exclusive(self):
        runtime = build_attack_simulator_runtime(WebOwnershipSettings.local_development())
        catalog = runtime.resolver.catalog()
        snapshot = load_ibm_kingston_snapshot()
        modes = {item["id"]: item for item in catalog["free_mode"]["idle_noise_modes"]}
        self.assertTrue(modes["calibration_values"]["available"])
        self.assertTrue(modes["physical_parameters"]["available"])
        self.assertEqual(catalog["free_mode"]["standard_initial_values"]["qubit_count"], 156)
        region = tuple(sorted(deterministic_connected_region(snapshot, 6)))
        self.assertEqual(catalog["free_mode"]["standard_initial_values"]["active_region_initial"], list(region))
        for mode in ("calibration_values", "physical_parameters"):
            definition = runtime.resolver._free[mode]
            qpu = definition.template.qpu_service_config
            execution = qpu["runtime_execution"]
            self.assertIs(execution["measure_input_state_before_transition"], True)
            self.assertEqual(execution["placement_policy"], "guaranteed_same_physical_qubit")
            for field in ("measure_input_state_before_transition", "placement_policy"):
                path = f"qpu_service.runtime_execution.{field}"
                self.assertTrue(any(path in record.target_paths and record.rationale
                                    for record in definition.assumptions))
            self.assertEqual(tuple(qpu["topology"]["physical_qubit_ids"]), tuple(range(156)))
            self.assertEqual(len(qpu["topology"]["gate_edges"]), 176)
            self.assertEqual(tuple(qpu["retained_region"]["physical_qubit_ids"]), region)
            self.assertFalse(set(region) & set(snapshot.ineligible_qubit_reasons))
            placement = next(item for item in modes[mode]["fields"]
                             if item["path"] == "qpu_service.runtime_execution.placement_policy")
            self.assertEqual(placement["default"], "guaranteed_same_physical_qubit")
            self.assertEqual(placement["choices"],
                             ["guaranteed_same_physical_qubit", "tenant_dedicated_region"])
            alternate = tuple(qpu["dedicated_region"]["physical_qubit_ids"])
            self.assertEqual(len(alternate), 6)
            self.assertFalse(set(alternate) & set(region))
        selected = runtime.resolver.resolve(json.dumps({
            "mode": "free", "idle_noise_mode": "calibration_values",
            "settings": {"qpu_service.runtime_execution.placement_policy": "tenant_dedicated_region"},
        }).encode())
        selected_manifest = selected.establish()
        self.assertEqual(selected_manifest.qpu_service_config["runtime_execution"]["placement_policy"],
                         "tenant_dedicated_region")
        calibration = runtime.resolver._free["calibration_values"].template.qpu_service_config
        physical = runtime.resolver._free["physical_parameters"].template.qpu_service_config
        self.assertEqual(set(calibration["idle_dynamics"]["qubits"]), {str(qid) for qid in region})
        self.assertNotIn("qubits", physical["idle_dynamics"])
        self.assertIn("physical_inputs", physical["idle_dynamics"])
        frequency_path = "qpu_service.idle_dynamics.physical_inputs.qubit_frequency_ghz"
        self.assertTrue(next(item for item in modes["physical_parameters"]["fields"]
                             if item["path"] == frequency_path)["required"])
        with self.assertRaises(ValueError):
            runtime.resolver.resolve(json.dumps({
                "mode": "free", "idle_noise_mode": "physical_parameters", "settings": {},
            }).encode())
        self.assertIn("2026-04-15", catalog["free_mode"]["standard_initial_values"]["snapshot_date"])
        self.assertFalse(catalog["free_mode"]["standard_initial_values"]["qubits"]["146"]["usable_for_placement"])
        self.assertIsNone(catalog["free_mode"]["standard_initial_values"]["qubits"]["146"]["T1_us"])
        self.assertNotIn("2026-08-03", json.dumps(catalog["free_mode"]["standard_initial_values"]))
        reset = next(item for item in catalog["challenges"] if item["id"] == "reset-leakage")
        self.assertTrue(reset["available"])
        self.assertEqual(reset["fields"], [])
        self.assertNotIn("secret_bit", json.dumps(reset))
        self.assertNotIn("hidden_case_count", json.dumps(reset))
        self.assertNotIn("credentials", json.dumps(catalog).lower())
        for mode, definition in runtime.resolver._free.items():
            policy = definition.template.experiment_config["live_time_policy"]
            self.assertEqual(policy, {"mode": "host_clock", "pause_while_catching_up": False})
            field = next(item for item in definition.fields
                         if item.path == "experiment.live_time_policy.pause_while_catching_up")
            self.assertEqual(field.value_type, "boolean")
            self.assertTrue(field.validate(True))
        enabled = runtime.resolver.resolve(json.dumps({
            "mode": "free", "idle_noise_mode": "calibration_values",
            "settings": {"experiment.live_time_policy.pause_while_catching_up": True},
        }).encode()).establish()
        self.assertIs(enabled.experiment_config["live_time_policy"]["pause_while_catching_up"], True)

    def test_mount_is_profile_gated_and_invalid_attack_config_isolated(self):
        generic = FastAPI()
        self.assertFalse(configure_attack_simulator(profile="generic", target_app=generic))
        self.assertFalse(any(getattr(route, "path", "").startswith("/api/attack")
                             for route in generic.routes))

        malformed = FastAPI()
        with patch("api.main.WebOwnershipSettings.from_environment", side_effect=ValueError("bad profile")):
            with self.assertLogs("api.main", level="WARNING") as captured:
                self.assertFalse(configure_attack_simulator(profile="development", target_app=malformed))
        self.assertIn("Attack simulator disabled", captured.output[0])
        self.assertFalse(malformed.state.attack_simulator_enabled)
        self.assertFalse(any(getattr(route, "path", "").startswith("/api/attack")
                             for route in malformed.routes))

        local = FastAPI()
        settings = WebOwnershipSettings.local_development()
        self.assertTrue(configure_attack_simulator(
            profile="development", ownership_settings=settings, target_app=local,
        ))
        paths = set()
        for included in local.state.attack_simulator_runtime.router.routes:
            paths.update(getattr(route, "path", "")
                         for route in getattr(included, "original_router", included).routes)
        self.assertIn("/api/attack/settings/catalog", paths)
        self.assertIn("/api/attack/environments", paths)
        self.assertIn("/api/attack/environments/{environment_id}", paths)
        self.assertIn("/api/attack/live/{world_id}/socket", paths)
        status, _, _ = asyncio.run(DirectHttp(local).request(
            "GET", "/api/attack/settings/catalog", origin="http://127.0.0.1:5173",
            host="127.0.0.1:8001",
        ))
        self.assertEqual(status, 200)

    def test_catalog_create_connect_submit_server_pushed_reply_and_discard(self):
        async def scenario():
            runtime = build_attack_simulator_runtime(WebOwnershipSettings.local_development())
            app = FastAPI()
            app.include_router(runtime.router)
            http = DirectHttp(app)
            sockets = []
            created_ids = []
            async def next_type(socket, expected):
                for _ in range(100):
                    item = json.loads((await socket.next())["text"])
                    if item.get("type") == expected:
                        return item
                self.fail(f"did not receive WebSocket {expected}")
            try:
                status, _, catalog = await http.request("GET", "/api/attack/settings/catalog")
                self.assertEqual(status, 200)
                self.assertEqual(catalog["schema_version"], "attack-settings-catalog-v1")
                program_columns = [
                    {"step": 0, "gates": [{"type": "H", "targets": [0]}]},
                    {"step": 1, "gates": [{"type": "CZ", "controls": [0], "targets": [1]}]},
                    {"step": 2, "gates": [
                        {"type": "MEASURE", "targets": [0], "classical_targets": [0]},
                        {"type": "MEASURE", "targets": [1], "classical_targets": [1]},
                    ]},
                ]
                for mode, placement in (
                    ("calibration_values", None), ("physical_parameters", None),
                    ("calibration_values", "tenant_dedicated_region"),
                    ("physical_parameters", "tenant_dedicated_region"),
                ):
                    settings = {}
                    catch_up_pause = mode == "calibration_values" and placement is None
                    if catch_up_pause:
                        settings["experiment.live_time_policy.pause_while_catching_up"] = True
                    if placement is not None:
                        settings["qpu_service.runtime_execution.placement_policy"] = placement
                    if mode == "physical_parameters":
                        settings["qpu_service.idle_dynamics.physical_inputs.qubit_frequency_ghz"] = (
                            runtime.resolver._free[mode].template.qpu_service_config[
                                "idle_dynamics"]["physical_inputs"]["qubit_frequency_ghz"]
                        )
                    status, headers, body = await http.request(
                        "POST", "/api/attack/environments",
                        body={"mode": "free", "idle_noise_mode": mode, "settings": settings},
                        content_type="application/json",
                    )
                    self.assertEqual(status, 201, body)
                    self.assertEqual(set(body), {"environment_id", "attacker_credential"})
                    self.assertNotIn("victim", json.dumps(body).lower())
                    environment_id = body["environment_id"]
                    created_ids.append(environment_id)
                    cookie = headers["set-cookie"].split(";", 1)[0]
                    socket = DirectWebSocket(
                        app, f"/api/attack/live/{environment_id}/socket", cookie,
                    )
                    sockets.append(socket)
                    self.assertEqual((await socket.start())["type"], "websocket.accept")
                    await socket.send_text({
                        "type": "authenticate", "credential": body["attacker_credential"],
                    })
                    ready = json.loads((await socket.next())["text"])
                    self.assertEqual(ready["type"], "ready")
                    self.assertEqual(ready["clock_mode"], "host_clock")
                    self.assertIs(ready["pause_while_catching_up"], catch_up_pause)
                    self.assertGreaterEqual(ready["world_time_us"], 0.0)
                    await socket.send_text({
                        "type": "command", "command_id": f"submit-{mode}",
                        "operation": "program.submit", "payload": {
                            "device_id": "ibm_kingston", "shots": 2,
                            "circuit_config": {
                                "logical_qubits": 2,
                                "classical_bits": 2,
                                "initial_states": [0, 0],
                                "columns": program_columns,
                            }, "initialization": "prepare",
                        },
                    })
                    accepted = await next_type(socket, "accepted")
                    self.assertEqual(accepted["type"], "accepted")
                    self.assertGreaterEqual(accepted["issued_at_world_us"], ready["world_time_us"])
                    await socket.send_text({
                        "type": "command", "command_id": f"no-client-clock-{mode}",
                        "operation": "wait.duration", "payload": {"duration_us": 1.0},
                    })
                    unsupported = await next_type(socket, "error")
                    self.assertEqual(unsupported["code"], "OPERATION_NOT_SUPPORTED")
                    reply = await next_type(socket, "reply")
                    self.assertEqual(reply["type"], "reply")
                    self.assertIsNone(reply["error_code"], reply)
                    self.assertEqual(reply["value"]["status"], "queued")
                    if placement is not None:
                        await socket.send_text({
                            "type": "command", "command_id": f"result-{mode}",
                            "operation": "job.result.read",
                            "payload": {"job_id": reply["value"]["job_id"]},
                        })
                        await next_type(socket, "accepted")
                        result_reply = await next_type(socket, "reply")
                        self.assertIsNone(result_reply["error_code"], result_reply)
                        self.assertEqual(sum(result_reply["value"]["result"]["counts"].values()), 2)
                    deleted = await http.request(
                        "DELETE", f"/api/attack/environments/{environment_id}",
                        cookie=cookie,
                    )
                    self.assertEqual(deleted[0], 204)
                    closed = False
                    for _ in range(100):
                        if (await socket.next())["type"] == "websocket.close":
                            closed = True
                            break
                    self.assertTrue(closed)
            finally:
                for socket in sockets:
                    await socket.stop()
                for environment_id in created_ids:
                    if runtime.registry._get(environment_id) is not None:
                        await runtime.registry.discard(environment_id)

        asyncio.run(scenario())

    def test_multi_qubit_programs_execute_on_default_and_reselected_regions(self):
        """Exercise logical-to-physical binding through actual queue execution."""
        async def scenario():
            runtime = build_attack_simulator_runtime(WebOwnershipSettings.local_development())
            app = FastAPI()
            app.include_router(runtime.router)
            http = DirectHttp(app)
            sockets = []
            environment_ids = []
            snapshot = load_ibm_kingston_snapshot()
            default_region = tuple(sorted(deterministic_connected_region(snapshot, 6)))
            selected_region = tuple(sorted(deterministic_connected_region(
                snapshot, 6, excluded_qubit_ids=frozenset(default_region),
            )))

            async def next_type(socket, expected):
                for _ in range(100):
                    item = json.loads((await socket.next())["text"])
                    if item.get("type") == expected:
                        return item
                self.fail(f"did not receive WebSocket {expected}")

            try:
                await http.request("GET", "/api/attack/settings/catalog")
                for region_index, region in enumerate((default_region, selected_region)):
                    request_body = {
                        "mode": "free", "idle_noise_mode": "calibration_values",
                        "settings": {},
                    }
                    if region_index:
                        request_body["regions"] = {"shared_physical_qubit_ids": list(region)}
                    status, headers, body = await http.request(
                        "POST", "/api/attack/environments", body=request_body,
                        content_type="application/json",
                    )
                    self.assertEqual(status, 201, body)
                    environment_id = body["environment_id"]
                    environment_ids.append(environment_id)
                    cookie = headers["set-cookie"].split(";", 1)[0]
                    socket = DirectWebSocket(
                        app, f"/api/attack/live/{environment_id}/socket", cookie,
                    )
                    sockets.append(socket)
                    await socket.start()
                    await socket.send_text({
                        "type": "authenticate", "credential": body["attacker_credential"],
                    })
                    self.assertEqual(json.loads((await socket.next())["text"])["type"], "ready")

                    for width in (2, 6):
                        columns = [
                            {"step": 0, "gates": [
                                {"type": "H", "targets": [qubit]}
                                for qubit in range(width)
                            ]},
                            {"step": 1, "gates": [
                                {"type": "CNOT", "controls": [left], "targets": [right]}
                                for left, right in ((0, 1), (3, 2), (4, 5))
                                if right < width
                            ]},
                            {"step": 2, "gates": [
                                {"type": "CZ", "controls": [right], "targets": [left]}
                                for left, right in ((0, 1), (2, 3), (4, 5))
                                if right < width
                            ]},
                            {"step": 3, "gates": [
                                {"type": "MEASURE", "targets": [qubit],
                                 "classical_targets": [qubit]}
                                for qubit in range(width)
                            ]},
                        ]
                        circuit_config = {
                            "logical_qubits": width, "classical_bits": width,
                            "initial_states": [0] * width, "columns": columns,
                        }
                        entry = runtime.registry._get(environment_id)
                        service = entry.controller._world._runner._session._service
                        principal = entry.controller._world._runner._session._authority.resolve(
                            body["attacker_credential"],
                        )
                        direct_request = ProgramSubmitRequest(
                            CircuitProgram.from_editor_json(json.dumps(circuit_config)),
                            "ibm_kingston", 1, f"direct-{len(region)}-{width}",
                        )
                        prepared = service._prepare_program(principal, direct_request)
                        physical_ids = prepared.layout.physical_qubit_ids
                        self.assertEqual(physical_ids, tuple(region[:width]))
                        unitary_steps = [item for item in prepared.operation.instructions
                                         if item.kind == "unitary"]
                        pair_count = 1 if width == 2 else 3
                        self.assertEqual(len(unitary_steps), width + pair_count * 4)
                        measurements = [item for item in prepared.operation.instructions
                                        if item.kind == "measure"]
                        self.assertEqual(len(measurements), width)
                        self.assertEqual(
                            {(item.qubits[0], item.classical_targets[0]) for item in measurements},
                            {(qubit, qubit) for qubit in range(width)},
                        )
                        execution_config = (
                            runtime.resolver._free["calibration_values"].template
                            .qpu_service_config["circuit_execution"]
                        )
                        h_durations = {
                            qubit: sum(execution_config["single_qubit_native_gates"][str(physical_ids[qubit])]
                                       [native]["duration_us"] for native in ("rz", "sx", "rz"))
                            for qubit in range(width)
                        }
                        cnot_targets = (1,) if width == 2 else (1, 2, 5)
                        cnot_pairs = ((0, 1),) if width == 2 else ((0, 1), (3, 2), (4, 5))
                        cz_pairs = ((0, 1),) if width == 2 else ((0, 1), (2, 3), (4, 5))
                        cz_calibrations = execution_config["cz_gate_calibrations"]
                        cnot_cz_duration = max(
                            cz_calibrations[f"{min(physical_ids[left], physical_ids[right])}:"
                                            f"{max(physical_ids[left], physical_ids[right])}"]["duration_us"]
                            for left, right in cnot_pairs
                        )
                        direct_cz_duration = max(
                            cz_calibrations[f"{min(physical_ids[left], physical_ids[right])}:"
                                            f"{max(physical_ids[left], physical_ids[right])}"]["duration_us"]
                            for left, right in cz_pairs
                        )
                        expected_duration = (
                            max(h_durations.values())
                            + 2.0 * max(h_durations[qubit] for qubit in cnot_targets)
                            + cnot_cz_duration + direct_cz_duration
                        )
                        self.assertAlmostEqual(
                            sum(item.duration_us for item in unitary_steps), expected_duration,
                        )
                        await socket.send_text({
                            "type": "command", "command_id": f"submit-{len(region)}-{width}",
                            "operation": "program.submit", "payload": {
                                "device_id": "ibm_kingston", "shots": 1,
                                "circuit_config": circuit_config,
                                "initialization": "prepare",
                            },
                        })
                        accepted = await next_type(socket, "accepted")
                        self.assertEqual(accepted["type"], "accepted")
                        submission = await next_type(socket, "reply")
                        self.assertIsNone(submission["error_code"], submission)
                        job_id = submission["value"]["job_id"]
                        await socket.send_text({
                            "type": "command", "command_id": f"result-{len(region)}-{width}",
                            "operation": "job.result.read", "payload": {"job_id": job_id},
                        })
                        result_accept = await next_type(socket, "accepted")
                        self.assertEqual(result_accept["type"], "accepted")
                        result_reply = await next_type(socket, "reply")
                        self.assertIsNone(result_reply["error_code"], result_reply)
                        self.assertEqual(result_reply["value"]["result"]["shots"], 1)
                        self.assertEqual(sum(result_reply["value"]["result"]["counts"].values()), 1)

                    deleted = await http.request(
                        "DELETE", f"/api/attack/environments/{environment_id}", cookie=cookie,
                    )
                    self.assertEqual(deleted[0], 204)
                    environment_ids.remove(environment_id)
            finally:
                for socket in sockets:
                    await socket.stop()
                for environment_id in environment_ids:
                    if runtime.registry._get(environment_id) is not None:
                        await runtime.registry.discard(environment_id)
        asyncio.run(scenario())

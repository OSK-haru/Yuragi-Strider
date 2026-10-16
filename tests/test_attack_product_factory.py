"""End-to-end opt-in assembly from a frozen scenario, without ASGI/network."""

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from api.attack_product_factory import (
    D62_CALIBRATION_SOURCE, IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL,
    ProductAssemblyLimits, ProductEnvironmentFactory, _build_idle_model,
    standard_calibration_initial_values,
)
from api.attack_sessions import SessionHostLimits, WebOwnerContext
from api.attack_product_profiles import product_free_scenarios
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.qpu_topology import QpuTopology, TopologyLimits
from core.attack_simulation.request_receipts import ReceiptLimits
from core.attack_simulation.scenario_assumptions import AssumptionBasis, AssumptionRecord
from core.attack_simulation.scenario_compiler import ScenarioOverride
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import (
    ScenarioMode, ScenarioPolicy, ScenarioPreset, ScenarioTemplate,
    ScenarioSession,
)
from core.circuit_model import CircuitConfig
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntimeConfig
from core.physical_environment import (
    INPUT_MODE_PHYSICAL, UNIFIED_ENVIRONMENT_MODEL,
    compute_environment_rates_per_qubit,
)
from core.results import EnvironmentConfig
from core.results import SimulationConfig
from core.simulator import run_simulation
from core.experimental_isolation_physics.imperfect_reset import ImperfectResetSpec
from core.experimental_isolation_physics.reset_model import PhenomenologicalResetModel


OPS = ("device.info.read", "program.submit", "job.status.read", "job.result.read", "job.cancel")
HOST_LIMITS = ProductAssemblyLimits(
    session=SessionHostLimits(8, 4, ReceiptLimits(16, 64)),
    topology=TopologyLimits(256, 2048, 4096),
    max_runtime_jobs=32, max_events_per_job=128, max_unresolved_events=32,
    max_workload_pending=16, max_workload_history=64,
)


def _assumption(path, *, basis=AssumptionBasis.SIMULATOR_CHOICE, rationale="Explicit test configuration."):
    return AssumptionRecord(
        f"test.{path.replace('.', '_')}", (path,), basis, rationale,
        evidence_ref=(D62_CALIBRATION_SOURCE if basis is AssumptionBasis.MEASURED
                      or "Derived" in rationale else None),
    )


def prepared_environment(mode=ScenarioMode.FREE, *, malformed_idle=False,
                         idle_input_mode=IDLE_INPUT_CALIBRATION):
    standard = standard_calibration_initial_values()["values"]
    val = lambda name: standard[name]["value"]
    model = PhenomenologicalResetModel(
        ImperfectResetSpec(val("reset_success_probability"), (1.0, 0.0)),
        observed_one_given_zero=val("readout_p1_given_0"),
        observed_zero_given_one=val("readout_p0_given_1"),
    )
    runtime = SharedQpuRuntimeConfig(
        reset_model_id=model.model_id,
        reset_model_configuration_id=model.configuration_id,
        reset_count=1, reset_duration_us=val("reset_duration_us"),
        victim_operation_duration_us=3.0,
        attacker_measurement_duration_us=val("measurement_duration_us"),
        queue_policy_id="on_demand_fifo_v1", cancellation_policy_id="queued_only",
        measure_input_state_before_transition=True,
        placement_policy="guaranteed_same_physical_qubit",
        isolated_assignment_duration_us=0.0,
        resource_binding=None,
        program_measurement_duration_us=val("measurement_duration_us"),
        max_local_qubits=2, max_execution_work=1_000_000,
    )
    topology_limits = TopologyLimits(256, 2048, 4096)
    topology = QpuTopology("qpu-1", "topology-test-v1", tuple(range(150, 250)), topology_limits)
    unitary_model = "unsupported_for_test" if malformed_idle else "independent_constant_rate_v1"
    if idle_input_mode == IDLE_INPUT_CALIBRATION:
        idle_dynamics = {
            "model_id": unitary_model,
            "input_mode": IDLE_INPUT_CALIBRATION,
            "qubits": {
                "150": {"t1_us": val("t1_us"), "t2_us": val("t2_us_source"),
                        "equilibrium_excited_population": val("thermal_equilibrium_excited_population")},
                "151": {"t1_us": 250.0, "t2_us": 250.0,
                        "equilibrium_excited_population": 0.02},
            },
        }
    else:
        idle_dynamics = {
            "model_id": unitary_model,
            "input_mode": IDLE_INPUT_PHYSICAL,
            "physical_inputs": {
                "environment_model": UNIFIED_ENVIRONMENT_MODEL,
                "device_quality": 0.8, "temperature_mk": 15.0,
                "flux_noise_phi0": 1.0e-6, "qubit_frequency_ghz": 5.0,
                "t1_max_us": 100.0, "tphi_max_us": 100.0,
                "per_qubit": {"151": {"t1_max_us": 45.0, "tphi_max_us": 250.0}},
            },
        }
    qpu = {
        "execution_regime": "nisq_physical",
        "supported_operations": list(OPS),
        "runtime_execution": runtime.to_manifest_config(),
        "circuit_execution": {
            "compiler_id": "local_sampling_v1", "supported_gates": ["H", "MEASURE"],
            "max_program_qubits": 2, "max_shots": 32,
            "max_instruction_executions": 256, "gate_durations_us": {"H": val("single_qubit_gate_duration_us")},
        },
        "topology": topology.to_config(), "retained_region": {"physical_qubit_ids": [150, 151]},
        "initial_state": {
            "device_config_version": "test-initial-state-v1",
            "density_matrix": [
                [[1, 0], [0, 0], [0, 0], [0, 0]],
                [[0, 0], [0, 0], [0, 0], [0, 0]],
                [[0, 0], [0, 0], [0, 0], [0, 0]],
                [[0, 0], [0, 0], [0, 0], [0, 0]],
            ],
        },
        "reset_model": {
            "model_id": model.model_id,
            "success_probability": val("reset_success_probability"),
            "target_populations": [1.0, 0.0],
            "observed_one_given_zero": val("readout_p1_given_0"),
            "observed_zero_given_one": val("readout_p0_given_1"),
            "applies_to_physical_qubit_ids": [150, 151],
        },
        "idle_dynamics": idle_dynamics,
        "declared_service_timing": {
            "processing_delay_us": 0.0, "publication_delay_us": 0.0,
            "denial_delay_us": 0.0, "basis": "Explicit deterministic test timing.",
        },
        "live_ingress": {
            "receipt_origin": "authenticated_complete_envelope", "outbound_delay_us": 0.0,
            "basis": "Explicit deterministic test ingress timing.",
        },
        "control_timing": {
            "processing_delay_us": 0.0, "publication_delay_us": 0.0,
            "basis": "Explicit deterministic test control timing.", "cancellation_policy": "queued_only",
        },
    }
    experiment = {
        "world_clock": {"clock_domain": "scenario-world", "rate": 1.0},
        "live_time_policy": {"mode": "explicit_wait", "max_wait_us": 100.0, "max_steps_per_wait": 128},
    }
    relation = {"actor_tenants": {"attacker": "tenant-a", "victim": "tenant-v"}}
    attacker = {"principal_id": "attacker", "tenant_id": "tenant-a", "operation_grants": list(OPS)}
    victim = {"principal_id": "victim", "tenant_id": "tenant-v", "operation_grants": list(OPS)}
    editable = ("qpu_service.runtime_execution.reset_duration_us",)
    if idle_input_mode == IDLE_INPUT_PHYSICAL:
        editable += (
            "qpu_service.idle_dynamics.physical_inputs.temperature_mk",
            "qpu_service.idle_dynamics.physical_inputs.device_quality",
            "qpu_service.idle_dynamics.physical_inputs.flux_noise_phi0",
            "qpu_service.idle_dynamics.physical_inputs.qubit_frequency_ghz",
        )
    template = ScenarioTemplate(
        "product-factory-test", "1", attacker, victim, qpu, relation, experiment,
        free_override_fields=editable,
    )
    locked = (
        "attacker.operation_grants", "victim.operation_grants", "qpu_service.supported_operations",
        "qpu_service.runtime_execution.reset_duration_us",
    )
    policy = ScenarioPolicy(
        ScenarioMode.CHALLENGE, locked_fields=locked,
        allowed_operations=OPS,
    )
    assumptions = [
        _assumption("qpu_service.reset_model.success_probability", basis=AssumptionBasis.SIMULATOR_CHOICE,
                    rationale="Derived approximation: 1 - mean of the two measured readout assignment errors."),
        _assumption("qpu_service.reset_model.target_populations"),
        _assumption("qpu_service.reset_model.applies_to_physical_qubit_ids",
                    rationale="Explicit homogeneous model application to qubits 150 and 151; D62 remains single-qubit evidence only."),
        _assumption("qpu_service.reset_model.observed_one_given_zero", basis=AssumptionBasis.MEASURED),
        _assumption("qpu_service.reset_model.observed_zero_given_one", basis=AssumptionBasis.MEASURED),
        _assumption("qpu_service.runtime_execution.reset_duration_us", basis=AssumptionBasis.MEASURED),
        _assumption("qpu_service.runtime_execution.victim_operation_duration_us"),
        _assumption("qpu_service.runtime_execution.attacker_measurement_duration_us", basis=AssumptionBasis.MEASURED),
        _assumption("qpu_service.runtime_execution.program_measurement_duration_us", basis=AssumptionBasis.MEASURED),
        _assumption("qpu_service.runtime_execution.repetition_delay_us"),
        _assumption("qpu_service.declared_service_timing.processing_delay_us"),
        _assumption("qpu_service.declared_service_timing.publication_delay_us"),
        _assumption("qpu_service.declared_service_timing.denial_delay_us"),
        _assumption("qpu_service.live_ingress.outbound_delay_us"),
        _assumption("qpu_service.control_timing.processing_delay_us"),
        _assumption("qpu_service.control_timing.publication_delay_us"),
        _assumption("qpu_service.initial_state.density_matrix"),
        _assumption("qpu_service.topology"),
        _assumption("qpu_service.idle_dynamics.input_mode",
                    rationale=f"Preset-selected mutually exclusive {idle_input_mode} input mode."),
        _assumption("qpu_service.circuit_execution.gate_durations_us.H", basis=AssumptionBasis.MEASURED),
        _assumption("experiment.world_clock.rate"),
    ]
    if idle_input_mode == IDLE_INPUT_CALIBRATION:
        assumptions.extend([
            _assumption("qpu_service.idle_dynamics.qubits.150.t1_us", basis=AssumptionBasis.MEASURED),
            _assumption("qpu_service.idle_dynamics.qubits.150.t2_us", basis=AssumptionBasis.MEASURED),
            _assumption("qpu_service.idle_dynamics.qubits.150.equilibrium_excited_population",
                        rationale="Editable zero-population assumption; absent from the calibration data."),
            _assumption("qpu_service.idle_dynamics.qubits.151.t1_us"),
            _assumption("qpu_service.idle_dynamics.qubits.151.t2_us"),
            _assumption("qpu_service.idle_dynamics.qubits.151.equilibrium_excited_population"),
        ])
    else:
        physical_paths = (
            "environment_model", "device_quality", "temperature_mk", "flux_noise_phi0",
            "qubit_frequency_ghz", "t1_max_us", "tphi_max_us",
        )
        assumptions.extend(_assumption(
            f"qpu_service.idle_dynamics.physical_inputs.{field}",
            rationale="Explicit physical-mode fixture input.",
        ) for field in physical_paths)
        assumptions.extend([
            _assumption("qpu_service.idle_dynamics.physical_inputs.per_qubit.151.t1_max_us"),
            _assumption("qpu_service.idle_dynamics.physical_inputs.per_qubit.151.tphi_max_us"),
        ])
    session = ScenarioPreset(template, policy, tuple(assumptions)).materialize(mode)
    return ScenarioEnvironment(session)


class ProductFactoryTests(unittest.TestCase):
    def test_product_dedicated_profile_keeps_two_six_qubit_registers_not_one_twelve_qubit_matrix(self):
        definition = product_free_scenarios()[IDLE_INPUT_CALIBRATION]
        environment = ScenarioEnvironment(ScenarioSession(
            definition.template,
            ScenarioPolicy(ScenarioMode.FREE, allowed_operations=definition.allowed_operations),
            definition.assumptions,
        ))
        environment.set_assumption(ScenarioOverride(
            "qpu_service.runtime_execution.placement_policy", "tenant_dedicated_region",
            "User-selected scenario placement, not a cloud placement guarantee.",
        ))
        product = self.factory.assemble(environment, WebOwnerContext())
        try:
            runtime = product.controller._world._executor.runtime
            store = runtime.state_store
            self.assertEqual(len(store.read().density_matrix), 64)
            self.assertEqual(len(store.inactive_region().density_matrix), 64)
            self.assertEqual(len(store.regional_bindings), 2)
            self.assertFalse(set(runtime.tenant_regions["tenant-victim"])
                             & set(runtime.tenant_regions["tenant-attacker"]))
        finally:
            product.close()

    def test_alternate_region_calibration_identity_mismatch_rejects_assembly(self):
        definition = product_free_scenarios()[IDLE_INPUT_CALIBRATION]
        qpu = dict(definition.template.qpu_service_config)
        alternate = dict(qpu["dedicated_region"])
        alternate["reset_model_configuration_id"] = "sha256:" + "0" * 64
        qpu["dedicated_region"] = alternate
        template = replace(definition.template, qpu_service_config=qpu)
        environment = ScenarioEnvironment(ScenarioSession(
            template,
            ScenarioPolicy(ScenarioMode.FREE, allowed_operations=definition.allowed_operations),
            definition.assumptions,
        ))
        with self.assertRaisesRegex(ValueError, "alternate reset model differs"):
            self.factory.assemble(environment, WebOwnerContext())

    def setUp(self):
        self.factory = ProductEnvironmentFactory(HOST_LIMITS)

    def test_both_modes_assemble_submit_wait_reply_and_discard(self):
        circuit = CircuitProgram.from_editor_json(
            '{"logical_qubits":2,"classical_bits":2,"initial_states":[0,0],"columns":['
            '{"step":0,"gates":[{"type":"H","targets":[0]}]},'
            '{"step":1,"gates":[{"type":"MEASURE","targets":[0],"classical_targets":[0]}]}]}'
        )
        for mode in ScenarioMode:
            for input_mode in (IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL):
                with self.subTest(mode=mode, input_mode=input_mode):
                    env = prepared_environment(mode, idle_input_mode=input_mode)
                    owner = WebOwnerContext()
                    product = self.factory.assemble(env, owner)
                    controller = product.controller
                    self.assertEqual(controller.now(), 0.0)
                    receipt = controller.submit(
                        product.attacker_credential,
                        ProgramSubmitRequest(circuit, "qpu-1", 2, f"key-{mode.value}-{input_mode}"),
                        owner_context=owner,
                    )
                    self.assertEqual(controller.now(), 0.0)  # submit itself does not advance D60 time
                    controller.wait(product.attacker_credential, 10.0,
                                    owner_context=owner)
                    reply = controller.reply(product.attacker_credential, receipt,
                                             owner_context=owner)
                    self.assertIsNone(reply.error_code)
                    self.assertEqual(reply.value.status.value, "queued")
                    status_receipt = controller.status(
                        product.attacker_credential, reply.value.job_id,
                        owner_context=owner,
                    )
                    controller.wait(product.attacker_credential, 1.0,
                                    owner_context=owner)
                    status_reply = controller.reply(
                        product.attacker_credential, status_receipt,
                        owner_context=owner,
                    )
                    self.assertIsNone(status_reply.error_code)
                    self.assertEqual(status_reply.value.status.value, "succeeded")
                    product.close()
                    with self.assertRaises(Exception):
                        controller.now()

    def test_free_override_is_frozen_and_challenge_locked_value_cannot_change(self):
        free = prepared_environment(ScenarioMode.FREE)
        free.set_assumption(ScenarioOverride(
            "qpu_service.runtime_execution.reset_duration_us", 3.25,
            "User-selected free-mode value.",
        ))
        product = self.factory.assemble(free, WebOwnerContext())
        self.assertEqual(free.establish().qpu_service_config["runtime_execution"]["reset_duration_us"], 3.25)
        self.assertEqual(product.manifest_id, free.establish().manifest_id)
        product.close()

        challenge = prepared_environment(ScenarioMode.CHALLENGE)
        with self.assertRaisesRegex(PermissionError, "not editable in the selected mode"):
            challenge.set_assumption(ScenarioOverride(
                "qpu_service.runtime_execution.reset_duration_us", 3.25,
                "Must be fixed by this preset.",
            ))
        product = self.factory.assemble(challenge, WebOwnerContext())
        self.assertEqual(challenge.establish().qpu_service_config["runtime_execution"]["reset_duration_us"], 2.212)
        product.close()

    def test_d62_values_and_provenance_are_explicit_not_runtime_fallbacks(self):
        values = standard_calibration_initial_values()
        self.assertEqual(values["source"], D62_CALIBRATION_SOURCE)
        self.assertIn("one physical qubit only", values["scope_notice"])
        self.assertAlmostEqual(values["values"]["reset_success_probability"]["value"], 0.992919921875)
        self.assertIn("Derived approximation", values["values"]["reset_success_probability"]["basis"])
        self.assertIn("Editable zero-population assumption",
                      values["values"]["thermal_equilibrium_excited_population"]["basis"])

        env = prepared_environment()
        product = self.factory.assemble(env, WebOwnerContext())
        manifest = env.establish()
        self.assertEqual(manifest.qpu_service_config["runtime_execution"]["reset_duration_us"], 2.212)
        self.assertAlmostEqual(manifest.qpu_service_config["runtime_execution"]["program_measurement_duration_us"], 2.180)
        idle = manifest.qpu_service_config["idle_dynamics"]
        self.assertEqual(idle["input_mode"], IDLE_INPUT_CALIBRATION)
        self.assertEqual(idle["qubits"]["150"]["t1_us"],
                         values["values"]["t1_us"]["value"])
        self.assertEqual(idle["qubits"]["150"]["t2_us"],
                         values["values"]["t2_us_source"]["value"])
        self.assertNotIn("physical_inputs", idle)
        self.assertTrue(any("Derived approximation" in item.rationale
                            for item in manifest.assumptions
                            if any(path.endswith("reset_model.success_probability")
                                   for path in item.target_paths)))
        t1_basis = next(item for item in manifest.assumptions
                        if any(path.endswith("idle_dynamics.qubits.150.t1_us")
                               for path in item.target_paths))
        t2_basis = next(item for item in manifest.assumptions
                        if any(path.endswith("idle_dynamics.qubits.150.t2_us")
                               for path in item.target_paths))
        self.assertEqual(t1_basis.evidence_ref, D62_CALIBRATION_SOURCE)
        self.assertEqual(t2_basis.evidence_ref, D62_CALIBRATION_SOURCE)
        self.assertEqual(product.mode, ScenarioMode.FREE)
        product.close()

    def test_calibration_catalog_matches_the_single_source_json(self):
        calibration_path = Path(__file__).resolve().parents[1] / D62_CALIBRATION_SOURCE
        source = json.loads(calibration_path.read_text(encoding="utf-8"))
        properties = source["qubit_properties"]
        values = standard_calibration_initial_values()["values"]
        self.assertEqual(values["t1_us"]["value"], properties["T1"]["value"])
        self.assertEqual(values["t2_us_source"]["value"], properties["T2"]["value"])
        expected_tphi = 1.0 / (1.0 / properties["T2"]["value"]
                               - 1.0 / (2.0 * properties["T1"]["value"]))
        self.assertEqual(values["tphi_us"]["value"], expected_tphi)
        self.assertEqual(values["readout_p1_given_0"]["value"], properties["prob_meas1_prep0"]["value"])
        self.assertEqual(values["readout_p0_given_1"]["value"], properties["prob_meas0_prep1"]["value"])
        self.assertEqual(values["readout_error"]["value"], properties["readout_error"]["value"])
        self.assertEqual(values["measurement_duration_us"]["value"],
                         properties["readout_length"]["value"] / 1000.0)
        gate_properties = {entry["gate"]: entry["parameters"]["gate_length"]["value"]
                           for entry in source["single_qubit_gate_properties"]}
        reset_properties = gate_properties
        self.assertEqual(values["single_qubit_gate_duration_us"]["value"], gate_properties["id"] / 1000.0)
        self.assertEqual(values["reset_duration_us"]["value"], reset_properties["reset"] / 1000.0)

    def test_physical_parameter_mode_matches_existing_rate_derivation_and_trends(self):
        env = prepared_environment(idle_input_mode=IDLE_INPUT_PHYSICAL)
        manifest = env.establish()
        raw = manifest.qpu_service_config["idle_dynamics"]
        physical_inputs = raw["physical_inputs"]
        idle_model = _build_idle_model(raw, (150, 151), 2)
        expected_environment = EnvironmentConfig(
            input_mode=INPUT_MODE_PHYSICAL,
            device_quality=physical_inputs["device_quality"],
            temperature_mk=physical_inputs["temperature_mk"],
            flux_noise_phi0=physical_inputs["flux_noise_phi0"],
            qubit_frequency_ghz=physical_inputs["qubit_frequency_ghz"],
            t1_max_us=physical_inputs["t1_max_us"],
            tphi_max_us=physical_inputs["tphi_max_us"],
            per_qubit=[physical_inputs["per_qubit"].get(str(q), {}) for q in (150, 151)],
            ideal_reference=False,
        )
        rates = compute_environment_rates_per_qubit(expected_environment, 2)
        for spec, rate in zip(idle_model.qubits, rates, strict=True):
            population_rate = rate.gamma_down_per_us + rate.gamma_up_per_us
            self.assertAlmostEqual(spec.t1_us, 1.0 / population_rate)
            self.assertAlmostEqual(spec.tphi_us, 1.0 / rate.gamma_phi_per_us)
            self.assertAlmostEqual(spec.equilibrium_excited_population,
                                   rate.gamma_up_per_us / population_rate)
        self.assertNotEqual(idle_model.qubits[0].t1_us, idle_model.qubits[1].t1_us)

        body_result = run_simulation(SimulationConfig(
            circuit=CircuitConfig(logical_qubits=2, initial_states=["0", "0"], columns=[]),
            environment=expected_environment, duration_us=1.0, time_steps=2,
        ))
        body_rates = body_result.derived_parameters["per_qubit"]
        for rate, body_rate in zip(rates, body_rates, strict=True):
            self.assertAlmostEqual(rate.gamma_down_per_us, body_rate["gamma_down_per_us"])
            self.assertAlmostEqual(rate.gamma_up_per_us, body_rate["gamma_up_per_us"])
            self.assertAlmostEqual(rate.gamma_phi_per_us, body_rate["gamma_phi_per_us"])

        def build_with(**changes):
            changed = dict(physical_inputs)
            changed.update(changes)
            return _build_idle_model({**raw, "physical_inputs": changed}, (150, 151), 2)

        colder = build_with(temperature_mk=5.0)
        hotter = build_with(temperature_mk=30.0)
        self.assertGreater(hotter.qubits[0].equilibrium_excited_population,
                           colder.qubits[0].equilibrium_excited_population)
        low_flux = build_with(flux_noise_phi0=1.0e-6)
        high_flux = build_with(flux_noise_phi0=1.0e-5)
        self.assertLess(high_flux.qubits[0].tphi_us, low_flux.qubits[0].tphi_us)

    def test_wait_noise_modes_are_exclusive_and_only_selected_values_are_frozen(self):
        calibration = prepared_environment().establish().qpu_service_config["idle_dynamics"]
        with self.assertRaisesRegex(ValueError, "only calibration qubit values"):
            _build_idle_model({**calibration, "physical_inputs": {}}, (150, 151), 2)

        physical = prepared_environment(idle_input_mode=IDLE_INPUT_PHYSICAL)
        physical.set_assumption(ScenarioOverride(
            "qpu_service.idle_dynamics.physical_inputs.temperature_mk", 24.0,
            "Free-mode temperature adjustment.",
        ))
        manifest = physical.establish()
        frozen_idle = manifest.qpu_service_config["idle_dynamics"]
        self.assertEqual(frozen_idle["physical_inputs"]["temperature_mk"], 24.0)
        self.assertNotIn("qubits", frozen_idle)

        locked_challenge = prepared_environment(ScenarioMode.CHALLENGE,
                                                idle_input_mode=IDLE_INPUT_PHYSICAL)
        with self.assertRaisesRegex(PermissionError, "not editable in the selected mode"):
            locked_challenge.set_assumption(ScenarioOverride(
                "qpu_service.idle_dynamics.physical_inputs.temperature_mk", 24.0,
                "The preset fixes its noise assumptions.",
            ))
        challenge_values = locked_challenge.establish().qpu_service_config["idle_dynamics"]
        self.assertEqual(challenge_values["input_mode"], IDLE_INPUT_PHYSICAL)
        self.assertEqual(challenge_values["physical_inputs"]["temperature_mk"], 15.0)

        with self.assertRaisesRegex(ValueError, "only physical inputs"):
            _build_idle_model({**frozen_idle, "qubits": {}}, (150, 151), 2)

        # Calibration mode has no physical-input fields, so no temperature or
        # flux value can silently affect its frozen T1/T2 entries.
        self.assertEqual(calibration["input_mode"], IDLE_INPUT_CALIBRATION)
        self.assertNotIn("physical_inputs", calibration)

    def test_invalid_manifest_and_host_capacity_fail_before_runtime_parts_exist(self):
        bad = prepared_environment(malformed_idle=True)
        with patch("api.attack_product_factory.VirtualCredentialAuthority") as authority:
            with self.assertRaisesRegex(ValueError, "waiting physics"):
                self.factory.assemble(bad, WebOwnerContext())
            authority.assert_not_called()

        too_small = ProductEnvironmentFactory(replace_topology_limit(HOST_LIMITS, 10))
        with patch("api.attack_product_factory.VirtualCredentialAuthority") as authority:
            with self.assertRaisesRegex(ValueError, "qubit"):
                too_small.assemble(prepared_environment(), WebOwnerContext())
            authority.assert_not_called()

    def test_failure_after_world_start_revokes_and_drains_every_partial_component(self):
        from core.attack_simulation.virtual_credentials import VirtualCredentialAuthority
        authority = VirtualCredentialAuthority()
        with patch("api.attack_product_factory.VirtualCredentialAuthority", return_value=authority), \
             patch("api.attack_product_factory.PausedWorldController.from_manifest",
                   side_effect=RuntimeError("injected final assembly failure")):
            with self.assertRaisesRegex(RuntimeError, "injected final assembly failure"):
                self.factory.assemble(prepared_environment(), WebOwnerContext())
        self.assertTrue(authority._closed)
        with self.assertRaises(PermissionError):
            authority.resolve("invalid")


def replace_topology_limit(limits, max_qubits):
    return ProductAssemblyLimits(
        session=limits.session,
        topology=TopologyLimits(max_qubits, limits.topology.max_relations,
                                limits.topology.max_group_memberships),
        max_runtime_jobs=limits.max_runtime_jobs,
        max_events_per_job=limits.max_events_per_job,
        max_unresolved_events=limits.max_unresolved_events,
        max_workload_pending=limits.max_workload_pending,
        max_workload_history=limits.max_workload_history,
    )

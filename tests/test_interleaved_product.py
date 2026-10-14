"""Product-path interleaving: settings, live world, hidden secret and free-mode reproduction."""

import json
import time
import unittest

from api.attack_product_factory import ProductAssemblyLimits, ProductEnvironmentFactory
from api.attack_product_profiles import ABSTRACT_INTERLEAVE, product_free_scenarios
from api.attack_sessions import SessionHostLimits, WebOwnerContext
from core.attack_simulation.qpu_topology import TopologyLimits
from api.attack_settings_resolver import ScenarioSettingsResolver
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.job_service import JobResultNotReadyError
from core.attack_simulation.request_receipts import ReceiptLimits
from tests.test_local_program_runtime import program_json

POLL_SECONDS = 20.0
HOST_LIMITS = ProductAssemblyLimits(
    session=SessionHostLimits(8, 4, ReceiptLimits(16, 64)),
    topology=TopologyLimits(256, 2048, 4096),
    max_runtime_jobs=32, max_events_per_job=1100, max_unresolved_events=32,
    max_workload_pending=16, max_workload_history=64,
)
ROUNDS = "attacker.interleaved_access.max_rounds_per_query"


def resolver():
    return ScenarioSettingsResolver(free_scenarios=product_free_scenarios())


def settings_request(settings, mode="calibration_values"):
    return json.dumps({"mode": "free", "idle_noise_mode": mode, "settings": settings}).encode()


def first_reset_qubit(catalog_resolver):
    manifest = catalog_resolver.resolve(settings_request({})).establish()
    return manifest.qpu_service_config["retained_region"]["physical_qubit_ids"][0]


def await_reply(controller, credential, receipt, owner):
    deadline = time.monotonic() + POLL_SECONDS
    while True:
        try:
            return controller.reply(credential, receipt, owner_context=owner)
        except JobResultNotReadyError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def observe(settings, reference, rounds, key, program=None):
    """Run one interleaved observation through the real product assembly."""
    environment = resolver().resolve(settings_request({ABSTRACT_INTERLEAVE: True, **settings}))
    owner = WebOwnerContext()
    product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
    try:
        controller, credential = product.controller, product.attacker_credential
        request = ProgramSubmitRequest(CircuitProgram.from_editor_json(program or program_json(1)),
                                       "ibm_kingston", rounds, key, "inherit", interleave=reference)
        reply = await_reply(controller, credential,
                            controller.submit(credential, request, owner_context=owner), owner)
        if reply.error_code is not None:
            return reply
        job_id = reply.value.job_id
        deadline = time.monotonic() + POLL_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.05)
            status = await_reply(controller, credential,
                                 controller.status(credential, job_id, owner_context=owner), owner)
            if status.value.status.value == "succeeded":
                return await_reply(controller, credential,
                                   controller.result(credential, job_id, owner_context=owner), owner)
        raise AssertionError("interleaved job did not finish")
    finally:
        product.close()


def one_frequency(result):
    counts = result.value.result["counts"]
    return counts.get("1", 0) / sum(counts.values())


class InterleavedProductTests(unittest.TestCase):
    def test_free_mode_exposes_the_victim_attacker_and_reset_assumptions(self):
        catalog = resolver().catalog()
        fields = {item["path"]: item for item in catalog["free_mode"]["idle_noise_modes"][0]["fields"]}
        self.assertEqual(fields["victim.interleaved_run.secret_bit"]["default"], 1)
        self.assertEqual(fields["attacker.interleaved_access.max_rounds_per_query"]["maximum"], 256)
        qid = first_reset_qubit(resolver())
        reset = fields[f"qpu_service.reset_model.qubits.{qid}.success_probability"]
        self.assertTrue(0.0 < reset["default"] < 1.0)
        self.assertIn("実測値ではありません", reset["description"])

    def test_the_manifest_records_budget_and_recomputed_reset_digest(self):
        qid = first_reset_qubit(resolver())
        base = resolver().resolve(settings_request({})).establish()
        edited = resolver().resolve(settings_request({
            f"qpu_service.reset_model.qubits.{qid}.success_probability": 0.9,
            "attacker.interleaved_access.max_rounds_per_query": 32,
        })).establish()
        self.assertEqual(edited.observation_budget.shots_per_query, 32)
        self.assertNotEqual(
            base.qpu_service_config["runtime_execution"]["reset_model_configuration_id"],
            edited.qpu_service_config["runtime_execution"]["reset_model_configuration_id"])
        with self.assertRaises(ValueError):
            resolver().resolve(settings_request({
                "qpu_service.runtime_execution.reset_model_configuration_id": "sha256:" + "0" * 64}))

    def test_the_victim_bit_leaks_through_an_imperfect_reset_in_free_mode(self):
        qid = first_reset_qubit(resolver())
        weak = {f"qpu_service.reset_model.qubits.{qid}.success_probability": 0.80, ROUNDS: 200}
        zero = one_frequency(observe({**weak, "victim.interleaved_run.secret_bit": 0}, "victim", 200, "zero"))
        one = one_frequency(observe({**weak, "victim.interleaved_run.secret_bit": 1}, "victim", 200, "one"))
        self.assertGreater(one - zero, 0.10)

    def test_a_perfect_reset_removes_the_dependence_on_the_victim_bit(self):
        qid = first_reset_qubit(resolver())
        perfect = {f"qpu_service.reset_model.qubits.{qid}.success_probability": 1.0, ROUNDS: 200}
        one = one_frequency(observe({**perfect, "victim.interleaved_run.secret_bit": 1}, "victim", 60, "perfect"))
        self.assertLess(one, 0.1)

    def test_known_label_training_matches_the_real_victim_with_the_same_bit(self):
        qid = first_reset_qubit(resolver())
        weak = {f"qpu_service.reset_model.qubits.{qid}.success_probability": 0.80,
                "victim.interleaved_run.secret_bit": 1, ROUNDS: 200}
        trained = one_frequency(observe(weak, "training:1", 200, "train"))
        real = one_frequency(observe(weak, "victim", 200, "real"))
        self.assertAlmostEqual(trained, real, delta=0.15)

    def test_a_wider_attacker_circuit_still_observes_the_shared_qubit(self):
        qid = first_reset_qubit(resolver())
        weak = {f"qpu_service.reset_model.qubits.{qid}.success_probability": 0.80,
                "victim.interleaved_run.secret_bit": 1, ROUNDS: 200}
        wide = program_json(2, measurements=[(0, 0)], classical_bits=1)
        one = one_frequency(observe(weak, "training:1", 200, "wide", program=wide))
        zero = one_frequency(observe(weak, "training:0", 200, "wide0", program=wide))
        self.assertGreater(one - zero, 0.10)

    def test_the_abstract_command_is_off_by_default_and_the_service_does_not_offer_it(self):
        off = resolver().resolve(settings_request({})).establish()
        self.assertFalse(off.qpu_service_config["abstract_commands"]["interleaved_observation"])
        self.assertNotIn("program.interleave.victim", off.qpu_service_config["supported_operations"])
        self.assertIn("program.submit", off.qpu_service_config["supported_operations"])
        on = resolver().resolve(settings_request({ABSTRACT_INTERLEAVE: True})).establish()
        self.assertTrue(on.qpu_service_config["abstract_commands"]["interleaved_observation"])
        self.assertIn("program.interleave.victim", on.qpu_service_config["supported_operations"])
        # With the command off, an interleaved request is denied even though the circuit is fine.
        environment = resolver().resolve(settings_request({}))
        owner = WebOwnerContext()
        product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
        try:
            request = ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(1)),
                                           "ibm_kingston", 4, "disabled", "inherit", interleave="victim")
            reply = await_reply(product.controller, product.attacker_credential, product.controller.submit(
                product.attacker_credential, request, owner_context=owner), owner)
            self.assertIsNotNone(reply.error_code)
        finally:
            product.close()

    def test_dedicated_regions_reject_interleaving_with_a_public_reason(self):
        environment = resolver().resolve(settings_request({
            ABSTRACT_INTERLEAVE: True,
            "qpu_service.runtime_execution.placement_policy": "tenant_dedicated_region"}))
        owner = WebOwnerContext()
        product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
        try:
            request = ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(1)),
                                           "ibm_kingston", 4, "dedicated", "inherit", interleave="victim")
            receipt = product.controller.submit(product.attacker_credential, request, owner_context=owner)
            reply = await_reply(product.controller, product.attacker_credential, receipt, owner)
            self.assertEqual(reply.error_code, "INVALID_PROGRAM")
            self.assertIn("same-region placement", json.dumps(reply.value))
        finally:
            product.close()


if __name__ == "__main__":
    unittest.main()

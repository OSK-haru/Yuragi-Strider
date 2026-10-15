"""Victim activity on the product path: free mode opt-in, never in a challenge."""

import json
import time
import unittest

from api.attack_challenge import reset_leakage_challenge, reset_leakage_message_challenge
from api.attack_product_factory import ProductEnvironmentFactory
from api.attack_product_profiles import ABSTRACT_INTERLEAVE, ABSTRACT_VICTIM_ACTIVITY
from api.attack_sessions import WebOwnerContext
from api.attack_settings_resolver import ScenarioSettingsResolver
from api.attack_product_profiles import product_free_scenarios
from core.attack_simulation.job_service import JobResultNotReadyError
from core.attack_simulation.program_request import (
    VICTIM_ACTIVITY_OPERATION_ID, CircuitProgram, ProgramSubmitRequest,
)
from tests.test_interleaved_product import HOST_LIMITS, POLL_SECONDS, await_reply, settings_request
from tests.test_local_program_runtime import program_json


def free(settings):
    resolver = ScenarioSettingsResolver(free_scenarios=product_free_scenarios())
    return resolver.resolve(settings_request(settings))


def run_to_activity(settings, reference="victim", rounds=3):
    environment = free(settings)
    owner = WebOwnerContext()
    product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
    try:
        controller, credential = product.controller, product.attacker_credential
        request = ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(1)), "ibm_kingston",
                                       rounds, "act", "inherit", interleave=reference)
        reply = await_reply(controller, credential, controller.submit(credential, request, owner_context=owner), owner)
        job_id = reply.value.job_id
        deadline = time.monotonic() + POLL_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.05)
            status = await_reply(controller, credential, controller.status(credential, job_id, owner_context=owner), owner)
            if status.value.status.value == "succeeded":
                break
        return await_reply(controller, credential,
                           controller.victim_activity(credential, job_id, owner_context=owner), owner)
    finally:
        product.close()


class VictimActivityProductTests(unittest.TestCase):
    def test_it_is_off_by_default_and_added_only_by_its_own_setting(self):
        off = free({}).establish()
        self.assertFalse(off.qpu_service_config["abstract_commands"]["victim_activity"])
        self.assertNotIn(VICTIM_ACTIVITY_OPERATION_ID, off.qpu_service_config["supported_operations"])
        both = free({ABSTRACT_INTERLEAVE: True, ABSTRACT_VICTIM_ACTIVITY: True}).establish()
        self.assertIn(VICTIM_ACTIVITY_OPERATION_ID, both.qpu_service_config["supported_operations"])
        self.assertIn("program.interleave.victim", both.qpu_service_config["supported_operations"])
        # Each command adds only its own operations.
        only = free({ABSTRACT_VICTIM_ACTIVITY: True}).establish()
        self.assertIn(VICTIM_ACTIVITY_OPERATION_ID, only.qpu_service_config["supported_operations"])
        self.assertNotIn("program.interleave.victim", only.qpu_service_config["supported_operations"])
        interleave_only = free({ABSTRACT_INTERLEAVE: True}).establish()
        self.assertNotIn(VICTIM_ACTIVITY_OPERATION_ID, interleave_only.qpu_service_config["supported_operations"])

    def test_the_enabled_service_answers_over_the_live_path_with_a_timed_trace(self):
        reply = run_to_activity({ABSTRACT_INTERLEAVE: True, ABSTRACT_VICTIM_ACTIVITY: True})
        self.assertIsNone(reply.error_code, reply)
        activity = reply.value
        self.assertEqual((activity["reference"], activity["victim_prepared_bit"]), ("victim", 1))
        self.assertEqual(activity["shots_total"], 3)
        kinds = [step["kind"] for step in activity["shots"][0]["steps"]]
        # On a live clock the first shot may start with the wait before the job began.
        self.assertEqual([kind for kind in kinds if kind != "idle"],
                         ["victim_run", "service_reset", "attacker_measure"])
        self.assertNotIn("idle", kinds[1:])

    def test_the_prepared_label_follows_the_authors_secret_bit(self):
        zero = run_to_activity({ABSTRACT_INTERLEAVE: True, ABSTRACT_VICTIM_ACTIVITY: True,
                                "victim.interleaved_run.secret_bit": 0})
        self.assertEqual(zero.value["victim_prepared_bit"], 0)

    def test_the_command_is_denied_unless_its_own_setting_is_on(self):
        reply = run_to_activity({ABSTRACT_INTERLEAVE: True})
        self.assertEqual(reply.error_code, "OPERATION_DENIED")

    def test_neither_challenge_ever_offers_it(self):
        for challenge in (reset_leakage_challenge(), reset_leakage_message_challenge()):
            preset = challenge.preset
            session = preset.materialize(__import__("core.attack_simulation.scenario_policy",
                                                    fromlist=["ScenarioMode"]).ScenarioMode.CHALLENGE)
            template = session.template
            supported = template.qpu_service_config["supported_operations"]
            self.assertNotIn(VICTIM_ACTIVITY_OPERATION_ID, supported)
            self.assertFalse(template.qpu_service_config["abstract_commands"]["victim_activity"])
            for actor in (template.attacker_config, template.victim_config):
                self.assertNotIn(VICTIM_ACTIVITY_OPERATION_ID, actor["operation_grants"])
            self.assertNotIn(VICTIM_ACTIVITY_OPERATION_ID, session.policy.allowed_operations)
            # The author cannot turn it on: the path is locked and not editable in a challenge.
            self.assertFalse(session.is_assumption_editable(ABSTRACT_VICTIM_ACTIVITY))

    def test_a_live_challenge_environment_denies_it(self):
        resolver = ScenarioSettingsResolver(free_scenarios=product_free_scenarios(),
                                            challenges=(reset_leakage_challenge(),))
        environment = resolver.resolve(json.dumps(
            {"mode": "challenge", "preset_id": "reset-leakage", "settings": {}}).encode())
        owner = WebOwnerContext()
        product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
        try:
            controller, credential = product.controller, product.attacker_credential
            reply = await_reply(controller, credential, controller.victim_activity(
                credential, "job-0000", owner_context=owner), owner)
            self.assertEqual(reply.error_code, "OPERATION_DENIED")
        finally:
            product.close()


if __name__ == "__main__":
    unittest.main()

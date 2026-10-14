"""Reset-leakage message challenge: private message, sequenced victim, locked preset."""

import json
import unittest

from api.attack_challenge import (
    MESSAGE_EVALUATOR_ID, OBSERVATIONS_PER_MESSAGE_BIT, SECRET_MESSAGE, MessageExactEvaluator,
    SubmissionRejected, reset_leakage_message_challenge,
)
from api.attack_product_factory import ProductEnvironmentFactory
from api.attack_sessions import WebOwnerContext
from tests.test_interleaved_product import HOST_LIMITS
from api.attack_product_factory import _interleave_victims, message_bits
from api.attack_product_profiles import product_free_scenarios
from api.attack_settings_resolver import ScenarioSettingsResolver

REQUEST = json.dumps({"mode": "challenge", "preset_id": "reset-leakage-message", "settings": {}}).encode()


def resolver():
    return ScenarioSettingsResolver(free_scenarios=product_free_scenarios(),
                                    challenges=(reset_leakage_message_challenge(),))


def victim_bits(spec):
    """Bit each sequenced victim program prepares (its editor initial state)."""
    return tuple(int(program.to_circuit_dict()["initial_states"][0]) for program in spec.sequence)


class MessageEncodingTests(unittest.TestCase):
    def test_ascii_eight_bits_most_significant_first(self):
        self.assertEqual(message_bits("Wa"), (0, 1, 0, 1, 0, 1, 1, 1, 0, 1, 1, 0, 0, 0, 0, 1))
        self.assertEqual(len(message_bits(SECRET_MESSAGE)), 264)

    def test_non_printable_or_non_ascii_messages_are_rejected(self):
        for bad in ("", "a\nb", "ä", "x" * 65, 7):
            with self.assertRaises(ValueError):
                message_bits(bad)


class MessageChallengeTests(unittest.TestCase):
    def test_the_card_describes_the_rules_but_not_the_message(self):
        card = next(item for item in resolver().catalog()["challenges"]
                    if item["id"] == "reset-leakage-message")
        self.assertTrue(card["available"])
        self.assertEqual(card["title"], "Reset leakage")
        text = json.dumps(card, ensure_ascii=False)
        for private in (SECRET_MESSAGE, "secret_message", "interleaved_run"):
            self.assertNotIn(private, text)
        for rule in ("264 bit", "k mod 264", "1320", "完全一致", "3 回"):
            self.assertIn(rule, card["description"])
        self.assertIn("被害者のメッセージ", card["free_equivalent"]["not_reproduced"])

    def test_the_manifest_hides_the_message_and_sets_five_observations_per_bit(self):
        manifest = resolver().resolve(REQUEST).establish()
        self.assertEqual(manifest.evaluator_id, MESSAGE_EVALUATOR_ID)
        self.assertIn("victim.interleaved_run", manifest.hidden_fields)
        self.assertEqual(manifest.observation_budget.victim_observation_limit,
                         264 * OBSERVATIONS_PER_MESSAGE_BIT)
        self.assertEqual(manifest.victim_config["interleaved_run"]["secret_message"], SECRET_MESSAGE)
        with self.assertRaises(ValueError):
            resolver().resolve(json.dumps({
                "mode": "challenge", "preset_id": "reset-leakage-message",
                "settings": {"victim.interleaved_run.secret_message": "x"}}).encode())

    def test_the_victim_runs_the_message_bits_in_order_and_training_is_unchanged(self):
        manifest = resolver().resolve(REQUEST).establish()
        specs = {spec.reference: spec for spec in _interleave_victims(manifest)}
        self.assertEqual(victim_bits(specs["victim"]), message_bits(SECRET_MESSAGE))
        self.assertEqual((specs["training:0"].sequence, specs["training:1"].sequence), ((), ()))


class MessageEvaluatorTests(unittest.TestCase):
    def evaluator(self):
        return MessageExactEvaluator(resolver().resolve(REQUEST).establish())

    def test_exact_match_only_and_no_partial_hint(self):
        evaluator = self.evaluator()
        near = evaluator.submit({"message": SECRET_MESSAGE[:-1] + "e"})
        self.assertEqual(near.to_public_dict(), {"attempt": 1, "attempts_remaining": 2, "passed": False})
        self.assertFalse(evaluator.submit({"message": SECRET_MESSAGE.lower()}).passed)
        self.assertTrue(evaluator.submit({"message": SECRET_MESSAGE}).passed)
        with self.assertRaisesRegex(SubmissionRejected, "no submissions remain"):
            evaluator.submit({"message": SECRET_MESSAGE})

    def test_malformed_submissions_do_not_use_an_attempt(self):
        evaluator = self.evaluator()
        for bad in ({}, {"message": 3}, {"message": "ä"}, {"message": "x", "extra": 1},
                    {"threshold": 0.5, "direction": "ge"}):
            with self.assertRaises(SubmissionRejected):
                evaluator.submit(bad)
        self.assertEqual(evaluator.attempts_remaining, 3)

    def test_the_product_assembles_the_challenge_with_this_evaluator(self):
        product = ProductEnvironmentFactory(HOST_LIMITS).assemble(resolver().resolve(REQUEST), WebOwnerContext())
        try:
            self.assertIs(type(product.evaluator), MessageExactEvaluator)
        finally:
            product.close()


class MessageChallengeHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_product_catalog_lists_it_first_and_grades_over_http(self):
        from fastapi import FastAPI
        from api.attack_product_runtime import build_attack_simulator_runtime
        from api.attack_web_ownership import WebOwnershipSettings
        from tests.test_attack_web_ownership import DirectHttp

        runtime = build_attack_simulator_runtime(WebOwnershipSettings.local_development())
        app = FastAPI()
        app.include_router(runtime.router)
        http = DirectHttp(app)
        self.assertEqual([item["id"] for item in runtime.resolver.catalog()["challenges"]][:2],
                         ["reset-leakage-message", "reset-leakage"])
        status, headers, body = await http.request(
            "POST", "/api/attack/environments",
            body={"mode": "challenge", "preset_id": "reset-leakage-message", "settings": {}},
            content_type="application/json")
        self.assertEqual(status, 201, body)
        environment_id, cookie = body["environment_id"], headers["set-cookie"].split(";", 1)[0]
        path = f"/api/attack/environments/{environment_id}/submission"
        bad, _, detail = await http.request("POST", path, body={"message": 1},
                                            content_type="application/json", cookie=cookie)
        self.assertEqual((bad, detail["detail"]["code"]), (422, "INVALID_SUBMISSION"))
        status, _, result = await http.request("POST", path, body={"message": "guess"},
                                               content_type="application/json", cookie=cookie)
        self.assertEqual((status, result), (200, {"attempt": 1, "attempts_remaining": 2, "passed": False}))
        await http.request("DELETE", f"/api/attack/environments/{environment_id}", cookie=cookie)


if __name__ == "__main__":
    unittest.main()

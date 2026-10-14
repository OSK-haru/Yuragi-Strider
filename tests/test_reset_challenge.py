"""Reset-leakage challenge on the product path: locked preset, budget, hidden grading."""

from dataclasses import replace
import json
import unittest

from api.attack_challenge import (
    EVALUATOR_ID, MAX_SUBMISSIONS, HiddenCaseEvaluator, SubmissionRejected, reset_leakage_challenge,
)
from api.attack_product_factory import ProductEnvironmentFactory
from api.attack_product_profiles import product_free_scenarios
from api.attack_sessions import WebOwnerContext
from api.attack_settings_resolver import ScenarioSettingsResolver
from core.attack_simulation.job_service import ProgramRequestError
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.reset_leakage_analysis import (
    FrequencyThresholdTrainingError, LabeledBinaryObservation, fit_frequency_threshold_estimator,
)
from core.attack_simulation.reset_leakage_settings import RESET_LEAKAGE_V0_SETTINGS
from tests.test_interleaved_product import HOST_LIMITS
from tests.test_local_program_runtime import program_json

CHALLENGE_REQUEST = json.dumps({"mode": "challenge", "preset_id": "reset-leakage", "settings": {}}).encode()


def resolver(settings=RESET_LEAKAGE_V0_SETTINGS):
    return ScenarioSettingsResolver(
        free_scenarios=product_free_scenarios(),
        challenges=(reset_leakage_challenge(settings=settings),),
    )


def manifest_of(settings=RESET_LEAKAGE_V0_SETTINGS):
    return resolver(settings).resolve(CHALLENGE_REQUEST).establish()


def trained_rule(evaluator_manifest, per_label=10, rounds=32):
    """Learn a threshold from known-label observations, as a participant would."""
    from tests.test_interleaved_service import service
    observed = []
    from core.attack_simulation.jobs import JobStatus
    store, runtime, svc = service(bit=0, reset_success=RESET_LEAKAGE_V0_SETTINGS.reset_success_probability)
    for label in (0, 1):
        for index in range(per_label):
            job = svc.submit_program("a", ProgramSubmitRequest(
                CircuitProgram.from_editor_json(program_json(1)), "qpu-1", rounds,
                f"t-{label}-{index}", "inherit", interleave=f"training:{label}"))
            assert runtime.run_next().status is JobStatus.SUCCEEDED
            observed.append(LabeledBinaryObservation(
                label, rounds, svc.get_result("a", job.job_id).result["counts"]))
    return fit_frequency_threshold_estimator(observed).estimator


class ResetChallengeTests(unittest.TestCase):
    def test_the_catalog_publishes_the_card_but_no_private_value(self):
        catalog = resolver().catalog()
        card = next(item for item in catalog["challenges"] if item["id"] == "reset-leakage")
        self.assertTrue(card["available"])
        self.assertEqual(card["fields"], [])
        # The free-mode editor legitimately lists the secret bit; the challenge card must not.
        text = json.dumps(card)
        for private in ("secret_bit", "hidden_case_count", "required_correct", "interleaved_run"):
            self.assertNotIn(private, text)

    def test_the_manifest_locks_every_input_and_keeps_the_approved_values(self):
        manifest = manifest_of()
        self.assertEqual(manifest.evaluator_id, EVALUATOR_ID)
        self.assertTrue(manifest.submission_enabled)
        budget = manifest.observation_budget
        self.assertEqual((budget.shots_per_query, budget.training_query_limit),
                         (RESET_LEAKAGE_V0_SETTINGS.shots_per_case, RESET_LEAKAGE_V0_SETTINGS.training_query_limit))
        self.assertIn("victim.interleaved_run", manifest.hidden_fields)
        # A challenge retains only the shared qubit; free mode keeps the full region.
        self.assertEqual(len(manifest.qpu_service_config["retained_region"]["physical_qubit_ids"]), 1)
        reset = manifest.qpu_service_config["reset_model"]["qubits"]
        shared = manifest.qpu_service_config["retained_region"]["physical_qubit_ids"][0]
        self.assertEqual(reset[str(shared)]["success_probability"],
                         RESET_LEAKAGE_V0_SETTINGS.reset_success_probability)
        with self.assertRaises(ValueError):
            resolver().resolve(json.dumps({
                "mode": "challenge", "preset_id": "reset-leakage",
                "settings": {"victim.interleaved_run.secret_bit": 1}}).encode())

    def test_each_environment_draws_its_own_private_secret(self):
        bits = {manifest_of().victim_config["interleaved_run"]["secret_bit"] for _ in range(40)}
        self.assertEqual(bits, {0, 1})

    def test_the_approved_pass_rate_holds_on_this_physics_and_controls_fail(self):
        manifest = manifest_of()
        rule = trained_rule(manifest)
        evaluator = HiddenCaseEvaluator(manifest, HOST_LIMITS, case_seed=7)
        result = evaluator.submit({"threshold": rule.threshold,
                                   "direction": "ge" if rule.predict_one_when_at_or_above else "le"})
        self.assertGreaterEqual(result.correct, 13)
        # A constant guess is exactly half right and can never pass.
        constant = HiddenCaseEvaluator(manifest, HOST_LIMITS, case_seed=7).submit(
            {"threshold": 0.0, "direction": "ge"})
        self.assertEqual(constant.correct, 10)
        self.assertFalse(constant.passed)
        # With a perfect reset nothing leaks, so even the learned rule is near chance.
        perfect = replace(RESET_LEAKAGE_V0_SETTINGS, reset_success_probability=1.0)
        control = HiddenCaseEvaluator(manifest_of(perfect), HOST_LIMITS, case_seed=7).submit(
            {"threshold": rule.threshold, "direction": "ge"})
        self.assertLessEqual(control.correct, 15)

    def test_submissions_are_limited_and_reveal_only_aggregates(self):
        evaluator = HiddenCaseEvaluator(manifest_of(), HOST_LIMITS, case_seed=3)
        first = evaluator.submit({"threshold": 0.1, "direction": "ge"}).to_public_dict()
        self.assertEqual(set(first), {"attempt", "attempts_remaining", "correct", "total", "required", "passed"})
        for _ in range(MAX_SUBMISSIONS - 1):
            evaluator.submit({"threshold": 0.1, "direction": "ge"})
        with self.assertRaisesRegex(SubmissionRejected, "no submissions remain"):
            evaluator.submit({"threshold": 0.1, "direction": "ge"})

    def test_malformed_estimators_are_rejected_without_using_an_attempt(self):
        evaluator = HiddenCaseEvaluator(manifest_of(), HOST_LIMITS, case_seed=1)
        for bad in ({}, {"threshold": 2, "direction": "ge"}, {"threshold": 0.1, "direction": "up"},
                    {"threshold": "0.1", "direction": "ge"}, {"threshold": 0.1, "direction": "ge", "x": 1}):
            with self.assertRaises(SubmissionRejected):
                evaluator.submit(bad)
        self.assertEqual(evaluator.attempts_remaining, MAX_SUBMISSIONS)

    def test_the_live_product_runs_the_challenge_with_a_working_budget(self):
        environment = resolver().resolve(CHALLENGE_REQUEST)
        owner = WebOwnerContext()
        product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
        try:
            self.assertIsNotNone(product.evaluator)
            self.assertEqual(product.mode.value, "challenge")
        finally:
            product.close()


if __name__ == "__main__":
    unittest.main()


class ResetChallengeHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_submits_over_http_and_only_aggregates_come_back(self):
        from fastapi import FastAPI
        from api.attack_product_runtime import build_attack_simulator_runtime
        from api.attack_web_ownership import WebOwnershipSettings
        from tests.test_attack_web_ownership import DirectHttp

        runtime = build_attack_simulator_runtime(WebOwnershipSettings.local_development())
        app = FastAPI()
        app.include_router(runtime.router)
        http = DirectHttp(app)
        status, headers, body = await http.request(
            "POST", "/api/attack/environments",
            body={"mode": "challenge", "preset_id": "reset-leakage", "settings": {}},
            content_type="application/json")
        self.assertEqual(status, 201, body)
        environment_id, cookie = body["environment_id"], headers["set-cookie"].split(";", 1)[0]
        path = f"/api/attack/environments/{environment_id}/submission"
        bad, _, detail = await http.request(
            "POST", path, body={"threshold": 5, "direction": "ge"}, content_type="application/json",
            cookie=cookie)
        self.assertEqual((bad, detail["detail"]["code"]), (422, "INVALID_SUBMISSION"))
        status, _, result = await http.request(
            "POST", path, body={"threshold": 0.1, "direction": "ge"}, content_type="application/json",
            cookie=cookie)
        self.assertEqual(status, 200, result)
        self.assertEqual(set(result), {"attempt", "attempts_remaining", "correct", "total", "required", "passed"})
        # Without the owner cookie the environment does not exist for the caller.
        status, _, _ = await http.request(
            "POST", path, body={"threshold": 0.1, "direction": "ge"}, content_type="application/json")
        self.assertEqual(status, 404)
        # A free-mode environment has no evaluator at all.
        status, headers, free = await http.request(
            "POST", "/api/attack/environments",
            body={"mode": "free", "idle_noise_mode": "calibration_values", "settings": {}},
            content_type="application/json")
        status, _, _ = await http.request(
            "POST", f"/api/attack/environments/{free['environment_id']}/submission",
            body={"threshold": 0.1, "direction": "ge"}, content_type="application/json",
            cookie=headers["set-cookie"].split(";", 1)[0])
        self.assertEqual(status, 404)
        await http.request("DELETE", f"/api/attack/environments/{environment_id}", cookie=cookie)


class FreeEquivalentTests(unittest.TestCase):
    def test_the_card_offers_free_settings_that_reproduce_public_parameters_only(self):
        card = next(item for item in resolver().catalog()["challenges"] if item["id"] == "reset-leakage")
        equivalent = card["free_equivalent"]
        settings = equivalent["settings"]
        self.assertTrue(settings["qpu_service.abstract_commands.interleaved_observation"])
        self.assertEqual(settings["attacker.interleaved_access.max_rounds_per_query"],
                         RESET_LEAKAGE_V0_SETTINGS.shots_per_case)
        self.assertTrue(any(path.endswith(".success_probability")
                            and value == RESET_LEAKAGE_V0_SETTINGS.reset_success_probability
                            for path, value in settings.items()))
        text = json.dumps(equivalent)
        for private in ("secret_bit", "hidden_case_count", "required_correct"):
            self.assertNotIn(private, text)

    def test_loading_the_equivalent_into_free_mode_builds_an_environment_that_leaks_the_same_way(self):
        card = next(item for item in resolver().catalog()["challenges"] if item["id"] == "reset-leakage")
        equivalent = card["free_equivalent"]
        free = ScenarioSettingsResolver(free_scenarios=product_free_scenarios())
        request = json.dumps({"mode": "free", "idle_noise_mode": equivalent["idle_noise_mode"],
                              "settings": equivalent["settings"]}).encode()
        manifest = free.resolve(request).establish()
        self.assertIn("program.interleave.victim", manifest.qpu_service_config["supported_operations"])
        self.assertEqual(manifest.observation_budget.shots_per_query, RESET_LEAKAGE_V0_SETTINGS.shots_per_case)

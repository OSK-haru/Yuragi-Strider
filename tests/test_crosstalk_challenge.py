"""The crosstalk-Grover challenge: preset, visibility, grading rule, evaluator and the live victim."""

from dataclasses import replace
import json
import math
import unittest
from unittest.mock import patch

from api.attack_crosstalk_challenge import (
    EVALUATOR_ID, PRESET_ID, CrosstalkGroverEvaluator, CrosstalkGroverSettings, crosstalk_grover_challenge,
    disrupted,
)
from api.attack_device_snapshot import default_device_snapshot
from api.attack_product_factory import ProductAssemblyLimits, ProductEnvironmentFactory, _parse_frozen_inputs
from api.attack_product_profiles import product_free_scenarios
from api.attack_sessions import SessionHostLimits, WebOwnerContext
from api.attack_settings_resolver import ChallengeDefinition, ScenarioSettingsResolver
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.qpu_topology import TopologyLimits
from core.attack_simulation.scenario_policy import ScenarioPreset
from core.attack_simulation.request_receipts import ReceiptLimits

LIMITS = ProductAssemblyLimits(
    session=SessionHostLimits(16384, 4, ReceiptLimits(16, 64), max_queued_jobs=8),
    topology=TopologyLimits(256, 2048, 4096),
    max_runtime_jobs=160, max_events_per_job=1100, max_unresolved_events=32,
    max_workload_pending=16, max_workload_history=64, retained_jobs=128,
)
SNAPSHOT = default_device_snapshot()


def resolver(settings=CrosstalkGroverSettings()):
    return ScenarioSettingsResolver(free_scenarios=product_free_scenarios(SNAPSHOT),
                                    challenges=(crosstalk_grover_challenge(SNAPSHOT, settings),))


def manifest(settings=CrosstalkGroverSettings()):
    payload = {"mode": "challenge", "preset_id": PRESET_ID, "settings": {}}
    return resolver(settings).resolve(json.dumps(payload).encode()).establish()


class PresetTests(unittest.TestCase):
    def test_the_preset_selects_only_what_the_substrate_offers(self):
        frozen = manifest()
        execution = frozen.qpu_service_config["runtime_execution"]
        self.assertEqual(execution["placement_policy"], "tenant_dedicated_region")
        self.assertEqual(execution["concurrency_policy"], "overlap_disjoint_regions")
        self.assertEqual(execution["drive_crosstalk_strength"], 0.25)
        self.assertEqual(execution["drive_crosstalk_direction"], "both")
        self.assertEqual(frozen.evaluator_id, EVALUATOR_ID)
        self.assertTrue(frozen.submission_enabled)
        operations = set(frozen.qpu_service_config["supported_operations"])
        self.assertFalse({op for op in operations if "interleave" in op or "victim_activity" in op})
        self.assertEqual(frozen.qpu_service_config["abstract_commands"],
                         {"interleaved_observation": False, "victim_activity": False})

    def test_the_victim_repeats_a_grover_search_on_its_own_region(self):
        frozen = manifest()
        workload = frozen.victim_config["workload"]
        step = workload["steps"][0]
        self.assertEqual((step["shots"], step["max_visits"]), (50, 8))
        self.assertEqual(workload["starts"][0]["at_us"] + step["durations"]["classical_us"], 30_000_000.0)
        parsed = _parse_frozen_inputs(frozen, LIMITS)
        self.assertEqual(parsed["workload_actor_ids"], ("victim",))
        self.assertTrue(set(parsed["alternate_region"].physical_qubit_ids)
                        .isdisjoint(parsed["region"].physical_qubit_ids))

    def test_participants_see_neither_the_victim_workload_nor_the_grading_rule(self):
        catalog = json.dumps(resolver().catalog())
        card = next(item for item in resolver().catalog()["challenges"] if item["id"] == PRESET_ID)
        self.assertEqual(card["fields"], [])
        for secret in ("expected_bitstring", "significance_z", "minimum_drop", "circuit_config", "1011", "starts"):
            self.assertNotIn(secret, catalog, secret)

    def test_the_challenge_cannot_be_reconfigured(self):
        for path in ("qpu_service.runtime_execution.drive_crosstalk_strength",
                     "qpu_service.runtime_execution.concurrency_policy"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                resolver().resolve(json.dumps({"mode": "challenge", "preset_id": PRESET_ID,
                                               "settings": {path: 0.0}}).encode())


class FreeModeReproductionTests(unittest.TestCase):
    """Loading the preset's values into free mode gives the same victim and service, with no challenge code."""

    PREFIX = "qpu_service.runtime_execution."

    def free(self, extra):
        settings = {self.PREFIX + "placement_policy": "tenant_dedicated_region",
                    self.PREFIX + "concurrency_policy": "overlap_disjoint_regions",
                    self.PREFIX + "drive_crosstalk_strength": 0.25,
                    "victim.periodic_grover.enabled": True, **extra}
        payload = {"mode": "free", "idle_noise_mode": "calibration_values", "settings": settings}
        return ScenarioSettingsResolver(free_scenarios=product_free_scenarios(SNAPSHOT)).resolve(
            json.dumps(payload).encode()).establish()

    def test_same_victim_workload_and_service_conditions(self):
        challenge, free = manifest(), self.free({})
        self.assertEqual(free.victim_config["workload"], challenge.victim_config["workload"])
        for name in ("placement_policy", "concurrency_policy", "drive_crosstalk_strength",
                     "drive_crosstalk_direction", "repetition_delay_us"):
            self.assertEqual(free.qpu_service_config["runtime_execution"][name],
                             challenge.qpu_service_config["runtime_execution"][name], name)
        self.assertEqual(free.qpu_service_config["topology"], challenge.qpu_service_config["topology"])

    def test_the_schedule_is_editable_and_checked(self):
        free = self.free({"victim.periodic_grover.runs": 2, "victim.periodic_grover.shots_per_run": 10,
                          "victim.periodic_grover.first_run_us": 1000.0,
                          "victim.periodic_grover.pause_between_runs_us": 500.0})
        step = free.victim_config["workload"]["steps"][0]
        self.assertEqual((step["max_visits"], step["shots"], step["durations"]["classical_us"]), (2, 10, 500.0))
        self.assertEqual(free.victim_config["workload"]["starts"][0]["at_us"], 500.0)
        for bad in ({"victim.periodic_grover.first_run_us": 100.0},          # earlier than the pause
                    {"victim.periodic_grover.shots_per_run": 60},
                    {"victim.periodic_grover.runs": 0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.free(bad)

    def test_off_by_default_leaves_no_workload(self):
        payload = {"mode": "free", "idle_noise_mode": "calibration_values", "settings": {}}
        frozen = ScenarioSettingsResolver(free_scenarios=product_free_scenarios(SNAPSHOT)).resolve(
            json.dumps(payload).encode()).establish()
        self.assertIsNone(frozen.victim_config["workload"])
        self.assertIs(frozen.victim_config["periodic_grover"]["enabled"], False)


class GradingRuleTests(unittest.TestCase):
    RULE = dict(significance_z=3.0, minimum_drop=0.15)

    def test_a_large_significant_drop_passes(self):
        self.assertTrue(disrupted(120, 400, 280, 400, **self.RULE))          # 0.30 against 0.70

    def test_a_drop_inside_the_noise_fails(self):
        self.assertFalse(disrupted(268, 400, 280, 400, **self.RULE))         # 0.67 against 0.70

    def test_a_significant_but_small_drop_fails_the_minimum(self):
        self.assertFalse(disrupted(2800, 4000, 3200, 4000, **self.RULE))     # 0.70 against 0.80: z large, drop 0.10

    def test_a_big_drop_on_too_few_shots_fails_significance(self):
        self.assertFalse(disrupted(3, 10, 7, 10, **self.RULE))               # 0.3 against 0.7: drop 0.4, but z is only 1.9

    def test_a_higher_success_never_passes(self):
        self.assertFalse(disrupted(390, 400, 280, 400, **self.RULE))

    def test_degenerate_counts(self):
        self.assertTrue(disrupted(0, 400, 400, 400, **self.RULE))
        self.assertFalse(disrupted(400, 400, 400, 400, **self.RULE))

    def test_rust_matches_frozen_formula_across_thresholds_and_counts(self):
        for shots in (10, 100, 400):
            for attacked in (0, shots // 4, shots // 2, shots):
                for baseline in (0, shots // 2, shots):
                    for minimum in (-0.1, 0.0, 0.15, 1.2):
                        p_attack, p_base = attacked / shots, baseline / shots
                        variance = (p_attack * (1 - p_attack) / shots
                                    + p_base * (1 - p_base) / shots)
                        drop = p_base - p_attack
                        expected = drop >= minimum and (
                            variance == 0.0 or drop / math.sqrt(variance) >= 3.0
                        )
                        with self.subTest(shots=shots, attacked=attacked, baseline=baseline, minimum=minimum):
                            self.assertEqual(disrupted(attacked, shots, baseline, shots,
                                                       significance_z=3.0, minimum_drop=minimum), expected)

    def test_rust_rejects_invalid_schema_and_counts(self):
        import yuragi_strider_rust
        evaluate = yuragi_strider_rust.evaluate_crosstalk_disruption_v1
        for schema, runs, baseline, shots in (
            ("unknown", ({"success": 1},), 1, 1),
            ("crosstalk_disruption_v1", ({"success": 0},), 1, 1),
            ("crosstalk_disruption_v1", ({"success": 2**53 + 1},), 1, 1),
            ("crosstalk_disruption_v1", ({"success": 1},), 2, 1),
        ):
            with self.subTest(schema=schema, runs=runs), self.assertRaises(ValueError):
                evaluate(schema, runs, "success", baseline, shots, 3.0, 0.15)


class EvaluatorTests(unittest.TestCase):
    def evaluator(self, runs, baseline=(280, 400)):
        calls = []
        evaluator = CrosstalkGroverEvaluator(
            manifest(), LIMITS, lambda: tuple(runs),
            measure_baseline=lambda: calls.append(1) or baseline)
        return evaluator, calls

    def runs(self, success, count=8, shots=50):
        return [{"1011": success, "0000": shots - success} for _ in range(count)]

    def test_it_waits_for_every_victim_run_without_using_an_attempt(self):
        evaluator, calls = self.evaluator(self.runs(10, count=7))
        result = evaluator.submit({})
        self.assertEqual(result.to_public_dict(), {"attempt": 0, "attempts_remaining": 3, "ready": False, "passed": False})
        self.assertEqual(evaluator.attempts_remaining, 3)
        self.assertEqual(calls, [])

    def test_a_disrupted_victim_passes_and_the_result_hides_the_drop(self):
        evaluator, _ = self.evaluator(self.runs(10))                          # 0.20 against 0.70
        result = evaluator.submit({}).to_public_dict()
        self.assertEqual(result, {"attempt": 1, "attempts_remaining": 2, "ready": True, "passed": True})

    def test_an_undisturbed_victim_fails_and_the_baseline_is_measured_once(self):
        evaluator, calls = self.evaluator(self.runs(35))                      # 0.70 against 0.70
        self.assertFalse(evaluator.submit({}).passed)
        self.assertFalse(evaluator.submit({}).passed)
        self.assertEqual(calls, [1])

    def test_submissions_are_limited(self):
        from api.attack_challenge import SubmissionRejected
        evaluator, _ = self.evaluator(self.runs(35))
        for _ in range(3):
            evaluator.submit({})
        with self.assertRaisesRegex(SubmissionRejected, "no submissions remain"):
            evaluator.submit({})

    def test_only_an_empty_submission_is_accepted(self):
        from api.attack_challenge import SubmissionRejected
        evaluator, _ = self.evaluator(self.runs(35))
        for document in ({"claim": True}, [], None):
            with self.subTest(document=document), self.assertRaises(SubmissionRejected):
                evaluator.submit(document)
        self.assertEqual(evaluator.attempts_remaining, 3)

    def test_an_unavailable_baseline_does_not_use_an_attempt(self):
        from api.attack_challenge import EvaluationUnavailable
        def broken():
            raise RuntimeError("host failure")
        evaluator = CrosstalkGroverEvaluator(manifest(), LIMITS, lambda: tuple(self.runs(10)),
                                             measure_baseline=broken)
        with self.assertRaises(EvaluationUnavailable):
            evaluator.submit({})
        self.assertEqual(evaluator.attempts_remaining, 3)

    def test_invalid_native_evaluation_preserves_attempts(self):
        from api.attack_challenge import EvaluationUnavailable
        runs = self.runs(0, shots=0)
        calls = []
        evaluator = CrosstalkGroverEvaluator(manifest(), LIMITS, lambda: tuple(runs),
                                             measure_baseline=lambda: calls.append(1) or (280, 400))
        with self.assertRaises(EvaluationUnavailable):
            evaluator.submit({})
        self.assertEqual(evaluator.attempts_remaining, 3)
        runs[:] = self.runs(10)
        self.assertTrue(evaluator.submit({}).passed)
        self.assertEqual(evaluator.attempts_remaining, 2)
        self.assertEqual(calls, [1])

    def test_missing_native_runtime_is_an_explicit_error(self):
        with patch("api.attack_crosstalk_challenge.importlib.import_module", side_effect=ImportError):
            with self.assertRaisesRegex(RuntimeError, "Rust crosstalk evaluator is unavailable"):
                self.evaluator(self.runs(10))

    def test_the_real_baseline_is_the_victim_program_alone(self):
        small = CrosstalkGroverSettings(baseline_shots=50)
        evaluator = CrosstalkGroverEvaluator(manifest(small), LIMITS, lambda: ())
        successes, shots = evaluator._run_baseline()
        self.assertEqual(shots, 50)
        self.assertTrue(0.35 <= successes / shots <= 0.95)                    # about 0.7 for a noisy two-iteration search


def paused_challenge(settings):
    """The same preset with a manually advanced world clock, so a test can place a job at an exact world time."""
    preset = crosstalk_grover_challenge(SNAPSHOT, settings).preset
    experiment = dict(preset.template.experiment_config)
    experiment["live_time_policy"] = {"mode": "explicit_wait", "max_wait_us": 10_000_000.0, "max_steps_per_wait": 16384}
    template = replace(preset.template, experiment_config=experiment)
    return ChallengeDefinition(PRESET_ID, "paused", "test", ScenarioPreset(template, preset.challenge_policy, tuple(
        record for record in preset.assumptions
        if not any(path.endswith("pause_while_catching_up") for path in record.target_paths))))


def pulse_train(pulses):
    columns = [{"step": step, "gates": [{"type": "X", "targets": [0]}]} for step in range(pulses)]
    columns.append({"step": pulses, "gates": [{"type": "MEASURE", "targets": [0], "classical_targets": [0]}]})
    return CircuitProgram.from_editor_json(json.dumps(
        {"logical_qubits": 1, "initial_states": [0], "classical_bits": 1, "columns": columns}))


class LiveChallengeTests(unittest.TestCase):
    """The victim runs through the live world beside the attacker; only the attacker's timing differs."""

    SETTINGS = CrosstalkGroverSettings(first_run_us=5_000.0, pause_between_runs_us=1_000.0, victim_visits=1,
                                       baseline_shots=200)

    def play(self, attack):
        resolver_ = ScenarioSettingsResolver(free_scenarios=product_free_scenarios(SNAPSHOT),
                                             challenges=(paused_challenge(self.SETTINGS),))
        environment = resolver_.resolve(json.dumps(
            {"mode": "challenge", "preset_id": PRESET_ID, "settings": {}}).encode())
        owner = WebOwnerContext()
        product = ProductEnvironmentFactory(LIMITS).assemble(environment, owner)
        try:
            controller, credential = product.controller, product.attacker_credential
            controller.wait(credential, 4_900.0, owner_context=owner)                    # the attacker acts just before the victim's start
            if attack:
                request = ProgramSubmitRequest(pulse_train(150), "ibm_kingston", 200, "drive")
                receipt = controller.submit(credential, request, owner_context=owner)
                self.assertIsNotNone(receipt)
            controller.wait(credential, 20_000.0, owner_context=owner)
            runs = product.evaluator._observe()
            self.assertEqual(len(runs), 1)
            return product.evaluator, runs
        except BaseException:
            product.close()
            raise

    def test_an_aligned_drive_lowers_the_victim_and_a_missing_one_does_not(self):
        baseline_evaluator, quiet = self.play(attack=False)
        quiet_success = quiet[0].get("1011", 0) / sum(quiet[0].values())
        self.assertGreater(quiet_success, 0.4)                                  # the search works without an attacker
        self.assertFalse(baseline_evaluator.submit({}).passed)

        evaluator, attacked = self.play(attack=True)
        attacked_success = attacked[0].get("1011", 0) / sum(attacked[0].values())
        self.assertLess(attacked_success, quiet_success - 0.2)
        self.assertTrue(evaluator.submit({}).passed)


if __name__ == "__main__":
    unittest.main()

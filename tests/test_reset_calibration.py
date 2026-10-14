"""Contract tests for the shared reset model boundary and leakage calibration."""

import unittest

from core.attack_simulation import ObservationBudget
from core.experimental_isolation_physics import (
    ImperfectResetSpec,
    PhenomenologicalResetModel,
    ResetModelRegistry,
    binary_input_mutual_information,
    calibrate_frequency_threshold,
    derive_reset_profile,
    evaluate_frequency_threshold,
    hidden_evaluation_pass_probability,
    run_reset_leakage_calibration,
)


class ResetModelBoundaryTests(unittest.TestCase):
    def test_registry_selects_models_without_scenario_specific_branching(self) -> None:
        model = PhenomenologicalResetModel(
            ImperfectResetSpec(success_probability=0.8),
        )
        registry = ResetModelRegistry()

        registry.register(model)

        self.assertEqual(registry.model_ids(), (model.model_id,))
        self.assertIs(registry.get(model.model_id), model)
        with self.assertRaises(ValueError):
            registry.register(model)

    def test_existing_cptp_primitive_derives_binary_observation_profile(self) -> None:
        model = PhenomenologicalResetModel(
            ImperfectResetSpec(
                success_probability=0.8,
                target_populations=(0.98, 0.02),
            ),
        )

        profile = derive_reset_profile(
            model,
            reset_count=1,
            victim_measurement=True,
            generated_at_utc="2026-09-26T00:00:00+00:00",
        )

        self.assertEqual(profile.source, "simulator_derived")
        self.assertEqual(profile.fidelity_level, "behavioral_approximation")
        self.assertTrue(profile.model_configuration_id.startswith("sha256:"))
        self.assertEqual(
            profile.to_dict()["model_mode"],
            "state_transition_with_readout",
        )
        self.assertEqual(profile.to_dict()["input_mode"], "victim_dependent")
        self.assertAlmostEqual(profile.one_probability_given_victim_zero, 0.016)
        self.assertAlmostEqual(profile.one_probability_given_victim_one, 0.216)
        self.assertEqual(profile.to_dict()["model_id"], model.model_id)


class ObservationBudgetTests(unittest.TestCase):
    def test_attacker_budget_is_independent_and_enforced(self) -> None:
        budget = ObservationBudget(
            shots_per_query=128,
            training_query_limit=8,
            victim_observation_limit=2,
            retry_limit=1,
        )

        budget.require_within(
            shots=128,
            training_queries=8,
            victim_observations=2,
            retries=1,
        )
        with self.assertRaises(ValueError):
            budget.require_within(shots=129)
        with self.assertRaises(ValueError):
            budget.require_within(shots=1, victim_observations=3)
        with self.assertRaises(ValueError):
            budget.require_within(shots=0)

    def test_training_and_victim_access_can_be_disabled(self) -> None:
        budget = ObservationBudget(
            shots_per_query=1,
            training_query_limit=0,
            victim_observation_limit=0,
        )

        budget.require_within(shots=1)


class ResetLeakageCalibrationTests(unittest.TestCase):
    def test_perfect_reset_has_no_binary_information_leakage(self) -> None:
        model = PhenomenologicalResetModel(
            ImperfectResetSpec(
                success_probability=1.0,
                target_populations=(0.97, 0.03),
            ),
        )

        result = run_reset_leakage_calibration(
            model,
            reset_count=1,
            victim_measurement=True,
            shot_counts=(1, 32),
            generated_at_utc="2026-09-26T00:00:00+00:00",
        )

        for point in result.points:
            self.assertAlmostEqual(point.per_case_mutual_information_bits, 0.0)
            self.assertAlmostEqual(point.bayes_optimal_accuracy, 0.5)
            self.assertAlmostEqual(point.attacker_advantage, 0.0)

    def test_more_observations_increase_detectability_without_exceeding_one_bit(self) -> None:
        model = PhenomenologicalResetModel(
            ImperfectResetSpec(
                success_probability=0.8,
                target_populations=(0.98, 0.02),
            ),
        )

        result = run_reset_leakage_calibration(
            model,
            reset_count=1,
            victim_measurement=True,
            shot_counts=(1, 8, 64),
            generated_at_utc="2026-09-26T00:00:00+00:00",
        )

        information = [point.per_case_mutual_information_bits for point in result.points]
        accuracy = [point.bayes_optimal_accuracy for point in result.points]
        self.assertGreater(information[0], 0.0)
        self.assertLessEqual(information[0], information[1])
        self.assertLessEqual(information[1], information[2])
        self.assertLessEqual(information[2], 1.0)
        self.assertLessEqual(accuracy[0], accuracy[1])
        self.assertLessEqual(accuracy[1], accuracy[2])
        self.assertAlmostEqual(
            result.points[0].per_shot_mutual_information_bits,
            result.points[2].per_shot_mutual_information_bits,
        )

    def test_direct_mutual_information_matches_sweep(self) -> None:
        expected = binary_input_mutual_information(0.1, 0.3, 16)
        model = PhenomenologicalResetModel(
            ImperfectResetSpec(
                success_probability=0.8,
                target_populations=(0.875, 0.125),
            ),
        )

        result = run_reset_leakage_calibration(
            model,
            reset_count=1,
            victim_measurement=True,
            shot_counts=(16,),
            generated_at_utc="2026-09-26T00:00:00+00:00",
        )

        self.assertAlmostEqual(
            result.profile.one_probability_given_victim_zero,
            0.1,
        )
        self.assertAlmostEqual(
            result.profile.one_probability_given_victim_one,
            0.3,
        )
        self.assertAlmostEqual(result.points[0].per_case_mutual_information_bits, expected)

    def test_frequency_threshold_matches_bayes_optimal_accuracy(self) -> None:
        result = run_reset_leakage_calibration(
            PhenomenologicalResetModel(
                ImperfectResetSpec(
                    success_probability=0.9,
                    target_populations=(1.0, 0.0),
                ),
                observed_one_given_zero=0.04,
            ),
            reset_count=1,
            victim_measurement=True,
            shot_counts=(32,),
            generated_at_utc="2026-09-28T00:00:00+00:00",
        )
        calibration = calibrate_frequency_threshold(
            result.profile.one_probability_given_victim_zero,
            result.profile.one_probability_given_victim_one,
            32,
        )

        self.assertTrue(calibration.predict_one_when_at_or_above)
        self.assertAlmostEqual(
            calibration.balanced_accuracy,
            result.points[0].bayes_optimal_accuracy,
        )

    def test_candidate_threshold_has_half_balanced_accuracy_on_control(self) -> None:
        candidate = calibrate_frequency_threshold(0.04, 0.14, 32)

        control = evaluate_frequency_threshold(candidate, 0.04, 0.04)

        self.assertAlmostEqual(control.balanced_accuracy, 0.5)

    def test_hidden_pass_probability_is_exact_for_perfect_classifier(self) -> None:
        passing = hidden_evaluation_pass_probability(
            zero_case_count=10,
            one_case_count=10,
            required_correct=16,
            accuracy_given_zero=1.0,
            accuracy_given_one=1.0,
        )
        failing = hidden_evaluation_pass_probability(
            zero_case_count=10,
            one_case_count=10,
            required_correct=16,
            accuracy_given_zero=0.0,
            accuracy_given_one=0.0,
        )

        self.assertEqual(passing, 1.0)
        self.assertEqual(failing, 0.0)


if __name__ == "__main__":
    unittest.main()

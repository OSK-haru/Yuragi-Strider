"""Guards for the common free/challenge scenario-policy foundation."""

import unittest

from core.attack_simulation import (
    ObservationBudget,
    RESET_LEAKAGE_V0_SETTINGS,
    ScenarioMode,
    ScenarioPolicy,
    ScenarioPreset,
    ScenarioSession,
    ScenarioTemplate,
)


class SharedScenarioFoundationTests(unittest.TestCase):
    def test_non_sampling_policy_needs_no_paired_query_budget(self) -> None:
        policy = ScenarioPolicy(
            mode=ScenarioMode.CHALLENGE,
            evaluator_id="result_integrity_v1",
            submission_enabled=True,
        )
        self.assertIsNone(policy.observation_budget)
        self.assertIsNone(policy.as_free_mode().observation_budget)
        self.assertFalse(policy.as_free_mode().submission_enabled)

    def test_policy_rejects_untyped_observation_budget(self) -> None:
        with self.assertRaises(TypeError):
            ScenarioPolicy(mode=ScenarioMode.FREE, observation_budget={})

    def setUp(self) -> None:
        self.budget = ObservationBudget(
            shots_per_query=128,
            training_query_limit=8,
            victim_observation_limit=2,
            retry_limit=1,
        )
        self.template = ScenarioTemplate(
            template_id="reset_leakage_v0",
            template_version="1",
            attacker_config={"workspace": "terminal", "backend": "shared_qpu"},
            victim_config={"workload": {"kind": "binary_output"}},
            qpu_service_config={
                "execution_regime": "nisq_physical",
                "queue_policy": "fifo",
                "reset_model_id": "phenomenological_imperfect_reset",
                "reset_model_configuration_id": "test-reset-config-v1",
                "reset_model_mode": "state_transition_with_readout",
            },
            experiment_config={"hidden_seed": 7},
            free_override_fields=("victim.workload",),
        )
        self.challenge_policy = ScenarioPolicy(
            mode=ScenarioMode.CHALLENGE,
            observation_budget=self.budget,
            locked_fields=(
                "victim.workload",
                "qpu_service.reset_model_id",
                "experiment.hidden_seed",
            ),
            hidden_fields=("experiment.hidden_seed",),
            evaluator_id="binary_victim_output_v1",
            submission_enabled=True,
        )

    def test_free_and_challenge_sessions_share_the_same_template(self) -> None:
        preset = ScenarioPreset(self.template, self.challenge_policy)

        free = preset.materialize(ScenarioMode.FREE)
        challenge = preset.materialize(ScenarioMode.CHALLENGE)

        self.assertIs(free.template, challenge.template)
        self.assertEqual(free.template.to_dict(), challenge.template.to_dict())
        self.assertEqual(free.mode, ScenarioMode.FREE)
        self.assertEqual(challenge.mode, ScenarioMode.CHALLENGE)

    def test_only_challenge_overlay_locks_hides_and_enables_submission(self) -> None:
        preset = ScenarioPreset(self.template, self.challenge_policy)
        free = preset.materialize("free")
        challenge = preset.materialize("challenge")

        self.assertTrue(free.policy.is_editable("victim.workload.kind"))
        self.assertTrue(free.is_assumption_editable("victim.workload.kind"))
        self.assertFalse(free.is_assumption_editable("experiment.hidden_seed"))
        self.assertTrue(free.policy.is_visible("experiment.hidden_seed"))
        self.assertFalse(free.policy.submission_enabled)
        self.assertIsNone(free.policy.evaluator_id)

        self.assertFalse(challenge.policy.is_editable("victim.workload.kind"))
        self.assertFalse(challenge.policy.is_visible("experiment.hidden_seed"))
        self.assertTrue(challenge.policy.submission_enabled)
        self.assertEqual(
            challenge.policy.evaluator_id,
            "binary_victim_output_v1",
        )

    def test_free_mode_starts_with_the_same_budget_values(self) -> None:
        preset = ScenarioPreset(self.template, self.challenge_policy)

        free = preset.materialize("free")

        self.assertEqual(free.policy.observation_budget, self.budget)

    def test_template_configuration_cannot_be_mutated_through_a_session(self) -> None:
        preset = ScenarioPreset(self.template, self.challenge_policy)
        free = preset.materialize("free")

        with self.assertRaises(TypeError):
            free.template.victim_config["workload"] = {"kind": "changed"}
        with self.assertRaises(TypeError):
            free.template.victim_config["workload"]["kind"] = "changed"

        editable = free.create_editable_template_copy()
        editable["victim_config"]["workload"]["kind"] = "changed"
        self.assertEqual(
            free.template.victim_config["workload"]["kind"],
            "binary_output",
        )

    def test_challenge_session_does_not_produce_an_editable_copy(self) -> None:
        challenge = ScenarioPreset(
            self.template,
            self.challenge_policy,
        ).materialize("challenge")

        with self.assertRaises(PermissionError):
            challenge.create_editable_template_copy()

    def test_hidden_fields_must_be_covered_by_a_lock(self) -> None:
        with self.assertRaises(ValueError):
            ScenarioPolicy(
                mode="challenge",
                observation_budget=self.budget,
                hidden_fields=("victim.secret",),
            )

    def test_free_mode_rejects_challenge_only_controls(self) -> None:
        with self.assertRaises(ValueError):
            ScenarioPolicy(
                mode="free",
                observation_budget=self.budget,
                locked_fields=("qpu_service.reset_model_id",),
            )

    def test_reset_leakage_v0_uses_approved_default_shots(self) -> None:
        budget = RESET_LEAKAGE_V0_SETTINGS.observation_budget(
            training_query_limit=2,
            victim_observation_limit=1,
        )

        self.assertEqual(
            RESET_LEAKAGE_V0_SETTINGS.reset_success_probability,
            0.90,
        )
        self.assertEqual(budget.shots_per_query, 32)

    def test_reset_leakage_v0_supplies_adopted_training_budget(self) -> None:
        settings = RESET_LEAKAGE_V0_SETTINGS

        budget = settings.observation_budget(
            victim_observation_limit=1,
        )

        self.assertEqual(
            settings.minimum_calibrated_training_queries_per_label,
            8,
        )
        self.assertEqual(settings.training_queries_per_label, 10)
        self.assertEqual(budget.training_query_limit, 20)

    def test_reset_leakage_shots_can_be_overridden_before_compilation(self) -> None:
        budget = RESET_LEAKAGE_V0_SETTINGS.observation_budget(
            training_query_limit=2,
            victim_observation_limit=1,
            shots_per_case=64,
        )
        session = ScenarioSession(
            template=self.template,
            policy=ScenarioPolicy(
                mode=ScenarioMode.CHALLENGE,
                observation_budget=budget,
                evaluator_id="binary_victim_output_v1",
                submission_enabled=True,
            ),
        )

        self.assertEqual(session.policy.observation_budget.shots_per_query, 64)

    def test_reset_leakage_v0_uses_approved_hidden_grading(self) -> None:
        policy = RESET_LEAKAGE_V0_SETTINGS.hidden_evaluation_policy(
            case_order_seed=7,
            measurement_seed=13,
        )

        self.assertEqual(policy.case_count, 20)
        self.assertEqual(policy.zero_case_count, 10)
        self.assertEqual(policy.shots_per_case, 32)
        self.assertEqual(policy.required_correct, 16)

    def test_hidden_grading_uses_the_same_overridden_shot_value(self) -> None:
        policy = RESET_LEAKAGE_V0_SETTINGS.hidden_evaluation_policy(
            case_order_seed=7,
            measurement_seed=13,
            shots_per_case=64,
        )

        self.assertEqual(policy.shots_per_case, 64)


if __name__ == "__main__":
    unittest.main()

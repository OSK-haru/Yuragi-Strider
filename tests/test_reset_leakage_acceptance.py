"""Acceptance-contract tests for attack signal and mandatory controls."""

import unittest

from core.attack_simulation import (
    HiddenEvaluationPolicy,
    RESET_LEAKAGE_V0_SETTINGS,
    ResetLeakageAcceptancePolicy,
    audit_reset_leakage_acceptance,
)
from core.experimental_isolation_physics import (
    ImperfectResetSpec,
    PhenomenologicalResetModel,
    ResetCalibrationInputMode,
    run_reset_leakage_calibration,
)


class ResetLeakageAcceptanceTests(unittest.TestCase):
    def test_candidate_and_all_negative_controls_pass_contract(self) -> None:
        report = audit_reset_leakage_acceptance(
            candidate=_calibration(success_probability=0.8),
            perfect_reset_control=_calibration(success_probability=1.0),
            isolated_resource_control=_calibration(
                success_probability=0.8,
                input_mode=ResetCalibrationInputMode.FIXED_ZERO_CONTROL,
            ),
            hidden_policy=_hidden_policy(required_correct=5),
            observation_disabled_verified=True,
            policy=_acceptance_policy(),
        )

        self.assertTrue(report.passed)
        self.assertTrue(all(check.passed for check in report.checks))
        public = report.to_dict()
        self.assertNotIn("seed", str(public))
        self.assertNotIn("victim_bits", str(public))

    def test_constant_baseline_or_missing_access_control_fails_contract(self) -> None:
        report = audit_reset_leakage_acceptance(
            candidate=_calibration(success_probability=0.8),
            perfect_reset_control=_calibration(success_probability=1.0),
            isolated_resource_control=_calibration(
                success_probability=0.8,
                input_mode="fixed_zero_control",
            ),
            hidden_policy=_hidden_policy(required_correct=4),
            observation_disabled_verified=False,
            policy=_acceptance_policy(),
        )

        self.assertFalse(report.passed)
        failed = {
            check.check_id
            for check in report.checks
            if not check.passed
        }
        self.assertEqual(failed, {
            "constant_estimator_cannot_pass",
            "observation_disabled_control",
        })

    def test_isolated_control_must_use_fixed_zero_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "fixed-zero"):
            audit_reset_leakage_acceptance(
                candidate=_calibration(success_probability=0.8),
                perfect_reset_control=_calibration(success_probability=1.0),
                isolated_resource_control=_calibration(
                    success_probability=0.8,
                ),
                hidden_policy=_hidden_policy(required_correct=5),
                observation_disabled_verified=True,
                policy=_acceptance_policy(),
            )

    def test_approved_reset_only_profile_passes_its_acceptance_policy(self) -> None:
        settings = RESET_LEAKAGE_V0_SETTINGS
        candidate_model = settings.build_reset_model()
        perfect_model = PhenomenologicalResetModel(
            ImperfectResetSpec(
                success_probability=1.0,
                target_populations=(
                    1.0 - settings.target_excited_population,
                    settings.target_excited_population,
                ),
            ),
            observed_one_given_zero=settings.observed_one_given_zero,
            observed_zero_given_one=settings.observed_zero_given_one,
            model_id="perfect_reset_control",
        )
        calibration_arguments = {
            "reset_count": settings.reset_count,
            "victim_measurement": True,
            "shot_counts": (settings.shots_per_case,),
            "generated_at_utc": "2026-09-28T00:00:00+00:00",
        }

        report = audit_reset_leakage_acceptance(
            candidate=run_reset_leakage_calibration(
                candidate_model,
                profile_id="reset_leakage_v0_candidate",
                **calibration_arguments,
            ),
            perfect_reset_control=run_reset_leakage_calibration(
                perfect_model,
                profile_id="reset_leakage_v0_perfect_control",
                **calibration_arguments,
            ),
            isolated_resource_control=run_reset_leakage_calibration(
                candidate_model,
                profile_id="reset_leakage_v0_isolated_control",
                input_mode=ResetCalibrationInputMode.FIXED_ZERO_CONTROL,
                **calibration_arguments,
            ),
            hidden_policy=settings.hidden_evaluation_policy(
                case_order_seed=7,
                measurement_seed=13,
            ),
            observation_disabled_verified=True,
            policy=settings.acceptance_policy(),
        )

        self.assertEqual(settings.scope, "reset_leakage_challenge_only")
        self.assertEqual(
            candidate_model.configuration_id,
            "sha256:47b34a4a4261150ef347495e904530688a7c1e6adf54324e4d093f2206b77028",
        )
        self.assertTrue(report.passed)


def _calibration(
    *,
    success_probability: float,
    input_mode: ResetCalibrationInputMode | str = (
        ResetCalibrationInputMode.VICTIM_DEPENDENT
    ),
):
    return run_reset_leakage_calibration(
        PhenomenologicalResetModel(ImperfectResetSpec(
            success_probability=success_probability,
            target_populations=(0.98, 0.02),
        )),
        reset_count=1,
        victim_measurement=True,
        shot_counts=(8,),
        generated_at_utc="2026-09-26T00:00:00+00:00",
        input_mode=input_mode,
    )


def _hidden_policy(*, required_correct: int) -> HiddenEvaluationPolicy:
    return HiddenEvaluationPolicy(
        policy_id="reset-hidden-v0",
        policy_version="1",
        case_count=8,
        zero_case_count=4,
        shots_per_case=8,
        required_correct=required_correct,
        case_order_seed=7,
        measurement_seed=13,
    )


def _acceptance_policy() -> ResetLeakageAcceptancePolicy:
    return ResetLeakageAcceptancePolicy(
        policy_id="reset-acceptance-v0",
        policy_version="1",
        shots_per_case=8,
        minimum_candidate_information_bits=0.01,
        minimum_candidate_advantage=0.01,
        maximum_control_information_bits=1e-12,
        maximum_control_advantage=1e-12,
    )


if __name__ == "__main__":
    unittest.main()

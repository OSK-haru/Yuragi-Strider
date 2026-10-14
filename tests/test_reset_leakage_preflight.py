"""Tests for bounded, manager-private reset challenge preflight."""

import unittest
from unittest.mock import patch

from core.attack_simulation import (
    RESET_LEAKAGE_V0_SETTINGS,
    ResetLeakagePreflightCondition,
    ResetLeakagePreflightError,
    ResetLeakagePreflightPolicy,
    ResetLeakageSeedBundle,
    ScenarioCompilationError,
    build_reset_leakage_v0_orchestrator,
    preflight_reset_leakage_instance,
)


class ResetLeakagePreflightTests(unittest.TestCase):
    def test_rejected_configuration_does_not_create_qpu_or_job_runtime(self) -> None:
        module = "core.attack_simulation.reset_leakage_preflight"
        with (
            patch(f"{module}.ScenarioEnvironment.establish", side_effect=
                  ScenarioCompilationError("invalid challenge")),
            patch(f"{module}.QpuStateStore") as state_store,
            patch(f"{module}.SharedQpuRuntime") as runtime,
            patch(f"{module}.ResetLeakageScenarioOrchestrator") as orchestrator,
        ):
            with self.assertRaisesRegex(ScenarioCompilationError, "invalid challenge"):
                build_reset_leakage_v0_orchestrator(
                    condition=ResetLeakagePreflightCondition.CANDIDATE,
                    seed_bundle=ResetLeakageSeedBundle(1, 2, 3, 4),
                    attempt=1,
                )

        state_store.assert_not_called()
        runtime.assert_not_called()
        orchestrator.assert_not_called()

    def test_v0_selects_private_seed_with_required_controls(self) -> None:
        result = preflight_reset_leakage_instance(
            policy=ResetLeakagePreflightPolicy(
                selection_seed=20260929,
                max_attempts=8,
            ),
        )
        private = result.to_private_dict()

        self.assertTrue(result.candidate.passed)
        self.assertFalse(result.perfect_reset_control.passed)
        self.assertFalse(result.isolated_resource_control.passed)
        self.assertLess(
            result.constant_zero_correct,
            RESET_LEAKAGE_V0_SETTINGS.hidden_required_correct,
        )
        self.assertLess(
            result.constant_one_correct,
            RESET_LEAKAGE_V0_SETTINGS.hidden_required_correct,
        )
        self.assertEqual(private["visibility"], "scenario_manager_private")
        self.assertEqual(private["selection_seed"], 20260929)
        self.assertEqual(private["max_attempts"], 8)
        self.assertIn("seed_bundle", private)
        self.assertNotIn("seed_bundle", result.candidate.to_public_dict())

    def test_search_stops_at_configured_attempt_limit(self) -> None:
        factory = _NoSeparationFactory()

        with self.assertRaises(ResetLeakagePreflightError) as raised:
            preflight_reset_leakage_instance(
                policy=ResetLeakagePreflightPolicy(
                    selection_seed=3,
                    max_attempts=3,
                ),
                orchestrator_factory=factory,
            )

        self.assertEqual(factory.calls, 3)
        self.assertEqual(raised.exception.max_attempts, 3)
        self.assertEqual(raised.exception.rejected_training, 3)


class _PublicTrainingResult:
    def __init__(self, label: int) -> None:
        self.label = label

    def to_attacker_dict(self) -> dict[str, object]:
        return {
            "phase": "training",
            "known_label": self.label,
            "shots": 32,
            "counts": {"0": 16, "1": 16},
        }


class _NoSeparationOrchestrator:
    def run_training_query(self, *, victim_bit: int, shots: int):
        if shots != 32:
            raise AssertionError("unexpected shot count")
        return _PublicTrainingResult(victim_bit)


class _NoSeparationFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, condition, bundle, attempt):
        self.calls += 1
        return _NoSeparationOrchestrator()


if __name__ == "__main__":
    unittest.main()

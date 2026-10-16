"""Tests for attacker-visible reset-leakage estimator training."""

import unittest

from core.attack_simulation import (
    BinaryObservation,
    LabeledBinaryObservation,
    fit_frequency_threshold_estimator,
)
from core.attack_simulation.reset_leakage_analysis import FrequencyThresholdTrainingError


class ResetLeakageAnalysisTests(unittest.TestCase):
    def test_fits_threshold_from_public_training_results(self) -> None:
        training = fit_frequency_threshold_estimator((
            LabeledBinaryObservation.from_attacker_result({
                "phase": "training",
                "known_label": 0,
                "shots": 10,
                "counts": {"0": 9, "1": 1},
                "internal_value_that_must_not_be_read": "ignored",
            }),
            LabeledBinaryObservation.from_attacker_result({
                "phase": "training",
                "known_label": 1,
                "shots": 10,
                "counts": {"0": 2, "1": 8},
            }),
        ))

        self.assertAlmostEqual(training.estimator.threshold, 0.45)
        self.assertTrue(training.estimator.predict_one_when_at_or_above)
        self.assertEqual(
            training.estimator.predict(BinaryObservation(
                shots=10,
                counts={"0": 8, "1": 2},
            )),
            0,
        )
        self.assertEqual(
            training.estimator.predict(BinaryObservation(
                shots=10,
                counts={"0": 1, "1": 9},
            )),
            1,
        )
        self.assertEqual(
            training.to_public_dict()["provenance"],
            "attacker_visible_labelled_observations",
        )

    def test_supports_reversed_observation_direction(self) -> None:
        training = fit_frequency_threshold_estimator((
            LabeledBinaryObservation(0, 10, {"0": 2, "1": 8}),
            LabeledBinaryObservation(1, 10, {"0": 9, "1": 1}),
        ))

        self.assertFalse(training.estimator.predict_one_when_at_or_above)
        self.assertEqual(
            training.estimator.predict(BinaryObservation(
                shots=10,
                counts={"0": 8, "1": 2},
            )),
            1,
        )

    def test_requires_both_labels(self) -> None:
        with self.assertRaisesRegex(ValueError, "both labels"):
            fit_frequency_threshold_estimator((
                LabeledBinaryObservation(0, 4, {"0": 4, "1": 0}),
            ))

    def test_rejects_unlabelled_or_non_training_public_result(self) -> None:
        with self.assertRaisesRegex(ValueError, "public training"):
            LabeledBinaryObservation.from_attacker_result({
                "phase": "victim_observation",
                "shots": 4,
                "counts": {"0": 2, "1": 2},
            })

    def test_rejects_training_without_observed_separation(self) -> None:
        with self.assertRaisesRegex(ValueError, "no class separation"):
            fit_frequency_threshold_estimator((
                LabeledBinaryObservation(0, 4, {"0": 2, "1": 2}),
                LabeledBinaryObservation(1, 4, {"0": 2, "1": 2}),
            ))

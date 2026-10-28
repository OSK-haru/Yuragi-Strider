"""Tests for attacker-visible reset-leakage estimator training."""

import importlib
import unittest
from unittest.mock import patch

from core.attack_simulation import (
    BinaryObservation,
    LabeledBinaryObservation,
    analyze_public_training_results,
    fit_frequency_threshold_estimator,
)
from core.attack_simulation.reset_leakage_analysis import FrequencyThresholdTrainingError
from core.rust_dense_kernel import is_rust_kernel_available


def _rust_analysis_available():
    return is_rust_kernel_available() and callable(getattr(
        importlib.import_module("yuragi_strider_rust"), "binary_contrast_statistics_v1", None,
    ))


class ResetLeakageAnalysisTests(unittest.TestCase):
    def test_public_training_counts_are_pooled_and_analyzed_with_provenance(self):
        metadata = dict(
            manifest_id="manifest-1", mechanism_version="1",
            model_configuration_id="model-1", dataset_id="training-1",
            measurement_id="readout-1", assumption_ids=("independent-shots",),
            measurement_conditions={"shots": "variable"},
        )
        results = (
            {"phase": "training", "known_label": 0, "shots": 10, "counts": {"0": 9, "1": 1}},
            {"phase": "training", "known_label": 0, "shots": 30, "counts": {"0": 21, "1": 9}},
            {"phase": "training", "known_label": 1, "shots": 20, "counts": {"0": 4, "1": 16}},
        )
        report = analyze_public_training_results(results, **metadata)
        self.assertEqual(report.probability_one_by_class, (0.25, 0.8))
        self.assertAlmostEqual(report.difference_class_1_minus_class_0, 0.55)
        self.assertEqual(report.request.trials_0, 40)
        self.assertEqual(report.request.outcome_one_0, 10)
        self.assertEqual(report.request.manifest_id, "manifest-1")
        self.assertEqual(report.request.assumption_ids, ("independent-shots",))
        self.assertEqual(report.request.measurement_conditions["shots"], "variable")

    def test_public_analysis_rejects_hidden_and_malformed_inputs(self):
        metadata = dict(manifest_id="m", mechanism_version="1", model_configuration_id="c",
                        dataset_id="d", measurement_id="r")
        valid = {"phase": "training", "known_label": 0, "shots": 4, "counts": {"0": 3, "1": 1}}
        for invalid in (
            {**valid, "phase": "victim_observation"},
            {**valid, "known_label": True},
            {**valid, "counts": {"0": 3, "1": 1.0}},
            {**valid, "shots": 5},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                analyze_public_training_results((valid, invalid), **metadata)
        with self.assertRaisesRegex(ValueError, "both labels"):
            analyze_public_training_results((valid,), **metadata)

    def test_training_job_results_feed_the_common_analysis(self):
        from core.attack_simulation.jobs import JobStatus
        from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
        from tests.test_interleaved_service import service
        from tests.test_local_program_runtime import program_json

        _, runtime, svc = service(bit=0)
        observations = []
        for label in (0, 1):
            job = svc.submit_program("a", ProgramSubmitRequest(
                CircuitProgram.from_editor_json(program_json(1)), "qpu-1", 8,
                f"analysis-{label}", "inherit", interleave=f"training:{label}",
            ))
            self.assertIs(runtime.run_next().status, JobStatus.SUCCEEDED)
            counts = svc.get_result("a", job.job_id).result["counts"]
            observations.append(LabeledBinaryObservation(label, 8, counts))
        report = analyze_public_training_results(
            observations, manifest_id="test-manifest", mechanism_version="1",
            model_configuration_id="test-model", dataset_id="training-jobs",
            measurement_id="readout",
        )
        self.assertEqual(report.request.trials_0, 8)
        self.assertEqual(report.request.trials_1, 8)
        self.assertEqual(report.request.outcome_one_0, observations[0].one_count)
        self.assertEqual(report.request.outcome_one_1, observations[1].one_count)

    @unittest.skipUnless(_rust_analysis_available(), "Rust analysis kernel unavailable")
    def test_public_training_adapter_supports_rust_backend(self):
        metadata = dict(manifest_id="m", mechanism_version="1", model_configuration_id="c",
                        dataset_id="d", measurement_id="r")
        results = (
            {"phase": "training", "known_label": 0, "shots": 10, "counts": {"0": 8, "1": 2}},
            {"phase": "training", "known_label": 1, "shots": 10, "counts": {"0": 2, "1": 8}},
        )
        reference = analyze_public_training_results(results, backend="python", **metadata)
        native = analyze_public_training_results(results, **metadata)
        self.assertAlmostEqual(reference.balanced_prior_mutual_information_bits,
                               native.balanced_prior_mutual_information_bits, places=14)
        self.assertAlmostEqual(reference.difference_class_1_minus_class_0,
                               native.difference_class_1_minus_class_0, places=14)

    def test_default_reset_analysis_batches_observations_in_one_rust_call(self):
        results = (
            {"phase": "training", "known_label": 0, "shots": 10, "counts": {"0": 8, "1": 2}},
            {"phase": "training", "known_label": 1, "shots": 10, "counts": {"0": 2, "1": 8}},
        )
        summary = (1, 1, 10, 2, 10, 8, 0.5, True, 0.2, 0.8, 0.6,
                   0.17888543819998318, 3.3541019662496847, 0.2780719051126377, 0.8)
        with patch(
            "core.attack_simulation.reset_leakage_analysis.rust_reset_training_analysis",
            return_value=summary,
        ) as operation:
            report = analyze_public_training_results(
                results, manifest_id="m", mechanism_version="1",
                model_configuration_id="c", dataset_id="d", measurement_id="r",
            )

        operation.assert_called_once_with([(0, 10, 2), (1, 10, 8)])
        self.assertEqual(report.probability_one_by_class, (0.2, 0.8))

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

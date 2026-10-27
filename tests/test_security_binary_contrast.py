"""Contract and parity tests for shared actor-observation statistics."""

import math
import importlib
import unittest

from core.security_analysis import (
    BINARY_CONTRAST_SCHEMA_VERSION,
    BinaryContrastRequest,
    ObservationSource,
    analyze_binary_contrast,
)
from core.rust_dense_kernel import is_rust_kernel_available, rust_binary_contrast_statistics


def _rust_analysis_available():
    if not is_rust_kernel_available():
        return False
    module = importlib.import_module("yuragi_strider_rust")
    return callable(getattr(module, "binary_contrast_statistics_v1", None))


def request(**overrides):
    values = {
        "manifest_id": "sha256:" + "a" * 64,
        "mechanism_id": "reset_leakage",
        "mechanism_version": "1",
        "model_configuration_id": "sha256:" + "b" * 64,
        "dataset_id": "training-01",
        "measurement_id": "binary-readout-v1",
        "class_0_id": "victim-output-0",
        "class_1_id": "victim-output-1",
        "outcome_id": "attacker-measured-one",
        "source": ObservationSource.ACTOR_VISIBLE,
        "trials_0": 100,
        "outcome_one_0": 20,
        "trials_1": 100,
        "outcome_one_1": 80,
        "assumption_ids": ("noise-profile", "sampling-policy"),
    }
    values.update(overrides)
    return BinaryContrastRequest(**values)


class BinaryContrastContractTests(unittest.TestCase):
    def test_metadata_and_aggregates_round_trip_with_stable_assumptions(self):
        original = request(assumption_ids=("z", "a"))
        encoded = original.to_dict()
        self.assertEqual(encoded["schema_version"], BINARY_CONTRAST_SCHEMA_VERSION)
        self.assertEqual(encoded["assumption_ids"], ["a", "z"])
        self.assertEqual(BinaryContrastRequest.from_dict(encoded), original)
        self.assertEqual(BinaryContrastRequest.from_dict(encoded).to_dict(), encoded)

    def test_refuses_unknown_schema_and_fields(self):
        encoded = request().to_dict()
        with self.assertRaisesRegex(ValueError, "unsupported"):
            BinaryContrastRequest.from_dict({**encoded, "schema_version": "security_binary_contrast_v2"})
        with self.assertRaisesRegex(ValueError, "missing or unknown"):
            BinaryContrastRequest.from_dict({**encoded, "hidden_label": 0})

    def test_rejects_internal_grader_provenance(self):
        for source in ("simulator_internal", "researcher_authorized"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                request(source=source)

    def test_rejects_invalid_counts_and_duplicate_assumption_ids(self):
        for invalid in (
            {"trials_0": 0},
            {"outcome_one_0": 101},
            {"trials_1": True},
            {"assumption_ids": ("same", "same")},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                request(**invalid)


class BinaryContrastStatisticsTests(unittest.TestCase):
    def test_rust_boundary_validates_schema_and_exact_count_domain_before_loading(self):
        with self.assertRaisesRegex(ValueError, "schema version"):
            rust_binary_contrast_statistics("security_binary_contrast_v2", "actor_visible", 10, 2, 10, 8)
        with self.assertRaisesRegex(ValueError, "exactly representable"):
            rust_binary_contrast_statistics(
                BINARY_CONTRAST_SCHEMA_VERSION, "actor_visible", 2**53 + 1, 2, 10, 8,
            )

    def test_computes_shared_metrics_for_a_reset_classification_case(self):
        report = analyze_binary_contrast(request())
        self.assertEqual(report.probability_one_by_class, (0.2, 0.8))
        self.assertAlmostEqual(report.difference_class_1_minus_class_0, 0.6)
        self.assertAlmostEqual(report.standard_error, math.sqrt(0.0032))
        self.assertAlmostEqual(report.z_score, 0.6 / math.sqrt(0.0032))
        self.assertAlmostEqual(report.balanced_prior_mutual_information_bits, 1.0 - _entropy(0.2))
        self.assertAlmostEqual(report.balanced_prior_bayes_accuracy, 0.8)

    def test_reversed_direction_matches_crosstalk_drop_sign_convention(self):
        report = analyze_binary_contrast(request(
            mechanism_id="drive_crosstalk",
            class_0_id="victim-alone-baseline",
            class_1_id="attacker-present",
            trials_0=400,
            outcome_one_0=280,
            trials_1=400,
            outcome_one_1=120,
        ))
        self.assertAlmostEqual(report.difference_class_1_minus_class_0, -0.4)
        self.assertLess(report.z_score, 0.0)
        self.assertAlmostEqual(report.balanced_prior_bayes_accuracy, 0.7)

    def test_degenerate_and_no_effect_cases_are_finite(self):
        perfect = analyze_binary_contrast(request(
            trials_0=2, outcome_one_0=0, trials_1=2, outcome_one_1=2,
        ))
        self.assertIsNone(perfect.z_score)
        self.assertEqual(perfect.balanced_prior_mutual_information_bits, 1.0)
        self.assertEqual(perfect.balanced_prior_bayes_accuracy, 1.0)

        no_effect = analyze_binary_contrast(request(
            trials_0=20, outcome_one_0=10, trials_1=30, outcome_one_1=15,
        ))
        self.assertEqual(no_effect.difference_class_1_minus_class_0, 0.0)
        self.assertEqual(no_effect.z_score, 0.0)
        self.assertEqual(no_effect.balanced_prior_mutual_information_bits, 0.0)

    def test_rust_backend_selection_fails_closed_when_extension_lacks_kernel(self):
        if _rust_analysis_available():
            self.skipTest("native binary contrast kernel is available")
        with self.assertRaisesRegex(RuntimeError, "does not implement"):
            analyze_binary_contrast(request(), backend="rust")

    @unittest.skipUnless(_rust_analysis_available(), "Rust binary contrast kernel is not built")
    def test_rust_matches_python_for_endpoint_and_interior_probabilities(self):
        cases = (
            request(),
            request(trials_0=400, outcome_one_0=280, trials_1=400, outcome_one_1=120),
            request(trials_0=2, outcome_one_0=0, trials_1=2, outcome_one_1=2),
            request(trials_0=20, outcome_one_0=10, trials_1=30, outcome_one_1=15),
            request(trials_0=7, outcome_one_0=1, trials_1=11, outcome_one_1=10),
        )
        for item in cases:
            with self.subTest(item=item):
                python_result = analyze_binary_contrast(item, backend="python").to_dict()
                rust_result = analyze_binary_contrast(item, backend="rust").to_dict()
                self.assertEqual(python_result.keys(), rust_result.keys())
                for key in python_result:
                    if isinstance(python_result[key], (int, float)) and python_result[key] is not None:
                        self.assertAlmostEqual(python_result[key], rust_result[key], delta=2e-15)
                    else:
                        self.assertEqual(python_result[key], rust_result[key])

    @unittest.skipUnless(_rust_analysis_available(), "Rust binary contrast kernel is not built")
    def test_rust_boundary_rejects_unknown_schema_and_internal_source(self):
        module = importlib.import_module("yuragi_strider_rust")
        for schema, source in (
            ("security_binary_contrast_v2", "actor_visible"),
            (BINARY_CONTRAST_SCHEMA_VERSION, "simulator_internal"),
            (BINARY_CONTRAST_SCHEMA_VERSION, "researcher_authorized"),
        ):
            with self.subTest(schema=schema, source=source), self.assertRaises(ValueError):
                module.binary_contrast_statistics_v1(schema, source, 10, 2, 10, 8)


def _entropy(value):
    if value in (0.0, 1.0):
        return 0.0
    return -value * math.log2(value) - (1.0 - value) * math.log2(1.0 - value)


if __name__ == "__main__":
    unittest.main()

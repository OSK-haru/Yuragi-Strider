import json
from pathlib import Path
import unittest

from validation_physics.model_baseline import (
    PHYSICAL_MODEL_BASELINE_FREEZE_ID,
    build_freeze_report,
)


class PhysicalModelBaselineTests(unittest.TestCase):
    def test_smoke_cases_and_source_audit_pass(self) -> None:
        report = build_freeze_report()

        self.assertEqual(
            report["frozen_contract"]["freeze_id"],
            PHYSICAL_MODEL_BASELINE_FREEZE_ID,
        )
        self.assertTrue(report["smoke_cases"]["gate_aware_per_qubit"]["passed"])
        self.assertTrue(report["smoke_cases"]["pulse_network_per_transmon"]["passed"])
        self.assertTrue(all(item["exists"] for item in report["source_revision"]["files"]))
        self.assertTrue(all(item["exists"] for item in report["evidence_files"]))
        self.assertTrue(report["overall_pass"])

    def test_committed_artifact_matches_the_current_physics_fingerprint(self) -> None:
        report = build_freeze_report()
        artifact_path = (
            Path(__file__).resolve().parents[1]
            / "validation_results"
            / "physical_model_baseline_v1.json"
        )
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

        self.assertEqual(artifact["frozen_contract"], report["frozen_contract"])
        self.assertEqual(
            artifact["source_revision"]["critical_source_tree_sha256"],
            report["source_revision"]["critical_source_tree_sha256"],
        )
        self.assertEqual(artifact["evidence_files"], report["evidence_files"])
        self.assertTrue(artifact["overall_pass"])


if __name__ == "__main__":
    unittest.main()

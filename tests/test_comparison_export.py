import json
import tempfile
import unittest
from pathlib import Path

from core.circuit_model import CircuitConfig
from core.comparison import ComparisonConfig, run_comparison
from core.io.report_export import (
    comparison_markdown_report_text,
    export_comparison_markdown_report,
)
from core.io.result_export import (
    comparison_result_to_dict,
    comparison_to_csv_text,
    export_comparison_csv,
    save_comparison_result_json,
)
from core.results import EnvironmentConfig


class ComparisonExportTest(unittest.TestCase):
    def test_comparison_json_contains_delta_metrics_and_conditions(self) -> None:
        encoded = comparison_result_to_dict(_comparison())

        self.assertEqual(encoded["kind"], "yuragi_strider.comparison_result")
        self.assertIn("condition_a", encoded)
        self.assertIn("condition_b", encoded)
        self.assertIn("delta_metrics", encoded)
        json.dumps(encoded)

    def test_comparison_csv_contains_ab_columns(self) -> None:
        csv_text = comparison_to_csv_text(_comparison())

        header = csv_text.splitlines()[0]
        self.assertIn("state_fidelity_a", header)
        self.assertIn("purity_b", header)

    def test_comparison_export_files_can_be_written(self) -> None:
        comparison = _comparison()
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "comparison.qscope.result.json"
            csv_path = Path(directory) / "comparison.csv"
            md_path = Path(directory) / "comparison.md"

            save_comparison_result_json(comparison, json_path)
            export_comparison_csv(comparison, csv_path)
            export_comparison_markdown_report(comparison, md_path)

            self.assertIn("delta_metrics", json.loads(json_path.read_text(encoding="utf-8")))
            self.assertIn("state_fidelity_a", csv_path.read_text(encoding="utf-8"))
            self.assertIn("Yuragi-Strider Comparison Report", md_path.read_text(encoding="utf-8"))

    def test_comparison_markdown_report_is_generated(self) -> None:
        report = comparison_markdown_report_text(_comparison())

        self.assertIn("# Yuragi-Strider Comparison Report", report)
        self.assertIn("## Delta Metrics", report)


def _comparison():
    return run_comparison(ComparisonConfig(
        circuit=CircuitConfig.one_qubit_h(),
        environment_a=EnvironmentConfig(
            t1_zero_temperature_us=95.49925860214358,
            tphi_zero_temperature_us=64.63587019060685,
            temperature_mk=11.8,
        ),
        environment_b=EnvironmentConfig(
            t1_zero_temperature_us=2.5118864315095797,
            tphi_zero_temperature_us=2.4807298729688254,
            temperature_mk=11.8,
        ),
        duration_us=20.0,
        time_steps=11,
        fidelity_threshold=0.9,
        label_a="Low",
        label_b="High",
    ))


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from core.circuit_model import CircuitConfig
from core.io.report_export import export_markdown_report, markdown_report_text
from core.io.result_export import (
    export_result_csv,
    result_to_csv_text,
    result_to_dict,
    save_result_json,
)
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation


class ResultExportTest(unittest.TestCase):
    def test_result_json_export_contains_required_sections(self) -> None:
        result = _result()
        encoded = result_to_dict(result)

        self.assertEqual(encoded["kind"], "yuragi_strider.result")
        self.assertIn("summary", encoded)
        self.assertIn("timeseries", encoded)
        self.assertIn("derived_parameters", encoded)
        self.assertIn("diagnostics", encoded)
        json.dumps(encoded)

    def test_result_csv_contains_time_fidelity_and_purity(self) -> None:
        csv_text = result_to_csv_text(_result())

        self.assertIn("time_us,state_fidelity,purity", csv_text.splitlines()[0])
        self.assertGreater(len(csv_text.splitlines()), 1)

    def test_result_files_can_be_written(self) -> None:
        result = _result()
        with tempfile.TemporaryDirectory() as directory:
            json_path = Path(directory) / "result.qscope.result.json"
            csv_path = Path(directory) / "result.csv"
            md_path = Path(directory) / "report.md"

            save_result_json(result, json_path)
            export_result_csv(result, csv_path)
            export_markdown_report(result, md_path)

            self.assertIn("summary", json.loads(json_path.read_text(encoding="utf-8")))
            self.assertIn("time_us", csv_path.read_text(encoding="utf-8"))
            self.assertIn("Yuragi-Strider Simulation Report", md_path.read_text(encoding="utf-8"))

    def test_markdown_report_is_generated(self) -> None:
        report = markdown_report_text(_result())

        self.assertIn("# Yuragi-Strider Simulation Report", report)
        self.assertIn("## Derived Parameters", report)


def _result():
    return run_simulation(SimulationConfig(
        circuit=CircuitConfig.one_qubit_h(),
        environment=EnvironmentConfig(
            t1_zero_temperature_us=95.49925860214358,
            tphi_us=64.63587019060685,
            temperature_mk=11.8,
        ),
        duration_us=20.0,
        time_steps=11,
        fidelity_threshold=0.9,
    ))


if __name__ == "__main__":
    unittest.main()

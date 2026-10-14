from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.compare_phase3b_readout_model import analyze


class Phase3BReadoutModelReanalysisTests(unittest.TestCase):
    def _write_fixture(self, directory: Path) -> tuple[Path, Path]:
        raw = {
            "job_id": "job-1",
            "backend_properties": {"backend": "test"},
            "raw_counts": [
                {"kind": "readout_zero", "shots": 100, "counts": {"0": 90, "1": 10}},
                {"kind": "readout_one", "shots": 100, "counts": {"0": 20, "1": 80}},
                {"kind": "t1", "delay_us": 0.0, "shots": 100, "counts": {"0": 20, "1": 80}},
                {"kind": "t2_ramsey", "delay_us": 0.0, "shots": 100, "counts": {"0": 90, "1": 10}},
            ],
        }
        model = {
            "source_job_id": "job-1",
            "rows": [
                {"kind": "t1", "delay_us": 0.0, "native_model_p1": 1.0},
                {"kind": "t2", "delay_us": 0.0, "native_model_p1": 0.0},
            ],
        }
        raw_path = directory / "raw.json"
        model_path = directory / "model.json"
        raw_path.write_text(json.dumps(raw), encoding="utf-8")
        model_path.write_text(json.dumps(model), encoding="utf-8")
        return raw_path, model_path

    def test_model_side_readout_matches_fixture_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path, model_path = self._write_fixture(Path(temp_dir))
            report = analyze(
                raw_path,
                model_path,
                bootstrap_replicates=100,
                seed=1,
            )

        self.assertEqual(report["readout_calibration"]["p10_observed_1_given_0"], 0.1)
        self.assertAlmostEqual(
            report["readout_calibration"]["p01_observed_0_given_1"], 0.2
        )
        for row in report["rows"]:
            self.assertAlmostEqual(row["model_side_readout_absolute_difference"], 0.0)

    def test_job_id_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_path, model_path = self._write_fixture(Path(temp_dir))
            model = json.loads(model_path.read_text(encoding="utf-8"))
            model["source_job_id"] = "different-job"
            model_path.write_text(json.dumps(model), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "same job ID"):
                analyze(
                    raw_path,
                    model_path,
                    bootstrap_replicates=100,
                    seed=1,
                )


if __name__ == "__main__":
    unittest.main()

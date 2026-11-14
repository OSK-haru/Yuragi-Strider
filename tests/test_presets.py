import json
import unittest
from pathlib import Path

from core.io.config_io import load_config
from core.results import EnvironmentConfig, SimulationResult
from core.simulator import run_simulation


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRESETS = PROJECT_ROOT / "data" / "presets"


class PresetsTest(unittest.TestCase):
    def test_one_qubit_h_preset_loads_and_runs(self) -> None:
        config = load_config(PRESETS / "circuits" / "one_qubit_h.qscope.json")
        result = run_simulation(config)

        self.assertIsInstance(result, SimulationResult)
        self.assertEqual(result.issues, [])
        self.assertTrue(result.times)

    def test_bell_preset_loads_and_runs(self) -> None:
        config = load_config(PRESETS / "circuits" / "bell_state.qscope.json")
        result = run_simulation(config)

        self.assertIsInstance(result, SimulationResult)
        self.assertEqual(result.issues, [])
        self.assertEqual(set(result.output_probabilities), {"00", "01", "10", "11"})

    def test_environment_presets_are_valid(self) -> None:
        for path in (PRESETS / "environments").glob("*.json"):
            with self.subTest(path=path.name):
                environment = EnvironmentConfig.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                self.assertGreater(environment.t1_zero_temperature_us, 0.0)
                self.assertGreater(environment.tphi_us, 0.0)

    def test_example_presets_are_json_files(self) -> None:
        for path in (PRESETS / "examples").glob("*.qscope.json"):
            with self.subTest(path=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["schema_version"], "2.0")
                self.assertEqual(data["kind"], "yuragi_strider.comparison_config")


if __name__ == "__main__":
    unittest.main()

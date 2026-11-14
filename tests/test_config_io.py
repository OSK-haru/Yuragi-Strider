import json
import tempfile
import unittest
from pathlib import Path

from core.circuit_model import CircuitConfig
from core.io.config_io import (
    ConfigValidationError,
    config_from_dict,
    config_to_dict,
    load_config,
    save_config,
)
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult
from core.simulator import run_simulation
from core.circuit_state import CircuitState


class ConfigIoTest(unittest.TestCase):
    def test_config_can_be_saved_loaded_and_run(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig.one_qubit_h(),
            environment=EnvironmentConfig(
                t1_zero_temperature_us=95.49925860214358,
                tphi_us=64.63587019060685,
                temperature_mk=11.8,
            ),
            duration_us=20.0,
            time_steps=11,
            fidelity_threshold=0.9,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.qscope.json"
            save_config(config, path, metadata={"name": "demo"})
            loaded = load_config(path)

        result = run_simulation(loaded)

        self.assertIsInstance(result, SimulationResult)
        self.assertEqual(result.issues, [])
        self.assertEqual(loaded.to_dict(), config.to_dict())

    def test_loaded_config_round_trips_through_circuit_state(self) -> None:
        config = SimulationConfig(circuit=CircuitConfig.one_qubit_h())
        encoded = config_to_dict(config)
        loaded = config_from_dict(encoded)
        state = CircuitState.from_config(loaded.circuit)

        self.assertEqual(state.to_config().to_dict(), loaded.circuit.to_dict())

    def test_config_to_dict_uses_qscope_envelope(self) -> None:
        encoded = config_to_dict(SimulationConfig())

        self.assertEqual(encoded["schema_version"], "2.0")
        self.assertEqual(encoded["kind"], "yuragi_strider.config")
        self.assertIn("circuit", encoded)
        self.assertIn("environment", encoded)
        self.assertIn("t1_zero_temperature_us", encoded["environment"])
        self.assertIn("tphi_us", encoded["environment"])
        self.assertIn("simulation", encoded)
        json.dumps(encoded)

    def test_invalid_config_fails_validation(self) -> None:
        encoded = config_to_dict(SimulationConfig())
        encoded["environment"]["t1_zero_temperature_us"] = -1.0

        with self.assertRaises(ConfigValidationError) as context:
            config_from_dict(encoded)

        self.assertIn("INVALID_COHERENCE_TIME", {issue.code for issue in context.exception.issues})


if __name__ == "__main__":
    unittest.main()

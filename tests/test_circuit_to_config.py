import sys
import unittest

from core.circuit_model import GateOperation
from core.circuit_state import CircuitState
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult
from core.simulator import run_simulation


class CircuitToConfigTest(unittest.TestCase):
    def test_one_qubit_h_circuit_from_state_runs_simulation(self) -> None:
        state = CircuitState(logical_qubits=1, initial_states=["0"], columns=[])
        state.add_gate(
            0,
            GateOperation(
                type="H",
                targets=[0],
                controls=[],
                params={},
            ),
        )

        config = SimulationConfig(
            circuit=state.to_config(),
            environment=EnvironmentConfig(
                t1_zero_temperature_us=95.49925860214358,
                tphi_zero_temperature_us=64.63587019060685,
                temperature_mk=11.8,
            ),
            duration_us=20.0,
            time_steps=101,
            fidelity_threshold=0.9,
        )
        result = run_simulation(config)

        self.assertIsInstance(result, SimulationResult)
        self.assertEqual(len(result.times), 101)
        self.assertEqual(result.issues, [])
        self.assertNotIn("streamlit", sys.modules)

    def test_two_qubit_bell_circuit_runs_through_simulation_api(self) -> None:
        state = CircuitState(logical_qubits=2, initial_states=["0", "0"], columns=[])
        state.add_gate(
            0,
            GateOperation(
                type="H",
                targets=[0],
                controls=[],
                params={},
            ),
        )
        state.add_gate(
            1,
            GateOperation(
                type="CNOT",
                targets=[1],
                controls=[0],
                params={},
            ),
        )

        config = SimulationConfig(
            circuit=state.to_config(),
            environment=EnvironmentConfig(
                t1_zero_temperature_us=95.49925860214358,
                tphi_zero_temperature_us=64.63587019060685,
                temperature_mk=11.8,
            ),
            duration_us=20.0,
            time_steps=101,
            fidelity_threshold=0.9,
        )
        result = run_simulation(config)

        self.assertIsInstance(result, SimulationResult)
        self.assertEqual(result.issues, [])
        self.assertEqual(
            set(result.output_probabilities),
            {"00", "01", "10", "11"},
        )


if __name__ == "__main__":
    unittest.main()

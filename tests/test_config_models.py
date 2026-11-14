import json
import unittest

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import EnvironmentConfig, SimulationConfig, SimulationResult


class ConfigModelsTest(unittest.TestCase):
    def test_one_qubit_h_circuit_round_trips_through_dict(self) -> None:
        circuit = CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(
                            type="H",
                            targets=[0],
                            controls=[],
                            params={},
                        )
                    ],
                )
            ],
        )

        encoded = circuit.to_dict()
        decoded = CircuitConfig.from_dict(encoded)

        self.assertEqual(decoded.to_dict(), encoded)
        json.dumps(encoded)

    def test_simulation_config_is_json_serializable(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig.one_qubit_h(),
            environment=EnvironmentConfig(
                t1_zero_temperature_us=95.49925860214358,
                tphi_us=64.63587019060685,
                temperature_mk=11.8,
            ),
            duration_us=20.0,
            time_steps=101,
            fidelity_threshold=0.9,
        )

        encoded = config.to_dict()
        decoded = SimulationConfig.from_dict(encoded)

        self.assertEqual(decoded.to_dict(), encoded)
        json.dumps(encoded)

    def test_simulation_result_is_json_serializable(self) -> None:
        config = SimulationConfig()
        result = SimulationResult(
            config=config,
            times=[0.0, 1.0],
            fidelity=[1.0, 0.95],
            purity=[1.0, 0.99],
            effective_operation_time_us=1.0,
            output_probabilities={"0": 0.5, "1": 0.5},
            derived_parameters={"t1_us": 120.0, "t2_us": 90.0},
            diagnostics={"final_fidelity": 0.95},
            warnings=[],
        )

        encoded = result.to_dict()
        decoded = SimulationResult.from_dict(encoded)

        self.assertEqual(decoded.to_dict(), encoded)
        json.dumps(encoded)


if __name__ == "__main__":
    unittest.main()

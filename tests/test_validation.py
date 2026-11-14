import unittest
from unittest.mock import patch

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from core.validation import has_blocking_issues, validate_simulation_config


class ValidationTest(unittest.TestCase):
    def test_invalid_coherence_time_is_detected(self) -> None:
        issues = validate_simulation_config(
            SimulationConfig(
                environment=EnvironmentConfig(t1_zero_temperature_us=-1.0),
            )
        )

        self.assertIssueCode(issues, "INVALID_COHERENCE_TIME")
        self.assertTrue(has_blocking_issues(issues))

    def test_invalid_duration_us_is_detected(self) -> None:
        issues = validate_simulation_config(SimulationConfig(duration_us=0.0))

        self.assertIssueCode(issues, "INVALID_DURATION_US")
        self.assertTrue(has_blocking_issues(issues))

    def test_invalid_time_steps_is_detected(self) -> None:
        issues = validate_simulation_config(SimulationConfig(time_steps=1))

        self.assertIssueCode(issues, "INVALID_TIME_STEPS")
        self.assertTrue(has_blocking_issues(issues))

    def test_invalid_gate_target_is_detected(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig(
                logical_qubits=1,
                initial_states=["0"],
                columns=[
                    GateColumn(
                        step=0,
                        gates=[
                            GateOperation(
                                type="H",
                                targets=[1],
                                controls=[],
                                params={},
                            )
                        ],
                    )
                ],
            )
        )

        issues = validate_simulation_config(config)

        self.assertIssueCode(issues, "GATE_TARGET_OUT_OF_RANGE")
        self.assertTrue(has_blocking_issues(issues))

    def test_cnot_control_equal_target_is_detected(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig(
                logical_qubits=2,
                initial_states=["0", "0"],
                columns=[
                    GateColumn(
                        step=0,
                        gates=[
                            GateOperation(
                                type="CNOT",
                                targets=[0],
                                controls=[0],
                                params={},
                            )
                        ],
                    )
                ],
            )
        )

        issues = validate_simulation_config(config)

        self.assertIssueCode(issues, "CNOT_CONTROL_EQUALS_TARGET")
        self.assertTrue(has_blocking_issues(issues))

    def test_unsupported_gate_is_detected(self) -> None:
        config = SimulationConfig(
            circuit=CircuitConfig(
                logical_qubits=1,
                initial_states=["0"],
                columns=[
                    GateColumn(
                        step=0,
                        gates=[
                            GateOperation(
                                type="TOFFOLI",
                                targets=[0],
                                controls=[],
                                params={},
                            )
                        ],
                    )
                ],
            )
        )

        issues = validate_simulation_config(config)

        self.assertIssueCode(issues, "UNSUPPORTED_GATE")
        self.assertTrue(has_blocking_issues(issues))

    def test_three_logical_qubits_is_allowed(self) -> None:
        issues = validate_simulation_config(
            SimulationConfig(
                circuit=CircuitConfig(
                    logical_qubits=3,
                    initial_states=["0", "0", "0"],
                    columns=[],
                )
            )
        )

        self.assertNotIn("TOO_MANY_LOGICAL_QUBITS", {issue.code for issue in issues})
        self.assertFalse(has_blocking_issues(issues))

    def test_nineteen_logical_qubits_is_rejected(self) -> None:
        issues = validate_simulation_config(
            SimulationConfig(
                circuit=CircuitConfig(
                    logical_qubits=19,
                    initial_states=["0"] * 19,
                    columns=[],
                )
            )
        )

        self.assertIssueCode(issues, "TOO_MANY_LOGICAL_QUBITS")
        self.assertTrue(has_blocking_issues(issues))

    def test_run_simulation_does_not_execute_physics_for_blocking_config(self) -> None:
        config = SimulationConfig(
            environment=EnvironmentConfig(t1_zero_temperature_us=-1.0),
        )

        with patch("core.simulator.compute_environment_rates") as mapper:
            result = run_simulation(config)

        mapper.assert_not_called()
        self.assertEqual(result.times, [])
        self.assertEqual(result.fidelity, [])
        self.assertEqual(result.purity, [])
        self.assertIsNone(result.effective_operation_time_us)
        self.assertIssueCode(result.issues, "INVALID_COHERENCE_TIME")
        self.assertTrue(result.warnings)

    def assertIssueCode(self, issues, code: str) -> None:
        self.assertIn(code, {issue.code for issue in issues})


if __name__ == "__main__":
    unittest.main()

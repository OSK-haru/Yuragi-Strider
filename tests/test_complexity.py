import unittest

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.complexity import estimate_simulation_complexity
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation


class ComplexityTest(unittest.TestCase):
    def test_dimension_entry_and_matmul_scaling_follow_qubit_count(self) -> None:
        one = estimate_simulation_complexity(_config(1))
        two = estimate_simulation_complexity(_config(2))

        self.assertEqual(one["hilbert_dimension"], 2.0)
        self.assertEqual(two["hilbert_dimension"], 4.0)
        self.assertEqual(one["density_matrix_entries"], 4.0)
        self.assertEqual(two["density_matrix_entries"], 16.0)
        self.assertEqual(one["dense_matmul_scale"], 8.0)
        self.assertEqual(two["dense_matmul_scale"], 64.0)

    def test_collapse_operator_count_is_three_per_qubit(self) -> None:
        self.assertEqual(
            estimate_simulation_complexity(_config(1))["collapse_operator_count"],
            3.0,
        )
        self.assertEqual(
            estimate_simulation_complexity(_config(2))["collapse_operator_count"],
            6.0,
        )

    def test_run_simulation_result_contains_complexity_diagnostics(self) -> None:
        result = run_simulation(_config(1))

        for key in [
            "complexity_hilbert_dimension",
            "complexity_density_matrix_entries",
            "complexity_dense_matmul_scale",
            "complexity_collapse_operator_count",
            "complexity_estimated_rhs_evaluations",
            "complexity_estimated_rk4_steps",
            "complexity_estimated_work_units",
            "complexity_gate_segment_count",
            "complexity_idle_segment_count",
            "complexity_total_segment_count",
            "complexity_total_rk4_substeps",
            "complexity_total_rhs_evaluations",
            "complexity_gate_rk4_substeps",
            "complexity_idle_rk4_substeps",
            "complexity_max_hamiltonian_scale_per_us",
            "complexity_max_environment_rate_per_us",
            "complexity_max_generator_scale_per_us",
            "complexity_estimated_work_units_segmented",
        ]:
            self.assertIn(key, result.diagnostics)

    def test_increasing_qubits_increases_estimated_work_units(self) -> None:
        one = estimate_simulation_complexity(_config(1))
        two = estimate_simulation_complexity(_config(2))

        self.assertGreater(two["estimated_work_units"], one["estimated_work_units"])

    def test_increasing_time_steps_or_substeps_increases_work_units(self) -> None:
        base = estimate_simulation_complexity(_config(1, time_steps=11))
        more_steps = estimate_simulation_complexity(_config(1, time_steps=101))
        more_substeps = estimate_simulation_complexity(
            _config(1, time_steps=11),
            diagnostics={"integration_substeps": 5.0},
        )

        self.assertGreater(more_steps["estimated_work_units"], base["estimated_work_units"])
        self.assertGreater(more_substeps["estimated_work_units"], base["estimated_work_units"])

    def test_increasing_columns_increases_gate_aware_work_units(self) -> None:
        one_column = estimate_simulation_complexity(_config(1, columns=1))
        three_columns = estimate_simulation_complexity(_config(1, columns=3))

        self.assertGreater(
            three_columns["estimated_work_units"],
            one_column["estimated_work_units"],
        )

    def test_changing_cnot_duration_changes_total_gate_duration(self) -> None:
        short = run_simulation(_bell_config(cnot_duration_us=0.2, duration_us=20.0))
        long = run_simulation(_bell_config(cnot_duration_us=20.0, duration_us=20.02))

        self.assertNotEqual(
            short.diagnostics["total_gate_duration_us"],
            long.diagnostics["total_gate_duration_us"],
        )
        self.assertAlmostEqual(short.diagnostics["total_gate_duration_us"], 0.22)
        self.assertAlmostEqual(long.diagnostics["total_gate_duration_us"], 20.02)

    def test_fixed_total_duration_changes_idle_duration_inversely(self) -> None:
        short = run_simulation(_bell_config(cnot_duration_us=0.2, duration_us=20.0))
        long = run_simulation(_bell_config(cnot_duration_us=2.0, duration_us=20.0))

        self.assertGreater(
            short.diagnostics["idle_duration_us"],
            long.diagnostics["idle_duration_us"],
        )
        self.assertAlmostEqual(short.diagnostics["actual_duration_us"], 20.0)
        self.assertAlmostEqual(long.diagnostics["actual_duration_us"], 20.0)

    def test_fixed_post_gate_idle_increases_actual_duration_with_cnot_duration(self) -> None:
        short = run_simulation(_bell_config(cnot_duration_us=0.2, duration_us=5.22))
        long = run_simulation(_bell_config(cnot_duration_us=2.0, duration_us=7.02))

        self.assertAlmostEqual(short.diagnostics["idle_duration_us"], 5.0)
        self.assertAlmostEqual(long.diagnostics["idle_duration_us"], 5.0)
        self.assertGreater(
            long.diagnostics["actual_duration_us"],
            short.diagnostics["actual_duration_us"],
        )

    def test_segmented_work_units_change_when_hamiltonian_substeps_change(self) -> None:
        short = run_simulation(_bell_config(cnot_duration_us=0.2, duration_us=0.22))
        long = run_simulation(_bell_config(cnot_duration_us=20.0, duration_us=20.02))

        self.assertNotEqual(
            short.diagnostics["complexity_total_rk4_substeps"],
            long.diagnostics["complexity_total_rk4_substeps"],
        )
        self.assertNotEqual(
            short.diagnostics["complexity_estimated_work_units_segmented"],
            long.diagnostics["complexity_estimated_work_units_segmented"],
        )

    def test_time_steps_increase_recorded_points_and_memory_estimate(self) -> None:
        low = run_simulation(_bell_config(cnot_duration_us=0.2, duration_us=1.0, time_steps=11))
        high = run_simulation(_bell_config(cnot_duration_us=0.2, duration_us=1.0, time_steps=101))

        self.assertGreater(high.diagnostics["recorded_state_count"], low.diagnostics["recorded_state_count"])
        self.assertGreater(
            high.diagnostics["complexity_estimated_state_storage_entries"],
            low.diagnostics["complexity_estimated_state_storage_entries"],
        )

    def test_completion_and_final_metrics_differ_when_idle_is_present(self) -> None:
        result = run_simulation(_bell_config(cnot_duration_us=0.2, duration_us=20.0))

        self.assertGreater(result.diagnostics["idle_duration_us"], 0.0)
        self.assertNotAlmostEqual(
            result.diagnostics["completion_fidelity"],
            result.diagnostics["final_fidelity"],
            delta=1e-6,
        )

    def test_completion_and_final_metrics_match_without_idle(self) -> None:
        result = run_simulation(_bell_config(cnot_duration_us=0.2, duration_us=0.22))

        self.assertAlmostEqual(result.diagnostics["idle_duration_us"], 0.0)
        self.assertAlmostEqual(
            result.diagnostics["completion_fidelity"],
            result.diagnostics["final_fidelity"],
            delta=1e-10,
        )


def _config(
    qubits: int,
    columns: int = 1,
    time_steps: int = 11,
) -> SimulationConfig:
    circuit_columns = []
    for step in range(columns):
        if qubits == 1:
            gate = GateOperation(type="H", targets=[0])
        else:
            gate = (
                GateOperation(type="H", targets=[0])
                if step % 2 == 0
                else GateOperation(type="CNOT", targets=[1], controls=[0])
            )
        circuit_columns.append(GateColumn(step=step, gates=[gate]))
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=qubits,
            initial_states=["0"] * qubits,
            columns=circuit_columns,
        ),
        environment=EnvironmentConfig(temperature_mk=15.0, ideal_reference=True),
        duration_us=1.0,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


def _bell_config(
    cnot_duration_us: float,
    duration_us: float,
    time_steps: int = 51,
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(
                    step=1,
                    gates=[
                        GateOperation(
                            type="CNOT",
                            targets=[1],
                            controls=[0],
                            params={"duration_us": cnot_duration_us},
                        )
                    ],
                ),
            ],
        ),
        environment=EnvironmentConfig(t1_zero_temperature_us=10.0, tphi_zero_temperature_us=10.0, temperature_mk=0.0),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
    )


if __name__ == "__main__":
    unittest.main()

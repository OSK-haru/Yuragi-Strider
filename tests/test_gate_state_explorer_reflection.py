import math
import unittest

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation
from core.ui_response import simulation_result_to_ui_response


class GateStateExplorerReflectionTests(unittest.TestCase):
    def test_h_h_ccx_circuit_returns_three_qubit_snapshots_and_expected_population(self):
        circuit = CircuitConfig(
            logical_qubits=3,
            initial_states=["0", "0", "0"],
            columns=[
                GateColumn(0, []),
                GateColumn(1, [GateOperation(
                    "H", [0], params={"duration_us": 0.02},
                )]),
                GateColumn(2, [GateOperation(
                    "H", [1], params={"duration_us": 0.02},
                )]),
                GateColumn(3, [GateOperation(
                    "CCX", [2], controls=[0, 1], params={"duration_us": 0.4},
                )]),
            ],
        )

        result = run_simulation(_ideal_config(circuit))
        response = simulation_result_to_ui_response(run_simulation(_finite_ui_config(circuit)))
        after_circuit = next(
            snapshot for snapshot in result.state_snapshots
            if snapshot.kind == "after_circuit"
        )
        ideal_response_after_circuit = next(
            snapshot for snapshot in response["run"]["comparison"]["ideal_state_snapshots"]
            if snapshot["kind"] == "after_circuit"
        )
        noisy_response_after_circuit = next(
            snapshot for snapshot in response["state_snapshots"]
            if snapshot["kind"] == "after_circuit"
        )
        noisy_response_final = next(
            snapshot for snapshot in reversed(response["state_snapshots"])
            if snapshot["kind"] == "final"
        )

        self.assertEqual(response["circuit"]["qubit_count"], 3)
        self.assertLess(
            noisy_response_after_circuit["time_us"],
            noisy_response_final["time_us"],
        )
        self.assertTrue(all(len(row) == 8 for row in after_circuit.density_matrix))
        self.assertEqual(len(after_circuit.density_matrix), 8)
        for basis_index in range(8):
            basis_label = format(basis_index, "03b")
            self.assertAlmostEqual(
                ideal_response_after_circuit["density_matrix"]["real"][basis_index][basis_index],
                after_circuit.density_matrix[basis_index][basis_index].real,
                delta=1e-12,
            )
            self.assertAlmostEqual(
                response["output_probabilities"][basis_label],
                noisy_response_final["density_matrix"]["real"][basis_index][basis_index],
                delta=1e-12,
            )

        for basis_index in (0, 2, 4, 7):
            self.assertAlmostEqual(
                after_circuit.density_matrix[basis_index][basis_index].real,
                0.25,
                delta=1e-12,
            )
        for basis_index in (1, 3, 5, 6):
            self.assertAlmostEqual(
                after_circuit.density_matrix[basis_index][basis_index].real,
                0.0,
                delta=1e-12,
            )

    def test_rz_phase_is_present_in_boundary_snapshots_even_when_quality_metrics_stay_one(self):
        circuit = CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(0, [GateOperation(
                    "H", [0], params={"duration_us": 0.02},
                )]),
                GateColumn(1, [GateOperation(
                    "RZ", [0],
                    params={"duration_us": 0.02, "theta_rad": math.pi / 2.0},
                )]),
            ],
        )

        result = run_simulation(_ideal_config(circuit))
        after_h = next(
            snapshot
            for snapshot in result.state_snapshots
            if snapshot.kind == "column_boundary" and snapshot.column_index == 0
        )
        after_rz = next(
            snapshot for snapshot in result.state_snapshots
            if snapshot.kind == "after_circuit"
        )

        self.assertAlmostEqual(after_h.density_matrix[0][1].real, 0.5, delta=1e-12)
        self.assertAlmostEqual(after_h.density_matrix[0][1].imag, 0.0, delta=1e-12)
        self.assertAlmostEqual(after_rz.density_matrix[0][1].real, 0.0, delta=1e-12)
        self.assertAlmostEqual(after_rz.density_matrix[0][1].imag, -0.5, delta=1e-12)
        self.assertTrue(all(abs(value - 1.0) < 1e-12 for value in result.fidelity))
        self.assertTrue(all(abs(value - 1.0) < 1e-12 for value in result.purity))

    def test_ccx_active_controls_move_population_in_after_circuit_snapshot(self):
        circuit = CircuitConfig(
            logical_qubits=3,
            initial_states=["1", "1", "0"],
            columns=[GateColumn(0, [GateOperation(
                "CCX", [2], controls=[0, 1], params={"duration_us": 0.4},
            )])],
        )

        result = run_simulation(_ideal_config(circuit))
        initial = next(snapshot for snapshot in result.state_snapshots if snapshot.kind == "initial")
        after_ccx = next(
            snapshot for snapshot in result.state_snapshots
            if snapshot.kind == "after_circuit"
        )

        self.assertAlmostEqual(initial.density_matrix[6][6].real, 1.0, delta=1e-12)
        self.assertAlmostEqual(initial.density_matrix[7][7].real, 0.0, delta=1e-12)
        self.assertAlmostEqual(after_ccx.density_matrix[6][6].real, 0.0, delta=1e-12)
        self.assertAlmostEqual(after_ccx.density_matrix[7][7].real, 1.0, delta=1e-12)
        self.assertAlmostEqual(result.fidelity[-1], 1.0, delta=1e-12)
        self.assertAlmostEqual(result.purity[-1], 1.0, delta=1e-12)

    def test_rz_on_a_basis_state_correctly_has_no_observable_state_change(self):
        circuit = CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[GateColumn(0, [GateOperation(
                "RZ", [0],
                params={"duration_us": 0.02, "theta_rad": math.pi / 2.0},
            )])],
        )

        result = run_simulation(_ideal_config(circuit))
        initial = next(snapshot for snapshot in result.state_snapshots if snapshot.kind == "initial")
        after_rz = next(
            snapshot for snapshot in result.state_snapshots
            if snapshot.kind == "after_circuit"
        )

        for row in range(2):
            for column in range(2):
                self.assertAlmostEqual(
                    abs(after_rz.density_matrix[row][column] - initial.density_matrix[row][column]),
                    0.0,
                    delta=1e-12,
                )


def _ideal_config(circuit: CircuitConfig) -> SimulationConfig:
    return SimulationConfig(
        circuit=circuit,
        environment=EnvironmentConfig(temperature_mk=0.0, ideal_reference=True),
        duration_us=1.0,
        time_steps=21,
        fidelity_threshold=0.9,
        snapshot_options={
            "enabled": True,
            "uniform_count": 2,
            "include_initial": True,
            "include_final": True,
            "include_column_boundaries": True,
            "include_after_circuit": True,
        },
    )


def _finite_ui_config(circuit: CircuitConfig) -> SimulationConfig:
    return SimulationConfig(
        circuit=circuit,
        environment=EnvironmentConfig(t1_zero_temperature_us=1e9, tphi_zero_temperature_us=999999999.9999999, temperature_mk=0.0),
        duration_us=1.0,
        time_steps=21,
        fidelity_threshold=0.9,
        snapshot_options={
            "enabled": True,
            "uniform_count": 2,
            "include_initial": True,
            "include_final": True,
            "include_column_boundaries": True,
            "include_after_circuit": True,
        },
    )


if __name__ == "__main__":
    unittest.main()

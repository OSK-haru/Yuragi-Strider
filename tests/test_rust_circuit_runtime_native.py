"""Integration checks against the built PyO3 extension."""

import unittest

from core.backend_boundary import is_rust_backend_available, RUST_DENSE_PREVIEW_BACKEND
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.rust_circuit_runtime import (
    apply_readout_rust,
    execute_ideal_circuit_rust,
    execute_post_circuit_gates_rust,
)
from core.state_snapshots import StateSnapshotCollector


@unittest.skipUnless(is_rust_backend_available(), "Rust extension is not built")
class RustCircuitRuntimeNativeTest(unittest.TestCase):
    def test_ideal_bell_execution_and_readout_assignment(self) -> None:
        circuit = CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(0, [GateOperation("H", [0])]),
                GateColumn(1, [GateOperation("CNOT", [1], controls=[0])]),
            ],
        )
        ideal, observed = execute_ideal_circuit_rust(circuit, None)
        self.assertAlmostEqual(ideal["00"], 0.5, places=12)
        self.assertAlmostEqual(ideal["11"], 0.5, places=12)
        self.assertAlmostEqual(observed["00"], 0.5, places=12)

        assigned = apply_readout_rust(ideal, 2, [(0.1, 0.2), (0.3, 0.4)])
        self.assertAlmostEqual(assigned["00"], 0.5 * 0.9 * 0.7, places=12)
        self.assertAlmostEqual(assigned["11"], 0.5 * 0.1 * 0.3, places=12)
        self.assertAlmostEqual(sum(assigned.values()), 1.0, places=12)

    def test_post_circuit_measurement_removes_only_measured_coherence(self) -> None:
        circuit = CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(0, [GateOperation("H", [0]), GateOperation("H", [1])]),
                GateColumn(1, [GateOperation("MEASURE", [0])]),
            ],
        )
        state = execute_post_circuit_gates_rust(circuit)
        self.assertAlmostEqual(state[0][0].real, 0.25, places=12)
        self.assertAlmostEqual(state[3][3].real, 0.25, places=12)
        self.assertAlmostEqual(state[0][3], 0.0, places=12)
        self.assertAlmostEqual(state[0][1].real, 0.25, places=12)
        self.assertAlmostEqual(state[0][2], 0.0, places=12)

    def test_snapshot_candidate_selection_runs_in_rust(self) -> None:
        collector = StateSnapshotCollector(
            actual_duration_us=2.0,
            max_snapshots=3,
            simulation_backend=RUST_DENSE_PREVIEW_BACKEND,
        )
        matrix = ((1.0 + 0.0j,),)
        for time_us, kind in (
            (0.0, "initial"),
            (0.5, "uniform_time"),
            (1.0, "custom_time"),
            (1.5, "column_boundary"),
            (2.0, "final"),
        ):
            if kind in {"initial", "final"}:
                collector.capture_event(time_us=time_us, event_kind=kind, density_matrix=matrix)
            else:
                collector.capture(time_us=time_us, kind=kind, density_matrix=matrix)

        snapshots, diagnostics = collector.finalize()
        self.assertEqual([snapshot.kind for snapshot in snapshots], ["initial", "custom_time", "final"])
        self.assertEqual(diagnostics.selection_backend, RUST_DENSE_PREVIEW_BACKEND)
        self.assertEqual(diagnostics.selection_fallback_reason, "")


if __name__ == "__main__":
    unittest.main()

import math
import unittest

from pydantic import ValidationError

from api.main import SimulateRequest, simulate
from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation


class SnapshotRequestPolicyTests(unittest.TestCase):
    def test_missing_options_keeps_legacy_bounded_policy(self) -> None:
        result = run_simulation(_config(_empty_circuit(2)))

        self.assertEqual("bounded_semantic_v1", result.diagnostics["state_snapshot_policy"])
        self.assertLessEqual(len(result.state_snapshots), 10)

    def test_custom_and_uniform_times_are_exact_and_metadata_is_explicit(self) -> None:
        config = _config(
            _empty_circuit(2),
            duration_us=2.0,
            snapshot_options={
                "enabled": True,
                "uniform_count": 4,
                "custom_times_us": (0.5, 1.25),
            },
        )

        result = run_simulation(config)

        custom = next(
            snapshot for snapshot in result.state_snapshots
            if snapshot.kind == "custom_time" and math.isclose(snapshot.time_us, 1.25)
        )
        self.assertEqual(1.25, custom.requested_time_us)
        self.assertEqual(1.25, custom.time_us)
        self.assertEqual("exact_integration_boundary", custom.capture_method)
        self.assertIsNone(custom.event_kind)
        self.assertEqual("cptp_ready_requested_v1", result.diagnostics["state_snapshot_policy"])
        self.assertEqual(4.0, result.diagnostics["state_snapshot_requested_uniform_count"])
        self.assertEqual(2.0, result.diagnostics["state_snapshot_requested_custom_count"])
        self.assertEqual(0.0, result.diagnostics["state_snapshot_max_time_error_us"])

    def test_custom_times_have_priority_over_uniform_samples_under_cap(self) -> None:
        custom_times = tuple(index * 0.01 for index in range(1, 101))
        result = run_simulation(_config(
            _empty_circuit(2),
            duration_us=2.0,
            snapshot_options={
                "enabled": True,
                "uniform_count": 100,
                "custom_times_us": custom_times,
            },
        ))

        self.assertLessEqual(len(result.state_snapshots), 100)
        returned_custom = [
            snapshot for snapshot in result.state_snapshots
            if snapshot.kind == "custom_time"
        ]
        self.assertGreaterEqual(len(returned_custom), 95)
        self.assertEqual(
            sorted(snapshot.time_us for snapshot in result.state_snapshots),
            [snapshot.time_us for snapshot in result.state_snapshots],
        )

    def test_event_metadata_preserves_zero_based_column_index(self) -> None:
        result = run_simulation(_config(
            _bell_circuit(),
            duration_us=1.0,
            snapshot_options={
                "enabled": True,
                "uniform_count": 0,
                "custom_times_us": (),
            },
        ))

        boundaries = [
            snapshot for snapshot in result.state_snapshots
            if snapshot.kind == "column_boundary"
        ]
        self.assertEqual([0], [snapshot.column_index for snapshot in boundaries])
        self.assertTrue(all(snapshot.event_kind == "column_boundary" for snapshot in boundaries))
        self.assertTrue(all(snapshot.requested_time_us is None for snapshot in boundaries))

    def test_api_rejects_invalid_snapshot_options(self) -> None:
        base = {
            "circuit_preset": "bell",
            "simulation_backend": "python_dense",
            "parameters": {
                "temperature_mk": 28.0,
                "qubit_frequency_ghz": 5.0,
                "t1_zero_temperature_us": 63.09573444801933,
                "tphi_zero_temperature_us": 45.159858934213595,
                "readout_resonator_frequency_ghz": 7.0,
                "readout_resonator_kappa_mhz": 1.0,
                "readout_resonator_chi_mhz": 0.5,
                "duration_us": 2.0,
                "time_steps": 11,
                "fidelity_threshold": 0.9,
            },
        }
        for options in (
            {"uniform_count": 1},
            {"uniform_count": 101},
            {"custom_times_us": [-0.1]},
            {"custom_times_us": [2.1]},
            {"custom_times_us": [float("nan")]},
        ):
            with self.subTest(options=options):
                with self.assertRaises(ValidationError):
                    SimulateRequest(**base, snapshot_options=options)


class SnapshotQubitRegressionTests(unittest.TestCase):
    def test_exact_capture_is_numerically_safe_for_two_three_and_four_qubits(self) -> None:
        for qubit_count in (2, 3, 4):
            with self.subTest(qubit_count=qubit_count):
                result = run_simulation(_config(
                    _empty_circuit(qubit_count),
                    duration_us=0.2,
                    time_steps=5,
                    snapshot_options={
                        "enabled": True,
                        "uniform_count": 2,
                        "custom_times_us": (0.05, 0.15),
                    },
                ))
                self.assertLessEqual(len(result.state_snapshots), 100)
                for snapshot in result.state_snapshots:
                    matrix = snapshot.density_matrix
                    self.assertTrue(all(math.isfinite(value.real) and math.isfinite(value.imag)
                                        for row in matrix for value in row))
                    trace = sum(matrix[index][index] for index in range(len(matrix)))
                    self.assertAlmostEqual(1.0, trace.real, delta=1e-9)
                    self.assertAlmostEqual(0.0, trace.imag, delta=1e-9)


def _config(circuit: CircuitConfig, *, duration_us: float = 1.0, time_steps: int = 21,
            snapshot_options=None) -> SimulationConfig:
    return SimulationConfig(
        circuit=circuit,
        environment=EnvironmentConfig(),
        duration_us=duration_us,
        time_steps=time_steps,
        fidelity_threshold=0.9,
        snapshot_options=snapshot_options,
    )


def _empty_circuit(logical_qubits: int) -> CircuitConfig:
    return CircuitConfig(
        logical_qubits=logical_qubits,
        initial_states=["0" for _ in range(logical_qubits)],
        columns=[],
    )


def _bell_circuit() -> CircuitConfig:
    return CircuitConfig(
        logical_qubits=2,
        initial_states=["0", "0"],
        columns=[
            GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
            GateColumn(step=1, gates=[GateOperation(type="CNOT", controls=[0], targets=[1])]),
        ],
    )


if __name__ == "__main__":
    unittest.main()

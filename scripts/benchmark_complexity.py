"""Benchmark dense Lindblad complexity cases with standard-library tools."""

from __future__ import annotations

import argparse
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import EnvironmentConfig, SimulationConfig
from core.simulator import run_simulation


CSV_COLUMNS = [
    "backend_requested",
    "backend_name",
    "backend_fallback_used",
    "backend_fallback_reason",
    "rust_kernel_used",
    "rust_kernel_mode",
    "rust_kernel_fallback_used",
    "rust_kernel_call_count",
    "rust_kernel_segment_count",
    "rust_kernel_substep_count",
    "rust_kernel_batchable_interval_count",
    "rust_kernel_actual_batch_count",
    "rust_kernel_max_batch_substeps",
    "rust_kernel_mean_batch_substeps",
    "rust_kernel_batch_blocked_by_sampling_count",
    "rust_kernel_batch_blocked_by_boundary_count",
    "rust_kernel_sampled_batch_count",
    "rust_kernel_sampled_returned_state_count",
    "rust_kernel_max_sampled_batch_outputs",
    "rust_kernel_mean_sampled_batch_outputs",
    "rust_kernel_sampled_batch_fallback_count",
    "rust_kernel_sampled_batch_fallback_reason",
    "python_kernel_segment_count",
    "python_kernel_substep_count",
    "group",
    "case",
    "qubits",
    "gates",
    "columns",
    "configured_duration_us",
    "total_gate_duration_us",
    "idle_duration_us",
    "actual_duration_us",
    "time_steps",
    "wall_time_seconds",
    "peak_memory_bytes",
    "completion_fidelity",
    "final_fidelity",
    "final_purity",
    "completion_purity",
    "complexity_total_rk4_substeps",
    "complexity_total_rhs_evaluations",
    "complexity_estimated_work_units_segmented",
]


def main() -> None:
    args = _parse_args()
    cases = [
        ("baseline", "1Q H", _one_qubit(["H"])),
        ("baseline", "1Q HX", _one_qubit(["H", "X"])),
        ("baseline", "2Q Bell", _bell(0.2, 1.0)),
        ("long gate", "2Q Bell long CNOT", _bell(20.0, 20.02)),
        ("A fixed total", "2Q Bell CNOT 0.2 total 20", _bell(0.2, 20.0)),
        ("A fixed total", "2Q Bell CNOT 2 total 20", _bell(2.0, 20.0)),
        ("B fixed idle", "2Q Bell CNOT 0.2 idle 5", _bell_with_idle(0.2, 5.0)),
        ("B fixed idle", "2Q Bell CNOT 2 idle 5", _bell_with_idle(2.0, 5.0)),
        ("C completion-only", "2Q Bell CNOT 0.2 no idle", _bell_completion_only(0.2)),
        ("C completion-only", "2Q Bell CNOT 20 no idle", _bell_completion_only(20.0)),
        ("experimental", "3Q GHZ-like", _three_qubit_ghz_like()),
    ]
    print(",".join(CSV_COLUMNS))
    for group, name, config in cases:
        config.simulation_backend = args.backend
        result, wall_time, peak_memory = _run_profiled(config)
        diagnostics = result.diagnostics
        if not result.times:
            print(",".join(str(value) for value in [
                diagnostics.get("backend_requested", config.simulation_backend),
                "not_run",
                diagnostics.get("backend_fallback_used", ""),
                diagnostics.get("backend_fallback_reason", ""),
                diagnostics.get("rust_kernel_used", ""),
                diagnostics.get("rust_kernel_mode", ""),
                diagnostics.get("rust_kernel_fallback_used", ""),
                diagnostics.get("rust_kernel_call_count", ""),
                diagnostics.get("rust_kernel_segment_count", ""),
                diagnostics.get("rust_kernel_substep_count", ""),
                diagnostics.get("rust_kernel_batchable_interval_count", ""),
                diagnostics.get("rust_kernel_actual_batch_count", ""),
                diagnostics.get("rust_kernel_max_batch_substeps", ""),
                diagnostics.get("rust_kernel_mean_batch_substeps", ""),
                diagnostics.get("rust_kernel_batch_blocked_by_sampling_count", ""),
                diagnostics.get("rust_kernel_batch_blocked_by_boundary_count", ""),
                diagnostics.get("rust_kernel_sampled_batch_count", ""),
                diagnostics.get("rust_kernel_sampled_returned_state_count", ""),
                diagnostics.get("rust_kernel_max_sampled_batch_outputs", ""),
                diagnostics.get("rust_kernel_mean_sampled_batch_outputs", ""),
                diagnostics.get("rust_kernel_sampled_batch_fallback_count", ""),
                diagnostics.get("rust_kernel_sampled_batch_fallback_reason", ""),
                diagnostics.get("python_kernel_segment_count", ""),
                diagnostics.get("python_kernel_substep_count", ""),
                group,
                name,
                config.circuit.logical_qubits,
                _gate_count(config),
                len(config.circuit.columns),
                config.duration_us,
                "",
                "",
                "",
                config.time_steps,
                f"{wall_time:.6f}",
                peak_memory,
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ]))
            continue
        print(",".join(str(value) for value in [
            diagnostics.get("backend_requested", config.simulation_backend),
            diagnostics.get("backend_name", "unknown"),
            diagnostics.get("backend_fallback_used", ""),
            diagnostics.get("backend_fallback_reason", ""),
            diagnostics.get("rust_kernel_used", ""),
            diagnostics.get("rust_kernel_mode", ""),
            diagnostics.get("rust_kernel_fallback_used", ""),
            diagnostics.get("rust_kernel_call_count", ""),
            diagnostics.get("rust_kernel_segment_count", ""),
            diagnostics.get("rust_kernel_substep_count", ""),
            diagnostics.get("rust_kernel_batchable_interval_count", ""),
            diagnostics.get("rust_kernel_actual_batch_count", ""),
            diagnostics.get("rust_kernel_max_batch_substeps", ""),
            diagnostics.get("rust_kernel_mean_batch_substeps", ""),
            diagnostics.get("rust_kernel_batch_blocked_by_sampling_count", ""),
            diagnostics.get("rust_kernel_batch_blocked_by_boundary_count", ""),
            diagnostics.get("rust_kernel_sampled_batch_count", ""),
            diagnostics.get("rust_kernel_sampled_returned_state_count", ""),
            diagnostics.get("rust_kernel_max_sampled_batch_outputs", ""),
            diagnostics.get("rust_kernel_mean_sampled_batch_outputs", ""),
            diagnostics.get("rust_kernel_sampled_batch_fallback_count", ""),
            diagnostics.get("rust_kernel_sampled_batch_fallback_reason", ""),
            diagnostics.get("python_kernel_segment_count", ""),
            diagnostics.get("python_kernel_substep_count", ""),
            group,
            name,
            config.circuit.logical_qubits,
            _gate_count(config),
            len(config.circuit.columns),
            config.duration_us,
            f"{diagnostics.get('total_gate_duration_us', 0.0):.12g}",
            f"{diagnostics.get('idle_duration_us', 0.0):.12g}",
            f"{diagnostics.get('actual_duration_us', config.duration_us):.12g}",
            config.time_steps,
            f"{wall_time:.6f}",
            peak_memory,
            f"{diagnostics.get('completion_fidelity', 0.0):.12g}",
            f"{diagnostics.get('final_fidelity', result.fidelity[-1]):.12g}",
            f"{diagnostics.get('final_purity', result.purity[-1]):.12g}",
            f"{diagnostics.get('completion_purity', 0.0):.12g}",
            f"{diagnostics.get('complexity_total_rk4_substeps', 0.0):.12g}",
            f"{diagnostics.get('complexity_total_rhs_evaluations', 0.0):.12g}",
            f"{diagnostics.get('complexity_estimated_work_units_segmented', 0.0):.12g}",
        ]))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark dense Lindblad complexity cases.",
    )
    parser.add_argument(
        "--backend",
        choices=["python_dense", "rust_dense_preview"],
        default="python_dense",
        help="Requested simulation backend boundary target.",
    )
    return parser.parse_args()


def _run_profiled(config: SimulationConfig):
    tracemalloc.start()
    start = time.perf_counter()
    result = run_simulation(config)
    wall_time = time.perf_counter() - start
    _, peak_memory = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, wall_time, peak_memory


def _environment() -> EnvironmentConfig:
    return EnvironmentConfig(
        temperature_mk=0.0,
        t1_zero_temperature_us=100.0,
        tphi_us=100.0,
    )


def _one_qubit(gates: list[str]) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(
                    step=index,
                    gates=[GateOperation(type=gate, targets=[0])],
                )
                for index, gate in enumerate(gates)
            ],
        ),
        environment=_environment(),
        duration_us=1.0,
        time_steps=51,
        fidelity_threshold=0.9,
    )


def _bell(cnot_duration_us: float = 0.2, duration_us: float = 1.0) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[GateOperation(type="H", targets=[0])],
                ),
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
        environment=_environment(),
        duration_us=duration_us,
        time_steps=51,
        fidelity_threshold=0.9,
    )


def _bell_with_idle(cnot_duration_us: float, idle_duration_us: float) -> SimulationConfig:
    return _bell(
        cnot_duration_us=cnot_duration_us,
        duration_us=0.02 + cnot_duration_us + idle_duration_us,
    )


def _bell_completion_only(cnot_duration_us: float) -> SimulationConfig:
    return _bell(
        cnot_duration_us=cnot_duration_us,
        duration_us=0.02 + cnot_duration_us,
    )


def _three_qubit_ghz_like() -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=3,
            initial_states=["0", "0", "0"],
            columns=[
                GateColumn(step=0, gates=[GateOperation(type="H", targets=[0])]),
                GateColumn(
                    step=1,
                    gates=[GateOperation(type="CNOT", targets=[1], controls=[0])],
                ),
                GateColumn(
                    step=2,
                    gates=[GateOperation(type="CNOT", targets=[2], controls=[1])],
                ),
            ],
        ),
        environment=_environment(),
        duration_us=1.0,
        time_steps=21,
        fidelity_threshold=0.9,
    )


def _gate_count(config: SimulationConfig) -> int:
    return sum(len(column.gates) for column in config.circuit.columns)


if __name__ == "__main__":
    main()

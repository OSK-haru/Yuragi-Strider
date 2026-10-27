"""Compare Python and Rust batched local-reset transitions on identical states."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.register_operations import apply_local_replacement, freeze_register
from core.rust_dense_kernel import (
    is_rust_kernel_available,
    rust_apply_local_reset_sequence,
)


def _median_ms(operation: Callable[[], object], warmups: int, repeats: int) -> float:
    for _ in range(warmups):
        operation()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return statistics.median(samples)


def _python_peak_bytes(operation: Callable[[], object]) -> int:
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        operation()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qubits", type=int, default=4)
    parser.add_argument("--reset-count", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.qubits <= 8 or args.reset_count < 0 or args.warmups < 0 or args.repeats < 1:
        parser.error("qubits must be 1..8; reset-count non-negative; repeats positive")
    if not is_rust_kernel_available():
        print(json.dumps({"status": "unavailable", "reason": "yuragi_strider_rust is not importable"}))
        return 2

    rng = np.random.default_rng(20261028)
    dimension = 1 << args.qubits
    ket = rng.normal(size=dimension) + 1j * rng.normal(size=dimension)
    ket /= np.linalg.norm(ket)
    rho = freeze_register(np.outer(ket, ket.conj()))
    targets = tuple(range(args.qubits))
    probabilities = tuple(0.2 + 0.05 * (target % 4) for target in targets)
    populations = tuple((1.0 - (0.05 + 0.02 * (target % 3)), 0.05 + 0.02 * (target % 3)) for target in targets)

    def python_run() -> object:
        state = np.asarray(rho, dtype=complex)
        for _ in range(args.reset_count):
            for target, success, target_state in zip(targets, probabilities, populations, strict=True):
                state = apply_local_replacement(state, target, success, target_state)
        return state

    def rust_run() -> object:
        return rust_apply_local_reset_sequence(
            rho,
            subsystem_count=args.qubits,
            local_dimension=2,
            targets=targets,
            success_probabilities=probabilities,
            target_populations=populations,
            reset_count=args.reset_count,
        )

    python_result = np.asarray(python_run(), dtype=complex)
    rust_result = np.asarray(rust_run(), dtype=complex)
    matrix_f64_values = 2 * dimension * dimension
    parameter_f64_values = len(targets) * 3
    print(json.dumps({
        "status": "ok",
        "algorithm": "local replacement channel sequence",
        "local_reset_schema_version": "local_reset_sequence_v1",
        "qubits": args.qubits,
        "density_matrix_dimension": dimension,
        "target_count": len(targets),
        "reset_count": args.reset_count,
        "warmups": args.warmups,
        "repeats": args.repeats,
        "python_end_to_end_median_ms": _median_ms(python_run, args.warmups, args.repeats),
        "rust_pyo3_end_to_end_median_ms": _median_ms(rust_run, args.warmups, args.repeats),
        "python_tracemalloc_peak_bytes": _python_peak_bytes(python_run),
        "rust_wrapper_python_tracemalloc_peak_bytes": _python_peak_bytes(rust_run),
        "native_rust_memory_peak_bytes": None,
        "pyo3_calls_per_sequence": 1,
        "matrix_f64_values_including_input_and_result": matrix_f64_values * 2,
        "parameter_f64_values": parameter_f64_values,
        "target_index_values": len(targets),
        "estimated_numeric_payload_bytes_lower_bound": 8 * (
            matrix_f64_values * 2 + parameter_f64_values + len(targets)
        ),
        "max_abs_output_difference": float(np.max(np.abs(python_result - rust_result))),
        "trace_difference": float(abs(np.trace(python_result) - np.trace(rust_result))),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

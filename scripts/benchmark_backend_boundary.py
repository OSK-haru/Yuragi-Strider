"""Compare one raw Lindblad RK4 segment across the Python and Rust backends.

Run with the project's locked runtime and a built `yuragi_strider_rust` module.
The report separates Python flatten/reconstruction cost from end-to-end calls;
PyO3's internal conversion and kernel time cannot be separated by this wrapper.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.gates import (
    SIGMA_MINUS,
    prepare_collapse_operators,
    rk4_step_cached,
    scale,
    zero_hamiltonian,
)
from core.rust_dense_kernel import (
    flatten_collapse_ops,
    flatten_complex_matrix,
    is_rust_kernel_available,
    rust_rk4_evolve_segment,
    unflatten_complex_matrix,
)


def _median_ms(operation: Callable[[], object], warmups: int, repeats: int) -> float:
    for _ in range(warmups):
        operation()
    samples: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return statistics.median(samples)


def _python_traced_peak_bytes(operation: Callable[[], object]) -> int:
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        operation()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--substeps", type=int, default=500)
    parser.add_argument("--dt", type=float, default=0.0002)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    if args.substeps <= 0 or args.warmups < 0 or args.repeats <= 0:
        parser.error("substeps/repeats must be positive and warmups non-negative")

    if not is_rust_kernel_available():
        print(json.dumps({"status": "unavailable", "reason": "yuragi_strider_rust is not importable"}))
        return 2

    rho = ((1.0 + 0.0j, 0.0j), (0.0j, 0.0j))
    hamiltonian = zero_hamiltonian(2)
    collapses = [scale(0.03**0.5, SIGMA_MINUS)]
    prepared = prepare_collapse_operators(collapses)

    def python_run() -> object:
        state = rho
        for _ in range(args.substeps):
            state = rk4_step_cached(state, hamiltonian, prepared, args.dt)
        return state

    def rust_run() -> object:
        return rust_rk4_evolve_segment(rho, hamiltonian, collapses, args.dt, args.substeps)

    flat_rho = flatten_complex_matrix(rho)
    flat_h = flatten_complex_matrix(hamiltonian)
    flat_ops = flatten_collapse_ops(collapses)
    flat_result = flatten_complex_matrix(rust_run())
    python_ms = _median_ms(python_run, args.warmups, args.repeats)
    rust_ms = _median_ms(rust_run, args.warmups, args.repeats)
    python_peak_bytes = _python_traced_peak_bytes(python_run)
    rust_python_peak_bytes = _python_traced_peak_bytes(rust_run)
    flatten_ms = _median_ms(
        lambda: (flatten_complex_matrix(rho), flatten_complex_matrix(hamiltonian), flatten_collapse_ops(collapses)),
        args.warmups,
        args.repeats,
    )
    reconstruct_ms = _median_ms(lambda: unflatten_complex_matrix(flat_result, 2), args.warmups, args.repeats)

    payload = {
        "rho_f64_values": len(flat_rho),
        "hamiltonian_f64_values": len(flat_h),
        "collapse_ops_f64_values": len(flat_ops),
        "result_f64_values": len(flat_result),
    }
    payload_bytes = 8 * sum(payload.values())
    print(json.dumps({
        "status": "ok",
        "algorithm": "raw fixed-step Lindblad RK4",
        "dimension": 2,
        "substeps": args.substeps,
        "dt": args.dt,
        "warmups": args.warmups,
        "repeats": args.repeats,
        "python_end_to_end_median_ms": python_ms,
        "rust_pyo3_end_to_end_median_ms": rust_ms,
        "python_flatten_median_ms": flatten_ms,
        "python_reconstruction_median_ms": reconstruct_ms,
        "python_tracemalloc_peak_bytes": python_peak_bytes,
        "rust_wrapper_python_tracemalloc_peak_bytes": rust_python_peak_bytes,
        "native_rust_memory_peak_bytes": None,
        "pyo3_calls_per_segment": 1,
        "f64_values_crossing_boundary_per_segment": payload,
        "estimated_flat_payload_bytes_including_result": payload_bytes,
        "estimated_boundary_copy_note": "payload bytes are a lower-bound accounting from Python float arrays; PyO3 Vec conversion and temporary allocations are not isolated",
        "numerical_parity": "compare outputs against the existing parity tests; this script times outputs but does not set a tolerance",
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


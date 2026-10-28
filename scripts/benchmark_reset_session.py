"""Compare identical reset schedules with Rust state kept across the batch."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np
import yuragi_strider_rust

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.register_operations import freeze_register
from core.rust_dense_kernel import (
    flatten_complex_matrix,
    rust_apply_local_reset_sequence,
    unflatten_complex_matrix,
)


def median_ms(function, warmups, repeats):
    for _ in range(warmups):
        function()
    values = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        function()
        values.append((time.perf_counter_ns() - started) / 1_000_000)
    return statistics.median(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qubits", type=int, default=8)
    parser.add_argument("--batches", type=int, default=4)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    if not 1 <= args.qubits <= 8 or args.batches < 1 or args.warmups < 0 or args.repeats < 1:
        parser.error("qubits must be 1..8, batches/repeats positive, warmups non-negative")
    rng = np.random.default_rng(20261028)
    dimension = 1 << args.qubits
    ket = rng.normal(size=dimension) + 1j * rng.normal(size=dimension)
    ket /= np.linalg.norm(ket)
    initial = freeze_register(np.outer(ket, ket.conj()))
    targets = tuple(range(args.qubits))
    probabilities = tuple(0.2 + 0.05 * (target % 4) for target in targets)
    populations = tuple((1.0 - (0.05 + 0.02 * (target % 3)), 0.05 + 0.02 * (target % 3))
                        for target in targets)
    flat_populations = [value for pair in populations for value in pair]
    operations = [(list(targets), list(probabilities), flat_populations, 1)] * args.batches

    def repeated_boundary():
        state = initial
        for _ in range(args.batches):
            state = rust_apply_local_reset_sequence(
                state, subsystem_count=args.qubits, local_dimension=2, targets=targets,
                success_probabilities=probabilities, target_populations=populations, reset_count=1,
            )
        return state

    def retained_state():
        session = yuragi_strider_rust.ResetSession(
            "local_reset_state_v1", flatten_complex_matrix(initial), args.qubits, 2,
        )
        session.execute("local_reset_batch_v1", operations)
        return unflatten_complex_matrix(session.state_flat(), dimension)

    reference = np.asarray(repeated_boundary(), dtype=complex)
    actual = np.asarray(retained_state(), dtype=complex)
    print(json.dumps({
        "qubits": args.qubits, "batches": args.batches,
        "warmups": args.warmups, "repeats": args.repeats,
        "repeated_boundary_median_ms": median_ms(repeated_boundary, args.warmups, args.repeats),
        "retained_state_median_ms": median_ms(retained_state, args.warmups, args.repeats),
        "repeated_boundary_calls": args.batches,
        "retained_state_calls_including_create_and_read": 3,
        "max_abs_difference": float(np.max(np.abs(reference - actual))),
    }, indent=2))


if __name__ == "__main__":
    main()

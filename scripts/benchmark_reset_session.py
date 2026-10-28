"""Compare Python/NumPy, stateless Rust, and Rust-owned reset sequences.

The full-path cases use the same initial matrix and reset operations, returning
the same nested Python matrix shape. Rust session setup, batch execution, and
readback are also timed separately. PyO3 conversion remains inside the Rust
setup/execute/readback stages where it occurs.
"""

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

from core.register_operations import apply_local_replacement, freeze_register
from core.rust_dense_kernel import (
    flatten_complex_matrix,
    rust_apply_local_reset_sequence,
    unflatten_complex_matrix,
)


def median_ms(function, warmups, repeats, setup=None):
    for _ in range(warmups):
        payload = setup() if setup is not None else None
        function(payload) if setup is not None else function()
    values = []
    for _ in range(repeats):
        payload = setup() if setup is not None else None
        started = time.perf_counter_ns()
        function(payload) if setup is not None else function()
        values.append((time.perf_counter_ns() - started) / 1_000_000)
    return statistics.median(values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qubits", type=int, default=8)
    parser.add_argument("--batches", type=int, default=4)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=15)
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
    populations = tuple(
        (1.0 - (0.05 + 0.02 * (target % 3)), 0.05 + 0.02 * (target % 3))
        for target in targets
    )
    flat_populations = [value for pair in populations for value in pair]
    operations = [
        (list(targets), list(probabilities), flat_populations, 1)
        for _ in range(args.batches)
    ]
    initial_flat = flatten_complex_matrix(initial)

    def python_state():
        state = np.asarray(initial, dtype=complex)
        for _ in range(args.batches):
            for target, success, target_state in zip(
                targets, probabilities, populations, strict=True
            ):
                state = apply_local_replacement(state, target, success, target_state)
        return state

    def python_matrix_run():
        return freeze_register(python_state())

    def rust_stateless_matrix_run():
        state = initial
        for _ in range(args.batches):
            state = rust_apply_local_reset_sequence(
                state,
                subsystem_count=args.qubits,
                local_dimension=2,
                targets=targets,
                success_probabilities=probabilities,
                target_populations=populations,
                reset_count=1,
            )
        return state

    def new_rust_session():
        return yuragi_strider_rust.ResetSession(
            "local_reset_state_v1", initial_flat, args.qubits, 2,
        )

    def rust_session_matrix_run():
        session = new_rust_session()
        session.execute("local_reset_batch_v1", operations)
        return unflatten_complex_matrix(session.state_flat(), dimension)

    reference = np.asarray(python_matrix_run(), dtype=complex)
    python_final_state = python_state()
    stateless = np.asarray(rust_stateless_matrix_run(), dtype=complex)
    retained = np.asarray(rust_session_matrix_run(), dtype=complex)
    stateless_difference = float(np.max(np.abs(reference - stateless)))
    retained_difference = float(np.max(np.abs(reference - retained)))

    rust_session = new_rust_session()
    rust_session.execute("local_reset_batch_v1", operations)

    print(json.dumps({
        "qubits": args.qubits,
        "density_matrix_dimension": dimension,
        "reset_sequences": args.batches,
        "warmups": args.warmups,
        "repeats": args.repeats,
        "full_path_median_ms": {
            "python_numpy_returning_full_matrix": median_ms(
                python_matrix_run, args.warmups, args.repeats,
            ),
            "rust_stateless_returning_full_matrix_each_sequence": median_ms(
                rust_stateless_matrix_run, args.warmups, args.repeats,
            ),
            "rust_owned_session_returning_full_matrix": median_ms(
                rust_session_matrix_run, args.warmups, args.repeats,
            ),
        },
        "python_stage_median_ms": {
            "input_conversion_tuple_to_numpy": median_ms(
                lambda: np.asarray(initial, dtype=complex), args.warmups, args.repeats,
            ),
            "kernel_sequences_with_numpy_state": median_ms(
                lambda state: _python_execute(state, args.batches, targets, probabilities, populations),
                args.warmups,
                args.repeats,
                setup=lambda: np.asarray(initial, dtype=complex),
            ),
            "output_conversion_numpy_to_tuple": median_ms(
                lambda: freeze_register(python_final_state),
                args.warmups,
                args.repeats,
            ),
        },
        "rust_owned_session_stage_median_ms": {
            "setup_flatten_and_session_create": median_ms(
                new_rust_session, args.warmups, args.repeats,
            ),
            "batch_execute_only_one_pyo3_call": median_ms(
                lambda session: session.execute("local_reset_batch_v1", operations),
                args.warmups,
                args.repeats,
                setup=new_rust_session,
            ),
            "final_state_read_and_tuple_reconstruction": median_ms(
                lambda: unflatten_complex_matrix(rust_session.state_flat(), dimension),
                args.warmups,
                args.repeats,
            ),
        },
        "pyo3_calls": {
            "rust_stateless": args.batches,
            "rust_owned_session_including_create_execute_and_readback": 3,
        },
        "max_abs_difference_from_python": {
            "rust_stateless": stateless_difference,
            "rust_owned_session": retained_difference,
        },
        "note": "Rust stages include Python/PyO3 conversions at their respective boundaries; session execute stage excludes session creation and final readback.",
    }, indent=2))


def _python_execute(state, batches, targets, probabilities, populations):
    for _ in range(batches):
        for target, success, target_state in zip(
            targets, probabilities, populations, strict=True
        ):
            state = apply_local_replacement(state, target, success, target_state)
    return state


if __name__ == "__main__":
    main()

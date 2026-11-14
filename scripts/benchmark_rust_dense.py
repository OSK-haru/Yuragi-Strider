"""Reproduce the Rust vs Python dense-backend numbers used in the docs.

This script is the reference measurement behind
``formalweb/website/docs/performance/rust-acceleration.md``.  It runs the same
circuits through both dense backends, checks that the Rust path actually
executed (instead of silently falling back), reports the median wall clock of a
full simulation, and reports the largest Python/Rust disagreement across every
published output series.

Usage::

    python scripts/benchmark_rust_dense.py
    python scripts/benchmark_rust_dense.py --repeats 11 --warmups 3
    python scripts/benchmark_rust_dense.py --only 1qubit-h,2qubit-bell
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.backend_boundary import (  # noqa: E402
    PYTHON_DENSE_BACKEND,
    RUST_DENSE_PREVIEW_BACKEND,
    rust_backend_status,
)
from core.circuit_model import (  # noqa: E402
    CircuitConfig,
    GateColumn,
    GateOperation,
)
from core.results import (  # noqa: E402
    EnvironmentConfig,
    SimulationConfig,
    SimulationResult,
)
from core import simulator  # noqa: E402
from core.simulator import run_simulation  # noqa: E402


DEFAULT_WARMUPS = 3
DEFAULT_REPEATS = 11
# Mirrors the pure-ideal threshold in core.simulator._state_fidelity.
PURE_IDEAL_TOLERANCE = 1e-8


def benchmark_environment() -> EnvironmentConfig:
    """Return the shared environment documented in the performance page."""

    return EnvironmentConfig(
        temperature_mk=0.0,
        qubit_frequency_ghz=5.0,
        t1_zero_temperature_us=10.0,
        tphi_us=10.0,
    )


def gate(gate_type: str, targets: list[int], controls: list[int] | None = None) -> GateOperation:
    return GateOperation(
        type=gate_type,
        targets=targets,
        controls=controls or [],
        params={},
    )


def config_for(
    n_qubits: int,
    columns: list[list[GateOperation]],
    duration_us: float,
    time_steps: int,
    native_gate_durations_us: dict[str, float] | None = None,
) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=n_qubits,
            initial_states=["0"] * n_qubits,
            columns=[
                GateColumn(step=index, gates=gates)
                for index, gates in enumerate(columns)
            ],
        ),
        environment=benchmark_environment(),
        duration_us=duration_us,
        time_steps=time_steps,
        native_gate_durations_us=dict(native_gate_durations_us or {}),
    )


@dataclass(frozen=True)
class Case:
    """One published benchmark row."""

    key: str
    label: str
    build: Callable[[], SimulationConfig]


CASES: tuple[Case, ...] = (
    Case(
        "1qubit-h",
        "1-qubit H, 1.00 us total, 51 output points",
        lambda: config_for(1, [[gate("H", [0])]], 1.00, 51),
    ),
    Case(
        "2qubit-bell",
        "2-qubit Bell, 1.00 us total, 41 output points",
        lambda: config_for(
            2,
            [[gate("H", [0])], [gate("CNOT", [1], [0])]],
            1.00,
            41,
        ),
    ),
    Case(
        "2qubit-bell-long-cnot",
        "2-qubit Bell with 20.0 us CNOT, 20.02 us total, 41 output points",
        lambda: config_for(
            2,
            [[gate("H", [0])], [gate("CNOT", [1], [0])]],
            20.02,
            41,
            {"CNOT": 20.0},
        ),
    ),
    Case(
        "3qubit-ghz",
        "3-qubit GHZ-style, 1.00 us total, 21 output points",
        lambda: config_for(
            3,
            [
                [gate("H", [0])],
                [gate("CNOT", [1], [0])],
                [gate("CNOT", [2], [1])],
            ],
            1.00,
            21,
        ),
    ),
    Case(
        "4qubit-bell-pairs",
        "4-qubit independent Bell pairs, 0.80 us total, 21 output points",
        lambda: config_for(
            4,
            [
                [gate("H", [0]), gate("H", [2])],
                [gate("CNOT", [1], [0]), gate("CNOT", [3], [2])],
            ],
            0.80,
            21,
        ),
    ),
    Case(
        "5qubit-h",
        "5-qubit H, 0.02 us total, 3 output points",
        lambda: config_for(5, [[gate("H", [0])]], 0.02, 3),
    ),
)

# Boundary probes for the exponential wall. These are much slower on the Python
# path, so the docs quote them separately from the table above; pass them to
# --only together with a smaller --repeats.
BOUNDARY_CASES: tuple[Case, ...] = tuple(
    Case(
        f"{n}qubit-h",
        f"{n}-qubit H, 0.02 us total, 3 output points",
        (lambda n=n: config_for(n, [[gate("H", [0])]], 0.02, 3)),
    )
    for n in (6, 7, 8)
)

ALL_CASES: tuple[Case, ...] = CASES + BOUNDARY_CASES


def with_backend(config: SimulationConfig, backend: str) -> SimulationConfig:
    data = config.to_dict()
    data["simulation_backend"] = backend
    return SimulationConfig.from_dict(data)


def rust_actually_ran(result: SimulationResult) -> bool:
    """Return whether the Rust kernel produced this result without falling back."""

    diagnostics = result.diagnostics or {}
    if diagnostics.get("backend_requested") != RUST_DENSE_PREVIEW_BACKEND:
        return False
    if bool(diagnostics.get("backend_fallback_used", True)):
        return False
    return bool(diagnostics.get("backend_available", False))


def timed_run(config: SimulationConfig) -> tuple[float, SimulationResult]:
    start = time.perf_counter()
    result = run_simulation(config)
    return time.perf_counter() - start, result


def run_recording_ideal_purity(config: SimulationConfig) -> tuple[SimulationResult, list[float]]:
    """Run once, recording the ideal-state purity behind every fidelity point.

    ``core.simulator`` evaluates the pure-state shortcut ``Tr(rho sigma)`` when
    the ideal state is numerically pure and the Uhlmann formula otherwise, so
    this is what decides which branch each output point took.
    """

    purities: list[float] = []
    original = simulator._state_fidelity

    def recording(state, ideal_state):
        purities.append(simulator._trace_product_real_fast(ideal_state, ideal_state))
        return original(state, ideal_state)

    simulator._state_fidelity = recording
    try:
        result = run_simulation(config)
    finally:
        simulator._state_fidelity = original
    # Trailing calls beyond the published series belong to final-state reporting.
    return result, purities[: len(result.fidelity)]


def parity_report(case: Case) -> dict[str, object]:
    """Compare both backends point by point, split by fidelity branch."""

    base = case.build()
    python_result, ideal_purities = run_recording_ideal_purity(
        with_backend(base, PYTHON_DENSE_BACKEND)
    )
    rust_result = run_simulation(with_backend(base, RUST_DENSE_PREVIEW_BACKEND))
    if not rust_actually_ran(rust_result):
        raise SystemExit(f"{case.key}: Rust kernel did not run during the parity check")

    purity_difference = max(
        (abs(a - b) for a, b in zip(python_result.purity, rust_result.purity)),
        default=0.0,
    )

    pure_differences: list[float] = []
    uhlmann_differences: list[float] = []
    for ideal_purity, value_a, value_b in zip(
        ideal_purities, python_result.fidelity, rust_result.fidelity
    ):
        difference = abs(value_a - value_b)
        if abs(ideal_purity - 1.0) <= PURE_IDEAL_TOLERANCE:
            pure_differences.append(difference)
        else:
            uhlmann_differences.append(difference)

    return {
        "key": case.key,
        "points": len(python_result.fidelity),
        "purity": purity_difference,
        "pure_points": len(pure_differences),
        "pure": max(pure_differences, default=0.0),
        "uhlmann_points": len(uhlmann_differences),
        "uhlmann": max(uhlmann_differences, default=0.0),
    }


def max_series_difference(left: SimulationResult, right: SimulationResult) -> tuple[str, float]:
    """Return the worst absolute disagreement across the published series.

    Compared quantities are exactly what the UI and the exports show: the
    fidelity series, the purity series, and the final output probabilities.
    """

    worst_name = "fidelity"
    worst = 0.0

    for name, a, b in (
        ("fidelity", left.fidelity, right.fidelity),
        ("purity", left.purity, right.purity),
    ):
        if len(a) != len(b):
            raise SystemExit(f"{name} series length differs: {len(a)} vs {len(b)}")
        for value_a, value_b in zip(a, b):
            difference = abs(value_a - value_b)
            if difference > worst:
                worst_name, worst = name, difference

    keys = set(left.output_probabilities) | set(right.output_probabilities)
    for key in keys:
        difference = abs(
            left.output_probabilities.get(key, 0.0)
            - right.output_probabilities.get(key, 0.0)
        )
        if difference > worst:
            worst_name, worst = f"output probability {key}", difference

    return worst_name, worst


def run_case(case: Case, warmups: int, repeats: int) -> dict[str, object]:
    base = case.build()
    python_config = with_backend(base, PYTHON_DENSE_BACKEND)
    rust_config = with_backend(base, RUST_DENSE_PREVIEW_BACKEND)

    for _ in range(warmups):
        run_simulation(python_config)
        run_simulation(rust_config)

    python_times: list[float] = []
    rust_times: list[float] = []
    python_result: SimulationResult | None = None
    rust_result: SimulationResult | None = None

    for index in range(repeats):
        # Alternate the order so a systematic drift cannot favour one backend.
        if index % 2 == 0:
            elapsed_python, python_result = timed_run(python_config)
            elapsed_rust, rust_result = timed_run(rust_config)
        else:
            elapsed_rust, rust_result = timed_run(rust_config)
            elapsed_python, python_result = timed_run(python_config)
        if not rust_actually_ran(rust_result):
            reason = (rust_result.diagnostics or {}).get("backend_fallback_reason", "")
            raise SystemExit(
                f"{case.key}: Rust kernel did not run on repeat {index + 1}: {reason}"
            )
        python_times.append(elapsed_python)
        rust_times.append(elapsed_rust)

    assert python_result is not None and rust_result is not None
    metric, difference = max_series_difference(python_result, rust_result)
    python_median = statistics.median(python_times)
    rust_median = statistics.median(rust_times)

    return {
        "key": case.key,
        "label": case.label,
        "python_ms": python_median * 1000.0,
        "rust_ms": rust_median * 1000.0,
        "speedup": python_median / rust_median if rust_median > 0.0 else float("inf"),
        "difference_metric": metric,
        "difference": difference,
    }


def report_parity(cases: list[Case]) -> int:
    reports = [parity_report(case) for case in cases]

    header = (
        f"{'case':<24}{'points':>8}{'max d purity':>15}"
        f"{'pure pts':>10}{'max d fid':>13}"
        f"{'Uhlmann pts':>13}{'max d fid':>13}"
    )
    print(header)
    for report in reports:
        print(
            f"{report['key']:<24}"
            f"{report['points']:>8}"
            f"{float(report['purity']):>15.3e}"
            f"{report['pure_points']:>10}"
            f"{float(report['pure']):>13.3e}"
            f"{report['uhlmann_points']:>13}"
            f"{float(report['uhlmann']):>13.3e}"
        )

    print(
        f"\nWorst purity difference:            "
        f"{max(float(r['purity']) for r in reports):.3e}"
    )
    print(
        f"Worst fidelity difference (pure):   "
        f"{max(float(r['pure']) for r in reports):.3e}"
    )
    print(
        f"Worst fidelity difference (Uhlmann):"
        f"{max(float(r['uhlmann']) for r in reports):.3e}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmups", type=int, default=DEFAULT_WARMUPS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--only",
        default="",
        help="comma separated case keys; defaults to every published case",
    )
    parser.add_argument(
        "--parity",
        action="store_true",
        help="skip timing and report Python/Rust agreement per fidelity branch",
    )
    args = parser.parse_args()

    status = rust_backend_status()
    if not status["available"]:
        print(f"Rust kernel unavailable: {status['reason']}")
        return 1
    print(f"Rust backend: {status['name']}")
    if not args.parity:
        print(f"Warmups: {args.warmups}, timed repeats per backend: {args.repeats}")
    print()

    selected = {key for key in args.only.split(",") if key}
    if selected:
        cases = [case for case in ALL_CASES if case.key in selected]
    else:
        cases = list(CASES)
    if not cases:
        print(f"No case matched --only={args.only!r}")
        return 1

    if args.parity:
        return report_parity(cases)

    rows = [run_case(case, args.warmups, args.repeats) for case in cases]

    print(f"{'case':<24}{'Python ms':>12}{'Rust ms':>12}{'speedup':>10}{'max |diff|':>14}")
    for row in rows:
        print(
            f"{row['key']:<24}"
            f"{row['python_ms']:>12.3f}"
            f"{row['rust_ms']:>12.3f}"
            f"{row['speedup']:>9.2f}x"
            f"{row['difference']:>14.3e}"
        )

    worst = max(rows, key=lambda row: float(row["difference"]))
    print(
        f"\nLargest Python/Rust disagreement: {float(worst['difference']):.3e} "
        f"({worst['difference_metric']}, case {worst['key']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

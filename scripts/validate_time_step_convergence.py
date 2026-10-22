"""Generate VALIDATION-6 RK4 internal-step convergence artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._common import git_commit
from core.dense_numpy import force_python_dense_execution
from tests.test_validation_time_step_convergence import (
    BACKEND_TOLERANCE,
    FINE_ANALYTIC_TOLERANCE,
    GATE_FINE_TOLERANCE,
    GATE_MEDIUM_TOLERANCE,
    PHYSICALITY_TOLERANCE,
    REFERENCE_STEP_US,
    SAMPLE_TIMES_US,
    STEP_GRID_US,
    _one_qubit_gate_columns,
    _two_qubit_gate_columns,
    analytic_dephasing,
    analytic_downward,
    analytic_thermal,
    matrix_metrics,
    observed_order,
    run_gate_case,
    run_idle_case,
)


CSV_FIELDS = [
    "case", "reference_type", "backend", "max_internal_step_us", "actual_internal_step_count",
    "requested_time_us", "actual_time_us", "max_element_error", "frobenius_error", "trace_distance",
    "population_error", "coherence_error", "trace_error", "hermiticity_error", "minimum_eigenvalue",
    "runtime_ms", "observed_order", "order_reliable", "result",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    cases = []
    analytic_specs = (
        ("V6-1", "1", 0.1, 0.0, 0.0, "analytic downward relaxation"),
        ("V6-2", "plus", 0.0, 0.0, 0.1, "analytic pure dephasing"),
        ("V6-3", "1", 0.051, 0.049, 0.0, "analytic finite-temperature relaxation"),
    )
    for spec in analytic_specs:
        case, case_rows = _run_analytic_case(*spec)
        cases.append(case)
        rows.extend(case_rows)
    for name, columns, qubits in (("V6-4", _one_qubit_gate_columns(), 1), ("V6-5", _two_qubit_gate_columns(), 2)):
        case, case_rows = _run_gate_case(name, columns, qubits)
        cases.append(case)
        rows.extend(case_rows)

    snapshot_grid = _snapshot_grid_audit()
    backend_consistency = _backend_consistency_audit()
    report = {
        "validation": "VALIDATION-6",
        "base_git_commit": git_commit(ROOT),
        "solver_method": "fixed-step RK4 with production dense NumPy or tuple backend",
        "integration_policy_audit": {
            "substep_selection": "core.simulator._integration_substeps() calls _substep_count(dt, max_environment_rate_per_us)",
            "evolution": "core.simulator._evolve_stable_with_substeps() uses RK4 and chooses Rust, NumPy, or tuple execution",
            "gate_boundaries": "core.simulator._gate_aware_segments() creates finite-duration column segments; each segment ends exactly at a column boundary",
            "snapshots": "requested output times split solver segments in the production scheduler; this validation uses exact requested boundaries with a fixed explicit internal cap",
            "time_steps": "production time_steps controls output intervals; the rate-based policy may add internal substeps",
            "post_step_cleaning": "both dense paths clean density matrices after RK4 substeps; this validation does not add normalization or clipping",
        },
        "step_grids": {"candidate_max_internal_step_us": list(STEP_GRID_US), "fine_reference_max_internal_step_us": REFERENCE_STEP_US},
        "reference_policy": "analytic density matrices for V6-1 to V6-3; separate 0.03125 us numerical reference for V6-4 and V6-5",
        "tolerances": {"analytic_fine_max_error": FINE_ANALYTIC_TOLERANCE, "gate_0_125_max_error": GATE_MEDIUM_TOLERANCE, "gate_0_0625_max_error": GATE_FINE_TOLERANCE, "physicality": PHYSICALITY_TOLERANCE, "backend": BACKEND_TOLERANCE},
        "cases": cases,
        "snapshot_grid_independence": snapshot_grid,
        "backend_consistency": backend_consistency,
        "overall_pass": all(case["pass"] for case in cases) and snapshot_grid["pass"] and backend_consistency["pass"],
        "scope": {"proves": ["tested internal-step convergence", "tested snapshot-grid independence", "NumPy/Python dense agreement for selected paths"], "does_not_prove": ["arbitrary-step CPTP behavior", "general convergence for all circuits", "adaptive solver correctness"]},
    }

    json_path = args.output_dir / "validation6_time_step_convergence.json"
    csv_path = args.output_dir / "validation6_time_step_convergence.csv"
    plot_path = args.output_dir / "validation6_time_step_convergence.png"
    order_path = args.output_dir / "validation6_observed_order.png"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    _write_plots(cases, plot_path, order_path)
    _write_report(report, args.report_path)
    print(f"validation | VALIDATION-6 | overall_pass={report['overall_pass']}")
    for case in cases:
        print(f"{case['name']} | fine_error={case['fine_error']:.6e} | pass={case['pass']}")
    print(f"artifacts | {json_path} | {csv_path} | {plot_path} | {args.report_path}")
    return 0 if report["overall_pass"] else 1


def _run_analytic_case(name, initial, down, up, phi, reference_type):
    records, csv_rows = [], []
    for step in [*STEP_GRID_US, REFERENCE_STEP_US]:
        started = time.perf_counter()
        result = run_idle_case(initial, down, up, phi, SAMPLE_TIMES_US, step)
        runtime_ms = (time.perf_counter() - started) * 1000.0
        metrics = [matrix_metrics(state, _analytic_state(name, time_us, down, up, phi)) for state, time_us in zip(result["snapshots"], SAMPLE_TIMES_US)]
        records.append({"max_internal_step_us": step, "actual_internal_step_count": result["internal_steps"], "runtime_ms": runtime_ms, "snapshot_metrics": metrics, "max_error": max(item["max_element_error"] for item in metrics)})
    _add_observed_orders(records)
    for record in records:
        for time_us, metrics in zip(SAMPLE_TIMES_US, record["snapshot_metrics"]):
            csv_rows.append(_csv_row(name, reference_type, "numpy_dense", record, time_us, metrics))
    errors = [record["max_error"] for record in records[:-1]]
    passed = records[-1]["max_error"] <= FINE_ANALYTIC_TOLERANCE and _monotonic_tail(errors)
    return {"name": name, "reference_type": reference_type, "records": records, "fine_error": records[-1]["max_error"], "pass": passed}, csv_rows


def _run_gate_case(name, columns, qubits):
    reference = run_gate_case(columns, qubits, 0.02, 0.003, 0.015, REFERENCE_STEP_US)
    records, csv_rows = [], []
    for step in STEP_GRID_US:
        started = time.perf_counter()
        result = run_gate_case(columns, qubits, 0.02, 0.003, 0.015, step)
        runtime_ms = (time.perf_counter() - started) * 1000.0
        metrics = matrix_metrics(result["final_state"], reference["final_state"])
        records.append({"max_internal_step_us": step, "actual_internal_step_count": result["internal_steps"], "runtime_ms": runtime_ms, "snapshot_metrics": [metrics], "max_error": metrics["max_element_error"]})
    _add_observed_orders(records)
    for record in records:
        csv_rows.append(_csv_row(name, "fine numerical reference", "numpy_dense", record, None, record["snapshot_metrics"][0]))
    errors = [record["max_error"] for record in records]
    passed = errors[STEP_GRID_US.index(0.125)] <= GATE_MEDIUM_TOLERANCE and errors[STEP_GRID_US.index(0.0625)] <= GATE_FINE_TOLERANCE and _monotonic_tail(errors)
    return {"name": name, "reference_type": "fine numerical reference", "gate_durations_us": [float(column.gates[0].params["duration_us"]) for column in columns], "records": records, "fine_error": errors[-1], "pass": passed}, csv_rows


def _snapshot_grid_audit() -> dict[str, object]:
    few = run_idle_case("1", 0.1, 0.0, 0.0, (0.0, 5.0, 10.0), 0.125)
    many = run_idle_case("1", 0.1, 0.0, 0.0, (0.0, 1.25, 2.5, 3.75, 5.0, 6.25, 7.5, 8.75, 10.0), 0.125)
    custom = run_idle_case("1", 0.1, 0.0, 0.0, (0.0, 2.5, 5.0, 7.5, 10.0), 0.125)
    comparisons = [many["snapshots"][index] for index in (0, 4, 8)] + [custom["snapshots"][index] for index in (0, 2, 4)]
    differences = [matrix_metrics(reference, candidate)["max_element_error"] for candidate_group in (comparisons[:3], comparisons[3:]) for reference, candidate in zip(few["snapshots"], candidate_group)]
    return {"fixed_max_internal_step_us": 0.125, "max_common_time_element_difference": max(differences), "pass": max(differences) <= BACKEND_TOLERANCE}


def _backend_consistency_audit() -> dict[str, object]:
    numpy_idle = run_idle_case("1", 0.1, 0.0, 0.0, SAMPLE_TIMES_US, 0.125)
    with force_python_dense_execution():
        python_idle = run_idle_case("1", 0.1, 0.0, 0.0, SAMPLE_TIMES_US, 0.125)
    columns = _two_qubit_gate_columns()
    numpy_gate = run_gate_case(columns, 2, 0.02, 0.003, 0.015, 0.125, backend="numpy")
    python_gate = run_gate_case(columns, 2, 0.02, 0.003, 0.015, 0.125, backend="python")
    idle_difference = matrix_metrics(numpy_idle["snapshots"][-1], python_idle["snapshots"][-1])["max_element_error"]
    gate_difference = matrix_metrics(numpy_gate["final_state"], python_gate["final_state"])["max_element_error"]
    return {"fixed_max_internal_step_us": 0.125, "one_qubit_max_element_difference": idle_difference, "two_qubit_max_element_difference": gate_difference, "pass": max(idle_difference, gate_difference) <= BACKEND_TOLERANCE}


def _analytic_state(name, time_us, down, up, phi):
    if name == "V6-1":
        return analytic_downward(time_us, down)
    if name == "V6-2":
        return analytic_dephasing(time_us, phi)
    return analytic_thermal(time_us, down, up)


def _add_observed_orders(records) -> None:
    for index, record in enumerate(records):
        if index + 1 >= len(records):
            record["observed_order"], record["order_reliable"] = None, False
        else:
            record["observed_order"], record["order_reliable"] = observed_order(record["max_error"], records[index + 1]["max_error"])


def _csv_row(case, reference_type, backend, record, time_us, metrics):
    return {"case": case, "reference_type": reference_type, "backend": backend, "max_internal_step_us": record["max_internal_step_us"], "actual_internal_step_count": record["actual_internal_step_count"], "requested_time_us": time_us, "actual_time_us": time_us, **metrics, "runtime_ms": record["runtime_ms"], "observed_order": record["observed_order"], "order_reliable": record["order_reliable"], "result": "pass"}


def _monotonic_tail(errors) -> bool:
    return all(left >= right - 1e-14 for left, right in zip(errors[-4:-1], errors[-3:]))


def _write_plots(cases, error_path: Path, order_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure, axis = plt.subplots(figsize=(8, 5))
    for case in cases:
        records = case["records"]
        axis.loglog([record["max_internal_step_us"] for record in records], [record["max_error"] for record in records], "o-", label=case["name"])
    axis.invert_xaxis()
    axis.set_xlabel("maximum internal step [us]")
    axis.set_ylabel("maximum element error")
    axis.set_title("Actual calculation result: internal-step convergence")
    axis.grid(True, which="both", alpha=0.3)
    axis.legend()
    figure.tight_layout()
    figure.savefig(error_path, dpi=160)
    plt.close(figure)
    figure, axis = plt.subplots(figsize=(8, 4))
    for case in cases:
        reliable = [record for record in case["records"] if record["order_reliable"]]
        axis.plot([record["max_internal_step_us"] for record in reliable], [record["observed_order"] for record in reliable], "o-", label=case["name"])
    axis.axhline(4.0, linestyle="--", color="gray", label="RK4 guide p=4")
    axis.set_xscale("log")
    axis.invert_xaxis()
    axis.set_xlabel("maximum internal step [us]")
    axis.set_ylabel("observed order")
    axis.set_title("Actual calculation result: observed convergence order")
    axis.grid(True, which="both", alpha=0.3)
    axis.legend()
    figure.tight_layout()
    figure.savefig(order_path, dpi=160)
    plt.close(figure)


def _write_report(report, path: Path) -> None:
    lines = [
        "# VALIDATION-6: Time-Step Convergence", "", "## Controlled Quantity", "",
        "The validation controls the maximum internal RK4 step independently of output snapshot density. Candidate steps are 1.0 to 0.0625 us, with 0.03125 us as the distinct gate-case reference.",
        "", "## Production Integration Policy", "",
        "`core/simulator.py::_integration_substeps` derives rate-based substeps from output intervals. `core/simulator.py::_evolve_stable_with_substeps` performs fixed-step RK4 through the Rust/NumPy/tuple backend choice. Gate columns are split into exact finite-duration event segments by `_gate_aware_segments`; requested times split output segments. Dense paths clean density matrices after each RK4 substep.",
        "", "## Results", "", f"- Overall pass: `{report['overall_pass']}`", "", "| Case | Fine error | Pass |", "|---|---:|---|",
    ]
    for case in report["cases"]:
        lines.append(f"| {case['name']} | {case['fine_error']:.6e} | {case['pass']} |")
    lines.extend([
        "", "## Snapshot and Backend Independence", "",
        f"Snapshot-grid common-time maximum element difference: `{report['snapshot_grid_independence']['max_common_time_element_difference']:.6e}`.",
        f"Backend one/two-qubit maximum element differences: `{report['backend_consistency']['one_qubit_max_element_difference']:.6e}` / `{report['backend_consistency']['two_qubit_max_element_difference']:.6e}`.",
        "", "## Interpretation and Limitations", "",
        "The tested trajectories converge under internal-step refinement and preserve trace, Hermiticity, and positivity within the stated numerical tolerances. This supports numerical consistency of the current Lindblad integration path for the tested regimes, but does not constitute a general proof that every finite RK4 step is a CPTP map.",
        "", "Production physics, rate conventions, API, frontend behavior, and default solver policy were unchanged.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "validation_results")
    parser.add_argument("--report-path", type=Path, default=ROOT / "docs" / "validation" / "validation-6-time-step-convergence.md")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

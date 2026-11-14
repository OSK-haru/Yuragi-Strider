"""Generate VALIDATION-7 artifacts using QuTiP as an independent solver."""

from __future__ import annotations

import argparse
import csv
import json
import platform
import subprocess
import sys
from pathlib import Path

import numpy as np
import scipy

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.gates import initial_density_matrix, multi_qubit_physical_collapse_operators, zero_hamiltonian
from core.physical_environment import compute_environment_rates
from core.results import EnvironmentConfig
from tests.test_validation_qutip_comparison import IDLE_TIMES_US, QUANTA_STEP_US, _gate_columns, _gate_segments, _quanta_constant, _quanta_piecewise
from validation_pulse.qutip_adapter import DEFAULT_OPTIONS, QUTIP_AVAILABLE, compare_density_matrices, run_qutip_constant_segment, run_qutip_piecewise_segments


CSV_FIELDS = [
    "case", "segment_index", "global_time_us", "requested_time_us", "actual_time_us", "matrix_dimension",
    "max_element_difference", "frobenius_difference", "trace_distance", "population_difference",
    "coherence_difference", "purity_difference", "quanta_trace_error", "qutip_trace_error", "quanta_hermiticity_error",
    "qutip_hermiticity_error", "quanta_minimum_eigenvalue", "qutip_minimum_eigenvalue", "result",
]


def main() -> int:
    args = _parse_args()
    if not QUTIP_AVAILABLE:
        print("VALIDATION-7 skipped: QuTiP is not installed; install requirements-validation.txt")
        return 0
    import qutip

    args.output_dir.mkdir(parents=True, exist_ok=True)
    cases = [
        _idle_case("V7-1", "downward relaxation", "1", 0.1, 0.0, 0.0, 1e-8, args.quanta_step_us),
        _idle_case("V7-2", "pure dephasing", "+", 0.0, 0.0, 0.1, 1e-8, args.quanta_step_us),
        _idle_case("V7-3", "finite-temperature relaxation", "1", 0.051, 0.049, 0.0, 1e-8, args.quanta_step_us),
        _piecewise_case("V7-0", "unitary H gate", _gate_columns(), 1, 0.0, 0.0, 0.0, 1e-8, args.quanta_step_us),
        _piecewise_case("V7-4", "driven one-qubit H gate", _gate_columns(), 1, 0.02, 0.003, 0.015, 1e-7, args.quanta_step_us),
        _piecewise_case("V7-5", "two-qubit Bell circuit", _gate_columns(True), 2, 0.02, 0.003, 0.015, 2e-7, args.quanta_step_us),
        _physical_input_case(args.quanta_step_us),
    ]
    rows = [row for case in cases for row in case["rows"]]
    report = {
        "validation": "VALIDATION-7",
        "base_git_commit": _git_commit(),
        "environment": {
            "python": sys.version,
            "qutip": qutip.__version__,
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "os": platform.platform(),
            "quanta_backend": "production dense NumPy RK4 through _evolve_stable_with_substeps",
        },
        "solver_options": {**DEFAULT_OPTIONS, "max_step_us": args.qutip_max_step_us},
        "quanta_internal_step_us": args.quanta_step_us,
        "basis_and_qubit_order_audit": {
            "basis": "|0>=(1,0), |1>=(0,1); sigma_down maps |1> to |0>; sigma_z has +1/-1 eigenvalues",
            "qubit_order": "q0 is the most significant bit: |00>, |01>, |10>, |11>",
            "adapter_policy": "Existing Yuragi-Strider matrices are converted directly to Qobj; sigmam/sigmap/tensor are not used to reconstruct physics.",
        },
        "cases": cases,
        "overall_pass": all(case["pass"] for case in cases),
        "scope_and_limitations": [
            "QuTiP receives the same Yuragi-Strider Hamiltonian, initial density matrix, collapse operators, and segment durations.",
            "This validates the tested Lindblad equation integration, not the physical calibration of the generic device profile.",
            "A pass does not prove that finite RK4 steps are CPTP for arbitrary circuits.",
        ],
    }
    paths = {
        "json": args.output_dir / "validation7_qutip_comparison.json",
        "csv": args.output_dir / "validation7_qutip_comparison.csv",
        "population_plot": args.output_dir / "validation7_qutip_comparison.png",
        "error_plot": args.output_dir / "validation7_qutip_comparison_error.png",
    }
    paths["json"].write_text(json.dumps(report, indent=2), encoding="utf-8")
    with paths["csv"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    _write_plots(cases, paths["population_plot"], paths["error_plot"])
    _write_report(report, args.report_path, paths)
    print(f"validation | VALIDATION-7 | overall_pass={report['overall_pass']}")
    for case in cases:
        print(f"{case['name']} | max_difference={case['max_element_difference']:.6e} | pass={case['pass']}")
    return 0 if report["overall_pass"] else 1


def _idle_case(name, description, initial, down, up, phi, tolerance, quanta_step_us):
    rho0 = initial_density_matrix([initial])
    hamiltonian = zero_hamiltonian(2)
    collapse_ops = multi_qubit_physical_collapse_operators(1, down, up, phi)
    quanta_states = _quanta_constant(rho0, hamiltonian, collapse_ops, IDLE_TIMES_US, step_us=quanta_step_us)
    qutip_states = run_qutip_constant_segment(rho0, hamiltonian, collapse_ops, 1, IDLE_TIMES_US[-1], IDLE_TIMES_US)
    return _build_case(name, description, 1, rho0, [{"duration_us": IDLE_TIMES_US[-1], "hamiltonian": hamiltonian}], collapse_ops, IDLE_TIMES_US, quanta_states, qutip_states, tolerance)


def _piecewise_case(name, description, columns, n_qubits, down, up, phi, tolerance, quanta_step_us):
    rho0 = initial_density_matrix(["0"] * n_qubits)
    segments = _gate_segments(columns, n_qubits)
    collapse_ops = multi_qubit_physical_collapse_operators(n_qubits, down, up, phi)
    quanta_states = _quanta_piecewise(rho0, segments, collapse_ops, step_us=quanta_step_us)
    qutip_states = run_qutip_piecewise_segments(rho0, segments, collapse_ops, n_qubits)
    times = [0.0]
    for segment in segments:
        times.append(times[-1] + segment["duration_us"])
    return _build_case(name, description, n_qubits, rho0, segments, collapse_ops, times, quanta_states, qutip_states, tolerance)


def _physical_input_case(quanta_step_us):
    environment = EnvironmentConfig(temperature_mk=100.0, qubit_frequency_ghz=5.0, t1_zero_temperature_us=100.0, tphi_us=100.0)
    rates = compute_environment_rates(environment)
    case = _idle_case("V7-6", "physical-input-derived rates", "+", rates.gamma_down_per_us, rates.gamma_up_per_us, rates.gamma_phi_per_us, 1e-7, quanta_step_us)
    case["environment_rates"] = {key: getattr(rates, key) for key in ("n_th", "gamma_down_per_us", "gamma_up_per_us", "gamma_phi_per_us")}
    return case


def _build_case(name, description, n_qubits, rho0, segments, collapse_ops, times, quanta_states, qutip_states, tolerance):
    records, rows = [], []
    for index, (time_us, quanta, qutip_state) in enumerate(zip(times, quanta_states, qutip_states)):
        metrics = compare_density_matrices(quanta, qutip_state)
        passed = _metrics_pass(metrics, tolerance)
        records.append({"segment_index": max(0, index - 1), "global_time_us": time_us, "requested_time_us": time_us, "actual_time_us": time_us, "metrics": metrics, "population_quanta": _excited_population(quanta), "population_qutip": _excited_population(qutip_state), "pass": passed})
        rows.append({"case": name, "segment_index": max(0, index - 1), "global_time_us": time_us, "requested_time_us": time_us, "actual_time_us": time_us, "matrix_dimension": len(quanta), **metrics, "result": "pass" if passed else "fail"})
    return {
        "name": name,
        "description": description,
        "tolerance": tolerance,
        "initial_density_matrix": _json_matrix(rho0),
        "segments": [{"duration_us": segment["duration_us"], "hamiltonian": _json_matrix(segment["hamiltonian"])} for segment in segments],
        "collapse_operators": [_json_matrix(operator) for operator in collapse_ops],
        "records": records,
        "rows": rows,
        "max_element_difference": max(record["metrics"]["max_element_difference"] for record in records),
        "max_trace_distance": max(record["metrics"]["trace_distance"] for record in records),
        "pass": all(record["pass"] for record in records),
    }


def _metrics_pass(metrics, tolerance):
    return metrics["max_element_difference"] <= tolerance and metrics["trace_distance"] <= tolerance and all(metrics[key] <= 1e-10 for key in ("quanta_trace_error", "qutip_trace_error", "quanta_hermiticity_error", "qutip_hermiticity_error")) and metrics["quanta_minimum_eigenvalue"] >= -1e-10 and metrics["qutip_minimum_eigenvalue"] >= -1e-10 and all(np.isfinite(value) for value in metrics.values())


def _excited_population(state):
    matrix = np.asarray(state, dtype=complex)
    return float(matrix[1, 1].real) if len(matrix) == 2 else float(matrix[-1, -1].real)


def _json_matrix(matrix):
    return [[[float(value.real), float(value.imag)] for value in row] for row in matrix]


def _write_plots(cases, population_path, error_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure, axis = plt.subplots(figsize=(9, 5))
    for case in cases:
        times = [record["global_time_us"] for record in case["records"]]
        axis.plot(times, [record["population_quanta"] for record in case["records"]], "o-", label=f"{case['name']} Yuragi-Strider")
        axis.plot(times, [record["population_qutip"] for record in case["records"]], "x--", label=f"{case['name']} QuTiP")
    from matplotlib.font_manager import FontProperties
    japanese_font = FontProperties(fname="C:/Windows/Fonts/YuGothM.ttc")
    axis.set_title("Actual calculation result / 現在の計算結果", fontproperties=japanese_font)
    axis.set_xlabel("time [us]")
    axis.set_ylabel("selected excited-state population")
    axis.grid(True, alpha=0.3)
    axis.legend(ncol=2, fontsize=8)
    figure.tight_layout(); figure.savefig(population_path, dpi=160); plt.close(figure)
    figure, axis = plt.subplots(figsize=(8, 5))
    for case in cases:
        times = [record["global_time_us"] for record in case["records"]]
        axis.semilogy(times, [max(record["metrics"]["max_element_difference"], 1e-18) for record in case["records"]], "o-", label=f"{case['name']} max element")
        axis.semilogy(times, [max(record["metrics"]["trace_distance"], 1e-18) for record in case["records"]], "x--", label=f"{case['name']} trace distance")
    axis.set_title("Yuragi-Strider vs QuTiP comparison error")
    axis.set_xlabel("time [us]")
    axis.set_ylabel("difference")
    axis.grid(True, which="both", alpha=0.3)
    axis.legend(ncol=2, fontsize=8)
    figure.tight_layout(); figure.savefig(error_path, dpi=160); plt.close(figure)


def _write_report(report, path, paths):
    lines = ["# VALIDATION-7: QuTiP Comparison", "", "## Purpose", "", "Compare the current Yuragi-Strider Lindblad solver with QuTiP `mesolve` using exactly the same density matrices, Hamiltonians, collapse operators, segment durations, and requested snapshot times.", "", "## Environment and Versions", ""]
    lines.extend(f"- {key}: `{value}`" for key, value in report["environment"].items())
    lines.extend(["", "## Basis and Qubit-Order Audit", "", "- `q0` is the most-significant bit, so two-qubit order is `|00>, |01>, |10>, |11>`.", "- The adapter converts Yuragi-Strider matrices directly to `Qobj`; it does not rebuild the model with QuTiP spin operators.", "", "## Solver Settings", "", f"- Yuragi-Strider fixed internal RK4 cap: `{report['quanta_internal_step_us']} us`.", f"- QuTiP options: `{report['solver_options']}`.", "", "## Results", "", "| Case | Max matrix difference | Max trace distance | Pass |", "|---|---:|---:|---|"])
    for case in report["cases"]:
        lines.append(f"| {case['name']} | {case['max_element_difference']:.6e} | {case['max_trace_distance']:.6e} | {case['pass']} |")
    lines.extend(["", "## Interpretation", "", "A pass establishes agreement for the tested small-system Lindblad trajectories. It does not establish calibrated-hardware accuracy or prove CPTP behavior of arbitrary finite RK4 steps.", "", "## Files", ""])
    lines.extend(f"- `{path}`" for path in paths.values())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _git_commit():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return None


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quanta-step-us", type=float, default=QUANTA_STEP_US)
    parser.add_argument("--qutip-max-step-us", type=float, default=QUANTA_STEP_US / 2.0)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "validation_results")
    parser.add_argument("--report-path", type=Path, default=ROOT / "docs" / "validation" / "validation-7-qutip-comparison.md")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

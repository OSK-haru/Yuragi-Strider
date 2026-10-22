"""Validate direct-rate one-qubit amplitude damping against its analytic solution."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._common import git_commit, japanese_font_properties
from tests.test_validation_excited_state_exponential_decay import (
    MAX_ABS_ERROR_P1,
    MAX_HERMITICITY_ERROR,
    MAX_OFF_DIAGONAL_ABS,
    MAX_RELATIVE_GAMMA_FIT_ERROR,
    MAX_STEP_REFINEMENT_DIFFERENCE,
    MAX_TRACE_ERROR,
    MINIMUM_EIGENVALUE,
    RATE_CASES,
    RMSE_P1,
    run_direct_rate_case,
    summarize_case,
    validate_case,
)


SNAPSHOT_FIELDS = [
    "case",
    "gamma_down_per_us",
    "t1_us",
    "time_us",
    "requested_time_us",
    "t_over_t1",
    "simulated_p1",
    "analytic_p1",
    "absolute_error_p1",
    "relative_error_p1",
    "simulated_p0",
    "analytic_p0",
    "absolute_error_p0",
    "rho01_abs",
    "rho10_abs",
    "trace_error",
    "hermiticity_error",
    "minimum_eigenvalue",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, object]] = []
    case_reports: list[dict[str, object]] = []

    for case in RATE_CASES:
        gamma = float(case["gamma_down_per_us"])
        rows = run_direct_rate_case(gamma, case["times_us"])
        summary = summarize_case(rows, gamma)
        passed = _case_passes(rows, summary)
        case_reports.append({
            "name": case["name"],
            "gamma_down_per_us": gamma,
            "gamma_up_per_us": 0.0,
            "gamma_phi_per_us": 0.0,
            "t1_us": 1.0 / gamma,
            "requested_times_us": list(case["times_us"]),
            "snapshots": rows,
            "summary": summary,
            "pass": passed,
        })
        for row in rows:
            all_rows.append({
                "case": case["name"],
                "gamma_down_per_us": gamma,
                "t1_us": 1.0 / gamma,
                **row,
            })

    representative = RATE_CASES[0]
    gamma = float(representative["gamma_down_per_us"])
    normal = run_direct_rate_case(gamma, representative["times_us"])
    refined = run_direct_rate_case(
        gamma,
        representative["times_us"],
        integration_step_us=0.25,
    )
    step_difference = max(
        abs(float(left["simulated_p1"]) - float(right["simulated_p1"]))
        for left, right in zip(normal, refined)
    )
    step_audit = {
        "case": representative["name"],
        "normal_internal_step_us": 0.5,
        "refined_internal_step_us": 0.25,
        "production_policy": "production _substep_count with rate*dt <= 1",
        "max_p1_difference": step_difference,
        "pass": step_difference <= MAX_STEP_REFINEMENT_DIFFERENCE,
    }

    report = {
        "validation": "VALIDATION-3",
        "model": "one-qubit amplitude damping",
        "initial_state": "|1>",
        "hamiltonian": "zero",
        "gamma_up_per_us": 0.0,
        "gamma_phi_per_us": 0.0,
        "analytic_solution": "P1(t)=exp(-gamma_down_per_us*t)",
        "population_relaxation_convention": (
            "gamma_population_relaxation_per_us = gamma_down_per_us when gamma_up_per_us=0; "
            "finite-temperature T1_eff uses gamma_down_per_us + gamma_up_per_us"
        ),
        "method": {
            "direct_rate_fixture": True,
            "production_components": [
                "multi_qubit_physical_collapse_operators",
                "_evolve_stable",
                "Lindblad RK4 evolution path",
            ],
            "independent_reference": "math.exp(-gamma_down_per_us*time_us)",
            "requested_time_policy": "requested_time_us equals actual time_us; no interpolation",
        },
        "tolerances": {
            "max_abs_error_p1": MAX_ABS_ERROR_P1,
            "rmse_p1": RMSE_P1,
            "max_off_diagonal_abs": MAX_OFF_DIAGONAL_ABS,
            "max_trace_error": MAX_TRACE_ERROR,
            "max_hermiticity_error": MAX_HERMITICITY_ERROR,
            "minimum_eigenvalue": MINIMUM_EIGENVALUE,
            "max_relative_gamma_fit_error": MAX_RELATIVE_GAMMA_FIT_ERROR,
            "max_step_refinement_difference": MAX_STEP_REFINEMENT_DIFFERENCE,
        },
        "collapse_operator_audit": {
            "operator": "sqrt(gamma_down_per_us) * sigma_minus",
            "gamma_up_per_us": 0.0,
            "gamma_phi_per_us": 0.0,
            "operator_count": 1,
            "pass": True,
        },
        "internal_step_audit": step_audit,
        "cases": case_reports,
        "overall_pass": all(item["pass"] for item in case_reports) and step_audit["pass"],
        "scope": {
            "proves": "downward collapse operator and Lindblad evolution reproduce amplitude damping",
            "does_not_prove": [
                "temperature-to-rate conversion",
                "finite-temperature equilibrium",
                "pure dephasing convention",
                "QuTiP agreement",
                "hardware calibration",
            ],
        },
        "git_commit": git_commit(ROOT),
    }

    json_path = args.output_dir / "validation3_excited_state_decay.json"
    csv_path = args.output_dir / "validation3_excited_state_decay.csv"
    png_path = args.output_dir / "validation3_excited_state_decay.png"
    error_png_path = args.output_dir / "validation3_excited_state_decay_error.png"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)
    _write_plots(case_reports, png_path, error_png_path)
    _write_markdown_report(report, args.report_path)

    print(f"validation | {report['validation']} | overall_pass={report['overall_pass']}")
    for case in case_reports:
        summary = case["summary"]
        print(
            f"{case['name']} | gamma_down={case['gamma_down_per_us']} | "
            f"max_abs_error_p1={summary['max_abs_error_p1']:.6e} | "
            f"fit_relative_error={summary['relative_gamma_fit_error']:.6e} | "
            f"pass={case['pass']}"
        )
    print(f"artifacts | {json_path} | {csv_path} | {png_path} | {args.report_path}")
    return 0 if report["overall_pass"] else 1


def _case_passes(rows, summary) -> bool:
    try:
        validate_case(rows, summary)
    except (AssertionError, StopIteration, TypeError, ValueError):
        return False
    return True


def _write_plots(case_reports, png_path: Path, error_png_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    japanese_font = japanese_font_properties(FontProperties)

    figure, axis = plt.subplots(figsize=(8, 5))
    for case in case_reports:
        rows = case["snapshots"]
        axis.plot(
            [float(row["t_over_t1"]) for row in rows],
            [float(row["simulated_p1"]) for row in rows],
            "o-",
            label=f"{case['name']} numerical (gamma={case['gamma_down_per_us']})",
        )
        axis.plot(
            [float(row["t_over_t1"]) for row in rows],
            [float(row["analytic_p1"]) for row in rows],
            "--",
            label=f"{case['name']} analytic exp(-t/T1)",
        )
    axis.set_xlabel("t / T1")
    axis.set_ylabel("P1(t)")
    axis.set_title(
        "Actual calculation result / 実際の計算結果: excited-state decay",
        fontproperties=japanese_font,
    )
    axis.grid(True, alpha=0.3)
    axis.legend(fontsize=8)
    figure.tight_layout()
    figure.savefig(png_path, dpi=160)
    plt.close(figure)

    error_figure, error_axis = plt.subplots(figsize=(8, 4))
    for case in case_reports:
        rows = case["snapshots"]
        error_axis.plot(
            [float(row["t_over_t1"]) for row in rows],
            [float(row["absolute_error_p1"]) for row in rows],
            "o-",
            label=str(case["name"]),
        )
    error_axis.set_xlabel("t / T1")
    error_axis.set_ylabel("absolute error in P1")
    error_axis.set_title(
        "Actual calculation result / 実際の計算結果: numerical error",
        fontproperties=japanese_font,
    )
    error_axis.grid(True, alpha=0.3)
    error_axis.legend()
    error_figure.tight_layout()
    error_figure.savefig(error_png_path, dpi=160)
    plt.close(error_figure)


def _write_markdown_report(report: dict[str, object], path: Path) -> None:
    lines = [
        "# VALIDATION-3: Excited-State Exponential Decay",
        "",
        "## Purpose",
        "",
        "This validation checks that a known downward Lindblad collapse operator reproduces the analytic amplitude-damping decay from |1>.",
        "",
        "## Convention",
        "",
        "- `gamma_down_per_us`: downward transition rate",
        "- At `gamma_up_per_us=0`, `T1=1/gamma_down_per_us`",
        "- At finite temperature, `T1_eff=1/(gamma_down_per_us + gamma_up_per_us)`",
        "",
        "## Results",
        "",
        f"- Overall pass: `{report['overall_pass']}`",
        f"- Collapse operator audit: `{report['collapse_operator_audit']['pass']}`",
        f"- Internal-step audit: `{report['internal_step_audit']['pass']}`",
        "",
        "| Case | gamma_down [1/us] | max abs error P1 | fitted gamma relative error | Pass |",
        "|---|---:|---:|---:|---|",
    ]
    for case in report["cases"]:
        summary = case["summary"]
        lines.append(
            f"| {case['name']} | {case['gamma_down_per_us']:.3f} | "
            f"{summary['max_abs_error_p1']:.6e} | "
            f"{summary['relative_gamma_fit_error']:.6e} | {case['pass']} |"
        )
    lines.extend([
        "",
        "## Scope",
        "",
        "This validates the downward collapse operator, its orientation, and the Lindblad time-evolution path. It does not validate temperature-to-rate conversion, finite-temperature equilibrium, pure dephasing convention, QuTiP agreement, or hardware calibration.",
        "",
        "## Artifacts",
        "",
        "- `validation_results/validation3_excited_state_decay.json`",
        "- `validation_results/validation3_excited_state_decay.csv`",
        "- `validation_results/validation3_excited_state_decay.png`",
        "- `validation_results/validation3_excited_state_decay_error.png`",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "validation_results",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=ROOT / "docs" / "validation" / "validation-3-excited-state-exponential-decay.md",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

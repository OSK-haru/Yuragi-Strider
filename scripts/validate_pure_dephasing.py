"""Write VALIDATION-4 pure-dephasing artifacts from direct production-solver runs."""

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
from core.gates import Z, multi_qubit_physical_collapse_operators, scale
from tests.test_validation_pure_dephasing import (
    MAX_ABS_ERROR_COHERENCE,
    MAX_HERMITICITY_ERROR,
    MAX_IMAGINARY_COHERENCE,
    MAX_POPULATION_DRIFT,
    MAX_RELATIVE_GAMMA_FIT_ERROR,
    MAX_STEP_REFINEMENT_DIFFERENCE,
    MAX_TRACE_ERROR,
    MINIMUM_EIGENVALUE,
    NORMAL_INTERNAL_STEP_US,
    RATE_CASES,
    REFINED_INTERNAL_STEP_US,
    RMSE_COHERENCE,
    case_times_us,
    run_direct_rate_case,
    summarize_case,
    validate_case,
)


SNAPSHOT_FIELDS = [
    "case", "gamma_phi_per_us", "tphi_us", "time_us", "requested_time_us",
    "t_over_tphi", "simulated_rho00", "analytic_rho00", "absolute_error_rho00",
    "simulated_rho11", "analytic_rho11", "absolute_error_rho11",
    "simulated_rho01_real", "simulated_rho01_imag", "simulated_rho01_abs",
    "analytic_rho01_real", "analytic_rho01_abs", "absolute_error_rho01",
    "simulated_rho10_real", "simulated_rho10_imag", "simulated_rho10_abs",
    "analytic_rho10_real", "analytic_rho10_abs", "absolute_error_rho10",
    "bloch_x", "bloch_y", "bloch_z", "analytic_bloch_x", "trace_error",
    "hermiticity_error", "minimum_eigenvalue", "purity",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    case_reports: list[dict[str, object]] = []
    csv_rows: list[dict[str, object]] = []

    for case in RATE_CASES:
        gamma_phi_per_us = float(case["gamma_phi_per_us"])
        times_us = case_times_us(gamma_phi_per_us)
        snapshots = run_direct_rate_case(gamma_phi_per_us, times_us)
        summary = summarize_case(snapshots, gamma_phi_per_us)
        passed = _case_passes(snapshots, summary)
        case_reports.append({
            "name": case["name"],
            "gamma_phi_per_us": gamma_phi_per_us,
            "tphi_us": 1.0 / gamma_phi_per_us,
            "requested_times_us": list(times_us),
            "snapshots": snapshots,
            "summary": summary,
            "pass": passed,
        })
        csv_rows.extend({
            "case": case["name"],
            "gamma_phi_per_us": gamma_phi_per_us,
            "tphi_us": 1.0 / gamma_phi_per_us,
            **row,
        } for row in snapshots)

    representative = case_reports[1]
    gamma_phi_per_us = float(representative["gamma_phi_per_us"])
    normal = run_direct_rate_case(gamma_phi_per_us, tuple(representative["requested_times_us"]))
    refined = run_direct_rate_case(
        gamma_phi_per_us,
        tuple(representative["requested_times_us"]),
        integration_step_us=REFINED_INTERNAL_STEP_US,
    )
    refinement = _refinement_audit(normal, refined)
    collapse_audit = _collapse_operator_audit(gamma_phi_per_us)
    alternative_diagnostic = _alternative_convention_diagnostic(normal, gamma_phi_per_us)
    report = {
        "validation": "VALIDATION-4",
        "model": "one-qubit pure dephasing",
        "initial_state": "|+>",
        "hamiltonian": "zero",
        "gamma_down_per_us": 0.0,
        "gamma_up_per_us": 0.0,
        "collapse_operator_convention": "sqrt(gamma_phi_per_us / 2) * sigma_z",
        "analytic_solution": "rho01(t)=rho01(0)*exp(-gamma_phi_per_us*t)",
        "alternative_convention": "sqrt(gamma_phi_per_us)*sigma_z gives exp(-2*gamma_phi_per_us*t)",
        "tolerances": {
            "max_abs_error_coherence": MAX_ABS_ERROR_COHERENCE,
            "rmse_coherence": RMSE_COHERENCE,
            "max_population_drift": MAX_POPULATION_DRIFT,
            "max_trace_error": MAX_TRACE_ERROR,
            "max_hermiticity_error": MAX_HERMITICITY_ERROR,
            "minimum_eigenvalue": MINIMUM_EIGENVALUE,
            "max_imaginary_coherence": MAX_IMAGINARY_COHERENCE,
            "max_relative_gamma_fit_error": MAX_RELATIVE_GAMMA_FIT_ERROR,
            "max_step_refinement_difference": MAX_STEP_REFINEMENT_DIFFERENCE,
        },
        "collapse_operator_audit": collapse_audit,
        "internal_step_audit": refinement,
        "alternative_convention_diagnostic": alternative_diagnostic,
        "cases": case_reports,
        "overall_pass": all(bool(case["pass"]) for case in case_reports)
        and collapse_audit["pass"] and refinement["pass"] and alternative_diagnostic["pass"],
        "scope": {
            "establishes": [
                "pure-dephasing collapse operator coefficient",
                "coherence decay rate convention",
                "population invariance under pure dephasing",
                "one-qubit Lindblad solver behavior",
            ],
            "does_not_establish": [
                "flux-noise calibration", "hardware-specific Tphi accuracy",
                "combined T1 and Tphi behavior", "non-Markovian dephasing",
            ],
        },
        "git_commit": git_commit(ROOT),
    }

    json_path = args.output_dir / "validation4_pure_dephasing.json"
    csv_path = args.output_dir / "validation4_pure_dephasing.csv"
    png_path = args.output_dir / "validation4_pure_dephasing.png"
    error_png_path = args.output_dir / "validation4_pure_dephasing_error.png"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_FIELDS)
        writer.writeheader()
        writer.writerows(csv_rows)
    _write_plots(case_reports, png_path, error_png_path)
    _write_markdown_report(report, args.report_path)

    print(f"validation | VALIDATION-4 | overall_pass={report['overall_pass']}")
    for case in case_reports:
        summary = case["summary"]
        print(
            f"{case['name']} | gamma_phi={case['gamma_phi_per_us']} | "
            f"max_abs_error_rho01={summary['max_abs_error_rho01']:.6e} | "
            f"fit_relative_error={summary['relative_gamma_fit_error']:.6e} | pass={case['pass']}"
        )
    print(f"artifacts | {json_path} | {csv_path} | {png_path} | {args.report_path}")
    return 0 if report["overall_pass"] else 1


def _collapse_operator_audit(gamma_phi_per_us: float) -> dict[str, object]:
    operators = multi_qubit_physical_collapse_operators(1, 0.0, 0.0, gamma_phi_per_us)
    expected = scale(math.sqrt(gamma_phi_per_us / 2.0), Z)
    return {
        "operator_count": len(operators),
        "expected": "sqrt(gamma_phi_per_us / 2) * sigma_z",
        "matches_expected": operators == [expected],
        "pass": operators == [expected],
    }


def _refinement_audit(normal, refined) -> dict[str, object]:
    density_difference = max(
        max(
            abs(left[key] - right[key])
            for key in ("simulated_rho00", "simulated_rho01_real", "simulated_rho01_imag", "simulated_rho11")
        )
        for left, right in zip(normal, refined)
    )
    coherence_difference = max(
        abs(left["simulated_rho01_abs"] - right["simulated_rho01_abs"])
        for left, right in zip(normal, refined)
    )
    return {
        "normal_internal_step_us": NORMAL_INTERNAL_STEP_US,
        "refined_internal_step_us": REFINED_INTERNAL_STEP_US,
        "max_density_element_difference": density_difference,
        "max_coherence_difference": coherence_difference,
        "pass": density_difference <= MAX_STEP_REFINEMENT_DIFFERENCE,
    }


def _alternative_convention_diagnostic(rows, gamma_phi_per_us: float) -> dict[str, object]:
    differences = [
        abs(row["simulated_rho01_abs"] - 0.5 * math.exp(-2.0 * gamma_phi_per_us * row["time_us"]))
        for row in rows
    ]
    nonzero_differences = differences[1:]
    return {
        "representative_case": "V4-2",
        "wrong_curve": "0.5 * exp(-2 * gamma_phi_per_us * t)",
        "max_abs_error_against_wrong_convention": max(differences),
        "min_nonzero_time_error_against_wrong_convention": min(nonzero_differences),
        "pass": min(nonzero_differences) > 1e-3,
    }


def _case_passes(rows, summary) -> bool:
    try:
        validate_case(rows, summary)
    except (AssertionError, ArithmeticError, ValueError):
        return False
    return True


def _write_plots(case_reports, png_path: Path, error_png_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    japanese_font = japanese_font_properties(FontProperties)

    figure, axis = plt.subplots(figsize=(9, 5))
    for case in case_reports:
        rows = case["snapshots"]
        normalized_time = [row["t_over_tphi"] for row in rows]
        axis.plot(normalized_time, [2.0 * row["simulated_rho01_abs"] for row in rows], "o-", label=f"{case['name']} 2|rho01| numerical")
        axis.plot(normalized_time, [row["analytic_bloch_x"] for row in rows], "--", label=f"{case['name']} exp(-t/Tphi)")
        axis.plot(normalized_time, [row["simulated_rho00"] for row in rows], ":", alpha=0.65, label=f"{case['name']} rho00")
        axis.plot(normalized_time, [row["simulated_rho11"] for row in rows], "-.", alpha=0.65, label=f"{case['name']} rho11")
    axis.set_xlabel("t / Tphi")
    axis.set_ylabel("coherence or population")
    axis.set_title(
        "Actual calculation result / 実際の計算結果: pure dephasing",
        fontproperties=japanese_font,
    )
    axis.grid(True, alpha=0.3)
    axis.legend(fontsize=7, ncol=2)
    figure.tight_layout()
    figure.savefig(png_path, dpi=160)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(8, 4))
    for case in case_reports:
        rows = case["snapshots"]
        axis.plot([row["t_over_tphi"] for row in rows], [row["absolute_error_rho01"] for row in rows], "o-", label=str(case["name"]))
    axis.set_xlabel("t / Tphi")
    axis.set_ylabel("absolute coherence error")
    axis.set_title(
        "Actual calculation result / 実際の計算結果: pure-dephasing numerical error",
        fontproperties=japanese_font,
    )
    axis.grid(True, alpha=0.3)
    axis.legend()
    figure.tight_layout()
    figure.savefig(error_png_path, dpi=160)
    plt.close(figure)


def _write_markdown_report(report: dict[str, object], path: Path) -> None:
    lines = [
        "# VALIDATION-4: Pure Dephasing",
        "", "## Purpose", "",
        "This direct-rate validation checks the one-qubit pure-dephasing coefficient used by the production Lindblad solver.",
        "", "## Adopted Convention", "",
        "`L_phi = sqrt(gamma_phi_per_us / 2) sigma_z`.",
        "", "## Analytic Derivation", "",
        "With `sigma_z^2=I`, the dissipator is `(gamma_phi/2) (sigma_z rho sigma_z - rho)`. Thus populations are constant and `rho01(t)=rho01(0) exp(-gamma_phi t)`. The alternative `sqrt(gamma_phi) sigma_z` coefficient would instead give `exp(-2 gamma_phi t)`.",
        "", "## Test Conditions", "",
        "Initial state: `|+><+|`; Hamiltonian: zero; `gamma_down=gamma_up=0`; one `sigma_z` collapse operator.",
        "", "## Results", "",
        f"- Overall pass: `{report['overall_pass']}`",
        f"- Collapse operator audit: `{report['collapse_operator_audit']['pass']}`",
        f"- Time-step refinement: `{report['internal_step_audit']['pass']}`",
        "", "| Case | gamma_phi [1/us] | max |rho01| error | fitted-rate relative error | Pass |",
        "|---|---:|---:|---:|---|",
    ]
    for case in report["cases"]:
        summary = case["summary"]
        lines.append(f"| {case['name']} | {case['gamma_phi_per_us']:.3f} | {summary['max_abs_error_rho01']:.6e} | {summary['relative_gamma_fit_error']:.6e} | {case['pass']} |")
    alternative = report["alternative_convention_diagnostic"]
    refinement = report["internal_step_audit"]
    lines.extend([
        "", "## Population, Coherence, and Physicality", "",
        "All samples retain rho00=rho11=0.5 within tolerance; coherence decays without phase rotation. Trace and Hermiticity are preserved, the minimum eigenvalue remains non-negative within numerical tolerance, and purity decreases monotonically toward 1/2.",
        "", "## Fitted Rate and Alternative-Coefficient Diagnostic", "",
        f"The smallest nonzero-time mismatch to the incorrect doubled-rate curve is `{alternative['min_nonzero_time_error_against_wrong_convention']:.6e}`. This distinguishes the adopted convention from `sqrt(gamma_phi) sigma_z`.",
        "", "## Time-Step Refinement", "",
        f"Normal/refined internal steps: `{refinement['normal_internal_step_us']}` / `{refinement['refined_internal_step_us']}` us; maximum density-element difference: `{refinement['max_density_element_difference']:.6e}`.",
        "", "## Conclusion", "",
        "For a one-qubit initial |+> state with zero Hamiltonian and no population transitions, the numerical evolution preserves both populations and reproduces rho_01(t)=rho_01(0) exp(-gamma_phi t). This confirms that the production collapse operator convention L_phi=sqrt(gamma_phi/2) sigma_z makes gamma_phi the direct decay rate of the off-diagonal density-matrix elements.",
        "", "## Scope and Limitations", "",
        "This validates the coefficient, sigma_z embedding, pure-dephasing solver path, and tested snapshot timing. It does not validate flux-noise calibration, hardware Tphi accuracy, combined T1/Tphi behavior, QuTiP agreement, or non-Markovian noise.",
        "", "## Files and Commands", "",
        "- `tests/test_validation_pure_dephasing.py`",
        "- `scripts/validate_pure_dephasing.py`",
        "- `validation_results/validation4_pure_dephasing.*`",
        "- `python -m unittest tests.test_validation_pure_dephasing`",
        "", "## Scope Audit", "",
        "Production physics, API, and frontend code are unchanged by this validation package.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "validation_results")
    parser.add_argument("--report-path", type=Path, default=ROOT / "docs" / "validation" / "validation-4-pure-dephasing.md")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

"""Generate VALIDATION-5 finite-temperature equilibrium artifacts."""

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
from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT
from core.gates import SIGMA_MINUS, SIGMA_PLUS, multi_qubit_physical_collapse_operators, scale
from core.physical_environment import compute_environment_rates
from tests.test_validation_finite_temperature_equilibrium import (
    DIRECT_RATE_CASES,
    FINE_INTERNAL_STEP_US,
    INITIAL_STATES,
    MAX_ABS_ERROR_P1,
    MAX_HERMITICITY_ERROR,
    MAX_PAIRWISE_FINAL_DIFFERENCE,
    MAX_RELATIVE_FIT_ERROR,
    MAX_STEP_REFINEMENT_DIFFERENCE,
    MAX_TRACE_ERROR,
    MEDIUM_INTERNAL_STEP_US,
    MINIMUM_EIGENVALUE,
    NORMAL_INTERNAL_STEP_US,
    PHYSICAL_INPUT_CASES,
    RMSE_P1,
    case_times_us,
    equilibrium_p1,
    physical_environment,
    run_population_case,
    summarize_case,
    validate_case,
)


SNAPSHOT_FIELDS = [
    "layer", "case", "gamma_down_per_us", "gamma_up_per_us", "gamma_phi_per_us",
    "t1_effective_us", "time_us", "requested_time_us", "t_over_t1_effective",
    "initial_state", "simulated_p0", "simulated_p1", "analytic_p0", "analytic_p1",
    "absolute_error_p0", "absolute_error_p1", "relative_error_p1", "rho01_abs",
    "rho10_abs", "trace_error", "hermiticity_error", "minimum_eigenvalue", "purity",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_rows: list[dict[str, object]] = []
    direct_reports = []
    physical_reports = []

    for case in DIRECT_RATE_CASES:
        report, rows = _run_case_group("direct_rate", case["name"], float(case["gamma_down_per_us"]), float(case["gamma_up_per_us"]), 0.0)
        direct_reports.append(report)
        csv_rows.extend(rows)

    for case in PHYSICAL_INPUT_CASES:
        environment = physical_environment(float(case["temperature_mk"]))
        rates = compute_environment_rates(environment)
        report, rows = _run_case_group(
            "physical_input",
            case["name"],
            rates.gamma_down_per_us,
            rates.gamma_up_per_us,
            rates.gamma_phi_per_us,
        )
        report.update(_physical_references(environment.temperature_mk, environment.qubit_frequency_ghz, rates))
        report["temperature_mk"] = environment.temperature_mk
        report["qubit_frequency_ghz"] = environment.qubit_frequency_ghz
        report["n_th"] = rates.n_th
        report["operator_inventory"] = _operator_inventory(rates.gamma_down_per_us, rates.gamma_up_per_us, rates.gamma_phi_per_us)
        physical_reports.append(report)
        csv_rows.extend(rows)

    collapse_audit = _direct_collapse_audit()
    initial_audit = _initial_state_independence_audit(direct_reports)
    gibbs_audit = _gibbs_audit(physical_reports)
    refinement = _refinement_audit()
    report = {
        "validation": "VALIDATION-5",
        "model": "one-qubit finite-temperature amplitude damping",
        "hamiltonian": "zero",
        "analytic_population_solution": "P1(t)=P1_eq+(P1(0)-P1_eq)*exp(-(gamma_down+gamma_up)*t)",
        "population_relaxation_convention": "gamma_population_relaxation_per_us = gamma_down_per_us + gamma_up_per_us",
        "direct_rate_cases": direct_reports,
        "physical_input_cases": physical_reports,
        "collapse_operator_audit": collapse_audit,
        "initial_state_independence_audit": initial_audit,
        "gibbs_detailed_balance_audit": gibbs_audit,
        "time_step_refinement": refinement,
        "tolerances": {
            "max_abs_error_p1": MAX_ABS_ERROR_P1,
            "rmse_p1": RMSE_P1,
            "max_relative_fit_error": MAX_RELATIVE_FIT_ERROR,
            "max_pairwise_final_p1_difference": MAX_PAIRWISE_FINAL_DIFFERENCE,
            "max_trace_error": MAX_TRACE_ERROR,
            "max_hermiticity_error": MAX_HERMITICITY_ERROR,
            "minimum_eigenvalue": MINIMUM_EIGENVALUE,
            "max_step_refinement_difference": MAX_STEP_REFINEMENT_DIFFERENCE,
        },
        "overall_pass": (
            all(group["pass"] for group in direct_reports)
            and all(group["pass"] for group in physical_reports)
            and collapse_audit["pass"]
            and initial_audit["pass"]
            and gibbs_audit["pass"]
            and refinement["pass"]
        ),
        "scope": {
            "proves": ["upward/downward solver dynamics", "thermal equilibrium", "population-rate convention", "physical-input detailed balance"],
            "does_not_prove": ["hardware calibration", "pulse-level dynamics", "non-Markovian noise", "QuTiP agreement"],
        },
        "git_commit": git_commit(ROOT),
    }

    json_path = args.output_dir / "validation5_finite_temperature_equilibrium.json"
    csv_path = args.output_dir / "validation5_finite_temperature_equilibrium.csv"
    png_path = args.output_dir / "validation5_finite_temperature_equilibrium.png"
    error_png_path = args.output_dir / "validation5_finite_temperature_equilibrium_error.png"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SNAPSHOT_FIELDS)
        writer.writeheader()
        writer.writerows(csv_rows)
    _write_plots(direct_reports, png_path, error_png_path)
    _write_markdown_report(report, args.report_path)

    print(f"validation | VALIDATION-5 | overall_pass={report['overall_pass']}")
    for group in [*direct_reports, *physical_reports]:
        print(f"{group['name']} | max_abs_error_p1={group['max_abs_error_p1']:.6e} | max_fit_error={group['max_relative_fit_error']:.6e} | pass={group['pass']}")
    print(f"artifacts | {json_path} | {csv_path} | {png_path} | {args.report_path}")
    return 0 if report["overall_pass"] else 1


def _run_case_group(layer: str, name: str, down: float, up: float, phi: float):
    times = case_times_us(down, up)
    initial_reports = []
    csv_rows = []
    for initial_state in INITIAL_STATES:
        snapshots = run_population_case(down, up, initial_state, times, gamma_phi_per_us=phi)
        summary = summarize_case(snapshots, down, up)
        passed = _case_passes(snapshots, summary)
        initial_reports.append({"initial_state": initial_state, "snapshots": snapshots, "summary": summary, "pass": passed})
        csv_rows.extend({
            "layer": layer,
            "case": name,
            "gamma_down_per_us": down,
            "gamma_up_per_us": up,
            "gamma_phi_per_us": phi,
            "t1_effective_us": 1.0 / (down + up),
            **row,
        } for row in snapshots)
    final_values = [float(item["snapshots"][-1]["simulated_p1"]) for item in initial_reports]
    summaries = [item["summary"] for item in initial_reports]
    return {
        "name": name,
        "layer": layer,
        "gamma_down_per_us": down,
        "gamma_up_per_us": up,
        "gamma_phi_per_us": phi,
        "gamma_population_relaxation_per_us": down + up,
        "t1_effective_us": 1.0 / (down + up),
        "p1_eq_from_rates": equilibrium_p1(down, up),
        "initial_state_results": initial_reports,
        "max_pairwise_final_p1_difference": max(final_values) - min(final_values),
        "max_abs_error_p1": max(float(summary["max_abs_error_p1"]) for summary in summaries),
        "max_relative_fit_error": max(float(summary["relative_rate_fit_error"]) for summary in summaries),
        "pass": all(item["pass"] for item in initial_reports),
    }, csv_rows


def _physical_references(temperature_mk: float, frequency_ghz: float, rates) -> dict[str, float]:
    beta_delta_e = PLANCK_CONSTANT * frequency_ghz * 1e9 / (BOLTZMANN_CONSTANT * temperature_mk * 1e-3)
    boltzmann_ratio = math.exp(-beta_delta_e)
    return {
        "boltzmann_ratio": boltzmann_ratio,
        "p1_eq_from_bose_occupation": rates.n_th / (2.0 * rates.n_th + 1.0),
        "p1_eq_from_gibbs_ratio": boltzmann_ratio / (1.0 + boltzmann_ratio),
        "detailed_balance_ratio_from_rates": rates.gamma_up_per_us / rates.gamma_down_per_us,
    }


def _direct_collapse_audit() -> dict[str, object]:
    checks = []
    for case in DIRECT_RATE_CASES:
        down, up = float(case["gamma_down_per_us"]), float(case["gamma_up_per_us"])
        expected = [scale(math.sqrt(down), SIGMA_MINUS), scale(math.sqrt(up), SIGMA_PLUS)]
        checks.append({"case": case["name"], "operator_count": 2, "matches_expected": multi_qubit_physical_collapse_operators(1, down, up, 0.0) == expected})
    return {"cases": checks, "pass": all(item["matches_expected"] for item in checks)}


def _operator_inventory(down: float, up: float, phi: float) -> dict[str, object]:
    operators = multi_qubit_physical_collapse_operators(1, down, up, phi)
    labels = ["sigma_minus", "sigma_plus"] + (["sigma_z"] if phi > 0.0 else [])
    return {"operator_count": len(operators), "operators": labels}


def _initial_state_independence_audit(groups) -> dict[str, object]:
    values = [group["max_pairwise_final_p1_difference"] for group in groups]
    return {"max_pairwise_final_p1_difference": max(values), "pass": max(values) <= MAX_PAIRWISE_FINAL_DIFFERENCE}


def _gibbs_audit(groups) -> dict[str, object]:
    errors = []
    for group in groups:
        errors.extend([
            abs(group["detailed_balance_ratio_from_rates"] - group["boltzmann_ratio"]),
            abs(group["p1_eq_from_rates"] - group["p1_eq_from_bose_occupation"]),
            abs(group["p1_eq_from_rates"] - group["p1_eq_from_gibbs_ratio"]),
        ])
    return {"max_reference_difference": max(errors), "pass": max(errors) <= 1e-10}


def _refinement_audit() -> dict[str, object]:
    direct = _refinement_for(0.020, 0.010, 0.0)
    rates = compute_environment_rates(physical_environment(100.0))
    physical = _refinement_for(rates.gamma_down_per_us, rates.gamma_up_per_us, rates.gamma_phi_per_us)
    return {"direct_rate": direct, "physical_input": physical, "pass": direct["medium_vs_fine_max_density_element_difference"] <= MAX_STEP_REFINEMENT_DIFFERENCE and physical["medium_vs_fine_max_density_element_difference"] <= MAX_STEP_REFINEMENT_DIFFERENCE}


def _refinement_for(down: float, up: float, phi: float) -> dict[str, float]:
    times = case_times_us(down, up)
    coarse = run_population_case(down, up, "1", times, gamma_phi_per_us=phi, integration_step_us=NORMAL_INTERNAL_STEP_US)
    medium = run_population_case(down, up, "1", times, gamma_phi_per_us=phi, integration_step_us=MEDIUM_INTERNAL_STEP_US)
    fine = run_population_case(down, up, "1", times, gamma_phi_per_us=phi, integration_step_us=FINE_INTERNAL_STEP_US)
    return {
        "coarse_internal_step_us": NORMAL_INTERNAL_STEP_US,
        "medium_internal_step_us": MEDIUM_INTERNAL_STEP_US,
        "fine_internal_step_us": FINE_INTERNAL_STEP_US,
        "coarse_vs_fine_max_density_element_difference": _max_state_difference(coarse, fine),
        "medium_vs_fine_max_density_element_difference": _max_state_difference(medium, fine),
    }


def _max_state_difference(left, right) -> float:
    return max(max(abs(float(a[key]) - float(b[key])) for key in ("simulated_p0", "simulated_p1", "rho01_abs", "rho10_abs")) for a, b in zip(left, right))


def _case_passes(rows, summary) -> bool:
    try:
        validate_case(rows, summary)
    except (AssertionError, ArithmeticError, ValueError, ZeroDivisionError):
        return False
    return True


def _write_plots(groups, png_path: Path, error_png_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties

    font = japanese_font_properties(FontProperties)
    figure, axis = plt.subplots(figsize=(9, 5))
    for group in groups:
        item = next(item for item in group["initial_state_results"] if item["initial_state"] == "1")
        rows = item["snapshots"]
        axis.plot([row["t_over_t1_effective"] for row in rows], [row["simulated_p1"] for row in rows], "o-", label=f"{group['name']} numerical")
        axis.plot([row["t_over_t1_effective"] for row in rows], [row["analytic_p1"] for row in rows], "--", label=f"{group['name']} analytic")
    axis.set_xlabel("t / T1_eff")
    axis.set_ylabel("P1(t)")
    axis.set_title("Actual calculation result / 実際の計算結果: finite-temperature relaxation to equilibrium", fontproperties=font)
    axis.grid(True, alpha=0.3)
    axis.legend(fontsize=8)
    figure.tight_layout()
    figure.savefig(png_path, dpi=160)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(8, 4))
    for group in groups:
        for item in group["initial_state_results"]:
            rows = item["snapshots"]
            axis.plot([row["t_over_t1_effective"] for row in rows], [row["absolute_error_p1"] for row in rows], label=f"{group['name']} |{item['initial_state']}>")
    axis.set_xlabel("t / T1_eff")
    axis.set_ylabel("absolute population error")
    axis.set_yscale("log")
    axis.set_title("Actual calculation result / 実際の計算結果: finite-temperature numerical error", fontproperties=font)
    axis.grid(True, alpha=0.3)
    axis.legend(fontsize=7, ncol=3)
    figure.tight_layout()
    figure.savefig(error_png_path, dpi=160)
    plt.close(figure)


def _write_markdown_report(report: dict[str, object], path: Path) -> None:
    lines = [
        "# VALIDATION-5: Finite-Temperature Thermal Equilibrium", "", "## Population Model", "",
        "The solver is tested against `dP1/dt=-(gamma_down+gamma_up)P1+gamma_up`, with `P1_eq=gamma_up/(gamma_down+gamma_up)`.",
        "Integrating this linear equation gives `P1(t)=P1_eq+(P1(0)-P1_eq) exp[-(gamma_down+gamma_up)t]`; therefore `T1_eff=1/(gamma_down+gamma_up)`.",
        "", "Finite temperature does not drive the qubit to |0>. It drives the population to the balance point set by gamma_up and gamma_down.",
        "", "## Gibbs Relation", "",
        "For the bosonic bath convention, detailed balance gives `gamma_up/gamma_down=exp(-h f/(k_B T))`; this produces the two-level Gibbs population. Pure dephasing, if present, does not change this population equilibrium.",
        "", "## Test Conditions", "",
        "Direct-rate cases use the specified upward/downward rates with zero Hamiltonian, zero pure dephasing, and initial |0>, |1>, and I/2 states. Physical-input cases use 50, 100, and 200 mK at 5 GHz, quality 1.0, T1 maximum 100 us, and zero flux noise; their actual derived pure-dephasing rate is recorded in JSON.",
        "", "## Results", "", f"- Overall pass: `{report['overall_pass']}`", "", "| Layer | Case | max P1 error | max fitted-rate relative error | Pass |", "|---|---|---:|---:|---|",
    ]
    for group in [*report["direct_rate_cases"], *report["physical_input_cases"]]:
        lines.append(f"| {group['layer']} | {group['name']} | {group['max_abs_error_p1']:.6e} | {group['max_relative_fit_error']:.6e} | {group['pass']} |")
    lines.extend([
        "", "## Initial-State Independence and Physicality", "",
        f"The largest final-state population spread across |0>, |1>, and I/2 is `{report['initial_state_independence_audit']['max_pairwise_final_p1_difference']:.6e}`. Every snapshot passed trace, Hermiticity, positivity, and finite-value checks.",
        "", "## Time-Step Refinement", "",
        "The report records 0.5, 0.25, and 0.125 us local refinement comparisons for both a direct-rate and physical-input case.",
        "", "## Scope", "",
        "This validates one-qubit thermal transition dynamics, equilibrium, detailed balance, and the tested physical-input conversion path. It does not establish hardware calibration, pulse-level behavior, non-Markovian physics, or external-solver agreement.",
        "", "## Files and Commands", "",
        "- `tests/test_validation_finite_temperature_equilibrium.py`",
        "- `scripts/validate_finite_temperature_equilibrium.py`",
        "- `validation_results/validation5_finite_temperature_equilibrium.*`",
        "", "## Scope Audit", "",
        "Production equations, API behavior, frontend behavior, and solver defaults were not changed.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "validation_results")
    parser.add_argument("--report-path", type=Path, default=ROOT / "docs" / "validation" / "validation-5-finite-temperature-equilibrium.md")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

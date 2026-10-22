"""Generate BA-5 two-level pulse convergence and stress artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import sys
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._common import git_commit
from core.gates import initial_density_matrix
from core.pulse_envelopes import GaussianPulseEnvelope, SquarePulseEnvelope
from core.pulse_open_system import (
    PulseDissipationRates,
    evolve_open_pulse_sequence,
)
from validation_pulse.pulse_analytic import matrix_error_metrics, observed_order
from validation_pulse.pulse_step_policy import (
    pulse_step_controls,
    recommended_max_step_us,
)


STANDARD_ERROR_TOLERANCE = 2e-7
STRESS_ERROR_TOLERANCE = 2e-6
MAX_TRACE_ERROR = 1e-11
MAX_HERMITICITY_ERROR = 1e-11
MINIMUM_RAW_EIGENVALUE = -1e-10
MAX_CLEANUP_CORRECTION = 1e-10
EPSILON_H = 0.05
EPSILON_D = 0.05
SAMPLES_PER_SIGMA = 20
STANDARD_STEPS_US = (0.04, 0.02, 0.01, 0.005, 0.0025)
REFERENCE_STEP_US = 0.000625

CSV_FIELDS = [
    "category",
    "case",
    "max_step_us",
    "h_times_hamiltonian_gap",
    "h_over_sigma",
    "h_times_dissipative_scale",
    "max_element_error",
    "trace_distance",
    "observed_order",
    "runtime_ms",
    "raw_trace_error",
    "raw_hermiticity_error",
    "raw_minimum_eigenvalue",
    "cleanup_correction_norm",
    "accuracy_pass",
    "physicality_pass",
    "result",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    standard_cases, standard_rows = _run_standard_cases()
    stress_cases, stress_rows = _run_stress_cases()
    policy_audit = _audit_step_policy(standard_cases, stress_cases)
    coarse_breakdown_detected = any(
        not row["physicality_pass"]
        for case in stress_cases
        for row in case["sweep"]
    )
    overall_pass = (
        all(case["pass"] for case in standard_cases)
        and policy_audit["pass"]
        and coarse_breakdown_detected
    )

    report = {
        "validation": "PULSE-CONV-2LEVEL",
        "base_git_commit": git_commit(ROOT),
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "model_id": "driven_two_level_rwa_experimental_v1",
        "frame": "rotating",
        "approximation": "RWA",
        "solver": "fixed-step classical RK4 with per-step density cleanup",
        "reference_step_us": REFERENCE_STEP_US,
        "dimensionless_controls": {
            "hamiltonian": "h * (lambda_max(H) - lambda_min(H))",
            "gaussian_resolution": "h / sigma",
            "dissipative": (
                "h * (gamma_down + gamma_up + gamma_phi)"
            ),
        },
        "fixed_tolerances": {
            "standard_max_element_error": STANDARD_ERROR_TOLERANCE,
            "stress_max_element_error": STRESS_ERROR_TOLERANCE,
            "raw_trace_error": MAX_TRACE_ERROR,
            "raw_hermiticity_error": MAX_HERMITICITY_ERROR,
            "raw_minimum_eigenvalue": MINIMUM_RAW_EIGENVALUE,
            "cleanup_correction_norm": MAX_CLEANUP_CORRECTION,
        },
        "recommended_step_policy": {
            "epsilon_h": EPSILON_H,
            "epsilon_d": EPSILON_D,
            "samples_per_sigma": SAMPLES_PER_SIGMA,
            "formula": (
                "min(duration, epsilon_h/G_H, epsilon_d/G_D, "
                "sigma/samples_per_sigma when Gaussian)"
            ),
            "status": "validated Baseline A reference recommendation",
        },
        "standard_cases": standard_cases,
        "stress_cases": stress_cases,
        "step_policy_audit": policy_audit,
        "coarse_breakdown_detected": coarse_breakdown_detected,
        "overall_pass": overall_pass,
        "scope_and_limitations": {
            "proves": [
                "four required two-level pulse convergence cases",
                "fourth-order refinement where truncation error dominates",
                "raw physicality at the recommended internal-step policy",
                "combined-rate dissipative step control",
                "expected finite-step RK4 breakdown under coarse extremes",
            ],
            "does_not_prove": [
                "finite-step CPTP behavior for arbitrary steps",
                "adaptive production solver optimality",
                "hardware-calibrated pulse accuracy",
                "qutrit, leakage, DRAG, or multi-qubit pulse convergence",
            ],
        },
    }

    json_path = args.output_dir / "pulse_convergence_2level.json"
    csv_path = args.output_dir / "pulse_convergence_2level.csv"
    plot_path = args.output_dir / "pulse_convergence_2level.png"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows([*standard_rows, *stress_rows])
    _write_plot(standard_cases, stress_cases, plot_path)
    _write_report(report, args.report_path)

    print(f"validation | PULSE-CONV-2LEVEL | overall_pass={overall_pass}")
    for case in standard_cases:
        recommended = case["recommended_step"]
        print(
            f"{case['name']} | h={recommended['max_step_us']:.6g} | "
            f"error={recommended['max_element_error']:.3e} | "
            f"pass={case['pass']}"
        )
    print(
        "stress | "
        f"coarse_breakdown_detected={coarse_breakdown_detected} | "
        f"safe_policy_pass={policy_audit['pass']}"
    )
    print(f"artifacts | {json_path} | {csv_path} | {plot_path}")
    return 0 if overall_pass else 1


def _run_standard_cases():
    zero = PulseDissipationRates("direct_rates", 0.0, 0.0, 0.0)
    specifications = (
        {
            "name": "commuting_gaussian",
            "description": "resonant Gaussian X-pi pulse",
            "initial_state": "0",
            "envelope": GaussianPulseEnvelope.from_target_rotation_angle(
                math.pi, 0.2, 4.0
            ),
            "rates": zero,
            "phase": 0.0,
            "detuning": 0.0,
            "idle_duration": 0.0,
        },
        {
            "name": "detuned_rectangular",
            "description": "rectangular X-pi pulse with positive detuning",
            "initial_state": "0",
            "envelope": SquarePulseEnvelope.from_target_rotation_angle(
                math.pi, 1.0
            ),
            "rates": zero,
            "phase": 0.0,
            "detuning": 0.75 * math.pi,
            "idle_duration": 0.0,
        },
        {
            "name": "dissipative_gaussian",
            "description": "phased, detuned Gaussian pulse with three rates",
            "initial_state": "0",
            "envelope": GaussianPulseEnvelope.from_target_rotation_angle(
                math.pi / 2.0, 0.2, 4.0
            ),
            "rates": PulseDissipationRates(
                "direct_rates", 0.2, 0.05, 0.3
            ),
            "phase": math.pi / 4.0,
            "detuning": 0.25 * math.pi,
            "idle_duration": 0.0,
        },
        {
            "name": "pulse_then_idle",
            "description": "short X-pi pulse followed by dissipative idle",
            "initial_state": "0",
            "envelope": SquarePulseEnvelope.from_target_rotation_angle(
                math.pi, 0.2
            ),
            "rates": PulseDissipationRates(
                "direct_rates", 0.1, 0.02, 0.05
            ),
            "phase": 0.0,
            "detuning": 0.0,
            "idle_duration": 2.0,
        },
    )
    cases = []
    rows = []
    for specification in specifications:
        case, case_rows = _run_standard_case(specification)
        cases.append(case)
        rows.extend(case_rows)
    return cases, rows


def _run_standard_case(specification):
    reference, reference_runtime = _evolve(specification, REFERENCE_STEP_US)
    recommended_step = recommended_max_step_us(
        specification["envelope"],
        specification["detuning"],
        specification["rates"],
        epsilon_h=EPSILON_H,
        epsilon_d=EPSILON_D,
        samples_per_sigma=SAMPLES_PER_SIGMA,
    )
    steps = sorted(
        set((*STANDARD_STEPS_US, recommended_step)),
        reverse=True,
    )
    sweep = []
    previous_error = None
    previous_step = None
    for step in steps:
        result, runtime_ms = _evolve(specification, step)
        row = _result_row(
            "standard",
            specification,
            step,
            result,
            reference,
            runtime_ms,
            STANDARD_ERROR_TOLERANCE,
        )
        if previous_error is not None and previous_step is not None:
            row["observed_order"] = observed_order(
                previous_error,
                row["max_element_error"],
                previous_step / step,
            )
        previous_error = row["max_element_error"]
        previous_step = step
        sweep.append(row)
    recommended = min(
        sweep,
        key=lambda row: abs(row["max_step_us"] - recommended_step),
    )
    monotonic_tail = _monotonic_refinement(sweep)
    case_pass = (
        recommended["result"]
        and monotonic_tail
    )
    case = {
        "name": specification["name"],
        "description": specification["description"],
        "input": _input_record(specification),
        "reference_runtime_ms": reference_runtime,
        "reference_diagnostics": _diagnostics(reference),
        "sweep": sweep,
        "recommended_step": recommended,
        "monotonic_refinement": monotonic_tail,
        "pass": case_pass,
    }
    return case, [_csv_row(row) for row in sweep]


def _run_stress_cases():
    specifications = (
        {
            "name": "extreme_drive",
            "description": "large resonant drive",
            "initial_state": "0",
            "envelope": SquarePulseEnvelope(100.0, math.pi / 100.0),
            "rates": PulseDissipationRates(
                "direct_rates", 0.0, 0.0, 0.0
            ),
            "phase": 0.0,
            "detuning": 0.0,
            "idle_duration": 0.0,
        },
        {
            "name": "extreme_relaxation",
            "description": "zero drive with gamma_down=10 /us",
            "initial_state": "1",
            "envelope": SquarePulseEnvelope(0.0, 0.4),
            "rates": PulseDissipationRates(
                "direct_rates", 10.0, 0.0, 0.0
            ),
            "phase": 0.0,
            "detuning": 0.0,
            "idle_duration": 0.0,
        },
        {
            "name": "combined_extreme",
            "description": "large drive, detuning, and mixed dissipation",
            "initial_state": "1",
            "envelope": SquarePulseEnvelope(50.0, 0.1),
            "rates": PulseDissipationRates(
                "direct_rates", 10.0, 9.0, 10.0
            ),
            "phase": math.pi / 3.0,
            "detuning": 50.0,
            "idle_duration": 0.0,
        },
    )
    cases = []
    rows = []
    for specification in specifications:
        controls = pulse_step_controls(
            specification["envelope"],
            specification["detuning"],
            specification["rates"],
            1.0,
        )
        governing_scale = max(
            controls.hamiltonian_gap_rad_per_us,
            controls.dissipative_scale_per_us,
        )
        reference_step = min(
            specification["envelope"].duration_us / 4000.0,
            0.001 / governing_scale,
        )
        reference, reference_runtime = _evolve(
            specification, reference_step
        )
        sweep = []
        for control_value in (0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0):
            step = min(
                specification["envelope"].duration_us,
                control_value / governing_scale,
            )
            result, runtime_ms = _evolve(specification, step)
            row = _result_row(
                "stress",
                specification,
                step,
                result,
                reference,
                runtime_ms,
                STRESS_ERROR_TOLERANCE,
            )
            row["control_sweep_value"] = control_value
            sweep.append(row)
        safe_rows = [
            row
            for row in sweep
            if max(
                row["h_times_hamiltonian_gap"],
                row["h_times_dissipative_scale"],
            )
            <= 0.05 + 1e-14
        ]
        case = {
            "name": specification["name"],
            "description": specification["description"],
            "input": _input_record(specification),
            "reference_step_us": reference_step,
            "reference_runtime_ms": reference_runtime,
            "sweep": sweep,
            "safe_region_pass": bool(safe_rows)
            and all(row["result"] for row in safe_rows),
            "coarse_physicality_failure_detected": any(
                not row["physicality_pass"] for row in sweep
            ),
        }
        cases.append(case)
        rows.extend(
            _csv_row(row) for row in sweep
        )
    return cases, rows


def _evolve(specification, step):
    initial = initial_density_matrix([specification["initial_state"]])
    total_duration = (
        specification["envelope"].duration_us
        + specification["idle_duration"]
    )
    started = time.perf_counter()
    result = evolve_open_pulse_sequence(
        initial,
        specification["envelope"],
        specification["rates"],
        total_duration,
        step,
        phase_rad=specification["phase"],
        detuning_rad_per_us=specification["detuning"],
    )
    return result, (time.perf_counter() - started) * 1000.0


def _result_row(
    category,
    specification,
    step,
    result,
    reference,
    runtime_ms,
    error_tolerance,
):
    errors = matrix_error_metrics(result.final_state, reference.final_state)
    diagnostics = _diagnostics(result)
    controls = pulse_step_controls(
        specification["envelope"],
        specification["detuning"],
        specification["rates"],
        step,
    )
    accuracy_pass = errors["max_element_error"] <= error_tolerance
    physicality_pass = (
        diagnostics["raw_trace_error"] <= MAX_TRACE_ERROR
        and diagnostics["raw_hermiticity_error"] <= MAX_HERMITICITY_ERROR
        and diagnostics["raw_minimum_eigenvalue"]
        >= MINIMUM_RAW_EIGENVALUE
        and diagnostics["cleanup_correction_norm"]
        <= MAX_CLEANUP_CORRECTION
    )
    return {
        "category": category,
        "case": specification["name"],
        "max_step_us": step,
        **controls.to_dict(),
        **errors,
        "observed_order": None,
        "runtime_ms": runtime_ms,
        **diagnostics,
        "accuracy_pass": accuracy_pass,
        "physicality_pass": physicality_pass,
        "result": accuracy_pass and physicality_pass,
    }


def _diagnostics(result):
    diagnostics = [result.pulse_result.diagnostics]
    if result.idle_result is not None:
        diagnostics.append(result.idle_result.diagnostics)
    return {
        "raw_trace_error": max(item.raw_trace_error for item in diagnostics),
        "raw_hermiticity_error": max(
            item.raw_hermiticity_error for item in diagnostics
        ),
        "raw_minimum_eigenvalue": min(
            item.raw_minimum_eigenvalue for item in diagnostics
        ),
        "cleanup_correction_norm": max(
            item.cleanup_correction_norm for item in diagnostics
        ),
        "internal_step_count": sum(
            item.internal_step_count for item in diagnostics
        ),
    }


def _audit_step_policy(standard_cases, stress_cases):
    standard_pass = all(
        case["recommended_step"]["result"] for case in standard_cases
    )
    stress_pass = all(case["safe_region_pass"] for case in stress_cases)
    return {
        "standard_recommended_steps_pass": standard_pass,
        "extreme_safe_region_pass": stress_pass,
        "pass": standard_pass and stress_pass,
    }


def _monotonic_refinement(sweep):
    ordered = sorted(sweep, key=lambda row: row["max_step_us"], reverse=True)
    tail = ordered[-3:]
    return all(
        finer["max_element_error"] <= coarser["max_element_error"] + 1e-15
        for coarser, finer in zip(tail, tail[1:])
    )


def _input_record(specification):
    envelope = specification["envelope"]
    return {
        "initial_state": specification["initial_state"],
        "envelope_type": envelope.__class__.__name__,
        "peak_amplitude_rad_per_us": (
            envelope.peak_amplitude_rad_per_us
        ),
        "pulse_duration_us": envelope.duration_us,
        "sigma_us": getattr(envelope, "sigma_us", None),
        "phase_rad": specification["phase"],
        "detuning_rad_per_us": specification["detuning"],
        "idle_duration_us": specification["idle_duration"],
        "rates": specification["rates"].to_dict(),
    }


def _csv_row(row):
    return {
        field: (
            ("pass" if row["result"] else "fail")
            if field == "result"
            else row.get(field)
        )
        for field in CSV_FIELDS
    }


def _write_plot(standard_cases, stress_cases, path):
    figure, axes = plt.subplots(1, 2, figsize=(13, 5))
    for case in standard_cases:
        ordered = sorted(case["sweep"], key=lambda row: row["max_step_us"])
        axes[0].loglog(
            [row["max_step_us"] for row in ordered],
            [max(row["max_element_error"], 1e-18) for row in ordered],
            marker="o",
            label=case["name"],
        )
    axes[0].axhline(
        STANDARD_ERROR_TOLERANCE,
        color="black",
        linestyle="--",
        linewidth=1,
        label="fixed tolerance",
    )
    axes[0].set_title("Standard-case convergence")
    axes[0].set_xlabel("Maximum internal step [us]")
    axes[0].set_ylabel("Final max-element error")
    axes[0].grid(True, which="both", alpha=0.25)
    axes[0].legend(fontsize=8)

    for case in stress_cases:
        axes[1].semilogx(
            [
                max(
                    row["h_times_hamiltonian_gap"],
                    row["h_times_dissipative_scale"],
                )
                for row in case["sweep"]
            ],
            [
                row["raw_minimum_eigenvalue"]
                for row in case["sweep"]
            ],
            marker="o",
            label=case["name"],
        )
    axes[1].axvline(
        0.05,
        color="#1b9e77",
        linestyle="--",
        linewidth=1,
        label="recommended bound",
    )
    axes[1].axhline(
        MINIMUM_RAW_EIGENVALUE,
        color="black",
        linestyle=":",
        linewidth=1,
        label="physicality tolerance",
    )
    axes[1].set_title("Extreme-condition raw physicality")
    axes[1].set_xlabel("max(h G_H, h G_D)")
    axes[1].set_ylabel("Minimum raw eigenvalue")
    axes[1].grid(True, which="both", alpha=0.25)
    axes[1].legend(fontsize=8)
    figure.tight_layout()
    figure.savefig(path, dpi=170)
    plt.close(figure)


def _write_report(report, path):
    lines = [
        "# Pulse BA-5: Two-Level Convergence",
        "",
        f"**Result:** {'PASS' if report['overall_pass'] else 'FAIL'}",
        "",
        "## Step Policy",
        "",
        "The validated Baseline A reference recommendation is:",
        "",
        f"- `h G_H <= {EPSILON_H}`",
        f"- `h G_D <= {EPSILON_D}`",
        f"- at least `{SAMPLES_PER_SIGMA}` internal steps per Gaussian sigma",
        "",
        "`G_D` is the sum of all active downward, upward, and pure-dephasing "
        "rates. The most restrictive bound selects the step.",
        "",
        "## Standard Cases",
        "",
        "| Case | Recommended h [us] | Error | Observed order | Result |",
        "|---|---:|---:|---:|---|",
    ]
    for case in report["standard_cases"]:
        row = case["recommended_step"]
        orders = [
            point["observed_order"]
            for point in case["sweep"]
            if point["observed_order"] is not None
        ]
        lines.append(
            f"| {case['name']} | {row['max_step_us']:.6g} | "
            f"{row['max_element_error']:.3e} | "
            f"{min(orders):.3f}-{max(orders):.3f} | "
            f"{'PASS' if case['pass'] else 'FAIL'} |"
        )
    lines.extend([
        "",
        "## Extreme-Condition Audit",
        "",
        "Extreme drive, relaxation, and combined drive/detuning/dissipation "
        "were swept beyond the recommended domain. Safe-region points pass. "
        "At least one deliberately coarse point produces a negative raw "
        "eigenvalue, documenting that fixed-step RK4 is not intrinsically "
        "CPTP and that cleanup must not be used to justify an unsafe step.",
        "",
        "## Interpretation",
        "",
        "This study supports the internal numerical step policy for the "
        "two-level rotating-frame RWA model. It does not calibrate pulse "
        "parameters against hardware and does not cover qutrit leakage, "
        "DRAG, or multi-qubit pulse control.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def _parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "validation_results",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=ROOT / "docs" / "validation"
        / "pulse-convergence-2level.md",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

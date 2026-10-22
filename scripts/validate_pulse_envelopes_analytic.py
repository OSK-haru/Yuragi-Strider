"""Generate BA-2 square/Gaussian analytic validation artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import sys
import time
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._common import git_commit
from core.gates import zero_hamiltonian
from core.pulse_envelopes import (
    GaussianPulseEnvelope,
    PulseEnvelope,
    SquarePulseEnvelope,
    finite_gaussian_area_factor,
)
from core.pulse_evolution import (
    ConstantHamiltonian,
    evolve_time_dependent_segment,
)
from validation_pulse.pulse_analytic import (
    analytic_resonant_x_density,
    matrix_error_metrics,
    observed_order,
    pure_target_fidelity,
    run_resonant_closed_trajectory,
)
from validation_pulse.time_grid import uniform_times


TRAJECTORY_TOLERANCE = 2e-8
TARGET_INFIDELITY_TOLERANCE = 2e-8
AREA_TOLERANCE = 1e-13
CLEANUP_TOLERANCE = 1e-12
IDLE_TOLERANCE = 1e-14
MINIMUM_OBSERVED_ORDER = 3.0

CSV_FIELDS = [
    "case",
    "shape",
    "target_angle_rad",
    "time_us",
    "numeric_population_1",
    "analytic_population_1",
    "max_element_error",
    "frobenius_error",
    "trace_distance",
    "bloch_vector_error",
    "pulse_area_error",
    "pulse_end_target_fidelity",
    "pulse_end_target_infidelity",
    "cleanup_correction_norm",
    "result",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    cases, rows = _run_trajectory_cases()
    truncation = _run_truncation_audit()
    convergence = _run_convergence_audit()
    idle = _run_idle_audit()
    overall_pass = (
        all(case["pass"] for case in cases)
        and truncation["pass"]
        and convergence["pass"]
        and idle["pass"]
    )

    report = {
        "validation": "PULSE-BA2",
        "base_git_commit": git_commit(ROOT),
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "model_id": "driven_two_level_rwa_experimental_v1",
        "frame": "rotating",
        "approximation": "RWA",
        "solver": "BA-1 fixed-step time-dependent RK4 reference",
        "cleanup_policy": (
            "no cleanup inside RK4 stages; Hermitization and trace "
            "normalization once after each complete internal step"
        ),
        "tolerances": {
            "trajectory_max_element_error": TRAJECTORY_TOLERANCE,
            "pulse_end_target_infidelity": TARGET_INFIDELITY_TOLERANCE,
            "pulse_area_error": AREA_TOLERANCE,
            "cleanup_correction_norm": CLEANUP_TOLERANCE,
            "idle_state_error": IDLE_TOLERANCE,
            "minimum_observed_order": MINIMUM_OBSERVED_ORDER,
        },
        "cases": cases,
        "gaussian_truncation": truncation,
        "gaussian_step_refinement": convergence,
        "post_pulse_idle": idle,
        "overall_pass": overall_pass,
        "scope_and_limitations": {
            "proves": [
                "tested resonant zero-phase square trajectories",
                "tested resonant zero-phase finite Gaussian trajectories",
                "tested finite-support Gaussian target-angle normalization",
                "tested decreasing Gaussian error under step refinement",
                "tested closed idle state preservation after the pulse",
            ],
            "does_not_prove": [
                "nonzero phase correctness",
                "detuning correctness",
                "open-system driven dynamics",
                "qutrit leakage or DRAG",
                "hardware-calibrated pulse reproduction",
            ],
        },
    }

    json_path = args.output_dir / "pulse_ba2_envelopes_analytic.json"
    csv_path = args.output_dir / "pulse_ba2_envelopes_analytic.csv"
    square_plot = args.output_dir / "pulse_square_rabi_trajectory.png"
    gaussian_plot = args.output_dir / "pulse_gaussian_analytic_trajectory.png"
    truncation_plot = (
        args.output_dir / "pulse_gaussian_truncation_error.png"
    )

    json_path.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    _write_trajectory_plot(cases, "square", square_plot)
    _write_trajectory_plot(cases, "gaussian", gaussian_plot)
    _write_truncation_plot(truncation, truncation_plot)
    _write_report(report, args.report_path)

    print(f"validation | PULSE-BA2 | overall_pass={overall_pass}")
    for case in cases:
        print(
            f"{case['name']} | "
            f"max_error={case['max_trajectory_error']:.6e} | "
            f"area_error={case['pulse_area_error']:.6e} | "
            f"pass={case['pass']}"
        )
    print(
        "convergence | "
        f"finest_error={convergence['records'][-1]['error']:.6e} | "
        f"last_order={convergence['records'][-2]['observed_order']:.4f}"
    )
    print(
        f"artifacts | {json_path} | {csv_path} | "
        f"{square_plot} | {gaussian_plot} | {truncation_plot}"
    )
    return 0 if overall_pass else 1


def _run_trajectory_cases():
    specifications = (
        (
            "square_x_pi",
            SquarePulseEnvelope.from_target_rotation_angle(math.pi, 1.0),
            math.pi,
            101,
            0.005,
        ),
        (
            "square_x_pi_over_2",
            SquarePulseEnvelope.from_target_rotation_angle(
                math.pi / 2.0,
                1.0,
            ),
            math.pi / 2.0,
            101,
            0.01,
        ),
        (
            "square_two_rabi_periods",
            SquarePulseEnvelope(2.0 * math.pi, 2.0),
            4.0 * math.pi,
            201,
            0.0025,
        ),
        (
            "gaussian_x_pi",
            GaussianPulseEnvelope.from_target_rotation_angle(
                math.pi,
                0.2,
                4.0,
            ),
            math.pi,
            161,
            0.005,
        ),
        (
            "gaussian_x_pi_over_2",
            GaussianPulseEnvelope.from_target_rotation_angle(
                math.pi / 2.0,
                0.2,
                4.0,
            ),
            math.pi / 2.0,
            161,
            0.005,
        ),
    )
    cases = []
    rows = []
    for name, envelope, target_angle, sample_count, max_step in specifications:
        started = time.perf_counter()
        times = uniform_times(envelope.duration_us, sample_count)
        result = run_resonant_closed_trajectory(
            envelope,
            times,
            max_step,
        )
        runtime_ms = (time.perf_counter() - started) * 1000.0
        trajectory = []
        for checkpoint in result.checkpoints:
            expected = analytic_resonant_x_density(
                envelope,
                checkpoint.time_us,
            )
            metrics = matrix_error_metrics(
                checkpoint.cleaned_state,
                expected,
            )
            record = {
                "time_us": checkpoint.time_us,
                "numeric_population_1": checkpoint.cleaned_state[1][1].real,
                "analytic_population_1": expected[1][1].real,
                **metrics,
            }
            trajectory.append(record)
            rows.append({
                "case": name,
                "shape": _shape_name(envelope),
                "target_angle_rad": target_angle,
                **record,
                "pulse_area_error": abs(
                    envelope.pulse_area_rad - target_angle
                ),
                "cleanup_correction_norm": (
                    result.diagnostics.cleanup_correction_norm
                ),
                "result": "pass",
            })
        maximum_error = max(
            record["max_element_error"] for record in trajectory
        )
        area_error = abs(envelope.pulse_area_rad - target_angle)
        target_density = analytic_resonant_x_density(
            envelope,
            envelope.duration_us,
        )
        target_fidelity = pure_target_fidelity(
            result.state,
            target_density,
        )
        target_infidelity = abs(1.0 - target_fidelity)
        passed = (
            maximum_error <= TRAJECTORY_TOLERANCE
            and target_infidelity <= TARGET_INFIDELITY_TOLERANCE
            and area_error <= AREA_TOLERANCE
            and result.diagnostics.cleanup_correction_norm
            <= CLEANUP_TOLERANCE
        )
        for row in rows[-len(trajectory):]:
            row["pulse_end_target_fidelity"] = target_fidelity
            row["pulse_end_target_infidelity"] = target_infidelity
            row["result"] = "pass" if passed else "fail"
        cases.append({
            "name": name,
            "shape": _shape_name(envelope),
            "target_angle_rad": target_angle,
            "duration_us": envelope.duration_us,
            "max_internal_step_us": max_step,
            "runtime_ms": runtime_ms,
            "pulse_area_rad": envelope.pulse_area_rad,
            "pulse_area_error": area_error,
            "pulse_end_target_fidelity": target_fidelity,
            "pulse_end_target_infidelity": target_infidelity,
            "max_trajectory_error": maximum_error,
            "max_frobenius_error": max(
                record["frobenius_error"] for record in trajectory
            ),
            "max_trace_distance": max(
                record["trace_distance"] for record in trajectory
            ),
            "max_bloch_vector_error": max(
                record["bloch_vector_error"] for record in trajectory
            ),
            "cleanup_correction_norm": (
                result.diagnostics.cleanup_correction_norm
            ),
            "trajectory": trajectory,
            "pass": passed,
        })
    return cases, rows


def _run_truncation_audit() -> dict[str, object]:
    target = math.pi
    records = []
    for truncation in (3.0, 4.0, 5.0):
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(
            target,
            sigma_us=0.2,
            truncation_sigma=truncation,
        )
        finite_error = abs(envelope.pulse_area_rad - target)
        infinite_peak = target / (0.2 * math.sqrt(2.0 * math.pi))
        infinite_assumption_area = (
            infinite_peak
            * finite_gaussian_area_factor(0.2, truncation)
        )
        records.append({
            "truncation_sigma": truncation,
            "duration_us": envelope.duration_us,
            "finite_normalized_peak_rad_per_us": (
                envelope.peak_amplitude_rad_per_us
            ),
            "finite_normalization_area_error": finite_error,
            "infinite_normalization_area_error": abs(
                infinite_assumption_area - target
            ),
        })
    return {
        "target_angle_rad": target,
        "records": records,
        "pass": all(
            record["finite_normalization_area_error"] <= AREA_TOLERANCE
            for record in records
        ),
    }


def _run_convergence_audit() -> dict[str, object]:
    envelope = GaussianPulseEnvelope.from_target_rotation_angle(
        math.pi,
        sigma_us=0.2,
        truncation_sigma=4.0,
    )
    exact = analytic_resonant_x_density(envelope, envelope.duration_us)
    records = []
    for max_step in (0.08, 0.04, 0.02, 0.01):
        started = time.perf_counter()
        result = run_resonant_closed_trajectory(
            envelope,
            (0.0, envelope.duration_us),
            max_step,
        )
        runtime_ms = (time.perf_counter() - started) * 1000.0
        error = matrix_error_metrics(
            result.state,
            exact,
        )["max_element_error"]
        records.append({
            "max_internal_step_us": max_step,
            "actual_internal_step_count": (
                result.diagnostics.internal_step_count
            ),
            "error": error,
            "runtime_ms": runtime_ms,
            "cleanup_correction_norm": (
                result.diagnostics.cleanup_correction_norm
            ),
            "observed_order": None,
        })
    for index in range(len(records) - 1):
        records[index]["observed_order"] = observed_order(
            records[index]["error"],
            records[index + 1]["error"],
        )
    errors = [record["error"] for record in records]
    reliable_orders = [
        record["observed_order"]
        for record in records[:-1]
        if record["observed_order"] is not None
    ]
    return {
        "records": records,
        "pass": (
            all(left > right for left, right in zip(errors, errors[1:]))
            and reliable_orders[-1] >= MINIMUM_OBSERVED_ORDER
            and max(
                record["cleanup_correction_norm"] for record in records
            )
            <= CLEANUP_TOLERANCE
        ),
    }


def _run_idle_audit() -> dict[str, object]:
    envelope = GaussianPulseEnvelope.from_target_rotation_angle(
        math.pi,
        sigma_us=0.2,
        truncation_sigma=4.0,
    )
    pulse_result = run_resonant_closed_trajectory(
        envelope,
        (0.0, envelope.duration_us),
        max_step_us=0.005,
    )
    idle_result = evolve_time_dependent_segment(
        pulse_result.state,
        ConstantHamiltonian(zero_hamiltonian(2)),
        (),
        duration_us=1.0,
        max_step_us=0.1,
    )
    error = matrix_error_metrics(
        idle_result.state,
        pulse_result.state,
    )["max_element_error"]
    return {
        "idle_duration_us": 1.0,
        "max_element_error": error,
        "pass": error <= IDLE_TOLERANCE,
    }


def _shape_name(envelope: PulseEnvelope) -> str:
    if isinstance(envelope, SquarePulseEnvelope):
        return "square"
    return "gaussian"


def _write_trajectory_plot(cases, shape: str, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(8, 5))
    for case in cases:
        if case["shape"] != shape:
            continue
        times = [record["time_us"] for record in case["trajectory"]]
        numeric = [
            record["numeric_population_1"]
            for record in case["trajectory"]
        ]
        analytic = [
            record["analytic_population_1"]
            for record in case["trajectory"]
        ]
        axis.plot(times, numeric, label=f"{case['name']} numeric")
        axis.plot(
            times,
            analytic,
            linestyle="--",
            label=f"{case['name']} analytic",
        )
    axis.set_xlabel("time [us]")
    axis.set_ylabel("population |1>")
    axis.set_title(
        f"Actual calculation result: {shape} pulse trajectories"
    )
    axis.grid(True, alpha=0.3)
    axis.legend(fontsize=8)
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_truncation_plot(audit, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    records = audit["records"]
    truncations = [record["truncation_sigma"] for record in records]
    finite_errors = [
        max(record["finite_normalization_area_error"], 1e-18)
        for record in records
    ]
    infinite_errors = [
        record["infinite_normalization_area_error"] for record in records
    ]
    figure, axis = plt.subplots(figsize=(7, 4.5))
    axis.semilogy(
        truncations,
        finite_errors,
        "o-",
        label="finite-support normalization",
    )
    axis.semilogy(
        truncations,
        infinite_errors,
        "s--",
        label="infinite-support assumption",
    )
    axis.set_xlabel("truncation sigma")
    axis.set_ylabel("pulse-area error [rad]")
    axis.set_title("Actual calculation result: Gaussian truncation")
    axis.grid(True, which="both", alpha=0.3)
    axis.legend()
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_report(report, path: Path) -> None:
    lines = [
        "# PULSE-BA2: Envelope and Analytic Trajectory Validation",
        "",
        "## Result",
        "",
        f"- Overall pass: `{report['overall_pass']}`",
        f"- Model: `{report['model_id']}`",
        f"- Frame / approximation: `{report['frame']}` / `{report['approximation']}`",
        "",
        "## Trajectory Cases",
        "",
        "| Case | Max element error | End fidelity | Area error | Cleanup correction | Pass |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for case in report["cases"]:
        lines.append(
            f"| {case['name']} | {case['max_trajectory_error']:.6e} | "
            f"{case['pulse_end_target_fidelity']:.12f} | "
            f"{case['pulse_area_error']:.6e} | "
            f"{case['cleanup_correction_norm']:.6e} | {case['pass']} |"
        )
    truncation = report["gaussian_truncation"]
    convergence = report["gaussian_step_refinement"]
    idle = report["post_pulse_idle"]
    lines.extend([
        "",
        "## Gaussian Finite-Support Normalization",
        "",
        f"- Pass: `{truncation['pass']}`",
        "- Target-angle mode uses the finite erf integral, not the infinite-support approximation.",
        "",
        "| Truncation | Finite area error | Infinite-assumption error |",
        "|---:|---:|---:|",
    ])
    for record in truncation["records"]:
        lines.append(
            f"| {record['truncation_sigma']:.0f} | "
            f"{record['finite_normalization_area_error']:.6e} | "
            f"{record['infinite_normalization_area_error']:.6e} |"
        )
    lines.extend([
        "",
        "## Step Refinement",
        "",
        f"- Pass: `{convergence['pass']}`",
        "",
        "| Max step [us] | Error | Observed order |",
        "|---:|---:|---:|",
    ])
    for record in convergence["records"]:
        order = record["observed_order"]
        order_text = "n/a" if order is None else f"{order:.4f}"
        lines.append(
            f"| {record['max_internal_step_us']:.6g} | "
            f"{record['error']:.6e} | {order_text} |"
        )
    lines.extend([
        "",
        "## Post-Pulse Idle",
        "",
        f"- Idle duration: `{idle['idle_duration_us']}` us",
        f"- State error: `{idle['max_element_error']:.6e}`",
        f"- Pass: `{idle['pass']}`",
        "",
        "## Interpretation",
        "",
        "The tested resonant, zero-phase square and finite Gaussian pulses agree with the exact commuting-Hamiltonian trajectory over the full sampled evolution. Target-angle Gaussian normalization uses the finite support, and closed idle evolution preserves the pulse-end state.",
        "",
        "This phase does not validate nonzero phase, detuning, driven dissipation, qutrit leakage, DRAG, or calibrated hardware behavior.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        default=(
            ROOT
            / "docs"
            / "validation"
            / "pulse-ba2-envelopes-analytic.md"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

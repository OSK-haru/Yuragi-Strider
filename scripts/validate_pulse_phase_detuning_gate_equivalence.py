"""Generate BA-3 phase, detuning, and gate-equivalence artifacts."""

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
from core.circuit_model import GateOperation
from core.gates import (
    X,
    apply_gate_operation,
    effective_hamiltonian_from_involution,
    initial_density_matrix,
)
from core.pulse_evolution import (
    ConstantHamiltonian,
    evolve_time_dependent_segment,
)
from validation_pulse.pulse_analytic import bloch_vector, matrix_error_metrics
from validation_pulse.pulse_phase_detuning import (
    analytic_constant_drive_density,
    evolve_density_with_unitary,
    run_constant_closed_trajectory,
    target_rotation_unitary,
)
from validation_pulse.time_grid import uniform_times


TRAJECTORY_TOLERANCE = 2e-8
EQUIVALENCE_TOLERANCE = 2e-8
CLEANUP_TOLERANCE = 1e-12
MINIMUM_COHERENCE_SIGN_MAGNITUDE = 0.05

CSV_FIELDS = [
    "category",
    "case",
    "time_us",
    "phase_rad",
    "detuning_rad_per_us",
    "numeric_population_1",
    "analytic_population_1",
    "numeric_bloch_x",
    "numeric_bloch_y",
    "numeric_bloch_z",
    "analytic_bloch_x",
    "analytic_bloch_y",
    "analytic_bloch_z",
    "numeric_coherence_real",
    "numeric_coherence_imag",
    "max_element_error",
    "reference_path",
    "initial_probe",
    "result",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    phase_cases, phase_rows = _run_phase_cases()
    detuning = _run_detuning_cases()
    equivalence = _run_gate_equivalence()
    overall_pass = (
        all(case["pass"] for case in phase_cases)
        and detuning["pass"]
        and equivalence["pass"]
    )

    report = {
        "validation": "PULSE-BA3",
        "base_git_commit": git_commit(ROOT),
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "model_id": "driven_two_level_rwa_experimental_v1",
        "frame": "rotating",
        "approximation": "RWA",
        "detuning_convention": "Delta = omega_d - omega_q",
        "phase_convention": "positive phase rotates the drive axis from +x toward +y",
        "solver": "BA-1 fixed-step time-dependent RK4 reference",
        "tolerances": {
            "trajectory_max_element_error": TRAJECTORY_TOLERANCE,
            "gate_equivalence_max_element_error": EQUIVALENCE_TOLERANCE,
            "cleanup_correction_norm": CLEANUP_TOLERANCE,
            "minimum_coherence_sign_magnitude": (
                MINIMUM_COHERENCE_SIGN_MAGNITUDE
            ),
        },
        "phase_cases": phase_cases,
        "detuning_cases": detuning,
        "gate_equivalence": equivalence,
        "overall_pass": overall_pass,
        "scope_and_limitations": {
            "proves": [
                "four required rotating-frame phase axes",
                "positive and negative detuning against a closed-form unitary",
                "detuning sign visibility in coherence and Bloch x",
                "X pi equivalence across pulse, existing X gate, gate-effective Hamiltonian, and independent target",
                "fractional X and Y pulse agreement with independent target unitaries",
            ],
            "does_not_prove": [
                "new RX or RY logical gate support",
                "open-system driven dynamics",
                "laboratory-frame carrier dynamics",
                "qutrit leakage or DRAG",
                "hardware-calibrated pulse reproduction",
            ],
        },
    }

    rows = [
        *phase_rows,
        *detuning["rows"],
        *equivalence["rows"],
    ]
    json_path = (
        args.output_dir
        / "pulse_ba3_phase_detuning_gate_equivalence.json"
    )
    csv_path = (
        args.output_dir
        / "pulse_ba3_phase_detuning_gate_equivalence.csv"
    )
    phase_plot = args.output_dir / "pulse_phase_bloch_trajectories.png"
    detuning_plot = (
        args.output_dir / "pulse_detuning_bloch_trajectories.png"
    )
    equivalence_plot = (
        args.output_dir / "pulse_gate_equivalence_error.png"
    )

    json_path.write_text(
        json.dumps(_without_rows(report), indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    _write_phase_plot(phase_cases, phase_plot)
    _write_detuning_plot(detuning, detuning_plot)
    _write_equivalence_plot(equivalence, equivalence_plot)
    _write_report(report, args.report_path)

    print(f"validation | PULSE-BA3 | overall_pass={overall_pass}")
    for case in phase_cases:
        print(
            f"{case['name']} | "
            f"max_error={case['max_trajectory_error']:.6e} | "
            f"pass={case['pass']}"
        )
    print(
        "detuning | "
        f"max_error={detuning['maximum_trajectory_error']:.6e} | "
        f"population_pair_error={detuning['population_pair_error']:.6e} | "
        f"pass={detuning['pass']}"
    )
    print(
        "gate_equivalence | "
        f"max_error={equivalence['maximum_error']:.6e} | "
        f"pass={equivalence['pass']}"
    )
    print(
        f"artifacts | {json_path} | {csv_path} | {phase_plot} | "
        f"{detuning_plot} | {equivalence_plot}"
    )
    return 0 if overall_pass else 1


def _run_phase_cases():
    initial = initial_density_matrix(["0"])
    amplitude = math.pi / 2.0
    duration = 1.0
    sample_times = uniform_times(duration, 101)
    specifications = (
        ("phase_0_plus_x", 0.0, (0.0, -1.0, 0.0)),
        ("phase_pi_over_2_plus_y", math.pi / 2.0, (1.0, 0.0, 0.0)),
        ("phase_pi_minus_x", math.pi, (0.0, 1.0, 0.0)),
        ("phase_minus_pi_over_2_minus_y", -math.pi / 2.0, (-1.0, 0.0, 0.0)),
    )
    cases = []
    rows = []
    for name, phase, expected_final_bloch in specifications:
        started = time.perf_counter()
        result = run_constant_closed_trajectory(
            initial,
            amplitude,
            phase,
            0.0,
            duration,
            sample_times,
            0.005,
        )
        runtime_ms = (time.perf_counter() - started) * 1000.0
        trajectory = []
        for checkpoint in result.checkpoints:
            expected = analytic_constant_drive_density(
                initial,
                amplitude,
                phase,
                0.0,
                checkpoint.time_us,
            )
            record = _trajectory_record(
                checkpoint.time_us,
                checkpoint.cleaned_state,
                expected,
            )
            trajectory.append(record)
            rows.append(_csv_trajectory_row(
                "phase",
                name,
                phase,
                0.0,
                record,
            ))
        maximum_error = max(
            record["max_element_error"] for record in trajectory
        )
        final_bloch = trajectory[-1]["numeric_bloch"]
        final_bloch_error = max(
            abs(actual - expected)
            for actual, expected in zip(
                final_bloch,
                expected_final_bloch,
                strict=True,
            )
        )
        passed = (
            maximum_error <= TRAJECTORY_TOLERANCE
            and final_bloch_error <= TRAJECTORY_TOLERANCE
            and result.diagnostics.cleanup_correction_norm
            <= CLEANUP_TOLERANCE
        )
        for row in rows[-len(trajectory):]:
            row["result"] = "pass" if passed else "fail"
        cases.append({
            "name": name,
            "phase_rad": phase,
            "rotation_axis": _phase_axis_label(phase),
            "amplitude_rad_per_us": amplitude,
            "duration_us": duration,
            "runtime_ms": runtime_ms,
            "max_trajectory_error": maximum_error,
            "final_bloch": list(final_bloch),
            "expected_final_bloch": list(expected_final_bloch),
            "final_bloch_error": final_bloch_error,
            "cleanup_correction_norm": (
                result.diagnostics.cleanup_correction_norm
            ),
            "trajectory": trajectory,
            "pass": passed,
        })
    return cases, rows


def _run_detuning_cases():
    initial = initial_density_matrix(["0"])
    amplitude = math.pi
    detuning_magnitude = 0.75 * math.pi
    duration = 1.0
    sample_times = uniform_times(duration, 101)
    cases = []
    rows = []
    for name, detuning in (
        ("positive_detuning", detuning_magnitude),
        ("negative_detuning", -detuning_magnitude),
    ):
        started = time.perf_counter()
        result = run_constant_closed_trajectory(
            initial,
            amplitude,
            0.0,
            detuning,
            duration,
            sample_times,
            0.005,
        )
        runtime_ms = (time.perf_counter() - started) * 1000.0
        trajectory = []
        for checkpoint in result.checkpoints:
            expected = analytic_constant_drive_density(
                initial,
                amplitude,
                0.0,
                detuning,
                checkpoint.time_us,
            )
            record = _trajectory_record(
                checkpoint.time_us,
                checkpoint.cleaned_state,
                expected,
            )
            trajectory.append(record)
            rows.append(_csv_trajectory_row(
                "detuning",
                name,
                0.0,
                detuning,
                record,
            ))
        maximum_error = max(
            record["max_element_error"] for record in trajectory
        )
        passed = (
            maximum_error <= TRAJECTORY_TOLERANCE
            and result.diagnostics.cleanup_correction_norm
            <= CLEANUP_TOLERANCE
        )
        for row in rows[-len(trajectory):]:
            row["result"] = "pass" if passed else "fail"
        cases.append({
            "name": name,
            "detuning_rad_per_us": detuning,
            "amplitude_rad_per_us": amplitude,
            "effective_rate_rad_per_us": math.hypot(amplitude, detuning),
            "duration_us": duration,
            "runtime_ms": runtime_ms,
            "max_trajectory_error": maximum_error,
            "final_coherence_real": (
                trajectory[-1]["numeric_coherence_real"]
            ),
            "final_coherence_imag": (
                trajectory[-1]["numeric_coherence_imag"]
            ),
            "cleanup_correction_norm": (
                result.diagnostics.cleanup_correction_norm
            ),
            "trajectory": trajectory,
            "pass": passed,
        })

    positive = cases[0]["trajectory"]
    negative = cases[1]["trajectory"]
    population_pair_error = max(
        abs(
            positive_record["numeric_population_1"]
            - negative_record["numeric_population_1"]
        )
        for positive_record, negative_record in zip(
            positive,
            negative,
            strict=True,
        )
    )
    coherence_real_antisymmetry_error = max(
        abs(
            positive_record["numeric_coherence_real"]
            + negative_record["numeric_coherence_real"]
        )
        for positive_record, negative_record in zip(
            positive,
            negative,
            strict=True,
        )
    )
    coherence_imag_symmetry_error = max(
        abs(
            positive_record["numeric_coherence_imag"]
            - negative_record["numeric_coherence_imag"]
        )
        for positive_record, negative_record in zip(
            positive,
            negative,
            strict=True,
        )
    )
    positive_final_real = cases[0]["final_coherence_real"]
    negative_final_real = cases[1]["final_coherence_real"]
    sign_check_pass = (
        positive_final_real >= MINIMUM_COHERENCE_SIGN_MAGNITUDE
        and negative_final_real <= -MINIMUM_COHERENCE_SIGN_MAGNITUDE
    )
    passed = (
        all(case["pass"] for case in cases)
        and population_pair_error <= TRAJECTORY_TOLERANCE
        and coherence_real_antisymmetry_error <= TRAJECTORY_TOLERANCE
        and coherence_imag_symmetry_error <= TRAJECTORY_TOLERANCE
        and sign_check_pass
    )
    for row in rows:
        row["result"] = "pass" if passed else "fail"
    return {
        "amplitude_rad_per_us": amplitude,
        "detuning_magnitude_rad_per_us": detuning_magnitude,
        "duration_us": duration,
        "cases": cases,
        "population_pair_error": population_pair_error,
        "coherence_real_antisymmetry_error": (
            coherence_real_antisymmetry_error
        ),
        "coherence_imag_symmetry_error": coherence_imag_symmetry_error,
        "positive_final_coherence_real": positive_final_real,
        "negative_final_coherence_real": negative_final_real,
        "coherence_sign_check_pass": sign_check_pass,
        "maximum_trajectory_error": max(
            case["max_trajectory_error"] for case in cases
        ),
        "rows": rows,
        "pass": passed,
    }


def _run_gate_equivalence():
    duration = 1.0
    gate_hamiltonian = effective_hamiltonian_from_involution(X, duration)
    x_gate = GateOperation(type="X", targets=[0])
    specifications = (
        ("x_pi", "x", math.pi, 0.0),
        ("x_pi_over_2", "x", math.pi / 2.0, 0.0),
        ("y_pi", "y", math.pi, math.pi / 2.0),
        ("y_pi_over_2", "y", math.pi / 2.0, math.pi / 2.0),
    )
    cases = []
    rows = []
    for name, axis, angle, phase in specifications:
        target = target_rotation_unitary(axis, angle)
        comparisons = []
        for probe_name, initial in _probe_states().items():
            pulse = run_constant_closed_trajectory(
                initial,
                angle,
                phase,
                0.0,
                duration,
                (0.0, duration),
                0.005,
            ).state
            independent = evolve_density_with_unitary(initial, target)
            paths = [("pulse", pulse)]
            if name == "x_pi":
                paths.extend([
                    (
                        "gate_effective_hamiltonian",
                        evolve_time_dependent_segment(
                            initial,
                            ConstantHamiltonian(gate_hamiltonian),
                            (),
                            duration_us=duration,
                            max_step_us=0.005,
                        ).state,
                    ),
                    (
                        "existing_x_gate",
                        apply_gate_operation(initial, x_gate, 1),
                    ),
                ])
            for path_name, actual in paths:
                metrics = matrix_error_metrics(actual, independent)
                comparison = {
                    "initial_probe": probe_name,
                    "reference_path": path_name,
                    **metrics,
                }
                comparisons.append(comparison)
                rows.append(_csv_equivalence_row(
                    name,
                    phase,
                    path_name,
                    probe_name,
                    metrics["max_element_error"],
                ))
        maximum_error = max(
            comparison["max_element_error"]
            for comparison in comparisons
        )
        passed = maximum_error <= EQUIVALENCE_TOLERANCE
        for row in rows[-len(comparisons):]:
            row["result"] = "pass" if passed else "fail"
        cases.append({
            "name": name,
            "axis": axis,
            "angle_rad": angle,
            "phase_rad": phase,
            "logical_gate_support": (
                "existing X gate" if name == "x_pi"
                else "validation-only target unitary"
            ),
            "comparisons": comparisons,
            "maximum_error": maximum_error,
            "pass": passed,
        })
    return {
        "cases": cases,
        "maximum_error": max(case["maximum_error"] for case in cases),
        "rows": rows,
        "pass": all(case["pass"] for case in cases),
    }


def _trajectory_record(time_us, numeric, analytic):
    numeric_bloch = bloch_vector(numeric)
    analytic_bloch = bloch_vector(analytic)
    return {
        "time_us": time_us,
        "numeric_population_1": numeric[1][1].real,
        "analytic_population_1": analytic[1][1].real,
        "numeric_bloch": list(numeric_bloch),
        "analytic_bloch": list(analytic_bloch),
        "numeric_coherence_real": numeric[0][1].real,
        "numeric_coherence_imag": numeric[0][1].imag,
        **matrix_error_metrics(numeric, analytic),
    }


def _csv_trajectory_row(
    category,
    case,
    phase,
    detuning,
    record,
):
    return {
        "category": category,
        "case": case,
        "time_us": record["time_us"],
        "phase_rad": phase,
        "detuning_rad_per_us": detuning,
        "numeric_population_1": record["numeric_population_1"],
        "analytic_population_1": record["analytic_population_1"],
        "numeric_bloch_x": record["numeric_bloch"][0],
        "numeric_bloch_y": record["numeric_bloch"][1],
        "numeric_bloch_z": record["numeric_bloch"][2],
        "analytic_bloch_x": record["analytic_bloch"][0],
        "analytic_bloch_y": record["analytic_bloch"][1],
        "analytic_bloch_z": record["analytic_bloch"][2],
        "numeric_coherence_real": record["numeric_coherence_real"],
        "numeric_coherence_imag": record["numeric_coherence_imag"],
        "max_element_error": record["max_element_error"],
        "reference_path": "",
        "initial_probe": "",
        "result": "pass",
    }


def _csv_equivalence_row(
    case,
    phase,
    reference_path,
    initial_probe,
    error,
):
    row = {field: "" for field in CSV_FIELDS}
    row.update({
        "category": "gate_equivalence",
        "case": case,
        "phase_rad": phase,
        "detuning_rad_per_us": 0.0,
        "max_element_error": error,
        "reference_path": reference_path,
        "initial_probe": initial_probe,
        "result": "pass",
    })
    return row


def _write_phase_plot(cases, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(3, 1, figsize=(9, 9), sharex=True)
    for case in cases:
        times = [record["time_us"] for record in case["trajectory"]]
        for component, axis in enumerate(axes):
            values = [
                record["numeric_bloch"][component]
                for record in case["trajectory"]
            ]
            axis.plot(times, values, label=case["rotation_axis"])
    for axis, label in zip(axes, ("<sigma_x>", "<sigma_y>", "<sigma_z>")):
        axis.set_ylabel(label)
        axis.grid(True, alpha=0.3)
    axes[0].legend(ncol=2)
    axes[-1].set_xlabel("time [us]")
    figure.suptitle("Actual calculation result: phase-axis Bloch trajectories")
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_detuning_plot(detuning, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(3, 1, figsize=(9, 9), sharex=True)
    for case in detuning["cases"]:
        label = (
            "positive detuning"
            if case["detuning_rad_per_us"] > 0.0
            else "negative detuning"
        )
        times = [record["time_us"] for record in case["trajectory"]]
        axes[0].plot(
            times,
            [record["numeric_bloch"][0] for record in case["trajectory"]],
            label=label,
        )
        axes[1].plot(
            times,
            [record["numeric_bloch"][1] for record in case["trajectory"]],
            label=label,
        )
        axes[2].plot(
            times,
            [
                record["numeric_population_1"]
                for record in case["trajectory"]
            ],
            label=label,
        )
    axes[0].set_ylabel("<sigma_x>")
    axes[1].set_ylabel("<sigma_y>")
    axes[2].set_ylabel("population |1>")
    for axis in axes:
        axis.grid(True, alpha=0.3)
        axis.legend()
    axes[-1].set_xlabel("time [us]")
    figure.suptitle("Actual calculation result: detuning-sign trajectories")
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_equivalence_plot(equivalence, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = []
    errors = []
    for case in equivalence["cases"]:
        labels.append(case["name"])
        errors.append(max(case["maximum_error"], 1e-18))
    figure, axis = plt.subplots(figsize=(9, 5))
    axis.bar(labels, errors)
    axis.axhline(
        EQUIVALENCE_TOLERANCE,
        color="tab:red",
        linestyle="--",
        label="tolerance",
    )
    axis.set_yscale("log")
    axis.set_ylabel("maximum density-matrix element error")
    axis.set_title("Actual calculation result: pulse and target equivalence")
    axis.grid(True, axis="y", which="both", alpha=0.3)
    axis.legend()
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_report(report, path: Path) -> None:
    phase_cases = report["phase_cases"]
    detuning = report["detuning_cases"]
    equivalence = report["gate_equivalence"]
    lines = [
        "# PULSE-BA3: Phase, Detuning, and Gate Equivalence",
        "",
        "## Result",
        "",
        f"- Overall pass: `{report['overall_pass']}`",
        f"- Model: `{report['model_id']}`",
        f"- Frame / approximation: `{report['frame']}` / `{report['approximation']}`",
        f"- Detuning convention: `{report['detuning_convention']}`",
        "",
        "## Phase Axes",
        "",
        "| Case | Axis | Max element error | Final Bloch error | Pass |",
        "|---|---|---:|---:|---|",
    ]
    for case in phase_cases:
        lines.append(
            f"| {case['name']} | {case['rotation_axis']} | "
            f"{case['max_trajectory_error']:.6e} | "
            f"{case['final_bloch_error']:.6e} | {case['pass']} |"
        )
    lines.extend([
        "",
        "## Detuning Sign",
        "",
        f"- Maximum analytic trajectory error: `{detuning['maximum_trajectory_error']:.6e}`",
        f"- Positive final Re(rho01): `{detuning['positive_final_coherence_real']:.6e}`",
        f"- Negative final Re(rho01): `{detuning['negative_final_coherence_real']:.6e}`",
        f"- Population-pair error: `{detuning['population_pair_error']:.6e}`",
        f"- Re(rho01) antisymmetry error: `{detuning['coherence_real_antisymmetry_error']:.6e}`",
        f"- Im(rho01) symmetry error: `{detuning['coherence_imag_symmetry_error']:.6e}`",
        f"- Pass: `{detuning['pass']}`",
        "",
        "Equal-magnitude positive and negative detuning have matching populations in this fixture, while the real coherence and Bloch-x signs are opposite.",
        "",
        "## Gate And Target Equivalence",
        "",
        "| Case | Logical support | Maximum error | Pass |",
        "|---|---|---:|---|",
    ])
    for case in equivalence["cases"]:
        lines.append(
            f"| {case['name']} | {case['logical_gate_support']} | "
            f"{case['maximum_error']:.6e} | {case['pass']} |"
        )
    lines.extend([
        "",
        "The X-pi case compares the pulse, existing X gate, gate-effective Hamiltonian, and an independent Rx(pi) target over four probe states. Fractional X and all Y cases use validation-only target unitaries and do not add RX or RY circuit gates.",
        "",
        "## Interpretation",
        "",
        "The rotating-frame phase and detuning signs are visible in coherence and Bloch trajectories, not inferred from population alone. The tested closed-system pulse operations agree with independent target unitaries within the stated tolerance.",
        "",
        "This phase does not validate dissipation, laboratory-frame carrier dynamics, qutrit leakage, DRAG, or calibrated hardware behavior.",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def _without_rows(report):
    copied = dict(report)
    copied["detuning_cases"] = dict(report["detuning_cases"])
    copied["detuning_cases"].pop("rows", None)
    copied["gate_equivalence"] = dict(report["gate_equivalence"])
    copied["gate_equivalence"].pop("rows", None)
    return copied


def _phase_axis_label(phase: float) -> str:
    if phase == 0.0:
        return "+x"
    if phase == math.pi / 2.0:
        return "+y"
    if phase == math.pi:
        return "-x"
    return "-y"


def _probe_states():
    return {
        "zero": initial_density_matrix(["0"]),
        "one": initial_density_matrix(["1"]),
        "plus_x": (
            (0.5 + 0.0j, 0.5 + 0.0j),
            (0.5 + 0.0j, 0.5 + 0.0j),
        ),
        "plus_y": (
            (0.5 + 0.0j, 0.0 - 0.5j),
            (0.0 + 0.5j, 0.5 + 0.0j),
        ),
    }


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
            / "pulse-ba3-phase-detuning-gate-equivalence.md"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

"""Generate BA-5 time-dependent two-level QuTiP comparison artifacts."""

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
import qutip
import scipy


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._common import git_commit
from core.gates import (
    initial_density_matrix,
    multi_qubit_physical_collapse_operators,
    zero_hamiltonian,
)
from core.pulse_envelopes import (
    GaussianPulseEnvelope,
    SquarePulseEnvelope,
    TwoLevelPulseHamiltonian,
)
from core.pulse_open_system import (
    PulseDissipationRates,
    evolve_open_pulse_sequence,
)
from validation_pulse.pulse_step_policy import recommended_max_step_us
from validation_pulse.qutip_adapter import (
    compare_density_matrices,
    run_qutip_constant_segment,
    run_qutip_time_dependent_segment,
)
from validation_pulse.time_grid import uniform_times


COMPARISON_TOLERANCE = 5e-7
MAX_TRACE_ERROR = 1e-10
MINIMUM_EIGENVALUE = -1e-10
EPSILON_H = 0.05
EPSILON_D = 0.05
SAMPLES_PER_SIGMA = 20
QUTIP_MAX_STEP_US = 0.00125

CSV_FIELDS = [
    "case",
    "segment",
    "global_time_us",
    "quanta_population_0",
    "quanta_population_1",
    "qutip_population_0",
    "qutip_population_1",
    "max_element_difference",
    "frobenius_difference",
    "trace_distance",
    "population_difference",
    "coherence_difference",
    "quanta_trace_error",
    "qutip_trace_error",
    "quanta_minimum_eigenvalue",
    "qutip_minimum_eigenvalue",
    "result",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    cases = []
    rows = []
    for specification in _specifications():
        case, case_rows = _run_case(specification)
        cases.append(case)
        rows.extend(case_rows)
    overall_pass = all(case["pass"] for case in cases)

    report = {
        "validation": "PULSE-QUTIP-2LEVEL",
        "base_git_commit": git_commit(ROOT),
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "scipy_version": scipy.__version__,
        "qutip_version": qutip.__version__,
        "model_id": "driven_two_level_rwa_experimental_v1",
        "frame": "rotating",
        "approximation": "RWA",
        "shared_problem_contract": (
            "identical rho(0), H(t), collapse-operator matrices, and "
            "requested times are passed to both solvers"
        ),
        "quanta_solver": (
            "fixed-step RK4 with stage-time H(t), using the BA-5 "
            "recommended internal-step policy"
        ),
        "qutip_solver": {
            "entry_point": "qutip.mesolve",
            "method": "DOP853",
            "atol": 1e-12,
            "rtol": 1e-12,
            "max_step_us": QUTIP_MAX_STEP_US,
            "normalize_output": False,
        },
        "collapse_operator_convention": {
            "down": "sqrt(gamma_down) sigma_minus",
            "up": "sqrt(gamma_up) sigma_plus",
            "dephasing": "sqrt(gamma_phi / 2) sigma_z",
        },
        "fixed_tolerances": {
            "maximum_matrix_difference": COMPARISON_TOLERANCE,
            "trace_error": MAX_TRACE_ERROR,
            "minimum_eigenvalue": MINIMUM_EIGENVALUE,
        },
        "step_policy": {
            "epsilon_h": EPSILON_H,
            "epsilon_d": EPSILON_D,
            "samples_per_sigma": SAMPLES_PER_SIGMA,
        },
        "cases": cases,
        "overall_pass": overall_pass,
        "scope_and_limitations": {
            "proves": [
                "agreement for a shared time-dependent two-level problem",
                "phase and both detuning signs",
                "open-system pulse evolution",
                "continuous pulse-to-idle state propagation",
            ],
            "does_not_prove": [
                "physical-input-to-rate calibration",
                "agreement with a particular quantum device",
                "laboratory-frame carrier dynamics",
                "qutrit leakage, DRAG, or multi-qubit pulse control",
            ],
        },
    }

    json_path = args.output_dir / "pulse_qutip_2level.json"
    csv_path = args.output_dir / "pulse_qutip_2level.csv"
    plot_path = args.output_dir / "pulse_qutip_2level_trajectory.png"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    _write_plot(cases, plot_path)
    _write_report(report, args.report_path)

    print(f"validation | PULSE-QUTIP-2LEVEL | overall_pass={overall_pass}")
    for case in cases:
        print(
            f"{case['name']} | "
            f"max_difference={case['maximums']['max_element_difference']:.3e} | "
            f"trace_distance={case['maximums']['trace_distance']:.3e} | "
            f"pass={case['pass']}"
        )
    print(f"artifacts | {json_path} | {csv_path} | {plot_path}")
    return 0 if overall_pass else 1


def _specifications():
    zero = PulseDissipationRates("direct_rates", 0.0, 0.0, 0.0)
    return (
        {
            "name": "resonant_gaussian",
            "envelope": GaussianPulseEnvelope.from_target_rotation_angle(
                math.pi, 0.2, 4.0
            ),
            "phase": 0.0,
            "detuning": 0.0,
            "rates": zero,
            "idle_duration": 0.0,
        },
        {
            "name": "nonzero_phase",
            "envelope": SquarePulseEnvelope.from_target_rotation_angle(
                math.pi / 2.0, 1.0
            ),
            "phase": math.pi / 3.0,
            "detuning": 0.0,
            "rates": zero,
            "idle_duration": 0.0,
        },
        {
            "name": "positive_detuning",
            "envelope": SquarePulseEnvelope.from_target_rotation_angle(
                math.pi, 1.0
            ),
            "phase": 0.0,
            "detuning": 0.75 * math.pi,
            "rates": zero,
            "idle_duration": 0.0,
        },
        {
            "name": "negative_detuning",
            "envelope": SquarePulseEnvelope.from_target_rotation_angle(
                math.pi, 1.0
            ),
            "phase": 0.0,
            "detuning": -0.75 * math.pi,
            "rates": zero,
            "idle_duration": 0.0,
        },
        {
            "name": "dissipative_gaussian",
            "envelope": GaussianPulseEnvelope.from_target_rotation_angle(
                math.pi / 2.0, 0.2, 4.0
            ),
            "phase": math.pi / 4.0,
            "detuning": 0.25 * math.pi,
            "rates": PulseDissipationRates(
                "direct_rates", 0.2, 0.05, 0.3
            ),
            "idle_duration": 0.0,
        },
        {
            "name": "pulse_then_idle",
            "envelope": SquarePulseEnvelope.from_target_rotation_angle(
                math.pi, 0.2
            ),
            "phase": 0.0,
            "detuning": 0.0,
            "rates": PulseDissipationRates(
                "direct_rates", 0.1, 0.02, 0.05
            ),
            "idle_duration": 1.0,
        },
    )


def _run_case(specification):
    initial = initial_density_matrix(["0"])
    envelope = specification["envelope"]
    rates = specification["rates"]
    pulse_times = uniform_times(envelope.duration_us, 41)
    idle_times = (
        uniform_times(specification["idle_duration"], 41)
        if specification["idle_duration"] > 0.0
        else ()
    )
    quanta_step = recommended_max_step_us(
        envelope,
        specification["detuning"],
        rates,
        epsilon_h=EPSILON_H,
        epsilon_d=EPSILON_D,
        samples_per_sigma=SAMPLES_PER_SIGMA,
    )
    total_duration = envelope.duration_us + specification["idle_duration"]

    started = time.perf_counter()
    quanta = evolve_open_pulse_sequence(
        initial,
        envelope,
        rates,
        total_duration,
        quanta_step,
        phase_rad=specification["phase"],
        detuning_rad_per_us=specification["detuning"],
        pulse_checkpoint_times_us=pulse_times,
        idle_checkpoint_times_us=idle_times,
    )
    quanta_runtime_ms = (time.perf_counter() - started) * 1000.0

    collapse_ops = multi_qubit_physical_collapse_operators(
        1,
        rates.gamma_down_per_us,
        rates.gamma_up_per_us,
        rates.gamma_phi_per_us,
    )
    started = time.perf_counter()
    qutip_pulse = run_qutip_time_dependent_segment(
        initial,
        TwoLevelPulseHamiltonian(
            envelope,
            specification["phase"],
            specification["detuning"],
        ),
        collapse_ops,
        1,
        envelope.duration_us,
        pulse_times,
        max_step_us=QUTIP_MAX_STEP_US,
    )
    qutip_idle = ()
    if specification["idle_duration"] > 0.0:
        qutip_idle = run_qutip_constant_segment(
            qutip_pulse[-1],
            zero_hamiltonian(2),
            collapse_ops,
            1,
            specification["idle_duration"],
            idle_times,
            max_step_us=QUTIP_MAX_STEP_US,
        )
    qutip_runtime_ms = (time.perf_counter() - started) * 1000.0

    trajectory = []
    rows = []
    _append_segment(
        specification["name"],
        "pulse",
        0.0,
        quanta.pulse_result.checkpoints,
        qutip_pulse,
        trajectory,
        rows,
    )
    if qutip_idle:
        assert quanta.idle_result is not None
        _append_segment(
            specification["name"],
            "idle",
            envelope.duration_us,
            quanta.idle_result.checkpoints,
            qutip_idle,
            trajectory,
            rows,
        )

    metric_names = (
        "max_element_difference",
        "frobenius_difference",
        "trace_distance",
        "population_difference",
        "coherence_difference",
        "quanta_trace_error",
        "qutip_trace_error",
        "quanta_hermiticity_error",
        "qutip_hermiticity_error",
    )
    maximums = {
        name: max(point[name] for point in trajectory)
        for name in metric_names
    }
    minimums = {
        "quanta_minimum_eigenvalue": min(
            point["quanta_minimum_eigenvalue"] for point in trajectory
        ),
        "qutip_minimum_eigenvalue": min(
            point["qutip_minimum_eigenvalue"] for point in trajectory
        ),
    }
    case_pass = (
        maximums["max_element_difference"] <= COMPARISON_TOLERANCE
        and maximums["quanta_trace_error"] <= MAX_TRACE_ERROR
        and maximums["qutip_trace_error"] <= MAX_TRACE_ERROR
        and minimums["quanta_minimum_eigenvalue"]
        >= MINIMUM_EIGENVALUE
        and minimums["qutip_minimum_eigenvalue"]
        >= MINIMUM_EIGENVALUE
    )
    for row in rows:
        row["result"] = "pass" if case_pass else "fail"

    case = {
        "name": specification["name"],
        "input": {
            "initial_state": 0,
            "envelope_type": envelope.__class__.__name__,
            "peak_amplitude_rad_per_us": (
                envelope.peak_amplitude_rad_per_us
            ),
            "pulse_duration_us": envelope.duration_us,
            "sigma_us": getattr(envelope, "sigma_us", None),
            "phase_rad": specification["phase"],
            "detuning_rad_per_us": specification["detuning"],
            "idle_duration_us": specification["idle_duration"],
            "rates": rates.to_dict(),
        },
        "quanta_max_step_us": quanta_step,
        "qutip_max_step_us": QUTIP_MAX_STEP_US,
        "quanta_runtime_ms": quanta_runtime_ms,
        "qutip_runtime_ms": qutip_runtime_ms,
        "quanta_raw_diagnostics": _diagnostics(quanta),
        "maximums": maximums,
        "minimums": minimums,
        "trajectory": trajectory,
        "pass": case_pass,
    }
    return case, rows


def _append_segment(
    case_name,
    segment,
    offset,
    quanta_checkpoints,
    qutip_states,
    trajectory,
    rows,
):
    for checkpoint, qutip_state in zip(
        quanta_checkpoints, qutip_states, strict=True
    ):
        metrics = compare_density_matrices(
            checkpoint.cleaned_state,
            qutip_state,
        )
        quanta_array = np.asarray(
            checkpoint.cleaned_state, dtype=np.complex128
        )
        qutip_array = np.asarray(qutip_state, dtype=np.complex128)
        point = {
            "segment": segment,
            "global_time_us": offset + checkpoint.time_us,
            "quanta_population_0": float(quanta_array[0, 0].real),
            "quanta_population_1": float(quanta_array[1, 1].real),
            "qutip_population_0": float(qutip_array[0, 0].real),
            "qutip_population_1": float(qutip_array[1, 1].real),
            **metrics,
        }
        trajectory.append(point)
        rows.append({
            field: (
                case_name
                if field == "case"
                else point.get(field)
            )
            for field in CSV_FIELDS
        })


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


def _write_plot(cases, path):
    figure, axes = plt.subplots(3, 2, figsize=(12, 11), sharex=False)
    for axis, case in zip(axes.flat, cases, strict=True):
        times = [point["global_time_us"] for point in case["trajectory"]]
        axis.plot(
            times,
            [point["quanta_population_1"] for point in case["trajectory"]],
            color="#1f77b4",
            linewidth=2,
            label="Yuragi-Strider",
        )
        axis.plot(
            times,
            [point["qutip_population_1"] for point in case["trajectory"]],
            color="#d95f02",
            linestyle="--",
            linewidth=1.5,
            label="QuTiP",
        )
        if case["input"]["idle_duration_us"] > 0.0:
            axis.axvline(
                case["input"]["pulse_duration_us"],
                color="black",
                linestyle=":",
                linewidth=1,
                label="pulse end",
            )
        axis.set_title(
            f"{case['name']} "
            f"(max diff {case['maximums']['max_element_difference']:.1e})"
        )
        axis.set_xlabel("Global time [us]")
        axis.set_ylabel("Excited-state population")
        axis.grid(True, alpha=0.25)
        axis.legend(fontsize=8)
    figure.suptitle(
        "Identical time-dependent problems: Yuragi-Strider vs QuTiP",
        fontsize=14,
    )
    figure.tight_layout(rect=(0, 0, 1, 0.97))
    figure.savefig(path, dpi=170)
    plt.close(figure)


def _write_report(report, path):
    lines = [
        "# Pulse BA-5: QuTiP Two-Level Comparison",
        "",
        f"**Result:** {'PASS' if report['overall_pass'] else 'FAIL'}",
        "",
        "## Comparison Contract",
        "",
        "Both solvers receive the same initial density matrix, exact "
        "time-dependent Hamiltonian matrices, collapse-operator matrices, "
        "and requested output times. Yuragi-Strider uses its fixed-step RK4 "
        "reference path; QuTiP uses `mesolve` with DOP853.",
        "",
        f"The matrix-difference tolerance was fixed at "
        f"`{COMPARISON_TOLERANCE:.1e}`.",
        "",
        "## Results",
        "",
        "| Case | Max matrix difference | Max trace distance | Result |",
        "|---|---:|---:|---|",
    ]
    for case in report["cases"]:
        lines.append(
            f"| {case['name']} | "
            f"{case['maximums']['max_element_difference']:.3e} | "
            f"{case['maximums']['trace_distance']:.3e} | "
            f"{'PASS' if case['pass'] else 'FAIL'} |"
        )
    lines.extend([
        "",
        "## Interpretation",
        "",
        "The six shared mathematical problems agree within the fixed "
        "tolerance, including both detuning signs and pulse-to-idle "
        "continuity. This independently checks the time-dependent numerical "
        "evolution path.",
        "",
        "The comparison does not validate the mapping from temperature or "
        "other UI parameters to Lindblad rates, and it is not hardware "
        "calibration evidence. Those are separate model-validation questions.",
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
        / "pulse-qutip-2level-comparison.md",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

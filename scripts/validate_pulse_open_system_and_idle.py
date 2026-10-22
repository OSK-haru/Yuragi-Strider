"""Generate BA-4 open-system pulse and post-pulse idle artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import platform
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._common import git_commit
from core.gates import initial_density_matrix
from core.pulse_envelopes import (
    GaussianPulseEnvelope,
    SquarePulseEnvelope,
)
from core.pulse_open_system import (
    PulseDissipationRates,
    evolve_open_pulse_sequence,
    pulse_dissipation_rates,
)
from core.results import EnvironmentConfig
from validation_pulse.pulse_analytic import (
    analytic_resonant_x_density,
    matrix_error_metrics,
    pure_target_fidelity,
)
from validation_pulse.pulse_phase_detuning import (
    analytic_constant_drive_density,
)
from validation_pulse.time_grid import uniform_times


ZERO_RATE_TOLERANCE = 2e-8
MODE_MATCH_TOLERANCE = 1e-14
MAX_TRACE_ERROR = 1e-12
MAX_HERMITICITY_ERROR = 1e-12
MINIMUM_RAW_EIGENVALUE = -1e-10
MAX_CLEANUP_CORRECTION = 1e-12
MINIMUM_VISIBLE_EFFECT = 0.01

CSV_FIELDS = [
    "case",
    "segment",
    "global_time_us",
    "local_time_us",
    "gamma_down_per_us",
    "gamma_up_per_us",
    "gamma_phi_per_us",
    "population_0",
    "population_1",
    "coherence_abs",
    "fidelity_to_closed_pulse_trajectory",
    "fidelity_to_target",
    "raw_trace_error",
    "raw_hermiticity_error",
    "raw_minimum_eigenvalue",
    "cleanup_correction_norm",
    "result",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    cases, rows = _run_required_cases()
    zero_rate = _run_zero_rate_audit()
    mode_match = _run_mode_match_audit()
    effect_audit = _run_effect_audit(cases)
    physicality = _physicality_audit(cases)
    overall_pass = (
        all(case["pass"] for case in cases)
        and zero_rate["pass"]
        and mode_match["pass"]
        and effect_audit["pass"]
        and physicality["pass"]
    )
    for row in rows:
        row["result"] = "pass" if overall_pass else "fail"

    report = {
        "validation": "PULSE-BA4",
        "base_git_commit": git_commit(ROOT),
        "python_version": platform.python_version(),
        "numpy_version": np.__version__,
        "model_id": "driven_two_level_rwa_experimental_v1",
        "frame": "rotating",
        "approximation": "RWA",
        "environment_model": "existing two-level Lindblad environment",
        "collapse_operator_convention": {
            "down": "sqrt(gamma_down) sigma_minus",
            "up": "sqrt(gamma_up) sigma_plus",
            "dephasing": "sqrt(gamma_phi / 2) sigma_z",
        },
        "segment_semantics": (
            "pulse: H(t)+D; idle: H=0+D; state is continuous at boundary"
        ),
        "fidelity_definitions": {
            "fidelity_to_closed_pulse_trajectory": (
                "Tr(rho_open(t) rho_closed(t)); closed reference is pure"
            ),
            "final_state_fidelity_to_target": (
                "Tr(rho_final rho_requested_target); target is pure"
            ),
        },
        "tolerances": {
            "zero_rate_max_element_error": ZERO_RATE_TOLERANCE,
            "physical_direct_mode_match_error": MODE_MATCH_TOLERANCE,
            "raw_trace_error": MAX_TRACE_ERROR,
            "raw_hermiticity_error": MAX_HERMITICITY_ERROR,
            "raw_minimum_eigenvalue": MINIMUM_RAW_EIGENVALUE,
            "cleanup_correction_norm": MAX_CLEANUP_CORRECTION,
            "minimum_visible_environment_effect": MINIMUM_VISIBLE_EFFECT,
        },
        "cases": cases,
        "zero_rate_limit": zero_rate,
        "physical_direct_rate_match": mode_match,
        "environment_effect_audit": effect_audit,
        "raw_physicality_audit": physicality,
        "overall_pass": overall_pass,
        "scope_and_limitations": {
            "proves": [
                "tested dissipation during square and Gaussian drives",
                "tested the same dissipation rates during post-pulse idle",
                "tested finite-temperature upward transitions in both segments",
                "tested equivalent physical and direct-rate inputs",
                "tested the zero-rate limit against BA-2 and BA-3 references",
                "audited raw physicality before per-step cleanup",
            ],
            "does_not_prove": [
                "strict finite-step CPTP behavior",
                "driven thermal steady-state formulas",
                "non-Markovian dynamics",
                "qutrit transition-specific dissipation",
                "hardware-calibrated pulse reproduction",
            ],
        },
    }

    json_path = args.output_dir / "pulse_ba4_open_system_idle.json"
    csv_path = args.output_dir / "pulse_ba4_open_system_idle.csv"
    trajectory_plot = (
        args.output_dir / "pulse_open_system_drive_idle_trajectories.png"
    )
    fidelity_plot = (
        args.output_dir / "pulse_open_system_segment_fidelity.png"
    )
    physicality_plot = (
        args.output_dir / "pulse_open_system_raw_physicality.png"
    )

    json_path.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    _write_trajectory_plot(cases, trajectory_plot)
    _write_fidelity_plot(cases, fidelity_plot)
    _write_physicality_plot(cases, physicality_plot)
    _write_report(report, args.report_path)

    print(f"validation | PULSE-BA4 | overall_pass={overall_pass}")
    for case in cases:
        print(
            f"{case['name']} | "
            f"pulse_fidelity={case['pulse_end']['fidelity_to_closed_pulse_trajectory']:.6f} | "
            f"final_target_fidelity={case['final']['fidelity_to_target']:.6f} | "
            f"pass={case['pass']}"
        )
    print(
        "zero_rate | "
        f"max_error={zero_rate['maximum_error']:.6e} | "
        f"pass={zero_rate['pass']}"
    )
    print(
        "mode_match | "
        f"max_error={mode_match['maximum_error']:.6e} | "
        f"pass={mode_match['pass']}"
    )
    print(
        f"artifacts | {json_path} | {csv_path} | {trajectory_plot} | "
        f"{fidelity_plot} | {physicality_plot}"
    )
    return 0 if overall_pass else 1


def _run_required_cases():
    specifications = (
        {
            "name": "square_relaxation",
            "description": "resonant X-pi square pulse with downward relaxation",
            "envelope": SquarePulseEnvelope.from_target_rotation_angle(
                math.pi,
                1.0,
            ),
            "rates": PulseDissipationRates(
                "direct_rates",
                0.8,
                0.0,
                0.0,
            ),
            "total_duration_us": 4.0,
            "max_step_us": 0.005,
            "target_angle_rad": math.pi,
        },
        {
            "name": "gaussian_dephasing",
            "description": "resonant Gaussian X-pi/2 pulse with pure dephasing",
            "envelope": GaussianPulseEnvelope.from_target_rotation_angle(
                math.pi / 2.0,
                0.2,
                4.0,
            ),
            "rates": PulseDissipationRates(
                "direct_rates",
                0.0,
                0.0,
                1.0,
            ),
            "total_duration_us": 3.6,
            "max_step_us": 0.005,
            "target_angle_rad": math.pi / 2.0,
        },
        {
            "name": "finite_temperature_excitation",
            "description": "small resonant drive with upward and downward transitions",
            "envelope": SquarePulseEnvelope.from_target_rotation_angle(
                0.1,
                0.5,
            ),
            "rates": PulseDissipationRates(
                "direct_rates",
                0.3,
                0.2,
                0.0,
            ),
            "total_duration_us": 3.0,
            "max_step_us": 0.005,
            "target_angle_rad": 0.1,
        },
        {
            "name": "long_idle_relaxation",
            "description": "short X-pi pulse followed by a long relaxation idle",
            "envelope": SquarePulseEnvelope.from_target_rotation_angle(
                math.pi,
                0.2,
            ),
            "rates": PulseDissipationRates(
                "direct_rates",
                0.1,
                0.0,
                0.0,
            ),
            "total_duration_us": 20.2,
            "max_step_us": 0.01,
            "target_angle_rad": math.pi,
        },
    )
    cases = []
    rows = []
    for specification in specifications:
        case, case_rows = _run_case(specification)
        cases.append(case)
        rows.extend(case_rows)
    return cases, rows


def _run_case(specification):
    initial = initial_density_matrix(["0"])
    envelope = specification["envelope"]
    pulse_times = uniform_times(envelope.duration_us, 81)
    idle_duration = (
        specification["total_duration_us"] - envelope.duration_us
    )
    idle_times = uniform_times(idle_duration, 101)
    zero_rates = PulseDissipationRates(
        "direct_rates",
        0.0,
        0.0,
        0.0,
    )
    started = time.perf_counter()
    open_result = evolve_open_pulse_sequence(
        initial,
        envelope,
        specification["rates"],
        specification["total_duration_us"],
        specification["max_step_us"],
        pulse_checkpoint_times_us=pulse_times,
        idle_checkpoint_times_us=idle_times,
    )
    closed_result = evolve_open_pulse_sequence(
        initial,
        envelope,
        zero_rates,
        specification["total_duration_us"],
        specification["max_step_us"],
        pulse_checkpoint_times_us=pulse_times,
        idle_checkpoint_times_us=idle_times,
    )
    runtime_ms = (time.perf_counter() - started) * 1000.0
    target = analytic_resonant_x_density(
        envelope,
        envelope.duration_us,
    )
    snapshots = []
    snapshots.extend(_segment_rows(
        specification["name"],
        "pulse",
        0.0,
        open_result.pulse_result.checkpoints,
        closed_result.pulse_result.checkpoints,
        specification["rates"],
        target,
    ))
    assert open_result.idle_result is not None
    assert closed_result.idle_result is not None
    idle_rows = _segment_rows(
        specification["name"],
        "idle",
        envelope.duration_us,
        open_result.idle_result.checkpoints,
        closed_result.idle_result.checkpoints,
        specification["rates"],
        target,
    )
    snapshots.extend(idle_rows[1:])

    pulse_end = snapshots[len(pulse_times) - 1]
    final = snapshots[-1]
    diagnostics = [
        open_result.pulse_result.diagnostics,
        open_result.idle_result.diagnostics,
    ]
    physicality_pass = _diagnostics_pass(diagnostics)
    case = {
        "name": specification["name"],
        "description": specification["description"],
        "rates": specification["rates"].to_dict(),
        "collapse_operator_count": (
            specification["rates"].collapse_operator_count
        ),
        "pulse_duration_us": envelope.duration_us,
        "idle_duration_us": idle_duration,
        "total_duration_us": specification["total_duration_us"],
        "runtime_ms": runtime_ms,
        "pulse_internal_step_count": (
            open_result.pulse_result.diagnostics.internal_step_count
        ),
        "idle_internal_step_count": (
            open_result.idle_result.diagnostics.internal_step_count
        ),
        "pulse_end": _summary_snapshot(pulse_end),
        "final": _summary_snapshot(final),
        "max_raw_trace_error": max(
            diagnostic.raw_trace_error for diagnostic in diagnostics
        ),
        "max_raw_hermiticity_error": max(
            diagnostic.raw_hermiticity_error
            for diagnostic in diagnostics
        ),
        "minimum_raw_eigenvalue": min(
            diagnostic.raw_minimum_eigenvalue
            for diagnostic in diagnostics
        ),
        "max_cleanup_correction_norm": max(
            diagnostic.cleanup_correction_norm
            for diagnostic in diagnostics
        ),
        "snapshots": snapshots,
        "pass": physicality_pass,
    }
    return case, snapshots


def _segment_rows(
    case_name,
    segment,
    time_offset,
    open_checkpoints,
    closed_checkpoints,
    rates,
    target,
):
    rows = []
    for open_checkpoint, closed_checkpoint in zip(
        open_checkpoints,
        closed_checkpoints,
        strict=True,
    ):
        state = open_checkpoint.cleaned_state
        rows.append({
            "case": case_name,
            "segment": segment,
            "global_time_us": time_offset + open_checkpoint.time_us,
            "local_time_us": open_checkpoint.time_us,
            "gamma_down_per_us": rates.gamma_down_per_us,
            "gamma_up_per_us": rates.gamma_up_per_us,
            "gamma_phi_per_us": rates.gamma_phi_per_us,
            "population_0": state[0][0].real,
            "population_1": state[1][1].real,
            "coherence_abs": abs(state[0][1]),
            "fidelity_to_closed_pulse_trajectory": (
                pure_target_fidelity(
                    state,
                    closed_checkpoint.cleaned_state,
                )
            ),
            "fidelity_to_target": pure_target_fidelity(state, target),
            "raw_trace_error": (
                open_checkpoint.raw_physicality.trace_error
            ),
            "raw_hermiticity_error": (
                open_checkpoint.raw_physicality.hermiticity_error
            ),
            "raw_minimum_eigenvalue": (
                open_checkpoint.raw_physicality.minimum_eigenvalue
            ),
            "cleanup_correction_norm": (
                open_checkpoint.cleanup_correction_norm
            ),
            "result": "pass",
        })
    return rows


def _run_zero_rate_audit():
    initial = initial_density_matrix(["0"])
    zero_rates = PulseDissipationRates(
        "direct_rates",
        0.0,
        0.0,
        0.0,
    )
    square = SquarePulseEnvelope(
        peak_amplitude_rad_per_us=math.pi,
        duration_us=1.0,
    )
    square_result = evolve_open_pulse_sequence(
        initial,
        square,
        zero_rates,
        1.0,
        0.005,
        phase_rad=math.pi / 2.0,
        detuning_rad_per_us=0.3 * math.pi,
    )
    square_expected = analytic_constant_drive_density(
        initial,
        math.pi,
        math.pi / 2.0,
        0.3 * math.pi,
        1.0,
    )
    gaussian = GaussianPulseEnvelope.from_target_rotation_angle(
        math.pi,
        0.2,
        4.0,
    )
    gaussian_result = evolve_open_pulse_sequence(
        initial,
        gaussian,
        zero_rates,
        gaussian.duration_us,
        0.005,
    )
    gaussian_expected = analytic_resonant_x_density(
        gaussian,
        gaussian.duration_us,
    )
    records = [
        {
            "case": "detuned_y_axis_square",
            **matrix_error_metrics(
                square_result.final_state,
                square_expected,
            ),
        },
        {
            "case": "resonant_gaussian_x_pi",
            **matrix_error_metrics(
                gaussian_result.final_state,
                gaussian_expected,
            ),
        },
    ]
    maximum_error = max(
        record["max_element_error"] for record in records
    )
    return {
        "cases": records,
        "maximum_error": maximum_error,
        "pass": maximum_error <= ZERO_RATE_TOLERANCE,
    }


def _run_mode_match_audit():
    environment = EnvironmentConfig(
        input_mode="physical",
        device_quality=0.8,
        temperature_mk=100.0,
        flux_noise_phi0=2e-6,
        qubit_frequency_ghz=5.0,
        t1_max_us=100.0,
        tphi_max_us=100.0,
    )
    physical_rates = pulse_dissipation_rates(environment)
    direct_rates = pulse_dissipation_rates(SimpleNamespace(
        input_mode="direct_rates",
        gamma_down_per_us=physical_rates.gamma_down_per_us,
        gamma_up_per_us=physical_rates.gamma_up_per_us,
        gamma_phi_per_us=physical_rates.gamma_phi_per_us,
    ))
    envelope = SquarePulseEnvelope.from_target_rotation_angle(
        math.pi / 2.0,
        1.0,
    )
    initial = initial_density_matrix(["0"])
    physical = evolve_open_pulse_sequence(
        initial,
        envelope,
        physical_rates,
        3.0,
        0.005,
    )
    direct = evolve_open_pulse_sequence(
        initial,
        envelope,
        direct_rates,
        3.0,
        0.005,
    )
    pulse_error = matrix_error_metrics(
        physical.pulse_end_state,
        direct.pulse_end_state,
    )["max_element_error"]
    final_error = matrix_error_metrics(
        physical.final_state,
        direct.final_state,
    )["max_element_error"]
    maximum_error = max(pulse_error, final_error)
    return {
        "physical_input": {
            **physical_rates.to_dict(),
            "device_quality": environment.device_quality,
            "temperature_mk": environment.temperature_mk,
            "flux_noise_phi0": environment.flux_noise_phi0,
            "qubit_frequency_ghz": environment.qubit_frequency_ghz,
            "t1_max_us": environment.t1_max_us,
            "tphi_max_us": environment.tphi_max_us,
        },
        "direct_rates": direct_rates.to_dict(),
        "pulse_end_max_element_error": pulse_error,
        "final_max_element_error": final_error,
        "maximum_error": maximum_error,
        "pass": maximum_error <= MODE_MATCH_TOLERANCE,
    }


def _run_effect_audit(cases):
    by_name = {case["name"]: case for case in cases}
    square = by_name["square_relaxation"]
    gaussian = by_name["gaussian_dephasing"]
    excitation = by_name["finite_temperature_excitation"]
    long_idle = by_name["long_idle_relaxation"]

    initial = initial_density_matrix(["0"])
    excitation_envelope = SquarePulseEnvelope.from_target_rotation_angle(
        0.1,
        0.5,
    )
    no_up = evolve_open_pulse_sequence(
        initial,
        excitation_envelope,
        PulseDissipationRates("direct_rates", 0.3, 0.0, 0.0),
        3.0,
        0.005,
    )
    excitation_pulse_delta = (
        excitation["pulse_end"]["population_1"]
        - no_up.pulse_end_state[1][1].real
    )
    excitation_idle_delta = (
        excitation["final"]["population_1"]
        - excitation["pulse_end"]["population_1"]
    )
    checks = {
        "square_drive_degradation": (
            1.0
            - square["pulse_end"][
                "fidelity_to_closed_pulse_trajectory"
            ]
        ),
        "gaussian_drive_degradation": (
            1.0
            - gaussian["pulse_end"][
                "fidelity_to_closed_pulse_trajectory"
            ]
        ),
        "excitation_pulse_population_delta_vs_no_up": (
            excitation_pulse_delta
        ),
        "excitation_idle_population_delta": excitation_idle_delta,
        "long_idle_target_fidelity_drop": (
            long_idle["pulse_end"]["fidelity_to_target"]
            - long_idle["final"]["fidelity_to_target"]
        ),
    }
    return {
        **checks,
        "pass": all(
            value >= MINIMUM_VISIBLE_EFFECT
            for value in checks.values()
        ),
    }


def _physicality_audit(cases):
    maxima = {
        "max_raw_trace_error": max(
            case["max_raw_trace_error"] for case in cases
        ),
        "max_raw_hermiticity_error": max(
            case["max_raw_hermiticity_error"] for case in cases
        ),
        "minimum_raw_eigenvalue": min(
            case["minimum_raw_eigenvalue"] for case in cases
        ),
        "max_cleanup_correction_norm": max(
            case["max_cleanup_correction_norm"] for case in cases
        ),
    }
    return {
        **maxima,
        "pass": (
            maxima["max_raw_trace_error"] <= MAX_TRACE_ERROR
            and maxima["max_raw_hermiticity_error"]
            <= MAX_HERMITICITY_ERROR
            and maxima["minimum_raw_eigenvalue"]
            >= MINIMUM_RAW_EIGENVALUE
            and maxima["max_cleanup_correction_norm"]
            <= MAX_CLEANUP_CORRECTION
        ),
    }


def _diagnostics_pass(diagnostics) -> bool:
    return all(
        diagnostic.raw_trace_error <= MAX_TRACE_ERROR
        and diagnostic.raw_hermiticity_error <= MAX_HERMITICITY_ERROR
        and diagnostic.raw_minimum_eigenvalue >= MINIMUM_RAW_EIGENVALUE
        and diagnostic.cleanup_correction_norm <= MAX_CLEANUP_CORRECTION
        for diagnostic in diagnostics
    )


def _summary_snapshot(snapshot):
    return {
        "global_time_us": snapshot["global_time_us"],
        "population_0": snapshot["population_0"],
        "population_1": snapshot["population_1"],
        "coherence_abs": snapshot["coherence_abs"],
        "fidelity_to_closed_pulse_trajectory": (
            snapshot["fidelity_to_closed_pulse_trajectory"]
        ),
        "fidelity_to_target": snapshot["fidelity_to_target"],
    }


def _write_trajectory_plot(cases, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(
        len(cases),
        2,
        figsize=(12, 3.2 * len(cases)),
        squeeze=False,
    )
    for row_index, case in enumerate(cases):
        times = [
            snapshot["global_time_us"] for snapshot in case["snapshots"]
        ]
        pulse_end = case["pulse_duration_us"]
        axes[row_index][0].plot(
            times,
            [snapshot["population_1"] for snapshot in case["snapshots"]],
            label="P(|1>)",
        )
        axes[row_index][0].axvline(
            pulse_end,
            color="tab:gray",
            linestyle="--",
            label="pulse end",
        )
        axes[row_index][0].set_ylabel(case["name"])
        axes[row_index][0].set_ylim(-0.03, 1.03)
        axes[row_index][0].grid(True, alpha=0.3)
        axes[row_index][0].legend()

        axes[row_index][1].plot(
            times,
            [
                snapshot["fidelity_to_closed_pulse_trajectory"]
                for snapshot in case["snapshots"]
            ],
            label="fidelity to closed trajectory",
        )
        axes[row_index][1].plot(
            times,
            [
                snapshot["fidelity_to_target"]
                for snapshot in case["snapshots"]
            ],
            label="fidelity to target",
        )
        axes[row_index][1].axvline(
            pulse_end,
            color="tab:gray",
            linestyle="--",
        )
        axes[row_index][1].set_ylim(-0.03, 1.03)
        axes[row_index][1].grid(True, alpha=0.3)
        axes[row_index][1].legend()
    axes[-1][0].set_xlabel("global time [us]")
    axes[-1][1].set_xlabel("global time [us]")
    figure.suptitle(
        "Actual calculation result: driven and idle open-system trajectories"
    )
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_fidelity_plot(cases, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = [case["name"] for case in cases]
    positions = np.arange(len(names))
    width = 0.35
    figure, axis = plt.subplots(figsize=(11, 5))
    axis.bar(
        positions - width / 2.0,
        [
            case["pulse_end"]["fidelity_to_target"]
            for case in cases
        ],
        width,
        label="pulse-end target fidelity",
    )
    axis.bar(
        positions + width / 2.0,
        [case["final"]["fidelity_to_target"] for case in cases],
        width,
        label="final target fidelity",
    )
    axis.set_xticks(positions, names, rotation=15, ha="right")
    axis.set_ylim(0.0, 1.05)
    axis.set_ylabel("fidelity")
    axis.set_title(
        "Actual calculation result: pulse-end and final target fidelity"
    )
    axis.grid(True, axis="y", alpha=0.3)
    axis.legend()
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_physicality_plot(cases, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = [case["name"] for case in cases]
    metrics = (
        (
            "trace error",
            [max(case["max_raw_trace_error"], 1e-18) for case in cases],
        ),
        (
            "Hermiticity error",
            [
                max(case["max_raw_hermiticity_error"], 1e-18)
                for case in cases
            ],
        ),
        (
            "cleanup correction",
            [
                max(case["max_cleanup_correction_norm"], 1e-18)
                for case in cases
            ],
        ),
    )
    figure, axes = plt.subplots(3, 1, figsize=(10, 10), sharex=True)
    for axis, (label, values) in zip(axes, metrics, strict=True):
        axis.bar(names, values)
        axis.set_yscale("log")
        axis.set_ylabel(label)
        axis.grid(True, axis="y", which="both", alpha=0.3)
    axes[-1].tick_params(axis="x", rotation=15)
    figure.suptitle(
        "Actual calculation result: raw physicality and cleanup audit"
    )
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_report(report, path: Path) -> None:
    lines = [
        "# PULSE-BA4: Open-System Pulse and Post-Pulse Idle",
        "",
        "## Result",
        "",
        f"- Overall pass: `{report['overall_pass']}`",
        f"- Model: `{report['model_id']}`",
        f"- Frame / approximation: `{report['frame']}` / `{report['approximation']}`",
        "",
        "## Required Cases",
        "",
        "| Case | Collapse ops | Pulse-end closed fidelity | Pulse-end target fidelity | Final target fidelity | Pass |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for case in report["cases"]:
        lines.append(
            f"| {case['name']} | {case['collapse_operator_count']} | "
            f"{case['pulse_end']['fidelity_to_closed_pulse_trajectory']:.8f} | "
            f"{case['pulse_end']['fidelity_to_target']:.8f} | "
            f"{case['final']['fidelity_to_target']:.8f} | "
            f"{case['pass']} |"
        )
    lines.extend([
        "",
        "The two pulse-end fidelity columns coincide in these resonant fixtures because the closed pulse endpoint is the requested target. They are computed and labeled separately; they need not coincide for a mismatched target or other control setting.",
    ])
    zero_rate = report["zero_rate_limit"]
    mode_match = report["physical_direct_rate_match"]
    effect = report["environment_effect_audit"]
    physicality = report["raw_physicality_audit"]
    lines.extend([
        "",
        "## Environment Effects",
        "",
        f"- Square-drive degradation: `{effect['square_drive_degradation']:.6e}`",
        f"- Gaussian-drive degradation: `{effect['gaussian_drive_degradation']:.6e}`",
        f"- Excitation pulse population delta versus gamma-up=0: `{effect['excitation_pulse_population_delta_vs_no_up']:.6e}`",
        f"- Excitation idle population delta: `{effect['excitation_idle_population_delta']:.6e}`",
        f"- Long-idle target-fidelity drop: `{effect['long_idle_target_fidelity_drop']:.6e}`",
        f"- Pass: `{effect['pass']}`",
        "",
        "## Zero-Rate Limit",
        "",
        f"- Maximum density-matrix element error: `{zero_rate['maximum_error']:.6e}`",
        f"- Pass: `{zero_rate['pass']}`",
        "",
        "## Physical And Direct-Rate Equivalence",
        "",
        f"- Pulse-end error: `{mode_match['pulse_end_max_element_error']:.6e}`",
        f"- Final error: `{mode_match['final_max_element_error']:.6e}`",
        f"- Pass: `{mode_match['pass']}`",
        "",
        "## Raw Physicality",
        "",
        f"- Maximum raw trace error: `{physicality['max_raw_trace_error']:.6e}`",
        f"- Maximum raw Hermiticity error: `{physicality['max_raw_hermiticity_error']:.6e}`",
        f"- Minimum raw eigenvalue: `{physicality['minimum_raw_eigenvalue']:.6e}`",
        f"- Maximum cleanup correction: `{physicality['max_cleanup_correction_norm']:.6e}`",
        f"- Pass: `{physicality['pass']}`",
        "",
        "## Interpretation",
        "",
        "Dissipation is active during both the finite-duration drive and the zero-H idle segment. Fidelity to the closed pulse trajectory and fidelity to the requested target are reported separately because they answer different questions.",
        "",
        "The finite-temperature fixture is compared with an otherwise identical gamma-up=0 run. It is not compared with the undriven thermal-equilibrium formula during the drive.",
        "",
        "This phase does not establish strict finite-step CPTP behavior, driven steady-state formulas, non-Markovian dynamics, qutrit leakage, DRAG, or calibrated hardware behavior.",
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
        default=(
            ROOT
            / "docs"
            / "validation"
            / "pulse-ba4-open-system-and-idle.md"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

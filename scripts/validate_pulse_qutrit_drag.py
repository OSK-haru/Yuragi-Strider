"""Generate Pulse Extension B-4 DRAG validation artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._common import git_commit  # noqa: E402
from validation_pulse.qutrit_drag import (  # noqa: E402
    run_qutrit_drag_validation,
)


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    report, rows = run_qutrit_drag_validation()
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["git_commit"] = git_commit(ROOT)
    report["software_versions"] = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "matplotlib": matplotlib.__version__,
    }

    json_path = args.output_dir / "pulse_b_drag.json"
    csv_path = args.output_dir / "pulse_b_drag.csv"
    leakage_path = args.output_dir / "pulse_b_drag_leakage_sweep.png"
    fidelity_path = args.output_dir / "pulse_b_drag_fidelity_phase.png"
    convergence_path = args.output_dir / "pulse_b_drag_convergence.png"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(csv_path, rows)
    _write_leakage_plot(leakage_path, rows)
    _write_fidelity_phase_plot(fidelity_path, rows)
    _write_convergence_plot(convergence_path, rows)
    _write_report(args.report_path, report)

    print(f"overall_pass={report['overall_pass']}")
    print(f"json={json_path}")
    print(f"csv={csv_path}")
    print(f"report={args.report_path}")
    return 0 if report["overall_pass"] else 1


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _sweep_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (
            row for row in rows
            if row.get("case") == "beta_sweep"
        ),
        key=lambda row: row["drag_beta_us"],
    )


def _write_leakage_plot(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    sweep = _sweep_rows(rows)
    figure, axis = plt.subplots(figsize=(8.5, 5.2))
    axis.plot(
        [row["drag_beta_us"] for row in sweep],
        [row["maximum_recorded_leakage"] for row in sweep],
        marker="o",
        label="maximum recorded leakage",
    )
    axis.plot(
        [row["drag_beta_us"] for row in sweep],
        [row["end_leakage"] for row in sweep],
        marker="s",
        label="pulse-end leakage",
    )
    axis.axvline(0.001, color="tab:green", linestyle="--", label="selected beta")
    axis.set_xlabel("DRAG beta [us]")
    axis.set_ylabel("Leakage probability")
    axis.set_title("B-4 Gaussian DRAG beta sweep")
    axis.grid(True, alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_fidelity_phase_plot(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    sweep = _sweep_rows(rows)
    betas = [row["drag_beta_us"] for row in sweep]
    figure, fidelity_axis = plt.subplots(figsize=(8.5, 5.2))
    phase_axis = fidelity_axis.twinx()
    fidelity_line = fidelity_axis.plot(
        betas,
        [row["target_fidelity"] for row in sweep],
        color="tab:blue",
        marker="o",
        label="target fidelity",
    )
    phase_line = phase_axis.plot(
        betas,
        [row["phase_error_rad"] for row in sweep],
        color="tab:orange",
        marker="s",
        label="phase error",
    )
    fidelity_axis.axvline(0.001, color="tab:green", linestyle="--")
    fidelity_axis.set_xlabel("DRAG beta [us]")
    fidelity_axis.set_ylabel("Target-state fidelity", color="tab:blue")
    phase_axis.set_ylabel("Computational phase error [rad]", color="tab:orange")
    fidelity_axis.set_title("B-4 fidelity and phase guard")
    fidelity_axis.grid(True, alpha=0.25)
    fidelity_axis.legend(
        fidelity_line + phase_line,
        [line.get_label() for line in fidelity_line + phase_line],
        loc="lower right",
    )
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_convergence_plot(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    figure, axis = plt.subplots(figsize=(8.5, 5.2))
    for case, label in (
        ("drag_off", "DRAG off"),
        ("drag_on", "DRAG on"),
    ):
        points = sorted(
            (
                row for row in rows
                if row.get("row_type") == "convergence"
                and row.get("case") == case
            ),
            key=lambda row: row["requested_internal_step_cap_us"],
        )
        axis.loglog(
            [row["requested_internal_step_cap_us"] for row in points],
            [
                max(row["matrix_max_element_error"], 1e-16)
                for row in points
            ],
            marker="o",
            label=label,
        )
    axis.set_xlabel("Requested internal step cap [us]")
    axis.set_ylabel("Final density-matrix max error")
    axis.set_title("B-4 DRAG on/off fixed-step convergence")
    axis.grid(True, which="both", alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    derivative = _case(report, "analytic_gaussian_derivative")
    boundary = _case(report, "truncated_gaussian_boundary")
    primary = _case(
        report,
        "fixed_pi_leakage_and_fidelity_improvement",
    )
    secondary = _case(report, "pi_over_two_fidelity_phase_guard")
    convergence = _case(report, "drag_on_off_refinement")
    lines = [
        "# Pulse Extension B-4: Gaussian DRAG Control",
        "",
        f"**Status:** {'PASS' if report['overall_pass'] else 'FAIL'}",
        "",
        "## Fixed Convention",
        "",
        "$$",
        "\\Omega_x(t)=\\Omega(t),",
        "\\qquad",
        "\\Omega_y(t)=\\beta\\frac{d\\Omega(t)}{dt}.",
        "$$",
        "",
        "`drag_beta_us` is measured in microseconds. Positive quadrature is",
        "+90 degrees from the in-phase axis after applying `phase_rad`.",
        "The validated fixture uses `beta = 0.001 us`; this is not claimed",
        "to be a universal optimum.",
        "",
        "## Derivative And Boundary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        (
            "| Maximum finite-difference absolute error | "
            f"`{derivative['maximum_absolute_error']:.6e}` |"
        ),
        (
            "| Maximum finite-difference relative error | "
            f"`{derivative['maximum_relative_error']:.6e}` |"
        ),
        (
            "| Endpoint amplitude | "
            f"`{boundary['start_amplitude_rad_per_us']:.6e} rad/us` |"
        ),
        (
            "| Start derivative | "
            f"`{boundary['start_derivative_rad_per_us2']:.6e} rad/us^2` |"
        ),
        (
            "| End derivative | "
            f"`{boundary['end_derivative_rad_per_us2']:.6e} rad/us^2` |"
        ),
        "",
        "The analytic Gaussian and derivative are evaluated at both",
        "endpoints. Both are zero strictly outside the support. This retains",
        "the Baseline A hard cutoff; no smooth-edge pulse was introduced.",
        "",
        "## Fixed Pi Pulse",
        "",
        "| Metric | Beta 0 | Beta 0.001 us |",
        "|---|---:|---:|",
        (
            "| Maximum recorded leakage | "
            f"`{primary['baseline']['maximum_recorded_leakage']:.6f}` | "
            f"`{primary['drag']['maximum_recorded_leakage']:.6f}` |"
        ),
        (
            "| Pulse-end leakage | "
            f"`{primary['baseline']['end_leakage']:.6f}` | "
            f"`{primary['drag']['end_leakage']:.6f}` |"
        ),
        (
            "| Target fidelity | "
            f"`{primary['baseline']['target_fidelity']:.6f}` | "
            f"`{primary['drag']['target_fidelity']:.6f}` |"
        ),
        (
            "| Computational population | "
            f"`{primary['baseline']['computational_population']:.6f}` | "
            f"`{primary['drag']['computational_population']:.6f}` |"
        ),
        "",
        (
            "The pulse-end leakage ratio is "
            f"`{primary['end_leakage_ratio']:.6f}`."
        ),
        "",
        "## Pi/2 Fidelity And Phase Guard",
        "",
        "| Metric | Beta 0 | Beta 0.001 us |",
        "|---|---:|---:|",
        (
            "| Pulse-end leakage | "
            f"`{secondary['baseline']['end_leakage']:.6f}` | "
            f"`{secondary['drag']['end_leakage']:.6f}` |"
        ),
        (
            "| Target fidelity | "
            f"`{secondary['baseline']['target_fidelity']:.6f}` | "
            f"`{secondary['drag']['target_fidelity']:.6f}` |"
        ),
        (
            "| Phase error [rad] | "
            f"`{secondary['baseline']['phase_error_rad']:.6f}` | "
            f"`{secondary['drag']['phase_error_rad']:.6f}` |"
        ),
        (
            "| Computational population | "
            f"`{secondary['baseline']['computational_population']:.6f}` | "
            f"`{secondary['drag']['computational_population']:.6f}` |"
        ),
        "",
        "## Convergence",
        "",
        "| Mode | Policy matrix error | Observed orders |",
        "|---|---:|---|",
    ]
    for mode in convergence["modes"]:
        errors = mode["matrix_errors"]
        orders = ", ".join(
            "n/a" if value is None else f"{value:.3f}"
            for value in mode["observed_orders"]
        )
        lines.append(
            f"| {mode['mode']} | `{errors[2]:.6e}` | {orders} |"
        )
    lines.extend([
        "",
        "Both DRAG on and off are approximately fourth order for the fixed",
        "fixture. The endpoint discontinuity remains documented and no",
        "universal smooth-pulse convergence claim is made.",
        "",
        "## Scope Boundary",
        "",
        "- The improvement applies only to the fixed tested conditions.",
        "- The selected beta is not a hardware calibration.",
        "- Baseline A still rejects nonzero DRAG.",
        "- Qutrit HTTP execution remains `contract_only` until B-5.",
        "- Strict finite-step CPTP behavior is not established.",
        "",
        "## Artifacts",
        "",
        "```text",
        "validation_results/pulse_b_drag.json",
        "validation_results/pulse_b_drag.csv",
        "validation_results/pulse_b_drag_leakage_sweep.png",
        "validation_results/pulse_b_drag_fidelity_phase.png",
        "validation_results/pulse_b_drag_convergence.png",
        "```",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _case(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(case for case in report["cases"] if case["name"] == name)


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
        default=ROOT / "docs" / "validation" / "pulse-b-drag.md",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

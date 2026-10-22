"""Generate B-3 qutrit convergence artifacts and validation report."""

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
from validation_pulse.qutrit_convergence import (  # noqa: E402
    run_qutrit_convergence_validation,
)


def main() -> int:
    args = _parse_args()
    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    report, rows = run_qutrit_convergence_validation()
    report["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["git_commit"] = git_commit(ROOT)
    report["software_versions"] = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "matplotlib": matplotlib.__version__,
    }

    json_path = output_dir / "pulse_b_qutrit_convergence.json"
    csv_path = output_dir / "pulse_b_qutrit_convergence.csv"
    convergence_plot_path = (
        output_dir / "pulse_b_qutrit_convergence.png"
    )
    physicality_plot_path = (
        output_dir / "pulse_b_qutrit_physicality.png"
    )
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_csv(csv_path, rows)
    _write_convergence_plot(convergence_plot_path, rows)
    _write_physicality_plot(physicality_plot_path, rows)
    _write_report(args.report_path, report)

    print(f"overall_pass={report['overall_pass']}")
    print(f"json={json_path}")
    print(f"csv={csv_path}")
    print(f"report={args.report_path}")
    return 0 if report["overall_pass"] else 1


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({
        key for row in rows for key in row
    })
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_convergence_plot(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    figure, axis = plt.subplots(figsize=(9.0, 5.5))
    case_names = sorted({
        row["case"]
        for row in rows
        if row["case"] != "deliberately_coarse_unsafe_guard"
    })
    for name in case_names:
        points = sorted(
            (
                row for row in rows
                if row["case"] == name and not row["is_reference"]
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
            label=name.replace("_", " "),
        )
    axis.set_title("B-3 qutrit fixed-step convergence")
    axis.set_xlabel("Requested internal step cap [us]")
    axis.set_ylabel("Final density-matrix max error")
    axis.grid(True, which="both", alpha=0.25)
    axis.legend(fontsize=7)
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_physicality_plot(
    path: Path,
    rows: list[dict[str, Any]],
) -> None:
    standard_rows = [
        row for row in rows
        if row["case"] != "deliberately_coarse_unsafe_guard"
        and not row["is_reference"]
    ]
    labels = [
        f"{row['case']}\n{row['step_factor']:g}x"
        for row in standard_rows
    ]
    eigenvalue_violation = [
        max(0.0, -row["raw_minimum_eigenvalue"])
        for row in standard_rows
    ]
    cleanup = [
        row["cleanup_correction_norm"] for row in standard_rows
    ]
    positions = list(range(len(labels)))

    figure, axis = plt.subplots(figsize=(11.0, 5.8))
    axis.semilogy(
        positions,
        [max(value, 1e-18) for value in eigenvalue_violation],
        marker="o",
        linestyle="none",
        label="raw negative-eigenvalue magnitude",
    )
    axis.semilogy(
        positions,
        [max(value, 1e-18) for value in cleanup],
        marker="x",
        linestyle="none",
        label="cleanup correction norm",
    )
    axis.axhline(1e-9, color="tab:red", linestyle="--", label="PSD tolerance")
    axis.set_xticks(positions)
    axis.set_xticklabels(labels, rotation=70, ha="right", fontsize=6)
    axis.set_ylabel("Magnitude")
    axis.set_title("B-3 raw physicality and cleanup audit")
    axis.grid(True, which="both", axis="y", alpha=0.25)
    axis.legend(fontsize=8)
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    policy = report["policy"]
    performance = report["performance"]
    summary = report["policy_step_summary"]
    lines = [
        "# Pulse Extension B-3: Qutrit Convergence",
        "",
        f"**Status:** {'PASS' if report['overall_pass'] else 'FAIL'}",
        "",
        "## Scope",
        "",
        "This validation fixes the non-DRAG qutrit RK4 step policy before",
        "public API or UI exposure. It compares four step refinements with",
        "a finer reference for five standard cases and includes one",
        "deliberately unsafe coarse-step control.",
        "",
        "## Frozen Policy",
        "",
        "| Parameter | Value |",
        "|---|---:|",
        f"| Hamiltonian control epsilon | `{policy['epsilon_h']}` |",
        f"| Dissipation control epsilon | `{policy['epsilon_d']}` |",
        f"| Gaussian samples per sigma | `{policy['samples_per_sigma']}` |",
        (
            "| Maximum internal steps | "
            f"`{policy['maximum_internal_step_count']}` |"
        ),
        (
            "| State error tolerance | "
            f"`{policy['state_error_tolerance']:.1e}` |"
        ),
        (
            "| Raw minimum eigenvalue tolerance | "
            f"`{policy['raw_minimum_eigenvalue_tolerance']:.1e}` |"
        ),
        "",
        "The Hamiltonian limit uses the full qutrit eigenvalue span, so",
        "anharmonicity is included even when the drive is zero. The",
        "dissipative scale includes both adjacent upward/downward channels",
        "and four times the adjacent-coherence dephasing rate.",
        "",
        "## Results",
        "",
        "| Case | Result | Limiting reason | Policy matrix error |",
        "|---|---:|---|---:|",
    ]
    for case in report["cases"]:
        matrix_error = case.get("policy_step_matrix_error")
        matrix_text = (
            f"`{matrix_error:.3e}`" if matrix_error is not None else "n/a"
        )
        lines.append(
            f"| {case['name']} | "
            f"{'PASS' if case['pass'] else 'FAIL'} | "
            f"{case['policy']['step_limit_reason']} | {matrix_text} |"
        )
    lines.extend([
        "",
        "## Policy-Step Summary",
        "",
        "| Metric | Value |",
        "|---|---:|",
        (
            "| Maximum full-matrix error | "
            f"`{summary['maximum_matrix_error']:.6e}` |"
        ),
        (
            "| Maximum population error | "
            f"`{summary['maximum_population_error']:.6e}` |"
        ),
        (
            "| Maximum leakage error | "
            f"`{summary['maximum_leakage_error']:.6e}` |"
        ),
        (
            "| Minimum raw eigenvalue | "
            f"`{summary['minimum_raw_eigenvalue']:.6e}` |"
        ),
        (
            "| Maximum cleanup correction | "
            f"`{summary['maximum_cleanup_correction_norm']:.6e}` |"
        ),
        "",
        "The deliberately coarse control develops a large negative raw",
        "eigenvalue, while the selected policy returns to the declared raw",
        "physicality and state-error bounds. Cleanup is not a PSD projection",
        "and therefore does not conceal that failure.",
        "",
        "## Performance And Work Budget",
        "",
        "| Metric | Value |",
        "|---|---:|",
        (
            "| Measured internal steps | "
            f"`{performance['measured_total_internal_steps']}` |"
        ),
        (
            "| Measured total runtime | "
            f"`{performance['measured_total_runtime_ms']:.3f} ms` |"
        ),
        (
            "| Measured cost per step | "
            f"`{performance['measured_ms_per_internal_step']:.6f} ms` |"
        ),
        (
            "| Estimated runtime at budget | "
            f"`{performance['estimated_runtime_at_work_budget_ms']:.3f} ms` |"
        ),
        "",
        "The recommended future preflight budget is 25,000 internal steps.",
        "This is a deterministic work bound, not a latency guarantee. Qutrit",
        "HTTP execution remains `contract_only` until later B phases.",
        "",
        "## Limitations",
        "",
        "- Fixed-step RK4 is not claimed to be intrinsically CPTP.",
        "- DRAG, adaptive integration, and qutrit QuTiP comparison are not",
        "  covered here.",
        "- The threshold is supported only for the declared non-DRAG qutrit",
        "  operating fixtures.",
        "",
        "## Artifacts",
        "",
        "```text",
        "validation_results/pulse_b_qutrit_convergence.json",
        "validation_results/pulse_b_qutrit_convergence.csv",
        "validation_results/pulse_b_qutrit_convergence.png",
        "validation_results/pulse_b_qutrit_physicality.png",
        "```",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
        default=(
            ROOT
            / "docs"
            / "validation"
            / "pulse-b-qutrit-convergence.md"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

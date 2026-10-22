"""Generate Pulse Extension B-2 qutrit dissipation artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._common import git_commit
from validation_pulse.qutrit_dissipation import (
    run_qutrit_dissipation_validation,
)


CSV_FIELDS = [
    "case",
    "time_us",
    "segment",
    "population_0",
    "population_1",
    "population_2",
    "coherence_01_abs",
    "coherence_12_abs",
    "coherence_02_abs",
    "expected_population_0",
    "expected_population_1",
    "expected_population_2",
    "expected_coherence_01_abs",
    "expected_coherence_12_abs",
    "expected_coherence_02_abs",
    "population_sum_error",
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

    report, rows = run_qutrit_dissipation_validation()
    report["base_git_commit"] = git_commit(ROOT)
    report["python_version"] = platform.python_version()
    report["numpy_version"] = np.__version__

    json_path = args.output_dir / "pulse_b_qutrit_dissipation.json"
    csv_path = args.output_dir / "pulse_b_qutrit_dissipation.csv"
    equilibrium_path = (
        args.output_dir / "pulse_b_qutrit_thermal_equilibrium.png"
    )
    coherence_path = (
        args.output_dir / "pulse_b_qutrit_coherence_decay.png"
    )
    json_path.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    _write_equilibrium_plot(rows, equilibrium_path)
    _write_coherence_plot(rows, coherence_path)
    _write_markdown_report(report, args.report_path)

    print(
        "validation | PULSE-B2-QUTRIT-DISSIPATION | "
        f"overall_pass={report['overall_pass']}"
    )
    for case in report["cases"]:
        print(f"{case['name']} | pass={case['pass']}")
    print(
        f"artifacts | {json_path} | {csv_path} | "
        f"{equilibrium_path} | {coherence_path}"
    )
    return 0 if report["overall_pass"] else 1


def _write_equilibrium_plot(
    rows: list[dict[str, object]],
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    selected = [row for row in rows if row["case"] == "three_level_gibbs"]
    figure, axis = plt.subplots(figsize=(8.0, 4.8))
    for level in range(3):
        actual_field = f"population_{level}"
        expected_field = f"expected_population_{level}"
        line, = axis.plot(
            [row["time_us"] for row in selected],
            [row[actual_field] for row in selected],
            label=f"P{level}",
        )
        axis.axhline(
            float(selected[-1][expected_field]),
            linestyle="--",
            alpha=0.65,
            color=line.get_color(),
            label=f"Gibbs P{level}",
        )
    axis.set(
        title="Three-level no-drive thermal relaxation",
        xlabel="Time [us]",
        ylabel="Population",
        ylim=(-0.02, 1.02),
    )
    axis.grid(alpha=0.25)
    axis.legend(ncol=2)
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def _write_coherence_plot(
    rows: list[dict[str, object]],
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    selected = [
        row for row in rows
        if row["case"] == "pure_dephasing_one_one_four"
    ]
    figure, axis = plt.subplots(figsize=(8.0, 4.8))
    for suffix, label in (
        ("01", "|rho01|"),
        ("12", "|rho12|"),
        ("02", "|rho02|"),
    ):
        line, = axis.plot(
            [row["time_us"] for row in selected],
            [row[f"coherence_{suffix}_abs"] for row in selected],
            label=label,
        )
        axis.plot(
            [row["time_us"] for row in selected],
            [
                row[f"expected_coherence_{suffix}_abs"]
                for row in selected
            ],
            linestyle="--",
            alpha=0.7,
            color=line.get_color(),
            label=f"analytic {label}",
        )
    axis.set(
        title="Number-operator pure dephasing",
        xlabel="Time [us]",
        ylabel="Coherence magnitude",
    )
    axis.grid(alpha=0.25)
    axis.legend(ncol=2)
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def _write_markdown_report(
    report: dict[str, object],
    path: Path,
) -> None:
    cases_by_name = {
        case["name"]: case for case in report["cases"]
    }
    rates = cases_by_name["zero_temperature_and_detailed_balance"]
    cascade = cases_by_name["zero_temperature_cascade"]
    dephasing = cases_by_name["pure_dephasing_one_one_four"]
    outflow = cases_by_name["population_outflow_coherence"]
    gibbs = cases_by_name["three_level_gibbs"]
    drive_idle = cases_by_name["dissipative_pulse_and_idle"]
    mode_match = cases_by_name["physical_direct_rate_equivalence"]
    case_rows = "\n".join(
        f"| `{case['name']}` | {'PASS' if case['pass'] else 'FAIL'} |"
        for case in report["cases"]
    )
    lines = [
        "# Pulse B-2 Qutrit Dissipation Validation",
        "",
        "## Decision",
        "",
        f"**Result: {'PASS' if report['overall_pass'] else 'FAIL'}**",
        "",
        "The fixed B-2 cases validate transition-specific qutrit",
        "relaxation, excitation, number-operator dephasing, and no-drive",
        "thermal equilibrium. They do not establish the B-3 production",
        "step policy or enable qutrit HTTP execution.",
        "",
        "## Cases",
        "",
        "| Case | Result |",
        "|---|---|",
        case_rows,
        "",
        "## Key Measurements",
        "",
        "| Metric | Value |",
        "|---|---:|",
        (
            "| Detailed-balance error, 0-1 | "
            f"`{rates['detailed_balance_error_01']:.6e}` |"
        ),
        (
            "| Detailed-balance error, 1-2 | "
            f"`{rates['detailed_balance_error_12']:.6e}` |"
        ),
        (
            "| Cascade maximum population error | "
            f"`{cascade['maximum_population_error']:.6e}` |"
        ),
        (
            "| Pure-dephasing maximum coherence error | "
            f"`{max(dephasing['maximum_coherence_errors'].values()):.6e}` |"
        ),
        (
            "| Population-outflow coherence error | "
            f"`{outflow['maximum_coherence_error']:.6e}` |"
        ),
        (
            "| Gibbs maximum population error | "
            f"`{gibbs['maximum_population_error']:.6e}` |"
        ),
        (
            "| Pulse state change | "
            f"`{drive_idle['pulse_state_change']:.6e}` |"
        ),
        (
            "| Idle state change | "
            f"`{drive_idle['idle_state_change']:.6e}` |"
        ),
        (
            "| Physical/direct final-state error | "
            f"`{mode_match['final_max_element_error']:.6e}` |"
        ),
        "",
        "## Interpretation",
        "",
        "- Upward rates vanish at zero temperature.",
        "- Both adjacent transitions satisfy their own thermal detailed",
        "  balance relation.",
        "- The `|2>` population follows the expected `2 -> 1 -> 0` cascade.",
        "- `sqrt(2 gamma_phi_adjacent) n` gives coherence-rate ratios",
        "  `rho01 : rho12 : rho02 = 1 : 1 : 4`.",
        "- Long-time no-drive populations approach the three-level Gibbs",
        "  distribution.",
        "- The same collapse operators act during the pulse and idle.",
        "",
        "## Numerical Boundary",
        "",
        "B-2 uses explicit validation steps selected for these fixtures.",
        "The production qutrit safe-step and work-budget policy remains B-3.",
        "RK4 is audited before cleanup but is not claimed to be intrinsically",
        "CPTP at an arbitrary finite step.",
        "",
        "## Artifacts",
        "",
        "```text",
        "validation_results/pulse_b_qutrit_dissipation.json",
        "validation_results/pulse_b_qutrit_dissipation.csv",
        "validation_results/pulse_b_qutrit_thermal_equilibrium.png",
        "validation_results/pulse_b_qutrit_coherence_decay.png",
        "```",
    ]
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
            / "pulse-b-qutrit-dissipation.md"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

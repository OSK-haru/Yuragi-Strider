"""Generate Pulse Extension B-1 closed-qutrit validation artifacts."""

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
from validation_pulse.qutrit_closed import run_closed_qutrit_validation


CSV_FIELDS = [
    "case",
    "time_us",
    "segment",
    "population_0",
    "population_1",
    "population_2",
    "computational_population",
    "population_sum_error",
]


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    report, rows = run_closed_qutrit_validation()
    report["base_git_commit"] = git_commit(ROOT)
    report["python_version"] = platform.python_version()
    report["numpy_version"] = np.__version__

    json_path = args.output_dir / "pulse_b_closed_qutrit.json"
    csv_path = args.output_dir / "pulse_b_closed_qutrit.csv"
    populations_path = (
        args.output_dir / "pulse_b_closed_qutrit_populations.png"
    )
    leakage_path = (
        args.output_dir / "pulse_b_closed_qutrit_leakage.png"
    )

    json_path.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    _write_population_plot(rows, populations_path)
    _write_leakage_plot(rows, leakage_path)
    _write_markdown_report(report, args.report_path)

    print(
        "validation | PULSE-B1-CLOSED-QUTRIT | "
        f"overall_pass={report['overall_pass']}"
    )
    for case in report["cases"]:
        print(f"{case['name']} | pass={case['pass']}")
    print(
        f"artifacts | {json_path} | {csv_path} | "
        f"{populations_path} | {leakage_path}"
    )
    return 0 if report["overall_pass"] else 1


def _write_population_plot(
    rows: list[dict[str, object]],
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    selected = [
        row for row in rows if row["case"] == "alpha_-100_mhz"
    ]
    figure, axis = plt.subplots(figsize=(8.0, 4.8))
    for field, label in (
        ("population_0", "P0"),
        ("population_1", "P1"),
        ("population_2", "P2 leakage"),
    ):
        axis.plot(
            [row["time_us"] for row in selected],
            [row[field] for row in selected],
            label=label,
        )
    axis.set(
        title="Closed qutrit populations: alpha = -100 MHz",
        xlabel="Time [us]",
        ylabel="Population",
        ylim=(-0.02, 1.02),
    )
    axis.grid(alpha=0.25)
    axis.legend()
    figure.tight_layout()
    figure.savefig(output_path, dpi=160)
    plt.close(figure)


def _write_leakage_plot(
    rows: list[dict[str, object]],
    output_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(8.0, 4.8))
    for case, label in (
        ("alpha_-100_mhz", "alpha = -100 MHz"),
        ("alpha_-300_mhz", "alpha = -300 MHz"),
    ):
        selected = [row for row in rows if row["case"] == case]
        axis.plot(
            [row["time_us"] for row in selected],
            [row["population_2"] for row in selected],
            label=label,
        )
    axis.set(
        title="Recorded qutrit leakage under a fixed Gaussian pulse",
        xlabel="Time [us]",
        ylabel="P2",
    )
    axis.grid(alpha=0.25)
    axis.legend()
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
    free_coherence = cases_by_name["free_coherence_0_2"]
    weak = cases_by_name["weak_selective_pi_over_2"]
    alpha = cases_by_name["fixed_gaussian_anharmonicity_comparison"]
    idle = cases_by_name["closed_pulse_then_free_idle"]
    alpha_100 = alpha["by_anharmonicity_mhz"]["-100.0"]
    alpha_300 = alpha["by_anharmonicity_mhz"]["-300.0"]
    case_rows = "\n".join(
        f"| `{case['name']}` | {'PASS' if case['pass'] else 'FAIL'} |"
        for case in report["cases"]
    )
    lines = [
        "# Pulse B-1 Closed Qutrit Validation",
        "",
        "## Decision",
        "",
        f"**Result: {'PASS' if report['overall_pass'] else 'FAIL'}**",
        "",
        "The fixed B-1 cases validate closed 3x3 rotating-frame qutrit",
        "evolution and checkpoint-sampled leakage. They do not validate",
        "qutrit dissipation, DRAG, QuTiP agreement, or hardware behavior.",
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
            "| Free 0-2 coherence error | "
            f"`{free_coherence['coherence_02_error']:.6e}` |"
        ),
        (
            "| Weak-pulse qutrit/two-level block error | "
            f"`{weak['qutrit_to_two_level_block_error']:.6e}` |"
        ),
        (
            "| Weak-pulse final leakage | "
            f"`{weak['final_leakage_probability']:.6e}` |"
        ),
        (
            "| Maximum recorded leakage, -100 MHz | "
            f"`{alpha_100['maximum_recorded_leakage_probability']:.6e}` |"
        ),
        (
            "| Maximum recorded leakage, -300 MHz | "
            f"`{alpha_300['maximum_recorded_leakage_probability']:.6e}` |"
        ),
        (
            "| Idle population change | "
            f"`{idle['maximum_population_change_during_idle']:.6e}` |"
        ),
        (
            "| Idle 0-1 coherence change | "
            f"`{idle['coherence_01_change_during_idle']:.6e}` |"
        ),
        "",
        "## Interpretation",
        "",
        "- Zero drive preserves basis populations.",
        "- Free 0-2 coherence follows the diagonal Hamiltonian phase.",
        "- A weak selective pulse remains close to the two-level result but",
        "  retains a finite qutrit/AC-Stark correction.",
        "- The fixed strong-pulse comparison records lower leakage for",
        "  `-300 MHz` than for `-100 MHz`; this is not a universal hardware",
        "  performance claim.",
        "- Closed free idle preserves populations while coherent phases evolve.",
        "",
        "## Numerical Boundary",
        "",
        "B-1 uses explicitly fine validation steps. It does not establish the",
        "production qutrit safe-step policy; that belongs to B-3.",
        "`maximum_recorded_leakage_probability` is the maximum over retained",
        "checkpoints and is not guaranteed to capture an extremum between",
        "checkpoints.",
        "",
        "## Artifacts",
        "",
        "```text",
        "validation_results/pulse_b_closed_qutrit.json",
        "validation_results/pulse_b_closed_qutrit.csv",
        "validation_results/pulse_b_closed_qutrit_populations.png",
        "validation_results/pulse_b_closed_qutrit_leakage.png",
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
        default=ROOT / "docs" / "validation" / "pulse-b-closed-qutrit.md",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

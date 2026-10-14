"""Generate the physical-model baseline record used before tenant simulation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from validation_physics.model_baseline import write_freeze_report


def main() -> int:
    args = _parse_args()
    report = write_freeze_report(args.output_path)
    print(
        "validation | PHYSICAL-MODEL-BASELINE-V1 | "
        f"decision={report['decision']} | overall_pass={report['overall_pass']}"
    )
    print(f"freeze_id | {report['frozen_contract']['freeze_id']}")
    print(
        "critical_source_tree_sha256 | "
        f"{report['source_revision']['critical_source_tree_sha256']}"
    )
    return 0 if report["overall_pass"] else 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-path",
        type=Path,
        default=ROOT / "validation_results" / "physical_model_baseline_v1.json",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

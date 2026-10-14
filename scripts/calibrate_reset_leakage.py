"""Calibrate simulator-derived leakage for the internal reset behavior model."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.experimental_isolation_physics import (  # noqa: E402
    ImperfectResetSpec,
    PhenomenologicalResetModel,
    ResetCalibrationInputMode,
    run_reset_leakage_calibration,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--success-probability", type=float, required=True)
    parser.add_argument("--target-excited-population", type=float, default=0.0)
    parser.add_argument("--observed-one-given-zero", type=float, default=0.0)
    parser.add_argument("--observed-zero-given-one", type=float, default=0.0)
    parser.add_argument("--reset-count", type=int, default=1)
    parser.add_argument(
        "--shots",
        type=int,
        nargs="+",
        default=(1, 8, 32, 128),
        help="Per-case shot budgets to evaluate.",
    )
    parser.add_argument("--profile-id", default="reset_leakage_binary")
    parser.add_argument("--profile-version", default="1")
    parser.add_argument("--generated-at-utc")
    parser.add_argument(
        "--input-mode",
        choices=tuple(mode.value for mode in ResetCalibrationInputMode),
        default=ResetCalibrationInputMode.VICTIM_DEPENDENT.value,
        help="Use fixed_zero_control for an isolated-resource baseline.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    excited = float(args.target_excited_population)
    model = PhenomenologicalResetModel(
        spec=ImperfectResetSpec(
            success_probability=args.success_probability,
            target_populations=(1.0 - excited, excited),
        ),
        observed_one_given_zero=args.observed_one_given_zero,
        observed_zero_given_one=args.observed_zero_given_one,
    )
    result = run_reset_leakage_calibration(
        model,
        reset_count=args.reset_count,
        victim_measurement=True,
        shot_counts=args.shots,
        profile_id=args.profile_id,
        profile_version=args.profile_version,
        generated_at_utc=args.generated_at_utc,
        input_mode=args.input_mode,
    )
    report = {
        "contract": "reset_leakage_calibration_v1",
        "profile": result.profile.to_dict(),
        "model_parameters": {
            "success_probability": model.spec.success_probability,
            "target_populations": list(model.spec.target_populations),
            "observed_one_given_zero": model.observed_one_given_zero,
            "observed_zero_given_one": model.observed_zero_given_one,
        },
        "metrics": [asdict(point) for point in result.points],
        "runtime": {"model_compute_seconds": result.model_compute_seconds},
        "claims": {
            "paper_numeric_match": False,
            "hardware_replication": False,
            "profile_source": "simulator_derived",
        },
    }
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()

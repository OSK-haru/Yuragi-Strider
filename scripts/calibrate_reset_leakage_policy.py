"""Sweep reset-leakage profiles and exact hidden-evaluation pass rates."""

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
    calibrate_frequency_threshold,
    evaluate_frequency_threshold,
    hidden_evaluation_pass_probability,
    run_reset_leakage_calibration,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--success-probabilities",
        type=float,
        nargs="+",
        required=True,
    )
    parser.add_argument("--target-excited-population", type=float, required=True)
    parser.add_argument("--observed-one-given-zero", type=float, required=True)
    parser.add_argument("--observed-zero-given-one", type=float, required=True)
    parser.add_argument("--reset-count", type=int, default=1)
    parser.add_argument("--shots", type=int, nargs="+", required=True)
    parser.add_argument("--case-count", type=int, required=True)
    parser.add_argument("--zero-case-count", type=int, required=True)
    parser.add_argument("--required-correct", type=int, nargs="+", required=True)
    parser.add_argument("--generated-at-utc", required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _pass_probability(
    *,
    calibration,
    zero_case_count: int,
    one_case_count: int,
    required_correct: int,
) -> float:
    return hidden_evaluation_pass_probability(
        zero_case_count=zero_case_count,
        one_case_count=one_case_count,
        required_correct=required_correct,
        accuracy_given_zero=calibration.accuracy_given_zero,
        accuracy_given_one=calibration.accuracy_given_one,
    )


def main() -> None:
    args = _parse_args()
    case_count = int(args.case_count)
    zero_case_count = int(args.zero_case_count)
    one_case_count = case_count - zero_case_count
    if zero_case_count < 1 or one_case_count < 1:
        raise ValueError("case composition must contain both victim bits")
    required_correct_values = tuple(int(value) for value in args.required_correct)
    if any(value < 1 or value > case_count for value in required_correct_values):
        raise ValueError("required_correct is outside the hidden case count")

    excited = float(args.target_excited_population)
    common_model_arguments = {
        "target_populations": (1.0 - excited, excited),
    }
    profiles: list[dict[str, object]] = []
    for success_probability in args.success_probabilities:
        candidate_model = PhenomenologicalResetModel(
            spec=ImperfectResetSpec(
                success_probability=success_probability,
                **common_model_arguments,
            ),
            observed_one_given_zero=args.observed_one_given_zero,
            observed_zero_given_one=args.observed_zero_given_one,
        )
        perfect_model = PhenomenologicalResetModel(
            spec=ImperfectResetSpec(
                success_probability=1.0,
                **common_model_arguments,
            ),
            observed_one_given_zero=args.observed_one_given_zero,
            observed_zero_given_one=args.observed_zero_given_one,
            model_id="perfect_reset_control",
        )
        calibration_arguments = {
            "reset_count": args.reset_count,
            "victim_measurement": True,
            "shot_counts": tuple(args.shots),
            "generated_at_utc": args.generated_at_utc,
        }
        candidate = run_reset_leakage_calibration(
            candidate_model,
            profile_id=f"candidate_reset_{success_probability:g}",
            **calibration_arguments,
        )
        perfect = run_reset_leakage_calibration(
            perfect_model,
            profile_id="perfect_reset_control",
            **calibration_arguments,
        )
        isolated = run_reset_leakage_calibration(
            candidate_model,
            profile_id=f"isolated_reset_{success_probability:g}",
            input_mode=ResetCalibrationInputMode.FIXED_ZERO_CONTROL,
            **calibration_arguments,
        )
        points: list[dict[str, object]] = []
        for candidate_point in candidate.points:
            shots = candidate_point.shots_per_case
            perfect_point = next(
                point for point in perfect.points if point.shots_per_case == shots
            )
            isolated_point = next(
                point for point in isolated.points if point.shots_per_case == shots
            )
            estimator = calibrate_frequency_threshold(
                candidate.profile.one_probability_given_victim_zero,
                candidate.profile.one_probability_given_victim_one,
                shots,
            )
            perfect_estimator = evaluate_frequency_threshold(
                estimator,
                perfect.profile.one_probability_given_victim_zero,
                perfect.profile.one_probability_given_victim_one,
            )
            isolated_estimator = evaluate_frequency_threshold(
                estimator,
                isolated.profile.one_probability_given_victim_zero,
                isolated.profile.one_probability_given_victim_one,
            )
            pass_rates = []
            for required_correct in required_correct_values:
                pass_rates.append({
                    "required_correct": required_correct,
                    "candidate": _pass_probability(
                        calibration=estimator,
                        zero_case_count=zero_case_count,
                        one_case_count=one_case_count,
                        required_correct=required_correct,
                    ),
                    "perfect_reset_control": _pass_probability(
                        calibration=perfect_estimator,
                        zero_case_count=zero_case_count,
                        one_case_count=one_case_count,
                        required_correct=required_correct,
                    ),
                    "isolated_resource_control": _pass_probability(
                        calibration=isolated_estimator,
                        zero_case_count=zero_case_count,
                        one_case_count=one_case_count,
                        required_correct=required_correct,
                    ),
                })
            points.append({
                "shots_per_case": shots,
                "candidate_metrics": asdict(candidate_point),
                "perfect_reset_metrics": asdict(perfect_point),
                "isolated_resource_metrics": asdict(isolated_point),
                "estimator": asdict(estimator),
                "pass_probabilities": pass_rates,
            })
        profiles.append({
            "success_probability": success_probability,
            "candidate_profile": candidate.profile.to_dict(),
            "perfect_reset_control": perfect.profile.to_dict(),
            "isolated_resource_control": isolated.profile.to_dict(),
            "points": points,
        })

    constant_best = max(zero_case_count, one_case_count)
    payload = {
        "contract": "reset_leakage_policy_calibration_v1",
        "case_composition": {
            "case_count": case_count,
            "zero_case_count": zero_case_count,
            "one_case_count": one_case_count,
            "constant_estimator_best_correct": constant_best,
        },
        "model_parameters": {
            "target_excited_population": excited,
            "observed_one_given_zero": args.observed_one_given_zero,
            "observed_zero_given_one": args.observed_zero_given_one,
            "reset_count": args.reset_count,
        },
        "profiles": profiles,
        "claims": {
            "paper_numeric_match": False,
            "hardware_replication": False,
            "profile_source": "simulator_derived",
            "pass_probabilities": "exact_for_declared_behavioral_model",
            "runtime_scope": "metric_computation_only_not_end_to_end",
        },
    }
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()

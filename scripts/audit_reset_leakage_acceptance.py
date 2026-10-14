"""Audit one candidate reset-leakage profile against mandatory controls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.attack_simulation import (  # noqa: E402
    HiddenEvaluationPolicy,
    ResetLeakageAcceptancePolicy,
    audit_reset_leakage_acceptance,
)
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
    parser.add_argument("--shots-per-case", type=int, required=True)
    parser.add_argument("--case-count", type=int, required=True)
    parser.add_argument("--zero-case-count", type=int, required=True)
    parser.add_argument("--required-correct", type=int, required=True)
    parser.add_argument("--minimum-information-bits", type=float, required=True)
    parser.add_argument("--minimum-advantage", type=float, required=True)
    parser.add_argument("--maximum-control-information-bits", type=float, required=True)
    parser.add_argument("--maximum-control-advantage", type=float, required=True)
    parser.add_argument("--case-order-seed", type=int, required=True)
    parser.add_argument("--measurement-seed", type=int, required=True)
    parser.add_argument("--generated-at-utc")
    parser.add_argument(
        "--observation-disabled-verified",
        action="store_true",
        help="Confirm that the access-disabled control test passed.",
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    excited = float(args.target_excited_population)
    common = {
        "target_populations": (1.0 - excited, excited),
    }
    candidate_model = PhenomenologicalResetModel(
        spec=ImperfectResetSpec(
            success_probability=args.success_probability,
            **common,
        ),
        observed_one_given_zero=args.observed_one_given_zero,
        observed_zero_given_one=args.observed_zero_given_one,
    )
    perfect_model = PhenomenologicalResetModel(
        spec=ImperfectResetSpec(success_probability=1.0, **common),
        observed_one_given_zero=args.observed_one_given_zero,
        observed_zero_given_one=args.observed_zero_given_one,
        model_id="perfect_reset_control",
    )
    calibration_args = {
        "reset_count": args.reset_count,
        "victim_measurement": True,
        "shot_counts": (args.shots_per_case,),
        "generated_at_utc": args.generated_at_utc,
    }
    candidate = run_reset_leakage_calibration(
        candidate_model,
        profile_id="reset_leakage_candidate",
        **calibration_args,
    )
    perfect = run_reset_leakage_calibration(
        perfect_model,
        profile_id="perfect_reset_control",
        **calibration_args,
    )
    isolated = run_reset_leakage_calibration(
        candidate_model,
        profile_id="isolated_resource_control",
        input_mode=ResetCalibrationInputMode.FIXED_ZERO_CONTROL,
        **calibration_args,
    )
    hidden_policy = HiddenEvaluationPolicy(
        policy_id="reset_hidden_candidate",
        policy_version="1",
        case_count=args.case_count,
        zero_case_count=args.zero_case_count,
        shots_per_case=args.shots_per_case,
        required_correct=args.required_correct,
        case_order_seed=args.case_order_seed,
        measurement_seed=args.measurement_seed,
    )
    acceptance_policy = ResetLeakageAcceptancePolicy(
        policy_id="reset_acceptance_candidate",
        policy_version="1",
        shots_per_case=args.shots_per_case,
        minimum_candidate_information_bits=args.minimum_information_bits,
        minimum_candidate_advantage=args.minimum_advantage,
        maximum_control_information_bits=args.maximum_control_information_bits,
        maximum_control_advantage=args.maximum_control_advantage,
    )
    report = audit_reset_leakage_acceptance(
        candidate=candidate,
        perfect_reset_control=perfect,
        isolated_resource_control=isolated,
        hidden_policy=hidden_policy,
        observation_disabled_verified=args.observation_disabled_verified,
        policy=acceptance_policy,
    )
    payload = {
        "contract": "reset_leakage_acceptance_v0",
        "candidate_profile": candidate.profile.to_dict(),
        "perfect_reset_control": perfect.profile.to_dict(),
        "isolated_resource_control": isolated.profile.to_dict(),
        "acceptance": report.to_dict(),
    }
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()

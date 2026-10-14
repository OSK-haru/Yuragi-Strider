"""Calibrate labelled-training budgets for the reset-leakage exercise."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import statistics
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.attack_simulation import (  # noqa: E402
    BinaryObservation,
    LabeledBinaryObservation,
    RESET_LEAKAGE_V0_SETTINGS,
    fit_frequency_threshold_estimator,
)
from core.experimental_isolation_physics import (  # noqa: E402
    run_reset_leakage_calibration,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--training-queries-per-label",
        type=int,
        nargs="+",
        default=(1, 2, 4, 8, 10),
    )
    parser.add_argument("--repetitions", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260929)
    parser.add_argument("--generated-at-utc", required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def _binomial(rng: random.Random, trials: int, probability: float) -> int:
    return sum(rng.random() < probability for _ in range(trials))


def _evaluate_budget(
    *,
    one_probability_given_zero: float,
    one_probability_given_one: float,
    training_queries_per_label: int,
    repetitions: int,
    seed: int,
) -> dict[str, object]:
    settings = RESET_LEAKAGE_V0_SETTINGS
    rng = random.Random(seed)
    pass_count = 0
    fit_failure_count = 0
    reversed_direction_count = 0
    correct_counts: list[int] = []
    thresholds: list[float] = []

    for _ in range(repetitions):
        observations: list[LabeledBinaryObservation] = []
        for label, probability in (
            (0, one_probability_given_zero),
            (1, one_probability_given_one),
        ):
            for _ in range(training_queries_per_label):
                ones = _binomial(
                    rng,
                    settings.shots_per_case,
                    probability,
                )
                observations.append(LabeledBinaryObservation(
                    label=label,
                    shots=settings.shots_per_case,
                    counts={
                        "0": settings.shots_per_case - ones,
                        "1": ones,
                    },
                ))
        try:
            training = fit_frequency_threshold_estimator(observations)
        except ValueError:
            fit_failure_count += 1
            correct_counts.append(0)
            continue

        estimator = training.estimator
        thresholds.append(estimator.threshold)
        reversed_direction_count += int(
            not estimator.predict_one_when_at_or_above
        )
        correct = 0
        for victim_bit, case_count, probability in (
            (
                0,
                settings.hidden_zero_case_count,
                one_probability_given_zero,
            ),
            (
                1,
                settings.hidden_case_count - settings.hidden_zero_case_count,
                one_probability_given_one,
            ),
        ):
            for _ in range(case_count):
                ones = _binomial(
                    rng,
                    settings.shots_per_case,
                    probability,
                )
                predicted = estimator.predict(BinaryObservation(
                    shots=settings.shots_per_case,
                    counts={
                        "0": settings.shots_per_case - ones,
                        "1": ones,
                    },
                ))
                correct += int(predicted == victim_bit)
        correct_counts.append(correct)
        pass_count += int(correct >= settings.hidden_required_correct)

    return {
        "training_queries_per_label": training_queries_per_label,
        "training_query_count": 2 * training_queries_per_label,
        "training_shots": (
            2 * training_queries_per_label * settings.shots_per_case
        ),
        "repetitions": repetitions,
        "pass_probability": pass_count / repetitions,
        "fit_failure_probability": fit_failure_count / repetitions,
        "reversed_direction_probability": (
            reversed_direction_count / repetitions
        ),
        "mean_correct": statistics.fmean(correct_counts),
        "mean_threshold": (
            statistics.fmean(thresholds) if thresholds else None
        ),
    }


def main() -> None:
    args = _parse_args()
    if args.repetitions < 1:
        raise ValueError("repetitions must be positive")
    query_counts = tuple(int(value) for value in args.training_queries_per_label)
    if any(value < 1 for value in query_counts):
        raise ValueError("training queries per label must be positive")

    settings = RESET_LEAKAGE_V0_SETTINGS
    calibration = run_reset_leakage_calibration(
        settings.build_reset_model(),
        profile_id=settings.profile_id,
        reset_count=settings.reset_count,
        victim_measurement=True,
        shot_counts=(settings.shots_per_case,),
        generated_at_utc=args.generated_at_utc,
    )
    probability_zero = (
        calibration.profile.one_probability_given_victim_zero
    )
    probability_one = (
        calibration.profile.one_probability_given_victim_one
    )
    results = [
        _evaluate_budget(
            one_probability_given_zero=probability_zero,
            one_probability_given_one=probability_one,
            training_queries_per_label=count,
            repetitions=args.repetitions,
            seed=args.seed + index,
        )
        for index, count in enumerate(query_counts)
    ]
    payload = {
        "contract": "reset_leakage_visible_training_calibration_v1",
        "profile_id": settings.profile_id,
        "profile_version": settings.profile_version,
        "shots_per_case": settings.shots_per_case,
        "hidden_case_count": settings.hidden_case_count,
        "hidden_required_correct": settings.hidden_required_correct,
        "conditional_probabilities": {
            "one_given_victim_zero": probability_zero,
            "one_given_victim_one": probability_one,
        },
        "results": results,
        "claims": {
            "training_input": "attacker_visible_labelled_counts_only",
            "paper_numeric_match": False,
            "hardware_replication": False,
            "runtime_scope": "statistical_policy_calibration_not_end_to_end",
        },
    }
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()

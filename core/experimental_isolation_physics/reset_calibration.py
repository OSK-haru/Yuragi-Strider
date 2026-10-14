"""Deterministic calibration for reset-leakage behavioral profiles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import math
from time import perf_counter
from collections.abc import Sequence

from core.experimental_isolation_physics.reset_model import (
    ResetModel,
    ResetModelMode,
    ResetModelRequest,
)
from core.gates import density_from_ket


class ResetCalibrationInputMode(str, Enum):
    """Whether victim state may influence the reset-model input."""

    VICTIM_DEPENDENT = "victim_dependent"
    FIXED_ZERO_CONTROL = "fixed_zero_control"


@dataclass(frozen=True)
class ResetProfile:
    """Versioned simulator-derived conditional observation profile."""

    profile_id: str
    profile_version: str
    model_id: str
    model_version: str
    model_configuration_id: str
    fidelity_level: str
    provenance: str
    model_mode: ResetModelMode
    input_mode: ResetCalibrationInputMode
    source: str
    reset_count: int
    victim_measurement: bool
    one_probability_given_victim_zero: float
    one_probability_given_victim_one: float
    seed_policy: str
    generated_at_utc: str

    def __post_init__(self) -> None:
        if self.source != "simulator_derived":
            raise ValueError("reset calibration profiles must be simulator_derived")
        for name in (
            "profile_id",
            "profile_version",
            "model_id",
            "model_version",
            "model_configuration_id",
            "fidelity_level",
            "provenance",
            "seed_policy",
            "generated_at_utc",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be empty")
        model_mode = (
            self.model_mode
            if isinstance(self.model_mode, ResetModelMode)
            else ResetModelMode(self.model_mode)
        )
        input_mode = (
            self.input_mode
            if isinstance(self.input_mode, ResetCalibrationInputMode)
            else ResetCalibrationInputMode(self.input_mode)
        )
        object.__setattr__(self, "model_mode", model_mode)
        object.__setattr__(self, "input_mode", input_mode)
        if self.reset_count < 0:
            raise ValueError("reset_count must be non-negative")
        _probability(
            self.one_probability_given_victim_zero,
            "one_probability_given_victim_zero",
        )
        _probability(
            self.one_probability_given_victim_one,
            "one_probability_given_victim_one",
        )

    def to_dict(self) -> dict[str, bool | float | int | str]:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_configuration_id": self.model_configuration_id,
            "fidelity_level": self.fidelity_level,
            "provenance": self.provenance,
            "model_mode": self.model_mode.value,
            "input_mode": self.input_mode.value,
            "source": self.source,
            "reset_count": self.reset_count,
            "victim_measurement": self.victim_measurement,
            "one_probability_given_victim_zero": (
                self.one_probability_given_victim_zero
            ),
            "one_probability_given_victim_one": (
                self.one_probability_given_victim_one
            ),
            "seed_policy": self.seed_policy,
            "generated_at_utc": self.generated_at_utc,
        }


@dataclass(frozen=True)
class LeakageCalibrationPoint:
    """Exact balanced-prior leakage metrics for one shot budget."""

    shots_per_case: int
    per_shot_mutual_information_bits: float
    per_case_mutual_information_bits: float
    bayes_optimal_accuracy: float
    attacker_advantage: float
    metric_compute_seconds: float


@dataclass(frozen=True)
class FrequencyThresholdCalibration:
    """Exact performance of one count-threshold estimator."""

    shots_per_case: int
    count_threshold: int
    frequency_threshold: float
    predict_one_when_at_or_above: bool
    accuracy_given_zero: float
    accuracy_given_one: float
    balanced_accuracy: float


@dataclass(frozen=True)
class ResetCalibrationResult:
    profile: ResetProfile
    points: tuple[LeakageCalibrationPoint, ...]
    model_compute_seconds: float


def derive_reset_profile(
    model: ResetModel,
    *,
    reset_count: int,
    victim_measurement: bool,
    profile_id: str = "reset_leakage_binary",
    profile_version: str = "1",
    seed_policy: str = "explicit_seed_v1",
    generated_at_utc: str | None = None,
    input_mode: ResetCalibrationInputMode | str = (
        ResetCalibrationInputMode.VICTIM_DEPENDENT
    ),
) -> ResetProfile:
    """Derive P(attacker=1 | victim bit) from the selected simulator model."""

    zero = density_from_ket((1.0 + 0.0j, 0.0j))
    one = density_from_ket((0.0j, 1.0 + 0.0j))
    selected_input_mode = (
        input_mode
        if isinstance(input_mode, ResetCalibrationInputMode)
        else ResetCalibrationInputMode(input_mode)
    )
    input_states = (
        (zero, one)
        if selected_input_mode is ResetCalibrationInputMode.VICTIM_DEPENDENT
        else (zero, zero)
    )
    probabilities = tuple(
        model.run(ResetModelRequest(
            input_state=state,
            reset_count=reset_count,
            victim_measurement=victim_measurement,
        )).attacker_one_probability
        for state in input_states
    )
    timestamp = generated_at_utc or datetime.now(timezone.utc).isoformat()
    return ResetProfile(
        profile_id=profile_id,
        profile_version=profile_version,
        model_id=model.model_id,
        model_version=model.model_version,
        model_configuration_id=model.configuration_id,
        fidelity_level=model.fidelity_level,
        provenance=model.provenance,
        model_mode=ResetModelMode(model.model_mode),
        input_mode=selected_input_mode,
        source="simulator_derived",
        reset_count=int(reset_count),
        victim_measurement=bool(victim_measurement),
        one_probability_given_victim_zero=probabilities[0],
        one_probability_given_victim_one=probabilities[1],
        seed_policy=seed_policy,
        generated_at_utc=timestamp,
    )


def run_reset_leakage_calibration(
    model: ResetModel,
    *,
    reset_count: int,
    victim_measurement: bool,
    shot_counts: Sequence[int],
    profile_id: str = "reset_leakage_binary",
    profile_version: str = "1",
    seed_policy: str = "explicit_seed_v1",
    generated_at_utc: str | None = None,
    input_mode: ResetCalibrationInputMode | str = (
        ResetCalibrationInputMode.VICTIM_DEPENDENT
    ),
) -> ResetCalibrationResult:
    """Build one profile and sweep exact binomial leakage metrics.

    Each count observation is distributed as ``Binomial(shots, p_v)``.  The
    calculation is exact for the declared behavioral model and does not use
    the hidden grader case count as a mutual-information sample size.
    """

    counts = tuple(int(value) for value in shot_counts)
    if not counts or any(value < 1 for value in counts):
        raise ValueError("shot_counts must contain positive integers")
    if len(set(counts)) != len(counts):
        raise ValueError("shot_counts must not contain duplicates")

    model_start = perf_counter()
    profile = derive_reset_profile(
        model,
        reset_count=reset_count,
        victim_measurement=victim_measurement,
        profile_id=profile_id,
        profile_version=profile_version,
        seed_policy=seed_policy,
        generated_at_utc=generated_at_utc,
        input_mode=input_mode,
    )
    model_seconds = perf_counter() - model_start
    p_zero = profile.one_probability_given_victim_zero
    p_one = profile.one_probability_given_victim_one
    per_shot_information = binary_input_mutual_information(p_zero, p_one, 1)

    points: list[LeakageCalibrationPoint] = []
    for shots in counts:
        metric_start = perf_counter()
        distribution_zero = _binomial_distribution(shots, p_zero)
        distribution_one = _binomial_distribution(shots, p_one)
        information = _mutual_information_from_conditionals(
            distribution_zero,
            distribution_one,
        )
        accuracy = 0.5 * sum(
            max(left, right)
            for left, right in zip(distribution_zero, distribution_one)
        )
        points.append(LeakageCalibrationPoint(
            shots_per_case=shots,
            per_shot_mutual_information_bits=per_shot_information,
            per_case_mutual_information_bits=information,
            bayes_optimal_accuracy=accuracy,
            attacker_advantage=max(0.0, min(0.5, accuracy - 0.5)),
            metric_compute_seconds=perf_counter() - metric_start,
        ))
    return ResetCalibrationResult(
        profile=profile,
        points=tuple(points),
        model_compute_seconds=model_seconds,
    )


def binary_input_mutual_information(
    one_probability_given_zero: float,
    one_probability_given_one: float,
    shots: int,
) -> float:
    """Return I(V; C_N) for balanced V and binomial count observation C_N."""

    count = int(shots)
    if count < 1:
        raise ValueError("shots must be positive")
    distribution_zero = _binomial_distribution(
        count,
        _probability(one_probability_given_zero, "one_probability_given_zero"),
    )
    distribution_one = _binomial_distribution(
        count,
        _probability(one_probability_given_one, "one_probability_given_one"),
    )
    return _mutual_information_from_conditionals(
        distribution_zero,
        distribution_one,
    )


def calibrate_frequency_threshold(
    one_probability_given_zero: float,
    one_probability_given_one: float,
    shots: int,
) -> FrequencyThresholdCalibration:
    """Return the best deterministic count threshold for balanced victim bits."""

    count = int(shots)
    if count < 1:
        raise ValueError("shots must be positive")
    distribution_zero = _binomial_distribution(
        count,
        _probability(one_probability_given_zero, "one_probability_given_zero"),
    )
    distribution_one = _binomial_distribution(
        count,
        _probability(one_probability_given_one, "one_probability_given_one"),
    )
    candidates = tuple(
        _evaluate_frequency_threshold_distributions(
            distribution_zero,
            distribution_one,
            count_threshold=count_threshold,
            predict_one_when_at_or_above=direction,
        )
        for direction in (True, False)
        for count_threshold in range(count + 1)
    )
    return max(
        candidates,
        key=lambda candidate: (
            candidate.balanced_accuracy,
            candidate.predict_one_when_at_or_above,
            -candidate.count_threshold,
        ),
    )


def evaluate_frequency_threshold(
    calibration: FrequencyThresholdCalibration,
    one_probability_given_zero: float,
    one_probability_given_one: float,
) -> FrequencyThresholdCalibration:
    """Evaluate a calibrated threshold against another conditional profile."""

    shots = calibration.shots_per_case
    return _evaluate_frequency_threshold_distributions(
        _binomial_distribution(
            shots,
            _probability(one_probability_given_zero, "one_probability_given_zero"),
        ),
        _binomial_distribution(
            shots,
            _probability(one_probability_given_one, "one_probability_given_one"),
        ),
        count_threshold=calibration.count_threshold,
        predict_one_when_at_or_above=(
            calibration.predict_one_when_at_or_above
        ),
    )


def hidden_evaluation_pass_probability(
    *,
    zero_case_count: int,
    one_case_count: int,
    required_correct: int,
    accuracy_given_zero: float,
    accuracy_given_one: float,
) -> float:
    """Return the exact pass probability for fixed zero/one hidden cases."""

    zero_cases = int(zero_case_count)
    one_cases = int(one_case_count)
    required = int(required_correct)
    if zero_cases < 1 or one_cases < 1:
        raise ValueError("hidden evaluation must contain both victim bits")
    case_count = zero_cases + one_cases
    if required < 1 or required > case_count:
        raise ValueError("required_correct is outside the hidden case count")
    zero_correct = _binomial_distribution(
        zero_cases,
        _probability(accuracy_given_zero, "accuracy_given_zero"),
    )
    one_correct = _binomial_distribution(
        one_cases,
        _probability(accuracy_given_one, "accuracy_given_one"),
    )
    probability = sum(
        zero_probability * one_probability
        for zero_count, zero_probability in enumerate(zero_correct)
        for one_count, one_probability in enumerate(one_correct)
        if zero_count + one_count >= required
    )
    return max(0.0, min(1.0, probability))


def _evaluate_frequency_threshold_distributions(
    distribution_zero: Sequence[float],
    distribution_one: Sequence[float],
    *,
    count_threshold: int,
    predict_one_when_at_or_above: bool,
) -> FrequencyThresholdCalibration:
    shots = len(distribution_zero) - 1
    if len(distribution_one) != shots + 1:
        raise ValueError("conditional distributions must have equal lengths")
    threshold = int(count_threshold)
    if threshold < 0 or threshold > shots:
        raise ValueError("count_threshold must be between zero and shots")
    below_zero = sum(distribution_zero[:threshold])
    below_one = sum(distribution_one[:threshold])
    if predict_one_when_at_or_above:
        accuracy_zero = below_zero
        accuracy_one = 1.0 - below_one
    else:
        accuracy_zero = 1.0 - below_zero
        accuracy_one = below_one
    return FrequencyThresholdCalibration(
        shots_per_case=shots,
        count_threshold=threshold,
        frequency_threshold=threshold / shots,
        predict_one_when_at_or_above=predict_one_when_at_or_above,
        accuracy_given_zero=accuracy_zero,
        accuracy_given_one=accuracy_one,
        balanced_accuracy=0.5 * (accuracy_zero + accuracy_one),
    )


def _binomial_distribution(shots: int, probability: float) -> tuple[float, ...]:
    if probability == 0.0:
        return (1.0,) + (0.0,) * shots
    if probability == 1.0:
        return (0.0,) * shots + (1.0,)

    log_probability = math.log(probability)
    log_complement = math.log1p(-probability)
    log_values = tuple(
        math.lgamma(shots + 1)
        - math.lgamma(ones + 1)
        - math.lgamma(shots - ones + 1)
        + ones * log_probability
        + (shots - ones) * log_complement
        for ones in range(shots + 1)
    )
    maximum = max(log_values)
    scaled = tuple(math.exp(value - maximum) for value in log_values)
    total = sum(scaled)
    return tuple(value / total for value in scaled)


def _mutual_information_from_conditionals(
    distribution_zero: Sequence[float],
    distribution_one: Sequence[float],
) -> float:
    information = 0.0
    for probability_zero, probability_one in zip(
        distribution_zero,
        distribution_one,
    ):
        marginal = 0.5 * (probability_zero + probability_one)
        if probability_zero > 0.0:
            information += 0.5 * probability_zero * math.log2(
                probability_zero / marginal
            )
        if probability_one > 0.0:
            information += 0.5 * probability_one * math.log2(
                probability_one / marginal
            )
    return max(0.0, min(1.0, information))


def _probability(value: float, name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0 or converted > 1.0:
        raise ValueError(f"{name} must be finite and between zero and one")
    return converted

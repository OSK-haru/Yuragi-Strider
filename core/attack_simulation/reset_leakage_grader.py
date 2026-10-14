"""Aggregate-only hidden grading for the reset-leakage scenario."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import random
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from core.attack_simulation.reset_leakage_scenario import (
    ResetLeakageScenarioOrchestrator,
)


@dataclass(frozen=True)
class BinaryObservation:
    """The complete input exposed to an estimator for one hidden case."""

    shots: int
    counts: Mapping[str, int]

    def __post_init__(self) -> None:
        shots = int(self.shots)
        if shots < 1:
            raise ValueError("shots must be positive")
        if set(self.counts) != {"0", "1"}:
            raise ValueError("counts must contain exactly zero and one outcomes")
        normalized = {
            "0": int(self.counts["0"]),
            "1": int(self.counts["1"]),
        }
        if any(value < 0 for value in normalized.values()):
            raise ValueError("counts must be non-negative")
        if sum(normalized.values()) != shots:
            raise ValueError("counts must sum to shots")
        object.__setattr__(self, "shots", shots)
        object.__setattr__(self, "counts", MappingProxyType(normalized))

    @property
    def one_frequency(self) -> float:
        return self.counts["1"] / self.shots


@runtime_checkable
class BinaryVictimEstimator(Protocol):
    """Trusted internal estimator boundary; not an untrusted-code sandbox."""

    estimator_id: str
    estimator_version: str

    def predict(self, observation: BinaryObservation) -> int:
        """Return a predicted hidden victim bit from attacker-visible data."""


@dataclass(frozen=True)
class FrequencyThresholdEstimator:
    """Minimal estimator that can be trained from labelled observations."""

    threshold: float
    predict_one_when_at_or_above: bool = True
    estimator_id: str = "frequency_threshold"
    estimator_version: str = "1"

    def __post_init__(self) -> None:
        threshold = float(self.threshold)
        if not math.isfinite(threshold) or threshold < 0.0 or threshold > 1.0:
            raise ValueError("threshold must be finite and between zero and one")
        object.__setattr__(self, "threshold", threshold)
        _identifier(self.estimator_id, "estimator_id")
        _identifier(self.estimator_version, "estimator_version")

    def predict(self, observation: BinaryObservation) -> int:
        is_above = observation.one_frequency >= self.threshold
        return int(
            is_above
            if self.predict_one_when_at_or_above
            else not is_above
        )


@dataclass(frozen=True)
class HiddenEvaluationPolicy:
    """Explicit product policy; this module supplies no acceptance defaults."""

    policy_id: str
    policy_version: str
    case_count: int
    zero_case_count: int
    shots_per_case: int
    required_correct: int
    case_order_seed: int
    measurement_seed: int

    def __post_init__(self) -> None:
        _identifier(self.policy_id, "policy_id")
        _identifier(self.policy_version, "policy_version")
        for name in (
            "case_count",
            "zero_case_count",
            "shots_per_case",
            "required_correct",
        ):
            value = int(getattr(self, name))
            if value < 1:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        if self.zero_case_count >= self.case_count:
            raise ValueError("evaluation must contain both victim bits")
        if self.required_correct > self.case_count:
            raise ValueError("required_correct cannot exceed case_count")
        object.__setattr__(
            self,
            "case_order_seed",
            _seed(self.case_order_seed, "case_order_seed"),
        )
        object.__setattr__(
            self,
            "measurement_seed",
            _seed(self.measurement_seed, "measurement_seed"),
        )


@dataclass(frozen=True)
class HiddenEvaluationResult:
    """Aggregate result intentionally containing no case-level evidence."""

    policy_id: str
    policy_version: str
    estimator_id: str
    estimator_version: str
    case_count: int
    correct_count: int
    accuracy: float
    passed: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "estimator_id": self.estimator_id,
            "estimator_version": self.estimator_version,
            "case_count": self.case_count,
            "correct_count": self.correct_count,
            "accuracy": self.accuracy,
            "passed": self.passed,
        }


class EstimatorExecutionError(RuntimeError):
    """Generic failure that does not reveal a hidden case or observation."""


class ResetLeakageHiddenGrader:
    """Runs shuffled hidden cases and exposes only aggregate grading output."""

    def __init__(
        self,
        *,
        orchestrator: ResetLeakageScenarioOrchestrator,
        policy: HiddenEvaluationPolicy,
    ) -> None:
        self.orchestrator = orchestrator
        self.policy = policy
        if policy.shots_per_case > (
            orchestrator.observation_budget.shots_per_query
        ):
            raise ValueError(
                "hidden evaluation shots exceed the scenario shot limit"
            )

    def grade(self, estimator: BinaryVictimEstimator) -> HiddenEvaluationResult:
        if not isinstance(estimator, BinaryVictimEstimator):
            raise TypeError("estimator must satisfy BinaryVictimEstimator")
        estimator_id = _identifier(estimator.estimator_id, "estimator_id")
        estimator_version = _identifier(
            estimator.estimator_version,
            "estimator_version",
        )
        victim_bits = [0] * self.policy.zero_case_count
        victim_bits.extend(
            [1] * (self.policy.case_count - self.policy.zero_case_count)
        )
        random.Random(self.policy.case_order_seed).shuffle(victim_bits)
        measurement_rng = random.Random(self.policy.measurement_seed)

        correct = 0
        for victim_bit in victim_bits:
            case = self.orchestrator._run_hidden_evaluation_case(
                victim_bit=victim_bit,
                shots=self.policy.shots_per_case,
                measurement_seeds=tuple(
                    measurement_rng.getrandbits(32)
                    for _ in range(self.policy.shots_per_case)
                ),
            )
            observation = BinaryObservation(
                shots=case.shots,
                counts=case.counts,
            )
            try:
                prediction = int(estimator.predict(observation))
            except Exception as error:
                raise EstimatorExecutionError(
                    "estimator failed during hidden evaluation"
                ) from error
            if prediction not in (0, 1):
                raise EstimatorExecutionError(
                    "estimator returned an invalid binary prediction"
                )
            correct += int(prediction == victim_bit)

        return HiddenEvaluationResult(
            policy_id=self.policy.policy_id,
            policy_version=self.policy.policy_version,
            estimator_id=estimator_id,
            estimator_version=estimator_version,
            case_count=self.policy.case_count,
            correct_count=correct,
            accuracy=correct / self.policy.case_count,
            passed=correct >= self.policy.required_correct,
        )


def _identifier(value: str, name: str) -> str:
    converted = str(value).strip()
    if not converted:
        raise ValueError(f"{name} must not be empty")
    return converted


def _seed(value: int, name: str) -> int:
    converted = int(value)
    if converted < 0 or converted > 2 ** 32 - 1:
        raise ValueError(f"{name} must be between zero and 2**32 - 1")
    return converted

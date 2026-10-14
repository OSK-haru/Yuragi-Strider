"""Attacker-visible analysis helpers for the reset-leakage exercise."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from core.attack_simulation.reset_leakage_grader import (
    BinaryObservation,
    FrequencyThresholdEstimator,
)


class FrequencyThresholdTrainingError(ValueError):
    """Raised when visible training data cannot identify both classes."""


@dataclass(frozen=True)
class LabeledBinaryObservation:
    """One labelled sample reconstructed only from attacker-visible data."""

    label: int
    shots: int
    counts: Mapping[str, int]

    def __post_init__(self) -> None:
        label = int(self.label)
        if label not in (0, 1):
            raise ValueError("label must be zero or one")
        observation = BinaryObservation(shots=self.shots, counts=self.counts)
        object.__setattr__(self, "label", label)
        object.__setattr__(self, "shots", observation.shots)
        object.__setattr__(self, "counts", observation.counts)

    @classmethod
    def from_attacker_result(
        cls,
        result: Mapping[str, object],
    ) -> "LabeledBinaryObservation":
        """Parse a public training result without accepting hidden fields."""

        if result.get("phase") != "training":
            raise ValueError("only public training results can be labelled")
        try:
            label = result["known_label"]
            shots = result["shots"]
            counts = result["counts"]
        except KeyError as error:
            raise ValueError(
                "training result must contain known_label, shots, and counts"
            ) from error
        if not isinstance(counts, Mapping):
            raise TypeError("training result counts must be a mapping")
        return cls(
            label=int(label),
            shots=int(shots),
            counts={str(key): int(value) for key, value in counts.items()},
        )

    @property
    def one_count(self) -> int:
        return int(self.counts["1"])


@dataclass(frozen=True)
class FrequencyThresholdTrainingResult:
    """Auditable fit result derived from labelled public observations."""

    estimator: FrequencyThresholdEstimator
    observation_count_by_label: Mapping[int, int]
    shots_by_label: Mapping[int, int]
    one_frequency_by_label: Mapping[int, float]

    def __post_init__(self) -> None:
        for name in (
            "observation_count_by_label",
            "shots_by_label",
            "one_frequency_by_label",
        ):
            values = dict(getattr(self, name))
            if set(values) != {0, 1}:
                raise ValueError(f"{name} must contain both binary labels")
            object.__setattr__(self, name, MappingProxyType(values))

    def to_public_dict(self) -> dict[str, object]:
        return {
            "estimator": {
                "estimator_id": self.estimator.estimator_id,
                "estimator_version": self.estimator.estimator_version,
                "threshold": self.estimator.threshold,
                "predict_one_when_at_or_above": (
                    self.estimator.predict_one_when_at_or_above
                ),
            },
            "observation_count_by_label": {
                str(label): value
                for label, value in self.observation_count_by_label.items()
            },
            "shots_by_label": {
                str(label): value
                for label, value in self.shots_by_label.items()
            },
            "one_frequency_by_label": {
                str(label): value
                for label, value in self.one_frequency_by_label.items()
            },
            "provenance": "attacker_visible_labelled_observations",
        }


def fit_frequency_threshold_estimator(
    observations: Iterable[LabeledBinaryObservation],
) -> FrequencyThresholdTrainingResult:
    """Fit a threshold using only counts and labels visible to the attacker."""

    sample_count = {0: 0, 1: 0}
    shots = {0: 0, 1: 0}
    ones = {0: 0, 1: 0}
    for observation in observations:
        if not isinstance(observation, LabeledBinaryObservation):
            raise TypeError(
                "observations must contain LabeledBinaryObservation values"
            )
        label = observation.label
        sample_count[label] += 1
        shots[label] += observation.shots
        ones[label] += observation.one_count

    if sample_count[0] == 0 or sample_count[1] == 0:
        raise FrequencyThresholdTrainingError(
            "training requires observations for both labels"
        )

    frequencies = {
        label: ones[label] / shots[label]
        for label in (0, 1)
    }
    if frequencies[0] == frequencies[1]:
        raise FrequencyThresholdTrainingError(
            "training observations contain no class separation"
        )

    estimator = FrequencyThresholdEstimator(
        threshold=(frequencies[0] + frequencies[1]) / 2.0,
        predict_one_when_at_or_above=frequencies[1] > frequencies[0],
        estimator_id="trained_frequency_threshold",
        estimator_version="1",
    )
    return FrequencyThresholdTrainingResult(
        estimator=estimator,
        observation_count_by_label=sample_count,
        shots_by_label=shots,
        one_frequency_by_label=frequencies,
    )

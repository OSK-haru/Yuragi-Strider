"""Attacker-visible analysis helpers for the reset-leakage exercise."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from core.attack_simulation.reset_leakage_grader import (
    BinaryObservation,
    FrequencyThresholdEstimator,
)
from core.attack_simulation.reset_leakage_path import RESET_LEAKAGE_MECHANISM
from core.security_analysis import (
    BinaryContrastReport,
    BinaryContrastRequest,
    ObservationSource,
    analyze_binary_contrast,
)
from core.rust_dense_kernel import rust_reset_training_analysis


class FrequencyThresholdTrainingError(ValueError):
    """Raised when visible training data cannot identify both classes."""


def analyze_public_training_results(
    results: Iterable[Mapping[str, object] | LabeledBinaryObservation],
    *,
    manifest_id: str,
    mechanism_version: str,
    model_configuration_id: str,
    dataset_id: str,
    measurement_id: str,
    assumption_ids: tuple[str, ...] = (),
    measurement_conditions: Mapping[str, str] | None = None,
    backend: str = "rust",
) -> BinaryContrastReport:
    """Analyze labelled training counts obtained from an authorized projection.

    Entries can be labelled observations assembled from an authorized job
    result, or public training-result mappings. A phase field is a format
    check, not proof of authorization. The caller must supply only its own
    public training results and accurate provenance.
    Counts are pooled by label; the standard error assumes independent shots.
    This descriptive report is not the hidden challenge grader's score.
    """
    observations: list[LabeledBinaryObservation] = []
    for result in results:
        if isinstance(result, LabeledBinaryObservation):
            observation = result
        else:
            if not isinstance(result, Mapping):
                raise TypeError("training results must be mappings or labelled observations")
            counts = result.get("counts")
            if (type(result.get("known_label")) is not int
                    or type(result.get("shots")) is not int
                    or not isinstance(counts, Mapping)
                    or set(counts) != {"0", "1"}
                    or any(type(value) is not int for value in counts.values())):
                raise ValueError("training results require exact integer labels, shots and counts")
            observation = LabeledBinaryObservation.from_attacker_result(result)
        observations.append(observation)
    if backend not in ("rust", "python"):
        raise ValueError("backend must be 'rust' or 'python'")
    if backend == "rust":
        try:
            summary = rust_reset_training_analysis([
                (item.label, item.shots, item.one_count) for item in observations
            ])
        except ValueError as error:
            if "both labels" in str(error):
                raise FrequencyThresholdTrainingError(
                    "training requires observations for both labels"
                ) from error
            raise
        _, _, shots_0, ones_0, shots_1, ones_1, _, _, p0, p1, difference, standard_error, z_score, mutual_information, bayes_accuracy = summary
        shots = [shots_0, shots_1]
    else:
        shots = [0, 0]
        ones = [0, 0]
        for item in observations:
            shots[item.label] += item.shots
            ones[item.label] += item.one_count
        if not all(shots):
            raise FrequencyThresholdTrainingError("training requires observations for both labels")
    request = BinaryContrastRequest(
        manifest_id=manifest_id,
        mechanism_id=RESET_LEAKAGE_MECHANISM,
        mechanism_version=mechanism_version,
        model_configuration_id=model_configuration_id,
        dataset_id=dataset_id,
        measurement_id=measurement_id,
        class_0_id="known_label_0",
        class_1_id="known_label_1",
        outcome_id="attacker_measured_one",
        source=ObservationSource.ACTOR_VISIBLE,
        trials_0=shots[0], outcome_one_0=(ones_0 if backend == "rust" else ones[0]),
        trials_1=shots[1], outcome_one_1=(ones_1 if backend == "rust" else ones[1]),
        assumption_ids=assumption_ids,
        measurement_conditions=measurement_conditions if measurement_conditions is not None else {},
    )
    if backend == "rust":
        return BinaryContrastReport(
            request, (p0, p1), difference, standard_error, z_score,
            mutual_information, bayes_accuracy,
        )
    return analyze_binary_contrast(request, backend="python")


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
    *,
    backend: str = "rust",
) -> FrequencyThresholdTrainingResult:
    """Fit a threshold using only counts and labels visible to the attacker."""

    values = tuple(observations)
    if any(not isinstance(observation, LabeledBinaryObservation) for observation in values):
        raise TypeError("observations must contain LabeledBinaryObservation values")
    if backend not in ("rust", "python"):
        raise ValueError("backend must be 'rust' or 'python'")
    if backend == "rust":
        try:
            (count_0, count_1, shots_0, ones_0, shots_1, ones_1,
             threshold, direction, p0, p1, *_) = rust_reset_training_analysis([
                (item.label, item.shots, item.one_count) for item in values
            ])
        except ValueError as error:
            if "both labels" in str(error) or "no class separation" in str(error):
                raise FrequencyThresholdTrainingError(str(error)) from error
            raise
        if p0 == p1:
            raise FrequencyThresholdTrainingError(
                "training observations contain no class separation"
            )
        return FrequencyThresholdTrainingResult(
            FrequencyThresholdEstimator(
                threshold=threshold,
                predict_one_when_at_or_above=direction,
                estimator_id="trained_frequency_threshold",
                estimator_version="1",
            ),
            {0: count_0, 1: count_1},
            {0: shots_0, 1: shots_1},
            {0: p0, 1: p1},
        )

    sample_count = {0: 0, 1: 0}
    shots = {0: 0, 1: 0}
    ones = {0: 0, 1: 0}
    for observation in observations:
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

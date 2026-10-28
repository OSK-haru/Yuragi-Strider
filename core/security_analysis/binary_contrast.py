"""Versioned binary-observation analysis shared across security mechanisms.

This consumes aggregated participant-visible or service-published observations.
It is a local analysis contract, not an authentication boundary: a future API
adapter must build requests only from the caller's authorized result projection.
Mechanism-specific state and acceptance policies remain outside this contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from collections.abc import Mapping
from types import MappingProxyType

from core.rust_dense_kernel import rust_binary_contrast_statistics

BINARY_CONTRAST_SCHEMA_VERSION = "security_binary_contrast_v1"
_MAX_EXACT_COUNT = 2**53


class ObservationSource(str, Enum):
    ACTOR_VISIBLE = "actor_visible"
    SERVICE_PUBLISHED = "service_published"


@dataclass(frozen=True)
class BinaryContrastRequest:
    """Reproducible metadata and two binary outcome groups for local analysis."""

    manifest_id: str
    mechanism_id: str
    mechanism_version: str
    model_configuration_id: str
    dataset_id: str
    measurement_id: str
    class_0_id: str
    class_1_id: str
    outcome_id: str
    source: ObservationSource
    trials_0: int
    outcome_one_0: int
    trials_1: int
    outcome_one_1: int
    assumption_ids: tuple[str, ...] = ()
    measurement_conditions: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        for name in (
            "manifest_id", "mechanism_id", "mechanism_version",
            "model_configuration_id", "dataset_id", "measurement_id",
            "class_0_id", "class_1_id", "outcome_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.class_0_id == self.class_1_id:
            raise ValueError("binary class identifiers must be distinct")
        source = self.source if isinstance(self.source, ObservationSource) else ObservationSource(self.source)
        object.__setattr__(self, "source", source)
        for group in (0, 1):
            trials = getattr(self, f"trials_{group}")
            successes = getattr(self, f"outcome_one_{group}")
            if type(trials) is not int or not 1 <= trials <= _MAX_EXACT_COUNT:
                raise ValueError("trial counts must be positive exactly representable integers")
            if type(successes) is not int or not 0 <= successes <= trials:
                raise ValueError("outcome-one counts must be between zero and trials")
        assumptions = tuple(_text(item, "assumption_id") for item in self.assumption_ids)
        if len(set(assumptions)) != len(assumptions):
            raise ValueError("assumption_ids must not contain duplicates")
        object.__setattr__(self, "assumption_ids", tuple(sorted(assumptions)))
        if not isinstance(self.measurement_conditions, Mapping) or len(self.measurement_conditions) > 256:
            raise ValueError("measurement_conditions must be a bounded string mapping")
        conditions: dict[str, str] = {}
        for key, value in self.measurement_conditions.items():
            normalized_key = _text(key, "measurement condition key")
            if normalized_key in conditions:
                raise ValueError("measurement condition keys must be unique")
            conditions[normalized_key] = _text(value, f"measurement_conditions.{normalized_key}")
        object.__setattr__(self, "measurement_conditions", MappingProxyType(conditions))

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "BinaryContrastRequest":
        """Parse the exact v1 shape; unknown fields are rejected."""

        expected = {
            "schema_version", "manifest_id", "mechanism_id", "mechanism_version",
            "model_configuration_id", "dataset_id", "measurement_id", "class_0_id",
            "class_1_id", "outcome_id", "source", "trials_0", "outcome_one_0",
            "trials_1", "outcome_one_1", "assumption_ids",
            "measurement_conditions",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("binary contrast request has missing or unknown fields")
        if value["schema_version"] != BINARY_CONTRAST_SCHEMA_VERSION:
            raise ValueError("unsupported binary contrast schema version")
        assumptions = value["assumption_ids"]
        if type(assumptions) not in (tuple, list) or any(type(item) is not str for item in assumptions):
            raise ValueError("assumption_ids must be a string array")
        conditions = value["measurement_conditions"]
        if not isinstance(conditions, Mapping):
            raise ValueError("measurement_conditions must be a string mapping")
        return cls(
            manifest_id=value["manifest_id"],
            mechanism_id=value["mechanism_id"],
            mechanism_version=value["mechanism_version"],
            model_configuration_id=value["model_configuration_id"],
            dataset_id=value["dataset_id"],
            measurement_id=value["measurement_id"],
            class_0_id=value["class_0_id"],
            class_1_id=value["class_1_id"],
            outcome_id=value["outcome_id"],
            source=value["source"],
            trials_0=value["trials_0"],
            outcome_one_0=value["outcome_one_0"],
            trials_1=value["trials_1"],
            outcome_one_1=value["outcome_one_1"],
            assumption_ids=tuple(assumptions),
            measurement_conditions=conditions,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": BINARY_CONTRAST_SCHEMA_VERSION,
            "manifest_id": self.manifest_id,
            "mechanism_id": self.mechanism_id,
            "mechanism_version": self.mechanism_version,
            "model_configuration_id": self.model_configuration_id,
            "dataset_id": self.dataset_id,
            "measurement_id": self.measurement_id,
            "class_0_id": self.class_0_id,
            "class_1_id": self.class_1_id,
            "outcome_id": self.outcome_id,
            "source": self.source.value,
            "trials_0": self.trials_0,
            "outcome_one_0": self.outcome_one_0,
            "trials_1": self.trials_1,
            "outcome_one_1": self.outcome_one_1,
            "assumption_ids": list(self.assumption_ids),
            "measurement_conditions": dict(self.measurement_conditions),
        }


@dataclass(frozen=True)
class BinaryContrastReport:
    request: BinaryContrastRequest
    probability_one_by_class: tuple[float, float]
    difference_class_1_minus_class_0: float
    standard_error: float
    z_score: float | None
    balanced_prior_mutual_information_bits: float
    balanced_prior_bayes_accuracy: float

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": BINARY_CONTRAST_SCHEMA_VERSION,
            "manifest_id": self.request.manifest_id,
            "mechanism_id": self.request.mechanism_id,
            "mechanism_version": self.request.mechanism_version,
            "model_configuration_id": self.request.model_configuration_id,
            "dataset_id": self.request.dataset_id,
            "measurement_id": self.request.measurement_id,
            "source": self.request.source.value,
            "class_0_id": self.request.class_0_id,
            "class_1_id": self.request.class_1_id,
            "outcome_id": self.request.outcome_id,
            "assumption_ids": list(self.request.assumption_ids),
            "measurement_conditions": dict(self.request.measurement_conditions),
            "trials": [self.request.trials_0, self.request.trials_1],
            "outcome_one_counts": [self.request.outcome_one_0, self.request.outcome_one_1],
            "probability_one_by_class": list(self.probability_one_by_class),
            "difference_class_1_minus_class_0": self.difference_class_1_minus_class_0,
            "standard_error": self.standard_error,
            "z_score": self.z_score,
            "balanced_prior_mutual_information_bits": self.balanced_prior_mutual_information_bits,
            "balanced_prior_bayes_accuracy": self.balanced_prior_bayes_accuracy,
        }


def analyze_binary_contrast(
    request: BinaryContrastRequest,
    *,
    backend: str = "python",
) -> BinaryContrastReport:
    """Compute generic metrics without deciding mechanism-specific pass criteria."""

    if not isinstance(request, BinaryContrastRequest):
        raise TypeError("request must be BinaryContrastRequest")
    if backend not in ("python", "rust"):
        raise ValueError("backend must be 'python' or 'rust'")
    if backend == "rust":
        values = rust_binary_contrast_statistics(
            BINARY_CONTRAST_SCHEMA_VERSION,
            request.source.value,
            request.trials_0,
            request.outcome_one_0,
            request.trials_1,
            request.outcome_one_1,
        )
    else:
        values = _python_statistics(request)
    return BinaryContrastReport(request, values[:2], *values[2:])


def _python_statistics(request: BinaryContrastRequest) -> tuple[float, float, float, float, float | None, float, float]:
    p0 = request.outcome_one_0 / request.trials_0
    p1 = request.outcome_one_1 / request.trials_1
    difference = p1 - p0
    standard_error = math.sqrt(
        p0 * (1.0 - p0) / request.trials_0
        + p1 * (1.0 - p1) / request.trials_1
    )
    z_score = difference / standard_error if standard_error > 0.0 else None
    mutual_information = (
        _binary_entropy((p0 + p1) / 2.0)
        - 0.5 * _binary_entropy(p0)
        - 0.5 * _binary_entropy(p1)
    )
    bayes_accuracy = 0.5 * (max(p0, p1) + max(1.0 - p0, 1.0 - p1))
    return p0, p1, difference, standard_error, z_score, mutual_information, bayes_accuracy


def _binary_entropy(probability: float) -> float:
    result = 0.0
    if probability > 0.0:
        result -= probability * math.log2(probability)
    complement = 1.0 - probability
    if complement > 0.0:
        result -= complement * math.log2(complement)
    return result


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()

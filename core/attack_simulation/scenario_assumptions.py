"""Provenance records shared by scenario presets and resolved manifests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType

from core.attack_simulation.config_limits import MAX_METADATA_ITEMS, MAX_TEXT_BYTES, check_json_bounds


class AssumptionBasis(str, Enum):
    SERVICE_DOCUMENTED = "service_documented"
    MEASURED = "measured"
    PAPER_ASSUMPTION = "paper_assumption"
    SIMULATOR_CHOICE = "simulator_choice"
    USER_OVERRIDE = "user_override"


@dataclass(frozen=True)
class AssumptionRecord:
    """Provenance annotation that references configuration without copying it."""

    assumption_id: str
    target_paths: tuple[str, ...]
    basis: AssumptionBasis
    rationale: str
    evidence_ref: str | None = None
    applicability: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "assumption_id", _text(self.assumption_id, "assumption_id"))
        if len(self.target_paths) > MAX_METADATA_ITEMS:
            raise ValueError("target_paths exceeds the field limit")
        paths = tuple(_configuration_path(path) for path in self.target_paths)
        if not paths:
            raise ValueError("target_paths must not be empty")
        if len(set(paths)) != len(paths):
            raise ValueError("target_paths must not contain duplicates")
        object.__setattr__(self, "target_paths", paths)
        basis = self.basis if isinstance(self.basis, AssumptionBasis) else AssumptionBasis(self.basis)
        object.__setattr__(self, "basis", basis)
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))
        if self.evidence_ref is not None:
            object.__setattr__(self, "evidence_ref", _text(self.evidence_ref, "evidence_ref"))
        check_json_bounds(self.applicability, "applicability")
        object.__setattr__(self, "applicability", _freeze_string_mapping(self.applicability))

    def to_dict(self) -> dict[str, object]:
        return {
            "assumption_id": self.assumption_id,
            "target_paths": list(self.target_paths),
            "basis": self.basis.value,
            "rationale": self.rationale,
            "evidence_ref": self.evidence_ref,
            "applicability": dict(self.applicability),
        }


def _configuration_path(value: str) -> str:
    converted = _text(value, "configuration path")
    roots = ("attacker.", "victim.", "qpu_service.", "relation.", "experiment.", "policy.")
    if not converted.startswith(roots) or converted.endswith("."):
        raise ValueError("configuration path uses an unknown root")
    if any(not segment for segment in converted.split(".")):
        raise ValueError("configuration path contains an empty segment")
    return converted


def _text(value: str, name: str) -> str:
    converted = str(value).strip()
    if not converted:
        raise ValueError(f"{name} must not be empty")
    if len(converted.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError(f"{name} exceeds the byte limit")
    return converted


def _freeze_string_mapping(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("applicability must be a mapping")
    frozen: dict[str, str] = {}
    for key, item in value.items():
        normalized_key = _text(key, "applicability key")
        if normalized_key in frozen:
            raise ValueError("applicability contains duplicate normalized keys")
        frozen[normalized_key] = _text(item, f"applicability.{normalized_key}")
    return MappingProxyType(frozen)

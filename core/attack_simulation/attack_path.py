"""Declared causal path between an attacker operation and a victim asset.

The attacker entry and attacker observation are separate: information can flow
from the victim's state back to the attacker, as in incomplete-reset leakage.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest


@dataclass(frozen=True)
class AttackPathDeclaration:
    attacker_entry: str
    shared_resource: str
    mechanism_id: str
    victim_asset: str
    attacker_observation: str

    @classmethod
    def from_manifest(cls, manifest: ResolvedScenarioManifest) -> "AttackPathDeclaration":
        raw = manifest.relation_config.get("attack_path")
        if not isinstance(raw, Mapping):
            raise ValueError("relation_config.attack_path must be configured")
        fields = ("attacker_entry", "shared_resource", "mechanism_id",
                  "victim_asset", "attacker_observation")
        values = {}
        for field in fields:
            value = raw.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"relation_config.attack_path.{field} is required")
            values[field] = value.strip()
        path = cls(**values)
        supported = manifest.qpu_service_config.get("supported_attack_mechanisms")
        if not isinstance(supported, (tuple, list)) or not all(
            isinstance(item, str) for item in supported
        ):
            raise ValueError("qpu_service_config.supported_attack_mechanisms is required")
        if path.mechanism_id not in supported:
            raise ValueError("attack path mechanism is not supported by the service")
        return path


RESET_LEAKAGE_MECHANISM = "incomplete_reset_state_carryover"


def reset_leakage_attack_path_config() -> dict[str, str]:
    """Authoring values for the first shared-QPU profile, not a universal path."""

    return {
        "attacker_entry": "job_submission",
        "shared_resource": "same_physical_qubit",
        "mechanism_id": RESET_LEAKAGE_MECHANISM,
        "victim_asset": "nonpublic_output_bit",
        "attacker_observation": "own_measurement_counts",
    }

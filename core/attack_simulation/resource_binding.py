"""Single-QPU resource binding for the initial reset-leakage profile."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest


@dataclass(frozen=True)
class PhysicalQubitRef:
    qpu_id: str
    physical_qubit_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.qpu_id, str) or not self.qpu_id.strip():
            raise ValueError("qpu_id must be a nonempty string")
        if type(self.physical_qubit_id) is not int or self.physical_qubit_id < 0:
            raise ValueError("physical_qubit_id must be a non-negative integer")


@dataclass(frozen=True)
class PhysicalQubitLayout:
    """Server-owned logical-index -> physical-ID mapping for a program."""

    qpu_id: str
    physical_qubit_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        qubits = tuple(self.physical_qubit_ids)
        if not isinstance(self.qpu_id, str) or not self.qpu_id.strip():
            raise ValueError("qpu_id must be a nonempty string")
        if (not qubits or len(set(qubits)) != len(qubits)
                or any(type(q) is not int or q < 0 for q in qubits)):
            raise ValueError("layout qubits must be unique non-negative physical IDs")
        object.__setattr__(self, "physical_qubit_ids", qubits)


@dataclass(frozen=True)
class ResetResourceBinding:
    victim: PhysicalQubitRef
    attacker: PhysicalQubitRef

    @classmethod
    def from_manifest(
        cls,
        manifest: ResolvedScenarioManifest,
        placement: PlacementPolicy,
    ) -> "ResetResourceBinding":
        raw = manifest.relation_config.get("resource_binding")
        if not isinstance(raw, Mapping):
            raise ValueError("relation_config.resource_binding must be configured")
        refs = {}
        for role in ("victim", "attacker"):
            entry = raw.get(role)
            if not isinstance(entry, Mapping):
                raise ValueError(f"relation_config.resource_binding.{role} is required")
            qpu_id = entry.get("qpu_id")
            qubit_id = entry.get("physical_qubit_id")
            refs[role] = PhysicalQubitRef(qpu_id, qubit_id)
        binding = cls(**refs)
        if binding.victim.qpu_id != binding.attacker.qpu_id:
            raise ValueError("the reset-leakage runtime supports only one QPU")
        inventory = manifest.qpu_service_config.get("resource_inventory")
        if not isinstance(inventory, Mapping):
            raise ValueError("qpu_service_config.resource_inventory must be configured")
        if inventory.get("qpu_id") != binding.victim.qpu_id:
            raise ValueError("resource binding QPU ID does not match service inventory")
        available = inventory.get("physical_qubit_ids")
        if (not isinstance(available, (tuple, list)) or not available
                or any(type(value) is not int or value < 0 for value in available)
                or len(set(available)) != len(available)):
            raise ValueError("service inventory physical_qubit_ids must be unique non-negative IDs")
        if (binding.victim.physical_qubit_id not in available
                or binding.attacker.physical_qubit_id not in available):
            raise ValueError("resource binding qubit ID is absent from service inventory")
        same = binding.victim == binding.attacker
        if placement is PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT and not same:
            raise ValueError("same-qubit placement requires identical resource IDs")
        if placement is PlacementPolicy.ISOLATED_PHYSICAL_RESOURCE and same:
            raise ValueError("isolated placement requires distinct physical qubit IDs")
        return binding


def reset_resource_binding_config(
    qpu_id: str,
    *,
    isolated: bool = False,
    victim_qubit_id: int = 0,
    attacker_qubit_id: int | None = None,
) -> dict[str, dict[str, str | int]]:
    if attacker_qubit_id is None:
        attacker_qubit_id = victim_qubit_id + 1 if isolated else victim_qubit_id
    return {
        "victim": {"qpu_id": qpu_id, "physical_qubit_id": victim_qubit_id},
        "attacker": {"qpu_id": qpu_id, "physical_qubit_id": attacker_qubit_id},
    }

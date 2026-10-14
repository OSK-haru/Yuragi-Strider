"""Compile job submission grants from service, actor, and scenario policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest
from core.attack_simulation.scenario_policy import ScenarioMode


@dataclass(frozen=True)
class JobAuthorizationPolicy:
    service_operations: frozenset[str]
    scenario_operations: frozenset[str]
    actor_grants: Mapping[str, frozenset[str]]
    tenant_roles: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "service_operations", frozenset(self.service_operations))
        object.__setattr__(self, "scenario_operations", frozenset(self.scenario_operations))
        object.__setattr__(self, "actor_grants", MappingProxyType({
            role: frozenset(operations)
            for role, operations in self.actor_grants.items()
        }))
        object.__setattr__(self, "tenant_roles", MappingProxyType(dict(self.tenant_roles)))

    @classmethod
    def from_manifest(
        cls,
        manifest: ResolvedScenarioManifest,
        *,
        attacker_tenant_id: str,
        victim_tenant_id: str,
    ) -> "JobAuthorizationPolicy":
        if manifest.allowed_operations is None:
            raise ValueError("ScenarioPolicy.allowed_operations must be explicit")
        service_operations = _operation_set(
            manifest.qpu_service_config.get("supported_operations"),
            "qpu_service_config.supported_operations",
        )
        attacker_grants = _operation_set(
            manifest.attacker_config.get("operation_grants"),
            "attacker_config.operation_grants",
            allow_empty=True,
        )
        victim_grants = _operation_set(
            manifest.victim_config.get("operation_grants"),
            "victim_config.operation_grants",
            allow_empty=True,
        )
        roles = {attacker_tenant_id: "attacker", victim_tenant_id: "victim"}
        if attacker_tenant_id == victim_tenant_id:
            raise ValueError("attacker and victim tenants must be distinct")
        if manifest.mode is ScenarioMode.CHALLENGE:
            required_locks = (
                "attacker.operation_grants",
                "victim.operation_grants",
                "qpu_service.supported_operations",
            )
            for path in required_locks:
                if not any(rule == path or path.startswith(f"{rule}.")
                           for rule in manifest.locked_fields):
                    raise ValueError(f"graded job permission must be locked: {path}")
        return cls(
            service_operations=service_operations,
            scenario_operations=frozenset(manifest.allowed_operations),
            actor_grants={"attacker": attacker_grants, "victim": victim_grants},
            tenant_roles=roles,
        )

    def authorize(self, tenant_id: str, operation_id: str) -> None:
        role = self.tenant_roles.get(tenant_id)
        if role is None:
            raise PermissionError("tenant has no actor role in this scenario")
        if operation_id not in self.service_operations:
            raise PermissionError("operation is not provided by the service profile")
        if operation_id not in self.actor_grants.get(role, frozenset()):
            raise PermissionError("actor role is not granted this operation")
        if operation_id not in self.scenario_operations:
            raise PermissionError("operation is not allowed by the scenario policy")

    def with_tenants(
        self,
        *,
        attacker_tenant_id: str,
        victim_tenant_id: str,
    ) -> "JobAuthorizationPolicy":
        if not attacker_tenant_id or not victim_tenant_id:
            raise ValueError("actor tenant IDs must be nonempty")
        if attacker_tenant_id == victim_tenant_id:
            raise ValueError("attacker and victim tenants must be distinct")
        return JobAuthorizationPolicy(
            service_operations=self.service_operations,
            scenario_operations=self.scenario_operations,
            actor_grants=self.actor_grants,
            tenant_roles={attacker_tenant_id: "attacker", victim_tenant_id: "victim"},
        )


def _operation_set(value: object, name: str, *, allow_empty: bool = False) -> frozenset[str]:
    if (not isinstance(value, (tuple, list)) or (not value and not allow_empty)
            or any(not isinstance(item, str) or not item.strip() for item in value)):
        qualifier = "" if allow_empty else "nonempty "
        raise ValueError(f"{name} must be a {qualifier}operation list")
    operations = tuple(item.strip() for item in value)
    if len(set(operations)) != len(operations):
        raise ValueError(f"{name} must not contain duplicate operations")
    return frozenset(operations)

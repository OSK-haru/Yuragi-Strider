"""Compile job submission grants from service, actor, and scenario policy."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest
from core.attack_simulation.scenario_policy import ScenarioMode


@dataclass(frozen=True, order=True)
class ActorRole:
    """Registered scenario actor role; the name has no authorization meaning by itself."""

    role_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.role_id, str) or not self.role_id.strip():
            raise ValueError("role_id must be a nonempty string")
        object.__setattr__(self, "role_id", self.role_id.strip())


ATTACKER_ROLE = ActorRole("attacker")
VICTIM_ROLE = ActorRole("victim")


class ActorGrantRegistry(Mapping[ActorRole, frozenset[str]]):
    """Immutable role registry with legacy string lookup for scenario adapters."""

    def __init__(self, entries: Mapping[ActorRole | str, frozenset[str]]) -> None:
        self._entries = MappingProxyType({_actor_role(role): frozenset(grants)
                                          for role, grants in entries.items()})

    def __getitem__(self, role: ActorRole | str) -> frozenset[str]:
        return self._entries[_actor_role(role)]

    def __iter__(self) -> Iterator[ActorRole]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(frozen=True)
class JobAuthorizationPolicy:
    service_operations: frozenset[str]
    scenario_operations: frozenset[str]
    actor_grants: Mapping[ActorRole, frozenset[str]]
    tenant_roles: Mapping[str, ActorRole]

    def __post_init__(self) -> None:
        object.__setattr__(self, "service_operations", frozenset(self.service_operations))
        object.__setattr__(self, "scenario_operations", frozenset(self.scenario_operations))
        object.__setattr__(self, "actor_grants", ActorGrantRegistry(self.actor_grants))
        object.__setattr__(self, "tenant_roles", MappingProxyType({
            tenant: _actor_role(role) for tenant, role in self.tenant_roles.items()
        }))

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
        roles = {attacker_tenant_id: ATTACKER_ROLE, victim_tenant_id: VICTIM_ROLE}
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
            actor_grants={ATTACKER_ROLE: attacker_grants, VICTIM_ROLE: victim_grants},
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
            tenant_roles={attacker_tenant_id: ATTACKER_ROLE, victim_tenant_id: VICTIM_ROLE},
        )

    def with_actor(self, tenant_id: str, role: ActorRole | str,
                   grants: frozenset[str] | set[str] | tuple[str, ...]) -> "JobAuthorizationPolicy":
        """Return a policy with one explicitly registered role-to-tenant grant set."""
        actor = _actor_role(role)
        if not isinstance(tenant_id, str) or not tenant_id.strip() or tenant_id in self.tenant_roles:
            raise ValueError("actor tenant ID must be nonempty and not already registered")
        registered = dict(self.actor_grants)
        actor_grants = frozenset(grants)
        if any(not isinstance(item, str) or not item.strip() for item in actor_grants):
            raise ValueError("registered grants must be nonempty operation IDs")
        if actor in registered and registered[actor] != actor_grants:
            raise ValueError("an ActorRole cannot be registered with conflicting grants")
        registered[actor] = actor_grants
        tenants = dict(self.tenant_roles)
        tenants[tenant_id] = actor
        return JobAuthorizationPolicy(self.service_operations, self.scenario_operations,
                                      registered, tenants)


def _actor_role(value: ActorRole | str) -> ActorRole:
    return value if isinstance(value, ActorRole) else ActorRole(value)


def _operation_set(value: object, name: str, *, allow_empty: bool = False) -> frozenset[str]:
    if (not isinstance(value, (tuple, list)) or (not value and not allow_empty)
            or any(not isinstance(item, str) or not item.strip() for item in value)):
        qualifier = "" if allow_empty else "nonempty "
        raise ValueError(f"{name} must be a {qualifier}operation list")
    operations = tuple(item.strip() for item in value)
    if len(set(operations)) != len(operations):
        raise ValueError(f"{name} must not contain duplicate operations")
    return frozenset(operations)

"""Policy overlay shared by free and challenge scenario sessions.

Both modes materialize the same :class:`ScenarioTemplate`.  Challenge mode
adds locks, hidden fields, an observation budget, and an evaluator.  Free mode
removes that CTF overlay without switching to a different simulator or set of
physical models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from core.attack_simulation.config_limits import (
    MAX_METADATA_ITEMS,
    MAX_TEXT_BYTES,
    check_json_bounds,
)
from core.attack_simulation.scenario_assumptions import AssumptionRecord


class ScenarioMode(str, Enum):
    FREE = "free"
    CHALLENGE = "challenge"


@dataclass(frozen=True)
class ObservationBudget:
    """Limits for paired training/victim queries, not a universal job budget.

    The Reset exercise uses this observation channel. Other scenarios need not
    expose it; service/host resource limits are enforced independently.
    """

    shots_per_query: int
    training_query_limit: int
    victim_observation_limit: int
    retry_limit: int = 0

    def __post_init__(self) -> None:
        for name in (
            "shots_per_query",
            "training_query_limit",
            "victim_observation_limit",
            "retry_limit",
        ):
            value = int(getattr(self, name))
            minimum = 1 if name == "shots_per_query" else 0
            if value < minimum:
                raise ValueError(f"{name} must be at least {minimum}")
            object.__setattr__(self, name, value)

    def require_within(
        self,
        *,
        shots: int,
        training_queries: int = 0,
        victim_observations: int = 0,
        retries: int = 0,
    ) -> None:
        limits = {
            "shots": (shots, self.shots_per_query),
            "training_queries": (training_queries, self.training_query_limit),
            "victim_observations": (
                victim_observations,
                self.victim_observation_limit,
            ),
            "retries": (retries, self.retry_limit),
        }
        for name, (requested, limit) in limits.items():
            value = int(requested)
            minimum = 1 if name == "shots" else 0
            if value < minimum or value > limit:
                raise ValueError(f"{name} is outside the observation budget")


@dataclass(frozen=True)
class ScenarioTemplate:
    """Mode-independent authoring configuration before scenario compilation."""

    template_id: str
    template_version: str
    attacker_config: Mapping[str, Any]
    victim_config: Mapping[str, Any]
    qpu_service_config: Mapping[str, Any]
    relation_config: Mapping[str, Any] = field(default_factory=dict)
    experiment_config: Mapping[str, Any] = field(default_factory=dict)
    free_override_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.template_id, "template_id")
        _identifier(self.template_version, "template_version")
        check_json_bounds({
            "attacker": self.attacker_config,
            "victim": self.victim_config,
            "qpu_service": self.qpu_service_config,
            "relation": self.relation_config,
            "experiment": self.experiment_config,
        }, "scenario template")
        for name in (
            "attacker_config",
            "victim_config",
            "qpu_service_config",
            "relation_config",
            "experiment_config",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping")
            object.__setattr__(self, name, _freeze_mapping(value, name))
        if len(self.free_override_fields) > MAX_METADATA_ITEMS:
            raise ValueError("free_override_fields exceeds the field limit")
        editable = _field_paths(self.free_override_fields, "free_override_fields")
        roots = {
            "attacker": self.attacker_config,
            "victim": self.victim_config,
            "qpu_service": self.qpu_service_config,
            "relation": self.relation_config,
            "experiment": self.experiment_config,
        }
        for path in editable:
            root, *segments = path.split(".")
            current: Any = roots[root]
            for segment in segments:
                if not isinstance(current, Mapping) or segment not in current:
                    raise ValueError(f"free_override_fields path does not exist: {path}")
                current = current[segment]
        object.__setattr__(self, "free_override_fields", editable)

    def allows_free_override(self, field_path: str) -> bool:
        path = _field_path(field_path)
        return any(_path_matches(path, rule) for rule in self.free_override_fields)

    def to_dict(self) -> dict[str, object]:
        return {
            "template_id": self.template_id,
            "template_version": self.template_version,
            "attacker_config": _thaw(self.attacker_config),
            "victim_config": _thaw(self.victim_config),
            "qpu_service_config": _thaw(self.qpu_service_config),
            "relation_config": _thaw(self.relation_config),
            "experiment_config": _thaw(self.experiment_config),
            "free_override_fields": list(self.free_override_fields),
        }


@dataclass(frozen=True)
class ScenarioPolicy:
    """Mode overlay controlling editability, visibility, budgets, and grading."""

    mode: ScenarioMode
    observation_budget: ObservationBudget | None = None
    locked_fields: tuple[str, ...] = ()
    hidden_fields: tuple[str, ...] = ()
    evaluator_id: str | None = None
    submission_enabled: bool = False
    reveal_individual_evaluations: bool = False
    allowed_operations: tuple[str, ...] | None = None
    editable_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        mode = self.mode if isinstance(self.mode, ScenarioMode) else ScenarioMode(self.mode)
        if self.observation_budget is not None and not isinstance(self.observation_budget, ObservationBudget):
            raise TypeError("observation_budget must be an ObservationBudget or None")
        if (
            len(self.locked_fields) > MAX_METADATA_ITEMS
            or len(self.hidden_fields) > MAX_METADATA_ITEMS
            or len(self.editable_fields) > MAX_METADATA_ITEMS
        ):
            raise ValueError("scenario policy exceeds the field limit")
        locked = _field_paths(self.locked_fields, "locked_fields")
        hidden = _field_paths(self.hidden_fields, "hidden_fields")
        editable = _field_paths(self.editable_fields, "editable_fields")
        if not all(any(_path_matches(path, rule) for rule in locked) for path in hidden):
            raise ValueError("hidden_fields must also be locked_fields")
        if any(any(_path_matches(path, rule) or _path_matches(rule, path) for rule in locked)
               for path in editable):
            raise ValueError("editable_fields must not overlap locked_fields")
        if mode is ScenarioMode.FREE:
            if locked or hidden or editable:
                raise ValueError("free mode must use template editability, not challenge policy fields")
            if self.evaluator_id is not None or self.submission_enabled:
                raise ValueError("free mode must not enable challenge submission")
        if self.submission_enabled and not self.evaluator_id:
            raise ValueError("submission_enabled requires evaluator_id")
        if self.evaluator_id is not None:
            _identifier(self.evaluator_id, "evaluator_id")
        allowed = self.allowed_operations
        if allowed is not None:
            allowed = tuple(_identifier(value, "allowed_operation") for value in allowed)
            if len(set(allowed)) != len(allowed):
                raise ValueError("allowed_operations must not contain duplicates")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "locked_fields", locked)
        object.__setattr__(self, "hidden_fields", hidden)
        object.__setattr__(self, "editable_fields", editable)
        object.__setattr__(self, "allowed_operations", allowed)

    def is_editable(self, field_path: str) -> bool:
        path = _field_path(field_path)
        return (self.mode is ScenarioMode.FREE or (
            any(_path_matches(path, rule) for rule in self.editable_fields)
            and not any(_path_matches(path, rule) for rule in self.locked_fields)
        ))

    def is_visible(self, field_path: str) -> bool:
        path = _field_path(field_path)
        return not any(_path_matches(path, rule) for rule in self.hidden_fields)

    def as_free_mode(self) -> "ScenarioPolicy":
        """Keep initial resource values but remove the challenge overlay."""

        return ScenarioPolicy(
            mode=ScenarioMode.FREE,
            observation_budget=self.observation_budget,
            allowed_operations=self.allowed_operations,
        )


@dataclass(frozen=True)
class ScenarioSession:
    """One mode-specific view over a shared template."""

    template: ScenarioTemplate
    policy: ScenarioPolicy
    assumptions: tuple[AssumptionRecord, ...] = ()

    def __post_init__(self) -> None:
        assumptions = tuple(self.assumptions)
        if len(assumptions) > MAX_METADATA_ITEMS:
            raise ValueError("scenario assumptions exceed the field limit")
        if any(not isinstance(item, AssumptionRecord) for item in assumptions):
            raise TypeError("scenario assumptions must be AssumptionRecord values")
        if len({item.assumption_id for item in assumptions}) != len(assumptions):
            raise ValueError("scenario assumption IDs must be unique")
        object.__setattr__(self, "assumptions", assumptions)

    @property
    def mode(self) -> ScenarioMode:
        return self.policy.mode

    def is_assumption_editable(self, field_path: str) -> bool:
        if self.mode is ScenarioMode.FREE:
            return self.template.allows_free_override(field_path)
        # A challenge preset may expose selected fields while keeping its
        # locked (and therefore possibly hidden) contract immutable.
        return self.policy.is_editable(field_path)

    def create_editable_template_copy(self) -> dict[str, object]:
        """Return a detached configuration copy only for a free-mode editor."""

        if self.mode is not ScenarioMode.FREE:
            raise PermissionError("challenge templates cannot be edited in-session")
        return self.template.to_dict()


@dataclass(frozen=True)
class ScenarioPreset:
    """A common template plus the CTF-style policy applied in challenge mode."""

    template: ScenarioTemplate
    challenge_policy: ScenarioPolicy
    assumptions: tuple[AssumptionRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.challenge_policy.mode is not ScenarioMode.CHALLENGE:
            raise ValueError("challenge_policy must use challenge mode")
        assumptions = tuple(self.assumptions)
        if len(assumptions) > MAX_METADATA_ITEMS:
            raise ValueError("scenario preset assumptions exceed the field limit")
        if any(not isinstance(item, AssumptionRecord) for item in assumptions):
            raise TypeError("scenario preset assumptions must be AssumptionRecord values")
        if len({item.assumption_id for item in assumptions}) != len(assumptions):
            raise ValueError("scenario preset assumption IDs must be unique")
        object.__setattr__(self, "assumptions", assumptions)

    def materialize(self, mode: ScenarioMode | str) -> ScenarioSession:
        selected = mode if isinstance(mode, ScenarioMode) else ScenarioMode(mode)
        policy = (
            self.challenge_policy
            if selected is ScenarioMode.CHALLENGE
            else self.challenge_policy.as_free_mode()
        )
        return ScenarioSession(
            template=self.template,
            policy=policy,
            assumptions=self.assumptions,
        )


def _identifier(value: str, name: str) -> str:
    converted = str(value).strip()
    if not converted:
        raise ValueError(f"{name} must not be empty")
    if len(converted.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError(f"{name} exceeds the byte limit")
    return converted


def _field_paths(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    converted = tuple(_field_path(value) for value in values)
    if len(set(converted)) != len(converted):
        raise ValueError(f"{name} must not contain duplicates")
    return converted


def _field_path(value: str) -> str:
    converted = str(value).strip()
    if len(converted.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("field path exceeds the byte limit")
    prefixes = (
        "attacker.",
        "victim.",
        "qpu_service.",
        "relation.",
        "experiment.",
    )
    if not converted.startswith(prefixes) or converted.endswith("."):
        raise ValueError("field paths must use a known configuration prefix")
    return converted


def _path_matches(path: str, rule: str) -> bool:
    return path == rule or path.startswith(f"{rule}.")


def _freeze_mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    frozen: dict[str, Any] = {}
    for key, item in value.items():
        converted_key = str(key).strip()
        if not converted_key:
            raise ValueError(f"{name} keys must not be empty")
        if converted_key in frozen:
            raise ValueError(f"{name} contains duplicate normalized keys")
        frozen[converted_key] = _freeze_value(item, f"{name}.{converted_key}")
    return MappingProxyType(frozen)


def _freeze_value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value, name)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_value(item, f"{name}[{index}]")
            for index, item in enumerate(value)
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError(f"{name} must contain JSON-compatible finite values")


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value

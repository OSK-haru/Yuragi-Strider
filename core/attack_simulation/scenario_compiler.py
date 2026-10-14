"""Compile rich scenario authoring data into immutable resolved manifests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from threading import Lock
from types import MappingProxyType
from typing import Any, Protocol

from core.attack_simulation.config_limits import (
    MAX_CONFIG_BYTES,
    MAX_METADATA_ITEMS,
    MAX_TEXT_BYTES,
    check_json_bounds,
)
from core.attack_simulation.scenario_policy import (
    ObservationBudget,
    ScenarioMode,
    ScenarioSession,
)
from core.attack_simulation.scenario_assumptions import (
    AssumptionBasis,
    AssumptionRecord,
)
from core.attack_simulation.credential_policy import validate_credential_policy


SCENARIO_MANIFEST_SCHEMA_VERSION = "attack_scenario_manifest_v2"


@dataclass(frozen=True)
class ScenarioOverride:
    """One free-mode authoring delta applied before manifest resolution."""

    path: str
    value: Any
    rationale: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _configuration_path(self.path))
        check_json_bounds(self.value, f"override.{self.path}")
        object.__setattr__(
            self,
            "value",
            _freeze_json_value(self.value, f"override.{self.path}"),
        )
        object.__setattr__(self, "rationale", _text(self.rationale, "rationale"))

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "value": _plain(self.value),
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class ResolvedScenarioManifest:
    """Content-addressed immutable scenario configuration and provenance."""

    template_id: str
    template_version: str
    mode: ScenarioMode
    attacker_config: Mapping[str, Any]
    victim_config: Mapping[str, Any]
    qpu_service_config: Mapping[str, Any]
    relation_config: Mapping[str, Any]
    experiment_config: Mapping[str, Any]
    observation_budget: ObservationBudget | None
    locked_fields: tuple[str, ...]
    hidden_fields: tuple[str, ...]
    evaluator_id: str | None
    submission_enabled: bool
    reveal_individual_evaluations: bool
    allowed_operations: tuple[str, ...] | None
    overrides: tuple[ScenarioOverride, ...]
    assumptions: tuple[AssumptionRecord, ...]
    editable_fields: tuple[str, ...] = ()
    schema_version: str = SCENARIO_MANIFEST_SCHEMA_VERSION
    manifest_id: str = ""

    def __post_init__(self) -> None:
        if self.observation_budget is not None and not isinstance(self.observation_budget, ObservationBudget):
            raise TypeError("observation_budget must be an ObservationBudget or None")
        object.__setattr__(self, "template_id", _identifier(self.template_id, "template_id"))
        object.__setattr__(
            self,
            "template_version",
            _identifier(self.template_version, "template_version"),
        )
        mode = self.mode if isinstance(self.mode, ScenarioMode) else ScenarioMode(self.mode)
        object.__setattr__(self, "mode", mode)
        if (
            len(self.assumptions) > MAX_METADATA_ITEMS
            or len(self.overrides) > MAX_METADATA_ITEMS
        ):
            raise ValueError("scenario manifest exceeds the metadata item limit")
        check_json_bounds({
            "config": (
                self.attacker_config,
                self.victim_config,
                self.qpu_service_config,
                self.relation_config,
                self.experiment_config,
            ),
            "policy": (
                self.locked_fields,
                self.hidden_fields,
                self.evaluator_id,
                self.allowed_operations,
            ),
            "assumptions": tuple(
                (
                    item.assumption_id,
                    item.target_paths,
                    item.rationale,
                    item.evidence_ref,
                    item.applicability,
                )
                for item in self.assumptions
            ),
            "overrides": tuple(
                (item.path, item.value, item.rationale)
                for item in self.overrides
            ),
        }, "scenario manifest")
        for name in (
            "attacker_config",
            "victim_config",
            "qpu_service_config",
            "relation_config",
            "experiment_config",
        ):
            object.__setattr__(
                self,
                name,
                _freeze_json_mapping(getattr(self, name), name),
            )
        for name in ("locked_fields", "hidden_fields", "editable_fields"):
            paths = tuple(_configuration_path(path) for path in getattr(self, name))
            if len(set(paths)) != len(paths):
                raise ValueError(f"{name} must not contain duplicates")
            object.__setattr__(self, name, paths)
        if self.allowed_operations is not None:
            operations = tuple(_identifier(value, "allowed_operation")
                               for value in self.allowed_operations)
            if len(set(operations)) != len(operations):
                raise ValueError("allowed_operations must not contain duplicates")
            object.__setattr__(self, "allowed_operations", operations)
        if any(any(rule == path or path.startswith(f"{rule}.")
                       or rule.startswith(f"{path}.") for rule in self.locked_fields)
               for path in self.editable_fields):
            raise ValueError("editable manifest paths must not overlap locked paths")
        assumptions = tuple(sorted(self.assumptions, key=lambda item: item.assumption_id))
        if len({item.assumption_id for item in assumptions}) != len(assumptions):
            raise ValueError("assumption IDs must be unique")
        object.__setattr__(self, "assumptions", assumptions)
        overrides = tuple(sorted(self.overrides, key=lambda item: item.path))
        if len({item.path for item in overrides}) != len(overrides):
            raise ValueError("override paths must be unique")
        object.__setattr__(self, "overrides", overrides)
        object.__setattr__(
            self,
            "schema_version",
            _identifier(self.schema_version, "schema_version"),
        )
        content = self._content_dict()
        encoded = _manifest_bytes(content)
        if len(encoded) > MAX_CONFIG_BYTES:
            raise ValueError("scenario manifest exceeds the serialized byte limit")
        expected = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
        if self.manifest_id and self.manifest_id != expected:
            raise ValueError("manifest_id does not match manifest content")
        object.__setattr__(self, "manifest_id", expected)

    def resolve_path(self, path: str) -> Any:
        normalized = _configuration_path(path)
        root, *segments = normalized.split(".")
        current: Any = self._root_mapping(root)
        for segment in segments:
            if not isinstance(current, Mapping) or segment not in current:
                raise KeyError(f"manifest path does not exist: {normalized}")
            current = current[segment]
        return current

    def to_dict(self) -> dict[str, object]:
        result = self._content_dict()
        result["manifest_id"] = self.manifest_id
        return result

    def _content_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "mode": self.mode.value,
            "config": {
                "attacker": _plain(self.attacker_config),
                "victim": _plain(self.victim_config),
                "qpu_service": _plain(self.qpu_service_config),
                "relation": _plain(self.relation_config),
                "experiment": _plain(self.experiment_config),
            },
            "policy": {
                "observation_budget": self._observation_budget_dict(),
                "locked_fields": list(self.locked_fields),
                "hidden_fields": list(self.hidden_fields),
                "evaluator_id": self.evaluator_id,
                "submission_enabled": self.submission_enabled,
                "reveal_individual_evaluations": self.reveal_individual_evaluations,
                "allowed_operations": self.allowed_operations,
                "editable_fields": list(self.editable_fields),
            },
            "assumptions": [item.to_dict() for item in self.assumptions],
            "overrides": [item.to_dict() for item in self.overrides],
        }

    def _root_mapping(self, root: str) -> Mapping[str, Any]:
        roots: dict[str, Mapping[str, Any]] = {
            "attacker": self.attacker_config,
            "victim": self.victim_config,
            "qpu_service": self.qpu_service_config,
            "relation": self.relation_config,
            "experiment": self.experiment_config,
            "policy": MappingProxyType({
                "observation_budget": (
                    MappingProxyType(self._observation_budget_dict())
                    if self.observation_budget is not None else None
                ),
                "locked_fields": self.locked_fields,
                "hidden_fields": self.hidden_fields,
                "evaluator_id": self.evaluator_id,
                "submission_enabled": self.submission_enabled,
                "reveal_individual_evaluations": self.reveal_individual_evaluations,
                "allowed_operations": self.allowed_operations,
                "editable_fields": self.editable_fields,
            }),
        }
        return roots[root]

    def _observation_budget_dict(self) -> dict[str, int] | None:
        budget = self.observation_budget
        if budget is None:
            return None
        return {
            "shots_per_query": budget.shots_per_query,
            "training_query_limit": budget.training_query_limit,
            "victim_observation_limit": budget.victim_observation_limit,
            "retry_limit": budget.retry_limit,
        }


class ScenarioConstraint(Protocol):
    constraint_id: str

    def validate(self, manifest: ResolvedScenarioManifest) -> None:
        """Raise ValueError when the resolved combination is invalid."""


class ScenarioCompilationError(ValueError):
    """Base error for configuration resolution and validation failures."""


class ScenarioConstraintViolation(ScenarioCompilationError):
    """A named cross-field constraint rejected the resolved scenario."""


class ScenarioCompiler:
    """Compile once before session runtime; never called from job/shot paths."""

    def __init__(self, constraints: Sequence[ScenarioConstraint] = ()) -> None:
        self._constraints = tuple(constraints)
        constraint_ids = tuple(
            _identifier(item.constraint_id, "constraint_id")
            for item in self._constraints
        )
        if len(set(constraint_ids)) != len(constraint_ids):
            raise ValueError("constraint IDs must be unique")

    def compile(
        self,
        session: ScenarioSession,
        *,
        assumptions: Sequence[AssumptionRecord] = (),
        overrides: Sequence[ScenarioOverride] = (),
    ) -> ResolvedScenarioManifest:
        policy = session.policy
        all_assumptions = (*session.assumptions, *tuple(assumptions))
        if len(all_assumptions) > MAX_METADATA_ITEMS or len(overrides) > MAX_METADATA_ITEMS:
            raise ScenarioCompilationError("scenario exceeds the metadata item limit")
        resolved_overrides = tuple(overrides)
        if len({item.path for item in resolved_overrides}) != len(resolved_overrides):
            raise ScenarioCompilationError("override paths must be unique")
        for override in resolved_overrides:
            if not session.is_assumption_editable(override.path):
                raise ScenarioCompilationError(
                    f"scenario assumption is not editable in the selected mode: {override.path}"
                )
        resolved_config = {
            "attacker": _plain(session.template.attacker_config),
            "victim": _plain(session.template.victim_config),
            "qpu_service": _plain(session.template.qpu_service_config),
            "relation": _plain(session.template.relation_config),
            "experiment": _plain(session.template.experiment_config),
        }
        for override in resolved_overrides:
            _apply_override(resolved_config, override)
        try:
            validate_credential_policy(
                resolved_config["attacker"], resolved_config["qpu_service"]
            )
        except ValueError as error:
            raise ScenarioCompilationError(str(error)) from error
        override_assumptions = tuple(
            AssumptionRecord(
                assumption_id=f"user_override.{item.path}",
                target_paths=(item.path,),
                basis=AssumptionBasis.USER_OVERRIDE,
                rationale=item.rationale,
                applicability={
                    "template_id": session.template.template_id,
                    "template_version": session.template.template_version,
                },
            )
            for item in resolved_overrides
        )
        manifest = ResolvedScenarioManifest(
            template_id=session.template.template_id,
            template_version=session.template.template_version,
            mode=policy.mode,
            attacker_config=resolved_config["attacker"],
            victim_config=resolved_config["victim"],
            qpu_service_config=resolved_config["qpu_service"],
            relation_config=resolved_config["relation"],
            experiment_config=resolved_config["experiment"],
            observation_budget=policy.observation_budget,
            locked_fields=policy.locked_fields,
            hidden_fields=policy.hidden_fields,
            evaluator_id=policy.evaluator_id,
            submission_enabled=policy.submission_enabled,
            reveal_individual_evaluations=policy.reveal_individual_evaluations,
            allowed_operations=policy.allowed_operations,
            overrides=resolved_overrides,
            assumptions=(*all_assumptions, *override_assumptions),
            editable_fields=policy.editable_fields,
        )
        for record in manifest.assumptions:
            for path in record.target_paths:
                try:
                    manifest.resolve_path(path)
                except KeyError as error:
                    raise ScenarioCompilationError(str(error)) from error
        for path in (*manifest.locked_fields, *manifest.hidden_fields, *manifest.editable_fields):
            try:
                manifest.resolve_path(path)
            except (KeyError, ValueError) as error:
                raise ScenarioCompilationError(
                    f"policy field path does not exist: {path}"
                ) from error
        self._validate_submittable_challenge(manifest)
        for constraint in self._constraints:
            try:
                constraint.validate(manifest)
            except ScenarioCompilationError:
                raise
            except (KeyError, TypeError, ValueError) as error:
                raise ScenarioConstraintViolation(
                    f"{constraint.constraint_id}: {error}"
                ) from error
        return manifest

    @staticmethod
    def _validate_submittable_challenge(manifest: ResolvedScenarioManifest) -> None:
        if manifest.mode is not ScenarioMode.CHALLENGE or not manifest.submission_enabled:
            return

        required_paths = (
            "attacker.operation_grants",
            "victim.operation_grants",
            "qpu_service.supported_operations",
        )
        for path in required_paths:
            if not any(rule == path or path.startswith(f"{rule}.")
                       for rule in manifest.locked_fields):
                raise ScenarioCompilationError(
                    f"submission permission field must be locked: {path}"
                )
            if not any(path in record.target_paths for record in manifest.assumptions):
                raise ScenarioCompilationError(
                    f"locked submission permission requires provenance: {path}"
                )

        allowed = manifest.allowed_operations
        if not allowed:
            raise ScenarioCompilationError(
                "submission requires explicit allowed_operations"
            )
        service = _validated_operations(
            manifest.qpu_service_config.get("supported_operations"),
            "qpu_service.supported_operations",
        )
        if not set(allowed).issubset(service):
            raise ScenarioCompilationError(
                "scenario allowed_operations exceed service capabilities"
            )
        for path, actor_config in (
            ("attacker.operation_grants", manifest.attacker_config),
            ("victim.operation_grants", manifest.victim_config),
        ):
            grants = _validated_operations(actor_config.get("operation_grants"), path, allow_empty=True)
            if not grants.issubset(service):
                raise ScenarioCompilationError(
                    f"{path} contains operations unsupported by the service"
                )


class InMemoryScenarioManifestStore:
    """Content-addressed deduplication; persistence remains a later adapter."""

    def __init__(
        self,
        *,
        max_entries: int = 256,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("manifest store limits must be positive")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._manifests: dict[str, ResolvedScenarioManifest] = {}
        self._stored_bytes = 0
        self._lock = Lock()

    def put(self, manifest: ResolvedScenarioManifest) -> ResolvedScenarioManifest:
        encoded_size = len(_manifest_bytes(manifest.to_dict()))
        with self._lock:
            existing = self._manifests.get(manifest.manifest_id)
            if existing is not None:
                if existing.to_dict() != manifest.to_dict():
                    raise RuntimeError("scenario manifest hash collision")
                return existing
            if len(self._manifests) >= self.max_entries:
                raise ValueError("manifest store entry limit exceeded")
            if self._stored_bytes + encoded_size > self.max_bytes:
                raise ValueError("manifest store byte limit exceeded")
            self._manifests[manifest.manifest_id] = manifest
            self._stored_bytes += encoded_size
            return manifest

    def get(self, manifest_id: str) -> ResolvedScenarioManifest:
        key = _identifier(manifest_id, "manifest_id")
        with self._lock:
            try:
                return self._manifests[key]
            except KeyError as error:
                raise KeyError(f"unknown scenario manifest: {key}") from error

    def manifest_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._manifests))


def _manifest_bytes(content: Mapping[str, Any]) -> bytes:
    return json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _apply_override(
    resolved_config: dict[str, Any],
    override: ScenarioOverride,
) -> None:
    root, *segments = override.path.split(".")
    if root == "policy":
        raise ScenarioCompilationError("policy values are not scenario overrides")
    current: Any = resolved_config[root]
    for segment in segments[:-1]:
        if not isinstance(current, dict) or segment not in current:
            raise ScenarioCompilationError(
                f"override path does not exist: {override.path}"
            )
        current = current[segment]
    leaf = segments[-1]
    if not isinstance(current, dict) or leaf not in current:
        raise ScenarioCompilationError(
            f"override path does not exist: {override.path}"
        )
    current[leaf] = _plain(override.value)


def _configuration_path(value: str) -> str:
    converted = str(value).strip()
    if len(converted.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("configuration path exceeds the byte limit")
    roots = (
        "attacker.",
        "victim.",
        "qpu_service.",
        "relation.",
        "experiment.",
        "policy.",
    )
    if not converted.startswith(roots) or converted.endswith("."):
        raise ValueError("configuration path uses an unknown root")
    if any(not segment for segment in converted.split(".")):
        raise ValueError("configuration path contains an empty segment")
    return converted


def _freeze_json_mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return _freeze_json_value(value, name)


def _freeze_json_value(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _identifier(key, f"{name} key")
            if normalized_key in frozen:
                raise ValueError(f"{name} contains duplicate normalized keys")
            frozen[normalized_key] = _freeze_json_value(
                item,
                f"{name}.{normalized_key}",
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json_value(item, f"{name}[{index}]")
            for index, item in enumerate(value)
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise TypeError(f"{name} must contain JSON-compatible values")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _identifier(value: str, name: str) -> str:
    converted = str(value).strip()
    if not converted:
        raise ValueError(f"{name} must not be empty")
    if len(converted.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError(f"{name} exceeds the byte limit")
    return converted


def _text(value: str, name: str) -> str:
    converted = str(value).strip()
    if not converted:
        raise ValueError(f"{name} must not be empty")
    if len(converted.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError(f"{name} exceeds the byte limit")
    return converted


def _validated_operations(value: object, name: str, *, allow_empty: bool = False) -> frozenset[str]:
    if (not isinstance(value, (tuple, list)) or (not value and not allow_empty)
            or any(not isinstance(item, str) or not item.strip() for item in value)):
        qualifier = "" if allow_empty else "nonempty "
        raise ScenarioCompilationError(f"{name} must be a {qualifier}operation list")
    operations = tuple(item.strip() for item in value)
    if len(set(operations)) != len(operations):
        raise ScenarioCompilationError(f"{name} must not contain duplicate operations")
    return frozenset(operations)

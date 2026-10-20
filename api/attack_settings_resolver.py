"""Resolve untrusted browser selections against server-owned scenario definitions.

This module is opt-in and deliberately does not mount itself.  It owns the
public settings catalog and converts a bounded JSON request into a prepared
ScenarioEnvironment; the product factory remains responsible for assembly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import JSONResponse

from api.attack_product_factory import (
    IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL, ProductEnvironmentFactory,
    standard_calibration_initial_values,
)
from api.attack_sessions import WebOwnerContext
from api.attack_web_ownership import (
    EnvironmentConfigurationRejected, MAX_SCENARIO_CREATE_BYTES,
    RequestOriginHostRejected, WebOwnershipSettings, _request_origin_and_host,
)
from core.attack_simulation.config_limits import check_json_bounds
from core.attack_simulation.scenario_assumptions import AssumptionRecord
from core.attack_simulation.scenario_compiler import ScenarioOverride
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import (
    ScenarioMode, ScenarioPolicy, ScenarioPreset, ScenarioSession, ScenarioTemplate,
)


RESET_LEAKAGE_PRESET_ID = "reset-leakage"
RESET_LEAKAGE_MESSAGE_PRESET_ID = "reset-leakage-message"
CATALOG_SCHEMA_VERSION = "attack-settings-catalog-v1"
_PROTECTED_SETTING_SEGMENTS = frozenset({
    "principal_id", "tenant_id", "operation_grants", "allowed_operations",
    "credential", "credentials", "token", "api_key", "password", "secret",
    "manifest", "manifest_id", "basis", "evidence_ref", "evaluator_id",
    "submission_enabled", "locked_fields", "hidden_fields",
})


@dataclass(frozen=True)
class SettingField:
    """One scalar field that the server explicitly permits a browser to edit."""

    path: str
    label: str
    value_type: str
    basis: str
    description: str
    minimum: float | int | None = None
    maximum: float | int | None = None
    choices: tuple[str, ...] = ()
    unit: str | None = None
    required: bool = False

    def __post_init__(self) -> None:
        if (type(self.path) is not str or not self.path.startswith((
                "attacker.", "victim.", "qpu_service.", "relation.", "experiment."))
                or self.path.endswith(".")):
            raise ValueError("setting field must use a canonical configuration path")
        if _PROTECTED_SETTING_SEGMENTS.intersection(self.path.split(".")):
            raise ValueError("settings cannot expose credentials, authority, provenance, or policy fields")
        for name in ("label", "basis", "description"):
            if type(getattr(self, name)) is not str or not getattr(self, name).strip():
                raise ValueError(f"{name} must be explicit public text")
        if self.value_type not in {"number", "integer", "boolean", "string", "choice"}:
            raise ValueError("unsupported setting field type")
        if self.value_type in {"number", "integer"}:
            if (self.minimum is None or self.maximum is None
                    or type(self.minimum) not in (int, float)
                    or type(self.maximum) not in (int, float)
                    or not math.isfinite(float(self.minimum))
                    or not math.isfinite(float(self.maximum))
                    or self.minimum > self.maximum):
                raise ValueError("numeric setting fields need finite ordered bounds")
        elif self.minimum is not None or self.maximum is not None:
            raise ValueError("only numeric setting fields may have numeric bounds")
        if self.value_type == "choice":
            if not self.choices or any(type(item) is not str or not item for item in self.choices):
                raise ValueError("choice fields need explicit string choices")
        elif self.choices:
            raise ValueError("choices are only valid for choice fields")
        if type(self.required) is not bool:
            raise ValueError("required must be an explicit boolean")

    def catalog_entry(self, default: object = ...) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path, "label": self.label, "type": self.value_type,
            "basis": self.basis, "description": self.description,
        }
        if self.unit is not None:
            result["unit"] = self.unit
        if self.minimum is not None:
            result["minimum"] = self.minimum
            result["maximum"] = self.maximum
        if self.choices:
            result["choices"] = list(self.choices)
        if default is not ...:
            result["default"] = _json_value(default)
        if self.required:
            result["required"] = True
        return result

    def validate(self, value: object) -> object:
        if self.value_type == "number":
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ValueError
            if not self.minimum <= value <= self.maximum:
                raise ValueError
            return value
        if self.value_type == "integer":
            if type(value) is not int or not self.minimum <= value <= self.maximum:
                raise ValueError
            return value
        if self.value_type == "boolean":
            if type(value) is not bool:
                raise ValueError
            return value
        if self.value_type == "string":
            if type(value) is not str or not value or len(value.encode("utf-8")) > 256:
                raise ValueError
            return value
        if type(value) is not str or value not in self.choices:
            raise ValueError
        return value


@dataclass(frozen=True)
class FreeScenarioDefinition:
    """Explicit server-owned free-mode template for one idle-noise input mode."""

    template: ScenarioTemplate
    assumptions: tuple[AssumptionRecord, ...]
    fields: tuple[SettingField, ...]
    allowed_operations: tuple[str, ...]
    catalog_fields: tuple[tuple[SettingField, object], ...] = ()
    region_metadata: Mapping[str, object] = field(default_factory=dict)
    region_resolver: Callable[[str, object], "FreeScenarioDefinition"] | None = None
    # Values the browser cannot set but that must follow its edits (e.g. a
    # digest of edited model parameters, or the observation budget they imply).
    derive_overrides: Callable[[Mapping[str, object]], tuple[ScenarioOverride, ...]] | None = None
    budget_resolver: Callable[[Mapping[str, object]], ObservationBudget] | None = None

    def __post_init__(self) -> None:
        if type(self.template) is not ScenarioTemplate:
            raise TypeError("free scenario template is required")
        if (type(self.allowed_operations) is not tuple or not self.allowed_operations
                or any(type(item) is not str or not item for item in self.allowed_operations)):
            raise ValueError("free-mode service operations must be explicitly declared")
        _validate_fields(self.fields)
        if any(not self.template.allows_free_override(field.path) for field in self.fields):
            raise ValueError("free setting fields must be allowed by the template")
        for field in self.fields:
            field.validate(_read_path(self.template, field.path))
        configured_mode = _read_path(self.template, "qpu_service.idle_dynamics.input_mode")
        if configured_mode not in {IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL}:
            raise ValueError("free template must explicitly select an idle-noise input mode")
        assumptions = tuple(self.assumptions)
        if any(type(item) is not AssumptionRecord for item in assumptions):
            raise TypeError("free scenario assumptions must be explicit AssumptionRecord values")
        object.__setattr__(self, "assumptions", assumptions)
        catalog_fields = tuple(self.catalog_fields)
        if any(type(item) is not tuple or len(item) != 2 or type(item[0]) is not SettingField
               for item in catalog_fields):
            raise TypeError("catalog-only fields must be explicit field/default pairs")
        if len({item[0].path for item in catalog_fields}) != len(catalog_fields):
            raise ValueError("catalog-only setting paths must be unique")
        for setting, default in catalog_fields:
            setting.validate(default)
        if not isinstance(self.region_metadata, Mapping):
            raise TypeError("region metadata must be a public JSON object")
        metadata = _json_value(self.region_metadata)
        if not callable(self.region_resolver) and self.region_resolver is not None:
            raise TypeError("region resolver must be callable")
        object.__setattr__(self, "catalog_fields", catalog_fields)
        object.__setattr__(self, "region_metadata", metadata)

    @property
    def idle_noise_mode(self) -> str:
        return _read_path(self.template, "qpu_service.idle_dynamics.input_mode")


@dataclass(frozen=True)
class ChallengeDefinition:
    """Public challenge card and optional server-owned preset.

    ``preset=None`` represents a known but unavailable challenge frame.  It is
    listed without hidden configuration and cannot be used to create a world.
    """

    preset_id: str
    title: str
    description: str
    preset: ScenarioPreset | None = None
    fields: tuple[SettingField, ...] = ()
    # Builds a fresh preset per environment, for challenges whose private
    # values (e.g. a secret) must differ between environments.
    preset_factory: Callable[[], ScenarioPreset] | None = None
    # What a free-mode environment cannot reproduce; empty uses the default list.
    not_reproduced: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.preset_factory is not None:
            if self.preset is not None or not callable(self.preset_factory):
                raise ValueError("a challenge has either a fixed preset or a preset factory")
            object.__setattr__(self, "preset", self.preset_factory())
        if (type(self.preset_id) is not str or not self.preset_id
                or any(type(getattr(self, name)) is not str or not getattr(self, name).strip()
                       for name in ("title", "description"))):
            raise ValueError("challenge public identity and description are required")
        _validate_fields(self.fields)
        if self.preset is None and self.fields:
            raise ValueError("unavailable challenge cannot publish editable fields")
        if self.preset is not None:
            if type(self.preset) is not ScenarioPreset:
                raise TypeError("challenge preset must be server-owned ScenarioPreset")
            session = self.preset.materialize(ScenarioMode.CHALLENGE)
            for field in self.fields:
                if (not session.is_assumption_editable(field.path)
                        or not session.policy.is_visible(field.path)
                        or any(_paths_overlap(field.path, hidden)
                               for hidden in session.policy.hidden_fields)):
                    raise ValueError("challenge fields must be visible and explicitly editable")
                field.validate(_read_path(session.template, field.path))


class ScenarioSettingsResolver:
    """Catalog and request resolver backed exclusively by injected definitions."""

    def __init__(self, *, free_scenarios: Mapping[str, FreeScenarioDefinition],
                 challenges: tuple[ChallengeDefinition, ...] = (),
                 include_reset_placeholder: bool = True,
                 standard_initial_values: Mapping[str, object] | None = None) -> None:
        if not isinstance(free_scenarios, Mapping):
            raise TypeError("free_scenarios must be an explicit mapping")
        copied = dict(free_scenarios)
        if any(key != definition.idle_noise_mode for key, definition in copied.items()
               if type(definition) is FreeScenarioDefinition):
            raise ValueError("free scenario registry keys must match each declared input mode")
        if any(type(definition) is not FreeScenarioDefinition for definition in copied.values()):
            raise TypeError("free scenario registry contains an invalid definition")
        if set(copied) - {IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL}:
            raise ValueError("unsupported idle-noise mode in free scenario registry")
        challenge_values = tuple(challenges)
        if any(type(item) is not ChallengeDefinition for item in challenge_values):
            raise TypeError("challenge registry contains an invalid definition")
        if len({item.preset_id for item in challenge_values}) != len(challenge_values):
            raise ValueError("challenge preset ids must be unique")
        if include_reset_placeholder and RESET_LEAKAGE_PRESET_ID not in {
                item.preset_id for item in challenge_values}:
            challenge_values += (ChallengeDefinition(
                RESET_LEAKAGE_PRESET_ID, "Presumption of reset bits", "Preset frame reserved; challenge is not available yet."),)
        self._free = copied
        self._challenges = {item.preset_id: item for item in challenge_values}
        self._standard_initial_values = (
            standard_calibration_initial_values()
            if standard_initial_values is None else _json_value(standard_initial_values)
        )
        if not isinstance(self._standard_initial_values, Mapping):
            raise ValueError("standard initial values must be a JSON object")

    def _free_equivalent(self, session: ScenarioSession,
                         not_reproduced: tuple[str, ...] = ()) -> dict[str, object] | None:
        """The free-mode settings that reproduce a challenge's public parameters.

        Only values the challenge shows to participants are included; hidden values
        (a secret, hidden cases) and the evaluator are never part of it. Budget limits
        and grading belong to challenge mode and are listed as not reproduced."""
        definition = self._free.get(IDLE_INPUT_CALIBRATION)
        if definition is None:
            return None
        settings: dict[str, object] = {}
        for field in definition.fields:
            if not session.policy.is_visible(field.path):
                continue
            try:
                value = field.validate(_read_path(session.template, field.path))
                default = _read_path(definition.template, field.path)
            except (KeyError, ValueError, TypeError):
                continue
            if value != default:
                settings[field.path] = _json_value(value)
        return {
            "idle_noise_mode": IDLE_INPUT_CALIBRATION,
            "settings": settings,
            "not_reproduced": list(not_reproduced or ("観測回数の上限", "隠しケースによる採点と提出")),
        }

    def catalog(self) -> dict[str, object]:
        free_modes = []
        for mode in (IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL):
            definition = self._free.get(mode)
            free_modes.append({
                "id": mode, "available": definition is not None,
                "label": ("Calibration-derived values" if mode == IDLE_INPUT_CALIBRATION
                          else "Physical parameters"),
                "description": (
                    "Set T1, T2 and equilibrium excited-state population directly; do not layer "
                    "temperature or flux-noise inputs over measured calibration values."
                    if mode == IDLE_INPUT_CALIBRATION else
                    "Derive idle rates from the existing physical-environment model inputs; this "
                    "profile is a declared model, not a universal device calibration."
                ),
                "fields": [] if definition is None else [
                    field.catalog_entry(_read_path(definition.template, field.path))
                    for field in definition.fields
                ] + ([] if definition is None else [
                    setting.catalog_entry(default)
                    for setting, default in definition.catalog_fields
                    if setting.path not in {active.path for active in definition.fields}
                ]),
                "region_selection": {} if definition is None else _json_value(definition.region_metadata),
            })
        challenges = []
        for challenge in self._challenges.values():
            fields = []
            if challenge.preset is not None:
                session = challenge.preset.materialize(ScenarioMode.CHALLENGE)
                fields = [field.catalog_entry(_read_path(session.template, field.path))
                          for field in challenge.fields if session.policy.is_visible(field.path)]
            challenges.append({
                "id": challenge.preset_id, "title": challenge.title,
                "description": challenge.description,
                "available": challenge.preset is not None,
                "fields": fields,
            })
            if challenge.preset is not None:
                equivalent = self._free_equivalent(session, challenge.not_reproduced)
                if equivalent is not None:
                    challenges[-1]["free_equivalent"] = equivalent
            if challenge.preset is not None:
                region_selection = _public_challenge_regions(session)
                if region_selection is not None:
                    challenges[-1]["region_selection"] = region_selection
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "modes": [
                {"id": "free", "label": "Free mode"},
                {"id": "challenge", "label": "Challenge mode"},
            ],
            "free_mode": {
                "idle_noise_modes": free_modes,
                "standard_initial_values": _json_value(self._standard_initial_values),
            },
            "challenges": challenges,
        }

    def resolve(self, raw: bytes) -> ScenarioEnvironment:
        payload = _parse_request(raw)
        if payload.get("mode") == "free":
            return self._resolve_free(payload)
        if payload.get("mode") == "challenge":
            return self._resolve_challenge(payload)
        raise ValueError

    def _resolve_free(self, payload: dict[str, object]) -> ScenarioEnvironment:
        if set(payload) not in ({"mode", "idle_noise_mode", "settings"},
                                {"mode", "idle_noise_mode", "settings", "regions"}):
            raise ValueError
        mode = payload["idle_noise_mode"]
        if type(mode) is not str or mode not in self._free:
            raise ValueError
        definition = self._free[mode]
        supplied = _settings(payload["settings"])
        if "regions" in payload or definition.region_resolver is not None:
            region_resolver = definition.region_resolver
            if region_resolver is None:
                raise ValueError
            placement_field = next((item for item in definition.fields
                if item.path == "qpu_service.runtime_execution.placement_policy"), None)
            if placement_field is None:
                raise ValueError
            placement = placement_field.validate(supplied.get(
                placement_field.path, _read_path(definition.template, placement_field.path)))
            definition = region_resolver(placement, payload.get("regions"))
        fields = {field.path: field for field in definition.fields}
        _reject_unknown(supplied, set(fields))
        _require_explicit_fields(supplied, fields)
        budget = None if definition.budget_resolver is None else definition.budget_resolver(supplied)
        environment = ScenarioEnvironment(ScenarioSession(
            definition.template,
            ScenarioPolicy(ScenarioMode.FREE, observation_budget=budget,
                           allowed_operations=definition.allowed_operations),
            definition.assumptions,
        ))
        _apply_settings(environment, supplied, fields)
        _check_concurrency_placement(definition, supplied)
        if definition.derive_overrides is not None:
            for derived in definition.derive_overrides(supplied):
                environment.set_assumption(derived)
        return environment

    def _resolve_challenge(self, payload: dict[str, object]) -> ScenarioEnvironment:
        if set(payload) != {"mode", "preset_id", "settings"}:
            raise ValueError
        preset_id = payload["preset_id"]
        if type(preset_id) is not str:
            raise ValueError
        challenge = self._challenges.get(preset_id)
        if challenge is None or challenge.preset is None:
            raise ValueError
        preset = challenge.preset_factory() if challenge.preset_factory is not None else challenge.preset
        supplied = _settings(payload["settings"])
        fields = {field.path: field for field in challenge.fields}
        _reject_unknown(supplied, set(fields))
        _require_explicit_fields(supplied, fields)
        session = preset.materialize(ScenarioMode.CHALLENGE)
        environment = ScenarioEnvironment(session)
        _apply_settings(environment, supplied, fields)
        return environment

    def bind_product_factory(self, factory: ProductEnvironmentFactory) -> Callable[[WebOwnerContext, bytes], object]:
        if type(factory) is not ProductEnvironmentFactory:
            raise TypeError("a product environment factory is required")

        def assemble(owner: WebOwnerContext, raw: bytes):
            try:
                environment = self.resolve(raw)
            except (ValueError, TypeError, KeyError, RecursionError):
                raise EnvironmentConfigurationRejected from None
            return factory.assemble(environment, owner)

        return assemble


def create_settings_catalog_router(resolver: ScenarioSettingsResolver,
                                  settings: WebOwnershipSettings) -> APIRouter:
    """Create an opt-in public catalog route; callers choose whether to mount."""
    if type(resolver) is not ScenarioSettingsResolver or type(settings) is not WebOwnershipSettings:
        raise TypeError("resolver and explicit ownership settings are required")
    router = APIRouter(prefix="/api/attack/settings", tags=["attack-settings-candidate"])

    @router.get("/catalog")
    async def get_catalog(request: Request):
        try:
            _catalog_origin_and_host(request, settings)
        except RequestOriginHostRejected:
            raise HTTPException(status_code=403, detail={"code": "ORIGIN_HOST_REJECTED"}) from None
        return JSONResponse(resolver.catalog(), headers={"Cache-Control": "no-store"})

    return router


def _catalog_origin_and_host(request: Request, settings: WebOwnershipSettings) -> None:
    """A browser same-origin GET normally omits Origin. This fallback applies
    only to the public catalog: mutations and WebSockets still require Origin.
    """
    if request.headers.getlist("origin"):
        _request_origin_and_host(request, settings)
        return
    if request.method != "GET" or request.headers.getlist("sec-fetch-site") != ["same-origin"]:
        raise RequestOriginHostRejected
    referers = request.headers.getlist("referer")
    if len(referers) > 1:
        raise RequestOriginHostRejected
    try:
        # The application deliberately sets Referrer-Policy: no-referrer.
        # Fetch Metadata is browser-controlled; keep Host validation even
        # when that policy suppresses Referer for this public read-only GET.
        hosts = request.headers.getlist("host")
        if len(hosts) != 1:
            raise RequestOriginHostRejected
        if not referers:
            origin = next((candidate for candidate in settings.allowed_origins
                           if urlsplit(candidate).hostname == urlsplit(f"//{hosts[0]}").hostname), None)
            if origin is None:
                raise RequestOriginHostRejected
        else:
            referer = urlsplit(referers[0])
            if referer.username is not None or referer.password is not None:
                raise RequestOriginHostRejected
            origin = f"{referer.scheme}://{referer.netloc}"
        checked = Request({**request.scope, "headers": [
            *request.scope["headers"], (b"origin", origin.encode("ascii")),
        ]})
        _request_origin_and_host(checked, settings)
    except (ValueError, UnicodeError):
        raise RequestOriginHostRejected from None


def _parse_request(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_SCENARIO_CREATE_BYTES:
        raise ValueError

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError("non-finite number")

    parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant)
    check_json_bounds(parsed, "scenario selection")
    if type(parsed) is not dict:
        raise ValueError
    return parsed


def _settings(value: object) -> dict[str, object]:
    if type(value) is not dict or len(value) > 128:
        raise ValueError
    if any(type(key) is not str or not key or len(key.encode("utf-8")) > 256 for key in value):
        raise ValueError
    return value


def _public_challenge_regions(session: ScenarioSession) -> dict[str, object] | None:
    qpu = session.template.qpu_service_config
    runtime = qpu.get("runtime_execution")
    retained = qpu.get("retained_region")
    if not isinstance(runtime, Mapping) or not isinstance(retained, Mapping):
        return None
    if not session.policy.is_visible("qpu_service.retained_region.physical_qubit_ids"):
        return {"locked": True}
    primary = retained.get("physical_qubit_ids")
    if type(primary) not in (list, tuple) or any(type(qid) is not int for qid in primary):
        return None
    result: dict[str, object] = {
        "locked": True,
        "placement_policy": runtime.get("placement_policy"),
        "primary_physical_qubit_ids": list(primary),
    }
    idle = qpu.get("idle_dynamics")
    if (session.policy.is_visible("qpu_service.idle_dynamics.input_mode")
            and isinstance(idle, Mapping)
            and idle.get("input_mode") in {"calibration_values", "physical_parameters"}):
        result["noise_mode"] = idle["input_mode"]
    if runtime.get("placement_policy") == "tenant_dedicated_region":
        alternate = qpu.get("dedicated_region")
        if not isinstance(alternate, Mapping):
            return None
        ids = alternate.get("physical_qubit_ids")
        if type(ids) not in (list, tuple) or any(type(qid) is not int for qid in ids):
            return None
        result["dedicated_physical_qubit_ids"] = list(ids)
    return result


def _check_concurrency_placement(definition, supplied: Mapping[str, object]) -> None:
    """Overlapping jobs run on the two dedicated regions, so they need dedicated placement."""
    prefix = "qpu_service.runtime_execution."
    try:
        policy = supplied.get(prefix + "concurrency_policy", _read_path(definition.template, prefix + "concurrency_policy"))
        placement = supplied.get(prefix + "placement_policy", _read_path(definition.template, prefix + "placement_policy"))
    except ValueError:
        return                      # a template without these fields has no concurrency choice
    if policy == "overlap_disjoint_regions" and placement != "tenant_dedicated_region":
        raise ValueError("overlapping jobs need tenant-dedicated regions")


def _reject_unknown(values: Mapping[str, object], allowed: set[str]) -> None:
    if set(values) - allowed:
        raise ValueError


def _require_explicit_fields(values: Mapping[str, object], fields: Mapping[str, SettingField]) -> None:
    if any(field.required and path not in values for path, field in fields.items()):
        raise ValueError("a required scenario input was not supplied")


def _apply_settings(environment: ScenarioEnvironment, supplied: Mapping[str, object],
                    fields: Mapping[str, SettingField]) -> None:
    for path, value in supplied.items():
        field = fields.get(path)
        if field is None:
            raise ValueError
        validated = field.validate(value)
        environment.set_assumption(ScenarioOverride(
            path, validated, f"Browser-selected value validated by server catalog field '{field.path}'.",
        ))


def _read_path(source: ScenarioTemplate | ScenarioSession, path: str) -> object:
    template = source.template if type(source) is ScenarioSession else source
    roots = {
        "attacker": template.attacker_config, "victim": template.victim_config,
        "qpu_service": template.qpu_service_config, "relation": template.relation_config,
        "experiment": template.experiment_config,
    }
    root, *segments = path.split(".")
    current: Any = roots[root]
    for segment in segments:
        if not isinstance(current, Mapping) or segment not in current:
            raise ValueError("catalog field does not exist in server template")
        current = current[segment]
    return current


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise TypeError("catalog defaults must be JSON-compatible")


def _validate_fields(fields: tuple[SettingField, ...]) -> None:
    if type(fields) is not tuple or any(type(field) is not SettingField for field in fields):
        raise TypeError("setting fields must be an explicit tuple")
    if len({field.path for field in fields}) != len(fields):
        raise ValueError("setting paths must be unique")


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(f"{right}.") or right.startswith(f"{left}.")

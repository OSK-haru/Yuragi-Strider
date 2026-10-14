"""Bounded v1 JSON boundary for an actor's frozen manifest workload.

The parent assembles actor_config['workload'] before environment establishment.
This module only copies and validates definitions; authentication, grants,
execution, private seeds, physical placement and QPU truth belong elsewhere.
Every workload field, including initialization, is required in v1. There is no
implicit prepare or duration default. Circuit configs retain the common editor
ingestion contract (including its optional visual fields); serialized 0/1
preparation labels are integers, rather than the core's canonical strings.

Mappings and list/tuple arrays support both decoded JSON and frozen manifests.
Raw JSON decoding, including duplicate-key policy, belongs to the caller's
existing manifest transport. A Mapping cannot recover duplicate source keys.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from core.attack_simulation.config_limits import MAX_CONFIG_BYTES, check_json_bounds
from core.attack_simulation.program_request import CircuitProgram
from core.attack_simulation.victim_workload import (
    Branch, CountAtLeast, DeclaredDurations, Outcome, RegisteredProgram,
    TimedStart, WorkloadPlan, WorkloadStep,
)


_PLAN_FIELDS = frozenset({
    "version", "plan_id", "actor_id", "clock_domain", "programs", "steps",
    "starts", "max_events", "max_pending",
})
_PROGRAM_FIELDS = frozenset({"program_id", "circuit_config"})
_STEP_FIELDS = frozenset({
    "step_id", "program_ref", "device_id", "shots", "durations", "max_visits",
    "branches", "operation_id", "initialization",
})
_DURATION_FIELDS = frozenset({"classical_us", "outbound_us", "inbound_us"})
_BRANCH_FIELDS = frozenset({"outcome", "next_step", "predicate"})
_PREDICATE_FIELDS = frozenset({"bitstring", "minimum"})
_START_FIELDS = frozenset({"step_id", "at_us"})


def _fields(value: Any, fields: frozenset[str], name: str) -> None:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    if (len(value) != len(fields) or any(type(key) is not str for key in value)
            or set(value) != fields):
        raise ValueError(f"{name} requires exactly these fields: {sorted(fields)}")


def _array(value: Any, minimum: int, maximum: int, name: str) -> None:
    # Never materialize an arbitrary Iterable, even one claiming a finite len.
    if type(value) not in (list, tuple):
        raise TypeError(f"{name} must be a finite list or tuple")
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} must contain {minimum}..{maximum} items")


def _typed(value: Any, kind: type, name: str) -> None:
    if type(value) is not kind:
        raise TypeError(f"{name} must be {kind.__name__}")


def _check_shape(config: Mapping[str, Any]) -> None:
    """Check all workload collection caps before constructing any children.

    These are the WorkloadPlan/WorkloadStep caps; their immutable validators
    remain responsible for ranges, references, branch ordering and visit bounds.
    Circuit shape/caps remain owned by CircuitProgram's common ingestion path.
    """
    _fields(config, _PLAN_FIELDS, "workload")
    _typed(config["version"], int, "version")
    if config["version"] != 1:
        raise ValueError("unsupported workload version")
    for name in ("plan_id", "actor_id", "clock_domain"):
        _typed(config[name], str, name)
    for name in ("max_events", "max_pending"):
        _typed(config[name], int, name)
    _array(config["programs"], 1, 64, "programs")
    _array(config["steps"], 1, 256, "steps")
    _array(config["starts"], 1, 256, "starts")
    for program in config["programs"]:
        _fields(program, _PROGRAM_FIELDS, "program")
        _typed(program["program_id"], str, "program_id")
        if not isinstance(program["circuit_config"], Mapping):
            raise TypeError("circuit_config must be an object")
    for step in config["steps"]:
        _fields(step, _STEP_FIELDS, "step")
        for name in ("step_id", "program_ref", "device_id", "operation_id", "initialization"):
            _typed(step[name], str, name)
        for name in ("shots", "max_visits"):
            _typed(step[name], int, name)
        _fields(step["durations"], _DURATION_FIELDS, "durations")
        for name in _DURATION_FIELDS:
            if type(step["durations"][name]) not in (int, float):
                raise TypeError(f"{name} must be a finite number")
        _array(step["branches"], 0, 16, "branches")
        for branch in step["branches"]:
            _fields(branch, _BRANCH_FIELDS, "branch")
            _typed(branch["outcome"], str, "outcome")
            _typed(branch["next_step"], str, "next_step")
            predicate = branch["predicate"]
            if predicate is not None:
                _fields(predicate, _PREDICATE_FIELDS, "predicate")
                _typed(predicate["bitstring"], str, "bitstring")
                _typed(predicate["minimum"], int, "minimum")
    for start in config["starts"]:
        _fields(start, _START_FIELDS, "start")
        _typed(start["step_id"], str, "step_id")
        if type(start["at_us"]) not in (int, float):
            raise TypeError("at_us must be a finite number")


def _copy_json(value: Any) -> Any:
    """Thaw only after bounds/shape preflight, without key or type coercion."""
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("JSON object keys must be strings")
        return {key: _copy_json(child) for key, child in value.items()}
    if type(value) in (list, tuple):
        return [_copy_json(child) for child in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError("workload must contain JSON values")


def _check_encoded_size(document: dict[str, Any]) -> None:
    # check_json_bounds is a preflight estimate; also count JSON punctuation.
    encoded = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("workload exceeds the serialized byte limit")


def parse_workload_plan(config: Mapping[str, Any]) -> WorkloadPlan:
    """Parse a closed, explicit v1 definition into validated immutable values.

    Global byte/node/depth bounds run before copies or child construction.
    Unknown/missing fields and collection caps run before common Program
    ingestion. Validation/capacity errors propagate from the immutable schema;
    this function does not decide whether an actor is granted an operation.
    """
    if not isinstance(config, Mapping):
        raise TypeError("workload must be an object")
    check_json_bounds(config, "workload")
    _check_shape(config)
    document = _copy_json(config)
    _check_encoded_size(document)
    programs = tuple(RegisteredProgram(
        item["program_id"], CircuitProgram.from_editor_json(json.dumps(
            item["circuit_config"], ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        )),
    ) for item in document["programs"])
    steps = tuple(WorkloadStep(
        step_id=item["step_id"], program_ref=item["program_ref"],
        device_id=item["device_id"], shots=item["shots"],
        durations=DeclaredDurations(**item["durations"]), max_visits=item["max_visits"],
        branches=tuple(Branch(
            outcome=Outcome(branch["outcome"]), next_step=branch["next_step"],
            predicate=(None if branch["predicate"] is None
                       else CountAtLeast(**branch["predicate"])),
        ) for branch in item["branches"]),
        operation_id=item["operation_id"], initialization=item["initialization"],
    ) for item in document["steps"])
    return WorkloadPlan(
        plan_id=document["plan_id"], actor_id=document["actor_id"],
        clock_domain=document["clock_domain"], programs=programs, steps=steps,
        starts=tuple(TimedStart(**item) for item in document["starts"]),
        max_events=document["max_events"], max_pending=document["max_pending"],
        version=document["version"],
    )


def serialize_workload_plan(plan: WorkloadPlan) -> dict[str, Any]:
    """Return a detached, bounded JSON dict with every workload field explicit."""
    if type(plan) is not WorkloadPlan:
        raise TypeError("a frozen validated WorkloadPlan is required")
    programs = []
    for registered in plan.programs:
        circuit = registered.program.to_circuit_dict()
        circuit["initial_states"] = [int(state) if state in ("0", "1") else state
                                     for state in circuit["initial_states"]]
        programs.append({"program_id": registered.program_id, "circuit_config": circuit})
    document = {
        "version": plan.version, "plan_id": plan.plan_id, "actor_id": plan.actor_id,
        "clock_domain": plan.clock_domain, "programs": programs,
        "steps": [{
            "step_id": step.step_id, "program_ref": step.program_ref,
            "device_id": step.device_id, "shots": step.shots,
            "durations": {
                "classical_us": step.durations.classical_us,
                "outbound_us": step.durations.outbound_us,
                "inbound_us": step.durations.inbound_us,
            },
            "max_visits": step.max_visits,
            "branches": [{
                "outcome": branch.outcome.value, "next_step": branch.next_step,
                "predicate": (None if branch.predicate is None else {
                    "bitstring": branch.predicate.bitstring, "minimum": branch.predicate.minimum,
                }),
            } for branch in step.branches],
            "operation_id": step.operation_id, "initialization": step.initialization,
        } for step in plan.steps],
        "starts": [{"step_id": start.step_id, "at_us": start.at_us} for start in plan.starts],
        "max_events": plan.max_events, "max_pending": plan.max_pending,
    }
    check_json_bounds(document, "workload")
    _check_encoded_size(document)
    return document

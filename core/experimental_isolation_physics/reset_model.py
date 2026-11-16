"""Common internal boundary for reset behavior and future reset mechanisms.

The scenario runtime depends on :class:`ResetModel`, not on a paper-specific
model identifier.  A behavioral approximation and a later measurement-plus-
conditional-X implementation can therefore share orchestration, state-store,
and grading code.

Subsystems with more than two levels (D91). A reset is a measurement and then an X on outcome 1: the
measurement tells a level above 1 apart from the qubit levels, and the X exchanges |0> and |1> only, so a
leaked level passes through the reset (Xu et al., CCS 2023: the standard reset does not return |2> and
above). The declared replacement acts on the qubit levels. Assignment errors are ``(p10, p01, p02, ...)``:
``p10`` is P(read 1 | true 0) and ``p0k`` is P(read 0 | true k), one entry per level the readout must
handle. A level above 1 has no default: how a discriminator trained on |0> and |1> reads |2> is a measured
property of the device.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Protocol, runtime_checkable
import numpy as np

from core.experimental_isolation_physics.imperfect_reset import (
    ImperfectResetSpec,
    apply_local_imperfect_reset,
)
from core.gates import Matrix
from core.register_operations import (
    apply_local_replacement, apply_reset_with_feedback_kick, freeze_register, nonselective_measurement,
    register_dims,
)

_IDENTITY = ((1.0 + 0.0j, 0.0j), (0.0j, 1.0 + 0.0j))


class ResetModelMode(str, Enum):
    """How a reset model composes physical state and observed output."""

    STATE_TRANSITION_WITH_READOUT = "state_transition_with_readout"
    EFFECTIVE_OBSERVATION_ONLY = "effective_observation_only"


@dataclass(frozen=True)
class ResetModelRequest:
    """One service-owned reset between victim and attacker observations."""

    input_state: Matrix
    reset_count: int = 1
    victim_measurement: bool = True
    subsystem_count: int = 1
    targets: tuple[int, ...] | None = None
    dims: tuple[int, ...] | None = None          # levels per subsystem; None: two-level qubits

    def __post_init__(self) -> None:
        count = int(self.reset_count)
        if count < 0:
            raise ValueError("reset_count must be non-negative")
        if type(self.subsystem_count) is not int or self.subsystem_count < 1:
            raise ValueError("subsystem_count must be positive")
        dims = (2,) * self.subsystem_count if self.dims is None else tuple(self.dims)
        if len(dims) != self.subsystem_count:
            raise ValueError("reset register levels do not match subsystem_count")
        dimension = math.prod(dims)
        if len(self.input_state) != dimension or any(len(row) != dimension for row in self.input_state):
            raise ValueError("reset register dimension does not match subsystem_count")
        register_dims(dimension, dims)
        object.__setattr__(self, "dims", dims)
        targets = tuple(range(self.subsystem_count)) if self.targets is None else tuple(self.targets)
        if (not targets or len(set(targets)) != len(targets)
                or any(type(q) is not int or not 0 <= q < self.subsystem_count for q in targets)):
            raise ValueError("invalid reset targets")
        object.__setattr__(self, "targets", targets)
        object.__setattr__(self, "reset_count", count)


@dataclass(frozen=True)
class ResetModelResult:
    """State and attacker-visible probability produced by one reset model."""

    post_reset_state: Matrix | None
    attacker_one_probability: float
    observed_one_given_true_zero: float | None = None
    observed_zero_given_true_one: float | None = None

    def __post_init__(self) -> None:
        probability = _probability(
            self.attacker_one_probability,
            "attacker_one_probability",
        )
        object.__setattr__(self, "attacker_one_probability", probability)
        for name in (
            "observed_one_given_true_zero",
            "observed_zero_given_true_one",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _probability(value, name))


@dataclass(frozen=True)
class RegisterResetResult:
    """Register state and independent assignment channels, ordered by target."""

    post_reset_state: Matrix
    assignment_errors: tuple[tuple[float, ...], ...]     # (p10, p01, p02, ...) per target

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignment_errors", tuple(_assignment(channel) for channel in self.assignment_errors))


@runtime_checkable
class ResetModel(Protocol):
    """Replaceable boundary shared by behavioral and mechanistic models."""

    model_id: str
    model_version: str
    fidelity_level: str
    provenance: str
    model_mode: ResetModelMode
    configuration_id: str

    def run(self, request: ResetModelRequest) -> ResetModelResult:
        """Evaluate a reset and its attacker-visible observation channel."""


class ResetModelRegistry:
    """Small explicit registry; scenario code never branches on model names."""

    def __init__(self) -> None:
        self._models: dict[str, ResetModel] = {}

    def register(self, model: ResetModel) -> None:
        if not isinstance(model, ResetModel):
            raise TypeError("model must satisfy the ResetModel protocol")
        model_id = _identifier(model.model_id, "model_id")
        ResetModelMode(model.model_mode)
        _identifier(model.configuration_id, "configuration_id")
        if model_id in self._models:
            raise ValueError(f"reset model is already registered: {model_id}")
        self._models[model_id] = model

    def get(self, model_id: str) -> ResetModel:
        key = _identifier(model_id, "model_id")
        try:
            return self._models[key]
        except KeyError as error:
            raise KeyError(f"unknown reset model: {key}") from error

    def model_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))

    def supports_register(self, model_id: str, subsystem_count: int) -> bool:
        model = self.get(model_id)
        return subsystem_count == 1 or callable(getattr(model, "run_register", None))

    def run_register(self, model_id: str, request: ResetModelRequest) -> RegisterResetResult:
        model = self.get(model_id)
        if request.subsystem_count == 1 and request.dims == (2,):
            result = model.run(request)
            if result.post_reset_state is None or result.observed_one_given_true_zero is None or result.observed_zero_given_true_one is None:
                raise ValueError("register reset requires state and assignment channels")
            return RegisterResetResult(result.post_reset_state, ((result.observed_one_given_true_zero, result.observed_zero_given_true_one),))
        method = getattr(model, "run_register", None)
        if not callable(method):
            raise ValueError("selected reset model does not support register states")
        result = method(request)
        if not isinstance(result, RegisterResetResult) or len(result.assignment_errors) != len(request.targets):
            raise ValueError("invalid register reset result")
        return result


@dataclass(frozen=True)
class PhenomenologicalResetModel:
    """Adapter from the existing CPTP primitive to the common model boundary.

    This remains a behavioral approximation.  It must not be presented as a
    measurement-plus-conditional-X mechanism or as an IBM device calibration.
    """

    spec: ImperfectResetSpec
    observed_one_given_zero: float = 0.0
    observed_zero_given_one: float = 0.0
    model_id: str = "phenomenological_imperfect_reset"
    model_version: str = "1"
    fidelity_level: str = "behavioral_approximation"
    provenance: str = "simulator_derived"
    model_mode: ResetModelMode = ResetModelMode.STATE_TRANSITION_WITH_READOUT
    # P(read 0 | true k) for k = 2, 3, ...: needed only on subsystems with those levels.
    observed_zero_given_higher: tuple[float, ...] = ()

    @property
    def configuration_id(self) -> str:
        payload = {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_mode": self.model_mode.value,
            "success_probability": self.spec.success_probability,
            "target_populations": list(self.spec.target_populations),
            "observed_one_given_zero": self.observed_one_given_zero,
            "observed_zero_given_one": self.observed_zero_given_one,
        }
        if self.observed_zero_given_higher:          # absent before D91, so earlier IDs are unchanged
            payload["observed_zero_given_higher"] = list(self.observed_zero_given_higher)
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def __post_init__(self) -> None:
        _identifier(self.model_id, "model_id")
        _identifier(self.model_version, "model_version")
        _identifier(self.fidelity_level, "fidelity_level")
        _identifier(self.provenance, "provenance")
        mode = (
            self.model_mode
            if isinstance(self.model_mode, ResetModelMode)
            else ResetModelMode(self.model_mode)
        )
        if mode is not ResetModelMode.STATE_TRANSITION_WITH_READOUT:
            raise ValueError(
                "PhenomenologicalResetModel requires state_transition_with_readout"
            )
        object.__setattr__(self, "model_mode", mode)
        object.__setattr__(
            self,
            "observed_one_given_zero",
            _probability(self.observed_one_given_zero, "observed_one_given_zero"),
        )
        object.__setattr__(
            self,
            "observed_zero_given_one",
            _probability(self.observed_zero_given_one, "observed_zero_given_one"),
        )
        higher = tuple(_probability(value, "observed_zero_given_higher") for value in self.observed_zero_given_higher)
        if len(higher) > 2:
            raise ValueError("observed_zero_given_higher covers levels 2 and 3 at most")
        object.__setattr__(self, "observed_zero_given_higher", higher)

    def run(self, request: ResetModelRequest) -> ResetModelResult:
        if request.subsystem_count != 1 or request.dims != (2,):
            raise ValueError("use the register reset boundary for multiple qubits or more levels")
        state = request.input_state
        if request.victim_measurement:
            state = (
                (state[0][0], 0.0j),
                (0.0j, state[1][1]),
            )
        for _ in range(request.reset_count):
            state = apply_local_imperfect_reset(
                state,
                subsystem_count=1,
                target=0,
                spec=self.spec,
            )

        true_one = _probability(float(state[1][1].real), "true_one_probability")
        observed_one = (
            (1.0 - true_one) * self.observed_one_given_zero
            + true_one * (1.0 - self.observed_zero_given_one)
        )
        return ResetModelResult(
            post_reset_state=state,
            attacker_one_probability=observed_one,
            observed_one_given_true_zero=self.observed_one_given_zero,
            observed_zero_given_true_one=self.observed_zero_given_one,
        )

    def local_spec(self, target: int) -> ImperfectResetSpec:
        """The one-qubit reset channel applied to register slot ``target``."""
        return self.spec

    def run_register(self, request: ResetModelRequest) -> RegisterResetResult:
        if self.spec.local_dimension != 2:
            raise ValueError("this register adapter supports qubits, not qutrits")
        channel = (self.observed_one_given_zero, self.observed_zero_given_one, *self.observed_zero_given_higher)
        state = _reset_register(request, lambda target: self.spec)
        return RegisterResetResult(
            freeze_register(state), tuple(_for_levels(channel, request.dims[target]) for target in request.targets))


@dataclass(frozen=True)
class PerQubitPhenomenologicalResetModel:
    """Bounded register reset with a separately declared channel per physical qubit.

    The tuple order is the frozen retained-region order. This is still a
    phenomenological reset approximation; it does not claim an IBM pulse-level
    reset mechanism.
    """

    physical_qubit_ids: tuple[int, ...]
    specs: tuple[ImperfectResetSpec, ...]
    assignment_errors: tuple[tuple[float, float], ...]
    model_id: str = "per_qubit_phenomenological_reset"
    model_version: str = "1"
    fidelity_level: str = "behavioral_approximation"
    provenance: str = "snapshot_derived_approximation"
    model_mode: ResetModelMode = ResetModelMode.STATE_TRANSITION_WITH_READOUT

    def __post_init__(self) -> None:
        ids = tuple(self.physical_qubit_ids)
        specs = tuple(self.specs)
        errors = tuple(tuple(pair) for pair in self.assignment_errors)
        if (not ids or len(set(ids)) != len(ids)
                or any(type(value) is not int or value < 0 for value in ids)):
            raise ValueError("reset calibration needs unique physical qubit IDs")
        if (len(specs) != len(ids) or any(type(spec) is not ImperfectResetSpec for spec in specs)
                or any(spec.local_dimension != 2 for spec in specs)):
            raise ValueError("one qubit reset channel is required per retained qubit")
        if len(errors) != len(ids) or any(not 2 <= len(pair) <= 4 for pair in errors):
            raise ValueError("one set of readout assignment errors (p10, p01, ...) is required per qubit")
        errors = tuple(_assignment(pair) for pair in errors)
        for name in ("model_id", "model_version", "fidelity_level", "provenance"):
            _identifier(getattr(self, name), name)
        mode = self.model_mode if isinstance(self.model_mode, ResetModelMode) else ResetModelMode(self.model_mode)
        if mode is not ResetModelMode.STATE_TRANSITION_WITH_READOUT:
            raise ValueError("per-qubit reset supports state_transition_with_readout only")
        object.__setattr__(self, "physical_qubit_ids", ids)
        object.__setattr__(self, "specs", specs)
        object.__setattr__(self, "assignment_errors", errors)
        object.__setattr__(self, "model_mode", mode)

    @property
    def configuration_id(self) -> str:
        payload = {
            "model_id": self.model_id,
            "model_version": self.model_version,
            "model_mode": self.model_mode.value,
            "physical_qubit_ids": list(self.physical_qubit_ids),
            "channels": [
                {"success_probability": spec.success_probability,
                 "target_populations": list(spec.target_populations),
                 "p10": errors[0], "p01": errors[1],
                 # absent before D91, so earlier IDs are unchanged
                 **({"p0_higher": list(errors[2:])} if len(errors) > 2 else {})}
                for spec, errors in zip(self.specs, self.assignment_errors, strict=True)
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def run(self, request: ResetModelRequest) -> ResetModelResult:
        if (request.subsystem_count != 1 or len(self.specs) != 1 or request.targets != (0,)
                or request.dims != (2,)):
            raise ValueError("single-qubit reset request does not match this profile")
        state = request.input_state
        if request.victim_measurement:
            state = ((state[0][0], 0.0j), (0.0j, state[1][1]))
        for _ in range(request.reset_count):
            state = apply_local_imperfect_reset(
                state, subsystem_count=1, target=0, spec=self.specs[0],
            )
        true_one = _probability(float(state[1][1].real), "true_one_probability")
        p10, p01 = self.assignment_errors[0][:2]
        observed_one = (1.0 - true_one) * p10 + true_one * (1.0 - p01)
        return ResetModelResult(state, observed_one, p10, p01)

    def register_assignment_errors(self, *, subsystem_count: int,
                                   targets: tuple[int, ...]) -> tuple[tuple[float, float], ...]:
        """Readout-only query for inherited registers; it never evaluates a Reset."""
        if subsystem_count != len(self.specs) or any(target >= len(self.specs) for target in targets):
            raise ValueError("readout register differs from frozen physical-qubit profile")
        return tuple(self.assignment_errors[target] for target in targets)

    def local_spec(self, target: int) -> ImperfectResetSpec:
        """The one-qubit reset channel applied to register slot ``target``."""
        return self.specs[target]

    def run_register(self, request: ResetModelRequest) -> RegisterResetResult:
        if request.subsystem_count != len(self.specs):
            raise ValueError("reset register width differs from frozen physical-qubit profile")
        if any(target >= len(self.specs) for target in request.targets):
            raise ValueError("reset target has no per-qubit calibration")
        state = _reset_register(request, lambda target: self.specs[target])
        return RegisterResetResult(
            freeze_register(state),
            tuple(_for_levels(self.assignment_errors[target], request.dims[target]) for target in request.targets),
        )


def _reset_register(request: ResetModelRequest, spec_of) -> np.ndarray:
    """The declared replacement on every target; a leaked level passes through (see the module docstring)."""
    dims = request.dims
    state = np.asarray(request.input_state, dtype=complex)
    if request.victim_measurement:
        state = nonselective_measurement(state, request.targets, dims)
    for _ in range(request.reset_count):
        for target in request.targets:
            spec = spec_of(target)
            if dims[target] == 2:
                state = apply_local_replacement(state, target, spec.success_probability, spec.target_populations,
                                                dims)
            elif spec.local_dimension != 2:
                raise ValueError("a reset on more levels replaces the two qubit levels")
            else:
                state = apply_reset_with_feedback_kick(state, target, spec.success_probability,
                                                       spec.target_populations, _IDENTITY, dims)
    return state


def _assignment(channel) -> tuple[float, ...]:
    values = tuple(channel)
    if not 2 <= len(values) <= 4:
        raise ValueError("readout assignment errors are (p10, p01) and P(read 0 | true k) for higher levels")
    names = ("readout p10", "readout p01", "readout p02", "readout p03")
    return tuple(_probability(value, name) for value, name in zip(values, names))


def _for_levels(channel: tuple[float, ...], levels: int) -> tuple[float, ...]:
    """The assignment errors a subsystem with ``levels`` levels needs, or an error naming what is missing."""
    if len(channel) < levels:
        raise ValueError(f"reading out a subsystem with {levels} levels needs P(read 0 | true k) up to "
                         f"k = {levels - 1}; it is a measured property of the readout and has no default")
    return tuple(channel[:levels])


def _identifier(value: str, name: str) -> str:
    converted = str(value).strip()
    if not converted:
        raise ValueError(f"{name} must not be empty")
    return converted


def _probability(value: float, name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < 0.0 or converted > 1.0:
        raise ValueError(f"{name} must be finite and between zero and one")
    return converted

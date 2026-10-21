"""Validated hardware gate capabilities used by snapshots and the compiler."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from types import MappingProxyType

SUPPORTED_TWO_QUBIT_GATES = frozenset({"cz", "ecr"})


@dataclass(frozen=True)
class BackendDeclaration:
    native_single_qubit_gates: tuple[str, ...] = ("rz", "sx", "x")
    virtual_single_qubit_gates: tuple[str, ...] = ("rz",)
    two_qubit_gates: Mapping[str, bool] | tuple[str, ...] = ("cz",)
    native_pulse_angles_rad: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        if type(self.native_single_qubit_gates) not in (tuple, list):
            raise ValueError("native single-qubit gates must be a sequence")
        if type(self.virtual_single_qubit_gates) not in (tuple, list):
            raise ValueError("virtual single-qubit gates must be a sequence")
        if not isinstance(self.two_qubit_gates, Mapping) and type(self.two_qubit_gates) not in (tuple, list):
            raise ValueError("two-qubit gates must be a mapping or sequence")
        native = tuple(self.native_single_qubit_gates)
        virtual = tuple(self.virtual_single_qubit_gates)
        if (not native or len(set(native)) != len(native)
                or any(type(gate) is not str or not gate for gate in native)):
            raise ValueError("native single-qubit gates must be unique names")
        if (len(set(virtual)) != len(virtual) or not set(virtual) <= set(native)
                or any(type(gate) is not str for gate in virtual)):
            raise ValueError("virtual gates must be a unique subset of native single-qubit gates")
        raw_angles = self.native_pulse_angles_rad
        if raw_angles is None:
            defaults = {"sx": math.pi / 2.0, "x": math.pi}
            angles = {name: defaults[name] for name in native if name in defaults and name not in virtual}
        elif isinstance(raw_angles, Mapping):
            angles = dict(raw_angles)
        else:
            raise ValueError("native pulse angles must be a gate-to-angle mapping")
        if (set(angles) != set(native) - set(virtual)
                or any(type(name) is not str or type(angle) not in (int, float)
                       or not math.isfinite(float(angle)) or not 0.0 < float(angle) <= 2.0 * math.pi
                       for name, angle in angles.items())):
            raise ValueError("every non-virtual native gate requires a bounded pulse rotation angle")
        raw_two = self.two_qubit_gates
        if isinstance(raw_two, Mapping):
            two = dict(raw_two)
        else:
            two = {name: False for name in raw_two}
        if (not two or len(two) != len(set(two))
                or any(type(name) is not str or name not in SUPPORTED_TWO_QUBIT_GATES
                       or type(directed) is not bool
                       for name, directed in two.items())):
            raise ValueError("two-qubit gate is unsupported or lacks a symmetry/direction declaration")
        if "ecr" in two and not two["ecr"]:
            raise ValueError("ECR requires an ordered directed coupling declaration")
        object.__setattr__(self, "native_single_qubit_gates", native)
        object.__setattr__(self, "virtual_single_qubit_gates", virtual)
        object.__setattr__(self, "two_qubit_gates", MappingProxyType(two))
        object.__setattr__(self, "native_pulse_angles_rad",
                           MappingProxyType({name: float(angle) for name, angle in angles.items()}))

    def to_config(self) -> dict[str, object]:
        return {"native_single_qubit_gates": list(self.native_single_qubit_gates),
                "virtual_single_qubit_gates": list(self.virtual_single_qubit_gates),
                "two_qubit_gates": dict(self.two_qubit_gates),
                "native_pulse_angles_rad": dict(self.native_pulse_angles_rad)}

    @classmethod
    def from_config(cls, value: object) -> BackendDeclaration:
        if not isinstance(value, Mapping) or set(value) != {
                "native_single_qubit_gates", "virtual_single_qubit_gates", "two_qubit_gates",
                "native_pulse_angles_rad"}:
            raise ValueError("invalid backend declaration")
        if (type(value["native_single_qubit_gates"]) not in (tuple, list)
                or type(value["virtual_single_qubit_gates"]) not in (tuple, list)
                or not isinstance(value["two_qubit_gates"], Mapping)
                or not isinstance(value["native_pulse_angles_rad"], Mapping)):
            raise ValueError("invalid backend declaration field types")
        return cls(tuple(value["native_single_qubit_gates"]),
                   tuple(value["virtual_single_qubit_gates"]), value["two_qubit_gates"],
                   value["native_pulse_angles_rad"])


KINGSTON_BACKEND = BackendDeclaration()

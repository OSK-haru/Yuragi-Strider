"""Canonical metadata for gates exposed by the circuit model."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class GateSpec:
    name: str
    min_targets: int
    max_targets: int | None
    min_controls: int
    max_controls: int | None
    duration_us: float | None
    backend_native: bool = False
    program_supported: bool = True


_SPECS = (
    GateSpec("I", 1, 1, 0, 0, 0.0), GateSpec("H", 1, 1, 0, 0, 0.02),
    GateSpec("X", 1, 1, 0, 0, 0.02), GateSpec("Y", 1, 1, 0, 0, 0.02),
    GateSpec("Z", 1, 1, 0, 0, 0.0), GateSpec("S", 1, 1, 0, 0, 0.0),
    GateSpec("T", 1, 1, 0, 0, 0.0), GateSpec("RX", 1, 1, 0, 0, 0.02),
    GateSpec("RY", 1, 1, 0, 0, 0.02), GateSpec("RZ", 1, 1, 0, 0, 0.02),
    GateSpec("CNOT", 1, 1, 1, None, 0.20),
    GateSpec("CZ", 1, 1, 1, 1, 0.20, True),
    GateSpec("ECR", 2, 2, 0, 0, 0.20, True),
    GateSpec("CP", 1, 1, 1, 1, 0.20), GateSpec("CCX", 1, 1, 2, 2, 0.40),
    GateSpec("SWAP", 2, 2, 0, 0, 0.20), GateSpec("QFT", 1, None, 0, 0, None),
    GateSpec("ORACLE", 1, None, 0, 0, None), GateSpec("MEASURE", 1, 1, 0, 0, 0.0),
    GateSpec("MESSAGE", 1, 1, 0, 0, 0.04, program_supported=False),
)
GATE_SPECS = MappingProxyType({spec.name: spec for spec in _SPECS})
SUPPORTED_GATE_NAMES = frozenset(GATE_SPECS)
BACKEND_NATIVE_GATE_NAMES = frozenset(
    name for name, spec in GATE_SPECS.items() if spec.backend_native
)
PROGRAM_GATE_NAMES = tuple(name for name, spec in GATE_SPECS.items() if spec.program_supported)

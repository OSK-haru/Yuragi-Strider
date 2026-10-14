"""回路エディターと同じJSONを組み立てる小さなビルダー。"""

from __future__ import annotations

from typing import Any

_ROTATIONS = {"rx": "RX", "ry": "RY", "rz": "RZ"}
_SINGLE = {"x": "X", "y": "Y", "z": "Z", "h": "H", "s": "S", "t": "T", "sx": "SX"}


class Circuit:
    """ゲートを1つずつ列にして積む。各メソッドは自分を返すので繋げて書ける。"""

    def __init__(self, qubits: int, classical_bits: int | None = None, *, initial_states: list[int] | None = None):
        if type(qubits) is not int or qubits < 1:
            raise ValueError("qubits must be a positive integer")
        self.qubits = qubits
        self.classical_bits = qubits if classical_bits is None else classical_bits
        self.initial_states = list(initial_states) if initial_states is not None else [0] * qubits
        if len(self.initial_states) != qubits:
            raise ValueError("initial_states must have one entry per qubit")
        self._columns: list[list[dict[str, Any]]] = []

    def gate(self, name: str, *targets: int, controls: tuple[int, ...] = (), theta: float | None = None) -> "Circuit":
        document: dict[str, Any] = {"type": name, "targets": list(targets)}
        if controls:
            document["controls"] = list(controls)
        if theta is not None:
            document["params"] = {"theta_rad": float(theta)}
        self._columns.append([document])
        return self

    def measure(self, qubit: int, classical_bit: int) -> "Circuit":
        self._columns.append([{"type": "MEASURE", "targets": [qubit], "classical_targets": [classical_bit]}])
        return self

    def measure_all(self) -> "Circuit":
        for qubit in range(self.qubits):
            self.measure(qubit, qubit)
        return self

    def cnot(self, control: int, target: int) -> "Circuit":
        return self.gate("CNOT", target, controls=(control,))

    def cz(self, control: int, target: int) -> "Circuit":
        return self.gate("CZ", target, controls=(control,))

    def __getattr__(self, name: str):
        if name in _SINGLE:
            return lambda qubit: self.gate(_SINGLE[name], qubit)
        if name in _ROTATIONS:
            return lambda qubit, theta: self.gate(_ROTATIONS[name], qubit, theta=theta)
        raise AttributeError(name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_qubits": self.qubits,
            "initial_states": list(self.initial_states),
            "classical_bits": self.classical_bits,
            "columns": [{"step": index, "gates": gates} for index, gates in enumerate(self._columns)],
        }

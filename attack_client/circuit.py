"""回路エディターと同じJSONを組み立てる小さなビルダー。"""

from __future__ import annotations

from typing import Any

_ROTATIONS = {"rx": "RX", "ry": "RY", "rz": "RZ"}
_SINGLE = {"x": "X", "y": "Y", "z": "Z", "h": "H", "s": "S", "t": "T", "sx": "SX"}


class Circuit:
    """ゲートを積む。各メソッドは自分を返すので繋げて書ける。

    ゲートは、同じ量子ビットを使う直前のゲートの次の列へ置かれる（実サービスのトランスパイラと同じで、
    別の量子ビットのゲートは同じ列に並び、並列に進む）。測定は最後の1列にまとまり、同時に行われる。
    """

    def __init__(self, qubits: int, classical_bits: int | None = None, *, initial_states: list[int] | None = None):
        if type(qubits) is not int or qubits < 1:
            raise ValueError("qubits must be a positive integer")
        self.qubits = qubits
        self.classical_bits = qubits if classical_bits is None else classical_bits
        self.initial_states = list(initial_states) if initial_states is not None else [0] * qubits
        if len(self.initial_states) != qubits:
            raise ValueError("initial_states must have one entry per qubit")
        self._columns: list[list[dict[str, Any]]] = []
        self._last_column: dict[int, int] = {}
        self._measures: list[dict[str, Any]] = []
        self._measured: set[int] = set()

    def _place(self, document: dict[str, Any]) -> "Circuit":
        operands = list(document["targets"]) + list(document.get("controls", []))
        if any(qubit in self._measured for qubit in operands):
            raise ValueError("a qubit cannot be used after it is measured; the service measures only at the end")
        index = max((self._last_column.get(qubit, -1) for qubit in operands), default=-1) + 1
        while len(self._columns) <= index:
            self._columns.append([])
        self._columns[index].append(document)
        for qubit in operands:
            self._last_column[qubit] = index
        return self

    def gate(self, name: str, *targets: int, controls: tuple[int, ...] = (), theta: float | None = None) -> "Circuit":
        document: dict[str, Any] = {"type": name, "targets": list(targets)}
        if controls:
            document["controls"] = list(controls)
        if theta is not None:
            document["params"] = {"theta_rad": float(theta)}
        return self._place(document)

    def delay(self, qubit: int, duration_us: float) -> "Circuit":
        """量子ビットを duration_us マイクロ秒だけ待たせる（Qiskit の delay に相当）。

        回路JSONでは、継続時間を指定した恒等ゲート（I）として書かれる。
        """
        return self._place({"type": "I", "targets": [qubit], "params": {"duration_us": float(duration_us)}})

    def measure(self, qubit: int, classical_bit: int) -> "Circuit":
        self._measures.append({"type": "MEASURE", "targets": [qubit], "classical_targets": [classical_bit]})
        self._measured.add(qubit)
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
        columns = [list(column) for column in self._columns]
        if self._measures:
            columns.append(list(self._measures))
        return {
            "logical_qubits": self.qubits,
            "initial_states": list(self.initial_states),
            "classical_bits": self.classical_bits,
            "columns": [{"step": index, "gates": gates} for index, gates in enumerate(columns)],
        }

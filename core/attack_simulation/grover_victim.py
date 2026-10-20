"""A victim's Grover search, routed onto a region's coupling tree.

The product service has no multi-controlled gate and runs CNOT/CZ only on coupled physical pairs. The
search over four data qubits (16 candidates) therefore builds its multi-controlled Z from two ancilla
qubits and relative-phase Toffoli gates (rotations and CNOTs; a compute/uncompute pair cancels the
relative phases), and routes every two-qubit gate with SWAPs (three CNOTs each) along the region's
shortest paths. The result is an editor-format circuit that uses only the service's gates.

``grover_circuit`` returns the circuit document and the program qubits that hold the data qubits at the
end, so a caller can read the answer from the measured bits. The routing is a deterministic function of
the region and its coupling edges.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
import math
from typing import Any

DATA_QUBITS = 4
ANCILLA_QUBITS = 2
REGISTER = DATA_QUBITS + ANCILLA_QUBITS

Op = tuple  # ("H", q) | ("X", q) | ("RY", q, theta) | ("CNOT", control, target) | ("CZ", control, target)


def _toffoli_relative_phase(c1: int, c2: int, target: int) -> list[Op]:
    """Toffoli up to a relative phase: needs only (c1, target) and (c2, target) to be coupled."""
    quarter = math.pi / 4.0
    return [("RY", target, quarter), ("CNOT", c2, target), ("RY", target, quarter), ("CNOT", c1, target),
            ("RY", target, -quarter), ("CNOT", c2, target), ("RY", target, -quarter)]


def _inverse(ops: Sequence[Op]) -> list[Op]:
    inverse: list[Op] = []
    for op in reversed(ops):
        inverse.append((op[0], op[1], -op[2]) if op[0] == "RY" else op)
    return inverse


def _multi_controlled_z(data: Sequence[int], ancilla: Sequence[int]) -> list[Op]:
    """Z on the all-ones state of four data qubits, using two ancillas that start and end at zero."""
    compute = (_toffoli_relative_phase(data[0], data[1], ancilla[0])
               + _toffoli_relative_phase(ancilla[0], data[2], ancilla[1]))
    return compute + [("CZ", ancilla[1], data[3])] + _inverse(compute)


def logical_grover(marked: str, iterations: int) -> list[Op]:
    """The search on logical qubits 0-3 (data) and 4-5 (ancilla), before any routing."""
    if len(marked) != DATA_QUBITS or set(marked) - {"0", "1"}:
        raise ValueError("the marked item is a four-bit string")
    if type(iterations) is not int or iterations < 0:
        raise ValueError("iterations must be a nonnegative integer")
    data, ancilla = list(range(DATA_QUBITS)), [DATA_QUBITS, DATA_QUBITS + 1]
    zeros = [q for q, bit in enumerate(marked) if bit == "0"]
    ops: list[Op] = [("H", q) for q in data]
    for _ in range(iterations):
        ops += [("X", q) for q in zeros] + _multi_controlled_z(data, ancilla) + [("X", q) for q in zeros]
        ops += [("H", q) for q in data] + [("X", q) for q in data]
        ops += _multi_controlled_z(data, ancilla)
        ops += [("X", q) for q in data] + [("H", q) for q in data]
    return ops


def _paths(neighbours: dict[int, set[int]], start: int) -> dict[int, int | None]:
    parent: dict[int, int | None] = {start: None}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for other in sorted(neighbours[node]):
            if other not in parent:
                parent[other] = node
                queue.append(other)
    return parent


def route(ops: Sequence[Op], region: Sequence[int], edges: Sequence[Sequence[int]]) -> tuple[list[Op], list[int]]:
    """Place the logical qubits on the region in order, and swap the control next to the target as needed.

    Returns the routed program-qubit operations and, for each logical qubit, the program qubit that holds it
    at the end. A program qubit is an index into ``region``.
    """
    index = {physical: position for position, physical in enumerate(region)}
    neighbours: dict[int, set[int]] = {position: set() for position in range(len(region))}
    for left, right in edges:
        if left in index and right in index:
            neighbours[index[left]].add(index[right])
            neighbours[index[right]].add(index[left])
    where = list(range(len(region)))              # where[logical] = program qubit
    holder = {position: logical for logical, position in enumerate(where)}
    routed: list[Op] = []

    def swap(left: int, right: int) -> None:
        for control, target in ((left, right), (right, left), (left, right)):
            routed.append(("CNOT", control, target))
        a, b = holder[left], holder[right]
        where[a], where[b] = right, left
        holder[left], holder[right] = b, a

    for op in ops:
        if op[0] in ("CNOT", "CZ"):
            control, target = where[op[1]], where[op[2]]
            if target not in neighbours[control]:
                parent = _paths(neighbours, target)
                if control not in parent:
                    raise ValueError("the region does not connect the gate's qubits")
                while target not in neighbours[control]:
                    step = parent[control]
                    swap(control, step)
                    control = step
            routed.append((op[0], control, where[op[2]]))
        elif op[0] == "RY":
            routed.append(("RY", where[op[1]], op[2]))
        else:
            routed.append((op[0], where[op[1]]))
    return routed, where


def to_editor_circuit(ops: Sequence[Op], qubits: int, measured: Sequence[int]) -> dict[str, Any]:
    """Editor-format circuit: gates of different qubits share a column, the measurements close it."""
    columns: list[list[dict[str, Any]]] = []
    last: dict[int, int] = {}
    for op in ops:
        if op[0] in ("CNOT", "CZ"):
            operands, document = (op[1], op[2]), {"type": op[0], "targets": [op[2]], "controls": [op[1]]}
        elif op[0] == "RY":
            operands, document = (op[1],), {"type": "RY", "targets": [op[1]], "params": {"theta_rad": op[2]}}
        else:
            operands, document = (op[1],), {"type": op[0], "targets": [op[1]]}
        column = max((last.get(q, -1) for q in operands), default=-1) + 1
        while len(columns) <= column:
            columns.append([])
        columns[column].append(document)
        for q in operands:
            last[q] = column
    columns.append([{"type": "MEASURE", "targets": [q], "classical_targets": [bit]}
                    for bit, q in enumerate(measured)])
    return {"logical_qubits": qubits, "initial_states": [0] * qubits, "classical_bits": len(measured),
            "columns": [{"step": step, "gates": gates} for step, gates in enumerate(columns)]}


def grover_circuit(marked: str, iterations: int, region: Sequence[int],
                   edges: Sequence[Sequence[int]]) -> tuple[dict[str, Any], str]:
    """The routed circuit and the bitstring that a correct run measures most often.

    Classical bit ``i`` holds the measured data qubit ``i``, so the counts' bit order follows the service's
    convention and ``expected`` is the marked item written in the same order.
    """
    if len(region) < REGISTER:
        raise ValueError("the region is too small for four data qubits and two ancillas")
    routed, where = route(logical_grover(marked, iterations), region, edges)
    measured = where[:DATA_QUBITS]
    return to_editor_circuit(routed, len(region), measured), marked

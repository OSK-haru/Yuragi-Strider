"""Small-matrix gate expansion and density-matrix operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import pi, sqrt

import numpy as np

from core.capabilities import normalize_gate_type
from core.circuit_model import GateOperation
from core.internal_profiling import active_internal_profile, elapsed_ms, timer_start


Matrix = tuple[tuple[complex, ...], ...]
Ket = tuple[complex, ...]


@dataclass(frozen=True)
class CachedCollapseOperator:
    """Collapse operator with reusable adjoint products for Lindblad RHS."""

    operator: Matrix
    operator_adjoint: Matrix
    operator_adjoint_operator: Matrix


I: Matrix = (
    (1.0 + 0.0j, 0.0 + 0.0j),
    (0.0 + 0.0j, 1.0 + 0.0j),
)
H: Matrix = (
    (1.0 / sqrt(2.0), 1.0 / sqrt(2.0)),
    (1.0 / sqrt(2.0), -1.0 / sqrt(2.0)),
)
X: Matrix = (
    (0.0 + 0.0j, 1.0 + 0.0j),
    (1.0 + 0.0j, 0.0 + 0.0j),
)
Y: Matrix = (
    (0.0 + 0.0j, 0.0 - 1.0j),
    (0.0 + 1.0j, 0.0 + 0.0j),
)
Z: Matrix = (
    (1.0 + 0.0j, 0.0 + 0.0j),
    (0.0 + 0.0j, -1.0 + 0.0j),
)
S: Matrix = (
    (1.0 + 0.0j, 0.0 + 0.0j),
    (0.0 + 0.0j, 0.0 + 1.0j),
)
T: Matrix = (
    (1.0 + 0.0j, 0.0 + 0.0j),
    (0.0 + 0.0j, complex(sqrt(0.5), sqrt(0.5))),
)
SIGMA_MINUS: Matrix = (
    (0.0 + 0.0j, 1.0 + 0.0j),
    (0.0 + 0.0j, 0.0 + 0.0j),
)
SIGMA_PLUS: Matrix = (
    (0.0 + 0.0j, 0.0 + 0.0j),
    (1.0 + 0.0j, 0.0 + 0.0j),
)

DEFAULT_GATE_DURATIONS_US = {
    "I": 0.0,
    "H": 0.02,
    "X": 0.02,
    "Y": 0.02,
    "Z": 0.0,
    "S": 0.0,
    "T": 0.0,
    "RX": 0.02,
    "RY": 0.02,
    "RZ": 0.02,
    "CNOT": 0.20,
    "CZ": 0.20,
    "SWAP": 0.20,
    "CP": 0.20,
    "CCX": 0.40,
    "MEASURE": 0.0,
    "MESSAGE": 0.04,
}
# QFT and ORACLE span a variable number of qubits, so their logical durations are
# declared per spanned qubit instead of as single entries in the table above.
QFT_DURATION_PER_QUBIT_US = 0.20
ORACLE_DURATION_PER_QUBIT_US = 0.20
INVOLUTION_TOLERANCE = 1e-9
DEGENERATE_EIGENVALUE_TOLERANCE = 1e-9


def expand_single_qubit_gate(gate: Matrix, target: int, n_qubits: int) -> Matrix:
    """Expand a one-qubit gate with q0 as the most significant bit."""

    _require_qubit_index(target, n_qubits)
    expanded: Matrix = ((1.0 + 0.0j,),)
    for qubit in range(n_qubits):
        expanded = tensor(expanded, gate if qubit == target else I)
    return expanded


def expand_cnot(
    control: int | Sequence[int],
    target: int,
    n_qubits: int,
    control_value: int | None = None,
    control_state: int | None = None,
) -> Matrix:
    """Return multi-controlled X; zero bits in the state are open controls."""

    controls = [control] if isinstance(control, int) else list(control)
    if not controls:
        raise ValueError("CNOT requires at least one control")
    for item in controls:
        _require_qubit_index(item, n_qubits)
    _require_qubit_index(target, n_qubits)
    if len(set(controls)) != len(controls) or target in controls:
        raise ValueError("CNOT controls and target must be different")
    if control_state is None:
        control_state = control_value if len(controls) == 1 else (2 ** len(controls) - 1)
    if control_state is None or control_state < 0 or control_state >= 2 ** len(controls):
        raise ValueError("CNOT control_state is outside the control-bit range")
    required_bits = format(control_state, f"0{len(controls)}b")

    dimension = 2 ** n_qubits
    rows = [[0.0 + 0.0j for _ in range(dimension)] for _ in range(dimension)]
    for input_index in range(dimension):
        bits = _index_to_bits(input_index, n_qubits)
        output_bits = list(bits)
        if all(bits[item] == required_bits[index] for index, item in enumerate(controls)):
            output_bits[target] = "0" if output_bits[target] == "1" else "1"
        output_index = _bits_to_index(output_bits)
        rows[output_index][input_index] = 1.0 + 0.0j
    return _freeze(rows)


def expand_cz(control: int, target: int, n_qubits: int) -> Matrix:
    """Return a controlled-Z matrix with q0 as the most significant bit."""

    _require_qubit_index(control, n_qubits)
    _require_qubit_index(target, n_qubits)
    if control == target:
        raise ValueError("CZ control and target must be different")
    dimension = 2 ** n_qubits
    rows = [[0.0 + 0.0j for _ in range(dimension)] for _ in range(dimension)]
    for index in range(dimension):
        bits = _index_to_bits(index, n_qubits)
        rows[index][index] = (
            -1.0 + 0.0j
            if bits[control] == "1" and bits[target] == "1"
            else 1.0 + 0.0j
        )
    return _freeze(rows)


def expand_swap(first: int, second: int, n_qubits: int) -> Matrix:
    """Return a SWAP matrix with q0 as the most significant bit."""

    _require_qubit_index(first, n_qubits)
    _require_qubit_index(second, n_qubits)
    if first == second:
        raise ValueError("SWAP targets must be different")
    dimension = 2 ** n_qubits
    rows = [[0.0 + 0.0j for _ in range(dimension)] for _ in range(dimension)]
    for index in range(dimension):
        bits = list(_index_to_bits(index, n_qubits))
        bits[first], bits[second] = bits[second], bits[first]
        destination = int("".join(bits), 2)
        rows[destination][index] = 1.0 + 0.0j
    return _freeze(rows)


def rz_matrix(theta_rad: float) -> Matrix:
    """Return RZ(theta)=exp(-i theta Z / 2)."""

    theta = float(theta_rad)
    if not np.isfinite(theta):
        raise ValueError("theta_rad must be finite")
    return (
        (complex(np.exp(-0.5j * theta)), 0.0 + 0.0j),
        (0.0 + 0.0j, complex(np.exp(0.5j * theta))),
    )


def rx_matrix(theta_rad: float) -> Matrix:
    """Return RX(theta)=exp(-i theta X / 2)."""

    theta = float(theta_rad)
    if not np.isfinite(theta):
        raise ValueError("theta_rad must be finite")
    cosine = float(np.cos(theta / 2.0))
    sine = float(np.sin(theta / 2.0))
    return (
        (cosine + 0.0j, complex(0.0, -sine)),
        (complex(0.0, -sine), cosine + 0.0j),
    )


def ry_matrix(theta_rad: float) -> Matrix:
    """Return RY(theta)=exp(-i theta Y / 2)."""

    theta = float(theta_rad)
    if not np.isfinite(theta):
        raise ValueError("theta_rad must be finite")
    cosine = float(np.cos(theta / 2.0))
    sine = float(np.sin(theta / 2.0))
    return (
        (cosine + 0.0j, -sine + 0.0j),
        (sine + 0.0j, cosine + 0.0j),
    )


def message_matrix(animation_parameter_t: float) -> Matrix:
    """Ordered Message(t): exp(-iYt), then X^t (global phase omitted)."""

    t = float(animation_parameter_t)
    if not np.isfinite(t):
        raise ValueError("animation_parameter_t must be finite")
    # RY(theta)=exp(-i theta Y / 2); X^t is RX(pi*t) up to global phase.
    return matmul(rx_matrix(pi * t), ry_matrix(2.0 * t))


def expand_controlled_phase(
    control: int,
    target: int,
    n_qubits: int,
    theta_rad: float,
) -> Matrix:
    """Return diag(1, 1, 1, exp(i theta)) on the selected qubits."""

    _require_qubit_index(control, n_qubits)
    _require_qubit_index(target, n_qubits)
    if control == target:
        raise ValueError("CP control and target must be different")
    theta = float(theta_rad)
    if not np.isfinite(theta):
        raise ValueError("theta_rad must be finite")
    dimension = 2 ** n_qubits
    phase = complex(np.exp(1.0j * theta))
    rows = [[0.0 + 0.0j for _ in range(dimension)] for _ in range(dimension)]
    for index in range(dimension):
        bits = _index_to_bits(index, n_qubits)
        rows[index][index] = (
            phase
            if bits[control] == "1" and bits[target] == "1"
            else 1.0 + 0.0j
        )
    return _freeze(rows)


def expand_ccx(
    control_a: int,
    control_b: int,
    target: int,
    n_qubits: int,
) -> Matrix:
    """Return a doubly-controlled X (Toffoli) matrix."""

    for qubit in (control_a, control_b, target):
        _require_qubit_index(qubit, n_qubits)
    if len({control_a, control_b, target}) != 3:
        raise ValueError("CCX controls and target must be different")
    dimension = 2 ** n_qubits
    rows = [[0.0 + 0.0j for _ in range(dimension)] for _ in range(dimension)]
    for index in range(dimension):
        bits = list(_index_to_bits(index, n_qubits))
        if bits[control_a] == "1" and bits[control_b] == "1":
            bits[target] = "0" if bits[target] == "1" else "1"
        destination = int("".join(bits), 2)
        rows[destination][index] = 1.0 + 0.0j
    return _freeze(rows)


def expand_qft(targets: Sequence[int], n_qubits: int) -> Matrix:
    """Return the QFT over an arbitrary ordered subset of qubits.

    ``targets[0]`` is the most significant bit of the transformed register and
    qubits outside ``targets`` are left untouched.  With ``N = 2 ** len(targets)``
    the register map is the standard convention

    ``|j> -> N ** -0.5 * sum_k exp(2 pi i j k / N) |k>``.
    """

    register = qft_register(targets)
    for qubit in register:
        _require_qubit_index(qubit, n_qubits)
    register_size = len(register)
    register_dimension = 2 ** register_size
    normalization = 1.0 / sqrt(register_dimension)
    dimension = 2 ** n_qubits
    rows = [[0.0 + 0.0j for _ in range(dimension)] for _ in range(dimension)]
    for column in range(dimension):
        bits = _index_to_bits(column, n_qubits)
        source_value = int("".join(bits[qubit] for qubit in register), 2)
        for output_value in range(register_dimension):
            output_bits = list(bits)
            value_bits = _index_to_bits(output_value, register_size)
            for position, qubit in enumerate(register):
                output_bits[qubit] = value_bits[position]
            phase = 2.0 * pi * source_value * output_value / register_dimension
            rows[_bits_to_index(output_bits)][column] = normalization * complex(
                np.exp(1.0j * phase)
            )
    return _freeze(rows)


def register_targets(targets: Sequence[int], label: str) -> tuple[int, ...]:
    """Validate and return an ordered operand register (most significant first)."""

    register = tuple(int(target) for target in targets)
    if not register:
        raise ValueError(f"{label} requires at least one target")
    if len(set(register)) != len(register):
        raise ValueError(f"{label} targets must be different")
    return register


def qft_register(targets: Sequence[int]) -> tuple[int, ...]:
    """Validate and return the ordered QFT register (most significant first)."""

    return register_targets(targets, "QFT")


def oracle_register(targets: Sequence[int]) -> tuple[int, ...]:
    """Validate and return the ordered oracle register (most significant first)."""

    return register_targets(targets, "ORACLE")


def oracle_marked_index(
    params: dict[str, float] | None,
    register_size: int,
) -> int:
    """Read and range-check the basis state an oracle marks."""

    value = float((params or {}).get("marked_index", 0.0))
    if not np.isfinite(value) or value != int(value):
        raise ValueError("ORACLE marked_index must be a finite integer")
    index = int(value)
    if not 0 <= index < 2 ** register_size:
        raise ValueError(
            "ORACLE marked_index must be inside the register range "
            f"0..{2 ** register_size - 1}"
        )
    return index


def expand_phase_oracle(
    targets: Sequence[int],
    n_qubits: int,
    marked_index: int,
) -> Matrix:
    """Return the phase oracle ``I - 2|m><m|`` over an ordered sub-register.

    ``targets[0]`` is the most significant bit of the marked value and qubits
    outside ``targets`` are untouched. The matrix is diagonal with -1 on every
    basis state whose register bits equal ``marked_index``, so it is Hermitian
    and involutory and takes the gate-aware involution generator branch.
    """

    register = oracle_register(targets)
    for qubit in register:
        _require_qubit_index(qubit, n_qubits)
    register_size = len(register)
    if not 0 <= int(marked_index) < 2 ** register_size:
        raise ValueError("ORACLE marked_index is outside the register range")
    marked_bits = _index_to_bits(int(marked_index), register_size)
    dimension = 2 ** n_qubits
    rows = [[0.0 + 0.0j for _ in range(dimension)] for _ in range(dimension)]
    for index in range(dimension):
        bits = _index_to_bits(index, n_qubits)
        is_marked = all(
            bits[qubit] == marked_bits[position]
            for position, qubit in enumerate(register)
        )
        rows[index][index] = -1.0 + 0.0j if is_marked else 1.0 + 0.0j
    return _freeze(rows)


def oracle_duration_us(target_count: int) -> float:
    """Return the logical-direct oracle duration for a register of this size."""

    if target_count < 1:
        raise ValueError("ORACLE requires at least one target")
    return ORACLE_DURATION_PER_QUBIT_US * float(target_count)


def qft_duration_us(target_count: int) -> float:
    """Return the logical-direct QFT duration for a register of this size."""

    if target_count < 1:
        raise ValueError("QFT requires at least one target")
    return QFT_DURATION_PER_QUBIT_US * float(target_count)


def initial_density_matrix(initial_states: list[str]) -> Matrix:
    """Build a tensor-product density matrix for 0, 1, +, -, +i, and -i states."""

    ket: Ket = (1.0 + 0.0j,)
    for state in initial_states:
        ket = tensor_ket(ket, _single_qubit_ket(state))
    return density_from_ket(ket)


def apply_unitary_to_density(rho: Matrix, u: Matrix) -> Matrix:
    """Return U rho U dagger."""

    if len(rho) >= 16:
        rho_array = np.asarray(rho, dtype=np.complex128)
        unitary_array = np.asarray(u, dtype=np.complex128)
        return _freeze((unitary_array @ rho_array @ unitary_array.conj().T).tolist())
    return matmul(matmul(u, rho), adjoint(u))


def reduced_density_matrix(rho: Matrix, n_qubits: int, target_qubit: int) -> Matrix:
    """Return the target qubit's reduced density matrix (q0 is MSB)."""

    _require_qubit_index(target_qubit, n_qubits)
    dimension = 2 ** n_qubits
    if len(rho) != dimension or any(len(row) != dimension for row in rho):
        raise ValueError("rho dimension must match n_qubits")
    reduced = [[0.0 + 0.0j for _ in range(2)] for _ in range(2)]
    for row in range(dimension):
        row_bits = _index_to_bits(row, n_qubits)
        for column in range(dimension):
            column_bits = _index_to_bits(column, n_qubits)
            if all(row_bits[q] == column_bits[q] for q in range(n_qubits) if q != target_qubit):
                reduced[int(row_bits[target_qubit])][int(column_bits[target_qubit])] += rho[row][column]
    return _freeze(reduced)


def apply_non_selective_computational_measurement(
    rho: Matrix,
    targets: Sequence[int],
    n_qubits: int,
) -> Matrix:
    """Apply sum_m P_m rho P_m while discarding measurement outcomes."""

    measured_targets = tuple(dict.fromkeys(int(target) for target in targets))
    for target in measured_targets:
        _require_qubit_index(target, n_qubits)
    dimension = 2 ** n_qubits
    if len(rho) != dimension or any(len(row) != dimension for row in rho):
        raise ValueError("rho dimension must match n_qubits")
    if not measured_targets:
        return tuple(tuple(complex(value) for value in row) for row in rho)

    rows: list[list[complex]] = []
    for row_index, row in enumerate(rho):
        row_bits = _index_to_bits(row_index, n_qubits)
        measured_row: list[complex] = []
        for column_index, value in enumerate(row):
            column_bits = _index_to_bits(column_index, n_qubits)
            keeps_coherence = all(
                row_bits[target] == column_bits[target]
                for target in measured_targets
            )
            measured_row.append(complex(value) if keeps_coherence else 0.0 + 0.0j)
        rows.append(measured_row)
    return _freeze(rows)


def computational_measurement_outcomes(
    rho: Matrix,
    targets: Sequence[int],
    n_qubits: int,
) -> dict[str, tuple[float, Matrix]]:
    """Return normalized conditional states for every computational outcome.

    This is the instrument counterpart to
    :func:`apply_non_selective_computational_measurement`. Outcomes with zero
    probability are retained with a zero probability and the original matrix
    as a harmless placeholder, so callers can build a stable classical
    register schema without special-casing basis labels.
    """

    measured_targets = tuple(dict.fromkeys(int(target) for target in targets))
    for target in measured_targets:
        _require_qubit_index(target, n_qubits)
    dimension = 2 ** n_qubits
    if len(rho) != dimension or any(len(row) != dimension for row in rho):
        raise ValueError("rho dimension must match n_qubits")
    if not measured_targets:
        return {"": (1.0, _freeze(rho))}

    outcomes: dict[str, tuple[float, Matrix]] = {}
    for outcome_index in range(2 ** len(measured_targets)):
        outcome = format(outcome_index, f"0{len(measured_targets)}b")
        selected = set()
        for basis_index in range(dimension):
            bits = _index_to_bits(basis_index, n_qubits)
            if all(bits[target] == outcome[position]
                   for position, target in enumerate(measured_targets)):
                selected.add(basis_index)
        projected = _freeze(tuple(
            tuple(
                complex(rho[row][column])
                if row in selected and column in selected
                else 0.0 + 0.0j
                for column in range(dimension)
            )
            for row in range(dimension)
        ))
        probability = max(0.0, float(trace(projected).real))
        if probability > 1e-15:
            normalized = _freeze(tuple(
                tuple(value / probability for value in row)
                for row in projected
            ))
        else:
            normalized = _freeze(rho)
            probability = 0.0
        outcomes[outcome] = (probability, normalized)
    return outcomes


def apply_gate_operation(rho: Matrix, gate: GateOperation, n_qubits: int) -> Matrix:
    """Apply one supported circuit operation to a density matrix."""

    if normalize_gate_type(gate.type) == "MEASURE":
        return apply_non_selective_computational_measurement(
            rho,
            gate.targets,
            n_qubits,
        )
    return apply_unitary_to_density(rho, gate_unitary(gate, n_qubits))


def gate_duration_us(gate: GateOperation) -> float:
    """Return the assigned gate duration in microseconds."""

    gate_type = normalize_gate_type(gate.type)
    if gate_type == "QFT":
        default_duration = qft_duration_us(len(gate.targets))
    elif gate_type == "ORACLE":
        default_duration = oracle_duration_us(len(gate.targets))
    else:
        default_duration = DEFAULT_GATE_DURATIONS_US.get(gate_type)
    duration = (gate.params or {}).get("duration_us", default_duration)
    if duration is None:
        raise ValueError(f"unsupported gate type: {gate.type}")
    duration = float(duration)
    if duration < 0.0:
        raise ValueError("gate duration_us must be non-negative")
    return duration


def column_duration_us(column) -> float:
    """Return the maximum duration of all gates in a circuit column."""

    if not column.gates:
        return 0.0
    return max(gate_duration_us(gate) for gate in column.gates)


def gate_unitary(gate: GateOperation, n_qubits: int) -> Matrix:
    """Return the expanded unitary matrix for one supported gate."""

    gate_type = normalize_gate_type(gate.type)
    dimension = 2 ** n_qubits
    if gate_type == "MEASURE":
        return identity_matrix(dimension)
    if gate_type == "CNOT":
        if len(gate.controls or []) < 1 or len(gate.targets) != 1:
            raise ValueError("CNOT requires at least one control and exactly one target")
        params = gate.params or {}
        raw_control_value = params.get("control_value", 1.0)
        raw_control_state = params.get("control_state")
        if raw_control_value not in {0.0, 1.0}:
            raise ValueError("CNOT control_value must be 0 or 1")
        if raw_control_state is not None and (
            raw_control_state != int(raw_control_state)
            or raw_control_state < 0
            or raw_control_state >= 2 ** len(gate.controls)
        ):
            raise ValueError("CNOT control_state is outside the control-bit range")
        return expand_cnot(
            gate.controls, gate.targets[0], n_qubits,
            control_value=int(raw_control_value),
            control_state=None if raw_control_state is None else int(raw_control_state),
        )
    if gate_type == "CZ":
        if len(gate.controls or []) != 1 or len(gate.targets) != 1:
            raise ValueError("CZ requires exactly one control and one target")
        return expand_cz(gate.controls[0], gate.targets[0], n_qubits)
    if gate_type == "CP":
        if len(gate.controls or []) != 1 or len(gate.targets) != 1:
            raise ValueError("CP requires exactly one control and one target")
        return expand_controlled_phase(
            gate.controls[0],
            gate.targets[0],
            n_qubits,
            _gate_angle_rad(gate),
        )
    if gate_type == "CCX":
        if len(gate.controls or []) != 2 or len(gate.targets) != 1:
            raise ValueError("CCX requires exactly two controls and one target")
        return expand_ccx(
            gate.controls[0], gate.controls[1], gate.targets[0], n_qubits
        )
    if gate_type == "SWAP":
        if gate.controls or len(gate.targets) != 2:
            raise ValueError("SWAP requires exactly two targets and no controls")
        return expand_swap(gate.targets[0], gate.targets[1], n_qubits)
    if gate_type == "QFT":
        if gate.controls:
            raise ValueError("QFT requires targets only and no controls")
        return expand_qft(gate.targets, n_qubits)
    if gate_type == "ORACLE":
        if gate.controls:
            raise ValueError("ORACLE requires targets only and no controls")
        return expand_phase_oracle(
            gate.targets,
            n_qubits,
            oracle_marked_index(gate.params, len(gate.targets)),
        )
    if gate_type in {"RX", "RY", "RZ"}:
        if len(gate.targets) != 1 or gate.controls:
            raise ValueError(
                f"{gate_type} requires exactly one target and no controls"
            )
        rotation = {
            "RX": rx_matrix,
            "RY": ry_matrix,
            "RZ": rz_matrix,
        }[gate_type](_gate_angle_rad(gate))
        return expand_single_qubit_gate(
            rotation,
            gate.targets[0],
            n_qubits,
        )
    if gate_type == "MESSAGE":
        if len(gate.targets) != 1 or gate.controls:
            raise ValueError("MESSAGE requires exactly one target and no controls")
        t = float((gate.params or {}).get("animation_parameter_t", 0.0)) % 1.0
        return expand_single_qubit_gate(
            message_matrix(t),
            gate.targets[0],
            n_qubits,
        )

    one_qubit_gate = {
        "I": I,
        "H": H,
        "X": X,
        "Y": Y,
        "Z": Z,
        "S": S,
        "T": T,
    }.get(gate_type)
    if one_qubit_gate is None:
        raise ValueError(f"unsupported gate type: {gate.type}")
    if len(gate.targets) != 1:
        raise ValueError(f"{gate.type} requires exactly one target")
    return expand_single_qubit_gate(one_qubit_gate, gate.targets[0], n_qubits)


def _gate_angle_rad(gate: GateOperation) -> float:
    theta = float((gate.params or {}).get("theta_rad", pi / 2.0))
    if not np.isfinite(theta):
        raise ValueError(f"{gate.type} theta_rad must be finite")
    return theta


def column_unitary(column, n_qubits: int) -> Matrix:
    """Return the unitary for all gates in a column, applied in listed order."""

    unitary = identity_matrix(2 ** n_qubits)
    for gate in column.gates:
        unitary = matmul(gate_unitary(gate, n_qubits), unitary)
    return unitary


def effective_hamiltonian_from_involution(
    unitary: Matrix,
    duration_us: float,
    *,
    validate: bool = True,
) -> Matrix:
    """Return H = pi / (2 tau) * (I - U) for Hermitian involutory U."""

    duration_us = float(duration_us)
    if duration_us <= 0.0:
        raise ValueError("duration_us must be positive to build a Hamiltonian")
    dimension = len(unitary)
    identity = identity_matrix(dimension)
    if validate:
        if _max_abs_difference(adjoint(unitary), unitary) > INVOLUTION_TOLERANCE:
            raise ValueError("column unitary must be Hermitian for effective Hamiltonian mode")
        if _max_abs_difference(matmul(unitary, unitary), identity) > INVOLUTION_TOLERANCE:
            raise ValueError("column unitary must satisfy U^2 = I for effective Hamiltonian mode")
    return scale(pi / (2.0 * duration_us), subtract(identity, unitary))


def effective_hamiltonian_from_unitary(
    unitary: Matrix,
    duration_us: float,
) -> Matrix:
    """Return a Hermitian generator for any small unitary gate column.

    Existing Hermitian involutions retain their original generator and hence
    their established within-gate trajectory.  Other unitary matrices use the
    principal eigenphase logarithm: if ``U v = exp(i theta) v``, then
    ``H v = -theta / duration * v`` so ``exp(-i H duration) = U``.
    """

    duration = float(duration_us)
    if duration <= 0.0 or not np.isfinite(duration):
        raise ValueError("duration_us must be finite and positive")
    array = np.asarray(unitary, dtype=np.complex128)
    if array.ndim != 2 or array.shape[0] == 0 or array.shape[0] != array.shape[1]:
        raise ValueError("unitary must be a non-empty square matrix")
    if not np.all(np.isfinite(array.real)) or not np.all(np.isfinite(array.imag)):
        raise ValueError("unitary must contain finite values")

    identity = np.eye(array.shape[0], dtype=np.complex128)
    if np.max(np.abs(array.conj().T @ array - identity)) > INVOLUTION_TOLERANCE:
        raise ValueError("matrix must be unitary to build an effective Hamiltonian")

    if (
        np.max(np.abs(array.conj().T - array)) <= INVOLUTION_TOLERANCE
        and np.max(np.abs(array @ array - identity)) <= INVOLUTION_TOLERANCE
    ):
        return effective_hamiltonian_from_involution(unitary, duration)

    generator = _general_eigensystem_generator(array, duration)
    if not _reproduces_unitary(generator, array, duration):
        # ``np.linalg.eig`` may return a non-orthogonal basis inside a degenerate
        # eigenspace, and Hermitizing that generator then no longer reproduces
        # the gate.  Multi-qubit QFT hits this because its spectrum is only the
        # four fourth roots of unity regardless of register size.
        generator = _normal_unitary_generator(array, duration)
        if not _reproduces_unitary(generator, array, duration):
            raise ValueError(
                "failed to reconstruct unitary from effective Hamiltonian"
            )
    return _freeze(generator.tolist())


def _general_eigensystem_generator(
    array: np.ndarray,
    duration_us: float,
) -> np.ndarray | None:
    """Return the principal-eigenphase generator, or None if unusable."""

    eigenvalues, eigenvectors = np.linalg.eig(array)
    if np.linalg.cond(eigenvectors) > 1.0 / INVOLUTION_TOLERANCE:
        return None
    eigenphases = np.angle(eigenvalues)
    generator = (
        eigenvectors
        @ np.diag(-eigenphases / duration_us)
        @ np.linalg.inv(eigenvectors)
    )
    return 0.5 * (generator + generator.conj().T)


def _normal_unitary_generator(
    array: np.ndarray,
    duration_us: float,
) -> np.ndarray:
    """Return the principal generator built on an orthonormal eigenbasis.

    Unitary matrices are normal, so their Hermitian and anti-Hermitian parts
    commute and can be diagonalized together.  Two ``eigh`` passes therefore
    give an exactly orthonormal eigenbasis even for degenerate eigenvalues.
    """

    hermitian_part = 0.5 * (array + array.conj().T)
    anti_hermitian_part = -0.5j * (array - array.conj().T)
    values, basis = np.linalg.eigh(hermitian_part)
    blocks: list[np.ndarray] = []
    block_start = 0
    for index in range(1, values.size + 1):
        if (
            index < values.size
            and values[index] - values[index - 1] <= DEGENERATE_EIGENVALUE_TOLERANCE
        ):
            continue
        block = basis[:, block_start:index]
        reduced = block.conj().T @ anti_hermitian_part @ block
        _, rotation = np.linalg.eigh(0.5 * (reduced + reduced.conj().T))
        blocks.append(block @ rotation)
        block_start = index
    eigenbasis = np.hstack(blocks)
    eigenvalues = np.einsum(
        "ji,jk,ki->i", eigenbasis.conj(), array, eigenbasis
    )
    generator = (
        eigenbasis * (-np.angle(eigenvalues) / duration_us)
    ) @ eigenbasis.conj().T
    return 0.5 * (generator + generator.conj().T)


def _reproduces_unitary(
    generator: np.ndarray | None,
    array: np.ndarray,
    duration_us: float,
) -> bool:
    if generator is None:
        return False
    reconstructed = _unitary_array_from_hermitian(generator, duration_us)
    return bool(
        np.max(np.abs(reconstructed - array)) <= 5.0 * INVOLUTION_TOLERANCE
    )


def unitary_from_hamiltonian(
    hamiltonian: Matrix,
    duration_us: float,
) -> Matrix:
    """Return ``exp(-i H duration)`` for a small Hermitian Hamiltonian."""

    duration = float(duration_us)
    if duration < 0.0 or not np.isfinite(duration):
        raise ValueError("duration_us must be finite and non-negative")
    array = np.asarray(hamiltonian, dtype=np.complex128)
    if array.ndim != 2 or array.shape[0] == 0 or array.shape[0] != array.shape[1]:
        raise ValueError("hamiltonian must be a non-empty square matrix")
    if np.max(np.abs(array - array.conj().T)) > INVOLUTION_TOLERANCE:
        raise ValueError("hamiltonian must be Hermitian")
    return _freeze(_unitary_array_from_hermitian(array, duration).tolist())


def _unitary_array_from_hermitian(
    hamiltonian: np.ndarray,
    duration_us: float,
) -> np.ndarray:
    eigenvalues, eigenvectors = np.linalg.eigh(hamiltonian)
    return (
        eigenvectors
        @ np.diag(np.exp(-1.0j * eigenvalues * duration_us))
        @ eigenvectors.conj().T
    )


def output_probabilities(rho: Matrix, n_qubits: int) -> dict[str, float]:
    """Return computational-basis probabilities from the density diagonal."""

    probabilities: dict[str, float] = {}
    for index, row in enumerate(rho):
        probabilities[_basis_label(index, n_qubits)] = _as_probability(row[index].real)
    return probabilities


def apply_readout_error(
    probabilities: Mapping[str, float],
    n_qubits: int,
    assignment_errors: Sequence[tuple[float, float]],
) -> dict[str, float]:
    """Apply per-qubit assignment errors to computational-basis probabilities.

    The readout model is the affine two-point approximation used by
    superconducting hardware calibration. For qubit ``k`` the column-stochastic
    confusion matrix is

        A_k = [[1 - p10, p01],
               [p10, 1 - p01]]

    with ``p10 = P(observe 1 | prepare 0)`` and ``p01 = P(observe 0 | prepare 1)``.
    The observed distribution is the tensor product of the per-qubit matrices
    applied to the true distribution. Qubits are applied sequentially so the
    2^n x 2^n product is never materialised.

    This is an *observation* stage: it never touches the density matrix, so
    state-level metrics such as fidelity and purity remain uncontaminated.
    """

    if len(assignment_errors) != n_qubits:
        raise ValueError(
            f"assignment_errors must provide {n_qubits} entries, got {len(assignment_errors)}"
        )

    dimension = 1 << n_qubits
    amplitudes = [0.0] * dimension
    for label, value in probabilities.items():
        amplitudes[int(label, 2)] = float(value)

    # Qubit 0 is the most significant bit, matching _basis_label.
    for qubit, (p10, p01) in enumerate(assignment_errors):
        _require_assignment_error(p10, p01, qubit)
        stride = 1 << (n_qubits - 1 - qubit)
        for block in range(0, dimension, stride << 1):
            for offset in range(block, block + stride):
                zero = amplitudes[offset]
                one = amplitudes[offset + stride]
                amplitudes[offset] = (1.0 - p10) * zero + p01 * one
                amplitudes[offset + stride] = p10 * zero + (1.0 - p01) * one

    return {
        _basis_label(index, n_qubits): _as_probability(amplitudes[index])
        for index in range(dimension)
    }


def apply_local_readout_error(
    probabilities: Mapping[str, float],
    n_subsystems: int,
    local_levels: int,
    assignment_errors: Sequence[tuple[float, float]],
) -> dict[str, float]:
    """Apply independent binary assignment errors to qubit or qutrit labels.

    Levels ``0`` and ``1`` are mixed by the same affine two-point model used by
    :func:`apply_readout_error`.  Any higher local level is preserved.  This
    makes the approximation usable for pulse qutrit outputs without pretending
    that a two-point calibration can describe ``|2>`` discrimination.
    """

    if n_subsystems < 1:
        raise ValueError("n_subsystems must be positive")
    if local_levels not in (2, 3):
        raise ValueError("local_levels must be 2 or 3")
    if len(assignment_errors) != n_subsystems:
        raise ValueError(
            "assignment_errors must provide "
            f"{n_subsystems} entries, got {len(assignment_errors)}"
        )

    expected_labels = {
        _local_basis_label(index, n_subsystems, local_levels)
        for index in range(local_levels ** n_subsystems)
    }
    if set(probabilities) != expected_labels:
        raise ValueError("probabilities must contain every local basis label exactly once")

    observed = {label: float(probabilities[label]) for label in expected_labels}
    for subsystem, (p10, p01) in enumerate(assignment_errors):
        _require_assignment_error(p10, p01, subsystem)
        updated = {label: 0.0 for label in expected_labels}
        for label, value in observed.items():
            level = label[subsystem]
            if level == "0":
                updated[label] += (1.0 - p10) * value
                flipped = f"{label[:subsystem]}1{label[subsystem + 1:]}"
                updated[flipped] += p10 * value
            elif level == "1":
                updated[label] += (1.0 - p01) * value
                flipped = f"{label[:subsystem]}0{label[subsystem + 1:]}"
                updated[flipped] += p01 * value
            else:
                updated[label] += value
        observed = updated

    return {label: _as_probability(observed[label]) for label in sorted(expected_labels)}


def _local_basis_label(index: int, width: int, base: int) -> str:
    digits = ["0"] * width
    remainder = index
    for position in range(width - 1, -1, -1):
        digits[position] = str(remainder % base)
        remainder //= base
    return "".join(digits)


def _require_assignment_error(p10: float, p01: float, qubit: int) -> None:
    if not 0.0 <= p10 <= 1.0 or not 0.0 <= p01 <= 1.0:
        raise ValueError(
            f"qubit {qubit} assignment errors must lie in [0, 1], got p10={p10}, p01={p01}"
        )
    if p10 + p01 >= 1.0:
        raise ValueError(
            f"qubit {qubit} assignment span must be positive: p10 + p01 = {p10 + p01} >= 1"
        )


def multi_qubit_collapse_operators(
    n_qubits: int,
    downward_rate_per_us: float,
    dephasing_rate_per_us: float,
) -> list[Matrix]:
    """Create legacy downward-relaxation and dephasing operators for each qubit."""

    collapse_ops: list[Matrix] = []
    if downward_rate_per_us > 0.0:
        relaxation = sqrt(downward_rate_per_us)
        for qubit in range(n_qubits):
            collapse_ops.append(
                scale(relaxation, expand_single_qubit_gate(SIGMA_MINUS, qubit, n_qubits))
            )
    if dephasing_rate_per_us > 0.0:
        dephasing = sqrt(dephasing_rate_per_us / 2.0)
        for qubit in range(n_qubits):
            collapse_ops.append(
                scale(dephasing, expand_single_qubit_gate(Z, qubit, n_qubits))
            )
    return collapse_ops


def multi_qubit_physical_collapse_operators(
    n_qubits: int,
    gamma_down_per_us: float,
    gamma_up_per_us: float,
    gamma_phi_per_us: float,
) -> list[Matrix]:
    """Create finite-temperature relaxation, excitation, and dephasing operators."""

    collapse_ops: list[Matrix] = []
    if gamma_down_per_us > 0.0:
        relaxation = sqrt(gamma_down_per_us)
        for qubit in range(n_qubits):
            collapse_ops.append(
                scale(relaxation, expand_single_qubit_gate(SIGMA_MINUS, qubit, n_qubits))
            )
    if gamma_up_per_us > 0.0:
        excitation = sqrt(gamma_up_per_us)
        for qubit in range(n_qubits):
            collapse_ops.append(
                scale(excitation, expand_single_qubit_gate(SIGMA_PLUS, qubit, n_qubits))
            )
    if gamma_phi_per_us > 0.0:
        dephasing = sqrt(gamma_phi_per_us / 2.0)
        for qubit in range(n_qubits):
            collapse_ops.append(
                scale(dephasing, expand_single_qubit_gate(Z, qubit, n_qubits))
            )
    return collapse_ops


def multi_qubit_environment_collapse_operators(
    n_qubits: int,
    rates,
) -> list[Matrix]:
    """Create unified environment collapse operators from EnvironmentRates."""

    if isinstance(rates, Sequence):
        if len(rates) != n_qubits:
            raise ValueError(
                f"local environment rates must match n_qubits ({n_qubits})"
            )
        collapse_ops: list[Matrix] = []
        for qubit, local_rates in enumerate(rates):
            down = float(getattr(local_rates, "gamma_down_per_us"))
            up = float(getattr(local_rates, "gamma_up_per_us"))
            dephasing = float(getattr(local_rates, "gamma_phi_per_us"))
            if down > 0.0:
                collapse_ops.append(scale(
                    sqrt(down),
                    expand_single_qubit_gate(SIGMA_MINUS, qubit, n_qubits),
                ))
            if up > 0.0:
                collapse_ops.append(scale(
                    sqrt(up),
                    expand_single_qubit_gate(SIGMA_PLUS, qubit, n_qubits),
                ))
            if dephasing > 0.0:
                collapse_ops.append(scale(
                    sqrt(dephasing / 2.0),
                    expand_single_qubit_gate(Z, qubit, n_qubits),
                ))
        return collapse_ops

    return multi_qubit_physical_collapse_operators(
        n_qubits,
        float(getattr(rates, "gamma_down_per_us")),
        float(getattr(rates, "gamma_up_per_us")),
        float(getattr(rates, "gamma_phi_per_us")),
    )


def prepare_collapse_operators(
    collapse_ops: Sequence[Matrix],
) -> tuple[CachedCollapseOperator, ...]:
    """Precompute adjoint products for repeated Lindblad RHS evaluations."""

    profile = active_internal_profile()
    cached = []
    for collapse_op in collapse_ops:
        if profile is None:
            operator_adjoint = adjoint(collapse_op)
            operator_adjoint_operator = matmul(operator_adjoint, collapse_op)
        else:
            started_at = timer_start()
            operator_adjoint = adjoint(collapse_op)
            profile.collapse_adjoint_build_count += 1
            profile.collapse_adjoint_build_ms += elapsed_ms(started_at)

            started_at = timer_start()
            operator_adjoint_operator = matmul(operator_adjoint, collapse_op)
            profile.ldagger_l_build_count += 1
            profile.ldagger_l_build_ms += elapsed_ms(started_at)

        cached.append(CachedCollapseOperator(
            operator=collapse_op,
            operator_adjoint=operator_adjoint,
            operator_adjoint_operator=operator_adjoint_operator,
        ))
    return tuple(cached)


def clean_density_matrix(rho: Matrix) -> Matrix:
    rho = scale(0.5, add(rho, adjoint(rho)))
    trace_value = trace(rho)
    if abs(trace_value) == 0.0:
        raise ValueError("density matrix trace vanished during evolution")
    return scale(1.0 / trace_value, rho)


def lindblad_rhs(rho: Matrix, hamiltonian: Matrix, collapse_ops: list[Matrix]) -> Matrix:
    return lindblad_rhs_cached(
        rho,
        hamiltonian,
        prepare_collapse_operators(collapse_ops),
    )


def lindblad_rhs_cached(
    rho: Matrix,
    hamiltonian: Matrix,
    collapse_ops: Sequence[CachedCollapseOperator],
) -> Matrix:
    profile = active_internal_profile()
    if profile is None:
        if _is_zero_matrix(hamiltonian):
            derivative = zero_hamiltonian(len(rho))
        else:
            commutator = subtract(matmul(hamiltonian, rho), matmul(rho, hamiltonian))
            derivative = scale(-1j, commutator)

        for collapse_op in collapse_ops:
            dissipator = subtract(
                matmul(matmul(collapse_op.operator, rho), collapse_op.operator_adjoint),
                scale(
                    0.5,
                    add(
                        matmul(collapse_op.operator_adjoint_operator, rho),
                        matmul(rho, collapse_op.operator_adjoint_operator),
                    ),
                ),
            )
            derivative = add(derivative, dissipator)
        return derivative

    rhs_started_at = timer_start()
    profile.rhs_call_count += 1
    try:
        started_at = timer_start()
        if _is_zero_matrix(hamiltonian):
            derivative = zero_hamiltonian(len(rho))
            profile.zero_hamiltonian_skip_count += 1
        else:
            commutator = subtract(matmul(hamiltonian, rho), matmul(rho, hamiltonian))
            derivative = scale(-1j, commutator)
        profile.hamiltonian_term_ms += elapsed_ms(started_at)

        for collapse_op in collapse_ops:
            profile.dissipator_operator_iterations += 1
            started_at = timer_start()
            dissipator = subtract(
                matmul(matmul(collapse_op.operator, rho), collapse_op.operator_adjoint),
                scale(
                    0.5,
                    add(
                        matmul(collapse_op.operator_adjoint_operator, rho),
                        matmul(rho, collapse_op.operator_adjoint_operator),
                    ),
                ),
            )
            profile.dissipator_total_ms += elapsed_ms(started_at)

            started_at = timer_start()
            derivative = add(derivative, dissipator)
            profile.matrix_accumulation_ms += elapsed_ms(started_at)
        return derivative
    finally:
        profile.rhs_total_ms += elapsed_ms(rhs_started_at)


def rk4_step(
    rho: Matrix,
    hamiltonian: Matrix,
    collapse_ops: list[Matrix],
    dt: float,
) -> Matrix:
    return rk4_step_cached(
        rho,
        hamiltonian,
        prepare_collapse_operators(collapse_ops),
        dt,
    )


def rk4_step_cached(
    rho: Matrix,
    hamiltonian: Matrix,
    collapse_ops: Sequence[CachedCollapseOperator],
    dt: float,
) -> Matrix:
    profile = active_internal_profile()
    if profile is None:
        k1 = lindblad_rhs_cached(rho, hamiltonian, collapse_ops)
        k2 = lindblad_rhs_cached(add(rho, scale(0.5 * dt, k1)), hamiltonian, collapse_ops)
        k3 = lindblad_rhs_cached(add(rho, scale(0.5 * dt, k2)), hamiltonian, collapse_ops)
        k4 = lindblad_rhs_cached(add(rho, scale(dt, k3)), hamiltonian, collapse_ops)
        return add(
            rho,
            scale(
                dt / 6.0,
                add(k1, scale(2.0, k2), scale(2.0, k3), k4),
            ),
        )

    started_at = timer_start()
    profile.rk4_step_count += 1
    try:
        k1 = lindblad_rhs_cached(rho, hamiltonian, collapse_ops)
        k2 = lindblad_rhs_cached(add(rho, scale(0.5 * dt, k1)), hamiltonian, collapse_ops)
        k3 = lindblad_rhs_cached(add(rho, scale(0.5 * dt, k2)), hamiltonian, collapse_ops)
        k4 = lindblad_rhs_cached(add(rho, scale(dt, k3)), hamiltonian, collapse_ops)
        return add(
            rho,
            scale(
                dt / 6.0,
                add(k1, scale(2.0, k2), scale(2.0, k3), k4),
            ),
        )
    finally:
        profile.rk4_total_ms += elapsed_ms(started_at)


def zero_hamiltonian(dimension: int) -> Matrix:
    return tuple(
        tuple(0.0 + 0.0j for _ in range(dimension))
        for _ in range(dimension)
    )


def identity_matrix(dimension: int) -> Matrix:
    return tuple(
        tuple(
            1.0 + 0.0j if row == column else 0.0 + 0.0j
            for column in range(dimension)
        )
        for row in range(dimension)
    )


def matmul(left: Matrix, right: Matrix) -> Matrix:
    profile = active_internal_profile()
    if profile is not None:
        started_at = timer_start()
        try:
            return _matmul(left, right)
        finally:
            elapsed = elapsed_ms(started_at)
            profile.matmul_call_count += 1
            profile.matmul_total_ms += elapsed
            profile.python_matmul_call_count += 1
            profile.python_matmul_total_ms += elapsed
    return _matmul(left, right)


def _matmul(left: Matrix, right: Matrix) -> Matrix:
    if len(left[0]) != len(right):
        raise ValueError("matrix dimensions do not align")
    return tuple(
        tuple(
            sum(left[row][inner] * right[inner][column] for inner in range(len(right)))
            for column in range(len(right[0]))
        )
        for row in range(len(left))
    )


def add(*matrices: Matrix) -> Matrix:
    profile = active_internal_profile()
    if profile is not None:
        started_at = timer_start()
        try:
            return _add(*matrices)
        finally:
            profile.matrix_add_scale_call_count += 1
            profile.matrix_add_scale_total_ms += elapsed_ms(started_at)
    return _add(*matrices)


def _add(*matrices: Matrix) -> Matrix:
    if not matrices:
        raise ValueError("at least one matrix is required")
    return tuple(
        tuple(
            sum(matrix[row][column] for matrix in matrices)
            for column in range(len(matrices[0][0]))
        )
        for row in range(len(matrices[0]))
    )


def subtract(left: Matrix, right: Matrix) -> Matrix:
    return add(left, scale(-1.0, right))


def scale(value: complex, matrix: Matrix) -> Matrix:
    profile = active_internal_profile()
    if profile is not None:
        started_at = timer_start()
        try:
            return _scale(value, matrix)
        finally:
            profile.matrix_add_scale_call_count += 1
            profile.matrix_add_scale_total_ms += elapsed_ms(started_at)
    return _scale(value, matrix)


def _scale(value: complex, matrix: Matrix) -> Matrix:
    return tuple(
        tuple(value * entry for entry in row)
        for row in matrix
    )


def adjoint(matrix: Matrix) -> Matrix:
    profile = active_internal_profile()
    if profile is not None:
        started_at = timer_start()
        try:
            return _adjoint(matrix)
        finally:
            profile.adjoint_call_count += 1
            profile.adjoint_total_ms += elapsed_ms(started_at)
    return _adjoint(matrix)


def _adjoint(matrix: Matrix) -> Matrix:
    return tuple(
        tuple(matrix[row][column].conjugate() for row in range(len(matrix)))
        for column in range(len(matrix[0]))
    )


def _is_zero_matrix(matrix: Matrix) -> bool:
    return all(entry == 0.0 for row in matrix for entry in row)


def trace(matrix: Matrix) -> complex:
    return sum(matrix[index][index] for index in range(len(matrix)))


def tensor(left: Matrix, right: Matrix) -> Matrix:
    return tuple(
        tuple(left_value * right_value for left_value in left_row for right_value in right_row)
        for left_row in left
        for right_row in right
    )


def tensor_ket(left: Ket, right: Ket) -> Ket:
    return tuple(left_value * right_value for left_value in left for right_value in right)


def density_from_ket(ket: Ket) -> Matrix:
    return tuple(
        tuple(row_value * column_value.conjugate() for column_value in ket)
        for row_value in ket
    )


def _single_qubit_ket(state: str) -> Ket:
    normalized = str(state).strip()
    if normalized == "0":
        return (1.0 + 0.0j, 0.0 + 0.0j)
    if normalized == "1":
        return (0.0 + 0.0j, 1.0 + 0.0j)
    if normalized == "+":
        return (1.0 / sqrt(2.0), 1.0 / sqrt(2.0))
    if normalized == "-":
        return (1.0 / sqrt(2.0), -1.0 / sqrt(2.0))
    # Y eigenstates. The API does not accept them; process characterization
    # needs them internally because {0, 1, +} alone cannot reach the Y axis.
    if normalized == "+i":
        return (1.0 / sqrt(2.0), 1.0j / sqrt(2.0))
    if normalized == "-i":
        return (1.0 / sqrt(2.0), -1.0j / sqrt(2.0))
    raise ValueError(f"unsupported initial state: {state}")


def _index_to_bits(index: int, n_qubits: int) -> str:
    return format(index, f"0{n_qubits}b")


def _bits_to_index(bits: list[str]) -> int:
    return int("".join(bits), 2)


def _basis_label(index: int, n_qubits: int) -> str:
    return _index_to_bits(index, n_qubits)


def _require_qubit_index(index: int, n_qubits: int) -> None:
    if not 0 <= int(index) < n_qubits:
        raise ValueError(f"qubit index {index} is outside range for {n_qubits} qubits")


def _freeze(rows: list[list[complex]]) -> Matrix:
    return tuple(tuple(row) for row in rows)


def _max_abs_difference(left: Matrix, right: Matrix) -> float:
    return max(
        abs(left[row][column] - right[row][column])
        for row in range(len(left))
        for column in range(len(left[0]))
    )


def _as_probability(value: float) -> float:
    if value < 0.0 and value > -1e-9:
        return 0.0
    if value > 1.0 and value < 1.0 + 1e-9:
        return 1.0
    return value

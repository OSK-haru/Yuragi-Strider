"""Coupled transmons from device values: the dressed frame, its static shifts, and pulse-level dynamics (D91).

The inputs are what a device reports: each transmon's 0-1 frequency and anharmonicity, measured with its
neighbours in |0>, and the exchange coupling ``J`` of each coupled pair. Nothing else is declared; residual
ZZ, the cross-resonance response to a neighbour's drive and leakage out of the qubit levels all follow.

Model. Each transmon is a Duffing oscillator truncated to ``levels`` levels, and coupled pairs exchange
excitations (rotating-wave approximation, so the total excitation number is conserved)::

    H = sum_i [w_i n_i + alpha_i/2 n_i (n_i - 1)] + sum_<ij> J_ij (a_i^dag a_j + a_i a_j^dag)

The bare ``w_i`` and ``alpha_i`` are not inputs: they are solved so that the *dressed* spectrum has the
measured frequencies and anharmonicities. A calibrated frequency already contains the shift ``J`` causes,
so putting ``J`` on top of the measured value would count that shift twice.

Dressed frame. A gate-level circuit acts on the eigenstates of ``H`` (a calibrated X turns the dressed
qubit), so states are labelled by the product state each eigenstate connects to, and the frame turns every
transmon at its measured 0-1 frequency. In that frame and basis the static Hamiltonian is diagonal:
``frame_energies`` is zero on every label with at most one excitation, the residual ZZ on |11>, the
anharmonicity on |2>, and so on. Labels are only meaningful while each eigenstate is mostly one product
state; where two levels collide (for example ``w_i - w_j = -alpha_i``) the model refuses.

Drives. The control line of transmon ``k`` carries ``Omega(t) cos(w_d t - phase)``; a declared classical
transfer puts a copy on other transmons. In the dressed frame the drive is

    H_d = 1/2 (w^* A_k + w A_k^dag),   w = (Omega + i beta dOmega/dt) exp(i (phase - (w_d - f) t))

with ``A_k`` the line's lowering operator written in the dressed basis and ``f`` the frame frequency of
each transition. ``A_k`` has elements of size ``J / detuning`` on the *neighbour's* transitions, different
for each state of transmon ``k``: that is the cross-resonance effect, and ``drive_element`` returns it.

Relaxation acts while the Hamiltonian does. Its operators are the ladder operators of the dressed labels
with the measured rates (the measured T1 is the dressed state's); the correlated decay that the mixing
adds, of relative size ``(J / detuning)^2``, is neglected. Levels above |1> decay ``k`` times faster
(ladder matrix element) into a bath at the temperature the |1> population implies, and dephasing is
frequency noise, ``sqrt(2 / T_phi) n``.

Truncation: with three levels the shifts of the qubit levels are right to leading order (|11> is pushed
by |20> and |02>), but the shifts of states that hold a |2> miss the push from |3>.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import product
import math

import numpy as np

from core.cptp_liouvillian import dense_matrix_exponential
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, ladder_relaxation_operators
from core.pulse_transmon_network import TransmonExchangeCoupling

MIN_LEVELS, MAX_LEVELS = 2, 4
MAX_DIMENSION = 27                  # the propagator is a dense (dimension^2)^2 matrix
MIN_LABEL_WEIGHT = 0.5              # an eigenstate must be mostly one product state to carry its label
STEP_ANGLE_RAD = 1.0                # one slice turns the fastest term of a driven piece by about this much;
                                    # fourth order: a 50 ns pulse is then within 2e-8, and 16 times closer per halving
_MATCH_TOLERANCE = 1e-9             # rad/us, on the dressed frequencies and anharmonicities
_MATCH_ITERATIONS = 200
_ELEMENT_FLOOR = 1e-9               # drive matrix elements below this are round-off, not transitions
_TWO_PI = 2.0 * math.pi
# Fourth-order commutator-free Magnus step: two exponentials of the generator at the Gauss nodes.
_NODE = math.sqrt(3.0) / 6.0
_WEIGHT_NEAR, _WEIGHT_FAR = 0.25 + _NODE, 0.25 - _NODE


class TransmonCollisionError(ValueError):
    """Two levels of the cluster hybridize: the dressed states can no longer be labelled as product states."""


@dataclass(frozen=True)
class TransmonSpec:
    """One transmon as a device reports it, measured with its neighbours in |0>."""

    frequency_ghz: float                    # 0-1 transition
    anharmonicity_mhz: float | None = None  # f12 - f01, negative; not needed with two levels
    levels: int = 3

    def __post_init__(self) -> None:
        if type(self.levels) is not int or not MIN_LEVELS <= self.levels <= MAX_LEVELS:
            raise ValueError(f"levels must be an integer from {MIN_LEVELS} to {MAX_LEVELS}")
        frequency = float(self.frequency_ghz)
        if not math.isfinite(frequency) or frequency <= 0.0:
            raise ValueError("frequency_ghz must be finite and positive")
        object.__setattr__(self, "frequency_ghz", frequency)
        if self.anharmonicity_mhz is None:
            if self.levels > 2:
                raise ValueError("a transmon with more than two levels needs its anharmonicity")
            return
        anharmonicity = float(self.anharmonicity_mhz)
        if not math.isfinite(anharmonicity) or anharmonicity >= 0.0:
            raise ValueError("anharmonicity_mhz must be finite and negative")
        object.__setattr__(self, "anharmonicity_mhz", anharmonicity)


@dataclass(frozen=True)
class ClusterDrive:
    """A pulse on the control line of transmon ``line``, at absolute times.

    The line carries ``Omega(t) cos(w_d t - phase_rad)`` with ``w_d`` the line transmon's 0-1 frequency plus
    ``frequency_offset_rad_per_us`` and ``Omega = angle_rad / duration_us * shape.density(u)``, so a resonant
    pulse turns the line's own qubit by ``angle_rad`` about the axis at ``phase_rad``. ``drag_us`` adds the
    quadrature ``drag_us * dOmega/dt`` (needs ``shape.slope``). ``shape`` is any object with the area-1
    profile ``density(u)`` on [0, 1] and a ``kind``; ``"square"`` is constant.
    """

    line: int
    start_us: float
    duration_us: float
    angle_rad: float
    shape: object
    phase_rad: float = 0.0
    frequency_offset_rad_per_us: float = 0.0
    drag_us: float = 0.0

    def __post_init__(self) -> None:
        if type(self.line) is not int or self.line < 0:
            raise ValueError("a drive line is a transmon index")
        for name in ("start_us", "duration_us", "angle_rad", "phase_rad", "frequency_offset_rad_per_us", "drag_us"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, value)
        if self.duration_us <= 0.0:
            raise ValueError("a drive needs a positive duration")
        if not callable(getattr(self.shape, "density", None)):
            raise ValueError("a drive shape needs density(u)")
        if self.drag_us != 0.0 and not callable(getattr(self.shape, "slope", None)):
            raise ValueError("a DRAG quadrature needs shape.slope(u)")

    @property
    def end_us(self) -> float:
        return self.start_us + self.duration_us

    @property
    def constant(self) -> bool:
        return getattr(self.shape, "kind", None) == "square" and self.drag_us == 0.0

    def rabi(self, time_us: float) -> complex:
        """``Omega + i drag dOmega/dt`` at an absolute time inside the pulse (rad/us)."""
        u = (time_us - self.start_us) / self.duration_us
        scale = self.angle_rad / self.duration_us
        value = complex(scale * float(self.shape.density(u)))
        if self.drag_us != 0.0:
            value += 1j * self.drag_us * scale / self.duration_us * float(self.shape.slope(u))
        return value


class TransmonCluster:
    """A few coupled transmons, in the dressed frame of their measured frequencies."""

    def __init__(self, transmons: Sequence[TransmonSpec], couplings: Sequence[TransmonExchangeCoupling] = (),
                 line_transfers: Sequence[tuple[int, int, complex]] = ()) -> None:
        """``line_transfers`` are classical leaks ``(source line, target transmon, coefficient)``: the target
        sees the source line's field times the coefficient (``DriveCrosstalkTransfer.coefficient``)."""
        transmons = tuple(transmons)
        if not transmons or any(type(item) is not TransmonSpec for item in transmons):
            raise ValueError("a cluster needs TransmonSpec values")
        self.transmons = transmons
        self.levels = tuple(item.levels for item in transmons)
        self.dimension = math.prod(self.levels)
        if self.dimension > MAX_DIMENSION:
            raise ValueError(f"a cluster may hold at most {MAX_DIMENSION} states")
        count = len(transmons)
        pairs = set()
        for coupling in couplings:
            if type(coupling) is not TransmonExchangeCoupling or max(coupling.left, coupling.right) >= count:
                raise ValueError("couplings must be TransmonExchangeCoupling values inside the cluster")
            pair = frozenset((coupling.left, coupling.right))
            if pair in pairs:
                raise ValueError("an exchange pair is declared twice")
            pairs.add(pair)
        self.couplings = tuple(couplings)
        self.labels = tuple(product(*(range(level) for level in self.levels)))
        self._index = {label: index for index, label in enumerate(self.labels)}
        self._ladder = tuple(_embed(_lowering(level), i, self.levels) for i, level in enumerate(self.levels))
        self._number = tuple(_embed(np.diag(np.arange(level, dtype=float)).astype(complex), i, self.levels)
                             for i, level in enumerate(self.levels))
        occupation = np.array(self.labels, dtype=float)                       # (dimension, count)
        self._occupation = occupation

        # Frequencies relative to transmon 0, so that no phase ever multiplies a GHz-sized number.
        target_offset = np.array([_TWO_PI * 1e3 * (item.frequency_ghz - transmons[0].frequency_ghz)
                                  for item in transmons])
        target_alpha = np.array([0.0 if item.anharmonicity_mhz is None else _TWO_PI * item.anharmonicity_mhz
                                 for item in transmons])
        bare_offset, bare_alpha = target_offset.copy(), target_alpha.copy()
        for _ in range(_MATCH_ITERATIONS):
            energies, vectors = self._diagonalize(bare_offset, bare_alpha)
            offset = np.array([energies[self._single(i, 1)] for i in range(count)])
            alpha = np.array([energies[self._single(i, 2)] - 2.0 * energies[self._single(i, 1)]
                              if self.levels[i] > 2 else 0.0 for i in range(count)])
            miss_offset, miss_alpha = target_offset - offset, target_alpha - alpha
            if max(np.max(np.abs(miss_offset)), np.max(np.abs(miss_alpha))) < _MATCH_TOLERANCE:
                break
            bare_offset, bare_alpha = bare_offset + miss_offset, bare_alpha + miss_alpha
        else:
            raise TransmonCollisionError("the bare parameters that give the measured spectrum did not converge")
        self.bare_offsets_rad_per_us = tuple(float(value) for value in bare_offset)
        self.bare_anharmonicities_rad_per_us = tuple(float(value) for value in bare_alpha)
        self.offsets_rad_per_us = tuple(float(value) for value in target_offset)
        self._vectors = vectors
        frame = energies - occupation @ target_offset
        frame[np.abs(frame) < 1e-12 * max(1.0, float(np.max(np.abs(energies))))] = 0.0
        self.frame_energies = frame                                           # rad/us, per label
        self.lowering = tuple(vectors.conj().T @ ladder @ vectors for ladder in self._ladder)

        line_operator = [np.array(operator) for operator in self.lowering]
        seen = set()
        for source, target, coefficient in line_transfers:
            if (type(source) is not int or type(target) is not int or source == target
                    or not 0 <= source < count or not 0 <= target < count or (source, target) in seen):
                raise ValueError("line transfers join two different transmons of the cluster, once each")
            seen.add((source, target))
            coefficient = complex(coefficient)
            if not math.isfinite(abs(coefficient)):
                raise ValueError("a line transfer must be finite")
            line_operator[source] = line_operator[source] + np.conj(coefficient) * self.lowering[target]
        self.line_operators = tuple(line_operator)
        self._line_super = tuple((_commutator(operator), _commutator(operator.conj().T))
                                 for operator in self.line_operators)

    # ----- spectrum ---------------------------------------------------------------------------------------

    def _single(self, transmon: int, level: int) -> int:
        label = [0] * len(self.levels)
        label[transmon] = level
        return self._index[tuple(label)]

    def _diagonalize(self, offset: np.ndarray, alpha: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Energies and eigenvectors ordered by label, relative to the ground state and transmon 0's frame."""
        hamiltonian = np.zeros((self.dimension, self.dimension), dtype=complex)
        for i, number in enumerate(self._number):
            hamiltonian += offset[i] * number + 0.5 * alpha[i] * (number @ number - number)
        for coupling in self.couplings:
            hop = self._ladder[coupling.left].conj().T @ self._ladder[coupling.right]
            hamiltonian += coupling.strength_rad_per_us * (hop + hop.conj().T)
        values, vectors = np.linalg.eigh(hamiltonian)
        weight = np.abs(vectors) ** 2
        owner = np.argmax(weight, axis=0)                     # the product state each eigenvector is mostly
        if np.any(weight[owner, np.arange(self.dimension)] <= MIN_LABEL_WEIGHT):
            raise TransmonCollisionError(
                "two levels of the cluster hybridize (a frequency collision); the dressed states have no labels")
        order = np.argsort(owner)                             # injective: each weight is above one half
        values, vectors = values[order], vectors[:, order]
        phase = np.diag(vectors) / np.abs(np.diag(vectors))   # the leading component real and positive
        vectors = vectors / phase[None, :]
        return values - values[0], vectors

    @property
    def mixing(self) -> float:
        """Largest weight any dressed state has outside its own product state."""
        return float(1.0 - np.min(np.abs(np.diag(self._vectors)) ** 2))

    def label_index(self, label: Sequence[int]) -> int:
        return self._index[tuple(int(level) for level in label)]

    def zz_rad_per_us(self, left: int, right: int) -> float:
        """Residual ZZ of a pair with every other transmon in |0>: ``E11 - E10 - E01 + E00``."""
        label = [0] * len(self.levels)
        label[left] = label[right] = 1
        return float(self.frame_energies[self._index[tuple(label)]])

    def drive_element(self, line: int, target: int, others: Sequence[int] | None = None) -> complex:
        """How strongly line ``line`` drives ``target``'s 0-1 transition, per unit Rabi rate on its own qubit.

        ``others`` gives every transmon's level (``target``'s entry is ignored). A pulse on ``line`` at the
        target's frequency turns the target at ``Omega`` times this element; for ``line != target`` it depends
        on the line transmon's own level, which is the cross-resonance effect.
        """
        label = [0] * len(self.levels) if others is None else [int(level) for level in others]
        label[target] = 0
        low = self._index[tuple(label)]
        label[target] = 1
        return complex(self.line_operators[line][low, self._index[tuple(label)]])

    # ----- dynamics ---------------------------------------------------------------------------------------

    def collapse_operators(self, relaxation: Sequence[IdleQubitSpec | None]) -> list[np.ndarray]:
        """Lindblad operators of the transmons that relax (``None``: that transmon does not, in this interval)."""
        if len(relaxation) != len(self.levels):
            raise ValueError("one relaxation entry per transmon is required")
        operators = []
        for i, spec in enumerate(relaxation):
            if spec is None:
                continue
            if type(spec) is not IdleQubitSpec:
                raise ValueError("relaxation entries are IdleQubitSpec values or None")
            operators += [_embed(local, i, self.levels)
                          for local in _local_collapse(self.transmons[i], spec)]
        return operators

    def generator(self, relaxation: Sequence[IdleQubitSpec | None]) -> np.ndarray:
        """The undriven Lindblad generator in the dressed frame (column-major vectorization, 1/us)."""
        return _dissipator(self.collapse_operators(relaxation), self.dimension) + _commutator(
            np.diag(self.frame_energies).astype(complex))

    def propagator(self, start_us: float, end_us: float, drives: Sequence[ClusterDrive] = (),
                   relaxation: Sequence[IdleQubitSpec | None] | None = None, *,
                   step_angle_rad: float = STEP_ANGLE_RAD) -> np.ndarray:
        """The map of the cluster over ``[start_us, end_us)`` in the dressed frame, as a superoperator.

        Column-major vectorization (``vec(rho)[column * D + row]``), labels with transmon 0 most significant.
        ``drives`` may start before or end after the interval; only their part inside acts.
        """
        start, end = float(start_us), float(end_us)
        if not (math.isfinite(start) and math.isfinite(end)) or end < start:
            raise ValueError("a propagation interval must be finite and ordered")
        if not math.isfinite(step_angle_rad) or step_angle_rad <= 0.0:
            raise ValueError("step_angle_rad must be positive")
        for drive in drives:
            if type(drive) is not ClusterDrive or drive.line >= len(self.levels):
                raise ValueError("drives must be ClusterDrive values on a transmon of the cluster")
        base = self.generator(relaxation if relaxation is not None else (None,) * len(self.levels))
        identity = np.eye(self.dimension ** 2, dtype=complex)
        if end == start:
            return identity
        edges = sorted({start, end} | {edge for drive in drives for edge in (drive.start_us, drive.end_us)
                                       if start < edge < end})
        total = identity
        for low, high in zip(edges, edges[1:]):
            middle = 0.5 * (low + high)
            active = [drive for drive in drives if drive.start_us <= middle < drive.end_us]
            total = self._piece(base, low, high, active, step_angle_rad) @ total
        return total

    def _piece(self, base: np.ndarray, low: float, high: float, active: Sequence[ClusterDrive],
               step_angle: float) -> np.ndarray:
        if not active:
            return dense_matrix_exponential(base * (high - low))
        # Work in the frame that turns every transmon at the first drive's frequency: a drive at that
        # frequency then has no carrier left, only its envelope.
        offsets = np.array(self.offsets_rad_per_us)
        carrier = [offsets[drive.line] + drive.frequency_offset_rad_per_us for drive in active]
        shift = self._occupation @ (carrier[0] - offsets)                    # K = diag(shift)
        static = base - _commutator(np.diag(shift).astype(complex))

        def generator(time_us: float) -> np.ndarray:
            result = static.copy()
            for drive, frequency in zip(active, carrier):
                rate = drive.rabi(time_us) * np.exp(1j * (drive.phase_rad - (frequency - carrier[0]) * time_us))
                lower, raise_ = self._line_super[drive.line]
                result += 0.5 * (np.conj(rate) * lower + rate * raise_)
            return result

        length = high - low
        if all(drive.constant for drive in active) and all(frequency == carrier[0] for frequency in carrier):
            inner = dense_matrix_exponential(generator(0.5 * (low + high)) * length)
        else:
            # The fastest thing a slice must resolve: a transition's detuning from the frame, a second
            # drive's carrier, or the envelope itself.
            energies = self.frame_energies - shift
            detuning = max((abs(energies[row] - energies[column])
                            for drive in active
                            for row, column in zip(*np.nonzero(
                                np.abs(self.line_operators[drive.line]) > _ELEMENT_FLOOR))), default=0.0)
            envelope = max((0.0 if drive.constant else 8.0 / drive.duration_us) for drive in active)
            beat = max(abs(frequency - carrier[0]) for frequency in carrier)
            slices = max(1, math.ceil(length * (detuning + beat + envelope) / step_angle))
            step = length / slices
            inner = np.eye(self.dimension ** 2, dtype=complex)
            for index in range(slices):
                begin = low + index * step
                first = generator(begin + (0.5 - _NODE) * step)
                second = generator(begin + (0.5 + _NODE) * step)
                inner = (dense_matrix_exponential(step * (_WEIGHT_FAR * first + _WEIGHT_NEAR * second))
                         @ dense_matrix_exponential(step * (_WEIGHT_NEAR * first + _WEIGHT_FAR * second))
                         @ inner)
        # rho_frame = exp(i K t) rho exp(-i K t): leave that frame at ``high``, enter it at ``low``.
        return _turn(-shift * high)[:, None] * inner * _turn(shift * low)[None, :]


def exchange_from_zz(transmons: Sequence[TransmonSpec], zz_khz: float) -> float:
    """The exchange coupling ``J / 2 pi`` in MHz of a pair whose measured residual ZZ is ``zz_khz``.

    ZZ is what devices usually report; ``J`` is what the model needs. The sign of ZZ is fixed by the two
    frequencies and anharmonicities, so a measured value of the other sign cannot come from this model.
    """
    transmons = tuple(transmons)
    if len(transmons) != 2:
        raise ValueError("the inversion is defined for one pair")
    target = _TWO_PI * 1e-3 * float(zz_khz)
    if not math.isfinite(target):
        raise ValueError("zz_khz must be finite")
    if target == 0.0:
        return 0.0

    def zz(strength_mhz: float) -> float:
        coupling = TransmonExchangeCoupling(0, 1, _TWO_PI * strength_mhz)
        return TransmonCluster(transmons, (coupling,)).zz_rad_per_us(0, 1)

    low, high = 0.0, 1e-3
    while True:
        try:
            value = zz(high)
        except TransmonCollisionError as error:
            raise ValueError("no exchange coupling below the collision limit gives this ZZ") from error
        if value != 0.0 and (value > 0.0) != (target > 0.0):
            raise ValueError("the measured ZZ has the opposite sign to what these transmons can produce")
        if abs(value) >= abs(target):
            break
        low, high = high, 2.0 * high
    for _ in range(80):
        middle = 0.5 * (low + high)
        if abs(zz(middle)) < abs(target):
            low = middle
        else:
            high = middle
    return 0.5 * (low + high)


# ----- helpers ---------------------------------------------------------------------------------------------


def _lowering(levels: int) -> np.ndarray:
    return np.diag(np.sqrt(np.arange(1, levels, dtype=float)), 1).astype(complex)


def _embed(local: np.ndarray, index: int, levels: Sequence[int]) -> np.ndarray:
    result = np.eye(1, dtype=complex)
    for position, level in enumerate(levels):
        result = np.kron(result, local if position == index else np.eye(level, dtype=complex))
    return result


def _commutator(operator: np.ndarray) -> np.ndarray:
    """``-i [operator, .]`` in column-major vectorization."""
    identity = np.eye(len(operator), dtype=complex)
    return -1j * (np.kron(identity, operator) - np.kron(operator.T, identity))


def _dissipator(collapse: Sequence[np.ndarray], dimension: int) -> np.ndarray:
    identity = np.eye(dimension, dtype=complex)
    result = np.zeros((dimension ** 2, dimension ** 2), dtype=complex)
    for operator in collapse:
        square = operator.conj().T @ operator
        result += np.kron(operator.conj(), operator) - 0.5 * (np.kron(identity, square) + np.kron(square.T, identity))
    return result


def _turn(angles: np.ndarray) -> np.ndarray:
    """Diagonal of the superoperator of ``rho -> exp(i angles) rho exp(-i angles)`` (column-major)."""
    phase = np.exp(1j * angles)
    return np.kron(phase.conj(), phase)


def _local_collapse(transmon: TransmonSpec, spec: IdleQubitSpec) -> list[np.ndarray]:
    """One transmon's Lindblad operators from its measured T1, T_phi and |1> population."""
    return ladder_relaxation_operators(replace(
        spec, levels=transmon.levels, frequency_ghz=transmon.frequency_ghz,
        anharmonicity_mhz=transmon.anharmonicity_mhz))

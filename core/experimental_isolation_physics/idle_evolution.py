"""Opt-in, constant-rate independent qubit idle channels on a joint state.

No clock is read here: elapsed_us must describe a declared uninterrupted
physical interval, not time spent computing on the host. This model contains
no Hamiltonian, drive, ZZ, drift, scheduling or state commit.

A subsystem may have more than two levels (D91). Its relaxation is then that of a weakly anharmonic
oscillator, fixed by the same measured numbers: level ``k`` decays to ``k - 1`` at ``k`` times the 1 -> 0
rate (the ladder matrix element), into a bath at the temperature the |1> population implies, and dephasing
is frequency noise, ``sqrt(2 / T_phi) n``. With two levels this is the usual T1 and T_phi channel.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
import math
from numbers import Real

import numpy as np

MIN_LEVELS, MAX_LEVELS = 2, 4


@dataclass(frozen=True)
class IdleQubitSpec:
    """Explicit microsecond time constants and equilibrium |1> population.

    Positive infinity disables the corresponding process. There are no
    device/temperature defaults; equilibrium population is not inferred.

    ``levels`` above two needs the transition frequencies only where they matter: with a nonzero |1>
    population the excitation of the higher levels follows from the temperature, which needs the 0-1
    frequency and the anharmonicity (``f12 - f01``).
    """

    t1_us: float
    tphi_us: float
    equilibrium_excited_population: float
    levels: int = 2
    frequency_ghz: float | None = None
    anharmonicity_mhz: float | None = None

    def __post_init__(self) -> None:
        for name in ("t1_us", "tphi_us"):
            value = _real(getattr(self, name), name)
            if math.isnan(value) or value <= 0.0:
                raise ValueError(f"{name} must be positive or positive infinity")
            object.__setattr__(self, name, value)
        population = _real(self.equilibrium_excited_population, "equilibrium_excited_population")
        if not math.isfinite(population) or not 0.0 <= population <= 1.0:
            raise ValueError("equilibrium_excited_population must be in [0, 1]")
        object.__setattr__(self, "equilibrium_excited_population", population)
        if type(self.levels) is not int or not MIN_LEVELS <= self.levels <= MAX_LEVELS:
            raise ValueError(f"levels must be an integer from {MIN_LEVELS} to {MAX_LEVELS}")
        for name in ("frequency_ghz", "anharmonicity_mhz"):
            value = getattr(self, name)
            if value is not None:
                value = _real(value, name)
                if not math.isfinite(value):
                    raise ValueError(f"{name} must be finite")
                object.__setattr__(self, name, value)
        if self.levels > 2 and population > 0.0 and math.isfinite(self.t1_us):
            if population >= 0.5:
                raise ValueError("levels above |1> need a thermal |1> population (below one half)")
            if (self.frequency_ghz is None or self.anharmonicity_mhz is None or self.frequency_ghz <= 0.0
                    or self.frequency_ghz + 1e-3 * (self.levels - 2) * self.anharmonicity_mhz <= 0.0):
                raise ValueError("thermal excitation above |1> needs the 0-1 frequency and the anharmonicity")


def ladder_relaxation_operators(spec: IdleQubitSpec) -> list[np.ndarray]:
    """The Lindblad operators of one subsystem's idle relaxation, on its ``spec.levels`` levels."""
    levels = spec.levels
    operators = []
    if math.isfinite(spec.t1_us):
        excited = spec.equilibrium_excited_population
        down, up = (1.0 - excited) / spec.t1_us, excited / spec.t1_us
        rates = [(down, up)]
        if levels > 2:
            if excited >= 0.5:
                raise ValueError("levels above |1> need a thermal |1> population (below one half)")
            cold = down - up                                 # the zero-temperature 1 -> 0 rate
            boltzmann = up / down                            # exp(-h f01 / k T)
            for level in range(2, levels):
                factor = 0.0
                if boltzmann > 0.0:
                    ratio = (spec.frequency_ghz + 1e-3 * (level - 1) * spec.anharmonicity_mhz) / spec.frequency_ghz
                    factor = boltzmann ** ratio
                thermal = factor / (1.0 - factor)
                rates.append((level * cold * (thermal + 1.0), level * cold * thermal))
        for level, (falling, rising) in enumerate(rates, start=1):
            step = np.zeros((levels, levels), dtype=complex)
            step[level - 1, level] = 1.0
            if falling > 0.0:
                operators.append(math.sqrt(falling) * step)
            if rising > 0.0:
                operators.append(math.sqrt(rising) * step.T)
    if math.isfinite(spec.tphi_us):
        operators.append(math.sqrt(2.0 / spec.tphi_us) * np.diag(np.arange(levels, dtype=float)).astype(complex))
    return operators


@lru_cache(maxsize=512)
def _ladder_channel(spec: IdleQubitSpec, elapsed_us: float) -> np.ndarray:
    """``exp(L t)`` of the subsystem's relaxation as ``[row, column, old row, old column]``."""
    from scipy.linalg import expm
    levels = spec.levels
    identity = np.eye(levels, dtype=complex)
    generator = np.zeros((levels ** 2, levels ** 2), dtype=complex)        # column-major vectorization
    for operator in ladder_relaxation_operators(spec):
        square = operator.conj().T @ operator
        generator += np.kron(operator.conj(), operator) - 0.5 * (np.kron(identity, square) + np.kron(square.T, identity))
    channel = expm(generator * elapsed_us).reshape(levels, levels, levels, levels)   # [col, row, old col, old row]
    channel = np.ascontiguousarray(channel.transpose(1, 0, 3, 2))
    channel.setflags(write=False)
    return channel


@dataclass(frozen=True)
class IndependentIdleModel:
    """A caller-bounded joint-register kernel, not a scenario default.

    Specs follow MSB-first local register axes, not physical topology IDs.
    The caller must supply a previously validated PSD density matrix and
    retain authority over its physical interval and transactional commit.
    Shape, finite values, Hermiticity and unit trace are checked here, but no
    cubic eigensolve is repeated. No input or authoritative state is mutated.
    """

    qubits: tuple[IdleQubitSpec, ...]
    max_local_qubits: int

    def __post_init__(self) -> None:
        if type(self.max_local_qubits) is not int or self.max_local_qubits < 1:
            raise ValueError("max_local_qubits must be an explicit positive integer")
        if (type(self.qubits) is not tuple or not self.qubits
                or len(self.qubits) > self.max_local_qubits
                or any(type(spec) is not IdleQubitSpec for spec in self.qubits)):
            raise ValueError("qubits must be a bounded non-empty tuple of IdleQubitSpec")

    @property
    def dims(self) -> tuple[int, ...]:
        """The level count of every subsystem, in register order."""
        return tuple(spec.levels for spec in self.qubits)

    def evolve(self, state: np.ndarray, *, elapsed_us: float) -> np.ndarray:
        """Apply local CPTP semigroups once, with O(n*4**n) time / O(4**n) space.

        Only use this model when independent constant-rate dissipative idle
        evolution is the declared complete dynamics for the interval. Split
        at intervening events; do not silently ignore required couplings.
        The returned complex128 array is independently owned even at dt=0.
        """
        elapsed = _real(elapsed_us, "elapsed_us")
        if not math.isfinite(elapsed) or elapsed < 0.0:
            raise ValueError("elapsed_us must be finite and non-negative")
        dimension = math.prod(self.dims)
        # Reject unbounded/mismatched input before copying or tensor reshaping.
        if (not isinstance(state, np.ndarray) or state.shape != (dimension, dimension)
                or state.dtype.kind not in "iufc"):
            raise ValueError("state must be a numeric array matching the declared qubit register")
        if not np.all(np.isfinite(state)):
            raise ValueError("state must contain finite values")
        if (np.max(np.abs(state - state.conj().T)) > 1e-10
                or abs(np.trace(state) - 1.0) > 1e-10):
            raise ValueError("state must be Hermitian with unit trace")
        result = np.array(state, dtype=np.complex128, copy=True, order="C")
        if elapsed == 0.0:
            return result
        relax_axes_in_place(result, tuple((axis, spec, elapsed) for axis, spec in enumerate(self.qubits)),
                            self.dims)
        return result


def relax_axes_in_place(matrix: np.ndarray, items, dims: Sequence[int] | None = None) -> None:
    """Apply each ``(axis, IdleQubitSpec, elapsed_us)`` relaxation to ``matrix`` in place.

    The same local semigroup as :meth:`IndependentIdleModel.evolve`, with its own elapsed time per axis
    and no density-matrix validation: it is linear, so callers may pass any square operator on the
    register (for example one term of a sum of products). The caller owns the validity of the result.
    A three-dimensional array is a stack of such operators, each relaxed alike. ``dims`` gives the level
    count of every subsystem (None: two-level qubits); each spec must have the levels of its axis.
    """
    lead = matrix.shape[:-2]
    if dims is None:
        dims = (2,) * (matrix.shape[-1].bit_length() - 1)
    dims = tuple(dims)
    n = len(dims)
    if math.prod(dims) != matrix.shape[-1]:
        raise ValueError("the declared levels do not match the register")
    tensor = matrix.reshape(lead + dims + dims)
    base = len(lead)
    for axis, spec, elapsed in items:
        if spec.levels != dims[axis]:
            raise ValueError("an idle spec must have the levels of its subsystem")
        if elapsed == 0.0 or (math.isinf(spec.t1_us) and math.isinf(spec.tphi_us)):
            continue
        if spec.levels > 2:
            blocks = np.moveaxis(tensor, (base + axis, base + n + axis), (0, 1))
            blocks[...] = np.einsum("rcab,ab...->rc...", _ladder_channel(spec, float(elapsed)), blocks)
            continue
        relaxation = elapsed / spec.t1_us
        damping = -math.expm1(-relaxation)
        coherence = math.exp(-0.5 * relaxation - elapsed / spec.tphi_us)
        population = spec.equilibrium_excited_population
        blocks = np.moveaxis(tensor, (base + axis, base + n + axis), (0, 1))
        old_zero = blocks[0, 0].copy()
        old_one = blocks[1, 1].copy()
        blocks[0, 0] = ((1.0 - population * damping) * old_zero
                        + (1.0 - population) * damping * old_one)
        blocks[1, 1] = (population * damping * old_zero
                        + (1.0 - (1.0 - population) * damping) * old_one)
        blocks[0, 1] *= coherence
        blocks[1, 0] *= coherence


def _real(value: float, name: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a real number")
    try:
        return float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} cannot be represented as a float") from error

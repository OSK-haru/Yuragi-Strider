"""Opt-in, constant-rate independent qubit idle channels on a joint state.

No clock is read here: elapsed_us must describe a declared uninterrupted
physical interval, not time spent computing on the host. This model contains
no Hamiltonian, drive, ZZ, drift, leakage levels, scheduling or state commit.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real

import numpy as np


@dataclass(frozen=True)
class IdleQubitSpec:
    """Explicit microsecond time constants and equilibrium |1> population.

    Positive infinity disables the corresponding process. There are no
    device/temperature defaults; equilibrium population is not inferred.
    """

    t1_us: float
    tphi_us: float
    equilibrium_excited_population: float

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
        dimension = 1 << len(self.qubits)
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
        n = len(self.qubits)
        tensor = result.reshape((2,) * (2 * n))
        for axis, spec in enumerate(self.qubits):
            if math.isinf(spec.t1_us) and math.isinf(spec.tphi_us):
                continue
            relaxation = elapsed / spec.t1_us
            damping = -math.expm1(-relaxation)
            coherence = math.exp(-0.5 * relaxation - elapsed / spec.tphi_us)
            population = spec.equilibrium_excited_population
            blocks = np.moveaxis(tensor, (axis, n + axis), (0, 1))
            old_zero = blocks[0, 0].copy()
            old_one = blocks[1, 1].copy()
            blocks[0, 0] = ((1.0 - population * damping) * old_zero
                            + (1.0 - population) * damping * old_one)
            blocks[1, 1] = (population * damping * old_zero
                            + (1.0 - (1.0 - population) * damping) * old_one)
            blocks[0, 1] *= coherence
            blocks[1, 0] *= coherence
        return result


def _real(value: float, name: str) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a real number")
    try:
        return float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{name} cannot be represented as a float") from error

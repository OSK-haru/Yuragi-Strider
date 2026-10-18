"""Native-gate steps of a calibrated single-qubit gate, with the virtual-Z angles worked out.

The service compiles a gate such as H into the hardware's native steps (``rz``, ``sx``, ``x``) and
charges their calibrated durations, but the compiled instruction only keeps the gate's matrix. A
drive that leaks onto a neighbour is set by the microwave pulses, so this module recovers the steps:
for every ``sx`` or ``x`` pulse, its duration and the virtual-Z angles that fix its carrier phase.

Conventions (time order is the order of ``sequence``):

* ``rz(a) = diag(exp(-ia/2), exp(ia/2))`` is a software frame shift and takes no time.
* ``sx`` and ``x`` are pulses, rotations about the x axis by pi/2 and pi.
* The compiled matrix is the product of the steps, later steps on the left, up to a global phase.
* A virtual Z of ``a`` shifts the carrier phase of the pulses after it by ``-a``.

Only the sequences the product service declares are solved: ``rz`` only, ``x``, ``x rz``,
``rz sx rz`` and ``rz sx rz sx rz``. Any other sequence raises ``UnsupportedNativeSequence`` so the
caller can leave the gate without pulse information instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
import cmath
import math
from collections.abc import Sequence

import numpy as np

PULSE_ANGLES = {"sx": math.pi / 2.0, "x": math.pi}
_TOLERANCE = 1e-9


class UnsupportedNativeSequence(ValueError):
    """The native sequence cannot be solved from the matrix, so no pulse data is kept."""


@dataclass(frozen=True)
class NativeStep:
    """One native step: ``rz`` carries an angle and no time; ``sx``/``x`` carry a duration."""

    name: str
    angle_rad: float
    duration_us: float

    def __post_init__(self) -> None:
        if self.name not in {"rz", "sx", "x"}:
            raise ValueError("unknown native step")
        if not (math.isfinite(self.angle_rad) and math.isfinite(self.duration_us)) or self.duration_us < 0.0:
            raise ValueError("native step values must be finite and the duration nonnegative")


def _rz(angle: float) -> np.ndarray:
    return np.array([[cmath.exp(-0.5j * angle), 0.0], [0.0, cmath.exp(0.5j * angle)]], dtype=complex)


_X = np.array([[0.0, 1.0], [1.0, 0.0]], dtype=complex)
_SX = 0.5 * np.array([[1 + 1j, 1 - 1j], [1 - 1j, 1 + 1j]], dtype=complex)


def _step_matrix(step: NativeStep) -> np.ndarray:
    if step.name == "rz":
        return _rz(step.angle_rad)
    return _SX if step.name == "sx" else _X


def product_of(steps: Sequence[NativeStep]) -> np.ndarray:
    """The unitary of the steps in time order."""
    matrix = np.eye(2, dtype=complex)
    for step in steps:
        matrix = _step_matrix(step) @ matrix
    return matrix


def _same_up_to_phase(left: np.ndarray, right: np.ndarray) -> bool:
    return abs(abs(np.trace(left.conj().T @ right)) / 2.0 - 1.0) <= _TOLERANCE


def _u3_angles(unitary: np.ndarray) -> tuple[float, float, float]:
    """(theta, phi, lambda) with unitary = exp(i g) * [[c, -e^{il} s], [e^{ip} s, e^{i(p+l)} c]]."""
    a, b = unitary[0, 0], unitary[0, 1]
    c, d = unitary[1, 0], unitary[1, 1]
    theta = 2.0 * math.atan2(abs(c), abs(a))
    if abs(c) < _TOLERANCE or abs(a) < _TOLERANCE:
        # Diagonal or anti-diagonal: only a combination of phi and lambda is fixed.
        if abs(c) < _TOLERANCE:
            return 0.0, 0.0, cmath.phase(d) - cmath.phase(a)
        return math.pi, cmath.phase(c) - cmath.phase(-b), 0.0
    phi = cmath.phase(c) - cmath.phase(a)
    lam = cmath.phase(-b) - cmath.phase(a)
    return theta, phi, lam


def solve_native_steps(unitary: Sequence[Sequence[complex]], sequence: Sequence[str],
                       durations_us: dict[str, float]) -> tuple[NativeStep, ...]:
    """The steps of ``sequence`` with the ``rz`` angles that reproduce ``unitary`` up to a phase."""
    matrix = np.array(unitary, dtype=complex)
    if matrix.shape != (2, 2):
        raise UnsupportedNativeSequence("only single-qubit gates have native steps")
    names = tuple(sequence)
    if any(name not in {"rz", "sx", "x"} for name in names):
        raise UnsupportedNativeSequence("unknown native step name")

    if all(name == "rz" for name in names):
        total = cmath.phase(matrix[1, 1]) - cmath.phase(matrix[0, 0])
        angles = [total] + [0.0] * (len(names) - 1)
    elif names == ("x",):
        angles = []
    elif names == ("x", "rz"):
        angles = [cmath.phase(matrix[1, 0]) - cmath.phase(matrix[0, 1])]
    elif names == ("rz", "sx", "rz"):
        theta, phi, lam = _u3_angles(matrix)
        if abs(theta - math.pi / 2.0) > 1e-7:
            raise UnsupportedNativeSequence("a single sx covers only half-pi rotations")
        angles = [lam - math.pi / 2.0, phi + math.pi / 2.0]
    elif names == ("rz", "sx", "rz", "sx", "rz"):
        theta, phi, lam = _u3_angles(matrix)
        angles = [lam, theta + math.pi, phi + math.pi]
    else:
        raise UnsupportedNativeSequence("unsupported native sequence")

    steps: list[NativeStep] = []
    rz_angles = iter(angles)
    for name in names:
        if name == "rz":
            steps.append(NativeStep("rz", next(rz_angles), 0.0))
        else:
            steps.append(NativeStep(name, PULSE_ANGLES[name], float(durations_us[name])))
    if not _same_up_to_phase(product_of(steps), matrix):
        raise UnsupportedNativeSequence("the native steps do not reproduce the gate")
    return tuple(steps)

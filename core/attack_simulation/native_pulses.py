"""Native-gate steps of a calibrated single-qubit gate, with virtual-frame angles worked out.

The service compiles a gate such as H into the hardware's declared native steps and charges their
calibrated durations, but the compiled instruction also keeps the gate's matrix. A drive that leaks
onto a neighbour is set by the microwave pulses, so this module recovers the pulse angles and frame
rotations from the backend declaration.

Conventions (time order is the order of ``sequence``):

* A declared virtual gate is a software Z-frame shift and takes no time.
* A declared native pulse is a rotation about x by its configured angle.
* The compiled matrix is the product of the steps, later steps on the left, up to a global phase.
* A virtual Z of ``a`` shifts the carrier phase of the pulses after it by ``-a``.

The solver handles virtual-only, pi-pulse, and one- or two-half-turn-pulse Euler sequences. Other
sequences raise ``UnsupportedNativeSequence``; calibrated product profiles reject them before they
can be submitted, so pulse information is never silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
import cmath
import math
from collections.abc import Sequence
from collections.abc import Mapping

import numpy as np

DEFAULT_PULSE_ANGLES = {"sx": math.pi / 2.0, "x": math.pi}
PULSE_ANGLES = DEFAULT_PULSE_ANGLES  # Compatibility export for callers using the Kingston default.
_TOLERANCE = 1e-9


class UnsupportedNativeSequence(ValueError):
    """The native sequence cannot be solved from the matrix, so no pulse data is kept."""


@dataclass(frozen=True)
class NativeStep:
    """One native step, marked virtual or pulse-driven by its backend declaration."""

    name: str
    angle_rad: float
    duration_us: float
    is_virtual: bool = False

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name or type(self.is_virtual) is not bool:
            raise ValueError("unknown native step")
        if not (math.isfinite(self.angle_rad) and math.isfinite(self.duration_us)) or self.duration_us < 0.0:
            raise ValueError("native step values must be finite and the duration nonnegative")


def _virtual_z_matrix(angle: float) -> np.ndarray:
    return np.array([[cmath.exp(-0.5j * angle), 0.0], [0.0, cmath.exp(0.5j * angle)]], dtype=complex)


_X = np.array([[0.0, 1.0], [1.0, 0.0]], dtype=complex)
def _step_matrix(step: NativeStep) -> np.ndarray:
    if step.is_virtual:
        return _virtual_z_matrix(step.angle_rad)
    c, s = math.cos(step.angle_rad / 2.0), math.sin(step.angle_rad / 2.0)
    return np.array([[c, -1j * s], [-1j * s, c]], dtype=complex)


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
                       durations_us: Mapping[str, float], *,
                       virtual_gates: Sequence[str] = ("rz",),
                       pulse_angles: Mapping[str, float] = DEFAULT_PULSE_ANGLES) -> tuple[NativeStep, ...]:
    """Solve a supported pulse sequence using its declared virtual and pulse gates."""
    matrix = np.array(unitary, dtype=complex)
    if matrix.shape != (2, 2):
        raise UnsupportedNativeSequence("only single-qubit gates have native steps")
    names = tuple(sequence)
    virtual = frozenset(virtual_gates)
    if any(name not in virtual and name not in pulse_angles for name in names):
        raise UnsupportedNativeSequence("native step is absent from backend declaration")
    pulse_names = tuple(name for name in names if name not in virtual)

    if not pulse_names:
        total = cmath.phase(matrix[1, 1]) - cmath.phase(matrix[0, 0])
        angles = [total] + [0.0] * (len(names) - 1)
    elif pulse_names == (next((name for name in pulse_names if abs(pulse_angles[name] - math.pi) < 1e-7), ""),) and len(names) == 1:
        angles = []
    elif len(pulse_names) == 1 and abs(pulse_angles[pulse_names[0]] - math.pi) < 1e-7 and len(names) == 2:
        angles = [cmath.phase(matrix[1, 0]) - cmath.phase(matrix[0, 1])]
    elif (len(pulse_names) == 1 and abs(pulse_angles[pulse_names[0]] - math.pi / 2.0) < 1e-7
          and len(names) == 3 and names[0] in virtual and names[2] in virtual):
        theta, phi, lam = _u3_angles(matrix)
        if abs(theta - math.pi / 2.0) > 1e-7:
            raise UnsupportedNativeSequence("a single pi/2 native pulse covers only half-pi rotations")
        angles = [lam - math.pi / 2.0, phi + math.pi / 2.0]
    elif (len(pulse_names) == 2 and all(abs(pulse_angles[name] - math.pi / 2.0) < 1e-7
                                         for name in pulse_names)
          and len(names) == 5 and names[0] in virtual and names[2] in virtual and names[4] in virtual):
        theta, phi, lam = _u3_angles(matrix)
        angles = [lam, theta + math.pi, phi + math.pi]
    else:
        raise UnsupportedNativeSequence("unsupported native sequence")

    steps: list[NativeStep] = []
    virtual_angles = iter(angles)
    for name in names:
        if name in virtual:
            steps.append(NativeStep(name, next(virtual_angles), 0.0, True))
        else:
            steps.append(NativeStep(name, float(pulse_angles[name]), float(durations_us[name])))
    if not _same_up_to_phase(product_of(steps), matrix):
        raise UnsupportedNativeSequence("the native steps do not reproduce the gate")
    return tuple(steps)

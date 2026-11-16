"""A pulse a tenant plays directly on its own drive line (D91).

A calibrated gate hides the pulse: the tenant names a gate and the service plays its own envelope at the
qubit's frequency. A direct pulse is the tenant's own: an area, a duration, a phase, an envelope and a
carrier that may sit away from the qubit's frequency. Whether a service accepts such pulses, and how far
the carrier may move, is the provider's policy rather than physics, so it is a declared input
(``DirectPulsePolicy``); without one the instruction does not exist.

Conventions. The line carries ``Omega(t) cos((w_q + offset)(t - t_start) + w_q t_start - phase)``: the
carrier's phase is counted from the start of the pulse, so what the pulse does to its own qubit does not
depend on when it is played. ``phase_rad`` is in the program's frame, like the axis of a gate: earlier
virtual Z rotations turn it with every other pulse of the qubit. ``angle_rad`` is the area of
``Omega``, so a resonant pulse turns the qubit by ``angle_rad`` about the axis at ``phase_rad``.

What the pulse does to its own qubit is computed here for a two-level qubit, which is all the runtime's
state holds so far; the multi-level reference is ``TransmonCluster`` and the two are tested against
each other. In the frame of the carrier the qubit sees ``1/2 Omega(t) sigma_x + offset/2 sigma_z``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
import cmath
import math

import numpy as np

from core.attack_simulation.native_pulses import NativeStep
from core.attack_simulation.pulse_shape import PulseShape, SQUARE_SHAPE

_TWO_PI = 2.0 * math.pi
PULSE_PARAMS = frozenset({"angle_rad", "duration_us", "phase_rad", "frequency_offset_mhz",
                          "gaussian_truncation_sigma"})
STEP_ANGLE_RAD = 0.05               # a slice turns the fastest term by about this much (fourth order)
MAX_STEPS = 400_000
# Fourth-order commutator-free Magnus step: two exponentials of the generator at the Gauss nodes.
_NODE = math.sqrt(3.0) / 6.0
_WEIGHT_NEAR, _WEIGHT_FAR = 0.25 + _NODE, 0.25 - _NODE
_PAULI = np.array([[[0, 1], [1, 0]], [[0, -1j], [1j, 0]], [[1, 0], [0, -1]]], dtype=complex)


@dataclass(frozen=True)
class DirectPulse:
    """One direct pulse, as the program states it."""

    angle_rad: float
    duration_us: float
    phase_rad: float = 0.0
    frequency_offset_rad_per_us: float = 0.0
    shape: PulseShape = SQUARE_SHAPE

    def __post_init__(self) -> None:
        for name in ("angle_rad", "duration_us", "phase_rad", "frequency_offset_rad_per_us"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, float(value))
        if self.duration_us <= 0.0:
            raise ValueError("a pulse needs a positive duration")
        if type(self.shape) is not PulseShape:
            raise TypeError("a PulseShape is required")

    @classmethod
    def from_params(cls, params: Mapping[str, float] | None) -> DirectPulse:
        """Read the instruction's parameters: ``angle_rad`` and ``duration_us`` are required.

        ``gaussian_truncation_sigma`` selects the truncated Gaussian envelope cut at that many standard
        deviations on each side; without it the envelope is square.
        """
        params = dict(params or {})
        if not set(params) <= PULSE_PARAMS or not {"angle_rad", "duration_us"} <= set(params):
            raise ValueError("a pulse takes angle_rad and duration_us, and optionally phase_rad, "
                             "frequency_offset_mhz and gaussian_truncation_sigma")
        shape = (PulseShape("gaussian", float(params["gaussian_truncation_sigma"]))
                 if "gaussian_truncation_sigma" in params else SQUARE_SHAPE)
        return cls(params["angle_rad"], params["duration_us"], params.get("phase_rad", 0.0),
                   _TWO_PI * params.get("frequency_offset_mhz", 0.0), shape)

    @property
    def peak_rabi_rad_per_us(self) -> float:
        return abs(self.angle_rad) / self.duration_us * float(self.shape.density(0.5))

    def unitary(self) -> np.ndarray:
        """What the pulse does to its own two-level qubit, in the program's frame."""
        return pulse_unitaries(self.angle_rad, self.duration_us, self.frequency_offset_rad_per_us, self.shape,
                               (1.0,), self.phase_rad)[0]

    def native_steps(self) -> tuple[NativeStep, ...]:
        """The pulse as native steps: its phase is a frame shift before and the opposite shift after."""
        played = NativeStep("pulse", self.angle_rad, self.duration_us,
                            frequency_offset_rad_per_us=self.frequency_offset_rad_per_us, shape=self.shape)
        if self.phase_rad == 0.0:
            return (played,)
        return (NativeStep("frame", -self.phase_rad, 0.0, True), played,
                NativeStep("frame", self.phase_rad, 0.0, True))


@dataclass(frozen=True)
class DirectPulsePolicy:
    """What the provider lets a tenant play on its own drive line. Every limit is the provider's choice.

    ``max_frequency_offset_mhz`` is how far the carrier may sit from the qubit's frequency (0: on the
    qubit only). A window that reaches a neighbour's frequency lets the tenant drive that neighbour through
    any leak; one that reaches the qubit's own anharmonicity lets it address the 1-2 transition.
    ``max_rabi_mhz`` caps the peak drive strength ``Omega / 2 pi`` and ``max_duration_us`` the length.
    """

    max_frequency_offset_mhz: float
    max_rabi_mhz: float
    max_duration_us: float

    def __post_init__(self) -> None:
        for name in ("max_frequency_offset_mhz", "max_rabi_mhz", "max_duration_us"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and nonnegative")
            object.__setattr__(self, name, float(value))
        if self.max_rabi_mhz == 0.0 or self.max_duration_us == 0.0:
            raise ValueError("a policy that allows no pulse is declared by leaving it out")

    def check(self, pulse: DirectPulse) -> None:
        """Raise ValueError naming the limit the pulse exceeds."""
        tolerance = 1.0 + 1e-12
        if abs(pulse.frequency_offset_rad_per_us) > _TWO_PI * self.max_frequency_offset_mhz * tolerance:
            raise ValueError(f"the pulse's carrier is more than {self.max_frequency_offset_mhz:g} MHz "
                             "from the qubit's frequency")
        if pulse.duration_us > self.max_duration_us * tolerance:
            raise ValueError(f"the pulse is longer than {self.max_duration_us:g} us")
        if pulse.peak_rabi_rad_per_us > _TWO_PI * self.max_rabi_mhz * tolerance:
            raise ValueError(f"the pulse's peak drive strength exceeds {self.max_rabi_mhz:g} MHz")

    def to_config(self) -> dict[str, float]:
        return {"max_frequency_offset_mhz": self.max_frequency_offset_mhz, "max_rabi_mhz": self.max_rabi_mhz,
                "max_duration_us": self.max_duration_us}

    @classmethod
    def from_config(cls, value: object) -> DirectPulsePolicy:
        names = {"max_frequency_offset_mhz", "max_rabi_mhz", "max_duration_us"}
        if not isinstance(value, Mapping) or set(value) != names:
            raise ValueError("a direct-pulse policy declares max_frequency_offset_mhz, max_rabi_mhz and "
                             "max_duration_us")
        return cls(value["max_frequency_offset_mhz"], value["max_rabi_mhz"], value["max_duration_us"])


def _exponential(x: np.ndarray, z: float) -> tuple[np.ndarray, np.ndarray]:
    """``exp(-i (x sigma_x + z sigma_z))`` as ``(a, b)`` of ``[[a, -conj(b)], [b, conj(a)]]``."""
    theta = np.hypot(x, z)
    sinc = np.sinc(theta / math.pi)                          # sin(theta) / theta, 1 at theta = 0
    return np.cos(theta) - 1j * z * sinc, -1j * x * sinc


def _compose(later: tuple[complex, complex], earlier: tuple[complex, complex]) -> tuple[complex, complex]:
    (a2, b2), (a1, b1) = later, earlier
    return a2 * a1 - b2.conjugate() * b1, b2 * a1 + a2.conjugate() * b1


def _step(shape: PulseShape, scale: float, offset: float, begin: np.ndarray, width: np.ndarray,
          duration: float) -> tuple[np.ndarray, np.ndarray]:
    """One Magnus step from ``begin`` over ``width`` (times in the pulse), elementwise, as ``(a, b)``."""
    first = 0.5 * scale * shape.density((begin + (0.5 - _NODE) * width) / duration)
    second = 0.5 * scale * shape.density((begin + (0.5 + _NODE) * width) / duration)
    # The two weights of each exponential add to one half, so the constant term enters with half the width.
    constant = 0.25 * width * offset
    a1, b1 = _exponential(width * (_WEIGHT_NEAR * first + _WEIGHT_FAR * second), constant)
    a2, b2 = _exponential(width * (_WEIGHT_FAR * first + _WEIGHT_NEAR * second), constant)
    return a2 * a1 - np.conj(b2) * b1, b2 * a1 + np.conj(a2) * b1


@lru_cache(maxsize=64)
def _grid(angle: float, duration: float, offset: float, shape: PulseShape) -> tuple[float, tuple]:
    """The carrier-frame evolution at the ends of equal slices of the pulse: the slice width and ``(a, b)``."""
    scale = angle / duration
    peak = abs(scale) * float(shape.density(0.5))
    slices = math.ceil(duration * (abs(offset) + peak + 8.0 / duration) / STEP_ANGLE_RAD)
    if slices > MAX_STEPS:
        raise ValueError("the pulse turns too far to be resolved")
    width = duration / slices
    a, b = _step(shape, scale, offset, width * np.arange(slices), np.full(slices, width), duration)
    current = (1.0 + 0.0j, 0.0j)
    points = [current]
    for pair in zip(a.tolist(), b.tolist()):
        current = _compose(pair, current)
        points.append(current)
    return width, tuple(points)


def pulse_unitaries(angle_rad: float, duration_us: float, offset_rad_per_us: float, shape: PulseShape,
                    fractions: Sequence[float], phase_rad: float = 0.0) -> np.ndarray:
    """A two-level qubit's evolution from the start of a pulse to each of ``fractions`` of it, shape (n, 2, 2).

    The frame is the qubit's own (the program's frame). Fractions are clipped to [0, 1]. A square envelope is
    one closed-form exponential per fraction; any other is cut into slices that each turn by
    ``STEP_ANGLE_RAD``.
    """
    u = np.clip(np.asarray(fractions, dtype=float).reshape(-1), 0.0, 1.0)
    elapsed = u * duration_us
    scale = angle_rad / duration_us
    if shape.kind == "square":
        a, b = _exponential(0.5 * scale * elapsed, 0.5 * offset_rad_per_us * elapsed)
    else:
        width, points = _grid(float(angle_rad), float(duration_us), float(offset_rad_per_us), shape)
        index = np.minimum((elapsed / width).astype(int), len(points) - 1)
        begin = index * width
        a_rest, b_rest = _step(shape, scale, offset_rad_per_us, begin, np.maximum(elapsed - begin, 0.0),
                               duration_us)
        a0 = np.array([points[k][0] for k in index])
        b0 = np.array([points[k][1] for k in index])
        a, b = a_rest * a0 - np.conj(b_rest) * b0, b_rest * a0 + np.conj(a_rest) * b0
    # Leave the carrier's frame (it runs ahead of the qubit by offset * elapsed), then turn to the phase.
    lead = np.exp(0.5j * offset_rad_per_us * elapsed)
    a, b = lead * a, np.conj(lead) * b * cmath.exp(1j * phase_rad)
    result = np.empty((len(u), 2, 2), dtype=complex)
    result[:, 0, 0], result[:, 0, 1], result[:, 1, 0], result[:, 1, 1] = a, -np.conj(b), b, np.conj(a)
    return result


def pulse_rotations(angle_rad: float, duration_us: float, offset_rad_per_us: float, shape: PulseShape,
                    fractions: Sequence[float], phase_rad: float = 0.0) -> np.ndarray:
    """The same evolution as rotations of the Bloch vector, shape (n, 3, 3)."""
    unitaries = pulse_unitaries(angle_rad, duration_us, offset_rad_per_us, shape, fractions, phase_rad)
    return 0.5 * np.einsum("iab,nbc,jcd,nad->nij", _PAULI, unitaries, _PAULI, np.conj(unitaries)).real

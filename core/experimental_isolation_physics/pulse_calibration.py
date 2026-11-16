"""Calibrated single-qubit pulses on coupled transmons, and the part of a gate error that is relaxation (D91).

A device calibrates each qubit's native pulses with its neighbours in |0>: the drive sits at the measured
frequency, and the amplitude and the DRAG quadrature are tuned until the pulse is the intended rotation of
the qubit. ``calibrate_pulse`` does the same on the model (``TransmonCluster``): it chooses the amplitude
scale and the DRAG coefficient that maximize the average gate fidelity of the qubit's 0-1 block against the
target rotation about x. What is left (leakage, a phase the two knobs cannot remove) is the model's own
prediction of the coherent error, and it is reported, not hidden.

A calibrated gate error already contains what T1 and T_phi do during the gate. Where the pulse is simulated
with relaxation, adding the calibrated error as well would count that part twice; ``residual_survival`` takes
the relaxation part out (D91, user's choice 5).
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec
from core.experimental_isolation_physics.transmon_cluster import ClusterDrive, TransmonCluster

_TOLERANCE = 1e-15          # stop when the simplex's infidelities agree to this ...
_PARAMETER_TOLERANCE = 1e-10  # ... and its points to this (relative); the optimum is flat, so both are needed
_MAX_ITERATIONS = 600


@dataclass(frozen=True)
class CalibratedPulse:
    amplitude_scale: float      # the drive's area is this times the nominal rotation angle
    drag_us: float              # quadrature drag_us * dOmega/dt (``ClusterDrive.drag_us``)
    infidelity: float           # 1 - average gate fidelity of the qubit block, neighbours in |0>
    leakage: float              # population lost from the qubit levels, averaged over the qubit's states


def qubit_block(cluster: TransmonCluster, unitary: np.ndarray, line: int) -> np.ndarray:
    """The 2x2 block of ``unitary`` on ``line``'s qubit levels with every other transmon in |0>."""
    label = [0] * len(cluster.levels)
    indices = []
    for level in (0, 1):
        label[line] = level
        indices.append(cluster.label_index(label))
    return unitary[np.ix_(indices, indices)]


def block_infidelity(block: np.ndarray, target: np.ndarray) -> float:
    """1 - average gate fidelity of a (possibly leaking) qubit block against a target unitary.

    ``F = (|Tr(T^+ M)|^2 + Tr(M^+ M)) / (d (d + 1))`` with ``d = 2`` (Pedersen, Moller and Molmer 2007),
    which counts leakage out of the block as error.
    """
    overlap = abs(np.trace(target.conj().T @ block)) ** 2
    return float(1.0 - (overlap + np.trace(block.conj().T @ block).real) / 6.0)


def rotation_x(angle_rad: float) -> np.ndarray:
    c, s = math.cos(0.5 * angle_rad), math.sin(0.5 * angle_rad)
    return np.array([[c, -1j * s], [-1j * s, c]], dtype=complex)


def calibrate_pulse(cluster: TransmonCluster, line: int, angle_rad: float, duration_us: float,
                    shape) -> CalibratedPulse:
    """The amplitude scale and DRAG that make the pulse closest to a rotation by ``angle_rad`` about x.

    The pulse runs at ``line``'s measured frequency with every other transmon in |0>, as on a device. A shape
    without a slope (square) has no DRAG to tune. The search is a Nelder-Mead simplex on the two knobs, started
    at the nominal amplitude and the textbook DRAG ``-1 / (2 alpha)``.
    """
    if not math.isfinite(angle_rad) or angle_rad == 0.0 or not math.isfinite(duration_us) or duration_us <= 0.0:
        raise ValueError("a calibrated pulse needs a nonzero angle and a positive duration")
    target = rotation_x(angle_rad)
    spec = cluster.transmons[line]
    tune_drag = getattr(shape, "kind", "square") != "square" and spec.levels > 2

    def block(scale: float, drag: float) -> np.ndarray:
        drive = ClusterDrive(line, 0.0, duration_us, scale * angle_rad, shape, drag_us=drag)
        return qubit_block(cluster, cluster.unitary(0.0, duration_us, (drive,)), line)

    def cost(point: np.ndarray) -> float:
        return block_infidelity(block(point[0], point[1] if tune_drag else 0.0), target)

    drag0 = -1.0 / (2.0 * 2.0 * math.pi * spec.anharmonicity_mhz) if tune_drag else 0.0
    if tune_drag:
        simplex = [np.array([1.0, drag0]), np.array([1.02, drag0]), np.array([1.0, 0.0])]
    else:
        simplex = [np.array([1.0, 0.0]), np.array([1.02, 0.0])]
    best = _nelder_mead(cost, simplex)
    scale, drag = float(best[0]), float(best[1]) if tune_drag else 0.0
    final = block(scale, drag)
    return CalibratedPulse(scale, drag, block_infidelity(final, target),
                           float(1.0 - 0.5 * np.trace(final.conj().T @ final).real))


def relaxation_error(spec: IdleQubitSpec, duration_us: float) -> float:
    """The average gate infidelity T1 and T_phi alone cause on a qubit over ``duration_us``.

    ``1 - F = (3 - e^{-t/T1} - 2 e^{-t/(2 T1) - t/T_phi}) / 6``: the Pauli transfer matrix keeps z by
    ``e^{-t/T1}`` and x, y by the coherence factor of ``relax_axes_in_place``.
    """
    if not math.isfinite(duration_us) or duration_us < 0.0:
        raise ValueError("duration_us must be finite and nonnegative")
    population = math.exp(-duration_us / spec.t1_us)
    coherence = math.exp(-0.5 * duration_us / spec.t1_us - duration_us / spec.tphi_us)
    return (3.0 - population - 2.0 * coherence) / 6.0


def residual_survival(calibrated_probability: float, relaxation: float) -> float:
    """The depolarizing weight left of a calibrated gate error once its relaxation part is simulated.

    A gate's calibrated channel is a depolarizing replacement with weight ``p`` (``1 - p`` survives, average
    infidelity ``p / 2``). Survivals multiply, so the part not explained by relaxation (average infidelity
    ``relaxation``) survives ``(1 - p) / (1 - 2 relaxation)``; it is at most one: a calibrated error smaller
    than the relaxation alone leaves nothing to add. Returns the residual weight ``p_res``.
    """
    if not 0.0 <= calibrated_probability <= 1.0 or not 0.0 <= relaxation < 0.5:
        raise ValueError("probabilities out of range")
    return max(0.0, 1.0 - min(1.0, (1.0 - calibrated_probability) / (1.0 - 2.0 * relaxation)))


def _nelder_mead(cost, simplex: list[np.ndarray]) -> np.ndarray:
    """Minimize ``cost`` from the starting simplex (standard reflection, expansion, contraction, shrink)."""
    points = [np.array(point, dtype=float) for point in simplex]
    values = [cost(point) for point in points]
    for _ in range(_MAX_ITERATIONS):
        order = np.argsort(values)
        points, values = [points[i] for i in order], [values[i] for i in order]
        spread = max(float(np.max(np.abs(point - points[0]) / np.maximum(np.abs(points[0]), 1e-12)))
                     for point in points[1:])
        if values[-1] - values[0] <= _TOLERANCE and spread <= _PARAMETER_TOLERANCE:
            break
        centroid = np.mean(points[:-1], axis=0)
        reflected = centroid + (centroid - points[-1])
        value = cost(reflected)
        if value < values[0]:
            expanded = centroid + 2.0 * (centroid - points[-1])
            expanded_value = cost(expanded)
            points[-1], values[-1] = (expanded, expanded_value) if expanded_value < value else (reflected, value)
        elif value < values[-2]:
            points[-1], values[-1] = reflected, value
        else:
            contracted = centroid + 0.5 * (points[-1] - centroid)
            contracted_value = cost(contracted)
            if contracted_value < values[-1]:
                points[-1], values[-1] = contracted, contracted_value
            else:
                points = [points[0] + 0.5 * (point - points[0]) for point in points]
                values = [values[0]] + [cost(point) for point in points[1:]]
    return points[int(np.argmin(values))]

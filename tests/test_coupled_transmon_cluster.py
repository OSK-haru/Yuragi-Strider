"""The coupled-transmon cluster (D91) against perturbation theory, the literature and an independent solver.

The independent solution works in the *bare* product basis, in one frame turning at transmon 0's frequency,
with an adaptive Runge-Kutta integrator (SciPy); the cluster works in the dressed basis, in per-drive frames,
with exponentials. They share only the physical model.
"""

import math
import unittest

import numpy as np
from scipy.integrate import solve_ivp

from core.attack_simulation.pulse_shape import GAUSSIAN_SHAPE, SQUARE_SHAPE
from core.experimental_isolation_physics.idle_evolution import IdleQubitSpec, relax_axes_in_place
from core.experimental_isolation_physics.transmon_cluster import (
    ClusterDrive, TransmonCluster, TransmonCollisionError, TransmonSpec, exchange_from_zz,
)
from core.pulse_transmon_network import TransmonExchangeCoupling

TWO_PI = 2.0 * math.pi
CONTROL, TARGET = TransmonSpec(5.10, -330.0), TransmonSpec(5.00, -310.0)
J_MHZ = 2.0
COUPLING = TransmonExchangeCoupling(0, 1, TWO_PI * J_MHZ)
DELTA_MHZ = 1e3 * (CONTROL.frequency_ghz - TARGET.frequency_ghz)


def vec(matrix):
    return np.asarray(matrix, dtype=complex).reshape(-1, order="F")


def unvec(vector):
    size = math.isqrt(len(vector))
    return vector.reshape(size, size, order="F")


def ket(cluster, label):
    state = np.zeros(cluster.dimension, dtype=complex)
    state[cluster.label_index(label)] = 1.0
    return state


def density(amplitudes):
    amplitudes = np.asarray(amplitudes, dtype=complex)
    amplitudes = amplitudes / np.linalg.norm(amplitudes)
    return np.outer(amplitudes, amplitudes.conj())


def independent_state(cluster, initial, start, end, drives=(), relaxation=None, transfers=()):
    """The same evolution from the bare Hamiltonian, in one common frame, by adaptive Runge-Kutta."""
    levels = cluster.levels
    count = len(levels)

    def embed(local, index):
        result = np.eye(1, dtype=complex)
        for position, level in enumerate(levels):
            result = np.kron(result, local if position == index else np.eye(level))
        return result

    ladders = [embed(np.diag(np.sqrt(np.arange(1, level)), 1).astype(complex), i) for i, level in enumerate(levels)]
    numbers = [ladder.conj().T @ ladder for ladder in ladders]
    hamiltonian = np.zeros((cluster.dimension,) * 2, dtype=complex)
    for i in range(count):
        hamiltonian += cluster.bare_offsets_rad_per_us[i] * numbers[i] + 0.5 * cluster.bare_anharmonicities_rad_per_us[i] * (
            numbers[i] @ numbers[i] - numbers[i])
    for coupling in cluster.couplings:
        hop = ladders[coupling.left].conj().T @ ladders[coupling.right]
        hamiltonian += coupling.strength_rad_per_us * (hop + hop.conj().T)
    values, vectors = np.linalg.eigh(hamiltonian)
    order = np.argsort(np.argmax(np.abs(vectors) ** 2, axis=0))
    vectors = vectors[:, order]
    vectors = vectors / (np.diag(vectors) / np.abs(np.diag(vectors)))[None, :]

    occupation = np.array(cluster.labels, dtype=float)
    frame = occupation @ np.array(cluster.offsets_rad_per_us)       # dressed frame minus the common frame

    def to_common(state, time):
        phase = np.exp(-1j * frame * time)
        return vectors @ (phase[:, None] * state * phase.conj()[None, :]) @ vectors.conj().T

    def to_dressed(state, time):
        phase = np.exp(1j * frame * time)
        return phase[:, None] * (vectors.conj().T @ state @ vectors) * phase.conj()[None, :]

    lines = [np.array(ladder) for ladder in ladders]
    for source, target, coefficient in transfers:
        lines[source] = lines[source] + np.conj(coefficient) * ladders[target]
    collapse = [vectors @ operator @ vectors.conj().T
                for operator in cluster.collapse_operators(relaxation or (None,) * count)]

    def rhs(time, flat):
        state = flat.reshape(cluster.dimension, cluster.dimension)
        total = hamiltonian.copy()
        for drive in drives:
            if drive.start_us <= time < drive.end_us:
                carrier = cluster.offsets_rad_per_us[drive.line] + drive.frequency_offset_rad_per_us
                rate = drive.rabi(time) * np.exp(1j * (drive.phase_rad - carrier * time))
                total += 0.5 * (np.conj(rate) * lines[drive.line] + rate * lines[drive.line].conj().T)
        result = -1j * (total @ state - state @ total)
        for operator in collapse:
            square = operator.conj().T @ operator
            result += operator @ state @ operator.conj().T - 0.5 * (square @ state + state @ square)
        return result.reshape(-1)

    state = to_common(initial, start)
    edges = sorted({start, end} | {edge for drive in drives for edge in (drive.start_us, drive.end_us)
                                   if start < edge < end})
    for low, high in zip(edges, edges[1:]):
        solution = solve_ivp(rhs, (low, high), state.reshape(-1), method="DOP853", rtol=1e-11, atol=1e-13)
        state = solution.y[:, -1].reshape(cluster.dimension, cluster.dimension)
    return to_dressed(state, end)


class SpectrumTests(unittest.TestCase):
    def test_uncoupled_cluster_is_the_bare_ladder(self):
        cluster = TransmonCluster((CONTROL, TARGET))
        self.assertEqual(cluster.mixing, 0.0)
        self.assertEqual(cluster.zz_rad_per_us(0, 1), 0.0)
        self.assertAlmostEqual(cluster.frame_energies[cluster.label_index((2, 0))], TWO_PI * -330.0, places=9)
        self.assertAlmostEqual(cluster.frame_energies[cluster.label_index((1, 2))], TWO_PI * -310.0, places=9)
        self.assertAlmostEqual(abs(cluster.drive_element(0, 0)), 1.0, places=12)
        self.assertEqual(cluster.drive_element(0, 1), 0.0)

    def test_the_dressed_spectrum_has_the_measured_values(self):
        cluster = TransmonCluster((CONTROL, TARGET), (COUPLING,))
        frame = cluster.frame_energies
        for label in ((0, 0), (1, 0), (0, 1)):
            self.assertEqual(frame[cluster.label_index(label)], 0.0)
        self.assertAlmostEqual(frame[cluster.label_index((2, 0))], TWO_PI * -330.0, places=7)
        self.assertAlmostEqual(frame[cluster.label_index((0, 2))], TWO_PI * -310.0, places=7)
        # The bare frequencies differ from the measured ones by the shift the coupling causes, J^2 / Delta.
        shift = cluster.bare_offsets_rad_per_us[0] - cluster.bare_offsets_rad_per_us[1] - TWO_PI * DELTA_MHZ
        self.assertAlmostEqual(shift / (-2.0 * TWO_PI * J_MHZ ** 2 / DELTA_MHZ), 1.0, delta=0.02)

    def test_zz_matches_second_order_perturbation_theory(self):
        cluster = TransmonCluster((CONTROL, TARGET), (COUPLING,))
        alpha_c, alpha_t = CONTROL.anharmonicity_mhz, TARGET.anharmonicity_mhz
        expected = 2.0 * J_MHZ ** 2 * (alpha_c + alpha_t) / ((DELTA_MHZ + alpha_c) * (DELTA_MHZ - alpha_t))
        self.assertAlmostEqual(cluster.zz_rad_per_us(0, 1) / (TWO_PI * expected), 1.0, delta=0.01)

    def test_two_level_transmons_have_no_zz(self):
        cluster = TransmonCluster((TransmonSpec(5.10, levels=2), TransmonSpec(5.00, levels=2)), (COUPLING,))
        self.assertEqual(cluster.zz_rad_per_us(0, 1), 0.0)
        self.assertGreater(cluster.mixing, 0.0)

    def test_cross_resonance_elements_match_magesan_and_gambetta(self):
        # arXiv:1804.04073, Eq. (133) and Appendix C, first order in the drive:
        # ZX/2 = -(J Omega / Delta) alpha_c / (alpha_c + Delta),  IX/2 = -J Omega / (Delta + alpha_c).
        cluster = TransmonCluster((CONTROL, TARGET), (COUPLING,))
        ground, excited = cluster.drive_element(0, 1, (0, 0)), cluster.drive_element(0, 1, (1, 0))
        alpha = CONTROL.anharmonicity_mhz
        self.assertAlmostEqual(ground.imag, 0.0, places=12)
        self.assertAlmostEqual(0.5 * (ground - excited).real / (-(J_MHZ / DELTA_MHZ) * alpha / (alpha + DELTA_MHZ)),
                               1.0, delta=0.01)
        self.assertAlmostEqual(0.5 * (ground + excited).real / (-J_MHZ / (DELTA_MHZ + alpha)), 1.0, delta=0.01)

    def test_a_frequency_collision_is_refused(self):
        with self.assertRaises(TransmonCollisionError):          # |11> and |20> meet at Delta = -alpha
            TransmonCluster((TransmonSpec(5.33, -330.0), TransmonSpec(5.00, -330.0)), (COUPLING,))
        with self.assertRaises(TransmonCollisionError):          # |10> and |01> meet at Delta = 0
            TransmonCluster((TransmonSpec(5.0, -330.0), TransmonSpec(5.0, -310.0)), (COUPLING,))

    def test_exchange_is_recovered_from_a_measured_zz(self):
        zz_khz = 1e3 * TransmonCluster((CONTROL, TARGET), (COUPLING,)).zz_rad_per_us(0, 1) / TWO_PI
        self.assertAlmostEqual(exchange_from_zz((CONTROL, TARGET), zz_khz), J_MHZ, places=6)
        self.assertEqual(exchange_from_zz((CONTROL, TARGET), 0.0), 0.0)
        with self.assertRaises(ValueError):
            exchange_from_zz((CONTROL, TARGET), -zz_khz)

    def test_three_transmons_and_limits(self):
        chain = TransmonCluster((CONTROL, TARGET, TransmonSpec(5.21, -320.0)),
                                (COUPLING, TransmonExchangeCoupling(1, 2, TWO_PI * 1.5)))
        self.assertEqual(chain.dimension, 27)
        self.assertNotEqual(chain.zz_rad_per_us(1, 2), 0.0)
        self.assertLess(abs(chain.zz_rad_per_us(0, 2)), 1e-2 * abs(chain.zz_rad_per_us(0, 1)))   # not coupled
        with self.assertRaises(ValueError):
            TransmonCluster((CONTROL,) * 4)
        with self.assertRaises(ValueError):
            TransmonSpec(5.0, None, 3)


class DynamicsTests(unittest.TestCase):
    def setUp(self):
        self.cluster = TransmonCluster((CONTROL, TARGET), (COUPLING,))

    def test_idle_evolution_is_the_static_shift(self):
        cluster, time = self.cluster, 7.5
        state = density(ket(cluster, (0, 0)) + ket(cluster, (1, 1)))
        out = unvec(cluster.propagator(3.0, 3.0 + time) @ vec(state))
        row, column = cluster.label_index((1, 1)), cluster.label_index((0, 0))
        self.assertAlmostEqual(out[row, column], 0.5 * np.exp(-1j * cluster.zz_rad_per_us(0, 1) * time), places=10)

    def test_two_level_relaxation_is_the_runtime_idle_channel(self):
        cluster = TransmonCluster((TransmonSpec(5.1, levels=2), TransmonSpec(5.0, levels=2)))
        specs = (IdleQubitSpec(40.0, 33.0, 0.02), IdleQubitSpec(60.0, math.inf, 0.0))
        rng = np.random.default_rng(5)
        raw = rng.normal(size=(4, 4)) + 1j * rng.normal(size=(4, 4))
        state = raw @ raw.conj().T
        state /= np.trace(state)
        expected = state.copy()
        relax_axes_in_place(expected, ((0, specs[0], 12.0), (1, specs[1], 12.0)))
        out = unvec(cluster.propagator(0.0, 12.0, relaxation=specs) @ vec(state))
        np.testing.assert_allclose(out, expected, atol=1e-12)

    def test_the_second_level_decays_twice_as_fast(self):
        cluster = TransmonCluster((CONTROL,))
        spec = IdleQubitSpec(50.0, math.inf, 0.0)
        out = unvec(cluster.propagator(0.0, 5.0, relaxation=(spec,)) @ vec(density(ket(cluster, (2,)))))
        self.assertAlmostEqual(out[2, 2].real, math.exp(-2.0 * 5.0 / 50.0), places=12)
        # A warm bath also refills |2> from |1>, at the Boltzmann factor of the 1-2 transition.
        warm = cluster.collapse_operators((IdleQubitSpec(50.0, math.inf, 0.02),))
        up_01 = next(abs(op[1, 0]) ** 2 for op in warm if op[1, 0])
        down_10 = next(abs(op[0, 1]) ** 2 for op in warm if op[0, 1])
        up_12 = next(abs(op[2, 1]) ** 2 for op in warm if op[2, 1])
        down_21 = next(abs(op[1, 2]) ** 2 for op in warm if op[1, 2])
        self.assertAlmostEqual(up_12 / down_21, (up_01 / down_10) ** ((5.10 - 0.330) / 5.10), places=12)
        self.assertAlmostEqual(down_21 - up_12, 2.0 * (down_10 - up_01), places=12)

    def check_against_independent(self, cluster, initial, start, end, drives, relaxation=None, transfers=(),
                                  tolerance=2e-7):
        ours = unvec(cluster.propagator(start, end, drives, relaxation) @ vec(initial))
        reference = independent_state(cluster, initial, start, end, drives, relaxation, transfers)
        self.assertLess(np.max(np.abs(ours - reference)), tolerance)
        return ours

    def test_a_drag_pulse_on_the_own_qubit_matches_the_independent_solution(self):
        cluster = self.cluster
        pulse = ClusterDrive(0, 2.0, 0.05, math.pi, GAUSSIAN_SHAPE, phase_rad=0.3, drag_us=4e-4)
        initial = density(ket(cluster, (0, 0)) + 0.6j * ket(cluster, (0, 1)))
        out = self.check_against_independent(cluster, initial, 1.9, 2.1, (pulse,))
        populations = np.diag(out).real
        self.assertGreater(populations[cluster.label_index((1, 0))] + populations[cluster.label_index((1, 1))], 0.95)

    def test_a_drive_at_the_neighbour_frequency_matches_the_independent_solution(self):
        cluster = self.cluster
        offset = cluster.offsets_rad_per_us[1] - cluster.offsets_rad_per_us[0]
        pulse = ClusterDrive(0, 100.0, 0.4, TWO_PI * 5.0 * 0.4, SQUARE_SHAPE, frequency_offset_rad_per_us=offset)
        for control in (0, 1):
            initial = density(ket(cluster, (control, 0)))
            out = self.check_against_independent(cluster, initial, 100.0, 100.4, (pulse,))
            # The target turns at Omega times the cross-resonance element of that control state.
            expected = math.sin(0.5 * TWO_PI * 5.0 * 0.4 * abs(cluster.drive_element(0, 1, (control, 0)))) ** 2
            moved = out[cluster.label_index((control, 1)), cluster.label_index((control, 1))].real
            self.assertAlmostEqual(moved / expected, 1.0, delta=0.05)

    def test_overlapping_drives_a_classical_leak_and_relaxation_match_the_independent_solution(self):
        transfers = ((0, 1, 0.03 - 0.01j),)
        cluster = TransmonCluster((CONTROL, TARGET), (COUPLING,), transfers)
        specs = (IdleQubitSpec(40.0, 33.0, 0.02), IdleQubitSpec(55.0, 70.0, 0.01))
        offset = cluster.offsets_rad_per_us[1] - cluster.offsets_rad_per_us[0]
        drives = (ClusterDrive(0, 0.02, 0.06, 2.4, GAUSSIAN_SHAPE, phase_rad=1.1, frequency_offset_rad_per_us=offset),
                  ClusterDrive(1, 0.05, 0.05, math.pi / 2, GAUSSIAN_SHAPE, phase_rad=-0.4, drag_us=3e-4))
        initial = density(ket(cluster, (1, 0)) + ket(cluster, (0, 1)) - 0.5 * ket(cluster, (1, 1)))
        self.check_against_independent(cluster, initial, 0.0, 0.12, drives, specs, transfers)

    def test_the_slice_size_has_converged(self):
        cluster = self.cluster
        pulse = ClusterDrive(0, 0.0, 0.05, math.pi, GAUSSIAN_SHAPE, drag_us=4e-4)
        coarse = cluster.propagator(0.0, 0.05, (pulse,))
        fine = cluster.propagator(0.0, 0.05, (pulse,), step_angle_rad=0.25)
        half = cluster.propagator(0.0, 0.05, (pulse,), step_angle_rad=2.0)
        self.assertLess(np.max(np.abs(coarse - fine)), 1e-7)
        # Fourth order: twice the slice size is about 16 times further away.
        self.assertAlmostEqual(np.max(np.abs(half - fine)) / np.max(np.abs(coarse - fine)), 16.0, delta=4.0)

    def test_splitting_off_the_relaxation_matches_the_whole_generator(self):
        """The default splits relaxation from the Hamiltonian per slice; the difference scales with the rates."""
        cluster = TransmonCluster((CONTROL, TARGET), (COUPLING,), ((0, 1, 0.03 - 0.01j),))
        offset = cluster.offsets_rad_per_us[1] - cluster.offsets_rad_per_us[0]
        drives = (ClusterDrive(0, 0.0, 0.05, math.pi, GAUSSIAN_SHAPE, drag_us=1e-3),
                  ClusterDrive(1, 0.01, 0.05, 1.3, GAUSSIAN_SHAPE, phase_rad=0.4, frequency_offset_rad_per_us=-offset))
        gaps = []
        for t1 in (100.0, 10.0):
            specs = (IdleQubitSpec(t1, 1.5 * t1, 0.01, levels=3, frequency_ghz=5.1, anharmonicity_mhz=-330.0),
                     IdleQubitSpec(t1, 1.5 * t1, 0.01, levels=3, frequency_ghz=5.0, anharmonicity_mhz=-310.0))
            split = cluster.propagator(0.0, 0.07, drives, specs)
            whole = cluster.propagator(0.0, 0.07, drives, specs, exact_relaxation=True)
            gaps.append(np.max(np.abs(split - whole)))
        self.assertLess(gaps[0], 1e-8)
        self.assertAlmostEqual(gaps[1] / gaps[0], 10.0, delta=3.0)
        # Without relaxation nothing is split: the two agree to round-off.
        np.testing.assert_allclose(cluster.propagator(0.0, 0.07, drives),
                                   cluster.propagator(0.0, 0.07, drives, exact_relaxation=True), atol=1e-11)

    def test_a_resonant_drive_on_the_second_transition_reaches_the_second_level(self):
        cluster = TransmonCluster((CONTROL,))
        # A pi pulse of the 1-2 transition: its matrix element is sqrt(2) times the 0-1 one.
        pulse = ClusterDrive(0, 0.0, 0.5, math.pi / math.sqrt(2.0), SQUARE_SHAPE,
                             frequency_offset_rad_per_us=TWO_PI * -330.0)
        out = unvec(cluster.propagator(0.0, 0.5, (pulse,)) @ vec(density(ket(cluster, (1,)))))
        self.assertGreater(out[2, 2].real, 0.999)


if __name__ == "__main__":
    unittest.main()

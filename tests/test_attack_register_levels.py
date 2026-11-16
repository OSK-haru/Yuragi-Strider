"""Register operations and idle relaxation on subsystems with more than two levels (D91)."""

import math
import random
import unittest

import numpy as np
from scipy.linalg import expm

from core.experimental_isolation_physics.idle_evolution import (
    IdleQubitSpec, IndependentIdleModel, ladder_relaxation_operators, relax_axes_in_place,
)
from core.register_operations import (
    apply_local_bloch_map, apply_local_depolarizing_replacement, apply_local_replacement, apply_operand_operator,
    apply_reset_with_feedback_kick, embed_qubit_operator, nonselective_measurement, register_dims,
    sample_measurement,
)

X = np.array([[0, 1], [1, 0]], dtype=complex)


def random_state(dimension, seed):
    rng = np.random.default_rng(seed)
    matrix = rng.normal(size=(dimension, dimension)) + 1j * rng.normal(size=(dimension, dimension))
    state = matrix @ matrix.conj().T
    return state / np.trace(state).real


def random_unitary(dimension, seed):
    rng = np.random.default_rng(seed)
    q, r = np.linalg.qr(rng.normal(size=(dimension, dimension)) + 1j * rng.normal(size=(dimension, dimension)))
    return q * (np.diag(r) / np.abs(np.diag(r)))


def embed(local, index, dims):
    result = np.eye(1, dtype=complex)
    for position, level in enumerate(dims):
        result = np.kron(result, local if position == index else np.eye(level))
    return result


def ket(dims, levels):
    index = 0
    for dim, level in zip(dims, levels):
        index = index * dim + level
    state = np.zeros((math.prod(dims),) * 2, dtype=complex)
    state[index, index] = 1.0
    return state


class RegisterTests(unittest.TestCase):
    DIMS = (3, 2, 4)

    def test_undeclared_levels_are_two_level_qubits(self):
        self.assertEqual(register_dims(8), (2, 2, 2))
        self.assertEqual(register_dims(24, self.DIMS), self.DIMS)
        for dimension, dims in ((6, None), (24, (3, 2, 2)), (4, (2, 5)), (4, (2, 2.0)), (1, ())):
            with self.subTest(dims=dims), self.assertRaises(ValueError):
                register_dims(dimension, dims)

    def test_two_level_results_do_not_depend_on_declaring_the_levels(self):
        state, unitary = random_state(8, 1), random_unitary(4, 2)
        dims = (2, 2, 2)
        for plain, declared in (
                (apply_operand_operator(state, unitary, (2, 0)), apply_operand_operator(state, unitary, (2, 0), dims)),
                (apply_local_replacement(state, 1, 0.3, (0.9, 0.1)), apply_local_replacement(state, 1, 0.3, (0.9, 0.1), dims)),
                (apply_reset_with_feedback_kick(state, 1, 0.7, (0.9, 0.1), random_unitary(2, 3)),
                 apply_reset_with_feedback_kick(state, 1, 0.7, (0.9, 0.1), random_unitary(2, 3), dims)),
                (apply_local_depolarizing_replacement(state, (0, 2), 0.2),
                 apply_local_depolarizing_replacement(state, (0, 2), 0.2, dims)),
                (nonselective_measurement(state, (0, 2)), nonselective_measurement(state, (0, 2), dims)),
                (sample_measurement(state, (2, 0), random.Random(5))[0],
                 sample_measurement(state, (2, 0), random.Random(5), dims)[0])):
            np.testing.assert_array_equal(plain, declared)
        self.assertEqual(sample_measurement(state, (2, 0), random.Random(5))[1],
                         sample_measurement(state, (2, 0), random.Random(5), dims)[1])

    def test_an_operator_on_mixed_levels_is_the_embedded_matrix(self):
        state = random_state(24, 4)
        local = random_unitary(4, 5)
        full = embed(local, 2, self.DIMS)
        np.testing.assert_allclose(apply_operand_operator(state, local, (2,), self.DIMS), full @ state @ full.conj().T,
                                   atol=1e-13)
        # Two operands out of register order: (subsystem 2, subsystem 0), 4 x 3 levels.
        pair = random_unitary(12, 6)
        swap = np.zeros((24, 24))
        for a in range(3):
            for b in range(2):
                for c in range(4):
                    swap[(c * 3 + a) * 2 + b, (a * 2 + b) * 4 + c] = 1.0     # (a, b, c) -> (c, a, b)
        full = swap.T @ np.kron(pair, np.eye(2)) @ swap
        np.testing.assert_allclose(apply_operand_operator(state, pair, (2, 0), self.DIMS),
                                   full @ state @ full.conj().T, atol=1e-13)
        with self.assertRaises(ValueError):
            apply_operand_operator(state, np.eye(2), (2,), self.DIMS)

    def test_a_qubit_gate_acts_on_the_qubit_levels_and_leaves_leaked_states_alone(self):
        np.testing.assert_array_equal(embed_qubit_operator(X, (2,)), X)
        np.testing.assert_array_equal(embed_qubit_operator(X, (3,)), [[0, 1, 0], [1, 0, 0], [0, 0, 1]])
        cz = np.diag([1, 1, 1, -1]).astype(complex)
        embedded = embed_qubit_operator(cz, (3, 2))                  # labels 00 01 10 11 20 21
        np.testing.assert_array_equal(embedded, np.diag([1, 1, 1, -1, 1, 1]))
        swap = np.eye(4)[[0, 2, 1, 3]].astype(complex)
        embedded = embed_qubit_operator(swap, (2, 3))                # labels 00 01 02 10 11 12
        expected = np.eye(6)
        expected[[1, 3]] = expected[[3, 1]]
        np.testing.assert_array_equal(embedded, expected)
        with self.assertRaises(ValueError):
            embed_qubit_operator(np.eye(3), (3,))

    def test_replacement_takes_one_population_per_level(self):
        state = random_state(24, 7)
        populations = (0.7, 0.2, 0.1)
        result = apply_local_replacement(state, 0, 0.4, populations, self.DIMS)
        reduced = np.einsum("aiaj->ij", state.reshape(3, 8, 3, 8))
        expected = 0.6 * state + 0.4 * np.kron(np.diag(populations), reduced)
        np.testing.assert_allclose(result, expected, atol=1e-13)
        with self.assertRaises(ValueError):
            apply_local_replacement(state, 0, 0.4, (1.0, 0.0), self.DIMS)

    def test_a_leaked_level_passes_through_a_measurement_based_reset(self):
        dims = (3,)
        reset = lambda state, kick=np.eye(2): apply_reset_with_feedback_kick(state, 0, 1.0, (1.0, 0.0), kick, dims)  # noqa: E731
        np.testing.assert_allclose(reset(ket(dims, (1,))), ket(dims, (0,)), atol=1e-14)
        np.testing.assert_allclose(reset(ket(dims, (0,))), ket(dims, (0,)), atol=1e-14)
        np.testing.assert_allclose(reset(ket(dims, (2,))), ket(dims, (2,)), atol=1e-14)
        # A superposition of |1> and |2> is measured: half goes to |0>, half stays in |2>, no coherence.
        plus = np.zeros((3, 3), dtype=complex)
        plus[1:, 1:] = 0.5
        np.testing.assert_allclose(reset(plus), np.diag([0.5, 0.0, 0.5]), atol=1e-14)
        # A rotation in the feedback acts on the collapsed qubit branch only.
        np.testing.assert_allclose(reset(ket(dims, (2,)), X), ket(dims, (2,)), atol=1e-14)
        np.testing.assert_allclose(reset(ket(dims, (0,)), X), ket(dims, (1,)), atol=1e-14)
        # An imperfect reset: the failed part is untouched.
        partial = apply_reset_with_feedback_kick(plus, 0, 0.6, (0.9, 0.1), np.eye(2), dims)
        np.testing.assert_allclose(partial, 0.4 * plus + 0.6 * np.diag([0.45, 0.05, 0.5]), atol=1e-14)

    def test_gate_error_mixes_the_qubit_levels_and_keeps_what_has_leaked(self):
        dims = (3, 2)
        state = random_state(6, 8)
        result = apply_local_depolarizing_replacement(state, (0,), 1.0, dims)
        self.assertAlmostEqual(np.trace(result).real, 1.0)
        blocks, old = result.reshape(3, 2, 3, 2), state.reshape(3, 2, 3, 2)
        half = 0.5 * (old[0, :, 0, :] + old[1, :, 1, :])
        np.testing.assert_allclose(blocks[0, :, 0, :], half, atol=1e-14)
        np.testing.assert_allclose(blocks[1, :, 1, :], half, atol=1e-14)
        np.testing.assert_allclose(blocks[2, :, 2, :], old[2, :, 2, :], atol=1e-14)
        for row, column in ((0, 1), (1, 0), (0, 2), (2, 0), (1, 2), (2, 1)):
            np.testing.assert_allclose(blocks[row, :, column, :], 0.0, atol=1e-14)
        self.assertGreaterEqual(np.linalg.eigvalsh(result).min(), -1e-12)
        weak = apply_local_depolarizing_replacement(state, (0,), 0.25, dims)
        np.testing.assert_allclose(weak, 0.75 * state + 0.25 * result, atol=1e-14)

    def test_measurement_reads_levels(self):
        state = ket(self.DIMS, (2, 1, 3))
        collapsed, levels = sample_measurement(state, (2, 0, 1), random.Random(1), self.DIMS)
        self.assertEqual(levels, (3, 2, 1))
        np.testing.assert_array_equal(collapsed, state)
        mixed = 0.25 * ket(self.DIMS, (0, 0, 0)) + 0.75 * ket(self.DIMS, (2, 0, 1))
        seen = {}
        generator = random.Random(3)
        for _ in range(400):
            collapsed, levels = sample_measurement(mixed, (0,), generator, self.DIMS)
            seen[levels] = seen.get(levels, 0) + 1
            np.testing.assert_allclose(collapsed, ket(self.DIMS, (levels[0], 0, 1 if levels[0] else 0)), atol=1e-14)
        self.assertEqual(set(seen), {(0,), (2,)})
        self.assertAlmostEqual(seen[(2,)] / 400, 0.75, delta=0.07)
        state = random_state(24, 9)
        dephased = nonselective_measurement(state, (0, 2), self.DIMS).reshape(3, 2, 4, 3, 2, 4)
        original = state.reshape(3, 2, 4, 3, 2, 4)
        for a in range(3):
            for c in range(4):
                np.testing.assert_array_equal(dephased[a, :, c, a, :, c], original[a, :, c, a, :, c])
        self.assertEqual(np.count_nonzero(dephased), 3 * 4 * 4)

    def test_a_bloch_map_is_refused_on_more_than_two_levels(self):
        with self.assertRaises(ValueError):
            apply_local_bloch_map(random_state(6, 1), 0, np.eye(3), (3, 2))
        np.testing.assert_allclose(apply_local_bloch_map(random_state(6, 1), 1, np.eye(3), (3, 2)),
                                   random_state(6, 1), atol=1e-14)


class IdleTests(unittest.TestCase):
    T1, TPHI = 40.0, 60.0

    def test_two_levels_through_the_ladder_are_the_closed_form(self):
        spec = IdleQubitSpec(self.T1, self.TPHI, 0.02)
        generator = np.zeros((4, 4), dtype=complex)
        for c in ladder_relaxation_operators(spec):
            cc = c.conj().T @ c
            generator += np.kron(c.conj(), c) - 0.5 * (np.kron(np.eye(2), cc) + np.kron(cc.T, np.eye(2)))
        state = random_state(2, 3)
        expected = (expm(generator * 7.0) @ state.reshape(-1, order="F")).reshape(2, 2, order="F")
        result = state.copy()
        relax_axes_in_place(result, ((0, spec, 7.0),))
        np.testing.assert_allclose(result, expected, atol=1e-13)

    def test_three_levels_at_zero_temperature_follow_the_ladder(self):
        """|2> decays twice as fast as |1>, through |1>; coherences decay with the noise each level sees."""
        spec = IdleQubitSpec(self.T1, self.TPHI, 0.0, levels=3)
        gamma, t = 1.0 / self.T1, 13.0
        state = random_state(3, 5)
        result = state.copy()
        relax_axes_in_place(result, ((0, spec, t),), (3,))
        e1, e2 = math.exp(-gamma * t), math.exp(-2.0 * gamma * t)
        p0, p1, p2 = np.diag(state).real
        np.testing.assert_allclose(np.diag(result).real, [p0 + p1 * (1 - e1) + p2 * (1 - 2 * e1 + e2),
                                                          p1 * e1 + 2 * p2 * (e1 - e2), p2 * e2], atol=1e-13)
        slow, fast = 0.5 * gamma + 1.0 / self.TPHI, 1.5 * gamma + 1.0 / self.TPHI
        self.assertAlmostEqual(result[1, 2], state[1, 2] * math.exp(-fast * t), places=13)
        self.assertAlmostEqual(result[0, 2], state[0, 2] * math.exp(-(gamma + 4.0 / self.TPHI) * t), places=13)
        # The two decays are separate jumps (their frequencies differ by the anharmonicity, far more than
        # either rate), so the coherence |1><2| loses does not reappear in |0><1|.
        self.assertAlmostEqual(result[0, 1], state[0, 1] * math.exp(-slow * t), places=13)
        np.testing.assert_allclose(result, result.conj().T, atol=1e-14)

    def test_a_warm_bath_leaves_the_levels_in_thermal_equilibrium(self):
        excited = 0.05
        spec = IdleQubitSpec(self.T1, math.inf, excited, levels=4, frequency_ghz=5.0, anharmonicity_mhz=-300.0)
        state = np.diag([0.1, 0.2, 0.3, 0.4]).astype(complex)
        relax_axes_in_place(state, ((0, spec, 400.0 * self.T1),), (4,))
        populations = np.diag(state).real
        boltzmann = excited / (1.0 - excited)                  # exp(-h f01 / k T), as the two-level value states
        self.assertAlmostEqual(populations[1] / populations[0], boltzmann, places=10)
        self.assertAlmostEqual(populations[2] / populations[1], boltzmann ** (4.7 / 5.0), places=10)
        self.assertAlmostEqual(populations[3] / populations[2], boltzmann ** (4.4 / 5.0), places=10)
        self.assertAlmostEqual(populations.sum(), 1.0)

    def test_a_register_of_mixed_levels_relaxes_each_subsystem_with_its_own_spec(self):
        specs = (IdleQubitSpec(30.0, 50.0, 0.0, levels=3), IdleQubitSpec(self.T1, self.TPHI, 0.01))
        model = IndependentIdleModel(specs, 2)
        self.assertEqual(model.dims, (3, 2))
        left, right = random_state(3, 1), random_state(2, 2)
        expected_left, expected_right = left.copy(), right.copy()
        relax_axes_in_place(expected_left, ((0, specs[0], 9.0),), (3,))
        relax_axes_in_place(expected_right, ((0, specs[1], 9.0),))
        np.testing.assert_allclose(model.evolve(np.kron(left, right), elapsed_us=9.0),
                                   np.kron(expected_left, expected_right), atol=1e-13)
        # A stack of operators, each axis with its own elapsed time.
        stack = np.stack([np.kron(left, right), np.kron(right, left).reshape(6, 6)[::-1, ::-1].copy()])
        single = stack[0].copy()
        relax_axes_in_place(stack, ((1, specs[1], 4.0), (0, specs[0], 2.0)), (3, 2))
        relax_axes_in_place(single, ((0, specs[0], 2.0), (1, specs[1], 4.0)), (3, 2))
        np.testing.assert_allclose(stack[0], single, atol=1e-13)
        with self.assertRaises(ValueError):
            model.evolve(np.eye(4) / 4.0, elapsed_us=1.0)
        with self.assertRaises(ValueError):
            relax_axes_in_place(np.kron(left, right), ((0, specs[1], 1.0),), (3, 2))

    def test_what_the_higher_levels_need_is_checked(self):
        IdleQubitSpec(self.T1, self.TPHI, 0.0, levels=3)                    # a cold bath needs no frequencies
        IdleQubitSpec(math.inf, self.TPHI, 0.3, levels=3)                   # no T1: the population is not used
        for options in (dict(levels=5), dict(levels=1), dict(levels=3.0),
                        dict(equilibrium_excited_population=0.02, levels=3),
                        dict(equilibrium_excited_population=0.6, levels=3, frequency_ghz=5.0, anharmonicity_mhz=-300.0),
                        dict(equilibrium_excited_population=0.02, levels=3, frequency_ghz=5.0)):
            values = {"t1_us": self.T1, "tphi_us": self.TPHI, "equilibrium_excited_population": 0.0, **options}
            with self.subTest(options=options), self.assertRaises(ValueError):
                IdleQubitSpec(**values)


if __name__ == "__main__":
    unittest.main()

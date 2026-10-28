"""Rust register batches compared with the existing independent Python operations."""

import math
import unittest

import numpy as np
import yuragi_strider_rust

from core.register_operations import (
    apply_local_depolarizing_replacement,
    apply_local_replacement,
    apply_operand_operator,
    freeze_register,
    nonselective_measurement,
    sample_measurement,
)
from core.rust_dense_kernel import flatten_complex_matrix, unflatten_complex_matrix
from core.experimental_isolation_physics.idle_evolution import IndependentIdleModel, IdleQubitSpec


def flat(matrix):
    return flatten_complex_matrix(freeze_register(np.asarray(matrix, dtype=complex)))


def session(matrix, qubits):
    return yuragi_strider_rust.RegisterSession("register_state_v1", flat(matrix), qubits)


class FixedDraw:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


class RustRegisterCoreTests(unittest.TestCase):
    def test_noncontiguous_reversed_operands_match_python_operator(self):
        rng = np.random.default_rng(919)
        ket = rng.normal(size=8) + 1j * rng.normal(size=8)
        ket /= np.linalg.norm(ket)
        rho = np.outer(ket, ket.conj())
        for operands in ((0,), (1,), (2,), (0, 2), (2, 0), (2, 1)):
            size = 1 << len(operands)
            candidate = rng.normal(size=(size, size)) + 1j * rng.normal(size=(size, size))
            unitary, _ = np.linalg.qr(candidate)
            expected = apply_operand_operator(rho, freeze_register(unitary), operands)
            actual = session(rho, 3)
            with self.subTest(operands=operands):
                self.assertEqual(actual.execute("register_batch_v1", [("unitary", list(operands), flat(unitary), 0.0)]), [])
                np.testing.assert_allclose(unflatten_complex_matrix(actual.state_flat(), 8), expected, atol=2e-13, rtol=0)

    def test_mixed_batch_matches_reference_and_preserves_measurement_order(self):
        rng = np.random.default_rng(882)
        ket = rng.normal(size=8) + 1j * rng.normal(size=8)
        ket /= np.linalg.norm(ket)
        initial = np.outer(ket, ket.conj())
        h = np.asarray([[1, 1], [1, -1]], dtype=complex) / math.sqrt(2)
        expected = apply_operand_operator(initial, freeze_register(h), (2,))
        expected = apply_local_replacement(expected, 0, 0.37, (0.2, 0.8))
        expected = apply_local_depolarizing_replacement(expected, (2, 0), 0.15)
        expected = nonselective_measurement(expected, (1,))
        expected, bits = sample_measurement(expected, (2, 0), FixedDraw(0.375))
        native = session(initial, 3)
        observed = native.execute("register_batch_v1", [
            ("unitary", [2], flat(h), 0.0),
            ("reset", [0], [0.37, 0.2, 0.8], 0.0),
            ("depolarize", [2, 0], [], 0.15),
            ("dephase", [1], [], 0.0),
            ("measure", [2, 0], [], 0.375),
        ])
        self.assertEqual(observed, [list(bits)])
        np.testing.assert_allclose(unflatten_complex_matrix(native.state_flat(), 8), expected, atol=2e-13, rtol=0)

    def test_boundary_draws_and_failed_batch_are_atomic(self):
        initial = np.diag([0.5, 0.0, 0.0, 0.5]).astype(complex)
        for draw, expected in ((0.0, [0]), (0.5, [1])):
            native = session(initial, 2)
            self.assertEqual(native.execute("register_batch_v1", [("measure", [0], [], draw)]), [expected])
        native = session(initial, 2)
        before = native.state_flat()
        x = flat(np.asarray([[0, 1], [1, 0]], dtype=complex))
        with self.assertRaises(ValueError):
            native.execute("register_batch_v1", [("unitary", [0], x, 0.0), ("measure", [1], [], 1.0)])
        self.assertEqual(native.state_flat(), before)
        with self.assertRaises(ValueError):
            native.execute("register_batch_v1", [("unitary", [0], x, 0.0), ("other", [1], [], 0.0)])
        self.assertEqual(native.state_flat(), before)
        with self.assertRaises(ValueError):
            native.execute("unknown", [])

    def test_idle_evolution_matches_independent_python_model_on_entangled_state(self):
        rng = np.random.default_rng(744)
        ket = rng.normal(size=8) + 1j * rng.normal(size=8)
        ket /= np.linalg.norm(ket)
        initial = np.outer(ket, ket.conj())
        specs = (IdleQubitSpec(80.0, 31.0, 0.18),
                 IdleQubitSpec(math.inf, math.inf, 0.0),
                 IdleQubitSpec(43.0, math.inf, 0.07))
        expected = IndependentIdleModel(specs, 3).evolve(initial, elapsed_us=7.3)
        native = session(initial, 3)
        native.execute("register_batch_v1", [("idle", [0, 2], [80.0, 31.0, 0.18, 43.0, math.inf, 0.07], 7.3)])
        np.testing.assert_allclose(unflatten_complex_matrix(native.state_flat(), 8), expected, atol=2e-13, rtol=0)
        before = native.state_flat()
        with self.assertRaises(ValueError):
            native.execute("register_batch_v1", [("idle", [1], [0.0, math.inf, 0.0], 2.0)])
        self.assertEqual(native.state_flat(), before)

    def test_measurement_draws_match_reference_and_keep_correlations(self):
        rng = np.random.default_rng(222)
        ket = rng.normal(size=8) + 1j * rng.normal(size=8)
        ket /= np.linalg.norm(ket)
        initial = np.outer(ket, ket.conj())
        for draw in (0.0, 0.05, 0.25, 0.5, 0.999999):
            expected, bits = sample_measurement(initial, (2, 0), FixedDraw(draw))
            native = session(initial, 3)
            with self.subTest(draw=draw):
                self.assertEqual(native.execute("register_batch_v1", [("measure", [2, 0], [], draw)]), [list(bits)])
                np.testing.assert_allclose(unflatten_complex_matrix(native.state_flat(), 8), expected,
                                           atol=2e-13, rtol=0)

    def test_checkpoint_snapshots_are_ordered_and_failed_batch_is_not_committed(self):
        initial = np.diag([0.0, 0.0, 0.0, 1.0]).astype(complex)
        native = session(initial, 2)
        x = np.asarray([[0, 1], [1, 0]], dtype=complex)
        reset = ("reset", [0], [1.0, 1.0, 0.0], 0.0)
        gate = ("unitary", [1], flat(x), 0.0)
        expected_checkpoint = apply_local_replacement(initial, 0, 1.0, (1.0, 0.0))
        expected_final = apply_operand_operator(expected_checkpoint, freeze_register(x), (1,))
        measurements, checkpoints = native.execute_with_checkpoints("register_batch_v1", [reset, gate], [0])
        self.assertEqual(measurements, [])
        self.assertEqual(len(checkpoints), 1)
        np.testing.assert_allclose(unflatten_complex_matrix(checkpoints[0], 4), expected_checkpoint, atol=1e-14)
        np.testing.assert_allclose(unflatten_complex_matrix(native.state_flat(), 4), expected_final, atol=1e-14)
        before = native.state_flat()
        with self.assertRaises(ValueError):
            native.execute_with_checkpoints("register_batch_v1", [reset, gate], [1, 1])
        self.assertEqual(native.state_flat(), before)


if __name__ == "__main__":
    unittest.main()

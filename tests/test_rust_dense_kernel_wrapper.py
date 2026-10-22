import math
import unittest

from core.gates import multi_qubit_physical_collapse_operators

from core.rust_dense_kernel import (
    RustDenseSession,
    flatten_collapse_ops,
    flatten_complex_matrix,
    is_rust_kernel_available,
    rust_rk4_evolve_segment,
    rust_rk4_evolve_segment_cleaned,
    rust_rk4_evolve_segment_samples,
    unflatten_complex_matrix,
)
from tests.physics_test_helpers import assert_matrix_close


class RustDenseKernelWrapperConversionTest(unittest.TestCase):
    def test_flatten_unflatten_round_trip(self) -> None:
        matrix = (
            (1.0 + 0.0j, 2.0 - 3.0j),
            (-0.5 + 0.25j, 4.0 + 0.0j),
        )

        flat = flatten_complex_matrix(matrix)
        restored = unflatten_complex_matrix(flat, 2)

        self.assertEqual(flat, [1.0, 0.0, 2.0, -3.0, -0.5, 0.25, 4.0, 0.0])
        self.assertEqual(restored, matrix)

    def test_flatten_rejects_non_square_matrix(self) -> None:
        with self.assertRaises(ValueError):
            flatten_complex_matrix(((1.0 + 0.0j, 2.0 + 0.0j),))

    def test_flatten_rejects_ragged_matrix(self) -> None:
        with self.assertRaises(ValueError):
            flatten_complex_matrix((
                (1.0 + 0.0j, 0.0 + 0.0j),
                (0.0 + 0.0j,),
            ))

    def test_unflatten_rejects_bad_length(self) -> None:
        with self.assertRaises(ValueError):
            unflatten_complex_matrix([1.0, 0.0], 2)

    def test_collapse_op_flattening_preserves_order(self) -> None:
        first = (
            (1.0 + 0.0j, 2.0 + 0.0j),
            (3.0 + 0.0j, 4.0 + 0.0j),
        )
        second = (
            (5.0 + 0.0j, 6.0 + 0.0j),
            (7.0 + 0.0j, 8.0 + 0.0j),
        )

        self.assertEqual(
            flatten_collapse_ops([first, second]),
            [
                1.0, 0.0, 2.0, 0.0, 3.0, 0.0, 4.0, 0.0,
                5.0, 0.0, 6.0, 0.0, 7.0, 0.0, 8.0, 0.0,
            ],
        )


@unittest.skipUnless(is_rust_kernel_available(), "yuragi_strider_rust is not importable")
class RustDenseKernelWrapperCallTest(unittest.TestCase):
    def test_wrapper_zero_rhs_leaves_rho_unchanged(self) -> None:
        rho = (
            (0.5 + 0.0j, 0.25 + 0.1j),
            (0.25 - 0.1j, 0.5 + 0.0j),
        )
        hamiltonian = _zero_matrix(2)

        result = rust_rk4_evolve_segment(rho, hamiltonian, [], 0.01, 3)

        assert_matrix_close(result, rho)

    def test_wrapper_hamiltonian_only_matches_reference(self) -> None:
        rho = (
            (0.6 + 0.0j, 0.2 + 0.15j),
            (0.2 - 0.15j, 0.4 + 0.0j),
        )
        hamiltonian = (
            (1.0 + 0.0j, 0.3 - 0.2j),
            (0.3 + 0.2j, -0.5 + 0.0j),
        )

        result = rust_rk4_evolve_segment(rho, hamiltonian, [], 0.005, 1)
        expected = _python_rk4_evolve(rho, hamiltonian, [], 0.005, 1)

        assert_matrix_close(result, expected)

    def test_wrapper_pure_dephasing_matches_reference(self) -> None:
        rho = (
            (0.5 + 0.0j, 0.4 + 0.1j),
            (0.4 - 0.1j, 0.5 + 0.0j),
        )
        hamiltonian = _zero_matrix(2)
        collapse = (
            (0.2 + 0.0j, 0.0 + 0.0j),
            (0.0 + 0.0j, -0.2 + 0.0j),
        )

        result = rust_rk4_evolve_segment(rho, hamiltonian, [collapse], 0.01, 1)
        expected = _python_rk4_evolve(rho, hamiltonian, [collapse], 0.01, 1)

        assert_matrix_close(result, expected)

    def test_cleaned_wrapper_returns_tuple_matrix_and_matches_reference(self) -> None:
        rho = (
            (0.35 + 0.0j, -0.2 + 0.25j),
            (-0.2 - 0.25j, 0.65 + 0.0j),
        )
        hamiltonian = (
            (0.0 + 0.0j, 0.1 + 0.2j),
            (0.1 - 0.2j, 0.25 + 0.0j),
        )
        collapse = (
            (0.2 + 0.0j, 0.0 + 0.0j),
            (0.0 + 0.0j, -0.2 + 0.0j),
        )

        result = rust_rk4_evolve_segment_cleaned(
            rho,
            hamiltonian,
            [collapse],
            0.004,
            5,
        )
        expected = _python_rk4_evolve_cleaned(
            rho,
            hamiltonian,
            [collapse],
            0.004,
            5,
        )

        self.assertIsInstance(result, tuple)
        self.assertIsInstance(result[0], tuple)
        assert_matrix_close(result, expected, tolerance=1e-10)

    def test_sampled_wrapper_returns_tuple_matrices(self) -> None:
        rho = (
            (0.5 + 0.0j, 0.25 + 0.1j),
            (0.25 - 0.1j, 0.5 + 0.0j),
        )
        hamiltonian = _zero_matrix(2)

        samples = rust_rk4_evolve_segment_samples(
            rho,
            hamiltonian,
            [],
            0.01,
            [1, 3],
        )

        self.assertEqual(len(samples), 2)
        self.assertIsInstance(samples, tuple)
        self.assertIsInstance(samples[0], tuple)
        self.assertIsInstance(samples[0][0], tuple)
        assert_matrix_close(samples[0], rho)
        assert_matrix_close(samples[1], rho)

    def test_persistent_session_batches_different_hamiltonians(self) -> None:
        rho = (
            (0.6 + 0.0j, 0.1 + 0.05j),
            (0.1 - 0.05j, 0.4 + 0.0j),
        )
        h1 = ((0.0 + 0.0j, 0.2 + 0.0j), (0.2 + 0.0j, 0.0 + 0.0j))
        h2 = ((0.1 + 0.0j, 0.0 - 0.15j), (0.0 + 0.15j, -0.1 + 0.0j))
        collapse = ((0.0 + 0.0j, 0.25 + 0.0j), (0.0 + 0.0j, 0.0 + 0.0j))
        session = RustDenseSession(rho, [collapse])

        samples = session.evolve_piecewise_cleaned_samples(
            rho,
            [h1, h2],
            [0.003, 0.002],
            [2, 3],
        )
        first = rust_rk4_evolve_segment_cleaned(rho, h1, [collapse], 0.003, 2)
        second = rust_rk4_evolve_segment_cleaned(first, h2, [collapse], 0.002, 3)

        self.assertEqual(len(samples), 2)
        assert_matrix_close(samples[0], first, tolerance=1e-10)
        assert_matrix_close(samples[1], second, tolerance=1e-10)

    def test_persistent_session_returns_paired_metrics(self) -> None:
        rho = ((1.0 + 0.0j, 0.0j), (0.0j, 0.0j))
        hamiltonian = ((0.0j, 0.2 + 0.0j), (0.2 + 0.0j, 0.0j))
        collapse = ((0.0j, 0.15 + 0.0j), (0.0j, 0.0j))
        session = RustDenseSession(rho, [collapse])

        noisy, ideal, metrics = session.evolve_paired_piecewise_metrics(
            rho,
            rho,
            [hamiltonian, hamiltonian],
            [0.002, 0.002],
            [2, 3],
        )

        self.assertEqual(len(noisy), 2)
        self.assertEqual(len(ideal), 2)
        self.assertEqual(len(metrics), 2)
        for noisy_state, ideal_state, (fidelity, purity, trace_error, ideal_purity) in zip(
            noisy, ideal, metrics
        ):
            self.assertAlmostEqual(
                fidelity,
                _python_lindblad_overlap(noisy_state, ideal_state),
                delta=1e-10,
            )
            self.assertAlmostEqual(
                purity,
                _python_lindblad_overlap(noisy_state, noisy_state),
                delta=1e-10,
            )
            self.assertLessEqual(trace_error, 1e-12)
            self.assertAlmostEqual(
                ideal_purity,
                _python_lindblad_overlap(ideal_state, ideal_state),
                delta=1e-10,
            )

    def test_local_rate_session_matches_explicit_two_qubit_operators(self) -> None:
        rho = tuple(
            tuple(1.0 + 0.0j if row == column == 3 else 0.0j for column in range(4))
            for row in range(4)
        )
        rates = (0.12, 0.03, 0.07)
        explicit = RustDenseSession(
            rho,
            multi_qubit_physical_collapse_operators(2, *rates),
        )
        local = RustDenseSession.from_local_rates(rho, 2, *rates)
        hamiltonian = _zero_matrix(4)

        expected = explicit.evolve_cleaned(rho, hamiltonian, 0.004, 5)
        actual = local.evolve_cleaned(rho, hamiltonian, 0.004, 5)

        assert_matrix_close(actual, expected, tolerance=1e-10)

    def test_compact_paired_output_keeps_metrics_but_selects_states(self) -> None:
        rho = ((1.0 + 0.0j, 0.0j), (0.0j, 0.0j))
        hamiltonian = ((0.0j, 0.2 + 0.0j), (0.2 + 0.0j, 0.0j))
        session = RustDenseSession.from_local_rates(rho, 1, 0.1, 0.02, 0.03)

        captured, metrics = session.evolve_paired_registered_compact(
            rho,
            rho,
            [hamiltonian] * 3,
            [0.002] * 3,
            [1, 1, 1],
            [False, True, False],
        )

        self.assertEqual(len(metrics), 3)
        self.assertEqual(set(captured), {1, 2})

    def test_wrapper_rejects_bad_dimensions(self) -> None:
        rho = ((1.0 + 0.0j,),)
        hamiltonian = _zero_matrix(2)

        with self.assertRaises(ValueError):
            rust_rk4_evolve_segment(rho, hamiltonian, [], 0.01, 1)

        with self.assertRaises(ValueError):
            rust_rk4_evolve_segment(hamiltonian, hamiltonian, [rho], 0.01, 1)

    def test_wrapper_rejects_invalid_step_inputs(self) -> None:
        matrix = _zero_matrix(2)

        with self.assertRaises(ValueError):
            rust_rk4_evolve_segment(matrix, matrix, [], 0.01, 0)

        with self.assertRaises(ValueError):
            rust_rk4_evolve_segment(matrix, matrix, [], math.nan, 1)

        with self.assertRaises(ValueError):
            rust_rk4_evolve_segment_cleaned(matrix, matrix, [], 0.01, 0)

        with self.assertRaises(ValueError):
            rust_rk4_evolve_segment_cleaned(matrix, matrix, [], math.inf, 1)

        with self.assertRaises(ValueError):
            rust_rk4_evolve_segment_samples(matrix, matrix, [], 0.01, [])

        with self.assertRaises(ValueError):
            rust_rk4_evolve_segment_samples(matrix, matrix, [], 0.01, [0])


def _zero_matrix(dimension: int):
    return tuple(
        tuple(0.0 + 0.0j for _ in range(dimension))
        for _ in range(dimension)
    )


def _python_rk4_evolve(
    rho,
    hamiltonian,
    collapse_ops,
    dt: float,
    substeps: int,
):
    evolved = tuple(tuple(entry for entry in row) for row in rho)
    for _ in range(substeps):
        k1 = _python_lindblad_rhs(evolved, hamiltonian, collapse_ops)
        k2 = _python_lindblad_rhs(_add_scaled(evolved, k1, 0.5 * dt), hamiltonian, collapse_ops)
        k3 = _python_lindblad_rhs(_add_scaled(evolved, k2, 0.5 * dt), hamiltonian, collapse_ops)
        k4 = _python_lindblad_rhs(_add_scaled(evolved, k3, dt), hamiltonian, collapse_ops)
        evolved = tuple(
            tuple(
                evolved[row][column]
                + dt / 6.0 * (
                    k1[row][column]
                    + 2.0 * k2[row][column]
                    + 2.0 * k3[row][column]
                    + k4[row][column]
                )
                for column in range(len(evolved[row]))
            )
            for row in range(len(evolved))
        )
    return evolved


def _python_rk4_evolve_cleaned(
    rho,
    hamiltonian,
    collapse_ops,
    dt: float,
    substeps: int,
):
    from core.gates import clean_density_matrix

    evolved = tuple(tuple(entry for entry in row) for row in rho)
    for _ in range(substeps):
        evolved = _python_rk4_evolve(evolved, hamiltonian, collapse_ops, dt, 1)
        evolved = clean_density_matrix(evolved)
    return evolved


def _python_lindblad_rhs(rho, hamiltonian, collapse_ops):
    derivative = _scale(
        -1.0j,
        _subtract(_matmul(hamiltonian, rho), _matmul(rho, hamiltonian)),
    )
    for collapse_op in collapse_ops:
        collapse_adjoint = _adjoint(collapse_op)
        ldl = _matmul(collapse_adjoint, collapse_op)
        dissipator = _subtract(
            _matmul(_matmul(collapse_op, rho), collapse_adjoint),
            _scale(0.5, _add(_matmul(ldl, rho), _matmul(rho, ldl))),
        )
        derivative = _add(derivative, dissipator)
    return derivative


def _matmul(left, right):
    dimension = len(left)
    return tuple(
        tuple(
            sum(left[row][inner] * right[inner][column] for inner in range(dimension))
            for column in range(dimension)
        )
        for row in range(dimension)
    )


def _adjoint(matrix):
    return tuple(
        tuple(matrix[row][column].conjugate() for row in range(len(matrix)))
        for column in range(len(matrix))
    )


def _add(left, right):
    return tuple(
        tuple(left[row][column] + right[row][column] for column in range(len(left[row])))
        for row in range(len(left))
    )


def _subtract(left, right):
    return tuple(
        tuple(left[row][column] - right[row][column] for column in range(len(left[row])))
        for row in range(len(left))
    )


def _scale(value: complex, matrix):
    return tuple(tuple(value * entry for entry in row) for row in matrix)


def _add_scaled(base, delta, scale: float):
    return tuple(
        tuple(
            base[row][column] + scale * delta[row][column]
            for column in range(len(base[row]))
        )
        for row in range(len(base))
    )


def _python_lindblad_overlap(left, right):
    return sum(
        left[row][column] * right[column][row]
        for row in range(len(left))
        for column in range(len(left))
    ).real


if __name__ == "__main__":
    unittest.main()

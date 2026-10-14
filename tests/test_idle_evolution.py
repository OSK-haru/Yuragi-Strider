"""Independent dense Kraus references and boundary guards for idle evolution."""

import math
import unittest
from unittest.mock import patch

import numpy as np

from core.cptp import generalized_amplitude_damping_channel, phase_damping_channel
from core.experimental_isolation_physics.idle_evolution import IndependentIdleModel, IdleQubitSpec


DISABLED = IdleQubitSpec(math.inf, math.inf, 0.0)


def _mixed_state(n: int, seed: int = 91) -> np.ndarray:
    generator = np.random.default_rng(seed)
    a = generator.normal(size=(1 << n, 1 << n)) + 1j * generator.normal(size=(1 << n, 1 << n))
    rho = a @ a.conj().T
    return rho / np.trace(rho)


def _reference(state: np.ndarray, specs: tuple[IdleQubitSpec, ...], elapsed: float) -> np.ndarray:
    # Deliberately use full embedded matrices, not the tensor-block kernel.
    result = state.copy()
    for axis, spec in enumerate(specs):
        channels = (
            generalized_amplitude_damping_channel(-math.expm1(-elapsed / spec.t1_us),
                                                   1.0 - spec.equilibrium_excited_population),
            phase_damping_channel(-math.expm1(-elapsed / spec.tphi_us)),
        )
        for channel in channels:
            transformed = np.zeros_like(result)
            for operator in channel.operators:
                embedded = np.ones((1, 1), dtype=complex)
                for q in range(len(specs)):
                    embedded = np.kron(embedded, operator if q == axis else np.eye(2))
                transformed += embedded @ result @ embedded.conj().T
            result = transformed
    return result


class IdleEvolutionTests(unittest.TestCase):
    def test_correlated_complex_register_matches_independent_kraus_reference(self) -> None:
        choices = (IdleQubitSpec(13.0, 27.0, 0.12), IdleQubitSpec(34.0, math.inf, 0.0),
                   IdleQubitSpec(math.inf, 8.0, 0.67))
        for n in (1, 2, 3):
            specs = choices[:n]
            model = IndependentIdleModel(specs, max_local_qubits=3)
            state = _mixed_state(n)
            original = state.copy()
            for elapsed in (0.0, 1e-9, 3.0, 50.0, 1e6):
                with self.subTest(n=n, elapsed=elapsed):
                    actual = model.evolve(state, elapsed_us=elapsed)
                    np.testing.assert_allclose(actual, _reference(state, specs, elapsed), atol=2e-14, rtol=0)
                    self.assertAlmostEqual(float(np.trace(actual).real), 1.0, places=13)
                    self.assertGreaterEqual(float(np.linalg.eigvalsh(actual).min()), -2e-14)
            np.testing.assert_array_equal(state, original)

    def test_interval_partition_and_local_channel_order_are_invariant(self) -> None:
        specs = (IdleQubitSpec(14.0, 19.0, 0.04), IdleQubitSpec(51.0, 9.0, 0.6))
        state = _mixed_state(2)
        model = IndependentIdleModel(specs, 2)
        split = model.evolve(model.evolve(state, elapsed_us=7.0), elapsed_us=11.0)
        combined = model.evolve(state, elapsed_us=18.0)
        np.testing.assert_allclose(split, combined, atol=2e-14, rtol=0)
        first = IndependentIdleModel((specs[0], DISABLED), 2)
        second = IndependentIdleModel((DISABLED, specs[1]), 2)
        reversed_order = first.evolve(second.evolve(state, elapsed_us=18.0), elapsed_us=18.0)
        np.testing.assert_allclose(reversed_order, combined, atol=2e-14, rtol=0)

    def test_t1_population_difference_and_tphi_coherence_have_declared_decay(self) -> None:
        spec = IdleQubitSpec(50.0, 80.0, 0.03)
        model = IndependentIdleModel((spec,), 1)
        elapsed = 17.0
        zero = model.evolve(np.diag([1.0, 0.0]), elapsed_us=elapsed)
        one = model.evolve(np.diag([0.0, 1.0]), elapsed_us=elapsed)
        self.assertAlmostEqual((one - zero)[1, 1].real, math.exp(-elapsed / 50.0), places=14)
        plus = model.evolve(np.full((2, 2), 0.5), elapsed_us=elapsed)
        self.assertAlmostEqual(plus[0, 1].real, 0.5 * math.exp(-elapsed / 100.0 - elapsed / 80.0), places=14)
        for source in (np.diag([1.0, 0.0]), np.diag([0.0, 1.0])):
            np.testing.assert_allclose(model.evolve(source, elapsed_us=1e8), np.diag([0.97, 0.03]), atol=1e-15)

    def test_bell_spectator_marginal_and_retained_coherence_are_not_replaced(self) -> None:
        ket = np.array([1, 0, 0, 1], dtype=complex) / math.sqrt(2)
        state = np.outer(ket, ket.conj())
        model = IndependentIdleModel((IdleQubitSpec(12.0, 18.0, 0.1), DISABLED), 2)
        evolved = model.evolve(state, elapsed_us=2.0)
        spectator = np.trace(evolved.reshape(2, 2, 2, 2), axis1=0, axis2=2)
        np.testing.assert_allclose(spectator, np.eye(2) / 2, atol=1e-14)
        self.assertAlmostEqual(evolved[0, 3].real, 0.5 * math.exp(-2.0 / 24.0 - 2.0 / 18.0), places=14)
        self.assertGreater(abs(evolved[0, 3]), 0.0)

    def test_disabled_and_zero_intervals_preserve_values_without_aliasing(self) -> None:
        state = _mixed_state(2).T  # Non-contiguous input is supported.
        model = IndependentIdleModel((DISABLED, DISABLED), 2)
        for elapsed in (0.0, 1e9):
            result = model.evolve(state, elapsed_us=elapsed)
            np.testing.assert_array_equal(result, state)
            self.assertFalse(np.shares_memory(result, state))
        active = IndependentIdleModel((IdleQubitSpec(1.0, 1.0, 0.0), DISABLED), 2)
        np.testing.assert_array_equal(active.evolve(state, elapsed_us=0.0), state)

    def test_tiny_and_extreme_ratios_stay_finite_without_time_steps(self) -> None:
        one = np.diag([0.0, 1.0])
        tiny = IndependentIdleModel((IdleQubitSpec(1.0, math.inf, 0.0),), 1)
        self.assertGreater(tiny.evolve(one, elapsed_us=1e-20)[0, 0].real, 0.0)
        extreme = IndependentIdleModel((IdleQubitSpec(5e-324, 5e-324, 0.25),), 1)
        with patch("numpy.linalg.eigvalsh", side_effect=AssertionError("kernel must not repeat PSD eigensolve")):
            result = extreme.evolve(one, elapsed_us=1e308)
        np.testing.assert_array_equal(result, np.diag([0.75, 0.25]))

    def test_invalid_specs_duration_and_capacity_are_rejected(self) -> None:
        for value in (True, "5", -math.inf, 0.0, -1.0, math.nan):
            for field in ("t1_us", "tphi_us"):
                values = dict(t1_us=5.0, tphi_us=8.0, equilibrium_excited_population=0.1)
                values[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    IdleQubitSpec(**values)
        for value in (True, "0", -0.1, 1.1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                IdleQubitSpec(5.0, 8.0, value)
        for specs, limit in (((), 2), ((DISABLED, DISABLED), 1), ([DISABLED], 2),
                             ((object(),), 2), ((DISABLED,), True), ((DISABLED,), 0)):
            with self.assertRaises(ValueError):
                IndependentIdleModel(specs, limit)
        model = IndependentIdleModel((DISABLED,), 1)
        for value in (True, "0", -1.0, math.nan, math.inf):
            with self.assertRaises(ValueError):
                model.evolve(np.eye(2) / 2, elapsed_us=value)

    def test_bad_state_shape_and_values_are_rejected_before_copy(self) -> None:
        model = IndependentIdleModel((DISABLED,), 1)
        states = (np.eye(4) / 4, np.eye(3) / 3, np.ones(2), np.eye(2, dtype=bool),
                  np.eye(2, dtype=object), np.full((2, 2), math.nan), np.eye(2),
                  np.array([[1, 1], [0, 0]], dtype=complex), [[1, 0], [0, 0]])
        for state in states:
            with self.subTest(shape=getattr(state, "shape", None)), self.assertRaises(ValueError):
                model.evolve(state, elapsed_us=0.0)
        with patch("numpy.array", side_effect=AssertionError("must reject before allocation")):
            with self.assertRaises(ValueError):
                model.evolve(np.eye(4) / 4, elapsed_us=1.0)

    def test_declared_interval_does_not_read_host_clocks(self) -> None:
        model = IndependentIdleModel((IdleQubitSpec(10.0, 20.0, 0.02),), 1)
        state = np.diag([0.0, 1.0])
        expected = model.evolve(state, elapsed_us=3.0)
        with (patch("time.monotonic", side_effect=AssertionError("no host clock")),
              patch("time.perf_counter", side_effect=AssertionError("no host clock")),
              patch("time.time", side_effect=AssertionError("no host clock"))):
            actual = model.evolve(state, elapsed_us=3.0)
        np.testing.assert_array_equal(actual, expected)


if __name__ == "__main__":
    unittest.main()

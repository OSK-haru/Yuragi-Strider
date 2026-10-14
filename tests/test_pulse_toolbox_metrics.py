"""Pulse toolbox metrics: QuTiP cross-checks, leakage handling, spec rules, and the API."""

import asyncio
import math
import unittest

import numpy as np

from api.main import PULSE_TOOLBOX_PATH, app
from core.pulse_toolbox_metrics import (
    PulseToolboxSpec,
    compute_pulse_toolbox_metrics,
    density_from_upper_triangle,
    pulse_negativity,
    pulse_partial_trace,
    validate_pulse_toolbox_spec,
)
from core.state_metrics import (
    concurrence,
    mutual_information,
    negativity,
    partial_trace,
    pauli_expectation,
    von_neumann_entropy,
)
from tests.test_pulse_api_baseline_a import _asgi_post_json

try:
    import qutip
except ImportError:  # QuTiP is a validation-only dependency.
    qutip = None


def _random_density(dimension: int, rank: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    columns = rng.normal(size=(dimension, rank)) + 1j * rng.normal(size=(dimension, rank))
    rho = columns @ columns.conj().T
    return rho / np.trace(rho)


def _ket_density(ket: np.ndarray) -> np.ndarray:
    ket = np.asarray(ket, dtype=np.complex128)
    ket = ket / np.linalg.norm(ket)
    return np.outer(ket, ket.conj())


def _basis(dimension: int, index: int) -> np.ndarray:
    ket = np.zeros(dimension, dtype=np.complex128)
    ket[index] = 1.0
    return ket


def _metrics(spec: PulseToolboxSpec, levels: int, count: int, rho: np.ndarray) -> dict:
    return compute_pulse_toolbox_metrics(spec, levels, count, [0.0], [rho])


class TwoLevelAgreesWithTheQubitMetricsTests(unittest.TestCase):
    def test_two_level_network_matches_the_qubit_implementations(self) -> None:
        rho = _random_density(8, 3, seed=7)
        spec = PulseToolboxSpec(
            entropy_transmons=(0, 2),
            concurrence_transmons=(0, 1),
            negativity_subsystem=(1,),
            mutual_information_a=(0,),
            mutual_information_b=(2,),
            observables=("XXI", "ZIZ"),
        )
        result = _metrics(spec, 2, 3, rho)

        self.assertAlmostEqual(
            result["entropy"]["values"][0],
            von_neumann_entropy(partial_trace(rho, 3, [0, 2])),
            places=12,
        )
        self.assertAlmostEqual(
            result["concurrence"]["values"][0],
            concurrence(partial_trace(rho, 3, [0, 1])),
            places=12,
        )
        self.assertAlmostEqual(
            result["negativity"]["values"][0], negativity(rho, 3, [1]), places=12
        )
        self.assertAlmostEqual(
            result["mutual_information"]["values"][0],
            mutual_information(rho, 3, [0], [2]),
            places=12,
        )
        for entry in result["observables"]:
            self.assertAlmostEqual(
                entry["values"][0], pauli_expectation(rho, entry["pauli"]), places=12
            )

    def test_bell_pair_values(self) -> None:
        bell = _ket_density([1.0, 0.0, 0.0, 1.0])
        spec = PulseToolboxSpec(
            entropy_transmons=(0,),
            concurrence_transmons=(0, 1),
            negativity_subsystem=(0,),
            mutual_information_a=(0,),
            mutual_information_b=(1,),
            observables=("XX", "YY", "ZZ"),
        )
        result = _metrics(spec, 2, 2, bell)
        self.assertAlmostEqual(result["entropy"]["values"][0], 1.0, places=12)
        self.assertAlmostEqual(result["concurrence"]["values"][0], 1.0, places=12)
        self.assertAlmostEqual(result["negativity"]["values"][0], 0.5, places=12)
        self.assertAlmostEqual(result["mutual_information"]["values"][0], 2.0, places=12)
        values = {entry["pauli"]: entry["values"][0] for entry in result["observables"]}
        self.assertAlmostEqual(values["XX"], 1.0, places=12)
        self.assertAlmostEqual(values["YY"], -1.0, places=12)
        self.assertAlmostEqual(values["ZZ"], 1.0, places=12)


class ThreeLevelTests(unittest.TestCase):
    def test_transmon_zero_is_the_most_significant_factor(self) -> None:
        # |1>|0> on two qutrits: basis index 1 * 3 + 0.
        rho = _ket_density(_basis(9, 3))
        # ZI and IZ are single-transmon Z strings, which the spec rejects; ZZ is not.
        result = _metrics(PulseToolboxSpec(observables=("ZZ",)), 3, 2, rho)
        self.assertAlmostEqual(result["observables"][0]["values"][0], -1.0, places=12)
        kept = pulse_partial_trace(rho, 3, 2, [0])
        self.assertAlmostEqual(kept[1, 1].real, 1.0, places=12)
        kept = pulse_partial_trace(rho, 3, 2, [1])
        self.assertAlmostEqual(kept[0, 0].real, 1.0, places=12)

    def test_leakage_level_contributes_nothing_to_x_y_z(self) -> None:
        leaked = _ket_density(_basis(3, 2))
        result = _metrics(PulseToolboxSpec(observables=("X", "Y")), 3, 1, leaked)
        for entry in result["observables"]:
            self.assertAlmostEqual(entry["values"][0], 0.0, places=12)

    def test_superposition_in_the_computational_levels(self) -> None:
        plus = _ket_density([1.0, 1.0, 0.0])
        result = _metrics(PulseToolboxSpec(observables=("X", "Y")), 3, 1, plus)
        values = {entry["pauli"]: entry["values"][0] for entry in result["observables"]}
        self.assertAlmostEqual(values["X"], 1.0, places=12)
        self.assertAlmostEqual(values["Y"], 0.0, places=12)

    def test_single_qutrit_entropy_is_independent_of_the_purity_shown(self) -> None:
        # Both spectra have purity 1/2, yet different entropy. That is only
        # possible above two levels, which is why the qutrit entropy is offered.
        flat = np.diag([0.5, 0.5, 0.0]).astype(np.complex128)
        skew = np.diag([2.0 / 3.0, 1.0 / 6.0, 1.0 / 6.0]).astype(np.complex128)
        self.assertAlmostEqual(np.trace(flat @ flat).real, np.trace(skew @ skew).real, places=12)
        spec = PulseToolboxSpec(entropy_transmons=(0,))
        self.assertAlmostEqual(_metrics(spec, 3, 1, flat)["entropy"]["values"][0], 1.0, places=12)
        self.assertAlmostEqual(
            _metrics(spec, 3, 1, skew)["entropy"]["values"][0],
            -(2.0 / 3.0 * math.log2(2.0 / 3.0) + 2 * (1.0 / 6.0) * math.log2(1.0 / 6.0)),
            places=12,
        )

    def test_qutrit_negativity_of_a_maximally_entangled_pair(self) -> None:
        ket = (_basis(9, 0) + _basis(9, 4) + _basis(9, 8)) / math.sqrt(3.0)
        result = _metrics(
            PulseToolboxSpec(negativity_subsystem=(0,), mutual_information_a=(0,), mutual_information_b=(1,)),
            3,
            2,
            _ket_density(ket),
        )
        # N = (d - 1) / 2 and I = 2 log2 d for a maximally entangled pair.
        self.assertAlmostEqual(result["negativity"]["values"][0], 1.0, places=12)
        self.assertAlmostEqual(
            result["mutual_information"]["values"][0], 2.0 * math.log2(3.0), places=12
        )

    @unittest.skipIf(qutip is None, "QuTiP is not installed")
    def test_qutip_agrees_on_random_three_level_states(self) -> None:
        dims = [[3, 3], [3, 3]]
        for seed in (1, 2, 3):
            rho = _random_density(9, 4, seed)
            state = qutip.Qobj(rho, dims=dims)
            spec = PulseToolboxSpec(
                entropy_transmons=(0,),
                negativity_subsystem=(0,),
                mutual_information_a=(0,),
                mutual_information_b=(1,),
            )
            result = _metrics(spec, 3, 2, rho)
            self.assertAlmostEqual(
                result["entropy"]["values"][0],
                qutip.entropy_vn(state.ptrace(0), base=2),
                places=9,
            )
            self.assertAlmostEqual(
                result["negativity"]["values"][0],
                qutip.negativity(state, 0, method="eigenvalues"),
                places=9,
            )
            self.assertAlmostEqual(
                result["mutual_information"]["values"][0],
                qutip.entropy_mutual(state, [0], [1], base=2),
                places=9,
            )
            self.assertAlmostEqual(
                pulse_negativity(rho, 3, 2, [1]),
                qutip.negativity(state, 1, method="eigenvalues"),
                places=9,
            )


class SpecRuleTests(unittest.TestCase):
    def assertRejected(self, spec: PulseToolboxSpec, levels: int, count: int) -> None:
        with self.assertRaises(ValueError):
            validate_pulse_toolbox_spec(spec, levels, count)

    def test_single_two_level_entropy_is_a_function_of_purity(self) -> None:
        self.assertRejected(PulseToolboxSpec(entropy_transmons=(0,)), 2, 1)
        validate_pulse_toolbox_spec(PulseToolboxSpec(entropy_transmons=(0,)), 3, 1)
        validate_pulse_toolbox_spec(PulseToolboxSpec(entropy_transmons=(0,)), 2, 2)

    def test_concurrence_needs_two_level_transmons(self) -> None:
        self.assertRejected(PulseToolboxSpec(concurrence_transmons=(0, 1)), 3, 2)
        self.assertRejected(PulseToolboxSpec(concurrence_transmons=(0, 0)), 2, 2)
        validate_pulse_toolbox_spec(PulseToolboxSpec(concurrence_transmons=(1, 0)), 2, 2)

    def test_entanglement_metrics_need_a_second_transmon(self) -> None:
        self.assertRejected(PulseToolboxSpec(negativity_subsystem=(0,)), 3, 1)
        self.assertRejected(
            PulseToolboxSpec(mutual_information_a=(0,), mutual_information_b=(0,)), 3, 2
        )

    def test_observables_must_not_restate_the_populations(self) -> None:
        for pauli in ("Z", "ZI", "IZI", "I", "II"):
            count = len(pauli)
            self.assertRejected(PulseToolboxSpec(observables=(pauli,)), 3, count)
        for pauli, count in (("X", 1), ("IY", 2), ("ZZ", 2), ("XZ", 2)):
            validate_pulse_toolbox_spec(PulseToolboxSpec(observables=(pauli,)), 3, count)

    def test_observable_shape_and_alphabet(self) -> None:
        self.assertRejected(PulseToolboxSpec(observables=("XX",)), 3, 3)
        self.assertRejected(PulseToolboxSpec(observables=("XA",)), 3, 2)
        self.assertRejected(PulseToolboxSpec(observables=tuple("XX" for _ in range(9))), 2, 2)

    def test_geometry_is_bounded(self) -> None:
        self.assertRejected(PulseToolboxSpec(), 4, 2)
        self.assertRejected(PulseToolboxSpec(), 2, 5)

    def test_wrong_matrix_shape_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compute_pulse_toolbox_metrics(
                PulseToolboxSpec(entropy_transmons=(0,)), 3, 1, [0.0], [np.eye(2)]
            )


def _matrix_payload(rho: np.ndarray) -> dict:
    rows, columns = np.triu_indices(rho.shape[0])
    return {
        "upper_real": rho.real[rows, columns].tolist(),
        "upper_imag": rho.imag[rows, columns].tolist(),
    }


class PulseToolboxApiTests(unittest.TestCase):
    def _payload(self, **extra: object) -> dict:
        bell = _ket_density([1.0, 0.0, 0.0, 1.0])
        product = _ket_density(_basis(4, 0))
        return {
            "local_levels": 2,
            "transmon_count": 2,
            "times_us": [0.0, 0.1],
            "density_matrices": [_matrix_payload(product), _matrix_payload(bell)],
            **extra,
        }

    def test_returns_one_value_per_point(self) -> None:
        status, response = asyncio.run(_asgi_post_json(
            app,
            PULSE_TOOLBOX_PATH,
            self._payload(
                entropy_transmons=[0],
                concurrence_transmons=[0, 1],
                negativity_subsystem=[0],
                observables=["XX"],
            ),
        ))
        self.assertEqual(status, 200)
        self.assertEqual(response["schema_version"], "pulse-toolbox-v1")
        self.assertEqual(response["times_us"], [0.0, 0.1])
        self.assertAlmostEqual(response["entropy"]["values"][0], 0.0, places=12)
        self.assertAlmostEqual(response["entropy"]["values"][1], 1.0, places=12)
        self.assertAlmostEqual(response["concurrence"]["values"][1], 1.0, places=12)
        self.assertAlmostEqual(response["negativity"]["values"][1], 0.5, places=12)
        self.assertAlmostEqual(response["observables"][0]["values"][1], 1.0, places=12)
        self.assertNotIn("mutual_information", response)

    def test_rejects_a_restated_metric(self) -> None:
        status, _ = asyncio.run(_asgi_post_json(
            app, PULSE_TOOLBOX_PATH, self._payload(observables=["ZI"])
        ))
        self.assertEqual(status, 422)

    def test_rejects_a_mismatched_series(self) -> None:
        payload = self._payload(entropy_transmons=[0])
        payload["times_us"] = [0.0]
        status, _ = asyncio.run(_asgi_post_json(app, PULSE_TOOLBOX_PATH, payload))
        self.assertEqual(status, 422)

    def test_rejects_an_oversized_request(self) -> None:
        # 4 transmons x 3 levels = 81 x 81; 40 points exceed the element budget.
        zero = {"upper_real": [0.0] * 3321, "upper_imag": [0.0] * 3321}
        payload = {
            "local_levels": 3,
            "transmon_count": 4,
            "times_us": [float(index) for index in range(40)],
            "density_matrices": [zero] * 40,
            "entropy_transmons": [0],
        }
        status, _ = asyncio.run(_asgi_post_json(app, PULSE_TOOLBOX_PATH, payload))
        self.assertEqual(status, 422)


class UpperTriangleTests(unittest.TestCase):
    def test_round_trip_rebuilds_the_hermitian_matrix(self) -> None:
        for dimension in (2, 9, 81):
            rho = _random_density(dimension, 3, seed=dimension)
            payload = _matrix_payload(rho)
            rebuilt = density_from_upper_triangle(
                payload["upper_real"], payload["upper_imag"], dimension
            )
            np.testing.assert_allclose(rebuilt, rho, atol=1e-15)

    def test_rounding_to_ten_digits_keeps_the_metrics(self) -> None:
        rho = _random_density(9, 4, seed=5)
        payload = _matrix_payload(rho)
        rounded = density_from_upper_triangle(
            [float(f"{value:.10g}") for value in payload["upper_real"]],
            [float(f"{value:.10g}") for value in payload["upper_imag"]],
            9,
        )
        spec = PulseToolboxSpec(entropy_transmons=(0,), negativity_subsystem=(0,))
        exact = _metrics(spec, 3, 2, rho)
        approx = _metrics(spec, 3, 2, rounded)
        for key in ("entropy", "negativity"):
            self.assertAlmostEqual(exact[key]["values"][0], approx[key]["values"][0], places=8)

    def test_wrong_length_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            density_from_upper_triangle([0.0] * 5, [0.0] * 5, 3)


class ToolboxCapacityTests(unittest.TestCase):
    def test_busy_slots_answer_503_and_are_released(self) -> None:
        from api import main

        payload = PulseToolboxApiTests()._payload(entropy_transmons=[0])
        held = [main._PULSE_TOOLBOX_EXECUTION_SLOTS.acquire(blocking=False) for _ in range(2)]
        try:
            self.assertTrue(all(held))
            status, _ = asyncio.run(_asgi_post_json(app, PULSE_TOOLBOX_PATH, payload))
            self.assertEqual(status, 503)
        finally:
            for _ in held:
                main._PULSE_TOOLBOX_EXECUTION_SLOTS.release()
        status, _ = asyncio.run(_asgi_post_json(app, PULSE_TOOLBOX_PATH, payload))
        self.assertEqual(status, 200)
        # A rejected request must not leak a slot either.
        bad = PulseToolboxApiTests()._payload(observables=["ZI"])
        for _ in range(3):
            self.assertEqual(asyncio.run(_asgi_post_json(app, PULSE_TOOLBOX_PATH, bad))[0], 422)
        self.assertEqual(asyncio.run(_asgi_post_json(app, PULSE_TOOLBOX_PATH, payload))[0], 200)


class RequestBodyLimitTests(unittest.TestCase):
    def test_default_routes_answer_413_when_the_body_is_too_large(self) -> None:
        status, _ = asyncio.run(_asgi_post_json(app, "/api/simulate", {"x": "a" * 70_000}))
        self.assertEqual(status, 413)

    def test_only_the_toolbox_route_has_the_larger_budget(self) -> None:
        # 70 KB is over the default budget but fine for the toolbox route, which
        # then rejects it on content (422), not on size.
        status, _ = asyncio.run(_asgi_post_json(
            app, PULSE_TOOLBOX_PATH, {"x": "a" * 70_000}
        ))
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()

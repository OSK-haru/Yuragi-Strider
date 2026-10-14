"""Toolbox state metrics: QuTiP cross-checks, spec rules, and the /api/simulate block."""

import asyncio
import math
import unittest

import numpy as np

from api.main import app
from core.state_metrics import (
    concurrence,
    mutual_information,
    negativity,
    partial_trace,
    pauli_expectation,
    trace_distance,
    von_neumann_entropy,
)
from core.toolbox_metrics import ToolboxSnapshotSpec, validate_toolbox_spec
from tests.test_pulse_api_baseline_a import _asgi_post_json

try:
    import qutip
except ImportError:  # QuTiP is a validation-only dependency.
    qutip = None


def _random_density(n_qubits: int, rank: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    dimension = 2 ** n_qubits
    columns = rng.normal(size=(dimension, rank)) + 1j * rng.normal(size=(dimension, rank))
    rho = columns @ columns.conj().T
    return rho / np.trace(rho)


def _bell() -> np.ndarray:
    ket = np.array([1.0, 0.0, 0.0, 1.0], dtype=np.complex128) / math.sqrt(2.0)
    return np.outer(ket, ket.conj())


class AnalyticStateMetricTests(unittest.TestCase):
    def test_bell_state_values(self) -> None:
        rho = _bell()
        self.assertAlmostEqual(von_neumann_entropy(rho), 0.0, places=12)
        self.assertAlmostEqual(von_neumann_entropy(partial_trace(rho, 2, [0])), 1.0, places=12)
        self.assertAlmostEqual(concurrence(rho), 1.0, places=12)
        self.assertAlmostEqual(negativity(rho, 2, [0]), 0.5, places=12)
        self.assertAlmostEqual(mutual_information(rho, 2, [0], [1]), 2.0, places=12)
        self.assertAlmostEqual(pauli_expectation(rho, "ZZ"), 1.0, places=12)
        self.assertAlmostEqual(pauli_expectation(rho, "XX"), 1.0, places=12)
        self.assertAlmostEqual(pauli_expectation(rho, "YY"), -1.0, places=12)

    def test_product_state_has_no_entanglement(self) -> None:
        ket = np.kron([1.0, 0.0], [1.0, 1.0]) / math.sqrt(2.0)
        rho = np.outer(ket, ket.conj()).astype(np.complex128)
        self.assertAlmostEqual(concurrence(rho), 0.0, places=12)
        self.assertAlmostEqual(negativity(rho, 2, [1]), 0.0, places=12)
        self.assertAlmostEqual(mutual_information(rho, 2, [0], [1]), 0.0, places=12)

    def test_partial_trace_keeps_qubit_zero_as_the_most_significant_bit(self) -> None:
        # |1> on q0, |0> on q1 is basis index 2 when q0 is the MSB.
        rho = np.zeros((4, 4), dtype=np.complex128)
        rho[2, 2] = 1.0
        self.assertAlmostEqual(partial_trace(rho, 2, [0])[1, 1].real, 1.0)
        self.assertAlmostEqual(partial_trace(rho, 2, [1])[0, 0].real, 1.0)

    def test_trace_distance_of_orthogonal_states_is_one(self) -> None:
        zero = np.diag([1.0, 0.0]).astype(np.complex128)
        one = np.diag([0.0, 1.0]).astype(np.complex128)
        self.assertAlmostEqual(trace_distance(zero, one), 1.0, places=12)


@unittest.skipIf(qutip is None, "QuTiP is not installed")
class QutipCrossCheckTests(unittest.TestCase):
    """Compare against QuTiP on mixed three-qubit states of several ranks."""

    def setUp(self) -> None:
        self.states = [_random_density(3, rank, seed) for seed, rank in enumerate((1, 2, 5, 8))]

    def _qobj(self, rho: np.ndarray, n_qubits: int):
        return qutip.Qobj(rho, dims=[[2] * n_qubits, [2] * n_qubits])

    def test_entropy_matches_qutip_entropy_vn(self) -> None:
        for rho in self.states:
            reference = qutip.entropy_vn(self._qobj(rho, 3).ptrace([0, 2]), base=2)
            self.assertAlmostEqual(von_neumann_entropy(partial_trace(rho, 3, [0, 2])), reference, places=10)

    def test_concurrence_matches_qutip(self) -> None:
        for rho in self.states:
            reduced = partial_trace(rho, 3, [1, 2])
            reference = qutip.concurrence(self._qobj(reduced, 2))
            # QuTiP takes square roots of a non-Hermitian spectrum; near-zero
            # eigenvalues turn 1e-16 round-off into ~1e-8 there.
            self.assertAlmostEqual(concurrence(reduced), reference, places=7)
        self.assertAlmostEqual(concurrence(_bell()), qutip.concurrence(self._qobj(_bell(), 2)), places=10)

    def test_negativity_matches_qutip_partial_transpose(self) -> None:
        for rho in self.states:
            for subsystem, mask in (([0], [1, 0, 0]), ([0, 2], [1, 0, 1])):
                transposed = qutip.partial_transpose(self._qobj(rho, 3), mask)
                reference = 0.5 * (np.sum(np.abs(transposed.eigenenergies())) - 1.0)
                self.assertAlmostEqual(negativity(rho, 3, subsystem), reference, places=10)

    def test_mutual_information_matches_qutip(self) -> None:
        for rho in self.states:
            reduced = self._qobj(rho, 3).ptrace([0, 1])
            reference = qutip.entropy_mutual(reduced, [0], [1], base=2)
            self.assertAlmostEqual(mutual_information(rho, 3, [0], [1]), reference, places=10)

    def test_trace_distance_matches_qutip(self) -> None:
        for left, right in zip(self.states, self.states[1:]):
            reference = qutip.tracedist(self._qobj(left, 3), self._qobj(right, 3))
            # QuTiP goes through a matrix square root, which costs ~1e-8.
            self.assertAlmostEqual(trace_distance(left, right), reference, places=7)

    def test_pauli_expectation_matches_qutip(self) -> None:
        operators = {"I": qutip.qeye(2), "X": qutip.sigmax(), "Y": qutip.sigmay(), "Z": qutip.sigmaz()}
        for rho in self.states:
            for pauli in ("XZY", "ZZI", "IYX"):
                reference = qutip.expect(qutip.tensor([operators[c] for c in pauli]), self._qobj(rho, 3))
                self.assertAlmostEqual(pauli_expectation(rho, pauli), reference, places=10)


class ToolboxSpecTests(unittest.TestCase):
    def test_rejects_metrics_already_fixed_by_the_display(self) -> None:
        with self.assertRaises(ValueError):
            validate_toolbox_spec(ToolboxSnapshotSpec(entropy_qubits=(0,)), 3)
        with self.assertRaises(ValueError):
            validate_toolbox_spec(ToolboxSnapshotSpec(observables=("ZII",)), 3)
        with self.assertRaises(ValueError):
            validate_toolbox_spec(ToolboxSnapshotSpec(observables=("III",)), 3)

    def test_rejects_malformed_subsets(self) -> None:
        bad = (
            ToolboxSnapshotSpec(concurrence_qubits=(0, 0)),
            ToolboxSnapshotSpec(negativity_subsystem=(0, 1, 2)),
            ToolboxSnapshotSpec(mutual_information_a=(0,), mutual_information_b=(0, 1)),
            ToolboxSnapshotSpec(mutual_information_a=(0,)),
            ToolboxSnapshotSpec(entropy_qubits=(0, 5)),
            ToolboxSnapshotSpec(observables=("ZZ",)),
        )
        for spec in bad:
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                validate_toolbox_spec(spec, 3)

    def test_normalizes_order_and_case(self) -> None:
        spec = validate_toolbox_spec(
            ToolboxSnapshotSpec(entropy_qubits=(2, 0), observables=("zzi", "ZZI")),
            3,
        )
        self.assertEqual(spec.entropy_qubits, (0, 2))
        self.assertEqual(spec.observables, ("ZZI",))


def _bell_request(toolbox: dict[str, object]) -> dict[str, object]:
    return {
        "simulation_backend": "python_dense",
        "input_mode": "physical",
        "circuit_config": {
            "logical_qubits": 2,
            "initial_states": ["0", "0"],
            "columns": [
                {"step": 0, "gates": [{"type": "H", "targets": [0]}]},
                {"step": 1, "gates": [{"type": "CNOT", "controls": [0], "targets": [1]}]},
            ],
        },
        "parameters": {
            "device_quality": 0.8,
            "temperature_mk": 15.0,
            "flux_noise_phi0": 1e-6,
            "qubit_frequency_ghz": 5.0,
            "t1_max_us": 20.0,
            "tphi_max_us": 20.0,
            "duration_us": 1.0,
            "time_steps": 21,
            "fidelity_threshold": 0.9,
        },
        "toolbox": toolbox,
    }


class ToolboxApiTests(unittest.TestCase):
    def test_simulate_returns_one_value_per_snapshot(self) -> None:
        status, response = asyncio.run(_asgi_post_json(app, "/api/simulate", _bell_request({
            "entropy_qubits": [0, 1],
            "concurrence_qubits": [0, 1],
            "negativity_subsystem": [0],
            "mutual_information_a": [0],
            "mutual_information_b": [1],
            "trace_distance": True,
            "observables": ["ZZ", "XX"],
        })))

        self.assertEqual(status, 200)
        toolbox = response["toolbox"]
        snapshots = response["state_snapshots"]
        self.assertEqual(toolbox["schema_version"], "toolbox-v1")
        self.assertEqual(len(toolbox["times_us"]), len(snapshots))
        for key in ("entropy", "concurrence", "negativity", "mutual_information", "trace_distance"):
            self.assertEqual(len(toolbox[key]["values"]), len(snapshots), key)
        self.assertEqual([entry["pauli"] for entry in toolbox["observables"]], ["ZZ", "XX"])
        # The initial snapshot is |00>: no entanglement and no distance from ideal.
        self.assertEqual(snapshots[0]["kind"], "initial")
        self.assertAlmostEqual(toolbox["concurrence"]["values"][0], 0.0, places=12)
        self.assertAlmostEqual(toolbox["trace_distance"]["values"][0], 0.0, places=12)
        self.assertAlmostEqual(toolbox["observables"][0]["values"][0], 1.0, places=12)
        # After the CNOT the state is a noisy Bell state.
        self.assertGreater(toolbox["concurrence"]["values"][-1], 0.5)

    def test_simulate_rejects_a_duplicate_metric(self) -> None:
        status, _ = asyncio.run(
            _asgi_post_json(app, "/api/simulate", _bell_request({"observables": ["ZI"]}))
        )
        self.assertEqual(status, 422)

    def test_simulate_without_toolbox_has_no_block(self) -> None:
        payload = _bell_request({})
        del payload["toolbox"]
        status, response = asyncio.run(_asgi_post_json(app, "/api/simulate", payload))
        self.assertEqual(status, 200)
        self.assertNotIn("toolbox", response)


if __name__ == "__main__":
    unittest.main()

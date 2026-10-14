"""Process characterization: QuTiP channel cross-check, 2-design check, estimator agreement."""

import asyncio
import math
import unittest

import numpy as np

from api.main import SimulateRequest, app, build_config_from_simulate_request
from core import process_characterization as process_module
from core.process_characterization import characterize_process, pauli_basis_labels
from core.results import SimulationConfig
from core.simulator import run_simulation
from core.state_metrics import PAULI_MATRICES
from tests.test_pulse_api_baseline_a import _asgi_post_json

try:
    import qutip
except ImportError:  # QuTiP is a validation-only dependency.
    qutip = None


def _request(n_qubits: int, columns: list[dict], *, t1_us: float = 5.0, method: str = "fixed_step_rk4") -> dict:
    return {
        "simulation_backend": "python_dense",
        "evolution_method": method,
        "input_mode": "physical",
        "circuit_config": {
            "logical_qubits": n_qubits,
            "initial_states": ["0"] * n_qubits,
            "columns": columns,
        },
        "parameters": {
            "device_quality": 0.8,
            "temperature_mk": 40.0,
            "flux_noise_phi0": 1e-6,
            "qubit_frequency_ghz": 5.0,
            "t1_max_us": t1_us,
            "tphi_max_us": t1_us,
            "duration_us": 2.0,
            "time_steps": 21,
            "fidelity_threshold": 0.9,
        },
    }


def _config(payload: dict) -> SimulationConfig:
    return build_config_from_simulate_request(SimulateRequest.model_validate(payload))


X_GATE = [{"step": 0, "gates": [{"type": "X", "targets": [0], "params": {"duration_us": 0.5}}]}]
BELL = [
    {"step": 0, "gates": [{"type": "H", "targets": [0]}]},
    {"step": 1, "gates": [{"type": "CNOT", "controls": [0], "targets": [1]}]},
]


def _local_rates(config: SimulationConfig) -> dict[str, float]:
    derived = run_simulation(config).derived_parameters
    return {name: float(derived[name]) for name in ("gamma_down_per_us", "gamma_up_per_us", "gamma_phi_per_us")}


@unittest.skipIf(qutip is None, "QuTiP is not installed")
class QutipChannelTests(unittest.TestCase):
    """Rebuild the single-qubit X-gate channel independently with QuTiP's propagator."""

    def _qutip_superoperator(self, config: SimulationConfig, duration_us: float):
        rates = _local_rates(config)
        x = qutip.sigmax()
        # The simulator's involution generator: H = pi / (2 tau) (I - X), exp(-i H tau) = X.
        hamiltonian = (math.pi / (2.0 * duration_us)) * (qutip.qeye(2) - x)
        lower = qutip.Qobj(np.array([[0.0, 1.0], [0.0, 0.0]]))
        c_ops = [
            math.sqrt(rates["gamma_down_per_us"]) * lower,
            math.sqrt(rates["gamma_up_per_us"]) * lower.dag(),
            math.sqrt(rates["gamma_phi_per_us"] / 2.0) * qutip.sigmaz(),
        ]
        return qutip.propagator(
            hamiltonian,
            duration_us,
            c_ops,
            options={"atol": 1e-12, "rtol": 1e-12},
        )

    def test_average_gate_fidelity_matches_qutip(self) -> None:
        # explicit_cptp exponentiates the generator, so it should agree to the
        # propagator's tolerance. Fixed-step RK4 carries the simulator's own
        # integration error (about 3e-5 here), which the looser bound admits.
        for method, tolerance in (("explicit_cptp", 1e-8), ("fixed_step_rk4", 1e-4)):
            with self.subTest(method=method):
                config = _config(_request(1, X_GATE, method=method))
                superoperator = self._qutip_superoperator(config, 0.5)
                reference = qutip.average_gate_fidelity(superoperator, target=qutip.sigmax())

                result = characterize_process(config)

                self.assertEqual(result["method"], "exact_tomography")
                self.assertLess(reference, 0.999)
                self.assertLess(abs(result["average_gate_fidelity"] - reference), tolerance)

    def test_chi_matrix_reproduces_the_qutip_channel(self) -> None:
        config = _config(_request(1, X_GATE, method="explicit_cptp"))
        superoperator = self._qutip_superoperator(config, 0.5)
        chi_data = characterize_process(config)["process_matrix"]
        chi = np.array(chi_data["noisy"]["real"]) + 1j * np.array(chi_data["noisy"]["imag"])
        paulis = [PAULI_MATRICES[label] for label in chi_data["basis"]]

        rng = np.random.default_rng(3)
        ket = rng.normal(size=2) + 1j * rng.normal(size=2)
        rho = np.outer(ket, ket.conj()) / np.vdot(ket, ket)
        expected = qutip.vector_to_operator(superoperator * qutip.operator_to_vector(qutip.Qobj(rho))).full()
        reconstructed = sum(
            chi[m, n] * paulis[m] @ rho @ paulis[n].conj().T
            for m in range(4)
            for n in range(4)
        )

        np.testing.assert_allclose(reconstructed, expected, atol=1e-8)
        self.assertAlmostEqual(chi_data["trace"], 1.0, places=10)
        self.assertGreater(chi_data["minimum_eigenvalue"], -1e-9)


class EstimatorTests(unittest.TestCase):
    def test_single_qubit_fidelity_equals_the_pauli_eigenstate_average(self) -> None:
        # The six Pauli eigenstates form a single-qubit 2-design, so their mean
        # state fidelity is F_avg without going through tomography inversion.
        config = _config(_request(1, X_GATE))
        channel = process_module._CircuitChannel(config, process_module.circuit_unitary(config))
        x = PAULI_MATRICES["X"]
        fidelities = []
        for label in ("0", "1", "+", "-", "+i", "-i"):
            ideal = x @ process_module._product_density((label,)) @ x
            fidelities.append(float(np.real(np.trace(ideal @ channel((label,))))))

        result = characterize_process(config)

        self.assertAlmostEqual(result["average_gate_fidelity"], float(np.mean(fidelities)), places=10)

    def test_noise_free_circuit_is_its_ideal_unitary(self) -> None:
        payload = _request(2, BELL)
        payload["parameters"].update({"device_quality": 1.0, "temperature_mk": 0.0, "flux_noise_phi0": 0.0})
        payload["parameters"]["ideal_reference"] = True
        result = characterize_process(_config(payload))
        self.assertAlmostEqual(result["average_gate_fidelity"], 1.0, places=10)
        noisy = np.array(result["process_matrix"]["noisy"]["real"])
        ideal = np.array(result["process_matrix"]["ideal"]["real"])
        np.testing.assert_allclose(noisy, ideal, atol=1e-10)

    def test_monte_carlo_agrees_with_exact_tomography(self) -> None:
        config = _config(_request(2, BELL))
        exact = characterize_process(config)["average_gate_fidelity"]
        original = process_module.EXACT_TOMOGRAPHY_MAX_QUBITS
        process_module.EXACT_TOMOGRAPHY_MAX_QUBITS = 1
        try:
            estimate = characterize_process(
                config,
                include_process_matrix=False,
                monte_carlo_samples=128,
                monte_carlo_seed=11,
            )
        finally:
            process_module.EXACT_TOMOGRAPHY_MAX_QUBITS = original

        self.assertEqual(estimate["method"], "monte_carlo")
        self.assertEqual(estimate["simulation_count"], 128)
        self.assertLess(abs(estimate["average_gate_fidelity"] - exact), 4.0 * estimate["standard_error"] + 1e-12)
        low, high = estimate["confidence_interval_95"]
        self.assertLessEqual(low, estimate["average_gate_fidelity"])
        self.assertGreaterEqual(high, estimate["average_gate_fidelity"])

    def test_pauli_basis_order(self) -> None:
        self.assertEqual(pauli_basis_labels(2)[:5], ["II", "IX", "IY", "IZ", "XI"])


class ProcessApiTests(unittest.TestCase):
    def test_endpoint_returns_fidelity_and_chi(self) -> None:
        status, response = asyncio.run(
            _asgi_post_json(app, "/api/analysis/process-characterization", _request(2, BELL))
        )
        self.assertEqual(status, 200)
        self.assertEqual(response["schema_version"], "process-characterization-v1")
        self.assertEqual(len(response["process_matrix"]["basis"]), 16)
        self.assertLess(response["average_gate_fidelity"], 1.0)

    def test_measurement_circuit_is_rejected(self) -> None:
        columns = [*BELL, {"step": 2, "gates": [{"type": "MEASURE", "targets": [0]}]}]
        status, _ = asyncio.run(
            _asgi_post_json(app, "/api/analysis/process-characterization", _request(2, columns))
        )
        self.assertEqual(status, 422)

    def test_chi_beyond_the_exact_limit_is_rejected(self) -> None:
        columns = [{"step": 0, "gates": [{"type": "H", "targets": [q]} for q in range(4)]}]
        status, _ = asyncio.run(
            _asgi_post_json(app, "/api/analysis/process-characterization", _request(4, columns))
        )
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()

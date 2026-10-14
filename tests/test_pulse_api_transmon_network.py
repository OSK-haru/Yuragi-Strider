import math
import unittest

import numpy as np
from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from api.main import pulse_simulate
from api.pulse_transmon_network_service import _build_rates
from api.pulse_models import (
    CoupledTransmonNetworkPulseSimulateRequest,
    CoupledTransmonNetworkPulseSimulateResponse,
    PulseApiRequest,
)
from core.capabilities import (
    DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL,
    SUPPORTED_PULSE_MODELS,
)
from core.gates import add, adjoint, matmul, subtract
from core.pulse_envelopes import GaussianPulseEnvelope, SquarePulseEnvelope
from core.pulse_evolution import (
    DenseCollapseDissipator,
    evolve_dense_time_dependent_segment,
    evolve_time_dependent_segment,
)
from core.pulse_qutrit_contract import NUMBER_QUTRIT
from core.pulse_qutrit_open_system import QutritDissipationRates
from core.pulse_transmon_network import (
    CoupledTransmonNetworkHamiltonian,
    ScheduledTransmonDrive,
    TransmonExchangeCoupling,
    embed_network_local_operator,
    network_collapse_operator_matrices,
    network_collapse_operators,
    network_initial_density_matrix,
    network_site_local_dissipator,
)


class CoupledTransmonNetworkApiTests(unittest.TestCase):
    def test_network_accepts_per_qubit_coherence_calibration(self) -> None:
        payload = _network_payload()
        payload["environment"] = {
            "input_mode": "physical",
            "device_quality": 1.0,
            "temperature_mk": 0.0,
            "flux_noise_phi0": 0.0,
            "qubit_frequency_ghz": 5.0,
            "t1_max_us": 100.0,
            "tphi_max_us": 100.0,
            "per_qubit": [
                {"t1_max_us": 10.0, "tphi_max_us": 20.0},
                {"t1_max_us": 100.0, "tphi_max_us": 200.0},
                {"t1_max_us": 1000.0, "tphi_max_us": 2000.0},
            ],
        }
        request = CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

        rates = _build_rates(request)

        self.assertGreater(rates[0].gamma_10_down_per_us, rates[1].gamma_10_down_per_us)
        self.assertGreater(rates[1].gamma_10_down_per_us, rates[2].gamma_10_down_per_us)
        self.assertGreater(
            rates[0].gamma_phi_adjacent_per_us,
            rates[1].gamma_phi_adjacent_per_us,
        )

    def test_network_rejects_per_qubit_coherence_count_mismatch(self) -> None:
        payload = _network_payload()
        payload["environment"] = {
            "input_mode": "physical",
            "device_quality": 1.0,
            "temperature_mk": 0.0,
            "flux_noise_phi0": 0.0,
            "qubit_frequency_ghz": 5.0,
            "t1_max_us": 100.0,
            "tphi_max_us": 100.0,
            "per_qubit": [{"t1_max_us": 10.0, "tphi_max_us": 20.0}],
        }

        with self.assertRaises(ValidationError):
            CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

    def test_network_is_discoverable_and_selected_by_discriminator(self) -> None:
        self.assertIn(
            DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL,
            SUPPORTED_PULSE_MODELS,
        )
        request = TypeAdapter(PulseApiRequest).validate_python(_network_payload())
        self.assertIsInstance(request, CoupledTransmonNetworkPulseSimulateRequest)

    def test_three_transmon_hamiltonian_is_hermitian_and_conserves_excitation(self) -> None:
        hamiltonian = CoupledTransmonNetworkHamiltonian(
            anharmonicities_rad_per_us=(-120.0, -130.0, -140.0),
            detunings_rad_per_us=(0.0, 5.0, -3.0),
            couplings=(
                TransmonExchangeCoupling(0, 1, 2.0),
                TransmonExchangeCoupling(1, 2, 3.0),
            ),
            drives=(
                ScheduledTransmonDrive(0, 0.0, SquarePulseEnvelope(0.0, 0.001)),
            ),
        ).evaluate(0.0005)
        hermitian = adjoint(hamiltonian)
        total_number = add(*(
            embed_network_local_operator(NUMBER_QUTRIT, index, 3)
            for index in range(3)
        ))
        commutator = subtract(
            matmul(hamiltonian, total_number),
            matmul(total_number, hamiltonian),
        )

        self.assertEqual(len(hamiltonian), 27)
        self.assertLess(
            max(
                abs(hamiltonian[row][column] - hermitian[row][column])
                for row in range(27)
                for column in range(27)
            ),
            1e-14,
        )
        self.assertLess(max(abs(value) for row in commutator for value in row), 1e-12)

    def test_three_transmon_api_returns_joint_density_matrix(self) -> None:
        request = CoupledTransmonNetworkPulseSimulateRequest.model_validate(
            _network_payload()
        )
        response = pulse_simulate(request)
        validated = CoupledTransmonNetworkPulseSimulateResponse.model_validate(response)

        self.assertEqual(validated.model["logical_qubits"], 3)
        self.assertEqual(validated.model["hilbert_dimension"], 27)
        self.assertEqual(len(validated.final.density_matrix), 27)
        self.assertEqual(len(validated.model["basis_order"]), 27)
        self.assertAlmostEqual(
            sum(validated.final.joint_populations.values()),
            1.0,
            places=10,
        )
        populations = validated.final.joint_populations
        q0_excited = sum(value for label, value in populations.items() if label[0] == "1")
        q2_excited = sum(value for label, value in populations.items() if label[2] == "1")
        self.assertGreater(q0_excited, 1e-4)
        self.assertGreater(q2_excited, 1e-4)

    def test_network_rejects_registers_above_four_transmons(self) -> None:
        payload = _network_payload()
        payload.update({
            "transmon_count": 5,
            "initial_state": "00000",
            "frequencies_ghz": [5.0] * 5,
            "anharmonicities_mhz": [-20.0] * 5,
            "detunings_rad_per_us": [0.0] * 5,
        })
        with self.assertRaises(ValidationError):
            CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

    def test_single_transmon_network_drives_the_computational_transition(self) -> None:
        request = CoupledTransmonNetworkPulseSimulateRequest.model_validate(
            _single_transmon_payload()
        )
        response = pulse_simulate(request)
        validated = CoupledTransmonNetworkPulseSimulateResponse.model_validate(response)

        self.assertEqual(validated.model["logical_qubits"], 1)
        self.assertEqual(validated.model["hilbert_dimension"], 3)
        self.assertEqual(len(validated.final.density_matrix), 3)
        self.assertEqual(validated.model["basis_order"], ["0", "1", "2"])
        self.assertAlmostEqual(
            sum(validated.final.joint_populations.values()),
            1.0,
            places=10,
        )
        # A pi pulse on the degenerate single-transmon network moves almost all
        # population into |1>, with only a little leakage into |2>.
        self.assertGreater(validated.final.joint_populations["1"], 0.9)
        self.assertLess(validated.final.joint_populations["2"], 0.05)

    def test_single_transmon_network_rejects_exchange_couplings(self) -> None:
        payload = _single_transmon_payload()
        payload["couplings"] = [
            {"left": 0, "right": 1, "exchange_coupling_rad_per_us": 1.0}
        ]
        with self.assertRaises(ValidationError):
            CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

    def test_two_level_network_has_no_leakage_state(self) -> None:
        payload = _single_transmon_payload()
        payload["local_levels"] = 2
        request = CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)
        response = pulse_simulate(request)
        validated = CoupledTransmonNetworkPulseSimulateResponse.model_validate(response)

        self.assertEqual(validated.model["local_levels"], 2)
        self.assertEqual(validated.model["hilbert_dimension"], 2)
        self.assertEqual(validated.model["basis_order"], ["0", "1"])
        self.assertEqual(validated.model["subsystem_dimensions"], [2])
        self.assertEqual(len(validated.final.density_matrix), 2)
        # No |2> state exists, so a pi pulse is an exact inversion and the
        # leakage channel is identically zero.
        self.assertAlmostEqual(validated.final.joint_populations["1"], 1.0, places=3)
        self.assertAlmostEqual(validated.final.leakage_probability, 0.0, places=12)

    def test_two_level_pair_transfers_population_through_exchange(self) -> None:
        payload = _network_payload()
        payload.update({
            "local_levels": 2,
            "transmon_count": 2,
            "initial_state": "00",
            "frequencies_ghz": [5.0, 5.1],
            "anharmonicities_mhz": [-320.0, -320.0],
            "detunings_rad_per_us": [0.0, 0.0],
            "couplings": [
                {"left": 0, "right": 1, "exchange_coupling_rad_per_us": 5.0}
            ],
            "drives": [
                {
                    "target": 0,
                    "start_time_us": 0.0,
                    "pulse": {
                        "shape": "square",
                        "amplitude_mode": "target_rotation_angle",
                        "target_rotation_angle_rad": math.pi,
                        "pulse_duration_us": 0.02,
                        "phase_rad": 0.0,
                        "detuning_rad_per_us": 0.0,
                        "drag_beta_us": 0.0,
                    },
                }
            ],
            "total_simulation_time_us": 0.05,
            "snapshot_options": {"uniform_count": 6, "custom_times_us": []},
        })
        request = CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)
        response = pulse_simulate(request)
        validated = CoupledTransmonNetworkPulseSimulateResponse.model_validate(response)

        self.assertEqual(validated.model["hilbert_dimension"], 4)
        self.assertEqual(
            sorted(validated.final.joint_populations),
            ["00", "01", "10", "11"],
        )
        self.assertAlmostEqual(
            sum(validated.final.joint_populations.values()), 1.0, places=10
        )
        # The drive inverts q0 and the exchange edge leaks some of that into q1.
        self.assertGreater(validated.final.joint_populations["01"], 1e-3)

    def test_two_level_network_still_requires_negative_anharmonicity(self) -> None:
        payload = _single_transmon_payload()
        payload["local_levels"] = 2
        payload["anharmonicities_mhz"] = [0.0]
        with self.assertRaises(ValidationError):
            CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

    def test_explicit_cptp_matches_rk4_and_reports_an_audit(self) -> None:
        rk4_payload = _network_payload()
        rk4_payload.update({
            "transmon_count": 2,
            "initial_state": "00",
            "frequencies_ghz": [5.0, 5.1],
            "anharmonicities_mhz": [-100.0, -100.0],
            "detunings_rad_per_us": [0.0, 0.0],
            "couplings": [
                {"left": 0, "right": 1, "exchange_coupling_rad_per_us": 5.0}
            ],
            "drives": [{
                "target": 0,
                "start_time_us": 0.0,
                "pulse": {
                    "shape": "gaussian",
                    "amplitude_mode": "target_rotation_angle",
                    "target_rotation_angle_rad": 0.7 * math.pi,
                    "sigma_us": 0.002,
                    "truncation_sigma": 4.0,
                    "phase_rad": 0.0,
                    "detuning_rad_per_us": 0.0,
                    "drag_beta_us": 0.0,
                },
            }],
            "total_simulation_time_us": 0.02,
            "environment": {
                "input_mode": "direct_rates",
                "gamma_10_down_per_us": 0.05,
                "gamma_01_up_per_us": 0.0,
                "gamma_21_down_per_us": 0.0,
                "gamma_12_up_per_us": 0.0,
                "gamma_phi_adjacent_per_us": 0.0,
            },
            "snapshot_options": {"uniform_count": 4, "custom_times_us": []},
        })
        cptp_payload = dict(rk4_payload, evolution_method="explicit_cptp")

        rk4 = CoupledTransmonNetworkPulseSimulateResponse.model_validate(
            pulse_simulate(
                CoupledTransmonNetworkPulseSimulateRequest.model_validate(rk4_payload)
            )
        )
        cptp = CoupledTransmonNetworkPulseSimulateResponse.model_validate(
            pulse_simulate(
                CoupledTransmonNetworkPulseSimulateRequest.model_validate(cptp_payload)
            )
        )

        audit = cptp.diagnostics["evolution"]["open_pulse_audit"]
        self.assertIsNotNone(audit)
        self.assertTrue(audit["all_maps_cptp"])
        self.assertTrue(
            cptp.diagnostics["evolution"]["cptp_guaranteed_by_construction"]
        )
        self.assertFalse(cptp.diagnostics["evolution"]["cleanup_applied"])
        for label, rk4_value in rk4.final.joint_populations.items():
            self.assertAlmostEqual(
                cptp.final.joint_populations[label], rk4_value, places=4
            )

    def test_explicit_cptp_is_rejected_above_hilbert_dimension_nine(self) -> None:
        payload = _network_payload()
        payload.update({
            "evolution_method": "explicit_cptp",
            "transmon_count": 3,
            "initial_state": "000",
            "frequencies_ghz": [5.0, 5.1, 4.9],
            "anharmonicities_mhz": [-320.0, -320.0, -320.0],
            "detunings_rad_per_us": [0.0, 0.0, 0.0],
        })
        with self.assertRaises(ValidationError):
            CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

    def test_correlated_quasi_static_ensemble_preserves_trace(self) -> None:
        payload = _network_payload()
        payload.update({
            "transmon_count": 2,
            "initial_state": "00",
            "frequencies_ghz": [5.0, 5.1],
            "anharmonicities_mhz": [-320.0, -320.0],
            "detunings_rad_per_us": [0.0, 0.0],
            "couplings": [
                {"left": 0, "right": 1, "exchange_coupling_rad_per_us": 5.0}
            ],
            "drives": [{
                "target": 0,
                "start_time_us": 0.0,
                "pulse": {
                    "shape": "square",
                    "amplitude_mode": "target_rotation_angle",
                    "target_rotation_angle_rad": math.pi,
                    "pulse_duration_us": 0.02,
                    "phase_rad": 0.0,
                    "detuning_rad_per_us": 0.0,
                    "drag_beta_us": 0.0,
                },
            }],
            "quasi_static_detuning_sigmas_rad_per_us": [3.0, 3.0],
            "quasi_static_detuning_adjacent_correlation": 0.5,
            "quasi_static_quadrature_order": 3,
            "total_simulation_time_us": 0.05,
            "snapshot_options": {"uniform_count": 4, "custom_times_us": []},
        })
        request = CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)
        response = pulse_simulate(request)
        validated = CoupledTransmonNetworkPulseSimulateResponse.model_validate(response)

        noise = validated.diagnostics["quasi_static_noise"]
        self.assertTrue(noise["enabled"])
        self.assertEqual(noise["sample_count"], 9)
        self.assertEqual(noise["adjacent_correlation"], 0.5)
        self.assertAlmostEqual(
            sum(validated.final.joint_populations.values()), 1.0, places=10
        )
        self.assertAlmostEqual(validated.final.population_sum_error, 0.0, places=9)

    def test_quasi_static_sigma_length_must_match_register(self) -> None:
        payload = _network_payload()
        payload["quasi_static_detuning_sigmas_rad_per_us"] = [1.0]
        with self.assertRaises(ValidationError):
            CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

    def test_four_transmon_response_budget_is_checked_before_evolution(self) -> None:
        payload = _network_payload()
        payload.update({
            "transmon_count": 4,
            "initial_state": "0000",
            "frequencies_ghz": [5.0] * 4,
            "anharmonicities_mhz": [-20.0] * 4,
            "detunings_rad_per_us": [0.0] * 4,
            "snapshot_options": {"uniform_count": 101, "custom_times_us": []},
        })
        request = CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

        with self.assertRaises(HTTPException) as raised:
            pulse_simulate(request)

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("density-matrix element limit", str(raised.exception.detail))

    def test_four_transmon_dense_work_budget_is_checked_before_evolution(self) -> None:
        payload = _four_transmon_payload()
        payload.update({
            "anharmonicities_mhz": [-200.0] * 4,
            "total_simulation_time_us": 0.2,
        })
        request = CoupledTransmonNetworkPulseSimulateRequest.model_validate(payload)

        with self.assertRaises(HTTPException) as raised:
            pulse_simulate(request)

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("dimension-aware dense-work limit", str(raised.exception.detail))

    def test_four_transmon_register_runs_a_driven_layer(self) -> None:
        request = CoupledTransmonNetworkPulseSimulateRequest.model_validate(
            _four_transmon_payload()
        )
        response = pulse_simulate(request)
        validated = CoupledTransmonNetworkPulseSimulateResponse.model_validate(response)

        self.assertEqual(validated.model["logical_qubits"], 4)
        self.assertEqual(validated.model["hilbert_dimension"], 81)
        self.assertEqual(len(validated.final.density_matrix), 81)
        self.assertAlmostEqual(
            sum(validated.final.joint_populations.values()),
            1.0,
            places=10,
        )
        self.assertLess(validated.final.population_sum_error, 1e-9)
        # Cleanup restores trace and Hermiticity but not positivity, so the
        # network path only claims eigenvalues at the RK4 truncation level.
        self.assertGreater(
            validated.final.cleaned_physicality.minimum_eigenvalue,
            -1e-6,
        )
        populations = validated.final.joint_populations
        for transmon in range(4):
            excited = sum(
                value
                for label, value in populations.items()
                if label[transmon] == "1"
            )
            self.assertGreater(excited, 1e-3)


class CoupledTransmonNetworkDenseKernelTests(unittest.TestCase):
    """Lock the fast network kernel to the reference dense formulation."""

    def test_site_local_dissipator_matches_dense_collapse_operators(self) -> None:
        for count in (2, 3, 4):
            with self.subTest(transmon_count=count):
                rates = _rates(count)
                dimension = 3 ** count
                state = _hermitian_probe_state(dimension)
                dense = DenseCollapseDissipator(
                    network_collapse_operator_matrices(rates)
                )
                local = network_site_local_dissipator(rates)

                self.assertLess(
                    float(np.max(np.abs(
                        dense.relaxation_array(dimension)
                        - local.relaxation_array(dimension)
                    ))),
                    1e-12,
                )
                self.assertLess(
                    float(np.max(np.abs(
                        dense.apply_jumps(state) - local.apply_jumps(state)
                    ))),
                    1e-12,
                )

    def test_dense_segment_matches_the_tuple_evolution_path(self) -> None:
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(
            math.pi / 2,
            0.001,
            3.0,
        )
        hamiltonian = CoupledTransmonNetworkHamiltonian(
            anharmonicities_rad_per_us=(-628.3, -640.0),
            detunings_rad_per_us=(0.0, 3.0),
            couplings=(TransmonExchangeCoupling(0, 1, 5.0),),
            drives=(
                ScheduledTransmonDrive(0, 0.0, envelope),
                ScheduledTransmonDrive(1, 0.0, envelope, math.pi / 2),
            ),
        )
        rates = _rates(2)
        initial = network_initial_density_matrix("00", 2)
        checkpoints = [0.003, 0.006]

        reference = evolve_time_dependent_segment(
            initial,
            hamiltonian,
            network_collapse_operators(rates),
            0.006,
            1e-4,
            checkpoint_times_us=checkpoints,
            backend="python",
        )
        dense = evolve_dense_time_dependent_segment(
            initial,
            hamiltonian,
            network_site_local_dissipator(rates),
            0.006,
            1e-4,
            checkpoint_times_us=checkpoints,
        )

        self.assertEqual(
            dense.diagnostics.internal_step_count,
            reference.diagnostics.internal_step_count,
        )
        self.assertEqual(len(dense.checkpoints), len(reference.checkpoints))
        self.assertLess(
            float(np.max(np.abs(
                np.asarray(dense.state) - np.asarray(reference.state)
            ))),
            1e-12,
        )
        for expected, actual in zip(
            reference.checkpoints,
            dense.checkpoints,
            strict=True,
        ):
            self.assertAlmostEqual(expected.time_us, actual.time_us, places=12)
            self.assertLess(
                float(np.max(np.abs(
                    np.asarray(expected.cleaned_state)
                    - np.asarray(actual.cleaned_state)
                ))),
                1e-12,
            )


def _rates(transmon_count: int) -> tuple[QutritDissipationRates, ...]:
    return tuple(
        QutritDissipationRates(
            input_mode="direct_rates",
            gamma_10_down_per_us=0.2 + 0.05 * index,
            gamma_01_up_per_us=0.02,
            gamma_21_down_per_us=0.4,
            gamma_12_up_per_us=0.03,
            gamma_phi_adjacent_per_us=0.08,
        )
        for index in range(transmon_count)
    )


def _hermitian_probe_state(dimension: int) -> np.ndarray:
    generator = np.random.default_rng(20260816)
    root = (
        generator.normal(size=(dimension, dimension))
        + 1j * generator.normal(size=(dimension, dimension))
    )
    state = root @ root.conj().T
    return state / np.trace(state)


def _four_transmon_payload() -> dict[str, object]:
    """Return one Gaussian half-pi layer across a four-transmon chain."""

    pulse = {
        "shape": "gaussian",
        "amplitude_mode": "target_rotation_angle",
        "target_rotation_angle_rad": math.pi / 2,
        "sigma_us": 0.001,
        "truncation_sigma": 3.0,
        "phase_rad": 0.0,
        "detuning_rad_per_us": 0.0,
        "drag_beta_us": 0.0,
    }
    payload = _network_payload()
    payload.update({
        "transmon_count": 4,
        "initial_state": "0000",
        "frequencies_ghz": [5.0, 5.1, 4.9, 5.05],
        "anharmonicities_mhz": [-100.0] * 4,
        "detunings_rad_per_us": [0.0] * 4,
        "couplings": [
            {"left": left, "right": left + 1, "exchange_coupling_rad_per_us": 5.0}
            for left in range(3)
        ],
        "drives": [
            {"target": target, "start_time_us": 0.0, "pulse": pulse}
            for target in range(4)
        ],
        "total_simulation_time_us": 0.006,
        "snapshot_options": {"uniform_count": 2, "custom_times_us": []},
    })
    return payload


def _network_payload() -> dict[str, object]:
    pulse = {
        "shape": "square",
        "amplitude_mode": "target_rotation_angle",
        "target_rotation_angle_rad": math.pi / 20,
        "pulse_duration_us": 0.001,
        "phase_rad": 0.0,
        "detuning_rad_per_us": 0.0,
        "drag_beta_us": 0.0,
    }
    return {
        "model_id": DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL,
        "transmon_count": 3,
        "initial_state": "000",
        "frequencies_ghz": [5.0, 5.1, 4.9],
        "anharmonicities_mhz": [-20.0, -22.0, -24.0],
        "detunings_rad_per_us": [0.0, 0.0, 0.0],
        "couplings": [
            {"left": 0, "right": 1, "exchange_coupling_rad_per_us": 1.0},
            {"left": 1, "right": 2, "exchange_coupling_rad_per_us": 1.5},
        ],
        "drives": [
            {"target": 0, "start_time_us": 0.0, "pulse": pulse},
            {
                "target": 2,
                "start_time_us": 0.0,
                "pulse": {**pulse, "phase_rad": math.pi / 2},
            },
        ],
        "total_simulation_time_us": 0.001,
        "backend": "python",
        "evolution_method": "fixed_step_rk4",
        "environment": {
            "input_mode": "direct_rates",
            "gamma_10_down_per_us": 0.0,
            "gamma_01_up_per_us": 0.0,
            "gamma_21_down_per_us": 0.0,
            "gamma_12_up_per_us": 0.0,
            "gamma_phi_adjacent_per_us": 0.0,
        },
        "snapshot_options": {"uniform_count": 3, "custom_times_us": []},
    }


def _single_transmon_payload() -> dict[str, object]:
    """Return a pi pulse on the degenerate one-transmon network."""

    pulse = {
        "shape": "square",
        "amplitude_mode": "target_rotation_angle",
        "target_rotation_angle_rad": math.pi,
        "pulse_duration_us": 0.02,
        "phase_rad": 0.0,
        "detuning_rad_per_us": 0.0,
        "drag_beta_us": 0.0,
    }
    return {
        "model_id": DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL,
        "transmon_count": 1,
        "initial_state": "0",
        "frequencies_ghz": [5.0],
        "anharmonicities_mhz": [-320.0],
        "detunings_rad_per_us": [0.0],
        "couplings": [],
        "drives": [
            {"target": 0, "start_time_us": 0.0, "pulse": pulse},
        ],
        "total_simulation_time_us": 0.05,
        "backend": "python",
        "evolution_method": "fixed_step_rk4",
        "environment": {
            "input_mode": "direct_rates",
            "gamma_10_down_per_us": 0.0,
            "gamma_01_up_per_us": 0.0,
            "gamma_21_down_per_us": 0.0,
            "gamma_12_up_per_us": 0.0,
            "gamma_phi_adjacent_per_us": 0.0,
        },
        "snapshot_options": {"uniform_count": 6, "custom_times_us": []},
    }


if __name__ == "__main__":
    unittest.main()

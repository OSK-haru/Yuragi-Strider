import math
import unittest
from types import SimpleNamespace

from core.constants import BOLTZMANN_CONSTANT, PLANCK_CONSTANT
from core.gates import density_from_ket
from core.pulse_envelopes import GaussianPulseEnvelope, SquarePulseEnvelope
from core.pulse_qutrit import (
    evolve_closed_qutrit_sequence,
    qutrit_initial_density_matrix,
)
from core.pulse_qutrit_contract import (
    transmon_anharmonicity_rad_per_us,
)
from core.pulse_qutrit_open_system import (
    QUTRIT_DEPHASING_MODEL,
    QutritDissipationRates,
    evolve_open_qutrit_sequence,
    qutrit_collapse_operator_matrices,
    qutrit_dissipation_rates,
    qutrit_gibbs_populations,
)
from tests.physics_test_helpers import assert_matrix_close


STATE_TOLERANCE = 2e-7
PHYSICALITY_TOLERANCE = 1e-10


class PulseB2QutritRateTests(unittest.TestCase):
    def test_zero_temperature_upward_rates_vanish(self) -> None:
        rates = qutrit_dissipation_rates(
            _physical_environment(temperature_mk=0.0),
            -250.0,
        )

        self.assertEqual(rates.gamma_01_up_per_us, 0.0)
        self.assertEqual(rates.gamma_12_up_per_us, 0.0)
        self.assertEqual(rates.n_01, 0.0)
        self.assertEqual(rates.n_12, 0.0)
        self.assertAlmostEqual(
            rates.gamma_21_zero_temperature_per_us,
            2.0 * rates.gamma_10_zero_temperature_per_us,
        )

    def test_both_transitions_satisfy_detailed_balance(self) -> None:
        temperature_mk = 120.0
        rates = qutrit_dissipation_rates(
            _physical_environment(temperature_mk=temperature_mk),
            -250.0,
        )
        assert rates.transition_01_frequency_ghz is not None
        assert rates.transition_12_frequency_ghz is not None

        expected_01 = _boltzmann_factor(
            temperature_mk,
            rates.transition_01_frequency_ghz,
        )
        expected_12 = _boltzmann_factor(
            temperature_mk,
            rates.transition_12_frequency_ghz,
        )
        self.assertAlmostEqual(
            rates.gamma_01_up_per_us / rates.gamma_10_down_per_us,
            expected_01,
            delta=1e-14,
        )
        self.assertAlmostEqual(
            rates.gamma_12_up_per_us / rates.gamma_21_down_per_us,
            expected_12,
            delta=1e-14,
        )

    def test_direct_rate_collapse_operators_have_frozen_coefficients(
        self,
    ) -> None:
        rates = QutritDissipationRates(
            "direct_rates",
            gamma_10_down_per_us=0.25,
            gamma_01_up_per_us=0.36,
            gamma_21_down_per_us=0.49,
            gamma_12_up_per_us=0.64,
            gamma_phi_adjacent_per_us=0.5,
        )
        operators = qutrit_collapse_operator_matrices(rates)

        self.assertEqual(len(operators), 5)
        self.assertAlmostEqual(operators[0][0][1], 0.5)
        self.assertAlmostEqual(operators[1][1][0], 0.6)
        self.assertAlmostEqual(operators[2][1][2], 0.7)
        self.assertAlmostEqual(operators[3][2][1], 0.8)
        self.assertAlmostEqual(operators[4][1][1], 1.0)
        self.assertAlmostEqual(operators[4][2][2], 2.0)
        self.assertEqual(rates.dephasing_model, QUTRIT_DEPHASING_MODEL)

    def test_invalid_rate_mode_and_values_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "physical or direct_rates"):
            qutrit_dissipation_rates(
                SimpleNamespace(input_mode="normalized"),
                -250.0,
            )
        with self.assertRaisesRegex(ValueError, "non-negative"):
            QutritDissipationRates(
                "direct_rates",
                -0.1,
                0.0,
                0.0,
                0.0,
                0.0,
            )


class PulseB2QutritEvolutionTests(unittest.TestCase):
    def test_zero_rate_limit_matches_closed_qutrit_path(self) -> None:
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(
            math.pi / 2.0,
            sigma_us=0.01,
            truncation_sigma=4.0,
        )
        alpha = transmon_anharmonicity_rad_per_us(-250.0)
        initial = qutrit_initial_density_matrix("0")
        zero_rates = _direct_rates()
        times = _uniform_times(envelope.duration_us, 21)

        open_result = evolve_open_qutrit_sequence(
            initial,
            envelope,
            alpha,
            zero_rates,
            envelope.duration_us,
            5e-6,
            pulse_checkpoint_times_us=times,
        )
        closed_result = evolve_closed_qutrit_sequence(
            initial,
            envelope,
            alpha,
            envelope.duration_us,
            5e-6,
            pulse_checkpoint_times_us=times,
        )

        assert_matrix_close(open_result.final_state,
            closed_result.final_state,
            1e-14,
        )

    def test_zero_temperature_two_to_one_to_zero_cascade(self) -> None:
        gamma_10 = 0.3
        gamma_21 = 0.8
        duration = 3.0
        result = evolve_open_qutrit_sequence(
            qutrit_initial_density_matrix("2"),
            SquarePulseEnvelope(0.0, duration),
            -1.0,
            _direct_rates(
                gamma_10_down_per_us=gamma_10,
                gamma_21_down_per_us=gamma_21,
            ),
            duration,
            0.002,
        )

        expected_2 = math.exp(-gamma_21 * duration)
        expected_1 = gamma_21 / (gamma_10 - gamma_21) * (
            math.exp(-gamma_21 * duration)
            - math.exp(-gamma_10 * duration)
        )
        expected_0 = 1.0 - expected_1 - expected_2
        self.assertAlmostEqual(
            result.final_state[0][0].real,
            expected_0,
            delta=STATE_TOLERANCE,
        )
        self.assertAlmostEqual(
            result.final_state[1][1].real,
            expected_1,
            delta=STATE_TOLERANCE,
        )
        self.assertAlmostEqual(
            result.final_state[2][2].real,
            expected_2,
            delta=STATE_TOLERANCE,
        )

    def test_number_noise_dephasing_has_one_one_four_rate_ratio(self) -> None:
        inverse_sqrt_three = 1.0 / math.sqrt(3.0)
        initial = density_from_ket((
            inverse_sqrt_three,
            inverse_sqrt_three,
            inverse_sqrt_three,
        ))
        gamma_phi = 0.7
        duration = 0.5
        result = evolve_open_qutrit_sequence(
            initial,
            SquarePulseEnvelope(0.0, duration),
            -1.0,
            _direct_rates(gamma_phi_adjacent_per_us=gamma_phi),
            duration,
            0.001,
        )

        for row, column, multiplier in (
            (0, 1, 1.0),
            (1, 2, 1.0),
            (0, 2, 4.0),
        ):
            expected_magnitude = (
                abs(initial[row][column])
                * math.exp(-multiplier * gamma_phi * duration)
            )
            self.assertAlmostEqual(
                abs(result.final_state[row][column]),
                expected_magnitude,
                delta=STATE_TOLERANCE,
            )
        for level in range(3):
            self.assertAlmostEqual(
                result.final_state[level][level].real,
                1.0 / 3.0,
                delta=STATE_TOLERANCE,
            )

    def test_population_outflow_sets_coherence_decay(self) -> None:
        inverse_sqrt_two = 1.0 / math.sqrt(2.0)
        initial = density_from_ket((
            inverse_sqrt_two,
            inverse_sqrt_two,
            0.0 + 0.0j,
        ))
        rates = _direct_rates(
            gamma_10_down_per_us=0.2,
            gamma_01_up_per_us=0.1,
            gamma_21_down_per_us=0.4,
            gamma_12_up_per_us=0.3,
        )
        duration = 0.7
        result = evolve_open_qutrit_sequence(
            initial,
            SquarePulseEnvelope(0.0, duration),
            -1.0,
            rates,
            duration,
            0.001,
        )

        decay_rate = rates.population_induced_coherence_decay_per_us(0, 1)
        self.assertAlmostEqual(decay_rate, 0.3)
        self.assertAlmostEqual(
            abs(result.final_state[0][1]),
            abs(initial[0][1]) * math.exp(-decay_rate * duration),
            delta=STATE_TOLERANCE,
        )

    def test_long_time_physical_rates_approach_qutrit_gibbs_state(
        self,
    ) -> None:
        temperature_mk = 300.0
        environment = _physical_environment(
            temperature_mk=temperature_mk,
            device_quality=0.0,
        )
        rates = qutrit_dissipation_rates(environment, -250.0)
        assert rates.transition_01_frequency_ghz is not None
        assert rates.transition_12_frequency_ghz is not None
        expected = qutrit_gibbs_populations(
            temperature_mk,
            rates.transition_01_frequency_ghz,
            rates.transition_12_frequency_ghz,
        )
        duration = 12.0
        result = evolve_open_qutrit_sequence(
            qutrit_initial_density_matrix("2"),
            SquarePulseEnvelope(0.0, 0.01),
            transmon_anharmonicity_rad_per_us(-250.0),
            rates,
            duration,
            0.005,
        )

        for level, expected_population in enumerate(expected):
            self.assertAlmostEqual(
                result.final_state[level][level].real,
                expected_population,
                delta=2e-6,
            )

    def test_physical_and_equivalent_direct_rates_match(self) -> None:
        environment = _physical_environment(temperature_mk=100.0)
        physical_rates = qutrit_dissipation_rates(environment, -250.0)
        direct_rates = _direct_rates(
            gamma_10_down_per_us=physical_rates.gamma_10_down_per_us,
            gamma_01_up_per_us=physical_rates.gamma_01_up_per_us,
            gamma_21_down_per_us=physical_rates.gamma_21_down_per_us,
            gamma_12_up_per_us=physical_rates.gamma_12_up_per_us,
            gamma_phi_adjacent_per_us=(
                physical_rates.gamma_phi_adjacent_per_us
            ),
        )
        envelope = SquarePulseEnvelope(0.0, 0.05)
        alpha = transmon_anharmonicity_rad_per_us(-250.0)
        initial = qutrit_initial_density_matrix("2")
        physical = evolve_open_qutrit_sequence(
            initial,
            envelope,
            alpha,
            physical_rates,
            0.5,
            0.001,
        )
        direct = evolve_open_qutrit_sequence(
            initial,
            envelope,
            alpha,
            direct_rates,
            0.5,
            0.001,
        )

        assert_matrix_close(physical.final_state,
            direct.final_state,
            1e-14,
        )

    def test_dissipation_acts_during_pulse_and_idle(self) -> None:
        envelope = SquarePulseEnvelope.from_target_rotation_angle(
            math.pi / 2.0,
            0.2,
        )
        rates = _direct_rates(
            gamma_10_down_per_us=0.4,
            gamma_01_up_per_us=0.2,
            gamma_21_down_per_us=0.8,
            gamma_12_up_per_us=0.1,
        )
        result = evolve_open_qutrit_sequence(
            qutrit_initial_density_matrix("0"),
            envelope,
            -1.0,
            rates,
            1.0,
            0.001,
            pulse_checkpoint_times_us=(0.0, 0.2),
            idle_checkpoint_times_us=(0.0, 0.8),
        )
        closed = evolve_closed_qutrit_sequence(
            qutrit_initial_density_matrix("0"),
            envelope,
            -1.0,
            1.0,
            0.001,
        )

        self.assertGreater(
            _matrix_max_error(result.pulse_end_state, closed.pulse_end_state),
            1e-3,
        )
        self.assertGreater(
            _matrix_max_error(result.final_state, result.pulse_end_state),
            1e-3,
        )
        self.assertIsNotNone(result.idle_result)

    def test_raw_physicality_remains_small(self) -> None:
        result = evolve_open_qutrit_sequence(
            qutrit_initial_density_matrix("1"),
            SquarePulseEnvelope(1.2, 0.4),
            -2.0,
            _direct_rates(
                gamma_10_down_per_us=0.5,
                gamma_01_up_per_us=0.2,
                gamma_21_down_per_us=0.8,
                gamma_12_up_per_us=0.1,
                gamma_phi_adjacent_per_us=0.3,
            ),
            0.8,
            0.001,
        )
        diagnostics = [result.pulse_result.diagnostics]
        assert result.idle_result is not None
        diagnostics.append(result.idle_result.diagnostics)

        for item in diagnostics:
            self.assertLessEqual(
                item.raw_trace_error,
                PHYSICALITY_TOLERANCE,
            )
            self.assertLessEqual(
                item.raw_hermiticity_error,
                PHYSICALITY_TOLERANCE,
            )
            self.assertGreaterEqual(
                item.raw_minimum_eigenvalue,
                -PHYSICALITY_TOLERANCE,
            )
            self.assertLessEqual(
                item.cleanup_correction_norm,
                PHYSICALITY_TOLERANCE,
            )


def _physical_environment(
    *,
    temperature_mk: float,
    device_quality: float = 0.8,
) -> SimpleNamespace:
    return SimpleNamespace(
        input_mode="physical",
        device_quality=device_quality,
        temperature_mk=temperature_mk,
        flux_noise_phi0=1e-6,
        qubit_frequency_ghz=5.0,
        t1_max_us=100.0,
        tphi_max_us=100.0,
        ideal_reference=False,
    )


def _direct_rates(
    *,
    gamma_10_down_per_us: float = 0.0,
    gamma_01_up_per_us: float = 0.0,
    gamma_21_down_per_us: float = 0.0,
    gamma_12_up_per_us: float = 0.0,
    gamma_phi_adjacent_per_us: float = 0.0,
) -> QutritDissipationRates:
    return QutritDissipationRates(
        "direct_rates",
        gamma_10_down_per_us,
        gamma_01_up_per_us,
        gamma_21_down_per_us,
        gamma_12_up_per_us,
        gamma_phi_adjacent_per_us,
    )


def _uniform_times(duration_us: float, count: int) -> tuple[float, ...]:
    return tuple(
        duration_us * index / (count - 1)
        for index in range(count)
    )


def _boltzmann_factor(
    temperature_mk: float,
    frequency_ghz: float,
) -> float:
    return math.exp(
        -PLANCK_CONSTANT * frequency_ghz * 1e9
        / (BOLTZMANN_CONSTANT * temperature_mk * 1e-3)
    )


def _matrix_max_error(actual, expected) -> float:
    return max(
        abs(actual[row][column] - expected[row][column])
        for row in range(len(expected))
        for column in range(len(expected))
    )


if __name__ == "__main__":
    unittest.main()

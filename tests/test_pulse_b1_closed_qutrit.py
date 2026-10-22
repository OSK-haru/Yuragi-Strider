import cmath
import math
import unittest

from core.gates import density_from_ket
from core.pulse_contract import SIGMA_X
from core.pulse_envelopes import (
    GaussianPulseEnvelope,
    SquarePulseEnvelope,
    TwoLevelPulseHamiltonian,
)
from core.pulse_evolution import evolve_time_dependent_segment
from core.pulse_qutrit import (
    QutritPulseHamiltonian,
    evolve_closed_qutrit_sequence,
    qutrit_initial_density_matrix,
    qutrit_populations,
)
from core.pulse_qutrit_contract import (
    qutrit_rotating_frame_hamiltonian,
    transmon_anharmonicity_rad_per_us,
)
from tests.physics_test_helpers import assert_matrix_close


STATE_TOLERANCE = 2e-7
PHYSICALITY_TOLERANCE = 1e-10


class PulseB1ClosedQutritTests(unittest.TestCase):
    def test_generic_solver_accepts_three_dimensional_state(self) -> None:
        initial = qutrit_initial_density_matrix("0")
        envelope = SquarePulseEnvelope(0.0, 0.01)
        result = evolve_time_dependent_segment(
            initial,
            QutritPulseHamiltonian(
                envelope,
                transmon_anharmonicity_rad_per_us(-250.0),
            ),
            (),
            duration_us=envelope.duration_us,
            max_step_us=1e-4,
        )
        assert_matrix_close(result.state, initial, 1e-14)

    def test_zero_drive_preserves_basis_populations(self) -> None:
        initial = qutrit_initial_density_matrix("2")
        envelope = SquarePulseEnvelope(0.0, 0.005)
        result = evolve_closed_qutrit_sequence(
            initial,
            envelope,
            transmon_anharmonicity_rad_per_us(-250.0),
            total_simulation_time_us=0.01,
            max_step_us=1e-5,
            pulse_checkpoint_times_us=(0.0, 0.005),
            idle_checkpoint_times_us=(0.0, 0.005),
        )

        for point in result.trajectory:
            self.assertAlmostEqual(point.population_0, 0.0, delta=1e-14)
            self.assertAlmostEqual(point.population_1, 0.0, delta=1e-14)
            self.assertAlmostEqual(point.population_2, 1.0, delta=1e-14)
            self.assertLessEqual(point.population_sum_error, 1e-14)

    def test_free_coherence_matches_diagonal_analytic_phase(self) -> None:
        inverse_sqrt_two = 1.0 / math.sqrt(2.0)
        initial = density_from_ket(
            (inverse_sqrt_two, 0.0 + 0.0j, inverse_sqrt_two)
        )
        detuning = 0.4
        anharmonicity = transmon_anharmonicity_rad_per_us(-250.0)
        duration = 0.002
        envelope = SquarePulseEnvelope(0.0, duration)
        result = evolve_closed_qutrit_sequence(
            initial,
            envelope,
            anharmonicity,
            total_simulation_time_us=duration,
            max_step_us=2e-6,
            detuning_rad_per_us=detuning,
        )

        energy_2 = -2.0 * detuning + anharmonicity
        expected_02 = initial[0][2] * cmath.exp(1j * energy_2 * duration)
        self.assertAlmostEqual(
            result.final_state[0][2],
            expected_02,
            delta=STATE_TOLERANCE,
        )
        for index in range(3):
            self.assertAlmostEqual(
                result.final_state[index][index],
                initial[index][index],
                delta=STATE_TOLERANCE,
            )

    def test_weak_selective_pulse_approaches_two_level_result(self) -> None:
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(
            math.pi / 2.0,
            sigma_us=0.05,
            truncation_sigma=4.0,
        )
        times = _uniform_times(envelope.duration_us, 41)
        qutrit = evolve_closed_qutrit_sequence(
            qutrit_initial_density_matrix("0"),
            envelope,
            transmon_anharmonicity_rad_per_us(-250.0),
            total_simulation_time_us=envelope.duration_us,
            max_step_us=2e-5,
            pulse_checkpoint_times_us=times,
        )
        two_level = evolve_time_dependent_segment(
            ((1.0 + 0.0j, 0.0 + 0.0j), (0.0 + 0.0j, 0.0 + 0.0j)),
            TwoLevelPulseHamiltonian(envelope),
            (),
            duration_us=envelope.duration_us,
            max_step_us=0.001,
            checkpoint_times_us=times,
        )

        self.assertLessEqual(
            _top_left_block_error(qutrit.final_state, two_level.state),
            2e-3,
        )
        self.assertLessEqual(
            qutrit.leakage.leakage_at_final_time,
            2e-5,
        )

    def test_larger_anharmonicity_reduces_recorded_leakage_in_fixed_case(
        self,
    ) -> None:
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(
            math.pi,
            sigma_us=0.002,
            truncation_sigma=4.0,
        )
        times = _uniform_times(envelope.duration_us, 81)
        leakage_by_alpha = {}
        for alpha_mhz in (-100.0, -300.0):
            result = evolve_closed_qutrit_sequence(
                qutrit_initial_density_matrix("0"),
                envelope,
                transmon_anharmonicity_rad_per_us(alpha_mhz),
                total_simulation_time_us=envelope.duration_us,
                max_step_us=2e-6,
                pulse_checkpoint_times_us=times,
            )
            leakage_by_alpha[alpha_mhz] = (
                result.leakage.maximum_recorded_leakage_probability
            )

        self.assertGreater(leakage_by_alpha[-100.0], leakage_by_alpha[-300.0])
        self.assertGreater(leakage_by_alpha[-100.0], 1e-4)

    def test_leakage_metrics_match_population_two(self) -> None:
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(
            math.pi,
            sigma_us=0.002,
            truncation_sigma=4.0,
        )
        result = evolve_closed_qutrit_sequence(
            qutrit_initial_density_matrix("0"),
            envelope,
            transmon_anharmonicity_rad_per_us(-100.0),
            total_simulation_time_us=envelope.duration_us,
            max_step_us=2e-6,
            pulse_checkpoint_times_us=_uniform_times(
                envelope.duration_us,
                81,
            ),
        )

        self.assertAlmostEqual(
            result.leakage.leakage_at_pulse_end,
            result.pulse_end_state[2][2].real,
            delta=1e-14,
        )
        self.assertAlmostEqual(
            result.leakage.leakage_at_final_time,
            result.final_state[2][2].real,
            delta=1e-14,
        )
        self.assertAlmostEqual(
            result.leakage.maximum_recorded_leakage_probability,
            max(point.population_2 for point in result.trajectory),
            delta=1e-14,
        )
        self.assertGreater(
            result.leakage.maximum_recorded_leakage_probability,
            1e-4,
        )

    def test_closed_idle_preserves_populations_and_changes_phase(self) -> None:
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(
            math.pi / 2.0,
            sigma_us=0.004,
            truncation_sigma=4.0,
        )
        pulse_duration = envelope.duration_us
        idle_duration = 0.003
        result = evolve_closed_qutrit_sequence(
            qutrit_initial_density_matrix("0"),
            envelope,
            transmon_anharmonicity_rad_per_us(-200.0),
            total_simulation_time_us=pulse_duration + idle_duration,
            max_step_us=2e-6,
            detuning_rad_per_us=0.5,
            pulse_checkpoint_times_us=(0.0, pulse_duration),
            idle_checkpoint_times_us=(0.0, idle_duration),
        )

        pulse_populations = qutrit_populations(
            result.pulse_end_state,
            pulse_duration,
            "pulse",
        )
        final_populations = qutrit_populations(
            result.final_state,
            pulse_duration + idle_duration,
            "idle",
        )
        for name in ("population_0", "population_1", "population_2"):
            self.assertAlmostEqual(
                getattr(pulse_populations, name),
                getattr(final_populations, name),
                delta=STATE_TOLERANCE,
            )
        self.assertGreater(
            abs(result.final_state[0][1] - result.pulse_end_state[0][1]),
            1e-4,
        )

    def test_raw_physicality_and_population_sum_are_small(self) -> None:
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(
            math.pi,
            sigma_us=0.003,
            truncation_sigma=4.0,
        )
        result = evolve_closed_qutrit_sequence(
            qutrit_initial_density_matrix("0"),
            envelope,
            transmon_anharmonicity_rad_per_us(-200.0),
            total_simulation_time_us=envelope.duration_us,
            max_step_us=2e-6,
            pulse_checkpoint_times_us=_uniform_times(
                envelope.duration_us,
                41,
            ),
        )

        self.assertLessEqual(
            result.pulse_result.diagnostics.raw_trace_error,
            PHYSICALITY_TOLERANCE,
        )
        self.assertLessEqual(
            result.pulse_result.diagnostics.raw_hermiticity_error,
            PHYSICALITY_TOLERANCE,
        )
        self.assertGreaterEqual(
            result.pulse_result.diagnostics.raw_minimum_eigenvalue,
            -PHYSICALITY_TOLERANCE,
        )
        self.assertLessEqual(
            result.pulse_result.diagnostics.cleanup_correction_norm,
            PHYSICALITY_TOLERANCE,
        )
        self.assertLessEqual(
            max(point.population_sum_error for point in result.trajectory),
            PHYSICALITY_TOLERANCE,
        )

    def test_invalid_state_and_timing_are_rejected(self) -> None:
        envelope = SquarePulseEnvelope(0.0, 0.1)
        alpha = transmon_anharmonicity_rad_per_us(-250.0)
        with self.assertRaises(ValueError):
            evolve_closed_qutrit_sequence(
                ((1.0 + 0.0j, 0.0 + 0.0j), (0.0 + 0.0j, 0.0 + 0.0j)),
                envelope,
                alpha,
                0.1,
                0.01,
            )
        with self.assertRaises(ValueError):
            evolve_closed_qutrit_sequence(
                qutrit_initial_density_matrix("0"),
                envelope,
                alpha,
                0.05,
                0.01,
            )
        with self.assertRaises(ValueError):
            qutrit_initial_density_matrix("3")

    def test_hamiltonian_provider_matches_frozen_constructor(self) -> None:
        envelope = SquarePulseEnvelope(2.0, 0.1)
        alpha = transmon_anharmonicity_rad_per_us(-250.0)
        provider = QutritPulseHamiltonian(
            envelope,
            alpha,
            phase_rad=math.pi / 2.0,
            detuning_rad_per_us=0.3,
        )
        assert_matrix_close(provider.evaluate(0.05),
            qutrit_rotating_frame_hamiltonian(
                0.3,
                alpha,
                0.0,
                2.0,
            ),
            1e-14,
        )


def _uniform_times(duration_us: float, count: int) -> tuple[float, ...]:
    return tuple(
        duration_us * index / (count - 1)
        for index in range(count)
    )


def _top_left_block_error(qutrit_state, two_level_state) -> float:
    return max(
        abs(qutrit_state[row][column] - two_level_state[row][column])
        for row in range(2)
        for column in range(2)
    )


if __name__ == "__main__":
    unittest.main()

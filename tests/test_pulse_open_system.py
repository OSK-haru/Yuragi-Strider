import math
import unittest
from types import SimpleNamespace

from core.gates import initial_density_matrix
from core.pulse_envelopes import (
    GaussianPulseEnvelope,
    SquarePulseEnvelope,
)
from core.pulse_open_system import (
    PulseDissipationRates,
    evolve_open_pulse_sequence,
    pulse_dissipation_rates,
)
from core.results import EnvironmentConfig
from validation_pulse.pulse_analytic import (
    matrix_error_metrics,
    pure_target_fidelity,
)
from validation_pulse.pulse_phase_detuning import (
    analytic_constant_drive_density,
)


TOLERANCE = 2e-8


class PulseOpenSystemTests(unittest.TestCase):
    def test_zero_rate_limit_matches_closed_square_and_gaussian(self) -> None:
        initial = initial_density_matrix(["0"])
        zero_rates = PulseDissipationRates(
            input_mode="direct_rates",
            gamma_down_per_us=0.0,
            gamma_up_per_us=0.0,
            gamma_phi_per_us=0.0,
        )
        cases = (
            (
                SquarePulseEnvelope.from_target_rotation_angle(
                    math.pi / 2.0,
                    1.0,
                ),
                math.pi / 2.0,
            ),
            (
                GaussianPulseEnvelope.from_target_rotation_angle(
                    math.pi,
                    0.2,
                    4.0,
                ),
                math.pi,
            ),
        )
        for envelope, angle in cases:
            with self.subTest(envelope=type(envelope).__name__):
                result = evolve_open_pulse_sequence(
                    initial,
                    envelope,
                    zero_rates,
                    envelope.duration_us,
                    0.005,
                )
                target = analytic_constant_drive_density(
                    initial,
                    angle / envelope.duration_us,
                    0.0,
                    0.0,
                    envelope.duration_us,
                )
                if isinstance(envelope, GaussianPulseEnvelope):
                    target = (
                        (0.0 + 0.0j, 0.0 + 0.0j),
                        (0.0 + 0.0j, 1.0 + 0.0j),
                    )
                self.assertLessEqual(
                    matrix_error_metrics(
                        result.final_state,
                        target,
                    )["max_element_error"],
                    TOLERANCE,
                )

    def test_relaxation_changes_state_during_square_drive(self) -> None:
        initial = initial_density_matrix(["0"])
        envelope = SquarePulseEnvelope.from_target_rotation_angle(
            math.pi,
            1.0,
        )
        open_result = evolve_open_pulse_sequence(
            initial,
            envelope,
            PulseDissipationRates("direct_rates", 0.8, 0.0, 0.0),
            1.0,
            0.0025,
        )
        closed_target = analytic_constant_drive_density(
            initial,
            math.pi,
            0.0,
            0.0,
            1.0,
        )

        self.assertLess(
            pure_target_fidelity(
                open_result.pulse_end_state,
                closed_target,
            ),
            0.99,
        )
        self.assertLess(open_result.pulse_end_state[1][1].real, 0.9)

    def test_dephasing_changes_gaussian_drive_coherence(self) -> None:
        initial = initial_density_matrix(["0"])
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(
            math.pi / 2.0,
            0.2,
            4.0,
        )
        closed = evolve_open_pulse_sequence(
            initial,
            envelope,
            PulseDissipationRates("direct_rates", 0.0, 0.0, 0.0),
            envelope.duration_us,
            0.005,
        )
        dephased = evolve_open_pulse_sequence(
            initial,
            envelope,
            PulseDissipationRates("direct_rates", 0.0, 0.0, 1.0),
            envelope.duration_us,
            0.005,
        )

        self.assertLess(
            abs(dephased.pulse_end_state[0][1]),
            abs(closed.pulse_end_state[0][1]),
        )
        self.assertLess(
            pure_target_fidelity(
                dephased.pulse_end_state,
                closed.pulse_end_state,
            ),
            0.9,
        )

    def test_finite_temperature_excitation_acts_in_pulse_and_idle(self) -> None:
        initial = initial_density_matrix(["0"])
        envelope = SquarePulseEnvelope.from_target_rotation_angle(
            0.1,
            0.5,
        )
        with_excitation = evolve_open_pulse_sequence(
            initial,
            envelope,
            PulseDissipationRates("direct_rates", 0.3, 0.2, 0.0),
            2.0,
            0.0025,
            idle_checkpoint_times_us=(0.0, 1.5),
        )
        without_excitation = evolve_open_pulse_sequence(
            initial,
            envelope,
            PulseDissipationRates("direct_rates", 0.3, 0.0, 0.0),
            2.0,
            0.0025,
        )

        self.assertGreater(
            with_excitation.pulse_end_state[1][1].real,
            without_excitation.pulse_end_state[1][1].real + 0.05,
        )
        self.assertGreater(
            with_excitation.final_state[1][1].real,
            with_excitation.pulse_end_state[1][1].real + 0.05,
        )
        self.assertIsNotNone(with_excitation.idle_result)
        assert with_excitation.idle_result is not None
        self.assertEqual(
            with_excitation.idle_result.checkpoints[0].cleaned_state,
            with_excitation.pulse_end_state,
        )

    def test_long_idle_relaxes_after_x_pi_pulse(self) -> None:
        initial = initial_density_matrix(["0"])
        envelope = SquarePulseEnvelope.from_target_rotation_angle(
            math.pi,
            0.2,
        )
        result = evolve_open_pulse_sequence(
            initial,
            envelope,
            PulseDissipationRates("direct_rates", 0.1, 0.0, 0.0),
            20.2,
            0.01,
        )

        self.assertGreater(result.pulse_end_state[1][1].real, 0.98)
        self.assertLess(result.final_state[1][1].real, 0.15)

    def test_physical_and_equivalent_direct_rates_match(self) -> None:
        environment = EnvironmentConfig(
            t1_zero_temperature_us=39.810717055349734,
            tphi_zero_temperature_us=28.474724895080143,
            temperature_mk=100.0,
            qubit_frequency_ghz=5.0,
        )
        physical_rates = pulse_dissipation_rates(environment)
        direct_rates = pulse_dissipation_rates(SimpleNamespace(
            input_mode="direct_rates",
            gamma_down_per_us=physical_rates.gamma_down_per_us,
            gamma_up_per_us=physical_rates.gamma_up_per_us,
            gamma_phi_per_us=physical_rates.gamma_phi_per_us,
        ))
        envelope = SquarePulseEnvelope.from_target_rotation_angle(
            math.pi / 2.0,
            1.0,
        )
        initial = initial_density_matrix(["0"])
        physical = evolve_open_pulse_sequence(
            initial,
            envelope,
            physical_rates,
            3.0,
            0.005,
        )
        direct = evolve_open_pulse_sequence(
            initial,
            envelope,
            direct_rates,
            3.0,
            0.005,
        )

        self.assertLessEqual(
            matrix_error_metrics(
                physical.pulse_end_state,
                direct.pulse_end_state,
            )["max_element_error"],
            1e-14,
        )
        self.assertLessEqual(
            matrix_error_metrics(
                physical.final_state,
                direct.final_state,
            )["max_element_error"],
            1e-14,
        )

    def test_reports_raw_physicality_for_both_segments(self) -> None:
        result = evolve_open_pulse_sequence(
            initial_density_matrix(["0"]),
            SquarePulseEnvelope.from_target_rotation_angle(
                math.pi / 2.0,
                0.5,
            ),
            PulseDissipationRates("direct_rates", 0.2, 0.1, 0.3),
            1.5,
            0.005,
        )
        diagnostics = [result.pulse_result.diagnostics]
        assert result.idle_result is not None
        diagnostics.append(result.idle_result.diagnostics)

        for segment in diagnostics:
            self.assertLessEqual(segment.raw_trace_error, 1e-12)
            self.assertLessEqual(segment.raw_hermiticity_error, 1e-12)
            self.assertGreaterEqual(segment.raw_minimum_eigenvalue, -1e-10)
            self.assertLessEqual(
                segment.cleanup_correction_norm,
                1e-12,
            )

    def test_rejects_unsupported_mode_and_invalid_timing(self) -> None:
        with self.assertRaisesRegex(ValueError, "physical or direct_rates"):
            pulse_dissipation_rates(SimpleNamespace(input_mode="normalized"))

        with self.assertRaisesRegex(
            ValueError,
            "must not exceed",
        ):
            evolve_open_pulse_sequence(
                initial_density_matrix(["0"]),
                SquarePulseEnvelope(1.0, 2.0),
                PulseDissipationRates("direct_rates", 0.0, 0.0, 0.0),
                1.0,
                0.01,
            )


if __name__ == "__main__":
    unittest.main()

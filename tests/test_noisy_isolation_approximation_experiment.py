"""Controls and numerical stability checks for the noisy comparison pilot."""

import math
import unittest

import numpy as np

from core.experimental_isolation_physics import (
    DriveCrosstalkTransfer,
    ExtendedTransmonNetworkHamiltonian,
    ResidualZZCoupling,
)
from core.pulse_envelopes import SquarePulseEnvelope
from core.pulse_evolution import evolve_dense_time_dependent_segment
from core.pulse_qutrit_open_system import QutritDissipationRates
from core.pulse_transmon_network import (
    CoupledTransmonNetworkHamiltonian,
    ScheduledTransmonDrive,
    network_site_local_dissipator,
)
from scripts.experiment_noisy_isolation_approximation import (
    PLUS,
    ZZ_SHIFT_RAD_PER_US,
    coupled_case,
    reset_decay_case,
    shot_sampling,
)


class NoisyIsolationApproximationPilotTests(unittest.TestCase):
    def test_drive_only_stays_separable_with_local_noise(self) -> None:
        for noise_scale in (0.0, 1.0, 4.0):
            with self.subTest(noise_scale=noise_scale):
                case = coupled_case(
                    noise_scale=noise_scale,
                    zz_shift_rad_per_us=0.0,
                    drive_enabled=True,
                )
                self.assertLess(
                    max(point["joint_outcome_tvd"] for point in case["checkpoints"]),
                    1e-7,
                )

    def test_zz_error_survives_noise_and_pulse_timing(self) -> None:
        for drive_enabled in (False, True):
            with self.subTest(drive_enabled=drive_enabled):
                case = coupled_case(
                    noise_scale=1.0,
                    zz_shift_rad_per_us=ZZ_SHIFT_RAD_PER_US,
                    drive_enabled=drive_enabled,
                )
                self.assertGreater(case["checkpoints"][-1]["joint_outcome_tvd"], 0.09)
                self.assertLess(case["final_exact_physicality"]["trace_error"], 1e-10)
                self.assertGreater(case["final_exact_physicality"]["minimum_eigenvalue"], -1e-7)

    def test_step_halving_changes_observable_far_less_than_model_choice(self) -> None:
        coarse = coupled_case(
            noise_scale=1.0,
            zz_shift_rad_per_us=ZZ_SHIFT_RAD_PER_US,
            drive_enabled=True,
            max_step_us=0.004,
        )
        fine = coupled_case(
            noise_scale=1.0,
            zz_shift_rad_per_us=ZZ_SHIFT_RAD_PER_US,
            drive_enabled=True,
            max_step_us=0.002,
        )
        for key in ("p_first_zero_exact", "p_first_zero_local", "joint_outcome_tvd"):
            self.assertLess(abs(coarse["checkpoints"][-1][key] - fine["checkpoints"][-1][key]), 1e-6)

    def test_reference_agrees_with_existing_dense_solver(self) -> None:
        drive = ScheduledTransmonDrive(0, 0.04, SquarePulseEnvelope(20.0, 0.06))
        network = CoupledTransmonNetworkHamiltonian(
            anharmonicities_rad_per_us=(0.0, 0.0),
            detunings_rad_per_us=(0.0, 0.0),
            couplings=(),
            drives=(drive,),
            local_dimension=2,
        )
        hamiltonian = ExtendedTransmonNetworkHamiltonian(
            network,
            residual_zz=(ResidualZZCoupling(0, 1, ZZ_SHIFT_RAD_PER_US),),
            drive_crosstalk=(DriveCrosstalkTransfer(0, 1, 0.2),),
        )
        rates = tuple(
            QutritDissipationRates(
                input_mode="direct_rates",
                gamma_10_down_per_us=down,
                gamma_01_up_per_us=up,
                gamma_21_down_per_us=0.0,
                gamma_12_up_per_us=0.0,
                gamma_phi_adjacent_per_us=phi,
            )
            for down, up, phi in ((0.20, 0.01, 0.05), (0.35, 0.02, 0.08))
        )
        initial = np.kron(PLUS, PLUS)
        density = np.outer(initial, initial.conj())
        result = evolve_dense_time_dependent_segment(
            tuple(tuple(complex(value) for value in row) for row in density),
            hamiltonian,
            network_site_local_dissipator(rates, 2),
            duration_us=0.30,
            max_step_us=0.002,
            checkpoint_times_us=(0.04, 0.10, 0.20),
        )
        hadamard = np.array(((1.0, 1.0), (1.0, -1.0))) / math.sqrt(2.0)
        basis = np.kron(hadamard, hadamard)
        probabilities = np.real(np.diag(basis @ np.asarray(result.state) @ basis.conj().T))
        pilot = coupled_case(
            noise_scale=1.0,
            zz_shift_rad_per_us=ZZ_SHIFT_RAD_PER_US,
            drive_enabled=True,
        )
        self.assertAlmostEqual(probabilities[:2].sum(), pilot["checkpoints"][-1]["p_first_zero_exact"], places=9)

    def test_reset_memory_decays_but_does_not_vanish_at_one_microsecond(self) -> None:
        checkpoints = reset_decay_case()["checkpoints"]
        self.assertAlmostEqual(checkpoints[0]["victim_dependence"], 0.1)
        self.assertGreater(checkpoints[-1]["victim_dependence"], 0.0)
        self.assertAlmostEqual(
            checkpoints[-1]["victim_dependence"],
            0.1 * math.exp(-0.42),
            places=9,
        )

    def test_shot_sampling_is_seeded_and_separate_from_model_probability(self) -> None:
        first = shot_sampling(p_exact=0.64, p_local=0.72, trials=100)
        second = shot_sampling(p_exact=0.64, p_local=0.72, trials=100)
        self.assertEqual(first, second)
        self.assertAlmostEqual(first["theoretical_difference"], 0.08)
        self.assertEqual([item["shots_per_model"] for item in first["summaries"]], [32, 4096])


if __name__ == "__main__":
    unittest.main()

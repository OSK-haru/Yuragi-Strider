"""R1-R4 parity checks for actual frozen pulse-model inputs.

The Python side evaluates time-dependent Hamiltonians at the four prescribed
RK4 stage times. Rust receives those matrices and performs only raw dense
linear algebra, so this test does not change the physical model or cleanup
policy.
"""

from __future__ import annotations

import unittest

from core.gates import (
    Matrix,
    add,
    lindblad_rhs_cached,
    multi_qubit_physical_collapse_operators,
    prepare_collapse_operators,
    scale,
)
from core.pulse_evolution import evolve_time_dependent_segment
from core.pulse_envelopes import (
    GaussianPulseEnvelope,
    SquarePulseEnvelope,
    TwoLevelPulseHamiltonian,
)
from core.pulse_open_system import (
    PulseDissipationRates,
    evolve_open_pulse_sequence,
)
from core.pulse_qutrit import (
    QutritPulseHamiltonian,
    qutrit_initial_density_matrix,
)
from core.pulse_qutrit_contract import mhz_to_rad_per_us
from core.pulse_qutrit_open_system import (
    QutritDissipationRates,
    evolve_open_qutrit_sequence,
    qutrit_collapse_operator_matrices,
)
from core.rust_dense_kernel import (
    is_rust_kernel_available,
    rust_lindblad_rhs,
    rust_rk4_time_dependent_stages,
    rust_rk4_time_dependent_step,
)
from tests.physics_test_helpers import assert_matrix_close




@unittest.skipUnless(
    is_rust_kernel_available(),
    "yuragi_strider_rust is not importable",
)
class RustTimeDependentParityTest(unittest.TestCase):
    def test_two_level_gaussian_rhs_stages_and_step_match_python(self) -> None:
        state: Matrix = (
            (0.5 + 0.0j, 0.5 + 0.0j),
            (0.5 + 0.0j, 0.5 + 0.0j),
        )
        provider = TwoLevelPulseHamiltonian(
            envelope=GaussianPulseEnvelope.from_target_rotation_angle(
                target_rotation_angle_rad=1.1,
                sigma_us=0.08,
                truncation_sigma=3.0,
            ),
            phase_rad=0.37,
            detuning_rad_per_us=0.29,
        )
        collapse_matrices = multi_qubit_physical_collapse_operators(
            1,
            gamma_down_per_us=0.03,
            gamma_up_per_us=0.007,
            gamma_phi_per_us=0.02,
        )
        self._assert_parity(
            state=state,
            provider=provider,
            collapse_matrices=collapse_matrices,
            local_time_us=0.071,
            dt=0.004,
        )

    def test_qutrit_drag_rhs_stages_and_step_match_python(self) -> None:
        state = qutrit_initial_density_matrix("1")
        provider = QutritPulseHamiltonian(
            envelope=GaussianPulseEnvelope.from_target_rotation_angle(
                target_rotation_angle_rad=1.3,
                sigma_us=0.06,
                truncation_sigma=3.0,
            ),
            anharmonicity_rad_per_us=mhz_to_rad_per_us(-220.0),
            phase_rad=-0.41,
            detuning_rad_per_us=0.23,
            drag_beta_us=0.014,
        )
        rates = QutritDissipationRates(
            input_mode="direct_rates",
            gamma_10_down_per_us=0.03,
            gamma_01_up_per_us=0.004,
            gamma_21_down_per_us=0.05,
            gamma_12_up_per_us=0.006,
            gamma_phi_adjacent_per_us=0.02,
        )
        self._assert_parity(
            state=state,
            provider=provider,
            collapse_matrices=qutrit_collapse_operator_matrices(rates),
            local_time_us=0.083,
            dt=0.002,
        )

    def test_rejects_invalid_stage_count(self) -> None:
        state: Matrix = (
            (1.0 + 0.0j, 0.0 + 0.0j),
            (0.0 + 0.0j, 0.0 + 0.0j),
        )
        with self.assertRaises(ValueError):
            rust_rk4_time_dependent_step(
                state,
                (state, state, state),
                (),
                0.01,
            )

    def test_two_level_multi_step_trajectory_matches_python(self) -> None:
        state: Matrix = (
            (1.0 + 0.0j, 0.0 + 0.0j),
            (0.0 + 0.0j, 0.0 + 0.0j),
        )
        provider = TwoLevelPulseHamiltonian(
            envelope=GaussianPulseEnvelope.from_target_rotation_angle(
                target_rotation_angle_rad=1.2,
                sigma_us=0.07,
                truncation_sigma=3.0,
            ),
            phase_rad=0.21,
            detuning_rad_per_us=-0.17,
        )
        collapse_matrices = multi_qubit_physical_collapse_operators(
            1,
            gamma_down_per_us=0.04,
            gamma_up_per_us=0.006,
            gamma_phi_per_us=0.015,
        )
        cached_ops = prepare_collapse_operators(collapse_matrices)
        python_result = evolve_time_dependent_segment(
            state,
            provider,
            cached_ops,
            duration_us=0.03,
            max_step_us=0.004,
            checkpoint_times_us=(0.012, 0.024),
            backend="python",
        )
        rust_result = evolve_time_dependent_segment(
            state,
            provider,
            cached_ops,
            duration_us=0.03,
            max_step_us=0.004,
            checkpoint_times_us=(0.012, 0.024),
            backend="rust",
        )
        assert_matrix_close(python_result.raw_final_state, rust_result.raw_final_state)
        assert_matrix_close(python_result.state, rust_result.state)
        self.assertEqual(len(python_result.checkpoints), len(rust_result.checkpoints))
        for python_checkpoint, rust_checkpoint in zip(
            python_result.checkpoints,
            rust_result.checkpoints,
        ):
            self.assertEqual(python_checkpoint.time_us, rust_checkpoint.time_us)
            assert_matrix_close(python_checkpoint.raw_state,
                rust_checkpoint.raw_state,
            )
            assert_matrix_close(python_checkpoint.cleaned_state,
                rust_checkpoint.cleaned_state,
            )

    def test_qutrit_drag_multi_step_trajectory_matches_python(self) -> None:
        state = qutrit_initial_density_matrix("1")
        provider = QutritPulseHamiltonian(
            envelope=GaussianPulseEnvelope.from_target_rotation_angle(
                target_rotation_angle_rad=1.1,
                sigma_us=0.05,
                truncation_sigma=3.0,
            ),
            anharmonicity_rad_per_us=mhz_to_rad_per_us(-210.0),
            phase_rad=-0.32,
            detuning_rad_per_us=0.18,
            drag_beta_us=0.012,
        )
        rates = QutritDissipationRates(
            input_mode="direct_rates",
            gamma_10_down_per_us=0.025,
            gamma_01_up_per_us=0.003,
            gamma_21_down_per_us=0.04,
            gamma_12_up_per_us=0.005,
            gamma_phi_adjacent_per_us=0.017,
        )
        cached_ops = prepare_collapse_operators(
            qutrit_collapse_operator_matrices(rates)
        )
        python_result = evolve_time_dependent_segment(
            state,
            provider,
            cached_ops,
            duration_us=0.022,
            max_step_us=0.004,
            checkpoint_times_us=(0.01,),
            backend="python",
        )
        rust_result = evolve_time_dependent_segment(
            state,
            provider,
            cached_ops,
            duration_us=0.022,
            max_step_us=0.004,
            checkpoint_times_us=(0.01,),
            backend="rust",
        )
        assert_matrix_close(python_result.raw_final_state, rust_result.raw_final_state)
        assert_matrix_close(python_result.state, rust_result.state)
        for python_checkpoint, rust_checkpoint in zip(
            python_result.checkpoints,
            rust_result.checkpoints,
        ):
            assert_matrix_close(python_checkpoint.raw_state,
                rust_checkpoint.raw_state,
            )
            assert_matrix_close(python_checkpoint.cleaned_state,
                rust_checkpoint.cleaned_state,
            )

    def test_auto_backend_matches_rust_when_kernel_is_available(self) -> None:
        state: Matrix = (
            (1.0 + 0.0j, 0.0 + 0.0j),
            (0.0 + 0.0j, 0.0 + 0.0j),
        )
        provider = TwoLevelPulseHamiltonian(
            envelope=GaussianPulseEnvelope.from_target_rotation_angle(
                target_rotation_angle_rad=0.8,
                sigma_us=0.04,
                truncation_sigma=3.0,
            )
        )
        rust_result = evolve_time_dependent_segment(
            state,
            provider,
            (),
            duration_us=0.01,
            max_step_us=0.002,
            backend="rust",
        )
        auto_result = evolve_time_dependent_segment(
            state,
            provider,
            (),
            duration_us=0.01,
            max_step_us=0.002,
            backend="auto",
        )
        assert_matrix_close(rust_result.state, auto_result.state)

    def test_two_level_sequence_cases_match_python(self) -> None:
        cases = (
            (
                "square_closed",
                SquarePulseEnvelope.from_target_rotation_angle(1.0, 0.04),
                PulseDissipationRates("direct_rates", 0.0, 0.0, 0.0),
                0.04,
                0.0,
                0.0,
            ),
            (
                "gaussian_detuned_dissipative_idle",
                GaussianPulseEnvelope.from_target_rotation_angle(
                    target_rotation_angle_rad=1.2,
                    sigma_us=0.01,
                    truncation_sigma=3.0,
                ),
                PulseDissipationRates("direct_rates", 0.04, 0.006, 0.02),
                0.09,
                0.28,
                0.19,
            ),
        )
        initial = (
            (1.0 + 0.0j, 0.0 + 0.0j),
            (0.0 + 0.0j, 0.0 + 0.0j),
        )
        for name, envelope, rates, duration, phase, detuning in cases:
            with self.subTest(case=name):
                python_result = evolve_open_pulse_sequence(
                    initial,
                    envelope,
                    rates,
                    duration,
                    max_step_us=0.004,
                    phase_rad=phase,
                    detuning_rad_per_us=detuning,
                    backend="python",
                )
                rust_result = evolve_open_pulse_sequence(
                    initial,
                    envelope,
                    rates,
                    duration,
                    max_step_us=0.004,
                    phase_rad=phase,
                    detuning_rad_per_us=detuning,
                    backend="rust",
                )
                _assert_sequence_result_close(self, python_result, rust_result)

    def test_qutrit_drag_dissipative_idle_sequence_matches_python(self) -> None:
        envelope = GaussianPulseEnvelope.from_target_rotation_angle(
            target_rotation_angle_rad=1.3,
            sigma_us=0.008,
            truncation_sigma=3.0,
        )
        rates = QutritDissipationRates(
            input_mode="direct_rates",
            gamma_10_down_per_us=0.04,
            gamma_01_up_per_us=0.005,
            gamma_21_down_per_us=0.07,
            gamma_12_up_per_us=0.008,
            gamma_phi_adjacent_per_us=0.025,
        )
        kwargs = {
            "anharmonicity_rad_per_us": mhz_to_rad_per_us(-220.0),
            "rates": rates,
            "total_simulation_time_us": 0.075,
            "max_step_us": 0.00001,
            "phase_rad": -0.31,
            "detuning_rad_per_us": 0.22,
            "drag_beta_us": 0.011,
        }
        initial = qutrit_initial_density_matrix("1")
        python_result = evolve_open_qutrit_sequence(
            initial,
            envelope,
            backend="python",
            **kwargs,
        )
        rust_result = evolve_open_qutrit_sequence(
            initial,
            envelope,
            backend="rust",
            **kwargs,
        )
        _assert_sequence_result_close(self, python_result, rust_result)
        self.assertGreater(
            python_result.leakage.maximum_recorded_leakage_probability,
            0.0,
        )

    def _assert_parity(
        self,
        *,
        state: Matrix,
        provider: TwoLevelPulseHamiltonian | QutritPulseHamiltonian,
        collapse_matrices: tuple[Matrix, ...] | list[Matrix],
        local_time_us: float,
        dt: float,
    ) -> None:
        hamiltonian_stages = (
            provider.evaluate(local_time_us),
            provider.evaluate(local_time_us + 0.5 * dt),
            provider.evaluate(local_time_us + 0.5 * dt),
            provider.evaluate(local_time_us + dt),
        )
        cached_ops = prepare_collapse_operators(collapse_matrices)
        python_stages = _python_rk4_stages(
            state,
            hamiltonian_stages,
            cached_ops,
            dt,
        )
        rust_stages = rust_rk4_time_dependent_stages(
            state,
            hamiltonian_stages,
            collapse_matrices,
            dt,
        )
        for python_stage, rust_stage in zip(python_stages, rust_stages):
            assert_matrix_close(python_stage, rust_stage)

        python_rhs = lindblad_rhs_cached(
            state,
            hamiltonian_stages[0],
            cached_ops,
        )
        rust_rhs = rust_lindblad_rhs(
            state,
            hamiltonian_stages[0],
            collapse_matrices,
        )
        assert_matrix_close(python_rhs, rust_rhs)

        python_step = _rk4_step_from_stages(state, python_stages, dt)
        rust_step = rust_rk4_time_dependent_step(
            state,
            hamiltonian_stages,
            collapse_matrices,
            dt,
        )
        assert_matrix_close(python_step, rust_step)


def _python_rk4_stages(
    state: Matrix,
    hamiltonian_stages: tuple[Matrix, Matrix, Matrix, Matrix],
    collapse_ops,
    dt: float,
) -> tuple[Matrix, Matrix, Matrix, Matrix]:
    h1, h2, h3, h4 = hamiltonian_stages
    k1 = lindblad_rhs_cached(state, h1, collapse_ops)
    k2 = lindblad_rhs_cached(add(state, scale(0.5 * dt, k1)), h2, collapse_ops)
    k3 = lindblad_rhs_cached(add(state, scale(0.5 * dt, k2)), h3, collapse_ops)
    k4 = lindblad_rhs_cached(add(state, scale(dt, k3)), h4, collapse_ops)
    return k1, k2, k3, k4


def _rk4_step_from_stages(
    state: Matrix,
    stages: tuple[Matrix, Matrix, Matrix, Matrix],
    dt: float,
) -> Matrix:
    k1, k2, k3, k4 = stages
    return add(
        state,
        scale(
            dt / 6.0,
            add(k1, scale(2.0, k2), scale(2.0, k3), k4),
        ),
    )


def _assert_sequence_result_close(
    test_case: unittest.TestCase,
    python_result,
    rust_result,
) -> None:
    assert_matrix_close(python_result.pulse_result.raw_final_state,
        rust_result.pulse_result.raw_final_state,
    )
    assert_matrix_close(python_result.pulse_result.state,
        rust_result.pulse_result.state,
    )
    test_case.assertEqual(
        python_result.idle_result is None,
        rust_result.idle_result is None,
    )
    if python_result.idle_result is not None:
        assert rust_result.idle_result is not None
        assert_matrix_close(python_result.idle_result.raw_final_state,
            rust_result.idle_result.raw_final_state,
        )
        assert_matrix_close(python_result.idle_result.state,
            rust_result.idle_result.state,
        )
    assert_matrix_close(python_result.final_state,
        rust_result.final_state,
    )

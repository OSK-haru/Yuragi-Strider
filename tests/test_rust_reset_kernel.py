"""Parity tests for the optional Rust local-reset sequence kernel."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import numpy as np
import yuragi_strider_rust

from core.experimental_isolation_physics.imperfect_reset import (
    ImperfectResetSpec,
    apply_local_imperfect_reset,
)
from core.experimental_isolation_physics.reset_model import (
    PerQubitPhenomenologicalResetModel,
    PhenomenologicalResetModel,
    ResetModelRequest,
)
from core.gates import density_from_ket
from core.register_operations import (
    apply_local_replacement,
    freeze_register,
)
from core.rust_dense_kernel import (
    flatten_complex_matrix,
    is_rust_kernel_available,
    rust_apply_local_reset_sequence,
    unflatten_complex_matrix,
)


@unittest.skipUnless(is_rust_kernel_available(), "yuragi_strider_rust is not importable")
class RustResetKernelTests(unittest.TestCase):
    def test_qubit_reset_adapter_uses_the_shared_register_batch_api_when_available(self):
        state = ((1.0 + 0.0j, 0.0j), (0.0j, 0.0j))
        operation = Mock(return_value=([], flatten_complex_matrix(state)))
        fake_module = Mock(execute_register_batch_flat=operation)

        with patch("core.rust_dense_kernel._load_rust_module", return_value=fake_module):
            actual = rust_apply_local_reset_sequence(
                state,
                subsystem_count=1,
                local_dimension=2,
                targets=(0,),
                success_probabilities=(0.4,),
                target_populations=((0.25, 0.75),),
                reset_count=2,
            )

        expected_operation = ("reset", [0], [0.4, 0.25, 0.75], 0.0)
        operation.assert_called_once_with(
            "register_batch_v1",
            flatten_complex_matrix(state),
            1,
            [expected_operation, expected_operation],
        )
        self.assertEqual(actual, state)

    def test_rust_owned_session_batches_operations_and_rejects_partial_commit(self):
        amplitudes = np.asarray((1.0, 1.0j, 0.4, -0.2j), dtype=complex)
        amplitudes /= np.linalg.norm(amplitudes)
        initial = freeze_register(np.outer(amplitudes, amplitudes.conj()))
        operations = [([0], [0.2], [0.8, 0.2], 2), ([1], [0.7], [0.1, 0.9], 1)]
        expected = np.asarray(initial)
        for targets, probabilities, populations, count in operations:
            for _ in range(count):
                expected = apply_local_replacement(expected, targets[0], probabilities[0], populations)
        session = yuragi_strider_rust.ResetSession(
            "local_reset_state_v1", flatten_complex_matrix(initial), 2, 2,
        )
        session.execute("local_reset_batch_v1", operations)
        np.testing.assert_allclose(unflatten_complex_matrix(session.state_flat(), 4), expected,
                                   rtol=0.0, atol=2e-13)
        before = session.state_flat()
        with self.assertRaises(ValueError):
            session.execute("local_reset_batch_v1", [operations[0], ([1], [0.5], [0.2, 0.2], 1)])
        self.assertEqual(session.state_flat(), before)
        with self.assertRaisesRegex(ValueError, "schema version"):
            session.execute("unknown", operations)
        self.assertEqual(session.state_flat(), before)

    def test_rust_owned_qutrit_session_matches_repeated_kraus_channels(self):
        initial = density_from_ket((0.3 + 0.2j, 0.5j, 0.7 - 0.1j))
        spec = ImperfectResetSpec(0.37, (0.2, 0.3, 0.5))
        expected = apply_local_imperfect_reset(initial, 1, 0, spec)
        expected = apply_local_imperfect_reset(expected, 1, 0, spec)
        session = yuragi_strider_rust.ResetSession(
            "local_reset_state_v1", flatten_complex_matrix(initial), 1, 3,
        )
        session.execute("local_reset_batch_v1", [([0], [0.37], [0.2, 0.3, 0.5], 2)])
        np.testing.assert_allclose(unflatten_complex_matrix(session.state_flat(), 3), expected,
                                   rtol=0.0, atol=2e-13)

    def test_multi_qubit_sequence_matches_python_and_preserves_density_invariants(self) -> None:
        amplitudes = np.asarray([1 + 1j, 2 - 0.5j, -1j, 0.3, 1.2j, -2, 0.7, 1.0 + 0.2j], dtype=complex)
        amplitudes /= np.linalg.norm(amplitudes)
        initial = np.outer(amplitudes, amplitudes.conj())
        targets = (0, 2)
        probabilities = (0.22, 0.61)
        populations = ((0.8, 0.2), (0.15, 0.85))
        expected = initial
        for _ in range(3):
            for target, probability, target_state in zip(
                targets, probabilities, populations, strict=True
            ):
                expected = apply_local_replacement(
                    expected, target, probability, target_state
                )

        actual = np.asarray(rust_apply_local_reset_sequence(
            freeze_register(initial),
            subsystem_count=3,
            local_dimension=2,
            targets=targets,
            success_probabilities=probabilities,
            target_populations=populations,
            reset_count=3,
        ), dtype=complex)

        np.testing.assert_allclose(actual, expected, rtol=0.0, atol=2e-13)
        self.assertAlmostEqual(float(np.trace(actual).real), 1.0, places=12)
        np.testing.assert_allclose(actual, actual.conj().T, rtol=0.0, atol=2e-13)
        self.assertGreaterEqual(float(np.linalg.eigvalsh(actual).min()), -2e-13)

    def test_qutrit_local_reset_matches_existing_kraus_channel(self) -> None:
        initial = density_from_ket((0.3 + 0.2j, 0.5j, 0.7 - 0.1j))
        spec = ImperfectResetSpec(0.37, (0.2, 0.3, 0.5))
        expected = apply_local_imperfect_reset(initial, 1, 0, spec)
        actual = apply_local_imperfect_reset(
            initial, 1, 0, spec, kernel_backend="rust_dense_preview"
        )
        np.testing.assert_allclose(actual, expected, rtol=0.0, atol=2e-13)

    def test_reset_model_runs_register_transition_with_one_rust_backend_selection(self) -> None:
        amplitudes = np.asarray((1.0, 1.0j, 0.4, -0.2j), dtype=complex)
        amplitudes /= np.linalg.norm(amplitudes)
        initial = freeze_register(np.outer(amplitudes, amplitudes.conj()))
        request = ResetModelRequest(
            input_state=initial,
            reset_count=2,
            victim_measurement=True,
            subsystem_count=2,
            targets=(0, 1),
        )
        specs = (
            ImperfectResetSpec(0.18, (0.7, 0.3)),
            ImperfectResetSpec(0.42, (0.25, 0.75)),
        )
        assignment_errors = ((0.03, 0.04), (0.01, 0.02))
        python_model = PerQubitPhenomenologicalResetModel(
            (13, 4), specs, assignment_errors,
        )
        rust_model = PerQubitPhenomenologicalResetModel(
            (13, 4), specs, assignment_errors, kernel_backend="rust_dense_preview",
        )
        python_result = python_model.run_register(request)
        rust_result = rust_model.run_register(request)
        np.testing.assert_allclose(
            rust_result.post_reset_state,
            python_result.post_reset_state,
            rtol=0.0,
            atol=2e-13,
        )
        self.assertEqual(rust_result.assignment_errors, python_result.assignment_errors)
        self.assertNotEqual(rust_model.configuration_id, python_model.configuration_id)

    def test_reset_model_preserves_observation_projection_on_rust_path(self) -> None:
        initial = density_from_ket((2**-0.5, 2**-0.5))
        request = ResetModelRequest(
            input_state=initial,
            reset_count=3,
            victim_measurement=True,
        )
        spec = ImperfectResetSpec(0.3, (0.9, 0.1))
        python_result = PhenomenologicalResetModel(spec).run(request)
        rust_result = PhenomenologicalResetModel(
            spec, kernel_backend="rust_dense_preview"
        ).run(request)
        np.testing.assert_allclose(
            rust_result.post_reset_state,
            python_result.post_reset_state,
            rtol=0.0,
            atol=2e-13,
        )
        self.assertAlmostEqual(
            rust_result.attacker_one_probability,
            python_result.attacker_one_probability,
            delta=2e-13,
        )

    def test_unknown_backend_is_rejected_even_when_reset_is_a_noop(self) -> None:
        state = ((1.0 + 0.0j, 0.0j), (0.0j, 0.0j))
        with self.assertRaisesRegex(ValueError, "unsupported reset kernel backend"):
            apply_local_imperfect_reset(
                state,
                1,
                0,
                ImperfectResetSpec(0.0),
                kernel_backend="silently_defaulted_backend",
            )

    def test_invalid_sequence_shape_and_probabilities_are_refused(self) -> None:
        state = ((1.0 + 0.0j, 0.0j), (0.0j, 0.0j))
        with self.assertRaisesRegex(ValueError, "normalized local state"):
            rust_apply_local_reset_sequence(
                state,
                subsystem_count=1,
                local_dimension=2,
                targets=(0,),
                success_probabilities=(0.4,),
                target_populations=((0.2, 0.2),),
                reset_count=1,
            )
        with self.assertRaisesRegex(ValueError, "targets must be unique"):
            rust_apply_local_reset_sequence(
                state,
                subsystem_count=1,
                local_dimension=2,
                targets=(0, 0),
                success_probabilities=(0.4, 0.4),
                target_populations=((1.0, 0.0), (1.0, 0.0)),
                reset_count=1,
            )

    def test_unknown_kernel_schema_version_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported reset sequence schema version"):
            yuragi_strider_rust.apply_local_reset_sequence_flat(
                "local_reset_sequence_v999",
                [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                1,
                2,
                [0],
                [0.4],
                [1.0, 0.0],
                1,
            )


if __name__ == "__main__":
    unittest.main()

"""Integration of the explicit Rust register engine with the existing runtime."""

import math
import sys
import unittest
from unittest.mock import patch

import numpy as np

from core.attack_simulation.circuit_program import CircuitExecutionOperation, CircuitInstruction, NATIVE_GATES
from core.attack_simulation.job_service import JobNotFoundError
from core.attack_simulation.kick_application import PlacedKick
from core.attack_simulation.shared_qpu_runtime import SharedQpuRuntimeConfig
from core.experimental_isolation_physics import ImperfectResetSpec, PhenomenologicalResetModel
from core.experimental_isolation_physics.idle_evolution import IndependentIdleModel, IdleQubitSpec
from core.experimental_isolation_physics.reset_model import PerQubitPhenomenologicalResetModel
from core.gates import density_from_ket
from tests.test_common_command_pipeline import pipeline
from tests.test_local_program_runtime import program_json, submit


class RustRegisterRuntimeTests(unittest.TestCase):
    def test_backend_is_explicitly_frozen_and_unknown_values_are_rejected(self):
        base = dict(reset_model_id="r", reset_model_configuration_id="c")
        self.assertNotIn("register_backend", SharedQpuRuntimeConfig(**base).to_manifest_config())
        self.assertEqual(SharedQpuRuntimeConfig(**base, register_backend="rust_register_v1")
                         .to_manifest_config()["register_backend"], "rust_register_v1")
        with self.assertRaisesRegex(ValueError, "unsupported register backend"):
            SharedQpuRuntimeConfig(**base, register_backend="unknown")

    def test_pre_measurement_matches_existing_runtime_with_idle_reset_noise_and_kicks(self):
        specs = (IdleQubitSpec(80.0, 31.0, 0.18), IdleQubitSpec(43.0, math.inf, 0.07))
        idle = IndependentIdleModel(specs, 2)
        ket = np.asarray([1, 1j, 0.5, -0.3j], dtype=complex)
        ket /= np.linalg.norm(ket)
        initial = density_from_ket(tuple(ket))
        h = ((2**-0.5, 2**-0.5), (2**-0.5, -(2**-0.5)))
        cx = ((1, 0, 0, 0), (0, 1, 0, 0), (0, 0, 0, 1), (0, 0, 1, 0))
        operation = CircuitExecutionOperation((
            CircuitInstruction("unitary", 0.5, unitary=h, qubits=(0,), depolarizing_probability=0.11),
            CircuitInstruction("reset", 0.0, qubits=(1,)),
            CircuitInstruction("delay", 1.5, qubits=(0,), wait_us=1.5),
            CircuitInstruction("unitary", 0.3, unitary=cx, qubits=(1, 0)),
            CircuitInstruction("measure", 0.0, qubits=(0, 1), classical_targets=(0, 1)),
        ), shots=1, sampling_seed=4, logical_qubits=2, classical_bits=2, initialization="inherit")
        kicks = (PlacedKick(0, 4, (0.1, 0.03, -0.02), (), 0.3),
                 PlacedKick(1, 9, (-0.12, 0.07, 0.02), ()),
                 PlacedKick(5, 4, (0.01, 0.0, 0.08), ()))
        reset_models = (
            PhenomenologicalResetModel(ImperfectResetSpec(0.31, (0.8, 0.2)), 0.02, 0.03),
            PerQubitPhenomenologicalResetModel((4, 9),
                (ImperfectResetSpec(0.2, (0.9, 0.1)), ImperfectResetSpec(0.6, (0.7, 0.3))),
                ((0.01, 0.02), (0.03, 0.04))),
        )
        for model in reset_models:
            with self.subTest(model=model.model_id):
                kwargs = dict(model=model, physical_qubit_ids=(4, 9), initial_density=initial,
                              idle_model=idle, repetition_delay_us=2.0)
                _, python_runtime, _, _ = pipeline(**kwargs)
                _, rust_runtime, _, _ = pipeline(**kwargs, register_backend="rust_register_v1")
                expected = python_runtime._run_before_measurement(operation, (0, 1), np.asarray(initial), 1, kicks)
                with patch.object(rust_runtime.state_store, "validate_state",
                                  wraps=rust_runtime.state_store.validate_state) as validate:
                    actual = rust_runtime._run_before_measurement(operation, (0, 1), np.asarray(initial), 1, kicks)
                self.assertEqual(validate.call_count, 1)
                np.testing.assert_allclose(actual[0], expected[0], atol=2e-13, rtol=0)
                self.assertEqual(actual[1:], expected[1:])

    def test_missing_extension_and_unknown_reset_model_fail_before_execution(self):
        with patch.dict(sys.modules, {"yuragi_strider_rust": None}):
            with self.assertRaisesRegex(RuntimeError, "Rust register backend is unavailable"):
                pipeline(register_backend="rust_register_v1")

        reference = PhenomenologicalResetModel(ImperfectResetSpec(0.5))
        class OtherModel:
            model_id = "other"
            model_version = "1"
            fidelity_level = "test"
            provenance = "test"
            model_mode = reference.model_mode
            configuration_id = "other-config"
            def run(self, request): return reference.run(request)
            def run_register(self, request): return reference.run_register(request)
        with self.assertRaisesRegex(ValueError, "does not support this reset model"):
            pipeline(model=OtherModel(), register_backend="rust_register_v1")

    def test_existing_terminal_executes_bell_job_and_keeps_tenant_isolation(self):
        runs = []
        for backend in ("python_dense", "rust_register_v1"):
            store, runtime, _, (attacker, victim) = pipeline(
                physical_qubit_ids=(8, 19), supported_gates=NATIVE_GATES, register_backend=backend,
            )
            attacker._client._service._sampling_salt = b"fixed-test-salt"
            circuit = program_json(2, [[{"type": "H", "targets": [0]}],
                                       [{"type": "CNOT", "controls": [0], "targets": [1]}]])
            job = submit(attacker, circuit, shots=12, request_id="same-request")
            runtime.run_next()
            counts = attacker.execute(f"job result {job.job_id}").result["counts"]
            with self.assertRaises(JobNotFoundError):
                victim.execute(f"job result {job.job_id}")
            runs.append((counts, np.asarray(store.read().density_matrix), store.read().device_time_us))
        self.assertEqual(runs[0][0], runs[1][0])
        np.testing.assert_allclose(runs[0][1], runs[1][1], atol=2e-13, rtol=0)
        self.assertEqual(runs[0][2], runs[1][2])


if __name__ == "__main__":
    unittest.main()

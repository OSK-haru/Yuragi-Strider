"""D69 native calibration, decomposition, channel and product-path checks."""

from dataclasses import replace
import json
import time
import unittest
from unittest.mock import patch

import numpy as np

from api.attack_device_snapshot import SNAPSHOT_DIRECTORY, load_ibm_kingston_snapshot
from api.attack_product_factory import IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL, ProductEnvironmentFactory
from api.attack_product_profiles import product_free_scenarios
from api.attack_sessions import WebOwnerContext
from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.program_request import CircuitProgram, ProgramRequestError, ProgramSubmitRequest
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import ScenarioMode, ScenarioPolicy, ScenarioSession
from core.register_operations import apply_local_depolarizing_replacement, apply_operand_operator
from tests.test_attack_product_factory import HOST_LIMITS
from tests.test_interleaved_product import POLL_SECONDS, await_reply


# Independent expected counts, not imported from the production decomposition.
COUNTS = {
    "Z": (1, 0, 0), "S": (1, 0, 0), "T": (1, 0, 0), "RZ": (1, 0, 0),
    "X": (0, 0, 1), "H": (2, 1, 0), "Y": (1, 0, 1),
    "RX": (3, 2, 0), "RY": (3, 2, 0),
}


def environment_for(snapshot, mode=IDLE_INPUT_CALIBRATION):
    definition = product_free_scenarios(snapshot)[mode]
    return ScenarioEnvironment(ScenarioSession(
        definition.template, ScenarioPolicy(ScenarioMode.FREE, allowed_operations=definition.allowed_operations),
        definition.assumptions,
    ))


def request_for(gates, *, width=1, initial=None):
    columns = [{"step": i, "gates": column} for i, column in enumerate(gates)]
    columns.append({"step": len(columns), "gates": [
        {"type": "MEASURE", "targets": [q], "classical_targets": [q]} for q in range(width)
    ]})
    return ProgramSubmitRequest(CircuitProgram.from_editor_json(json.dumps({
        "logical_qubits": width, "classical_bits": width,
        "initial_states": [0] * width if initial is None else initial, "columns": columns,
    })), "ibm_kingston", 1, "native-calibration")


def gate_document(name, target=0, theta=0.37):
    gate = {"type": name, "targets": [target]}
    if name in {"RX", "RY", "RZ"}:
        gate["params"] = {"theta_rad": theta}
    return gate


class SingleQubitCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = load_ibm_kingston_snapshot()

    def test_loader_preserves_each_qubit_native_values_and_units(self):
        source = json.loads((SNAPSHOT_DIRECTORY / "props_kingston.json").read_text(encoding="utf-8"))
        seen = set()
        for entry in source["gates"]:
            if entry["gate"] not in {"rz", "sx", "x"}:
                continue
            name, qid = entry["gate"], entry["qubits"][0]
            parameters = {p["name"]: p for p in entry["parameters"]}
            record = self.snapshot.single_qubit_calibrations[name, qid]
            self.assertEqual(parameters["gate_length"]["unit"], "ns")
            self.assertEqual(record.duration_us, parameters["gate_length"]["value"] / 1000)
            self.assertEqual(record.error, parameters["gate_error"]["value"])
            self.assertEqual(record.duration_us, 0.0 if name == "rz" else 0.032)
            seen.add((name, qid))
        self.assertEqual(seen, {(name, qid) for name in ("rz", "sx", "x") for qid in range(156)})

    def test_every_gate_uses_frozen_qubit_values_in_both_idle_modes(self):
        for mode in (IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL):
            env = environment_for(self.snapshot, mode)
            manifest = env.establish()
            compiler = LocalCircuitCompiler.from_manifest(manifest)
            native = manifest.qpu_service_config["circuit_execution"]["single_qubit_native_gates"]
            self.assertEqual(dict(compiler.single_qubit_gate_decompositions), {
                "Z": ("rz",), "S": ("rz",), "T": ("rz",), "RZ": ("rz",),
                "X": ("x",), "H": ("rz", "sx", "rz"), "Y": ("x", "rz"),
                "RX": ("rz", "sx", "rz", "sx", "rz"),
                "RY": ("rz", "sx", "rz", "sx", "rz"),
            })
            for logical, physical in enumerate(compiler.physical_qubit_ids):
                for name, (rz, sx, x) in COUNTS.items():
                    with self.subTest(mode=mode, physical=physical, gate=name):
                        instruction = next(i for i in compiler.compile(request_for(
                            [[gate_document(name, logical)]], width=logical + 1,
                        ), sampling_seed=1).instructions if i.kind == "unitary")
                        records = {n: self.snapshot.single_qubit_calibrations[n, physical] for n in ("rz", "sx", "x")}
                        expected = sum(count * records[n].duration_us for n, count in zip(("rz", "sx", "x"), (rz, sx, x)))
                        probability = 1 - (1 - 2 * records["sx"].error) ** sx * (1 - 2 * records["x"].error) ** x
                        self.assertAlmostEqual(instruction.duration_us, expected)
                        self.assertAlmostEqual(instruction.depolarizing_probability, probability)
                        self.assertEqual(native[str(physical)]["sx"]["gate_error"], records["sx"].error)
                        if name in {"Z", "S", "T", "RZ"}:
                            self.assertEqual(instruction.duration_us, 0)
                            self.assertEqual(instruction.depolarizing_probability, 0)
            self.assertTrue(any("D69" in a.rationale for a in manifest.assumptions))

    def test_dedicated_region_compiles_against_its_own_physical_calibration(self):
        manifest = environment_for(self.snapshot).establish()
        compiler = LocalCircuitCompiler.from_manifest(manifest)
        alternate = tuple(manifest.qpu_service_config["dedicated_region"]["physical_qubit_ids"])
        self.assertFalse(set(alternate) & set(compiler.physical_qubit_ids))
        request = request_for([[gate_document("X", 0)]], width=1)
        primary = next(i for i in compiler.compile(request, sampling_seed=1).instructions
                       if i.kind == "unitary")
        dedicated = next(i for i in compiler.compile(
            request, sampling_seed=1, physical_qubit_ids=alternate,
        ).instructions if i.kind == "unitary")
        primary_r = self.snapshot.single_qubit_calibrations["x", compiler.physical_qubit_ids[0]].error
        alternate_r = self.snapshot.single_qubit_calibrations["x", alternate[0]].error
        self.assertAlmostEqual(primary.depolarizing_probability, 2 * primary_r)
        self.assertAlmostEqual(dedicated.depolarizing_probability, 2 * alternate_r)
        self.assertNotEqual(primary_r, alternate_r)
        with self.assertRaises(ProgramRequestError):
            compiler.compile(request, sampling_seed=1, physical_qubit_ids=(155,))

    def test_heterogeneous_qubit_durations_parallel_max_and_preparation_are_calibrated(self):
        records = dict(self.snapshot.single_qubit_calibrations)
        original = LocalCircuitCompiler.from_manifest(environment_for(self.snapshot).establish())
        for index, qid in enumerate(original.physical_qubit_ids):
            for native in ("sx", "x"):
                records[native, qid] = replace(records[native, qid], duration_us=0.1 * (index + 1), error=0.01 * (index + 1))
        compiler = LocalCircuitCompiler.from_manifest(environment_for(replace(
            self.snapshot, single_qubit_calibrations=records,
        )).establish())
        for target in (0, 1):
            operation = compiler.compile(request_for([[gate_document("RX", target)]], width=2), sampling_seed=1)
            instruction = next(i for i in operation.instructions if i.kind == "unitary")
            self.assertAlmostEqual(instruction.duration_us, 0.2 * (target + 1))
            self.assertAlmostEqual(instruction.depolarizing_probability, 1 - (1 - 0.02 * (target + 1)) ** 2)
        parallel = compiler.compile(request_for([[gate_document("X", 0), gate_document("X", 1)]], width=2), sampling_seed=1)
        self.assertAlmostEqual(sum(i.duration_us for i in parallel.instructions if i.kind == "unitary"), 0.2)
        prepared = compiler.compile(request_for([], width=2, initial=["+", 1]), sampling_seed=1)
        gates = [i for i in prepared.instructions if i.kind == "unitary"]
        self.assertEqual([i.qubits for i in gates], [(1,), (0,)])
        np.testing.assert_allclose([i.duration_us for i in gates], [0.2, 0.1])
        np.testing.assert_allclose([i.depolarizing_probability for i in gates], [0.04, 0.02])

    def test_single_qubit_channel_average_infidelity_matches_native_gate_error(self):
        compiler = LocalCircuitCompiler.from_manifest(environment_for(self.snapshot).establish())
        phi = np.array([1, 0, 0, 1], dtype=complex) / np.sqrt(2)
        choi = np.outer(phi, phi.conj())
        for logical, qid in enumerate(compiler.physical_qubit_ids):
            for name, native in (("H", "sx"), ("X", "x")):
                with self.subTest(qubit=qid, gate=name):
                    instruction = next(i for i in compiler.compile(request_for(
                        [[gate_document(name, logical)]], width=logical + 1,
                    ), sampling_seed=1).instructions if i.kind == "unitary")
                    output = apply_local_depolarizing_replacement(choi, (1,), instruction.depolarizing_probability)
                    entanglement_fidelity = np.vdot(phi, output @ phi).real
                    average_fidelity = (2 * entanglement_fidelity + 1) / 3
                    self.assertAlmostEqual(1 - average_fidelity, self.snapshot.single_qubit_calibrations[native, qid].error, places=12)

    def test_two_sx_channels_compose_exactly_even_with_entanglement_and_zero_angle(self):
        compiler = LocalCircuitCompiler.from_manifest(environment_for(self.snapshot).establish())
        qid = compiler.physical_qubit_ids[0]
        probability = 2 * self.snapshot.single_qubit_calibrations["sx", qid].error
        ket = np.array([1, 1j, -1, 2j], dtype=complex) / np.sqrt(7)
        state = np.outer(ket, ket.conj())
        for name in ("RX", "RY"):
            for theta in (0.0, 0.37, np.pi):
                instruction = next(i for i in compiler.compile(request_for(
                    [[gate_document(name, theta=theta)]],
                ), sampling_seed=1).instructions if i.kind == "unitary")
                ideal = apply_operand_operator(state, instruction.unitary, (0,))
                repeated = apply_local_depolarizing_replacement(
                    apply_local_depolarizing_replacement(ideal, (0,), probability), (0,), probability,
                )
                combined = apply_local_depolarizing_replacement(ideal, (0,), instruction.depolarizing_probability)
                np.testing.assert_allclose(combined, repeated, atol=1e-14)
                self.assertAlmostEqual(instruction.duration_us, 2 * self.snapshot.single_qubit_calibrations["sx", qid].duration_us)

    def test_missing_or_unrepresentable_native_values_are_rejected_without_fallback(self):
        records = dict(self.snapshot.single_qubit_calibrations)
        compiler = LocalCircuitCompiler.from_manifest(environment_for(self.snapshot).establish())
        qid = compiler.physical_qubit_ids[0]
        del records["sx", qid]
        with self.assertRaisesRegex(ValueError, "missing native"):
            product_free_scenarios(replace(self.snapshot, single_qubit_calibrations=records))
        raw = {key: {native: dict(entry) for native, entry in values.items()}
               for key, values in compiler.single_qubit_native_gates.items()}
        for bad in (-0.1, 0.51, float("nan"), True):
            raw[str(qid)]["sx"]["gate_error"] = bad
            with self.assertRaises(ValueError):
                replace(compiler, single_qubit_native_gates=raw)
        with self.assertRaisesRegex(ValueError, "declared together"):
            replace(compiler, single_qubit_gate_decompositions=None)
        raw[str(qid)]["sx"]["gate_error"] = compiler.single_qubit_native_gates[str(qid)]["sx"]["gate_error"]
        raw[str(qid)]["rz"]["gate_error"] = 0.25
        no_rz_noise = replace(compiler, single_qubit_native_gates=raw)
        for name in ("Z", "S", "T", "RZ"):
            instruction = next(i for i in no_rz_noise.compile(request_for(
                [[gate_document(name)]],
            ), sampling_seed=1).instructions if i.kind == "unitary")
            self.assertEqual(instruction.depolarizing_probability, 0.0)

    def test_product_path_applies_compiled_single_qubit_channels(self):
        for mode in (IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL):
            environment = environment_for(self.snapshot, mode)
            owner = WebOwnerContext()
            product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
            try:
                controller = product.controller
                request = request_for([[gate_document("X")], [gate_document("RZ")], [gate_document("RX")]])
                with patch("core.attack_simulation.shared_qpu_runtime.apply_local_depolarizing_replacement",
                           wraps=apply_local_depolarizing_replacement) as channel:
                    receipt = controller.submit(product.attacker_credential, request, owner_context=owner)
                    reply = await_reply(controller, product.attacker_credential, receipt, owner)
                    self.assertIsNone(reply.error_code)
                    credential, job_id = product.attacker_credential, reply.value.job_id
                    deadline = time.monotonic() + POLL_SECONDS
                    while await_reply(controller, credential, controller.status(
                            credential, job_id, owner_context=owner), owner).value.status.value != "succeeded":
                        self.assertLess(time.monotonic(), deadline, "the job did not finish")
                        time.sleep(0.05)
                self.assertIsNone(reply.error_code)
                qid = environment.establish().qpu_service_config["retained_region"]["physical_qubit_ids"][0]
                expected = [2 * self.snapshot.single_qubit_calibrations["x", qid].error,
                            1 - (1 - 2 * self.snapshot.single_qubit_calibrations["sx", qid].error) ** 2]
                self.assertEqual(channel.call_count, 2)
                np.testing.assert_allclose([call.args[2] for call in channel.call_args_list], expected)
            finally:
                product.close()


if __name__ == "__main__":
    unittest.main()

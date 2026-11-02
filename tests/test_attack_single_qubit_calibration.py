"""D69 native calibration, decomposition, channel and product-path checks."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from api.attack_device_snapshot import SNAPSHOT_DIRECTORY, load_device_snapshot, default_device_snapshot
from api.attack_product_factory import IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL, ProductEnvironmentFactory
from api.attack_product_profiles import product_free_scenarios
from api.attack_sessions import WebOwnerContext
from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.backend_declaration import BackendDeclaration
from core.attack_simulation.program_request import CircuitProgram, ProgramRequestError, ProgramSubmitRequest
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.scenario_policy import ScenarioMode, ScenarioPolicy, ScenarioSession
from core.gates import ecr_matrix, expand_cnot, expand_single_qubit_gate, rx_matrix, S, X
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


def request_for(gates, *, width=1, initial=None, device_id="ibm_kingston"):
    columns = [{"step": i, "gates": column} for i, column in enumerate(gates)]
    columns.append({"step": len(columns), "gates": [
        {"type": "MEASURE", "targets": [q], "classical_targets": [q]} for q in range(width)
    ]})
    return ProgramSubmitRequest(CircuitProgram.from_editor_json(json.dumps({
        "logical_qubits": width, "classical_bits": width,
        "initial_states": [0] * width if initial is None else initial, "columns": columns,
    })), device_id, 1, "native-calibration")


def gate_document(name, target=0, theta=0.37):
    gate = {"type": name, "targets": [target]}
    if name in {"RX", "RY", "RZ"}:
        gate["params"] = {"theta_rad": theta}
    return gate


def synthetic_snapshot(directory, *, device_id, declaration, single_gate_rename=None,
                       remove_single_gates=(), two_gate_rename=None, directed_edges=False):
    """Copy a frozen snapshot, apply a structural calibration edit, then rebuild its hash manifest."""
    target = Path(directory)
    source = (SNAPSHOT_DIRECTORY / "SOURCE.md").read_text(encoding="utf-8")
    names = ("props_kingston.json", "conf_kingston.json", "eligibility_policy.json")
    for name in names:
        (target / name).write_bytes((SNAPSHOT_DIRECTORY / name).read_bytes())
    props_path, conf_path = target / names[0], target / names[1]
    props = json.loads(props_path.read_text(encoding="utf-8"))
    conf = json.loads(conf_path.read_text(encoding="utf-8"))
    props["backend_name"] = conf["backend_name"] = device_id
    gates = []
    for record in props["gates"]:
        gate = record["gate"]
        if gate in remove_single_gates:
            continue
        updated = dict(record)
        if gate in (single_gate_rename or {}):
            updated["gate"] = single_gate_rename[gate]
        if gate == "cz" and two_gate_rename:
            updated["gate"] = two_gate_rename
        gates.append(updated)
    props["gates"] = gates
    if two_gate_rename and directed_edges:
        # Keep one orientation per physical pair; the snapshot's calibration records
        # are filtered to exactly the same directed topology below.
        directed = {tuple(edge) for edge in conf["coupling_map"]}
        conf["coupling_map"] = [list(edge) for edge in sorted(
            edge for edge in directed if edge[0] < edge[1])]
        allowed = {tuple(edge) for edge in conf["coupling_map"]}
        props["gates"] = [record for record in props["gates"]
                          if record["gate"] != two_gate_rename or tuple(record["qubits"]) in allowed]
    props_path.write_text(json.dumps(props, separators=(",", ":")), encoding="utf-8")
    conf_path.write_text(json.dumps(conf, separators=(",", ":")), encoding="utf-8")
    hashes = {"props_kingston.json": hashlib.sha256(props_path.read_bytes()).hexdigest(),
              "conf_kingston.json": hashlib.sha256(conf_path.read_bytes()).hexdigest()}
    lines = []
    for line in source.splitlines():
        for name, digest in hashes.items():
            if f"`{name}`" in line:
                # Rebuild that manifest row from its structured columns.
                columns = [part.strip() for part in line.strip().strip("|").split("|")]
                line = f"| {columns[0]} | {columns[1]} | `{digest}` |"
                break
        lines.append(line)
    (target / "SOURCE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_device_snapshot(target, device_id=device_id, backend_declaration=declaration)


class SingleQubitCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = default_device_snapshot()

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

    def test_synthetic_backend_snapshot_compiles_and_executes_without_native_x(self):
        with tempfile.TemporaryDirectory() as temporary:
            declaration = BackendDeclaration(("rz", "sx"), ("rz",), {"cz": False})
            snapshot = synthetic_snapshot(temporary, device_id="synthetic_backend", declaration=declaration,
                                          remove_single_gates=("x",))

        definition = product_free_scenarios(snapshot)[IDLE_INPUT_CALIBRATION]
        environment = ScenarioEnvironment(ScenarioSession(
            definition.template, ScenarioPolicy(ScenarioMode.FREE, allowed_operations=definition.allowed_operations),
            definition.assumptions,
        ))
        compiler = LocalCircuitCompiler.from_manifest(environment.establish())
        self.assertNotIn("x", compiler.backend_declaration.native_single_qubit_gates)
        operation = compiler.compile(request_for([[gate_document("X")]], device_id="synthetic_backend"),
                                     sampling_seed=3)
        instruction = next(item for item in operation.instructions if item.kind == "unitary")
        self.assertEqual([step.name for step in instruction.natives if not step.is_virtual], ["sx", "sx"])
        qid = compiler.physical_qubit_ids[0]
        r = snapshot.single_qubit_calibrations["sx", qid].error
        self.assertAlmostEqual(instruction.depolarizing_probability, 1.0 - (1.0 - 2.0 * r) ** 2)

        owner = WebOwnerContext()
        product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
        try:
            receipt = product.controller.submit(
                product.attacker_credential,
                request_for([[gate_document("X")]], device_id="synthetic_backend"), owner_context=owner,
            )
            reply = await_reply(product.controller, product.attacker_credential, receipt, owner)
            self.assertIsNone(reply.error_code)
            job_id = reply.value.job_id
            deadline = time.monotonic() + POLL_SECONDS
            while True:
                status = await_reply(product.controller, product.attacker_credential,
                                     product.controller.status(product.attacker_credential, job_id,
                                                               owner_context=owner), owner)
                if status.value.status.value == "succeeded":
                    break
                self.assertLess(time.monotonic(), deadline, "synthetic-backend job did not execute")
                time.sleep(0.05)
            result = await_reply(product.controller, product.attacker_credential,
                                 product.controller.result(product.attacker_credential, job_id,
                                                           owner_context=owner), owner)
            self.assertIsNone(result.error_code)
            self.assertEqual(sum(result.value.result["counts"].values()), 1)
        finally:
            product.close()

    def test_synthetic_backend_without_virtual_gate_applies_x_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            declaration = BackendDeclaration(("sx", "x"), (), {"cz": False})
            snapshot = synthetic_snapshot(temporary, device_id="synthetic_no_virtual",
                                          declaration=declaration, remove_single_gates=("rz",))
        environment = environment_for(snapshot)
        compiler = LocalCircuitCompiler.from_manifest(environment.establish())
        operation = compiler.compile(request_for([[gate_document("X")]], device_id="synthetic_no_virtual"),
                                     sampling_seed=7)
        instruction = next(item for item in operation.instructions if item.kind == "unitary")
        qid = compiler.physical_qubit_ids[0]
        self.assertEqual([step.name for step in instruction.natives], ["x"])
        self.assertAlmostEqual(instruction.depolarizing_probability,
                               2.0 * snapshot.single_qubit_calibrations["x", qid].error)
        owner = WebOwnerContext()
        product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
        try:
            receipt = product.controller.submit(product.attacker_credential,
                request_for([[gate_document("X")]], device_id="synthetic_no_virtual"), owner_context=owner)
            reply = await_reply(product.controller, product.attacker_credential, receipt, owner)
            self.assertIsNone(reply.error_code)
            job_id = reply.value.job_id
            deadline = time.monotonic() + POLL_SECONDS
            while True:
                status = await_reply(product.controller, product.attacker_credential,
                    product.controller.status(product.attacker_credential, job_id, owner_context=owner), owner)
                if status.value.status.value == "succeeded":
                    break
                self.assertLess(time.monotonic(), deadline, "no-virtual backend job did not execute")
                time.sleep(0.05)
            result = await_reply(product.controller, product.attacker_credential,
                product.controller.result(product.attacker_credential, job_id, owner_context=owner), owner)
            self.assertIsNone(result.error_code)
            self.assertEqual(sum(result.value.result["counts"].values()), 1)
        finally:
            product.close()

    def test_virtual_gate_alias_is_used_as_frame_rotation(self):
        with tempfile.TemporaryDirectory() as temporary:
            declaration = BackendDeclaration(("phase", "sx", "x"), ("phase",), {"cz": False})
            snapshot = synthetic_snapshot(temporary, device_id="synthetic_phase_frame",
                declaration=declaration, single_gate_rename={"rz": "phase"})
        compiler = LocalCircuitCompiler.from_manifest(environment_for(snapshot).establish())
        operation = compiler.compile(request_for([[gate_document("RZ")]],
            device_id="synthetic_phase_frame"), sampling_seed=11)
        instruction = next(item for item in operation.instructions if item.kind == "unitary")
        self.assertEqual([step.name for step in instruction.natives], ["phase"])
        self.assertTrue(instruction.natives[0].is_virtual)
        self.assertEqual(instruction.natives[0].duration_us, 0.0)
        self.assertEqual(instruction.depolarizing_probability, 0.0)

    def test_declared_pulse_angle_names_drive_the_compiled_pulse_steps(self):
        with tempfile.TemporaryDirectory() as temporary:
            declaration = BackendDeclaration(("rz", "half", "pi"), ("rz",), {"cz": False},
                                             {"half": np.pi / 2.0, "pi": np.pi})
            snapshot = synthetic_snapshot(temporary, device_id="synthetic_pulse_alias",
                declaration=declaration, single_gate_rename={"sx": "half", "x": "pi"})
        compiler = LocalCircuitCompiler.from_manifest(environment_for(snapshot).establish())
        operation = compiler.compile(request_for([[gate_document("H")]],
            device_id="synthetic_pulse_alias"), sampling_seed=12)
        instruction = next(item for item in operation.instructions if item.kind == "unitary")
        self.assertEqual([step.name for step in instruction.natives], ["rz", "half", "rz"])
        self.assertEqual(instruction.natives[1].angle_rad, np.pi / 2.0)

    def test_directed_ecr_snapshot_compiles_and_executes_with_its_calibration(self):
        with tempfile.TemporaryDirectory() as temporary:
            declaration = BackendDeclaration(("rz", "sx", "x"), ("rz",), {"ecr": True})
            snapshot = synthetic_snapshot(temporary, device_id="synthetic_ecr", declaration=declaration,
                                          two_gate_rename="ecr", directed_edges=True)
        self.assertTrue(snapshot.directed_coupling_map)
        self.assertEqual(set(snapshot.two_qubit_calibrations), {"ecr"})
        environment = environment_for(snapshot)
        compiler = LocalCircuitCompiler.from_manifest(environment.establish())
        edge_id = next(iter(compiler.two_qubit_gate_calibrations["ecr"]))
        edge = tuple(map(int, edge_id.split(":")))
        self.assertEqual(snapshot.two_qubit_calibrations["ecr"][edge].physical_qubit_ids, edge)
        allocation = compiler.physical_qubit_ids
        logical_edge = [allocation.index(qid) for qid in edge]
        request = request_for([[{"type": "ECR", "targets": logical_edge}]], width=len(allocation),
                              device_id="synthetic_ecr")
        operation = compiler.compile(request, sampling_seed=13)
        instruction = next(item for item in operation.instructions if item.kind == "unitary")
        calibration = compiler.two_qubit_gate_calibrations["ecr"][edge_id]
        self.assertEqual(instruction.duration_us, calibration["duration_us"])
        self.assertEqual(instruction.depolarizing_probability, calibration["depolarizing_probability"])
        cnot_request = request_for([[{"type": "CNOT", "controls": [logical_edge[0]],
                                      "targets": [logical_edge[1]]}]], width=len(allocation),
                                   device_id="synthetic_ecr")
        cnot_operation = compiler.compile(cnot_request, sampling_seed=14)
        self.assertEqual(sum(len(item.qubits) == 2 for item in cnot_operation.instructions
                             if item.kind == "unitary"), 1)
        self.assertTrue(any(item.kind == "unitary" and len(item.qubits) == 1
                            for item in cnot_operation.instructions))
        cnot_unitary = (np.asarray(ecr_matrix())
                        @ np.asarray(expand_single_qubit_gate(rx_matrix(np.pi / 2.0), 1, 2))
                        @ np.asarray(expand_single_qubit_gate(X, 0, 2))
                        @ np.asarray(expand_single_qubit_gate(S, 0, 2)))
        expected_cnot = np.asarray(expand_cnot(0, 1, 2, control_value=1))
        self.assertAlmostEqual(abs(np.trace(expected_cnot.conj().T @ cnot_unitary) / 4.0), 1.0)
        request = request_for([[{"type": "CNOT", "controls": [logical_edge[0]],
                                 "targets": [logical_edge[1]]}],
                               [{"type": "ECR", "targets": logical_edge}]], width=len(allocation),
                              device_id="synthetic_ecr")
        owner = WebOwnerContext()
        product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
        try:
            receipt = product.controller.submit(product.attacker_credential, request, owner_context=owner)
            reply = await_reply(product.controller, product.attacker_credential, receipt, owner)
            self.assertIsNone(reply.error_code)
            job_id = reply.value.job_id
            deadline = time.monotonic() + POLL_SECONDS
            while True:
                status = await_reply(product.controller, product.attacker_credential,
                    product.controller.status(product.attacker_credential, job_id, owner_context=owner), owner)
                if status.value.status.value == "succeeded":
                    break
                self.assertLess(time.monotonic(), deadline, "directed-ECR job did not execute")
                time.sleep(0.05)
            result = await_reply(product.controller, product.attacker_credential,
                product.controller.result(product.attacker_credential, job_id, owner_context=owner), owner)
            self.assertIsNone(result.error_code)
            self.assertEqual(sum(result.value.result["counts"].values()), 1)
        finally:
            product.close()

    def test_directed_ecr_snapshot_rejects_calibration_outside_coupling_direction(self):
        with tempfile.TemporaryDirectory() as temporary:
            declaration = BackendDeclaration(("rz", "sx", "x"), ("rz",), {"ecr": True})
            target = Path(temporary)
            # Build a valid ECR fixture, then reverse one calibrated record without
            # changing the one-way coupling map and regenerate the source manifest.
            synthetic_snapshot(temporary, device_id="synthetic_ecr", declaration=declaration,
                              two_gate_rename="ecr", directed_edges=True)
            props_path = target / "props_kingston.json"
            props = json.loads(props_path.read_text(encoding="utf-8"))
            record = next(item for item in props["gates"] if item["gate"] == "ecr")
            record["qubits"].reverse()
            props_path.write_text(json.dumps(props, separators=(",", ":")), encoding="utf-8")
            source_path = target / "SOURCE.md"
            lines = source_path.read_text(encoding="utf-8").splitlines()
            digest = hashlib.sha256(props_path.read_bytes()).hexdigest()
            rebuilt = []
            for line in lines:
                if "`props_kingston.json`" in line:
                    columns = [part.strip() for part in line.strip().strip("|").split("|")]
                    line = f"| {columns[0]} | {columns[1]} | `{digest}` |"
                rebuilt.append(line)
            source_path.write_text("\n".join(rebuilt) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "direction"):
                load_device_snapshot(target, device_id="synthetic_ecr", backend_declaration=declaration)

    def test_invalid_backend_declarations_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "subset"):
            BackendDeclaration(("sx",), ("rz",), {"cz": False})
        with self.assertRaisesRegex(ValueError, "directed"):
            BackendDeclaration(("rz", "sx", "x"), ("rz",), {"ecr": False})
        with self.assertRaises(TypeError):
            load_device_snapshot(SNAPSHOT_DIRECTORY, device_id="ibm_kingston")
        compiler = LocalCircuitCompiler.from_manifest(environment_for(self.snapshot).establish())
        with self.assertRaisesRegex(ValueError, "not declared"):
            replace(compiler, backend_declaration=BackendDeclaration(
                ("rz", "sx", "x"), ("rz",), {"ecr": True},
            ))
        no_x_sequence = dict(compiler.single_qubit_gate_decompositions)
        no_x_sequence["X"] = ("x", "x")
        with self.assertRaisesRegex(ValueError, "cannot be executed"):
            replace(compiler, single_qubit_gate_decompositions=no_x_sequence)

    def test_product_path_applies_compiled_single_qubit_channels(self):
        for mode in (IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL):
            environment = environment_for(self.snapshot, mode)
            owner = WebOwnerContext()
            product = ProductEnvironmentFactory(HOST_LIMITS).assemble(environment, owner)
            try:
                controller = product.controller
                request = request_for([[gate_document("X")], [gate_document("RZ")], [gate_document("RX")]])
                runtime = product.controller._world._executor.runtime
                self.assertEqual(runtime.config.register_backend, "rust_register_v1")
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
            finally:
                product.close()


if __name__ == "__main__":
    unittest.main()

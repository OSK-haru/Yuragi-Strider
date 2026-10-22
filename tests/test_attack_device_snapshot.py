"""Integrity and product-default checks for the recorded device snapshot."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np

from api.attack_device_snapshot import (
    SNAPSHOT_DIRECTORY, deterministic_connected_region, default_device_snapshot,
    load_device_snapshot,
)
from core.attack_simulation.backend_declaration import KINGSTON_BACKEND
from api.attack_product_profiles import product_free_scenarios, standard_snapshot_initial_values
from api.attack_product_factory import IDLE_INPUT_CALIBRATION, IDLE_INPUT_PHYSICAL
from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.program_request import CircuitProgram, ProgramRequestError, ProgramSubmitRequest
from core.register_operations import apply_local_depolarizing_replacement


class DeviceSnapshotTests(unittest.TestCase):
    def test_source_hash_mismatch_rejects_snapshot_without_touching_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            for name in ("SOURCE.md", "props_kingston.json", "conf_kingston.json", "eligibility_policy.json"):
                shutil.copyfile(SNAPSHOT_DIRECTORY / name, target / name)
            props = target / "props_kingston.json"
            props.write_bytes(props.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                load_device_snapshot(target, device_id="ibm_kingston", backend_declaration=KINGSTON_BACKEND)

    def test_source_manifest_accepts_crlf_line_endings(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            for name in ("SOURCE.md", "props_kingston.json", "conf_kingston.json", "eligibility_policy.json"):
                shutil.copyfile(SNAPSHOT_DIRECTORY / name, target / name)
            source = target / "SOURCE.md"
            source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
            snapshot = load_device_snapshot(target, device_id="ibm_kingston", backend_declaration=KINGSTON_BACKEND)
        self.assertEqual(snapshot.n_qubits, 156)
        self.assertEqual(len(snapshot.undirected_coupling_edges), 176)

    def test_snapshot_counts_per_qubit_values_and_excluded_inventory(self):
        snapshot = default_device_snapshot()
        self.assertEqual(snapshot.n_qubits, 156)
        self.assertEqual(len(snapshot.directed_coupling_map), 352)
        self.assertEqual(len(snapshot.undirected_coupling_edges), 176)
        self.assertEqual(len(snapshot.cz_calibrations), 176)
        self.assertIn("attached_to_cz_error_at_or_above_threshold:146-147",
                      snapshot.ineligible_qubit_reasons[146])
        self.assertIsNone(snapshot.qubits[146].t1_us)
        self.assertIsNone(snapshot.qubits[146].t2_us)

        region = tuple(sorted(deterministic_connected_region(snapshot, 6)))
        self.assertEqual(len(region), 6)
        self.assertEqual(len(set(region)), 6)
        self.assertFalse(set(region) & set(snapshot.ineligible_qubit_reasons))
        alternate = tuple(sorted(deterministic_connected_region(snapshot, 6,
            excluded_qubit_ids=frozenset(region))))

        profiles = product_free_scenarios(snapshot)
        calibration = profiles[IDLE_INPUT_CALIBRATION].template.qpu_service_config
        physical = profiles[IDLE_INPUT_PHYSICAL].template.qpu_service_config
        self.assertEqual(tuple(calibration["dedicated_region"]["physical_qubit_ids"]), alternate)
        self.assertFalse(set(region) & set(alternate))
        alternate_idle = calibration["dedicated_region"]["idle_dynamics"]["qubits"]
        alternate_reset = calibration["dedicated_region"]["reset_model"]["qubits"]
        for qid in alternate:
            source = snapshot.qubits[qid]
            self.assertEqual(alternate_idle[str(qid)]["t1_us"], source.t1_us)
            self.assertEqual(alternate_idle[str(qid)]["t2_us"], source.t2_us)
            self.assertEqual(alternate_reset[str(qid)]["observed_one_given_zero"], source.p_one_given_zero)
            self.assertEqual(alternate_reset[str(qid)]["observed_zero_given_one"], source.p_zero_given_one)
        self.assertIsNone(physical["dedicated_region"]["idle_dynamics"])
        idle_qubits = calibration["idle_dynamics"]["qubits"]
        for qid in region:
            record = snapshot.qubits[qid]
            self.assertEqual(idle_qubits[str(qid)]["t1_us"], record.t1_us)
            self.assertEqual(idle_qubits[str(qid)]["t2_us"], record.t2_us)
            reset = calibration["reset_model"]["qubits"][str(qid)]
            self.assertEqual(reset["observed_one_given_zero"], record.p_one_given_zero)
            self.assertEqual(reset["observed_zero_given_one"], record.p_zero_given_one)
            self.assertAlmostEqual(reset["success_probability"],
                                   1.0 - (record.p_one_given_zero + record.p_zero_given_one) / 2.0)
        self.assertNotIn("qubits", physical["idle_dynamics"])
        self.assertIn("physical_inputs", physical["idle_dynamics"])

        inventory = standard_snapshot_initial_values(snapshot)
        self.assertEqual(inventory["qubit_count"], 156)
        self.assertEqual(inventory["directed_coupling_count"], 352)
        self.assertEqual(inventory["undirected_edge_count"], 176)
        self.assertFalse(inventory["qubits"]["146"]["usable_for_placement"])
        self.assertEqual(inventory["qubits"]["146"]["placement_status"], "unusable")
        self.assertIsNone(inventory["qubits"]["146"]["T1_us"])
        self.assertTrue(inventory["qubits"]["146"]["unusable_reasons"])
        self.assertEqual(inventory["eligibility_policy"]["readout_error_exclude_above"], 0.1)
        self.assertEqual(inventory["eligibility_policy"]["cz_error_exclude_at_or_above"], 0.5)
        for qid, source in snapshot.qubits.items():
            public = inventory["qubits"][str(qid)]
            self.assertEqual(public["T1_us"], source.t1_us)
            self.assertEqual(public["T2_us"], source.t2_us)
            self.assertEqual(public["readout_error"], source.readout_error)
            self.assertEqual(public["measurement_duration_us"], source.measurement_duration_us)
            self.assertEqual(public["reset_duration_us"], source.reset_duration_us)
        self.assertEqual(len(inventory["cz_calibrations"]), 176)
        self.assertNotIn("2026-08-03", json.dumps(inventory))

    def test_disjoint_connected_six_qubit_regions_use_eligible_snapshot_qubits(self):
        snapshot = default_device_snapshot()
        first = deterministic_connected_region(snapshot, 6)
        second = deterministic_connected_region(
            snapshot, 6, excluded_qubit_ids=frozenset(first),
        )
        self.assertEqual(len(second), 6)
        self.assertFalse(set(first) & set(second))
        self.assertTrue(set(second) <= set(snapshot.usable_qubit_ids))
        self.assertEqual(second, deterministic_connected_region(
            snapshot, 6, excluded_qubit_ids=frozenset(first),
        ))
        edges = {frozenset(edge) for edge in snapshot.undirected_coupling_edges}
        reachable = {second[0]}
        while True:
            expanded = reachable | {
                qid for qid in second if any(frozenset((qid, member)) in edges for member in reachable)
            }
            if expanded == reachable:
                break
            reachable = expanded
        self.assertEqual(reachable, set(second))

    def test_compiled_cz_uses_selected_snapshot_edge_duration_and_error(self):
        snapshot = default_device_snapshot()
        region = deterministic_connected_region(snapshot, 6)
        pair = tuple(sorted(region[:2]))
        expected = snapshot.cz_calibrations[pair]
        definition = product_free_scenarios(snapshot)[IDLE_INPUT_CALIBRATION]
        manifest = definition.template
        from core.attack_simulation.scenario_environment import ScenarioEnvironment
        from core.attack_simulation.scenario_policy import ScenarioMode, ScenarioPolicy, ScenarioSession

        environment = ScenarioEnvironment(ScenarioSession(
            manifest, ScenarioPolicy(ScenarioMode.FREE, allowed_operations=definition.allowed_operations),
            definition.assumptions,
        ))
        frozen = environment.establish()
        compiler = LocalCircuitCompiler.from_manifest(frozen)
        program_document = {
            "logical_qubits": 2, "classical_bits": 2, "initial_states": [0, 0],
            "columns": [
                {"step": 0, "gates": [{"type": "CZ", "controls": [0], "targets": [1]}]},
                {"step": 1, "gates": [
                    {"type": "MEASURE", "targets": [0], "classical_targets": [0]},
                    {"type": "MEASURE", "targets": [1], "classical_targets": [1]},
                ]},
            ],
        }
        program = CircuitProgram.from_editor_json(json.dumps(program_document))
        operation = compiler.compile(
            ProgramSubmitRequest(program, "ibm_kingston", 1, "snapshot-cz"), sampling_seed=1,
        )
        cz_instruction = next(item for item in operation.instructions if item.kind == "unitary"
                              and item.depolarizing_probability > 0.0)
        self.assertAlmostEqual(cz_instruction.duration_us, expected.duration_us)
        self.assertAlmostEqual(cz_instruction.depolarizing_probability, expected.error * 4.0 / 3.0)
        cnot_document = json.loads(json.dumps(program_document))
        cnot_document["columns"][0]["gates"][0] = {
            "type": "CNOT", "controls": [0], "targets": [1],
        }
        cnot_operation = compiler.compile(
            ProgramSubmitRequest(CircuitProgram.from_editor_json(json.dumps(cnot_document)),
                                  "ibm_kingston", 1, "snapshot-cnot"),
            sampling_seed=1,
        )
        self.assertEqual(sum(item.kind == "unitary" for item in cnot_operation.instructions), 3)

    def test_snapshot_cz_gate_error_matches_average_infidelity_after_d4_conversion(self):
        snapshot = default_device_snapshot()
        region = deterministic_connected_region(snapshot, 6)
        pair = tuple(sorted(region[:2]))
        gate_error = snapshot.cz_calibrations[pair].error
        probability = gate_error * 4.0 / 3.0

        # Normalized Choi vector |Phi_4> on a four-dimensional reference and
        # two-qubit system. Its overlap gives entanglement fidelity exactly.
        phi = np.zeros(16, dtype=complex)
        phi[[0, 5, 10, 15]] = 0.5
        choi = np.outer(phi, phi.conj())
        output = apply_local_depolarizing_replacement(choi, (2, 3), probability)
        entanglement_fidelity = float(np.vdot(phi, output @ phi).real)
        average_fidelity = (4.0 * entanglement_fidelity + 1.0) / 5.0
        self.assertAlmostEqual(1.0 - average_fidelity, gate_error, places=12)


if __name__ == "__main__":
    unittest.main()

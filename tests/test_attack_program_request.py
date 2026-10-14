"""Compatibility and safety checks for Circuit Studio JSON as program input."""

from copy import deepcopy
import json
import unittest

from core.attack_simulation.program_request import (
    CircuitProgram,
    ProgramRequestError,
    ProgramSubmitRequest,
)


def _editor_circuit() -> dict[str, object]:
    return {
        "logical_qubits": 1,
        "classical_bits": 1,
        "initial_states": [0],
        "columns": [
            {"step": 0, "gates": [{"type": "H", "targets": [0], "controls": []}]},
            {"step": 1, "gates": [{
                "type": "MEASURE", "targets": [0], "controls": [],
                "classical_targets": [0],
            }]},
        ],
        "annotations": [{
            "kind": "MESSAGE", "id": "label-1", "column_index": 0, "qubits": [0],
        }],
    }


def _bundle(circuit: dict[str, object]) -> str:
    return json.dumps({
        "version": 1,
        "kind": "quantscope_circuit_config",
        "circuit_config": circuit,
    })


class AttackProgramRequestTests(unittest.TestCase):
    def test_editor_export_and_raw_config_share_one_program_representation(self) -> None:
        source = _editor_circuit()
        from_bundle = CircuitProgram.from_editor_json(_bundle(source))
        from_raw = CircuitProgram.from_editor_json(json.dumps(source))

        self.assertEqual(from_bundle, from_raw)
        self.assertEqual(from_bundle.source_annotation_count, 1)
        self.assertEqual(from_bundle.to_circuit_dict()["annotations"], [])
        self.assertEqual(from_bundle.to_circuit_dict()["columns"][1]["gates"][0]["type"], "MEASURE")
        source["columns"][0]["gates"][0]["type"] = "X"
        detached = from_bundle.to_circuit_dict()
        detached["columns"][0]["gates"][0]["type"] = "Y"
        self.assertEqual(from_bundle.to_circuit_dict()["columns"][0]["gates"][0]["type"], "H")

    def test_submission_envelope_keeps_identity_and_placement_out_of_body(self) -> None:
        request = ProgramSubmitRequest(
            program=CircuitProgram.from_editor_json(_bundle(_editor_circuit())),
            device_id="virtual-qpu-1",
            shots=32,
            idempotency_key="request-1",
        )
        payload = request.to_application_payload()
        self.assertEqual(set(payload), {"device_id", "shots", "circuit_config"})
        self.assertEqual(payload["shots"], 32)
        self.assertNotIn("tenant_id", payload)
        self.assertNotIn("principal_id", payload)
        self.assertNotIn("physical_qubit_id", payload)
        self.assertNotIn("idempotency_key", payload)

    def test_malformed_version_or_unsafe_shape_is_rejected(self) -> None:
        base = _editor_circuit()
        cases = [
            json.dumps({"version": 2, "kind": "quantscope_circuit_config", "circuit_config": base}),
            json.dumps({"version": 1, "kind": "wrong", "circuit_config": base}),
            json.dumps({"kind": "quantscope_circuit_config", "circuit_config": base}),
            "{bad JSON",
            _bundle({**base, "columns": [{"step": 0, "gates": [{
                "type": "RX", "targets": [0], "params": {"theta_rad": float("nan")},
            }]}]}),
            "[1, 2]",
            " " * (64 * 1024 + 1),
        ]
        for text in cases:
            with self.subTest(text=text[:40]), self.assertRaises(ProgramRequestError):
                CircuitProgram.from_editor_json(text)

    def test_editor_bounds_and_circuit_semantics_are_checked_before_submission(self) -> None:
        base = _editor_circuit()
        variants = []
        too_many_qubits = deepcopy(base)
        too_many_qubits["logical_qubits"] = 9
        variants.append(too_many_qubits)
        duplicated_step = deepcopy(base)
        duplicated_step["columns"][1]["step"] = 0
        variants.append(duplicated_step)
        invalid_gate = deepcopy(base)
        invalid_gate["columns"][0]["gates"][0]["type"] = "HACK"
        variants.append(invalid_gate)
        invalid_target = deepcopy(base)
        invalid_target["columns"][0]["gates"][0]["targets"] = [1]
        variants.append(invalid_target)
        forged_label = deepcopy(base)
        forged_label["annotations"][0]["qubits"] = [99]
        variants.append(forged_label)
        string_target = deepcopy(base)
        string_target["columns"][0]["gates"][0]["targets"] = ["0"]
        variants.append(string_target)
        enormous_param = deepcopy(base)
        enormous_param["columns"][0]["gates"][0]["params"] = {"theta_rad": 2**2000}
        variants.append(enormous_param)
        for circuit in variants:
            with self.subTest(circuit=circuit), self.assertRaises(ProgramRequestError):
                CircuitProgram.from_editor_json(_bundle(circuit))

    def test_envelope_rejects_invalid_shots_and_identifiers(self) -> None:
        program = CircuitProgram.from_editor_json(_bundle(_editor_circuit()))
        for values in (
            {"device_id": "", "shots": 1, "idempotency_key": "once"},
            {"device_id": "qpu", "shots": 0, "idempotency_key": "once"},
            {"device_id": "qpu", "shots": True, "idempotency_key": "once"},
            {"device_id": "qpu", "shots": 1, "idempotency_key": "with space"},
        ):
            with self.subTest(values=values), self.assertRaises(ProgramRequestError):
                ProgramSubmitRequest(program=program, **values)


if __name__ == "__main__":
    unittest.main()

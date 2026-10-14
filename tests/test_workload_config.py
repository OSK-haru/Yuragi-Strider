"""Closed frozen-workload JSON boundary; execution stays in the common runner."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
from types import MappingProxyType
import unittest
from unittest.mock import patch

from core.attack_simulation.config_limits import MAX_CONFIG_BYTES, MAX_CONFIG_NODES
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.program_request import CircuitProgram, ProgramRequestError
from core.attack_simulation.victim_workload import (
    Outcome, RegisteredProgram, WorkloadCapacityError, advance_result, begin_dispatch,
    bind_submission, dispatch_operation, start_workload,
)
from core.attack_simulation.workload_config import parse_workload_plan, serialize_workload_plan


def config():
    return {
        "version": 1, "plan_id": "finite-plan", "actor_id": "actor", "clock_domain": "world",
        "programs": [{"program_id": "register", "circuit_config": {
            "logical_qubits": 3, "initial_states": [0, 1, "+"], "classical_bits": 3,
            "columns": [
                {"step": 0, "gates": [{"type": "H", "targets": [0]}]},
                {"step": 1, "gates": [{"type": "CNOT", "targets": [1], "controls": [0]}]},
                {"step": 2, "gates": [{"type": "MEASURE", "targets": [q],
                                        "classical_targets": [q]} for q in range(3)]},
            ],
        }}],
        "steps": [{
            "step_id": "loop", "program_ref": "register", "device_id": "qpu-register",
            "shots": 8, "durations": {"classical_us": 0, "outbound_us": 2.5, "inbound_us": 3},
            "max_visits": 2,
            "branches": [
                {"outcome": "succeeded", "next_step": "loop",
                 "predicate": {"bitstring": "011", "minimum": 2}},
                {"outcome": "denied", "next_step": "done", "predicate": None},
                {"outcome": "failed", "next_step": "done", "predicate": None},
                {"outcome": "cancelled", "next_step": "done", "predicate": None},
            ],
            "operation_id": "program.submit", "initialization": "inherit",
        }, {
            "step_id": "done", "program_ref": "register", "device_id": "qpu-register",
            "shots": 1, "durations": {"classical_us": 4, "outbound_us": 0, "inbound_us": 0},
            "max_visits": 1, "branches": [], "operation_id": "program.submit",
            "initialization": "prepare",
        }],
        "starts": [{"step_id": "loop", "at_us": 10}, {"step_id": "done", "at_us": 20}],
        "max_events": 4, "max_pending": 2,
    }


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(freeze(child) for child in value)
    return value


class Unbounded:
    def __iter__(self):
        raise AssertionError("unbounded iterable must never be consumed")


class WorkloadConfigTests(unittest.TestCase):
    def test_multiqubit_roundtrip_frozen_manifest_and_common_editor_ingestion(self):
        original = config()
        plan = parse_workload_plan(original)
        serialized = serialize_workload_plan(plan)
        self.assertEqual(serialized.keys(), original.keys())
        self.assertEqual(serialized["steps"], original["steps"])
        self.assertEqual(serialized["starts"], original["starts"])
        circuit = serialized["programs"][0]["circuit_config"]
        self.assertEqual(circuit["initial_states"], [0, 1, "+"])
        self.assertIs(type(circuit["initial_states"][0]), int)
        self.assertEqual(CircuitProgram.from_editor_json(json.dumps(circuit)), plan.programs[0].program)
        self.assertEqual(parse_workload_plan(json.loads(json.dumps(serialized, allow_nan=False))), plan)
        self.assertEqual(parse_workload_plan(freeze(serialized)), plan)
        self.assertEqual([step.initialization for step in plan.steps], ["inherit", "prepare"])
        self.assertEqual([branch.outcome.value for branch in plan.steps[0].branches],
                         [branch["outcome"] for branch in original["steps"][0]["branches"]])

    def test_input_and_every_serialized_child_are_detached_and_plan_is_frozen(self):
        source = config()
        plan = parse_workload_plan(source)
        baseline = serialize_workload_plan(plan)
        source["programs"][0]["circuit_config"]["columns"][1]["gates"][0]["controls"][0] = 2
        source["steps"][0]["durations"]["outbound_us"] = 900
        source["steps"][0]["branches"][0]["predicate"]["minimum"] = 7
        source["starts"][0]["at_us"] = 900
        self.assertEqual(serialize_workload_plan(plan), baseline)
        mutated = serialize_workload_plan(plan)
        mutated["programs"][0]["circuit_config"]["initial_states"][0] = 1
        mutated["programs"][0]["circuit_config"]["columns"][0]["gates"][0]["targets"][0] = 2
        mutated["steps"][0]["durations"]["inbound_us"] = 900
        mutated["steps"][0]["branches"][0]["predicate"]["minimum"] = 7
        mutated["steps"].pop()
        mutated["starts"][0]["at_us"] = 900
        self.assertEqual(serialize_workload_plan(plan), baseline)
        for value, field in ((plan, "actor_id"), (plan.steps[0], "initialization"),
                             (plan.steps[0].durations, "outbound_us"),
                             (plan.steps[0].branches[0].predicate, "minimum")):
            with self.assertRaises(FrozenInstanceError):
                setattr(value, field, None)

    def test_version_and_all_workload_fields_are_required_and_closed(self):
        paths = [(), ("programs", 0), ("steps", 0), ("steps", 0, "durations"),
                 ("steps", 0, "branches", 0), ("steps", 0, "branches", 0, "predicate"),
                 ("starts", 0)]
        for path in paths:
            node = config()
            for part in path:
                node = node[part]
            for field in [*node, "unknown"]:
                with self.subTest(path=path, field=field):
                    document = config()
                    changed = document
                    for part in path:
                        changed = changed[part]
                    if field == "unknown":
                        changed[field] = None
                    else:
                        del changed[field]
                    with self.assertRaises(ValueError):
                        parse_workload_plan(document)
        for version in (0, 2, -1, True, 1.0, "1", None):
            with self.subTest(version=version), self.assertRaises((ValueError, TypeError)):
                parse_workload_plan({**config(), "version": version})
        for document in (None, [], "{}", Unbounded()):
            with self.assertRaises(TypeError):
                parse_workload_plan(document)
        with self.assertRaises(TypeError):
            serialize_workload_plan(config())

    def test_global_limits_run_before_copies_and_child_ingestion(self):
        documents = []
        for value in ([None] * MAX_CONFIG_NODES, "x" * (MAX_CONFIG_BYTES + 1)):
            documents.append({**config(), "extra": value})
        nested = None
        for _ in range(34):
            nested = [nested]
        documents.append({**config(), "extra": nested})
        cyclic = config()
        cyclic["extra"] = cyclic
        documents.append(cyclic)
        for document in documents:
            with self.subTest(value=type(document.get("extra")).__name__):
                with patch("core.attack_simulation.workload_config._copy_json") as copy:
                    with patch.object(CircuitProgram, "from_editor_json") as ingest:
                        with self.assertRaisesRegex(ValueError, "(node|byte|nesting) limit"):
                            parse_workload_plan(document)
                        copy.assert_not_called()
                        ingest.assert_not_called()

    def test_collection_caps_checked_before_any_child_construction(self):
        for name, limit in (("programs", 64), ("steps", 256), ("starts", 256), ("branches", 16)):
            for count in (limit + 1, 0):
                if name == "branches" and count == 0:
                    continue
                with self.subTest(name=name, count=count):
                    document = config()
                    owner = document["steps"][1] if name == "branches" else document
                    sample = document["steps"][0]["branches"][1] if name == "branches" else owner[name][0]
                    owner[name] = [deepcopy(sample) for _ in range(count)]
                    with patch.object(CircuitProgram, "from_editor_json") as ingest:
                        with self.assertRaisesRegex(ValueError, "items"):
                            parse_workload_plan(document)
                        ingest.assert_not_called()
        for path in (("programs",), ("steps",), ("starts",), ("steps", 0, "branches"),
                     ("programs", 0, "circuit_config", "columns")):
            document = config()
            owner = document
            for key in path[:-1]:
                owner = owner[key]
            owner[path[-1]] = Unbounded()
            with self.subTest(path=path), self.assertRaises(TypeError):
                parse_workload_plan(document)

    def test_finite_numbers_and_exact_integer_types(self):
        integer_paths = [("max_events",), ("max_pending",), ("steps", 0, "shots"),
                         ("steps", 0, "max_visits"), ("steps", 0, "branches", 0, "predicate", "minimum"),
                         ("programs", 0, "circuit_config", "logical_qubits"),
                         ("programs", 0, "circuit_config", "initial_states", 0),
                         ("programs", 0, "circuit_config", "columns", 0, "step"),
                         ("programs", 0, "circuit_config", "columns", 0, "gates", 0, "targets", 0)]
        duration_paths = [("steps", 0, "durations", name) for name in
                          ("classical_us", "outbound_us", "inbound_us")] + [("starts", 0, "at_us")]
        for paths, values in ((integer_paths, (True, 1.0, "1")),
                              (duration_paths, (True, "0", -1, float("nan"), float("inf"),
                                                -float("inf"), 10**400))):
            for path in paths:
                for value in values:
                    document = config()
                    owner = document
                    for key in path[:-1]:
                        owner = owner[key]
                    owner[path[-1]] = value
                    with self.subTest(path=path, value=value), self.assertRaises((ValueError, TypeError)):
                        parse_workload_plan(document)

    def test_exact_collection_caps_are_accepted_and_serializer_enforces_global_bounds(self):
        document = config()
        sample = document["programs"][0]
        document["programs"] = [{**deepcopy(sample), "program_id": f"p-{i}"} for i in range(64)]
        for step in document["steps"]:
            step["program_ref"] = "p-0"
        self.assertEqual(len(parse_workload_plan(document).programs), 64)
        document = config()
        sample = document["steps"][1]
        document["steps"] = [{**deepcopy(sample), "step_id": f"s-{i}"} for i in range(256)]
        document.update(starts=[{"step_id": "s-0", "at_us": 0}], max_events=256)
        self.assertEqual(len(parse_workload_plan(document).steps), 256)
        document = config()
        document.update(starts=[{"step_id": "done", "at_us": i} for i in range(256)],
                        max_events=256, max_pending=256)
        self.assertEqual(len(parse_workload_plan(document).starts), 256)
        document = config()
        document["steps"][0]["branches"] = [{
            "outcome": "succeeded", "next_step": "loop",
            "predicate": {"bitstring": "011", "minimum": i},
        } for i in range(16)]
        self.assertEqual(len(parse_workload_plan(document).steps[0].branches), 16)
        document = config()
        document["programs"][0]["circuit_config"]["columns"] = [
            {"step": i, "gates": [{"type": "I", "targets": [0]}]} for i in range(200)
        ]
        plan = parse_workload_plan(document)
        expanded = replace(plan, programs=tuple(
            RegisteredProgram("register" if i == 0 else f"p-{i}", plan.programs[0].program)
            for i in range(16)
        ))
        with self.assertRaisesRegex(ValueError, "node limit"):
            serialize_workload_plan(expanded)

    def test_common_program_validation_and_no_private_or_physical_fields(self):
        for field in ("seed", "private_seed", "physical_layout", "qpu_truth", "density_matrix"):
            for path in ((), ("steps", 0), ("programs", 0), ("programs", 0, "circuit_config")):
                document = config()
                owner = document
                for key in path:
                    owner = owner[key]
                owner[field] = 0
                with self.subTest(path=path, field=field), self.assertRaises(ValueError):
                    parse_workload_plan(document)
        for circuit_changes in ({"initial_states": ["0", "1", "+"]}, {"initial_states": [0]},
                                {"logical_qubits": 9}, {"columns": []}):
            document = config()
            document["programs"][0]["circuit_config"].update(circuit_changes)
            with self.assertRaises(ProgramRequestError):
                parse_workload_plan(document)
        document = config()
        gate = document["programs"][0]["circuit_config"]["columns"][0]["gates"][0]
        gate["targets"] = [3]
        with self.assertRaises(ProgramRequestError):
            parse_workload_plan(document)
        gate["targets"] = [0]
        gate["params"] = {1: 0.5}
        with self.assertRaises(TypeError):
            parse_workload_plan(document)

    def test_immutable_plan_reference_branch_and_capacity_validators(self):
        cases = [(("steps", 0, "program_ref"), "missing"),
                 (("starts", 0, "step_id"), "missing"),
                 (("steps", 0, "branches", 0, "next_step"), "missing"),
                 (("steps", 1, "step_id"), "loop"),
                 (("steps", 0, "operation_id"), "internal.prepare_victim"),
                 (("steps", 0, "initialization"), "automatic"),
                 (("steps", 0, "max_visits"), 4097), (("steps", 0, "shots"), 0),
                 (("steps", 0, "branches", 0, "outcome"), "truth"),
                 (("steps", 0, "branches", 0, "predicate", "minimum"), -1),
                 (("max_events",), 3), (("max_pending",), 5)]
        for path, value in cases:
            document = config()
            owner = document
            for key in path[:-1]:
                owner = owner[key]
            owner[path[-1]] = value
            with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                parse_workload_plan(document)
        document = config()
        document["programs"].append(deepcopy(document["programs"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_workload_plan(document)
        document = config()
        document["max_pending"] = 1
        with self.assertRaises(WorkloadCapacityError):
            parse_workload_plan(document)
        document = config()
        branches = document["steps"][0]["branches"]
        branches.insert(0, {"outcome": "succeeded", "next_step": "done", "predicate": None})
        with self.assertRaisesRegex(ValueError, "fallback"):
            parse_workload_plan(document)
        document = config()
        document["steps"][0]["branches"][0]["outcome"] = "denied"
        with self.assertRaisesRegex(ValueError, "successful"):
            parse_workload_plan(document)

    def test_exact_encoded_byte_limit_before_common_ingestion(self):
        document = config()
        document["programs"][0]["circuit_config"]["annotations"] = [{
            "kind": "MESSAGE", "column_index": 0, "qubits": [0], "id": "\0" * 200_000,
        }]
        with patch.object(CircuitProgram, "from_editor_json") as ingest:
            with self.assertRaisesRegex(ValueError, "serialized byte limit"):
                parse_workload_plan(document)
            ingest.assert_not_called()

    def test_zero_and_irrelevant_grants_allow_definition_but_deny_common_dispatch(self):
        operations = frozenset({"program.submit", "job.result.read"})
        for grants in (frozenset(), frozenset({"job.result.read"})):
            with self.subTest(grants=grants):
                policy = JobAuthorizationPolicy(operations, operations,
                    {"victim": grants}, {"tenant-v": "victim"})

                class Client:
                    principal_id = "actor"

                    def submit_declared_program(self, request, *, arrival):
                        policy.authorize("tenant-v", "program.submit")
                        raise AssertionError("unauthorized submission reached execution")

                plan = parse_workload_plan(config())
                self.assertEqual(parse_workload_plan(serialize_workload_plan(plan)), plan)
                state = start_workload(plan, run_id="run")
                event = state.events[0]
                state = begin_dispatch(state, event.event_id, through=event.arrival)
                submission = dispatch_operation(state, event.event_id, Client())
                self.assertTrue(submission.denied)
                self.assertIsNone(submission.job_id)
                state = bind_submission(state, event.event_id, submission)
                state = advance_result(state, event.event_id, available=EventPosition("world", 30, 99))
                self.assertIs(state.events[0].outcome, Outcome.DENIED)
                successor = state.events[-1]
                self.assertEqual(successor.step_id, "done")
                self.assertEqual(successor.arrival.time_us, 37)


if __name__ == "__main__":
    unittest.main()

"""Service-level interleaving: scenario-owned victims, grants and observation budget."""

import unittest
from unittest.mock import patch

from core.attack_simulation.circuit_program import LocalCircuitCompiler
from core.attack_simulation.job_service import InterleaveVictimSpec, JobApplicationService, PrincipalGrant
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.program_request import (
    INTERLEAVE_TRAINING_OPERATION_ID, INTERLEAVE_VICTIM_OPERATION_ID, CircuitProgram,
    ProgramRequestError, ProgramSubmitRequest,
)
from core.attack_simulation.scenario_policy import ObservationBudget
from tests.test_interleaved_execution import ALL, ATTACKER, VICTIM, build
from tests.test_local_program_runtime import program_json


def service(bit=1, *, reset_success=0.90, budget=None, attacker_ops=ALL):
    store, runtime, compiler, manifest = build(reset_success)
    victims = tuple(
        InterleaveVictimSpec(ref, "v", CircuitProgram.from_editor_json(program_json(1, initial=[value])))
        for ref, value in (("victim", bit), ("training:0", 0), ("training:1", 1))
    )
    return store, runtime, JobApplicationService(
        runtime=runtime, program_compiler=compiler, scenario_manifest_id=manifest.manifest_id,
        principals=(PrincipalGrant("a", ATTACKER, frozenset(attacker_ops)),
                    PrincipalGrant("v", VICTIM, frozenset(ALL))),
        interleave_victims=victims, observation_budget=budget,
    )


def request(reference, rounds, key):
    return ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(1)), "qpu-1",
                                rounds, key, "inherit", interleave=reference)


def counts_of(runtime, svc, reference, rounds, key):
    job = svc.submit_program("a", request(reference, rounds, key))
    assert runtime.run_next().status is JobStatus.SUCCEEDED
    return svc.get_result("a", job.job_id).result["counts"]


def sequenced_service(bits, *, budget=None, attacker_principals=("a",)):
    store, runtime, compiler, manifest = build(0.90)
    sequence = tuple(CircuitProgram.from_editor_json(program_json(1, initial=[bit])) for bit in bits)
    victims = (InterleaveVictimSpec("victim", "v", sequence[0], sequence),)
    return runtime, JobApplicationService(
        runtime=runtime, program_compiler=compiler, scenario_manifest_id=manifest.manifest_id,
        principals=tuple(PrincipalGrant(principal, ATTACKER, frozenset(ALL))
                         for principal in attacker_principals)
                   + (PrincipalGrant("v", VICTIM, frozenset(ALL)),),
        interleave_victims=victims, observation_budget=budget,
    )


def victim_bit(runtime, job_id):
    """1 when the victim program flips its prepared qubit before measuring."""
    operation = runtime.scheduler.get(job_id).command.operation.interleave.operation
    return int(any(item.kind == "unitary" for item in operation.instructions))


class InterleavedServiceTests(unittest.TestCase):
    def test_sequence_selection_does_not_compile_under_the_admission_lock(self):
        runtime, svc = sequenced_service((1, 0))
        commands = [svc._prepare_program("a", request("victim", 1, f"k{i}")) for i in range(2)]
        with patch.object(svc, "_compile_victim", side_effect=AssertionError("compile during admission")):
            jobs = [svc._admit_program("a", command).job_id for command in commands]
        self.assertEqual([victim_bit(runtime, job) for job in jobs], [1, 0])

    def test_same_key_from_two_principals_consumes_two_observations(self):
        budget = ObservationBudget(shots_per_query=1, training_query_limit=0, victim_observation_limit=1)
        runtime, svc = sequenced_service((1, 0), budget=budget, attacker_principals=("a", "b"))
        first = svc.submit_program("a", request("victim", 1, "same"))
        with self.assertRaisesRegex(ProgramRequestError, "observation budget"):
            svc.submit_program("b", request("victim", 1, "same"))
        self.assertEqual(runtime.scheduler.queued_job_ids(), (first.job_id,))

    def test_same_key_from_two_principals_keeps_distinct_sequence_positions(self):
        runtime, svc = sequenced_service((1, 0), attacker_principals=("a", "b"))
        first = svc.submit_program("a", request("victim", 1, "same"))
        second = svc.submit_program("b", request("victim", 1, "same"))
        self.assertEqual((victim_bit(runtime, first.job_id), victim_bit(runtime, second.job_id)), (1, 0))
        self.assertEqual(svc.submit_program("b", request("victim", 1, "same")).job_id, second.job_id)

    def test_idempotency_conflict_does_not_consume_an_observation_or_position(self):
        budget = ObservationBudget(shots_per_query=1, training_query_limit=0, victim_observation_limit=1)
        runtime, svc = sequenced_service((1, 0), budget=budget)
        plain = ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(1)), "qpu-1", 1, "used")
        first = svc.submit_program("a", plain)
        with self.assertRaisesRegex(ProgramRequestError, "idempotency"):
            svc.submit_program("a", request("victim", 1, "used"))
        self.assertEqual(svc.observation_budget_view("a")["victim_observations"], 0)
        self.assertEqual(runtime.scheduler.queued_job_ids(), (first.job_id,))
        accepted = svc.submit_program("a", request("victim", 1, "fresh"))
        self.assertEqual(victim_bit(runtime, accepted.job_id), 1)

    def test_sequenced_victim_advances_by_acceptance_and_wraps(self):
        runtime, svc = sequenced_service((1, 0, 1))
        jobs = [svc.submit_program("a", request("victim", 1, f"k{index}")).job_id for index in range(5)]
        self.assertEqual([victim_bit(runtime, job) for job in jobs], [1, 0, 1, 1, 0])

    def test_replays_keep_their_position_and_rejections_take_none(self):
        budget = ObservationBudget(shots_per_query=1, training_query_limit=1, victim_observation_limit=2)
        runtime, svc = sequenced_service((1, 0), budget=budget)
        first = svc.submit_program("a", request("victim", 1, "first"))
        self.assertEqual(svc.submit_program("a", request("victim", 1, "first")).job_id, first.job_id)
        with self.assertRaisesRegex(ProgramRequestError, "observation budget"):
            svc.submit_program("a", request("victim", 2, "too-many-rounds"))
        second = svc.submit_program("a", request("victim", 1, "second")).job_id
        self.assertEqual((victim_bit(runtime, first.job_id), victim_bit(runtime, second)), (1, 0))

    def test_training_labels_and_the_real_victim_use_the_same_physics(self):
        _, runtime, svc = service(bit=1)
        trained = counts_of(runtime, svc, "training:1", 200, "t1")
        real = counts_of(runtime, svc, "victim", 200, "real")
        self.assertAlmostEqual(trained.get("1", 0) / 200, real.get("1", 0) / 200, delta=0.12)

    def test_the_attacker_chooses_a_reference_not_a_program(self):
        with self.assertRaises(ProgramRequestError):
            ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(1)), "qpu-1", 2, "k",
                                 "inherit", interleave="custom-victim")

    def test_grants_split_training_from_real_victim_observation(self):
        no_victim = tuple(op for op in ALL if op != INTERLEAVE_VICTIM_OPERATION_ID)
        _, runtime, svc = service(attacker_ops=no_victim)
        with self.assertRaises(PermissionError):
            svc.submit_program("a", request("victim", 2, "v"))
        self.assertEqual(sum(counts_of(runtime, svc, "training:0", 2, "t").values()), 2)
        no_training = tuple(op for op in ALL if op != INTERLEAVE_TRAINING_OPERATION_ID)
        _, _, svc = service(attacker_ops=no_training)
        with self.assertRaises(PermissionError):
            svc.submit_program("a", request("training:1", 2, "t"))

    def test_budget_counts_distinct_queries_and_caps_rounds(self):
        budget = ObservationBudget(shots_per_query=8, training_query_limit=2, victim_observation_limit=1)
        _, runtime, svc = service(budget=budget)
        counts_of(runtime, svc, "training:0", 8, "t0")
        counts_of(runtime, svc, "training:1", 8, "t1")
        with self.assertRaisesRegex(ProgramRequestError, "observation budget"):
            svc.submit_program("a", request("training:0", 8, "t2"))
        with self.assertRaisesRegex(ProgramRequestError, "observation budget"):
            svc.submit_program("a", request("victim", 9, "too-long"))
        view = svc.observation_budget_view("a")
        self.assertEqual((view["training_queries"], view["victim_observations"]), (2, 0))
        counts_of(runtime, svc, "victim", 8, "real")
        with self.assertRaisesRegex(ProgramRequestError, "observation budget"):
            svc.submit_program("a", request("victim", 8, "real-2"))

    def test_resubmitting_the_same_request_does_not_charge_twice(self):
        budget = ObservationBudget(shots_per_query=4, training_query_limit=1, victim_observation_limit=1)
        _, runtime, svc = service(budget=budget)
        first = svc.submit_program("a", request("training:0", 4, "same"))
        again = svc.submit_program("a", request("training:0", 4, "same"))
        self.assertEqual(first.job_id, again.job_id)
        self.assertEqual(svc.observation_budget_view("a")["training_queries"], 1)

    def test_invalid_requests_do_not_spend_budget(self):
        budget = ObservationBudget(shots_per_query=4, training_query_limit=1, victim_observation_limit=1)
        _, _, svc = service(budget=budget)
        with self.assertRaises(ProgramRequestError):
            svc.submit_program("a", ProgramSubmitRequest(
                CircuitProgram.from_editor_json(program_json(1)), "other-device", 4, "x", "inherit",
                interleave="training:0"))
        self.assertEqual(svc.observation_budget_view("a")["training_queries"], 0)


if __name__ == "__main__":
    unittest.main()

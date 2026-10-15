"""Abstract command: the runs behind the principal's own job (free mode only)."""

import unittest

from core.attack_simulation.job_service import (
    JobApplicationService, JobNotFoundError, JobResultNotReadyError, MAX_ACTIVITY_SHOTS,
    InterleaveVictimSpec, PrincipalGrant,
)
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.program_request import (
    VICTIM_ACTIVITY_OPERATION_ID, CircuitProgram, ProgramSubmitRequest,
)
from tests.test_interleaved_execution import ALL, ATTACKER, VICTIM, build
from tests.test_local_program_runtime import program_json

WITH_ACTIVITY = ALL + (VICTIM_ACTIVITY_OPERATION_ID,)


def service(bit=1, *, supported=WITH_ACTIVITY):
    store, runtime, compiler, manifest = build(0.90, supported=supported, grants=supported)
    victims = tuple(
        InterleaveVictimSpec(ref, "v", CircuitProgram.from_editor_json(program_json(1, initial=[value])))
        for ref, value in (("victim", bit), ("training:0", 0), ("training:1", 1)))
    return store, runtime, JobApplicationService(
        runtime=runtime, program_compiler=compiler, scenario_manifest_id=manifest.manifest_id,
        principals=(PrincipalGrant("a", ATTACKER, frozenset(supported)),
                    PrincipalGrant("v", VICTIM, frozenset(supported))),
        interleave_victims=victims,
    )


def request(reference, rounds, key):
    return ProgramSubmitRequest(CircuitProgram.from_editor_json(program_json(1)), "qpu-1", rounds, key,
                                "inherit" if reference else "prepare", interleave=reference)


def finished(runtime, svc, reference, rounds, key):
    job = svc.submit_program("a", request(reference, rounds, key))
    assert runtime.run_next().status is JobStatus.SUCCEEDED
    return job.job_id


class VictimActivityTests(unittest.TestCase):
    def test_each_shot_is_victim_run_then_service_reset_then_attacker_measurement(self):
        _, runtime, svc = service(bit=1)
        activity = svc.get_victim_activity("a", finished(runtime, svc, "victim", 3, "k1"))

        self.assertEqual((activity["reference"], activity["victim_prepared_bit"]), ("victim", 1))
        self.assertEqual((activity["shots_total"], activity["shots_shown"]), (3, 3))
        first = activity["shots"][0]["steps"]
        self.assertEqual([(s["actor"], s["kind"]) for s in first],
                         [("victim", "victim_run"), ("service", "service_reset"), ("attacker", "attacker_measure")])
        self.assertEqual(first[1]["after"], "victim")  # The reset that can leak the victim's state.
        # From the second shot the service also resets after the attacker, before the next victim run.
        second = activity["shots"][1]["steps"]
        self.assertEqual([s["kind"] for s in second],
                         ["service_reset", "victim_run", "service_reset", "attacker_measure"])
        self.assertEqual(second[0]["after"], "attacker")
        times = [(s["start_us"], s["end_us"]) for shot in activity["shots"] for s in shot["steps"]]
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(start < end for start, end in times))

    def test_a_training_reference_reports_its_known_label(self):
        _, runtime, svc = service(bit=1)
        zero = svc.get_victim_activity("a", finished(runtime, svc, "training:0", 2, "k0"))
        self.assertEqual((zero["reference"], zero["victim_prepared_bit"]), ("training:0", 0))
        one = svc.get_victim_activity("a", finished(runtime, svc, "training:1", 2, "k1"))
        self.assertEqual(one["victim_prepared_bit"], 1)

    def test_a_long_job_shows_only_the_first_shots_but_counts_all(self):
        _, runtime, svc = service()
        activity = svc.get_victim_activity("a", finished(runtime, svc, "victim", MAX_ACTIVITY_SHOTS + 4, "long"))
        self.assertEqual((activity["shots_total"], activity["shots_shown"]),
                         (MAX_ACTIVITY_SHOTS + 4, MAX_ACTIVITY_SHOTS))
        self.assertEqual(len(activity["shots"]), MAX_ACTIVITY_SHOTS)

    def test_an_ordinary_job_has_no_victim(self):
        _, runtime, svc = service()
        activity = svc.get_victim_activity("a", finished(runtime, svc, None, 2, "plain"))
        self.assertEqual((activity["reference"], activity["victim_prepared_bit"]), (None, None))
        self.assertNotIn("victim_run", {s["kind"] for shot in activity["shots"] for s in shot["steps"]})

    def test_it_exposes_no_state_measured_value_or_tenant_identity(self):
        _, runtime, svc = service()
        text = repr(svc.get_victim_activity("a", finished(runtime, svc, "victim", 2, "leak")))
        for private in (VICTIM, ATTACKER, "tenant-", "density", "counts", "probability"):
            self.assertNotIn(private, text)

    def test_the_command_must_be_enabled_by_the_service(self):
        _, runtime, svc = service(supported=ALL)
        job_id = finished(runtime, svc, "victim", 2, "off")
        with self.assertRaises(PermissionError):
            svc.get_victim_activity("a", job_id)

    def test_it_waits_for_the_result_and_stays_inside_the_tenant(self):
        _, runtime, svc = service()
        job = svc.submit_program("a", request("victim", 2, "early"))
        with self.assertRaises(JobResultNotReadyError):
            svc.get_victim_activity("a", job.job_id)
        runtime.run_next()
        with self.assertRaises(PermissionError):
            svc.get_victim_activity("v", job.job_id)  # Only the attacker is granted it, never the victim.
        with self.assertRaises(JobNotFoundError):
            svc.get_victim_activity("a", "job-nope")


if __name__ == "__main__":
    unittest.main()

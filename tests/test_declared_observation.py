"""Timed common views: host-delayed projections must not observe the future."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Event
import unittest
from unittest.mock import patch

from core.attack_simulation.declared_execution import DeclaredObservationNotReadyError
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.job_service import JobNotFoundError, JobResultNotReadyError
from core.attack_simulation.jobs import JobStatus
from tests.test_declared_execution import pos, setup


class DeclaredObservationTests(unittest.TestCase):
    def test_historical_views_remain_queued_or_running_after_actual_finish(self):
        args, _, runner, executor, job, window = setup()
        args[5].run_declared_next(executor, _workload_owner=runner._owner)
        service = args[5]._service
        for at, expected in ((window.arrival, JobStatus.QUEUED),
                             (window.start, JobStatus.RUNNING),
                             (window.finish, JobStatus.SUCCEEDED)):
            view = service._get_status_at("v", job.job_id, at)
            self.assertIs(view.status, expected)
            self.assertEqual(set(asdict(view)), {"job_id", "status", "error_code"})
            self.assertIsNone(view.error_code)
        for at in (window.arrival, window.start, window.finish):
            with self.assertRaises(JobResultNotReadyError):
                service._get_result_at("v", job.job_id, at)
        result = service._get_result_at("v", job.job_id, window.release)
        self.assertEqual(result.result, service.get_result("v", job.job_id).result)
        self.assertEqual(set(asdict(result)), {"job_id", "status", "result", "error_code"})

    def test_catch_up_wait_is_not_a_false_queued_or_successful_status(self):
        args, _, runner, executor, job, window = setup()
        service = args[5]._service
        self.assertIs(service._get_status_at("v", job.job_id, window.arrival).status, JobStatus.QUEUED)
        for at in (window.start, window.finish, window.release):
            with self.assertRaises(JobResultNotReadyError):
                service._get_status_at("v", job.job_id, at)
        args[5].begin_declared_next(executor, _workload_owner=runner._owner)
        self.assertIs(service._get_status_at("v", job.job_id, window.start).status, JobStatus.RUNNING)
        with self.assertRaises(JobResultNotReadyError):
            service._get_status_at("v", job.job_id, window.finish)

    def test_before_arrival_is_not_found_and_foreign_job_is_not_disclosed(self):
        args, _, _, executor, job, window = setup()
        before = EventPosition("world", window.arrival.time_us - 1, 0)
        for principal, at in (("v", before), ("a", window.arrival)):
            for method in (args[5]._service._get_status_at, args[5]._service._get_result_at):
                with self.assertRaises(JobNotFoundError):
                    method(principal, job.job_id, at)

    def test_foreign_domain_future_position_and_discard_are_rejected(self):
        _, _, _, executor, job, _ = setup()
        for at in (EventPosition("other", 10, 0), pos(101), None):
            with self.assertRaises(ValueError):
                executor.observation_at(job.job_id, at)
        executor.close()
        with self.assertRaises(RuntimeError):
            executor.observation_at(job.job_id, pos(10))

    def test_unmapped_cancellation_is_not_given_a_fabricated_historical_time(self):
        args, _, runner, executor, job, window = setup()
        args[5].request(args[4]["v"], lambda c, w: c.cancel(job.job_id),
                        operation_id="job.cancel", _workload_owner=runner._owner)
        with self.assertRaises(DeclaredObservationNotReadyError):
            executor.observation_at(job.job_id, window.arrival)
        with self.assertRaises(JobResultNotReadyError):
            args[5]._service._get_status_at("v", job.job_id, window.arrival)
        self.assertEqual(args[1].events(), ())

    def test_projection_work_uses_the_proved_position_not_later_target(self):
        args, _, runner, executor, job, window = setup()
        args[5].run_declared_next(executor, _workload_owner=runner._owner)
        entered, release = Event(), Event()
        from core.attack_simulation import job_service
        project = job_service._project_result

        def delayed(record):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test projection was not released")
            return project(record)

        with patch.object(job_service, "_project_result", side_effect=delayed):
            with ThreadPoolExecutor(max_workers=1) as pool:
                request = pool.submit(args[5]._service._get_result_at, "v", job.job_id, window.release)
                try:
                    self.assertTrue(entered.wait(5))
                    executor.frontier.set_target(pos(500))
                    with self.assertRaises(JobResultNotReadyError):
                        args[5]._service._get_result_at("v", job.job_id, window.finish)
                finally:
                    release.set()
                self.assertIs(request.result(timeout=5).status, JobStatus.SUCCEEDED)

    def test_other_jobs_finish_does_not_prove_this_jobs_start(self):
        from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
        from tests.test_attack_sessions import circuit_json
        args, _, runner, executor, first, window = setup()
        second = args[5].request(args[4]["v"], lambda c, w: c.submit_program(
            ProgramSubmitRequest(CircuitProgram.from_editor_json(circuit_json()), "qpu-1", 1, "overlap")),
            _workload_owner=runner._owner)
        executor.register_job(second.job_id, window)
        args[5].run_declared_next(executor, _workload_owner=runner._owner)
        self.assertIs(args[2].scheduler.get(first.job_id).status, JobStatus.SUCCEEDED)
        self.assertIs(args[2].scheduler.get(second.job_id).status, JobStatus.QUEUED)
        with self.assertRaises(DeclaredObservationNotReadyError):
            executor.observation_at(second.job_id, window.start)
        with self.assertRaises(JobResultNotReadyError):
            args[5]._service._get_status_at("v", second.job_id, window.start)


if __name__ == "__main__":
    unittest.main()

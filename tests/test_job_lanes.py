"""Per-lane FIFO: a lane's head may start while an earlier job of another lane waits."""

import unittest

from core.attack_simulation.jobs import FifoJobScheduler, JobCommand, JobStatus, VictimPrepareOperation


def _submit(scheduler, tenant, key):
    return scheduler.submit(JobCommand(tenant, VictimPrepareOperation(1), key)).job_id


class LaneFifoTests(unittest.TestCase):
    def _scheduler(self):
        scheduler = FifoJobScheduler()
        scheduler.bind_lanes(lambda command: command.tenant_id)
        return scheduler

    def test_other_lane_head_starts_ahead_of_an_older_job(self):
        s = self._scheduler()
        a1, a2, b1 = _submit(s, "a", "1"), _submit(s, "a", "2"), _submit(s, "b", "3")
        with self.assertRaises(ValueError):
            s._start_declared(a2)
        self.assertEqual(s._start_declared(b1).status, JobStatus.RUNNING)
        self.assertEqual(s.queued_job_ids(), (a1, a2))

    def test_rollback_restores_the_exact_queue_position(self):
        s = self._scheduler()
        a1, b1, a2 = _submit(s, "a", "1"), _submit(s, "b", "2"), _submit(s, "a", "3")
        s._start_declared(b1)
        self.assertTrue(s._rollback_declared_start(b1))
        self.assertEqual(s.queued_job_ids(), (a1, b1, a2))
        self.assertEqual(s.get(b1).status, JobStatus.QUEUED)

    def test_without_lanes_only_the_global_head_starts(self):
        s = FifoJobScheduler()
        a1, b1 = _submit(s, "a", "1"), _submit(s, "b", "2")
        with self.assertRaises(ValueError):
            s._start_declared(b1)
        self.assertEqual(s._start_declared(a1).status, JobStatus.RUNNING)

    def test_lanes_bind_once_and_before_jobs(self):
        s = self._scheduler()
        with self.assertRaises(RuntimeError):
            s.bind_lanes(lambda command: 0)
        t = FifoJobScheduler()
        _submit(t, "a", "1")
        with self.assertRaises(RuntimeError):
            t.bind_lanes(lambda command: 0)


if __name__ == "__main__":
    unittest.main()

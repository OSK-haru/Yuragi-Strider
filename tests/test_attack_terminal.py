"""Closed terminal grammar and shared-application-client dispatch."""

import unittest

from api.attack_terminal import (
    TerminalAction,
    TerminalCommand,
    TerminalSession,
    TerminalSyntaxError,
    TerminalWaitUnavailableError,
    TERMINAL_OPERATION_CATALOG,
    parse_terminal_command,
)
from core.attack_simulation.job_service import JobResultView, JobStatusView
from core.attack_simulation.jobs import JobStatus


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_status(self, job_id: str) -> JobStatusView:
        self.calls.append(("status", job_id))
        return JobStatusView(job_id, JobStatus.QUEUED)

    def get_result(self, job_id: str) -> JobResultView:
        self.calls.append(("result", job_id))
        return JobResultView(job_id, JobStatus.SUCCEEDED, {"shots": 1})

    def cancel(self, job_id: str) -> JobStatusView:
        self.calls.append(("cancel", job_id))
        return JobStatusView(job_id, JobStatus.CANCELLED)


class RecordingWaitClient(RecordingClient):
    def wait_duration(self, duration_us):
        self.calls.append(("wait.duration", duration_us))
        return duration_us

    def wait_request(self, request_id):
        self.calls.append(("wait.request", request_id))
        return request_id

    def wait_any(self):
        self.calls.append(("wait.any", ""))
        return "released"


class AttackTerminalTests(unittest.TestCase):
    def test_wait_commands_are_in_the_shared_terminal_catalog_and_dispatch(self) -> None:
        client = RecordingWaitClient()
        terminal = TerminalSession(client)
        self.assertTrue({"wait.duration", "wait.request", "wait.any"} <= TERMINAL_OPERATION_CATALOG)
        self.assertEqual(terminal.execute("wait 2ms"), 2000.0)
        self.assertEqual(terminal.execute("wait request req-7"), "req-7")
        self.assertEqual(terminal.execute("wait any"), "released")
        self.assertEqual(client.calls, [
            ("wait.duration", 2000.0), ("wait.request", "req-7"), ("wait.any", ""),
        ])
        self.assertIn("wait any", terminal.execute("help"))

    def test_wait_grammar_is_bounded_and_requires_an_explicit_wait_capability(self) -> None:
        terminal = TerminalSession(RecordingClient())
        for line in ("wait", "wait any extra", "wait request", "wait request ../x",
                     "wait 0us", "wait -1s", "wait nan", "wait 1e9s", "wait 1 s"):
            with self.subTest(line=line), self.assertRaises(TerminalSyntaxError):
                parse_terminal_command(line)
        with self.assertRaises(TerminalWaitUnavailableError):
            terminal.execute("wait 1ms")

    def test_help_and_existing_job_commands_use_only_shared_client(self) -> None:
        client = RecordingClient()
        terminal = TerminalSession(client)
        self.assertIn("job status <job_id>", terminal.execute("help"))
        self.assertEqual(client.calls, [])
        self.assertEqual(terminal.execute("job result job-7").job_id, "job-7")
        self.assertEqual(terminal.execute("job cancel job-7").status, JobStatus.CANCELLED)
        self.assertEqual(client.calls, [("result", "job-7"), ("cancel", "job-7")])

    def test_host_shell_and_credential_commands_are_not_accepted(self) -> None:
        client = RecordingClient()
        terminal = TerminalSession(client)
        for line in (
            "ls", "dir", "auth configure vqc_value", "job result job-1;dir",
            "job cancel job-1 | cat", "job result ../secret", "job result job-1\nhelp",
            "job result", "job result 'unterminated", "help extra",
        ):
            with self.subTest(line=line), self.assertRaises(TerminalSyntaxError):
                terminal.execute(line)
        self.assertEqual(client.calls, [])

    def test_huew_job_status_command(self) -> None:
        self.assertEqual(
            parse_terminal_command("job status job-7"),
            TerminalCommand(TerminalAction.JOB_STATUS, "job-7"),
        )
        client = RecordingClient()
        self.assertEqual(TerminalSession(client).execute("job status job-7").status, JobStatus.QUEUED)
        self.assertEqual(client.calls, [("status", "job-7")])

    def test_ascii_whitespace_does_not_change_command_structure(self) -> None:
        expected = TerminalCommand(TerminalAction.JOB_STATUS, "job-7")
        for line in (
            "job  status  job-7",
            "  job   status   job-7  ",
            "job\tstatus\tjob-7",
        ):
            with self.subTest(line=line):
                self.assertEqual(parse_terminal_command(line), expected)

    def test_options_are_not_yet_part_of_the_grammar(self) -> None:
        for line in (
            "job status job-7 --verbose",
            "job result job-7 --format json",
            "job cancel job-7 extra",
        ):
            with self.subTest(line=line), self.assertRaises(TerminalSyntaxError):
                parse_terminal_command(line)


if __name__ == "__main__":
    unittest.main()

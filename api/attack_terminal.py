"""Small, closed command language over the shared job application boundary.

This is a client adapter, not a host OS shell. The same job methods can be
called by a future UI adapter; this module adds no privileged QPU operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
import shlex
from threading import Lock
from typing import Protocol
from uuid import uuid4

from core.attack_simulation.job_service import DeviceInfo, JobResultView, JobStatusView
from core.attack_simulation.program_request import (
    CircuitProgram, PROGRAM_INITIALIZATIONS, ProgramSubmitRequest, interleave_operation_id,
)


_MAX_COMMAND_LENGTH = 4096
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_WAIT_DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?)(us|ms|s)\Z")


class TerminalSyntaxError(ValueError):
    """The input is not one of this client's supported commands."""


class TerminalAction(str, Enum):
    HELP = "help"
    DEVICE_LIST = "device.list"
    DEVICE_SHOW = "device.show"
    PROGRAM_SUBMIT = "program.submit"
    JOB_STATUS = "job.status"
    JOB_RESULT = "job.result"
    JOB_CANCEL = "job.cancel"
    WAIT_DURATION = "wait.duration"
    WAIT_REQUEST = "wait.request"
    WAIT_ANY = "wait.any"


# Terminal/client operations, not QPU service grants. Waiting only observes
# replies owned by the authenticated caller and never submits a QPU operation.
TERMINAL_OPERATION_CATALOG = frozenset(action.value for action in TerminalAction)


@dataclass(frozen=True)
class TerminalCommand:
    action: TerminalAction
    job_id: str | None = None
    device_id: str | None = None
    program_path: str | None = None
    shots: int | None = None
    request_id: str | None = None
    initialization: str = "prepare"
    interleave: str | None = None
    wait_duration_us: float | None = None


class TerminalWaitUnavailableError(RuntimeError):
    """The active client/time policy does not expose explicit waiting."""


class JobClient(Protocol):
    """Authenticated application methods shared by terminal and future UI."""

    def get_status(self, job_id: str) -> JobStatusView: ...
    def get_result(self, job_id: str) -> JobResultView: ...
    def cancel(self, job_id: str) -> JobStatusView: ...
    def list_devices(self) -> tuple[DeviceInfo, ...]: ...
    def get_device(self, device_id: str) -> DeviceInfo: ...
    def submit_program(self, request: ProgramSubmitRequest) -> JobStatusView: ...


class WaitClient(Protocol):
    """Optional live-time client capability; refs are client-issued opaque IDs."""

    def wait_duration(self, duration_us: float): ...
    def wait_request(self, request_id: str): ...
    def wait_any(self): ...


class TerminalWorkspace:
    """A bounded circuit-file namespace populated by the workspace UI/editor.

    Names never resolve to files on the host. Each actor receives its own
    workspace instance; challenge files can be installed before launch.
    """

    def __init__(self) -> None:
        self._files: dict[str, str] = {}
        self._lock = Lock()

    def put_circuit(self, name: str, text: str) -> None:
        name = _workspace_name(name)
        CircuitProgram.from_editor_json(text)
        with self._lock:
            if name not in self._files and len(self._files) >= 16:
                raise ValueError("workspace circuit-file limit exceeded")
            self._files[name] = text

    def read_circuit(self, name: str) -> CircuitProgram:
        try:
            with self._lock:
                text = self._files[_workspace_name(name)]
        except KeyError as error:
            raise FileNotFoundError("circuit file is absent from this workspace") from error
        # Import is bounded and revalidated at submission, like editor import.
        return CircuitProgram.from_editor_json(text)


def parse_terminal_command(line: str) -> TerminalCommand:
    if not isinstance(line, str) or len(line) > _MAX_COMMAND_LENGTH:
        raise TerminalSyntaxError("invalid command input")
    if any(character in line for character in "\r\n\x00"):
        raise TerminalSyntaxError("commands must contain one line")
    try:
        parts = shlex.split(line)
    except ValueError as error:
        raise TerminalSyntaxError("invalid command syntax") from error
    if not parts:
        raise TerminalSyntaxError("empty command")
    command, *arguments = parts
    if command == "help":
        if arguments:
            raise TerminalSyntaxError("help takes no arguments")
        return TerminalCommand(TerminalAction.HELP)
    if command == "job":
        return _parse_job_command(arguments)
    if command == "device":
        if arguments == ["list"]:
            return TerminalCommand(TerminalAction.DEVICE_LIST)
        if len(arguments) == 2 and arguments[0] == "show":
            if not _JOB_ID.fullmatch(arguments[1]):
                raise TerminalSyntaxError("invalid device ID")
            return TerminalCommand(TerminalAction.DEVICE_SHOW, device_id=arguments[1])
        raise TerminalSyntaxError("use device list or device show <device_id>")
    if command == "program":
        return _parse_program_command(arguments)
    if command == "wait":
        return _parse_wait_command(arguments)
    raise TerminalSyntaxError("unsupported command")


def _parse_wait_command(arguments: list[str]) -> TerminalCommand:
    if arguments == ["any"]:
        return TerminalCommand(TerminalAction.WAIT_ANY)
    if len(arguments) == 2 and arguments[0] == "request":
        if not _JOB_ID.fullmatch(arguments[1]):
            raise TerminalSyntaxError("invalid request ID")
        return TerminalCommand(TerminalAction.WAIT_REQUEST, request_id=arguments[1])
    if len(arguments) == 1:
        match = _WAIT_DURATION.fullmatch(arguments[0])
        if match is not None:
            amount, unit = match.groups()
            duration_us = float(amount) * {"us": 1.0, "ms": 1000.0, "s": 1_000_000.0}[unit]
            if duration_us > 0 and duration_us < float("inf"):
                return TerminalCommand(
                    TerminalAction.WAIT_DURATION, wait_duration_us=duration_us,
                )
    raise TerminalSyntaxError("use wait <duration-us|ms|s>, wait request <request_id>, or wait any")


def _parse_job_command(arguments: list[str]) -> TerminalCommand:
    if not arguments:
        raise TerminalSyntaxError("job action is required")
    action, *operands = arguments
    if action == "status":
        return _parse_job_status(_single_job_id(operands))
    if action == "result":
        return TerminalCommand(TerminalAction.JOB_RESULT, _single_job_id(operands))
    if action == "cancel":
        return TerminalCommand(TerminalAction.JOB_CANCEL, _single_job_id(operands))
    raise TerminalSyntaxError("unsupported job action")


def _single_job_id(operands: list[str]) -> str:
    """Current job commands take one ID; an action may later parse options."""

    if len(operands) != 1:
        raise TerminalSyntaxError("job action requires exactly one job ID")
    job_id = operands[0]
    if not _JOB_ID.fullmatch(job_id):
        raise TerminalSyntaxError("invalid job ID")
    return job_id


def _parse_job_status(job_id: str) -> TerminalCommand:
    return TerminalCommand(TerminalAction.JOB_STATUS, job_id)


def _parse_program_command(arguments: list[str]) -> TerminalCommand:
    if len(arguments) < 2 or arguments[0] != "submit":
        raise TerminalSyntaxError("use program submit <file> --device <id> --shots <n>")
    path = _workspace_name(arguments[1])
    options: dict[str, str] = {}
    remaining = arguments[2:]
    while remaining:
        option, *remaining = remaining
        if option not in {"--device", "--shots", "--request-id", "--initialization", "--interleave"}:
            raise TerminalSyntaxError("unknown program submit option")
        if option in options or not remaining or remaining[0].startswith("--"):
            raise TerminalSyntaxError("duplicate option or missing option value")
        options[option], *remaining = remaining
    if not {"--device", "--shots"} <= options.keys():
        raise TerminalSyntaxError("--device and --shots are required")
    if not _JOB_ID.fullmatch(options["--device"]):
        raise TerminalSyntaxError("invalid device ID")
    shots_text = options["--shots"]
    if not shots_text.isascii() or not shots_text.isdigit() or len(shots_text) > 10:
        raise TerminalSyntaxError("shots must be a positive bounded integer")
    shots = int(shots_text)
    if not 1 <= shots <= 2**31 - 1:
        raise TerminalSyntaxError("shots must be a positive bounded integer")
    request_id = options.get("--request-id")
    if request_id is not None and not _JOB_ID.fullmatch(request_id):
        raise TerminalSyntaxError("invalid request ID")
    initialization = options.get("--initialization", "prepare")
    if initialization not in PROGRAM_INITIALIZATIONS:
        raise TerminalSyntaxError("initialization must be prepare or inherit")
    interleave = options.get("--interleave")
    if interleave is not None:
        try:
            interleave_operation_id(interleave)
        except ValueError:
            raise TerminalSyntaxError("interleave must be victim, training:0 or training:1") from None
        if "--initialization" in options and initialization != "inherit":
            raise TerminalSyntaxError("--interleave needs --initialization inherit")
        initialization = "inherit"
    return TerminalCommand(
        TerminalAction.PROGRAM_SUBMIT, device_id=options["--device"],
        program_path=path, shots=shots, request_id=request_id, initialization=initialization,
        interleave=interleave,
    )


def _workspace_name(name: str) -> str:
    if (not isinstance(name, str) or not name or len(name.encode("utf-8")) > 256
            or any(c in name for c in "\\:\x00\r\n")
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise TerminalSyntaxError("invalid workspace circuit name")
    return name


class TerminalSession:
    """Dispatch parsed commands through the authenticated job client only."""

    def __init__(self, client: JobClient, workspace: TerminalWorkspace | None = None) -> None:
        self._client = client
        self.workspace = workspace or TerminalWorkspace()

    def execute(self, line: str, *, request_id: str | None = None) -> tuple[str, ...] | tuple[DeviceInfo, ...] | DeviceInfo | JobStatusView | JobResultView:
        if request_id is not None and (not isinstance(request_id, str) or not _JOB_ID.fullmatch(request_id)):
            raise TerminalSyntaxError("invalid transport request ID")
        command = parse_terminal_command(line)
        if command.action is TerminalAction.HELP:
            return (
                "help", "device list", "device show <device_id>",
                "program submit <file> --device <id> --shots <n> [--request-id <id>] [--initialization prepare|inherit] [--interleave victim|training:0|training:1]",
                "job status <job_id>", "job result <job_id>", "job cancel <job_id>",
                "wait <duration-us|ms|s>", "wait request <request_id>", "wait any",
            )
        if command.action in {TerminalAction.WAIT_DURATION, TerminalAction.WAIT_REQUEST,
                              TerminalAction.WAIT_ANY}:
            waiter = self._client if all(callable(getattr(self._client, name, None)) for name in
                                         ("wait_duration", "wait_request", "wait_any")) else None
            if waiter is None:
                raise TerminalWaitUnavailableError("the active client does not support explicit waits")
            if command.action is TerminalAction.WAIT_DURATION:
                return waiter.wait_duration(command.wait_duration_us)
            if command.action is TerminalAction.WAIT_REQUEST:
                return waiter.wait_request(command.request_id)
            return waiter.wait_any()
        if command.action is TerminalAction.DEVICE_LIST:
            return self._client.list_devices()
        if command.action is TerminalAction.DEVICE_SHOW:
            return self._client.get_device(command.device_id)
        if command.action is TerminalAction.PROGRAM_SUBMIT:
            request = ProgramSubmitRequest(
                program=self.workspace.read_circuit(command.program_path),
                device_id=command.device_id, shots=command.shots,
                idempotency_key=command.request_id or request_id or str(uuid4()),
                initialization=command.initialization, interleave=command.interleave,
            )
            return self._client.submit_program(request)
        if command.job_id is None:
            raise RuntimeError("job command has no job ID")
        if command.action is TerminalAction.JOB_STATUS:
            return self._client.get_status(command.job_id)
        if command.action is TerminalAction.JOB_RESULT:
            return self._client.get_result(command.job_id)
        if command.action is TerminalAction.JOB_CANCEL:
            return self._client.cancel(command.job_id)
        raise RuntimeError("unknown parsed command")

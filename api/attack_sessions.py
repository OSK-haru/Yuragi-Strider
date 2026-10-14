"""Opt-in, process-local ownership around an already established environment.

Construction, registration, execution and disposal are trusted supervisor calls,
not participant operations. No product-wide session limits are selected here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Condition, Lock, RLock
from typing import TypeVar

from api.attack_terminal import JobClient, TerminalWorkspace
from core.attack_simulation.job_service import DeviceInfo, JobApplicationService, JobResultView, JobStatusView
from core.attack_simulation.jobs import JobCommand
from core.attack_simulation.program_request import ProgramSubmitRequest
from core.attack_simulation.request_receipts import (
    ReceiptCapacityError, ReceiptLimits, ReceiptOutcome, ReceiptRecord,
    RequestReceipt, RequestReceiptLedger,
)
from core.attack_simulation.scenario_environment import ScenarioEnvironment
from core.attack_simulation.virtual_credentials import CredentialedJobConnection, VirtualCredentialAuthority


T = TypeVar("T")


class SessionCapacityError(RuntimeError):
    """Host protection, not a simulated attack failure or observation budget."""


class SessionBusyError(RuntimeError):
    """The host adapter is occupied; it does not modify simulated time."""


class WebOwnerContext:
    """Opaque in-process identity supplied by trusted real-owner authentication.

    Not a browser token, virtual credential, serialized user ID, or an
    authentication implementation. Construct/retain only in the trusted Web
    ownership resolver. Equal-looking or freshly constructed objects do not
    authorize another context's environments.
    """

    __slots__ = ()


@dataclass(frozen=True)
class SessionHostLimits:
    # Unique submissions over the environment's lifetime. Their idempotency
    # keys are kept even after job records expire, so this also bounds keys.
    max_jobs: int
    max_workspaces: int
    receipt_limits: ReceiptLimits
    # Jobs waiting in the queue at once; None leaves only max_jobs.
    max_queued_jobs: int | None = None

    def __post_init__(self) -> None:
        for value in (self.max_jobs, self.max_workspaces):
            if type(value) is not int or value < 1:
                raise ValueError("session host limits must be positive integers")
        if self.max_queued_jobs is not None and (
                type(self.max_queued_jobs) is not int or self.max_queued_jobs < 1):
            raise ValueError("session host limits must be positive integers")
        if not isinstance(self.receipt_limits, ReceiptLimits):
            raise TypeError("explicit receipt limits are required")


class _LimitedClient:
    """Keep both terminal and structured submissions on the same admission path."""

    def __init__(self, connection: CredentialedJobConnection, session: AttackSession,
                 principal: str, receipt: RequestReceipt) -> None:
        self._connection = connection
        self._session = session
        self._principal = principal
        self._receipt = receipt
        self._submitted = False

    def _check(self) -> JobApplicationService:
        if self._session._closed or self._session._service is None:
            raise PermissionError("environment is not available")
        self._session._receipts.validate(self._receipt, self._principal)
        self._connection.require_authenticated()
        return self._session._service

    def _capacity(self, key: tuple[str, str]) -> None:
        self._session._check_job_capacity(key)

    def submit_program(self, request: ProgramSubmitRequest) -> JobStatusView:
        with self._session._lock:
            service = self._check()
            if self._receipt.operation_id not in _ORDERED_OPERATIONS or self._submitted:
                raise PermissionError("submission requires its own ordered request")
            if not isinstance(request, ProgramSubmitRequest):
                raise TypeError("request must be a ProgramSubmitRequest")
            key = (self._principal, request.idempotency_key)
            self._capacity(key)
            self._submitted = True
        # Preparation is pure with respect to job/state ownership. Replays still
        # compile and compare content; host compilation never holds control.
        command = service._prepare_program(self._principal, request)
        with self._session._lock:
            if self._check() is not service:
                raise PermissionError("environment binding changed")
            self._capacity(key)
            result = service._admit_program(self._principal, command, receipt=self._receipt)
            self._session._accepted_requests.add(key)
            return result

    def _call(self, method: str, *args):
        with self._session._lock:
            service = self._check()
            if method == "cancel":
                # Queue mutation and close choose an atomic order. Projection
                # on read-only calls must not hold this control lock.
                return self._connection.cancel(*args)
        result = getattr(self._connection, method)(*args)
        with self._session._lock:
            if self._check() is not service:
                raise PermissionError("environment binding changed")
        return result

    def list_devices(self) -> tuple[DeviceInfo, ...]:
        return self._call("list_devices")

    def get_device(self, device_id: str) -> DeviceInfo:
        return self._call("get_device", device_id)

    def get_status(self, job_id: str) -> JobStatusView:
        return self._call("get_status", job_id)

    def get_result(self, job_id: str) -> JobResultView:
        return self._call("get_result", job_id)

    def cancel(self, job_id: str) -> JobStatusView:
        return self._call("cancel", job_id)


# Known submissions/workspace writes preserve ingress order. Unknown trusted
# callbacks are conservatively ordered; callers can declare a read operation.
_ORDERED_OPERATIONS = frozenset({
    "program.submit", "workspace.circuit.put", "terminal.execute", "application.request",
})


class AttackSession:
    """Trusted binding; never assemble a runtime from participant request JSON.

The assembler must provide matching environment/service/authority and route all
actor submissions through this adapter. Internal workload/budget integration is
not supplied by this class; notably it does not enable the legacy Reset task.
"""

    def __init__(self, *, environment: ScenarioEnvironment, service: JobApplicationService,
                 authority: VirtualCredentialAuthority, run_next: Callable[[], object],
                 limits: SessionHostLimits) -> None:
        if not environment.established:
            raise ValueError("establish and validate the environment before binding HTTP")
        if not isinstance(limits, SessionHostLimits) or not callable(run_next):
            raise TypeError("explicit host limits and a trusted executor are required")
        # Validate/allocate local bookkeeping before claiming shared owners.
        receipts = RequestReceiptLedger(limits.receipt_limits)
        manifest = environment.establish()
        service.validate_session_binding(manifest, run_next)
        self._manifest = manifest
        self._workload_owner: object | None = None
        self._web_owner_context: WebOwnerContext | None = None
        self._service: JobApplicationService | None = service
        self._authority = authority
        self._run_next: Callable[[], object] | None = run_next
        self._limits = limits
        self._workspaces: dict[str, TerminalWorkspace] = {}
        self._accepted_requests: set[tuple[str, str]] = set()
        self._receipts = receipts
        self._dispatch: dict[int, Future[None]] = {}
        self._dispatch_lock = RLock()
        self._lock = RLock()
        self._scope_lock = RLock()
        self._request_condition = Condition(self._lock)
        self._requests_in_flight = 0
        self._execution_lock = Lock()
        self._closed = False
        # The consistency check above is not a reservation. Recheck ownership
        # atomically, after local bookkeeping exists, before publishing a session.
        # Bind last: a rejected authority must not strand a runtime/service claim.
        service.claim_session(self)
        try:
            authority.bind_session(self, service.session_principal_ids)
        except BaseException:
            service._abort_session_claim(self)
            raise

    @property
    def manifest(self):
        """Supervisor assembly only; never a participant projection."""
        return self._manifest

    def claim_workload_runner(self, owner: object) -> None:
        """Fence an opt-in declared-only world before its first operation.

        Raw HTTP cannot share it until its host/world ingress mapping is wired.
        Closing this world does not thaw or relinquish the established binding.
        """
        if owner is None:
            raise ValueError("a trusted owner identity is required")
        with self._lock, self._scope_lock, self._dispatch_lock:
            if (self._closed or self._workload_owner is not None or self._requests_in_flight
                    or self._accepted_requests or self._receipts.records()):
                raise ValueError("declared runner requires a fresh unclaimed session")
            self._workload_owner = owner

    def validate_workload_binding(self, plan: object, *, role: str) -> None:
        """Trusted, read-only preflight before owner claim and clock start."""
        with self._lock:
            if self._closed or self._service is None or self._workload_owner is not None:
                raise ValueError("session is not available for workload assembly")
            service = self._service
        # Pure compilation must not delay revocation/disposal. Claim rechecks
        # closed/fresh ownership after all assembly validation has finished.
        service.validate_workload_binding(plan, role=role)
        with self._lock:
            if self._closed or self._service is not service or self._workload_owner is not None:
                raise ValueError("session changed during workload assembly")

    def _check_workload_owner(self, owner: object | None) -> None:
        if self._workload_owner is not None and owner is not self._workload_owner:
            raise PermissionError("this world requires declared ingress coordination")
        if self._workload_owner is None and owner is not None:
            raise PermissionError("world owner is not bound to this session")

    def workload_principal(self, owner: object, credential: str) -> str:
        """Trusted runner authentication; role/grants remain in the common service."""
        with self._lock:
            self._check_workload_owner(owner)
            if self._closed:
                raise PermissionError("environment is not available")
            return self._authority.resolve(credential)

    def _check_job_capacity(self, key: tuple[str, str]) -> None:
        # Caller holds control. Exact replays still reach common fingerprint
        # validation even when the host's unique-request capacity is exhausted.
        if key in self._accepted_requests:
            return
        if len(self._accepted_requests) >= self._limits.max_jobs:
            raise SessionCapacityError("host session admission capacity reached")
        queued_limit = self._limits.max_queued_jobs
        if (queued_limit is not None and self._service is not None
                and len(self._service._runtime.scheduler.queued_job_ids()) >= queued_limit):
            raise SessionCapacityError("host queued job capacity reached")

    def _check_live_program(self, owner: object, credential: str,
                            receipt: RequestReceipt) -> tuple[JobApplicationService, str]:
        # Trusted driver only: its own ledger validates exact receipt identity
        # and its grant orders admission. Never consult the legacy ledger here.
        if owner is None or owner is not self._workload_owner:
            raise PermissionError("live submission requires the bound workload owner")
        if self._closed or self._service is None:
            raise PermissionError("environment is not available")
        if type(receipt) is not RequestReceipt:
            raise TypeError("receipt must be an immutable RequestReceipt")
        if receipt.operation_id != "program.submit":
            raise PermissionError("live submission requires a program.submit receipt")
        principal = self._authority.resolve(credential)
        if principal != receipt.principal_id:
            raise PermissionError("live receipt belongs to another principal")
        return self._service, principal

    @contextmanager
    def _live_program_preparation(self, owner: object, credential: str,
                                  request: ProgramSubmitRequest, receipt: RequestReceipt):
        with self._lock:
            service, principal = self._check_live_program(owner, credential, receipt)
            if not isinstance(request, ProgramSubmitRequest):
                raise TypeError("request must be a ProgramSubmitRequest")
            self._check_job_capacity((principal, request.idempotency_key))
            self._requests_in_flight += 1
        try:
            # No session/control/physical gate may cover host compilation.
            yield service, principal
        finally:
            with self._lock:
                self._requests_in_flight -= 1
                self._request_condition.notify_all()

    def _compile_live_program(self, owner: object, credential: str,
                              request: ProgramSubmitRequest, receipt: RequestReceipt,
                              service: JobApplicationService, principal: str) -> JobCommand:
        command = service._prepare_program(principal, request)
        with self._lock:
            current, current_principal = self._check_live_program(owner, credential, receipt)
            if current is not service or current_principal != principal:
                raise PermissionError("environment binding changed during preparation")
        return command

    def prepare_live_program(self, owner: object, credential: str,
                             request: ProgramSubmitRequest, receipt: RequestReceipt) -> JobCommand:
        """Trusted live-driver preparation; no job admission or legacy receipt.

        The driver authenticates its ledger receipt before calling and compiles
        outside its ordering gate. Close drains compilation and fences its result.
        """
        with self._live_program_preparation(owner, credential, request, receipt) as (service, principal):
            return self._compile_live_program(owner, credential, request, receipt, service, principal)

    def admit_live_program(self, owner: object, credential: str,
                           command: JobCommand, receipt: RequestReceipt) -> JobStatusView:
        """Trusted live-driver ordered admission, not a participant/HTTP route.

        The driver must validate its exact receipt and admission grant before
        calling. Common service authorization, fingerprints and replay apply.
        """
        with self._lock:
            service, principal = self._check_live_program(owner, credential, receipt)
            if type(command) is not JobCommand:
                raise TypeError("admission requires a prepared JobCommand")
            key = (principal, command.idempotency_key)
            self._check_job_capacity(key)
            result = service._admit_program(principal, command, receipt=receipt)
            self._accepted_requests.add(key)
            return result

    def submit_live_program(self, owner: object, credential: str,
                            request: ProgramSubmitRequest, receipt: RequestReceipt) -> JobStatusView:
        """Trusted combined prepare/admit with lifecycle tracking throughout.

        A driver needing a gate only around admission uses the split methods;
        this helper itself never waits for or validates a driver's grant.
        """
        with self._live_program_preparation(owner, credential, request, receipt) as (service, principal):
            command = self._compile_live_program(owner, credential, request, receipt, service, principal)
            return self.admit_live_program(owner, credential, command, receipt)

    def commit_workload_transition(self, owner: object, credential: str,
                                   principal_id: str, update: Callable[[], T]) -> T:
        """Order a short trusted metadata update against session disposal.

        No compilation, numerical work, result projection or waiting is allowed
        in update. This is not a physical QPU/frontier transaction or a route.
        """
        with self._lock:
            self._check_workload_owner(owner)
            if self._closed or self._authority.resolve(credential) != principal_id:
                raise PermissionError("workload environment is not available")
            return update()

    def commit_coordinator_transition(self, owner: object, update: Callable[[], T]) -> T:
        """Commit trusted world metadata without requiring an actor credential."""
        if not callable(update):
            raise TypeError("a short trusted metadata transition is required")
        with self._lock:
            self._check_workload_owner(owner)
            if self._closed or self._service is None:
                raise PermissionError("workload environment is not available")
            return update()

    def _check_request_scope(self, workload_owner: object | None,
                             web_owner: WebOwnerContext | None) -> None:
        # Called under scope. Trusted declared runners already prove their
        # exact world ownership; raw participants must prove actual Web scope.
        self._check_workload_owner(workload_owner)
        if workload_owner is None:
            self._require_web_owner(web_owner)

    def receive(self, credential: str, operation_id: str, *, _workload_owner: object | None = None,
                _web_owner_context: WebOwnerContext | None = None) -> RequestReceipt:
        """Trusted ingress: record complete requests before worker/compile waits.

        Receipt sequence records host ingress, not world time or job admission.
        It is deliberately not a participant command or public result field.
        """
        with self._scope_lock, self._dispatch_lock:
            self._check_request_scope(_workload_owner, _web_owner_context)
            principal = self._authority.resolve(credential)
            try:
                receipt = self._receipts.reserve(principal, operation_id)
            except ReceiptCapacityError as error:
                raise SessionCapacityError("host pending receipt capacity reached") from error
            except (ValueError, RuntimeError):
                self._authority.resolve(credential)  # A concurrent close is authentication loss.
                raise
            if operation_id in _ORDERED_OPERATIONS:
                ready: Future[None] = Future()
                self._dispatch[receipt.sequence] = ready
                if len(self._dispatch) == 1:
                    ready.set_result(None)
            return receipt

    def dispatch_ready(self, credential: str, receipt: RequestReceipt) -> Future[None]:
        """Trusted transport gate. Await outside the numerical worker pool.

        It orders host preparation/admission, not virtual QPU timestamps. Async
        transports must shield this shared future from cancellation propagation.
        """
        with self._dispatch_lock:
            self._receipts.validate(receipt, self._authority.resolve(credential))
            ready = self._dispatch.get(receipt.sequence)
            if ready is None:
                ready = Future()
                ready.set_result(None)
            return ready

    def _release_dispatch(self, receipt: RequestReceipt) -> None:
        # No service/state calls under ingress synchronization. Never acquire
        # control while holding this lock; control -> ingress is the sole order.
        with self._dispatch_lock:
            removed = self._dispatch.pop(receipt.sequence, None)
            if removed is not None and not removed.done():
                removed.set_result(None)  # Retired waiters wake to ticket rejection.
            if self._dispatch:
                ready = next(iter(self._dispatch.values()))
                if not ready.done():
                    ready.set_result(None)

    def receipt_records(self) -> tuple[ReceiptRecord, ...]:
        """Supervisor diagnostics only; never expose these through job views."""
        return self._receipts.records()

    def abandon_receipt(self, receipt: RequestReceipt) -> None:
        """Release an ingress reservation if dispatch never reaches request()."""
        with self._dispatch_lock:
            if self._receipts.abandon(receipt):
                self._release_dispatch(receipt)

    async def request_async(
        self, credential: str, operation: Callable[[JobClient, TerminalWorkspace], T],
        *, receipt: RequestReceipt | None = None, operation_id: str = "application.request",
        worker: Callable[..., Awaitable[T]] = asyncio.to_thread,
        _workload_owner: object | None = None,
        _web_owner_context: WebOwnerContext | None = None,
    ) -> T:
        """Common asynchronous participant ingress, including trusted runners.

        Worker injection is a trusted transport concern, never participant JSON.
        The default uses the host executor; neither waiting nor compilation is
        interpreted as elapsed QPU time. No victim schedule is invented here.
        """
        with self._scope_lock:
            self._check_request_scope(_workload_owner, _web_owner_context)
        receipt = receipt if receipt is not None else self.receive(
            credential, operation_id, _workload_owner=_workload_owner,
            _web_owner_context=_web_owner_context)
        # A foreign caller must not gain cleanup authority over another ticket.
        with self._dispatch_lock:
            self._receipts.validate(receipt, self._authority.resolve(credential))
        try:
            await asyncio.shield(asyncio.wrap_future(self.dispatch_ready(credential, receipt)))
            return await worker(self.request, credential, operation, receipt=receipt,
                                _workload_owner=_workload_owner, _web_owner_context=_web_owner_context)
        finally:
            self.abandon_receipt(receipt)

    def request(self, credential: str, operation: Callable[[JobClient, TerminalWorkspace], T],
                *, receipt: RequestReceipt | None = None,
                operation_id: str = "application.request", _workload_owner: object | None = None,
                _web_owner_context: WebOwnerContext | None = None) -> T:
        # HTTP reserves before dispatch; trusted direct callers reserve here.
        with self._scope_lock:
            self._check_request_scope(_workload_owner, _web_owner_context)
            principal = self._authority.resolve(credential)
        receipt = receipt if receipt is not None else self.receive(
            credential, operation_id, _workload_owner=_workload_owner,
            _web_owner_context=_web_owner_context)
        # Synchronous compatibility for trusted callers. HTTP waits for this
        # gate asynchronously BEFORE dispatch, so waiting submissions cannot
        # occupy every worker and strand the earlier request behind them.
        self.dispatch_ready(credential, receipt).result()
        with self._lock:
            self._check_request_scope(_workload_owner, _web_owner_context)
            if self._authority.resolve(credential) != principal:
                raise PermissionError("request principal changed")
            self._receipts.claim(receipt, principal)
            self._requests_in_flight += 1
        try:
            with self._lock:
                if self._closed or self._service is None:
                    raise PermissionError("environment is not available")
                if self._authority.resolve(credential) != principal:
                    raise PermissionError("request principal changed")
                workspace = self._workspaces.get(principal)
                if workspace is None:
                    if len(self._workspaces) >= self._limits.max_workspaces:
                        raise SessionCapacityError("host workspace capacity reached")
                    workspace = TerminalWorkspace()
                    self._workspaces[principal] = workspace
                connection = CredentialedJobConnection(
                    authority=self._authority, service=self._service,
                    principal_id=principal, preconfigured_credential=credential,
                )
            result = operation(_LimitedClient(connection, self, principal, receipt), workspace)
            with self._lock:
                if self._closed:
                    raise PermissionError("environment is not available")
                job_id = result.job_id if isinstance(result, (JobStatusView, JobResultView)) else None
                self._receipts.finish(receipt, outcome=ReceiptOutcome.COMPLETED, job_id=job_id)
            return result
        except BaseException:
            # Do not retain exceptions, raw request bodies or credentials.
            self._receipts.finish(receipt, outcome=ReceiptOutcome.REJECTED)
            raise
        finally:
            with self._lock:
                self._release_dispatch(receipt)
                self._requests_in_flight -= 1
                self._request_condition.notify_all()

    def _bind_web_owner(self, context: WebOwnerContext) -> None:
        """Trusted assembly only; never derive ownership from actor credentials."""
        if type(context) is not WebOwnerContext:
            raise TypeError("an opaque real-owner context is required")
        with self._lock, self._scope_lock, self._dispatch_lock:
            if self._closed:
                raise ValueError("discarded environment cannot acquire real ownership")
            if self._web_owner_context is context:
                return
            if (self._closed or self._web_owner_context is not None
                    or self._workload_owner is not None
                    or self._requests_in_flight or self._receipts.records()
                    or self._accepted_requests or self._workspaces):
                raise ValueError("real ownership must bind before participant requests")
            self._web_owner_context = context

    def _require_web_owner(self, context: WebOwnerContext | None) -> None:
        with self._scope_lock:
            if self._web_owner_context is not context:
                raise PermissionError("environment is not available")

    def authenticate(self, credential: str, *, _web_owner_context: WebOwnerContext | None = None) -> None:
        with self._scope_lock:
            self._require_web_owner(_web_owner_context)
            self._authority.resolve(credential)

    def run_next(self, *, _workload_owner: object | None = None) -> object:
        """Supervisor-only. There is deliberately no corresponding HTTP route."""
        if not self._execution_lock.acquire(blocking=False):
            raise SessionBusyError("physical executor is already active")
        try:
            with self._lock:
                self._check_workload_owner(_workload_owner)
                if self._closed or self._run_next is None:
                    raise RuntimeError("environment is closed")
                executor = self._run_next
            # Queue/state records have their own short atomic locks. Matrix
            # computation must not lock participant admission or observation.
            return executor()
        finally:
            self._execution_lock.release()

    def run_declared_next(self, executor, *, _workload_owner: object | None = None):
        """Trusted explicit-trace execution; numerical work never holds control."""
        from core.attack_simulation.declared_execution import DeclaredQpuExecutor
        if not self._execution_lock.acquire(blocking=False):
            raise SessionBusyError("physical executor is already active")
        try:
            with self._lock:
                self._check_workload_owner(_workload_owner)
                if (self._closed or self._service is None or type(executor) is not DeclaredQpuExecutor
                        or executor.runtime is not self._service._runtime
                        or executor.runtime._declared_executor is not executor):
                    raise PermissionError("declared physical executor is not bound to this world")
            prepared = executor.prepare_next()
            with self._lock:
                self._check_workload_owner(_workload_owner)
                if self._closed:
                    raise PermissionError("environment was discarded during computation")
                return None if prepared is None else executor.commit(prepared)
        finally:
            self._execution_lock.release()

    def begin_declared_next(self, executor, *, _workload_owner: object | None = None):
        """Supervisor START boundary, independent of numerical FINISH readiness."""
        from core.attack_simulation.declared_execution import DeclaredQpuExecutor
        if not self._execution_lock.acquire(blocking=False):
            raise SessionBusyError("physical executor is already active")
        try:
            with self._lock:
                self._check_workload_owner(_workload_owner)
                if (self._closed or self._service is None or type(executor) is not DeclaredQpuExecutor
                        or executor.runtime is not self._service._runtime
                        or executor.runtime._declared_executor is not executor):
                    raise PermissionError("declared physical executor is not bound to this world")
            result = executor.begin_next()
            with self._lock:
                if self._closed:
                    raise PermissionError("environment was discarded during START preparation")
                return result
        finally:
            self._execution_lock.release()

    def workload_result_position(self, owner: object, credential: str, job_id: str):
        """Supervisor release evidence after common ownership/grant checks."""
        with self._lock:
            principal = self.workload_principal(owner, credential)
            job = self._service._visible_job(principal, job_id, "job.result.read")
            executor = self._service._runtime._declared_executor
            return None if executor is None else executor.release_position(job.job_id)

    def close(self) -> None:
        """Stop admission/revoke, finish current execution, release bindings.

        This does not reset physical state, undo completed shots, or restore a
        checkpoint. Assemblers holding other runtime references own their disposal.
        """
        with self._lock, self._scope_lock:
            self._fence_close()
        self._drain_close()

    def _begin_owned_close(self, context: WebOwnerContext | None) -> None:
        """Atomic registry disposal authorization and irreversible close fence."""
        with self._lock, self._scope_lock:
            self._require_web_owner(context)
            self._fence_close()

    def _fence_close(self) -> None:
        # Control and scope must be held. No numerical preparation or drain.
        self._closed = True
        self._authority.close()
        if self._service is not None and self._service._runtime._declared_executor is not None:
            self._service._runtime._declared_executor.close()
        with self._dispatch_lock:
            self._receipts.close()
            for ready in self._dispatch.values():
                if not ready.done():
                    ready.set_result(None)  # Wake to authentication loss, not admission.
            self._dispatch.clear()

    def _drain_close(self) -> None:
        # Do not hold the participant lock while waiting for physics to finish.
        with self._execution_lock, self._lock:
            # Pure preparation may still hold a workspace/service reference.
            # Drain it without holding control; its admission recheck fails.
            self._request_condition.wait_for(lambda: self._requests_in_flight == 0)
            self._workspaces.clear()
            self._accepted_requests.clear()
            self._service = None
            self._run_next = None


class AttackSessionRegistry:
    """Bounded trusted registry; no enumerate/create/remove participant endpoint."""

    def __init__(self, *, max_sessions: int) -> None:
        if type(max_sessions) is not int or max_sessions < 1:
            raise ValueError("max_sessions must be a positive integer")
        self._max_sessions = max_sessions
        self._sessions: dict[str, AttackSession] = {}
        self._lock = Lock()

    def register(self, session_id: str, session: AttackSession, *,
                 owner_context: WebOwnerContext | None = None) -> None:
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128 or not all(
            character.isascii() and (character.isalnum() or character in "._-") for character in session_id
        ):
            raise ValueError("invalid session ID")
        if type(session) is not AttackSession:
            raise TypeError("a common attack session is required")
        if owner_context is not None and type(owner_context) is not WebOwnerContext:
            raise TypeError("owner identity cannot be a credential or serialized user ID")
        with self._lock:
            if session_id in self._sessions:
                raise ValueError("session ID already registered")
            if len(self._sessions) >= self._max_sessions:
                raise SessionCapacityError("host session capacity reached")
            if owner_context is not None:
                session._bind_web_owner(owner_context)
            self._sessions[session_id] = session

    def authenticated(self, session_id: str, credential: str, *,
                      owner_context: WebOwnerContext | None = None) -> AttackSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise PermissionError("environment is not available")
        session.authenticate(credential, _web_owner_context=owner_context)
        return session

    def discard(self, session_id: str, *, owner_context: WebOwnerContext | None = None) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None and owner_context is not None:
                raise PermissionError("environment is not available")
            if session is not None:
                session._begin_owned_close(owner_context)
                del self._sessions[session_id]
        if session is not None:
            session._drain_close()

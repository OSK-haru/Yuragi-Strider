"""Opt-in trusted timed control over frozen settings and common authorities.

This joins cancellation, publication evidence and ALL bound result dependencies.
It is not an HTTP route or a live event loop: arrival is supplied only by the
trusted ingress/event coordinator, which must establish global event order.
Raw HTTP remains fenced from the declared world. No provider defaults, host
latency, secrets, reset, numerical work or automatic retries are introduced.
"""

from api.attack_workloads import DeclaredWorkloadRunner
from core.attack_simulation.declared_execution import DeclaredCancellationTrace, DeclaredQpuExecutor
from core.attack_simulation.declared_ingress import IngressReservation, _producer
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.live_service_config import parse_live_service_config


class DeclaredControlService:
    """Prepare before clock start; use the shared session's actor authorization."""

    def __init__(self, *, runner: DeclaredWorkloadRunner, executor: DeclaredQpuExecutor,
                 ingress_producer_id: str | None = None):
        if type(runner) is not DeclaredWorkloadRunner or type(executor) is not DeclaredQpuExecutor:
            raise TypeError("common declared owners are required")
        if ingress_producer_id is not None:
            _producer(ingress_producer_id)
        session, book = runner._session, runner._book
        config = parse_live_service_config(session.manifest,
            max_pending=book._max_pending, max_history=book._max_history)
        if (session._service is None or session._service._runtime is not executor.runtime
                or config.control_timing.clock_domain != executor._domain):
            raise ValueError("timed control requires matching world authorities")
        owner = object()
        with runner._dispatch_gate, runner._lock, executor._gate:
            runner._open()
            if (runner._clock._origin_ns is not None or runner._coordinator_owner is not None
                    or executor._control_owner is not None or executor._windows
                    or executor._releases or executor.runtime.scheduler._jobs
                    or executor.frontier.snapshot().target is not None or executor._closed):
                raise ValueError("timed control needs fresh unstarted authorities")
            if ingress_producer_id is not None and ingress_producer_id in book._owners:
                raise ValueError("receipt namespace cannot belong to a workload")
            if book._transaction_lock_bound:
                if book._lock is not executor._gate:
                    raise ValueError("ingress belongs to another transaction authority")
            else:
                book.bind_transaction_lock(executor._gate)
            executor.claim_control_coordinator(owner, ingress_producer_id=ingress_producer_id)
        self._runner, self._executor, self._owner = runner, executor, owner
        self._config = config

    @property
    def config(self):
        """Frozen established assumptions, not a mutable operation setting."""
        return self._config

    def bind_receipt_namespace(self, producer_id: str) -> None:
        """Trusted one-time pre-start binding, matching the driver's LiveIngress.

        This source cannot be a workload. Binding supplies no new actor grants,
        clock origin, receipt capture, physical seal or public API.
        """
        _producer(producer_id)
        runner, executor = self._runner, self._executor
        with runner._dispatch_gate, runner._lock, executor._gate:
            runner._open()
            runner._book._open()
            if runner._clock._origin_ns is not None or runner._clock._closed:
                raise ValueError("bind receipt namespace before clock start")
            if producer_id in runner._book._owners:
                raise ValueError("receipt namespace cannot belong to a workload")
            executor.bind_receipt_namespace(producer_id, owner=self._owner)

    def cancel(self, credential: str, job_id: str, request_id: str, *, arrival: EventPosition,
               ingress_reservation: IngressReservation | None = None) -> DeclaredCancellationTrace:
        """Trusted timeline input; return private trace, NOT a ready public reply.

        An actual response waits for trace.release. Exact replay reauthorizes,
        preserves application/release and does not repeat dependency changes.
        Lifecycle/grants and this short metadata transaction are ordered by the
        common session. The event driver supplies arrival, never a browser hint.
        Optional proof is the shared book's exact bound receipt reservation, retained
        at arrival even when the driver applies control after later admissions.
        """
        runner, executor = self._runner, self._executor
        principal = runner._session.workload_principal(runner._owner, credential)
        window = self.config.control_timing.plan_cancel(arrival)

        def update():
            runner._open()
            with executor._gate:
                service = runner._session._service
                service._visible_job(principal, job_id, "job.cancel")
                replay = executor._control_requests.get((principal, request_id))
                replacements = ()
                if replay is None:
                    barriers = [barrier for barrier in runner._book.snapshot().barriers
                        if runner._book._result_inputs.get(barrier.key, (None,))[0] == job_id]
                    replacements = tuple(runner._book.prepare_barrier_replacement(barrier,
                        runner._book.cancellation_successor_bound(barrier, job_id, window.release))
                        for barrier in barriers)
                return service._cancel_at(principal, job_id, owner=self._owner, request_id=request_id,
                    arrival=window.arrival, application=window.application, release=window.release,
                    book=runner._book, replacements=replacements,
                    ingress_reservation=ingress_reservation)

        return runner._session.commit_workload_transition(runner._owner, credential, principal, update)

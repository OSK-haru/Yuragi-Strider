"""Bounded pumping of a closed set of frozen actor workloads over common owners.

This is the finite-source integration of the nominal clock, common admission,
FIFO Program execution and own-result advancement. It is NOT a live HTTP ingress
owner or a public route. No unknown/control requests, ad-hoc service coefficients,
participant backdates, background retry loops or recovery are inferred here.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
from time import monotonic_ns

from api.attack_sessions import SessionBusyError
from api.attack_workloads import DeclaredWorkloadRunner, WorkloadInfrastructureError
from core.attack_simulation.declared_execution import DeclaredQpuExecutor
from core.attack_simulation.declared_service_timing import DeclaredServiceTiming
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.jobs import JobStatus
from core.attack_simulation.victim_workload import Phase


@dataclass(frozen=True)
class WorldPumpReport:
    """Supervisor diagnostics only: never an attacker projection."""
    target: EventPosition
    transitions: int
    finished: bool
    limit_reached: bool
    host_elapsed_ns: int = 0
    target_minus_confirmed_us: float = 0.0


class ClosedWorkloadWorld:
    """One finite submit-only source set, one retained-region FIFO executor.

    All source plans and service durations must be frozen before assembly.
    A caller periodically pumps with an explicit work bound; no manually
    declared windows, release callbacks or ingress seals are necessary. Long
    numeric work never holds the runner clock lock. Actual service loops/UI and
    live/control ingress are subsequent integrations, not silently enabled.
    """

    def __init__(self, *, runner: DeclaredWorkloadRunner, executor: DeclaredQpuExecutor):
        if type(runner) is not DeclaredWorkloadRunner or type(executor) is not DeclaredQpuExecutor:
            raise TypeError("the common declared runner and physical executor are required")
        if (runner._session._service is None
                or runner._session._service._runtime is not executor.runtime
                or executor.runtime._declared_executor is not executor):
            raise ValueError("world owners must belong to the same common session")
        config = runner._session.manifest.qpu_service_config.get("declared_service_timing")
        fields = {"processing_delay_us", "publication_delay_us", "denial_delay_us", "basis"}
        if not isinstance(config, Mapping) or set(config) != fields:
            raise ValueError("freeze explicit service durations and their basis before establishment")
        basis = config["basis"]
        if type(basis) is not str or not 1 <= len(basis.strip()) <= 2048:
            raise ValueError("service durations need a bounded explicit assumption/source basis")
        domain = runner._clock.settings.clock_domain
        if executor.frontier._clock_domain != domain:
            raise ValueError("world owners use different clock domains")
        timing = DeclaredServiceTiming(domain, **{k: config[k] for k in fields - {"basis"}})
        if (executor.frontier.snapshot().target is not None or executor._windows
                or executor.runtime.scheduler._jobs or executor._closed
                or executor._control_owner is not None):
            raise ValueError("closed world requires fresh physical owners")
        self._runner, self._executor, self._timing = runner, executor, timing
        self._owner = object()
        self._pump_gate = Lock()
        self._windows = {}  # Same explicit executor max_jobs bound; no extra history.
        self._closed = False
        runner.claim_world_coordinator(self._owner)  # Last: failed validation claims nothing.

    def _open(self):
        if self._closed:
            raise WorkloadInfrastructureError("closed-source world has been discarded")

    def start(self) -> EventPosition:
        self._open()
        return self._runner.start(_coordinator_owner=self._owner)

    def target(self) -> EventPosition:
        """Nominal time remains readable while compile or physics is blocked."""
        self._open()
        return self._runner.target()

    def _sample_target(self):
        sample = self.target()
        orders = [e.arrival.order for s in self._runner.states().values() for e in s.events]
        orders.extend(p.order for w in self._windows.values()
                      for p in (w.start, w.finish, w.release))
        prior = self._executor.frontier.snapshot().target
        order = max(orders, default=0) + 1
        if prior is not None and prior.time_us == sample.time_us:
            order = max(order, prior.order)
        through = EventPosition(sample.clock_domain, sample.time_us, order)
        self._executor.frontier.set_target(through)
        return through

    def _waiting(self):
        return [(producer, state, event) for producer, state in self._runner.states().items()
                for event in state.events if event.phase is Phase.WAITING]

    def _head_window(self):
        executor = self._executor
        if executor._active is not None:
            job, window, _, _ = executor._active
            return job, window
        queued = executor.runtime.scheduler.queued_job_ids()
        if not queued:
            return None
        job = executor.runtime.scheduler.get(queued[0])
        if job.job_id in self._windows:
            return job, self._windows[job.job_id]
        event = next((e for _, _, e in self._waiting() if e.job_id == job.job_id), None)
        if event is None:
            raise WorkloadInfrastructureError("unregistered source reached the closed-world queue")
        window = self._timing.plan_job_window(job, executor.runtime.config,
            previous_finish=executor.frontier.snapshot().confirmed,
            previous_tenant_id=executor.runtime.state_store.read().owner_tenant_id, arrival=event.arrival)
        executor.register_job(job.job_id, window)
        self._windows[job.job_id] = window
        return job, window

    def _refine_dependencies(self, head):
        """Prove successor bounds, never drop the current producer's dependency.

        Every source in this capability is submit-only. An admitted future job
        cannot return before the registered FIFO head finishes. With no live
        cancellation/control path, that lower bound is valid even before START.
        Unknown terminal transitions are rejected instead of guessing their time.
        """
        for producer, _, event in self._waiting():
            if event.job_id is None:
                lower = self._timing.denial_release(event.arrival)
            else:
                job = self._executor.runtime.scheduler.get(event.job_id)
                if job.status is JobStatus.SUCCEEDED and event.job_id not in self._executor._finished:
                    raise WorkloadInfrastructureError("uncoordinated physical success in closed world")
                if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.SUCCEEDED}:
                    raise WorkloadInfrastructureError("unmapped control/failure outcome in closed world")
                window = self._windows.get(event.job_id)
                if window is not None:
                    lower = window.release
                elif head is not None:
                    finish = head[1].finish
                    lower = EventPosition(finish.clock_domain, max(finish.time_us, event.arrival.time_us),
                        finish.order + 1 if finish.time_us >= event.arrival.time_us else event.arrival.order)
                else:
                    raise WorkloadInfrastructureError("queued workload has no FIFO physical head")
            self._runner.refine_result_bound(producer, event.event_id, available=lower,
                                            _coordinator_owner=self._owner)

    def _actions(self, through, head):
        actions = []
        ingress = self._runner._book.snapshot(through)
        t = ingress.eligible
        if t is None and ingress.reservations:
            candidate = ingress.reservations[0]
            if (candidate.position.key <= through.key and not any(
                    b.key != candidate.key and b.earliest_next_arrival.key <= candidate.position.key
                    for b in ingress.barriers)):
                # snapshot hides eligibility at a full history bound. Let the
                # actual admission owner report capacity rather than pretending
                # the due world is merely idle; it will not invoke the callback.
                t = candidate
        if t is not None:
            actions.append((t.position.key, 1, t.producer_id, t.event_id, "admit", None))
        for producer, _, event in self._waiting():
            if event.job_id is None:
                release = self._timing.denial_release(event.arrival)
            elif event.job_id in self._executor._finished:
                release = self._windows[event.job_id].release
            else:
                continue
            if release.key <= through.key:
                actions.append((release.key, 0, producer, event.event_id, "release", release))
        if head is not None:
            job, window = head
            phase = "finish" if job.status is JobStatus.RUNNING else "start"
            position = window.finish if phase == "finish" else window.start
            if (position.key <= through.key
                    and not any(t.position.key <= position.key for t in ingress.reservations)
                    and not any(b.earliest_next_arrival.key <= position.key for b in ingress.barriers)):
                actions.append((position.key, 2, "", 0, phase, position))
        return sorted(actions)

    def pump(self, *, max_transitions: int) -> WorldPumpReport:
        """Bounded catch-up, with explicit retry by caller after infrastructure error.

        Reaching this bound is host backlog, not a simulated quota/denial. No
        event is evicted and a later pump continues exactly the same causal trace.
        """
        if type(max_transitions) is not int or not 1 <= max_transitions <= 16384:
            raise ValueError("pump needs a positive finite transition limit <= 16384")
        if not self._pump_gate.acquire(blocking=False):
            raise SessionBusyError("world catch-up is already active")
        try:
            host_started_ns = monotonic_ns()
            self._open()
            count = 0
            for _ in range(max_transitions):
                self._open()
                through = self._sample_target()
                if self._runner.ingress_snapshot().granted is not None:
                    # A lost admission response may have already created the
                    # FIFO head. Bind that exact replay before interpreting the
                    # queue as an unregistered source or scheduling any physics.
                    self._runner.dispatch_next(_coordinator_owner=self._owner)
                    count += 1
                    continue
                head = self._head_window()
                self._refine_dependencies(head)
                actions = self._actions(through, head)
                if not actions:
                    break
                _, _, producer, event_id, kind, position = actions[0]
                if kind == "admit":
                    self._runner.dispatch_next(_coordinator_owner=self._owner)
                elif kind == "release":
                    self._runner.complete_result(producer, event_id, available=position,
                                                 _coordinator_owner=self._owner)
                else:
                    self._runner.seal_physical_through(self._executor, position,
                                                      _coordinator_owner=self._owner)
                    if kind == "start":
                        self._runner._session.begin_declared_next(
                            self._executor, _workload_owner=self._runner._owner)
                    else:
                        self._runner._session.run_declared_next(
                            self._executor, _workload_owner=self._runner._owner)
                count += 1
            through = self._sample_target()
            finished = all(s.finished for s in self._runner.states().values())
            confirmed = self._executor.frontier.snapshot().confirmed
            return WorldPumpReport(
                through, count, finished, count == max_transitions and not finished,
                monotonic_ns() - host_started_ns,
                max(0.0, through.time_us - (confirmed.time_us if confirmed is not None else 0.0)),
            )
        finally:
            self._pump_gate.release()

    def close(self):
        # Fence promptly before waiting for numerical drain; do not hold pump
        # gate while the session interrupts/revokes its current operation.
        self._closed = True
        self._runner.close()

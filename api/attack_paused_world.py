"""D60 explicit-wait control over one LiveWorkloadWorld; no HTTP mount.

The world's host clock is a virtual clock moved only by the attacker's waits.
Operations are captured at the paused world time. A wait advances event by
event to its exact stop position, so the same operation sequence yields the
same world regardless of host compute speed or user think time.
"""

from dataclasses import dataclass
from enum import Enum
import math
from threading import Event, Lock

from api.attack_live_world import LiveWorkloadWorld
from core.attack_simulation.live_time_policy import (
    EXPLICIT_WAIT, LiveTimePolicy,
)
from core.attack_simulation.request_receipts import RequestReceipt

_PUMP_TRANSITIONS = 16384


class VirtualHostClock:
    """Injectable host_ns source for a paused world; only the controller moves it."""

    def __init__(self, start_ns: int = 1_000_000_000):
        if type(start_ns) is not int or start_ns < 0:
            raise ValueError("start_ns must be a nonnegative integer")
        self.value = start_ns
        self._lock = Lock()

    def __call__(self) -> int:
        return self.value

    def advance_to(self, ns: int) -> None:
        with self._lock:
            if type(ns) is not int or ns < self.value:
                raise ValueError("the virtual clock cannot move backwards")
            self.value = ns


class WaitOutcome(Enum):
    REACHED = "reached"
    LIMITED = "limited"
    NOTHING_PENDING = "nothing_pending"


@dataclass(frozen=True)
class WaitReport:
    """World time is attacker-visible in this mode: it is the attacker's own clock."""
    outcome: WaitOutcome
    world_time_us: float


class WaitLimitError(RuntimeError):
    """A wait exceeded its frozen step bound before the world settled."""


class PausedWorldController:
    """Serializes operations and waits so capture never races an advance.

    Do not attach a background LiveWorldDriver to the same world; it would
    pump without moving time and only contend for the pump gate.
    """

    def __init__(self, world: LiveWorkloadWorld, clock: VirtualHostClock, policy: LiveTimePolicy):
        if (type(world) is not LiveWorkloadWorld or type(clock) is not VirtualHostClock
                or type(policy) is not LiveTimePolicy):
            raise TypeError("a live world, its virtual clock and a frozen policy are required")
        if policy.mode != EXPLICIT_WAIT:
            raise ValueError("this controller implements the explicit_wait policy only")
        if not world.uses_clock_source(clock):
            raise ValueError("the virtual clock must be the world's host clock")
        if policy != world.live_time_policy:
            raise ValueError("controller policy must match the established manifest")
        self._world, self._clock, self._policy = world, clock, policy
        self._lock = Lock()
        self._close_requested = Event()
        self._close_gate = Lock()

    @property
    def live_time_policy(self) -> LiveTimePolicy:
        return self._policy

    def controls_same_world(self, other) -> bool:
        return type(other) is PausedWorldController and self._world is other._world

    @classmethod
    def from_manifest(cls, world: LiveWorkloadWorld, clock: VirtualHostClock) -> "PausedWorldController":
        return cls(world, clock, world.live_time_policy)

    # Operations: captured at the paused time; none of them advances the world.

    def now(self) -> float:
        with self._lock:
            return self._world.target().time_us

    def submit(self, credential, request, *, owner_context=None):
        with self._lock:
            return self._world.submit(credential, request, owner_context=owner_context)

    def cancel(self, credential, job_id, *, owner_context=None):
        with self._lock:
            return self._world.cancel(credential, job_id, owner_context=owner_context)

    def status(self, credential, job_id, *, owner_context=None):
        with self._lock:
            return self._world.status(credential, job_id, owner_context=owner_context)

    def result(self, credential, job_id, *, owner_context=None):
        with self._lock:
            return self._world.result(credential, job_id, owner_context=owner_context)

    def victim_activity(self, credential, job_id, *, owner_context=None):
        with self._lock:
            return self._world.victim_activity(credential, job_id, owner_context=owner_context)

    def reply(self, credential, receipt, *, owner_context=None):
        with self._lock:
            return self._world.reply(credential, receipt, owner_context=owner_context)

    # Waits: the only way world time moves.

    def wait(self, credential, duration_us, *, owner_context=None) -> WaitReport:
        if (type(duration_us) not in {int, float} or not math.isfinite(duration_us)
                or not 0 < duration_us <= self._policy.max_wait_us):
            raise ValueError("wait duration must be positive and within max_wait_us")
        with self._lock:
            self._check_available()
            self._authorize(credential, owner_context)
            goal = self._world.target().time_us + float(duration_us)
            return self._advance(lambda: self._world.target().time_us >= goal, goal)

    def wait_reply(self, credential, receipt, *, owner_context=None) -> WaitReport:
        with self._lock:
            self._check_available()
            principal = self._authorize(credential, owner_context)
            if type(receipt) is not RequestReceipt or receipt.principal_id != principal:
                raise PermissionError("request is unavailable")
            if not self._known(receipt):
                raise LookupError("request is outside the retained window")
            goal = self._world.target().time_us + self._policy.max_wait_us
            return self._advance(lambda: self._released(receipt), goal)

    def wait_any(self, credential, *, owner_context=None) -> WaitReport:
        with self._lock:
            self._check_available()
            principal = self._authorize(credential, owner_context)
            pending = self._world.pending_receipts_for(principal)
            if not pending:
                self._check_available()
                return WaitReport(WaitOutcome.NOTHING_PENDING, self._world.target().time_us)
            goal = self._world.target().time_us + self._policy.max_wait_us
            return self._advance(lambda: any(self._released(r) for r in pending), goal)

    # Transport support: authentication and reply readiness, no world progress.

    def principal_for(self, credential, *, owner_context=None) -> str:
        """Authenticate a connection's attacker; grants no service operation."""
        with self._lock:
            return self._authorize(credential, owner_context)

    def max_program_qubits(self, principal_id: str) -> int | None:
        return self._world.max_program_qubits(principal_id)

    def is_reply_released(self, receipt) -> bool:
        with self._lock:
            return self._released(receipt)

    def request_world_time_us(self, receipt) -> float:
        with self._lock:
            return self._world.request_world_time_us(receipt)

    def close(self) -> None:
        self.request_close()
        with self._close_gate:
            # The world/session fence is independent of the wait serialization
            # lock. A detached numerical preparation may still have to drain.
            self._world.close()

    def request_close(self) -> None:
        """Fence waits immediately, before the lifecycle owner awaits their drain."""
        self._close_requested.set()

    # Internals.

    def _authorize(self, credential, owner_context):
        self._check_available()
        try:
            principal = self._world.authorize_wait(credential, owner_context)
        except (PermissionError, RuntimeError):
            self._check_available()
            raise
        self._check_available()
        return principal

    def _known(self, receipt):
        return self._world.known_receipt_for(receipt.principal_id, receipt)

    def _released(self, receipt):
        return self._world.is_reply_released(receipt)

    def _settle(self):
        for _ in range(self._policy.max_steps_per_wait):
            self._check_available()
            if not self._world.pump(max_transitions=_PUMP_TRANSITIONS).limit_reached:
                self._check_available()
                return
        raise WaitLimitError("the world did not settle within the frozen step bound")

    def _ns(self, world_us, *, after):
        return self._world.host_ns_for_world_time(world_us, after=after)

    def _next_event_us(self, horizon_us):
        return self._world.earliest_pending_event_time_us(horizon_us)

    def _advance(self, satisfied, goal_us):
        self._settle()
        for _ in range(self._policy.max_steps_per_wait):
            self._check_available()
            now = self._world.target().time_us
            if satisfied():
                self._check_available()
                return WaitReport(WaitOutcome.REACHED, now)
            if now >= goal_us:
                self._check_available()
                return WaitReport(WaitOutcome.LIMITED, now)
            goal_ns = self._ns(goal_us, after=False)
            nxt = self._next_event_us(goal_us)
            ns = goal_ns if nxt is None or nxt >= goal_us else min(self._ns(nxt, after=True), goal_ns)
            self._world.advance_host_clock_to(max(ns, self._world.host_clock_value_ns() + 1))
            self._settle()
        self._check_available()
        return WaitReport(WaitOutcome.LIMITED, self._world.target().time_us)

    def _check_available(self):
        if self._close_requested.is_set():
            raise RuntimeError("world is unavailable")

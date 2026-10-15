"""Serialized client operations for a host-clock LiveWorkloadWorld."""

from __future__ import annotations

from threading import Event, Lock

from api.attack_live_world import LiveWorkloadWorld
from core.attack_simulation.live_time_policy import HOST_CLOCK, LiveTimePolicy


class HostClockWorldController:
    """Host-clock counterpart to the test-oriented explicit-wait controller.

    It deliberately exposes no wait/advance operation. The background driver
    advances declared work independently, and the WebSocket observes released
    replies without granting a client clock-control operation.
    """

    def __init__(self, world: LiveWorkloadWorld) -> None:
        if type(world) is not LiveWorkloadWorld or world.live_time_policy.mode != HOST_CLOCK:
            raise TypeError("a host_clock live world is required")
        self._world = world
        self._policy: LiveTimePolicy = world.live_time_policy
        self._lock = Lock()
        self._closed = Event()

    @property
    def live_time_policy(self) -> LiveTimePolicy:
        return self._policy

    def controls_same_world(self, other) -> bool:
        return getattr(other, "_world", None) is self._world

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

    def principal_for(self, credential, *, owner_context=None):
        with self._lock:
            if self._closed.is_set():
                raise RuntimeError("world is unavailable")
            return self._world.authorize_wait(credential, owner_context)

    def max_program_qubits(self, principal_id):
        return self._world.max_program_qubits(principal_id)

    def can_read_victim_activity(self, principal_id):
        return self._world.can_read_victim_activity(principal_id)

    def is_reply_released(self, receipt):
        return self._world.is_reply_released(receipt)

    def request_world_time_us(self, receipt):
        return self._world.request_world_time_us(receipt)

    def pending_receipts_for(self, principal_id):
        return self._world.pending_receipts_for(principal_id)

    def request_close(self):
        self._closed.set()

    def close(self):
        self.request_close()
        self._world.close()

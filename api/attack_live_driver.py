"""Bounded background pump for one LiveWorkloadWorld; no HTTP mount.

The driver only calls the world's finite ``pump``. Pump granularity does not
change declared physics, so this moves catch-up off the request path without
adding device time, retries of participant operations or new service rules.
The first failure stops the driver; numerical/admission recovery stays an
explicit retry by the supervisor, as in the manual pump contract.
"""

from dataclasses import dataclass
from enum import Enum
from threading import Event, Lock, Thread

from api.attack_live_world import LiveWorkloadWorld
from api.attack_sessions import SessionBusyError
from api.attack_workloads import WorkloadInfrastructureError
from core.attack_simulation.live_time_policy import HOST_CLOCK

MAX_TRANSITIONS_PER_TICK = 16384


class LiveDriverState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    WORLD_CLOSED = "world_closed"
    FAULTED = "faulted"


@dataclass(frozen=True)
class LiveDriverSettings:
    """Host scheduling only. None of these values enter world time."""
    idle_interval_s: float
    max_transitions_per_tick: int
    stop_timeout_s: float

    def __post_init__(self):
        if (type(self.idle_interval_s) not in {int, float}
                or not 0.001 <= self.idle_interval_s <= 60.0):
            raise ValueError("idle interval must be between 1 ms and 60 s")
        if (type(self.max_transitions_per_tick) is not int
                or not 1 <= self.max_transitions_per_tick <= MAX_TRANSITIONS_PER_TICK):
            raise ValueError("per-tick transition limit is out of range")
        if type(self.stop_timeout_s) not in {int, float} or not 0.0 < self.stop_timeout_s <= 600.0:
            raise ValueError("stop timeout must be positive and at most 600 s")


@dataclass(frozen=True)
class LiveDriverStatus:
    """Supervisor diagnostics only: never an attacker projection."""
    state: LiveDriverState
    ticks: int
    transitions: int
    last_error: str | None
    last_report: object | None


class LiveWorldDriver:
    """One daemon thread per world, started and stopped explicitly.

    A tick pumps at most ``max_transitions_per_tick``. A tick that hits its
    limit is followed immediately by another; otherwise the thread sleeps for
    the idle interval or until ``wake()``. A concurrent manual pump is skipped,
    not retried in a loop. Any other exception stops the driver in FAULTED
    without retrying: the supervisor retries explicitly (manual ``pump`` or a
    new driver), so an interrupted RUNNING job is never resumed silently.
    """

    def __init__(self, world: LiveWorkloadWorld, settings: LiveDriverSettings):
        if type(world) is not LiveWorkloadWorld or type(settings) is not LiveDriverSettings:
            raise TypeError("a live world and explicit driver settings are required")
        if world.live_time_policy.mode != HOST_CLOCK:
            raise ValueError("the background driver is available only for host_clock worlds")
        self._world, self._settings = world, settings
        self._wake, self._stop = Event(), Event()
        self._lock = Lock()
        self._thread = None
        self._state = LiveDriverState.IDLE
        self._ticks = self._transitions = 0
        self._last_error = None
        self._last_report = None

    def start(self):
        with self._lock:
            if self._state is not LiveDriverState.IDLE:
                raise RuntimeError("a driver starts once; create a new one after stop/fault")
            self._state = LiveDriverState.RUNNING
            self._thread = Thread(target=self._run, name="attack-live-pump", daemon=True)
            self._thread.start()

    def wake(self):
        """Hint after new ingress; never changes ordering or world time."""
        self._wake.set()

    def stop(self):
        """Stop pumping; does not discard the world. Return False on join timeout."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(self._settings.stop_timeout_s)
            if thread.is_alive():
                return False
        with self._lock:
            if self._state in {LiveDriverState.IDLE, LiveDriverState.RUNNING}:
                self._state = LiveDriverState.STOPPED
        return True

    def close(self):
        """Stop the driver first, then discard the world."""
        stopped = self.stop()
        self._world.close()
        return stopped

    def status(self):
        with self._lock:
            return LiveDriverStatus(self._state, self._ticks, self._transitions,
                                    self._last_error, self._last_report)

    def _finish(self, state, error=None):
        with self._lock:
            self._state = state
            if error is not None:
                self._last_error = error

    def _run(self):
        settings = self._settings
        while not self._stop.is_set():
            again = False
            should_pause = self._world.live_time_policy.pause_while_catching_up
            paused = False
            try:
                if should_pause:
                    self._world.set_compute_catch_up_paused(True)
                    paused = True
                report = self._world.pump(max_transitions=settings.max_transitions_per_tick)
            except SessionBusyError:
                pass  # A manual pump holds the gate; the next tick tries once.
            except WorkloadInfrastructureError:
                # Discard/binding loss, or an unresolved numerical/admission
                # step that needs explicit recovery. Never spin on it.
                if self._world._closed or self._world._runner._session._closed:
                    self._finish(LiveDriverState.WORLD_CLOSED)
                else:
                    self._finish(LiveDriverState.FAULTED, "WorkloadInfrastructureError")
                if paused:
                    self._world.set_compute_catch_up_paused(False)
                return
            except Exception as error:  # Diagnostics keep the type, not the message.
                self._finish(LiveDriverState.FAULTED, type(error).__name__)
                if paused:
                    self._world.set_compute_catch_up_paused(False)
                return
            else:
                with self._lock:
                    self._ticks += 1
                    self._transitions += report.transitions
                    self._last_report = report
                again = report.limit_reached
            if paused and not again:
                self._world.set_compute_catch_up_paused(False)
            if not again:
                self._wake.wait(settings.idle_interval_s)
                self._wake.clear()
        if self._world.live_time_policy.pause_while_catching_up:
            self._world.set_compute_catch_up_paused(False)

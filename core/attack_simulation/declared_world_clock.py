"""Run-local host/world mapping; neither a device clock nor a commit frontier.

The trusted assembler marks all owners ready before start. Construction never
samples host time. There is deliberately no restore/restart-origin API.
"""

from dataclasses import dataclass
import math
from threading import Lock
from time import monotonic_ns
from typing import Callable
from time import monotonic_ns

from core.attack_simulation.event_commit_boundary import EventPosition


def seconds_to_world_us(seconds: int | float) -> float:
    """Convert a declared duration, without applying the host clock rate."""
    if type(seconds) not in (int, float):
        raise ValueError("seconds must be finite and nonnegative")
    try:
        value = float(seconds) * 1_000_000
    except OverflowError:
        raise ValueError("seconds exceed the supported world range") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError("seconds must be finite and nonnegative")
    return value


@dataclass(frozen=True)
class WorldClockSettings:
    clock_domain: str
    rate: float = 1.0

    def __post_init__(self) -> None:
        EventPosition(self.clock_domain, 0, 0)
        if type(self.rate) not in (int, float):
            raise ValueError("clock rate must be finite and positive")
        try:
            rate = float(self.rate)
        except OverflowError:
            raise ValueError("clock rate exceeds the supported range") from None
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("clock rate must be finite and positive")
        object.__setattr__(self, "rate", rate)


class DeclaredWorldClock:
    """Nondecreasing sampling, with explicit ties assigned by the coordinator.

    Positive finite rates are supported while mapped coordinates remain finite.
    Repeated host samples and equal world coordinates due to rounding are valid.
    This clock never invents or increments tie order. No host timestamp is
    included in public return values.
    """

    def __init__(self, settings: WorldClockSettings, *,
                 host_ns: Callable[[], int] = monotonic_ns) -> None:
        if type(settings) is not WorldClockSettings or not callable(host_ns):
            raise TypeError("immutable settings and an injectable host clock are required")
        self._settings = settings
        self._host_ns = host_ns
        self._ready = False
        self._origin_ns: int | None = None
        self._last_ns: int | None = None
        self._closed = False
        self._lock = Lock()

    @property
    def settings(self) -> WorldClockSettings:
        return self._settings

    def mark_ready(self, *, runtime_ready: bool, ingress_ready: bool,
                   workload_ready: bool, manifest_frozen: bool) -> None:
        with self._lock:
            self._open()
            if self._ready or self._origin_ns is not None:
                raise RuntimeError("readiness is a one-time assembly boundary")
            if any(value is not True for value in
                   (runtime_ready, ingress_ready, workload_ready, manifest_frozen)):
                raise ValueError("all run owners must be ready and settings frozen")
            self._ready = True

    def start(self) -> EventPosition:
        with self._lock:
            self._open()
            if not self._ready or self._origin_ns is not None:
                raise RuntimeError("start requires readiness and cannot be repeated")
            origin = self._validate_ns(self._host_ns())
            self._origin_ns = self._last_ns = origin
            return EventPosition(self._settings.clock_domain, 0, 0)

    def target(self, *, order: int = 0) -> EventPosition:
        """Read nominal world progress, independently of committed physics."""
        with self._lock:
            self._running()
            sampled = self._validate_ns(self._host_ns())
            if sampled < self._last_ns:
                raise ValueError("host samples cannot move backwards")
            position = self._map(sampled, order)
            self._last_ns = sampled
            return position

    def _map_receipt_ns(self, received_at_ns: int, *, order: int) -> EventPosition:
        """Trusted in-process receipt mapping, never a client backdate API.

        Accept only this run's already sampled interval. Receipt order may
        differ from mapping-call order; this does not rewind the target. The
        eventual ingress owner must synchronize sampling, receipt capture and
        sealing; this helper cannot establish an exhaustive live watermark.
        """
        with self._lock:
            self._running()
            received = self._validate_ns(received_at_ns)
            if not self._origin_ns <= received <= self._last_ns:
                raise ValueError("receipt is outside the observed run interval")
            return self._map(received, order)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def _open(self) -> None:
        if self._closed:
            raise RuntimeError("world clock is closed")

    def _running(self) -> None:
        self._open()
        if self._origin_ns is None:
            raise RuntimeError("world clock has not started")

    @staticmethod
    def _validate_ns(value: int) -> int:
        if type(value) is not int or value < 0:
            raise ValueError("host clock must supply nonnegative integer nanoseconds")
        return value

    def _map(self, sampled: int, order: int) -> EventPosition:
        try:
            elapsed_us = (sampled - self._origin_ns) / 1000 * self._settings.rate
        except OverflowError:
            raise ValueError("mapped world position exceeds the supported range") from None
        return EventPosition(self._settings.clock_domain, elapsed_us, order)


class CatchUpMonotonicSource:
    """Monotonic host source that can exclude local compute time when frozen.

    This does not advance, rewind, or synthesize QPU time. It only maps elapsed
    host time while unpaused; callers decide when numerical catch-up is active.
    """

    def __init__(self, clock: Callable[[], int] = monotonic_ns) -> None:
        if not callable(clock):
            raise TypeError("a monotonic nanosecond clock is required")
        self._clock = clock
        self._lock = Lock()
        self._paused_at: int | None = None
        self._paused_total = 0

    def __call__(self) -> int:
        with self._lock:
            now = self._clock()
            if type(now) is not int or now < 0:
                raise ValueError("host clock must return nonnegative integer nanoseconds")
            return (self._paused_at if self._paused_at is not None else now) - self._paused_total

    def pause(self) -> None:
        with self._lock:
            if self._paused_at is None:
                self._paused_at = self._clock()

    def resume(self) -> None:
        with self._lock:
            if self._paused_at is not None:
                now = self._clock()
                self._paused_total += max(0, now - self._paused_at)
                self._paused_at = None

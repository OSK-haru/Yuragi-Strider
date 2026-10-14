"""Pure opt-in extraction of frozen live transport/control assumptions.

No host clock, credential, receipt or owner is accepted here. This boundary
does not authorize operations, start a world, apply a cancellation or release a
result. Existing closed-source worlds do not require these fields. Durations
are explicit world microseconds, never measured worker latency or provider
defaults; the assembler supplies host bookkeeping limits separately.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import math

from core.attack_simulation.declared_world_clock import WorldClockSettings
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.live_ingress import LiveIngressSettings
from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest


class CancellationPolicy(str, Enum):
    """Supported control capability, not a universal service invariant."""

    QUEUED_ONLY = "queued_only"


def _duration(value: float, name: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be finite and nonnegative")
    try:
        converted = float(value)
    except OverflowError:
        raise ValueError(f"{name} exceeds the supported range") from None
    if not math.isfinite(converted) or converted < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return converted


def _after(position: EventPosition, delay_us: float) -> EventPosition:
    time_us = _duration(position.time_us + delay_us, "control event time")
    if delay_us > 0 and time_us <= position.time_us:
        raise ValueError("positive control duration collapsed in world coordinates")
    return EventPosition(position.clock_domain, time_us,
                         position.order + 1 if delay_us == 0 else 0)


@dataclass(frozen=True)
class ControlExecutionWindow:
    """Trusted arrival, service application and reply availability positions."""

    arrival: EventPosition
    application: EventPosition
    release: EventPosition

    def __post_init__(self) -> None:
        positions = (self.arrival, self.application, self.release)
        if any(type(p) is not EventPosition for p in positions):
            raise TypeError("explicit immutable EventPosition values are required")
        if any(p.clock_domain != self.arrival.clock_domain for p in positions):
            raise ValueError("control window uses different clock domains")
        if not self.arrival.key < self.application.key < self.release.key:
            raise ValueError("control events must advance in causal order")


@dataclass(frozen=True)
class DeclaredControlTiming:
    """Required explicit timing for common control requests.

    processing_delay_us runs from SERVICE arrival to the application position;
    publication_delay_us then determines reply availability. Live outbound
    delay has already been added by ingress and is not charged a second time.
    Zero advances only causal order; positive unrepresentable delays reject.
    This planner cannot decide whether the job is still queued at application.
    """

    clock_domain: str
    processing_delay_us: float
    publication_delay_us: float
    basis: str
    cancellation_policy: CancellationPolicy

    def __post_init__(self) -> None:
        EventPosition(self.clock_domain, 0, 0)
        for name in ("processing_delay_us", "publication_delay_us"):
            object.__setattr__(self, name, _duration(getattr(self, name), name))
        if type(self.basis) is not str or not self.basis.strip() or len(self.basis) > 1024:
            raise ValueError("control timing requires an explicit bounded basis")
        if type(self.cancellation_policy) is not CancellationPolicy:
            raise ValueError("an explicitly supported CancellationPolicy is required")

    def plan_control(self, arrival: EventPosition) -> ControlExecutionWindow:
        """Pure causal trace; neither successful cancellation nor physical work."""
        if type(arrival) is not EventPosition:
            raise TypeError("an explicit immutable EventPosition is required")
        if arrival.clock_domain != self.clock_domain:
            raise ValueError("control timing and arrival clock domains differ")
        application = _after(arrival, self.processing_delay_us)
        return ControlExecutionWindow(
            arrival, application, _after(application, self.publication_delay_us),
        )

    def plan_cancel(self, arrival: EventPosition) -> ControlExecutionWindow:
        """Compatibility name; cancellation uses the same control timing."""
        return self.plan_control(arrival)


@dataclass(frozen=True)
class LiveServiceConfig:
    ingress: LiveIngressSettings
    control_timing: DeclaredControlTiming

    def __post_init__(self) -> None:
        if (type(self.ingress) is not LiveIngressSettings
                or type(self.control_timing) is not DeclaredControlTiming):
            raise TypeError("immutable live ingress and control settings are required")
        if self.ingress.clock_domain != self.control_timing.clock_domain:
            raise ValueError("live service settings use different clock domains")


def _fields(value: object, fields: frozenset[str], name: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    if (len(value) != len(fields) or any(type(key) is not str for key in value)
            or set(value) != fields):
        raise ValueError(f"{name} requires exactly these fields: {sorted(fields)}")
    return value


def parse_live_service_config(
    manifest: ResolvedScenarioManifest, *, max_pending: int, max_history: int,
) -> LiveServiceConfig:
    """Read only the opt-in live blocks of an already frozen bounded manifest.

    Other service profiles are not interpreted, defaulted or repaired. The
    world clock domain/rate must be explicit and validated, though durations
    are NOT multiplied by rate. No mode-specific branch exists. Host domain is
    the existing receipt ledger contract; host caps cannot come from a scenario.
    Missing live fields reject only this opt-in parser, not older assemblers.
    """
    if type(manifest) is not ResolvedScenarioManifest:
        raise TypeError("a frozen ResolvedScenarioManifest is required")
    clock = _fields(manifest.experiment_config.get("world_clock"),
                    frozenset({"clock_domain", "rate"}), "experiment.world_clock")
    settings = WorldClockSettings(**clock)
    live = _fields(manifest.qpu_service_config.get("live_ingress"),
                   frozenset({"receipt_origin", "outbound_delay_us", "basis"}),
                   "qpu_service.live_ingress")
    if (type(live["receipt_origin"]) is not str
            or live["receipt_origin"] != "authenticated_complete_envelope"):
        raise ValueError("unsupported live receipt origin")
    control = _fields(manifest.qpu_service_config.get("control_timing"),
                      frozenset({"processing_delay_us", "publication_delay_us",
                                 "basis", "cancellation_policy"}),
                      "qpu_service.control_timing")
    policy = control["cancellation_policy"]
    if type(policy) is not str:
        raise ValueError("cancellation_policy must be an explicit supported string")
    try:
        policy = CancellationPolicy(policy)
    except ValueError:
        raise ValueError("unsupported cancellation policy") from None
    return LiveServiceConfig(
        ingress=LiveIngressSettings(
            clock_domain=settings.clock_domain, host_clock_domain="host_monotonic_ns",
            outbound_delay_us=live["outbound_delay_us"], basis=live["basis"],
            max_pending=max_pending, max_history=max_history,
        ),
        control_timing=DeclaredControlTiming(
            clock_domain=settings.clock_domain,
            processing_delay_us=control["processing_delay_us"],
            publication_delay_us=control["publication_delay_us"], basis=control["basis"],
            cancellation_policy=policy,
        ),
    )

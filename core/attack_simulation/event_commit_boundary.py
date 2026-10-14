"""Pure validation of a declared world trace, not a live clock or QPU owner.

The coordinator supplies a common world domain and explicit tie order. Host
receipts are deliberately not accepted or mapped here. Run this before physical
execution, not inside a kernel whose exception handler marks a job as failed.
"""

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class EventPosition:
    clock_domain: str
    time_us: float
    order: int

    def __post_init__(self) -> None:
        if (type(self.clock_domain) is not str or not self.clock_domain.isascii()
                or not 1 <= len(self.clock_domain) <= 128):
            raise ValueError("world clock domain must be a bounded nonempty ASCII identifier")
        if type(self.time_us) not in (int, float):
            raise ValueError("world time must be finite and nonnegative")
        try:
            time_us = float(self.time_us)
        except OverflowError:
            raise ValueError("world time must be finite and nonnegative") from None
        if not math.isfinite(time_us) or time_us < 0:
            raise ValueError("world time must be finite and nonnegative")
        if type(self.order) is not int or self.order < 0:
            raise ValueError("tie order must be an explicit nonnegative integer")
        object.__setattr__(self, "time_us", time_us)

    @property
    def key(self) -> tuple[float, int]:
        return self.time_us, self.order


class EventOrderError(ValueError):
    """A declared event would rewind or repeat already committed world order."""


class UnresolvedIngressError(RuntimeError):
    """Commit must wait for an earlier ingress decision, not fail a QPU job."""


def validate_event_commit(
    *, committed: EventPosition | None, candidate: EventPosition,
    unresolved: tuple[EventPosition, ...], max_unresolved: int,
) -> None:
    """Check a prospective commit against a bounded, supervisor-owned snapshot.

    Candidate is the frontier being finalized, not the numerical worker's
    completion time. The caller supplies relevant unresolved ingress positions
    including requests still being converted/compiled. This function mutates no
    clock, queue, RNG, budget, physical state or lifecycle. An eventual runtime
    coordinator must make check + commit atomic against ingress updates.
    """
    if type(candidate) is not EventPosition or (committed is not None and type(committed) is not EventPosition):
        raise TypeError("commit positions must be immutable EventPosition values")
    if type(max_unresolved) is not int or max_unresolved < 1:
        raise ValueError("an explicit positive ingress snapshot limit is required")
    if type(unresolved) is not tuple:
        raise TypeError("unresolved ingress must be an immutable finite snapshot")
    if len(unresolved) > max_unresolved:
        raise ValueError("unresolved ingress exceeds the host snapshot limit")
    for position in ((committed,) if committed is not None else ()) + unresolved:
        if type(position) is not EventPosition:
            raise TypeError("ingress positions must be immutable EventPosition values")
        if position.clock_domain != candidate.clock_domain:
            raise ValueError("world clock domains cannot be mixed")
    if committed is not None:
        if candidate.key <= committed.key:
            raise EventOrderError("candidate does not advance the committed frontier")
        if any(position.key <= committed.key for position in unresolved):
            raise EventOrderError("unresolved ingress is already behind the committed frontier")
    if any(position.key <= candidate.key for position in unresolved):
        raise UnresolvedIngressError("resolve earlier ingress before committing this event")

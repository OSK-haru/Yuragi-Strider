"""Manifest-frozen live world clock policy.

Product profiles use ``host_clock`` at a fixed 1x rate. The optional catch-up
pause excludes active local numerical work only. ``explicit_wait`` remains an
internal/test clock-control path, not a product catalog choice.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import math

EXPLICIT_WAIT = "explicit_wait"
HOST_CLOCK = "host_clock"
MAX_WAIT_STEPS = 1_000_000


@dataclass(frozen=True)
class LiveTimePolicy:
    mode: str
    max_wait_us: float | None = None
    max_steps_per_wait: int | None = None
    pause_while_catching_up: bool = False

    def __post_init__(self):
        if self.mode == HOST_CLOCK:
            if (self.max_wait_us is not None or self.max_steps_per_wait is not None
                    or type(self.pause_while_catching_up) is not bool):
                raise ValueError("host_clock takes only the catch-up pause setting")
            return
        if self.pause_while_catching_up is not False:
            raise ValueError("catch-up pause is only valid for host_clock")
        if self.mode != EXPLICIT_WAIT:
            raise ValueError("live time mode must be explicit_wait or host_clock")
        if (type(self.max_wait_us) not in {int, float} or not math.isfinite(self.max_wait_us)
                or self.max_wait_us <= 0):
            raise ValueError("max_wait_us must be positive and finite")
        object.__setattr__(self, "max_wait_us", float(self.max_wait_us))
        if (type(self.max_steps_per_wait) is not int
                or not 1 <= self.max_steps_per_wait <= MAX_WAIT_STEPS):
            raise ValueError("max_steps_per_wait is out of range")


def parse_live_time_policy(raw: object) -> LiveTimePolicy:
    """Strict parser for ``experiment.live_time_policy``."""
    if not isinstance(raw, Mapping):
        raise ValueError("freeze an explicit live_time_policy before establishment")
    mode = raw.get("mode")
    expected = ({"mode", "pause_while_catching_up"} if mode == HOST_CLOCK
                else {"mode", "max_wait_us", "max_steps_per_wait"})
    # Historical host_clock test manifests omitted the optional false value.
    if set(raw) not in (expected, {"mode"} if mode == HOST_CLOCK else expected):
        raise ValueError("live_time_policy fields do not match its mode")
    return LiveTimePolicy(mode, raw.get("max_wait_us"), raw.get("max_steps_per_wait"),
                          raw.get("pause_while_catching_up", False))

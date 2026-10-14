"""Small, dependency-free fitting helpers for T1 population sweeps."""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Sequence


@dataclass(frozen=True)
class ExponentialFloorFit:
    amplitude: float
    floor: float
    t1_us: float
    fitted: tuple[float, ...]
    residuals: tuple[float, ...]
    root_mean_square_error: float
    maximum_absolute_error: float
    r_squared: float | None


def fit_exponential_with_floor(
    times_us: Sequence[float],
    observed_probabilities: Sequence[float],
) -> ExponentialFloorFit:
    """Fit ``A exp(-t / T1) + B`` under physical probability constraints.

    For each candidate lifetime, amplitude and floor are a two-parameter
    constrained linear least-squares problem.  Searching only the logarithm of
    the lifetime keeps this production helper independent from SciPy while
    remaining stable across lifetimes that span several orders of magnitude.
    """

    if len(times_us) != len(observed_probabilities) or len(times_us) < 3:
        raise ValueError("at least three paired T1 sweep points are required")
    times = tuple(float(value) for value in times_us)
    observed = tuple(float(value) for value in observed_probabilities)
    if any(not math.isfinite(value) or value < 0.0 for value in times):
        raise ValueError("T1 sweep times must be finite and non-negative")
    if any(
        not math.isfinite(value) or value < 0.0 or value > 1.0
        for value in observed
    ):
        raise ValueError("T1 sweep probabilities must be finite and between 0 and 1")
    if any(right <= left for left, right in zip(times, times[1:])):
        raise ValueError("T1 sweep times must be strictly increasing")

    duration = times[-1] - times[0]
    if duration <= 0.0:
        raise ValueError("T1 sweep must span a positive duration")
    positive_steps = [right - left for left, right in zip(times, times[1:])]
    minimum_tau = max(min(positive_steps) * 1e-3, duration * 1e-6, 1e-9)
    maximum_tau = max(duration * 1e3, 1.0)
    low = math.log(minimum_tau)
    high = math.log(maximum_tau)

    sample_count = 241
    logs = [low + (high - low) * index / (sample_count - 1) for index in range(sample_count)]
    sampled = [
        _fit_linear_for_tau(times, observed, math.exp(log_tau))
        for log_tau in logs
    ]
    best_index = min(range(sample_count), key=lambda index: sampled[index][0])
    bracket_low = logs[max(0, best_index - 1)]
    bracket_high = logs[min(sample_count - 1, best_index + 1)]

    golden = (math.sqrt(5.0) - 1.0) / 2.0
    left = bracket_high - golden * (bracket_high - bracket_low)
    right = bracket_low + golden * (bracket_high - bracket_low)
    left_fit = _fit_linear_for_tau(times, observed, math.exp(left))
    right_fit = _fit_linear_for_tau(times, observed, math.exp(right))
    for _ in range(96):
        if left_fit[0] <= right_fit[0]:
            bracket_high = right
            right = left
            right_fit = left_fit
            left = bracket_high - golden * (bracket_high - bracket_low)
            left_fit = _fit_linear_for_tau(times, observed, math.exp(left))
        else:
            bracket_low = left
            left = right
            left_fit = right_fit
            right = bracket_low + golden * (bracket_high - bracket_low)
            right_fit = _fit_linear_for_tau(times, observed, math.exp(right))

    sse, amplitude, floor, lifetime = min(
        (
            (*left_fit, math.exp(left)),
            (*right_fit, math.exp(right)),
            (*sampled[best_index], math.exp(logs[best_index])),
        ),
        key=lambda item: item[0],
    )
    fitted = tuple(amplitude * math.exp(-time / lifetime) + floor for time in times)
    residuals = tuple(value - prediction for value, prediction in zip(observed, fitted))
    mean = sum(observed) / len(observed)
    total_sum_squares = sum((value - mean) ** 2 for value in observed)
    r_squared = None if total_sum_squares <= 1e-30 else 1.0 - sse / total_sum_squares
    return ExponentialFloorFit(
        amplitude=amplitude,
        floor=floor,
        t1_us=lifetime,
        fitted=fitted,
        residuals=residuals,
        root_mean_square_error=math.sqrt(sse / len(observed)),
        maximum_absolute_error=max(abs(value) for value in residuals),
        r_squared=r_squared,
    )


def _fit_linear_for_tau(
    times: tuple[float, ...],
    observed: tuple[float, ...],
    lifetime: float,
) -> tuple[float, float, float]:
    x = tuple(math.exp(-time / lifetime) for time in times)
    count = len(x)
    sum_x = sum(x)
    sum_xx = sum(value * value for value in x)
    sum_y = sum(observed)
    sum_xy = sum(left * right for left, right in zip(x, observed))
    determinant = sum_xx * count - sum_x * sum_x
    candidates: list[tuple[float, float]] = []
    if determinant > 1e-30:
        amplitude = (sum_xy * count - sum_x * sum_y) / determinant
        floor = (sum_xx * sum_y - sum_x * sum_xy) / determinant
        if amplitude >= 0.0 and floor >= 0.0 and amplitude + floor <= 1.0:
            candidates.append((amplitude, floor))

    candidates.append((0.0, _clamp(sum_y / count, 0.0, 1.0)))
    candidates.append((_clamp(sum_xy / max(sum_xx, 1e-30), 0.0, 1.0), 0.0))
    one_minus_x = tuple(1.0 - value for value in x)
    denominator = sum(value * value for value in one_minus_x)
    boundary_amplitude = _clamp(
        sum(left * (1.0 - right) for left, right in zip(one_minus_x, observed))
        / max(denominator, 1e-30),
        0.0,
        1.0,
    )
    candidates.append((boundary_amplitude, 1.0 - boundary_amplitude))

    def score(parameters: tuple[float, float]) -> float:
        amplitude, floor = parameters
        return sum(
            (value - (amplitude * basis + floor)) ** 2
            for value, basis in zip(observed, x)
        )

    amplitude, floor = min(candidates, key=score)
    return score((amplitude, floor)), amplitude, floor


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))

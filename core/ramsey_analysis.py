"""Damped-cosine fitting for dedicated Ramsey sweeps."""

from __future__ import annotations

from dataclasses import dataclass
import math
from collections.abc import Callable, Sequence

import numpy as np


@dataclass(frozen=True)
class RamseyEnvelopeFit:
    model: str
    envelope_power: float
    amplitude: float
    observation_floor: float
    t2_star_us: float
    frequency_mhz: float
    phase_rad: float
    fitted: tuple[float, ...]
    residuals: tuple[float, ...]
    root_mean_square_error: float
    maximum_absolute_error: float
    r_squared: float | None
    aicc: float


def fit_ramsey_envelopes(
    times_us: Sequence[float],
    observed_probabilities: Sequence[float],
) -> tuple[RamseyEnvelopeFit, RamseyEnvelopeFit]:
    """Fit exponential and Gaussian damped cosines, ordered by AICc."""

    times, observed = _validated_series(times_us, observed_probabilities)
    fits = (
        _fit_envelope(times, observed, 1.0, "exponential_envelope"),
        _fit_envelope(times, observed, 2.0, "gaussian_envelope"),
    )
    ordered = sorted(fits, key=lambda item: item.aicc)
    return ordered[0], ordered[1]


def _validated_series(
    times_us: Sequence[float],
    probabilities: Sequence[float],
) -> tuple[np.ndarray, np.ndarray]:
    if len(times_us) != len(probabilities) or len(times_us) < 9:
        raise ValueError("at least nine paired Ramsey sweep points are required")
    times = np.asarray(times_us, dtype=float)
    observed = np.asarray(probabilities, dtype=float)
    if np.any(~np.isfinite(times)) or np.any(times < 0.0):
        raise ValueError("Ramsey sweep times must be finite and non-negative")
    if np.any(np.diff(times) <= 0.0):
        raise ValueError("Ramsey sweep times must be strictly increasing")
    if np.any(~np.isfinite(observed)) or np.any(observed < 0.0) or np.any(observed > 1.0):
        raise ValueError("Ramsey probabilities must be finite and between 0 and 1")
    return times, observed


def _fit_envelope(
    times: np.ndarray,
    observed: np.ndarray,
    power: float,
    model: str,
) -> RamseyEnvelopeFit:
    duration = float(times[-1] - times[0])
    minimum_step = float(np.min(np.diff(times)))
    minimum_t2 = max(minimum_step * 1e-2, duration * 1e-5, 1e-8)
    maximum_t2 = max(duration * 1e3, 1.0)
    maximum_frequency = 0.5 / minimum_step
    log_t2_values = np.linspace(math.log(minimum_t2), math.log(maximum_t2), 101)
    frequency_values = np.linspace(0.0, maximum_frequency, 241)

    best: tuple[float, float, float, float, float] | None = None
    best_log_t2 = 0.0
    best_frequency = 0.0
    for log_t2 in log_t2_values:
        t2_star = math.exp(float(log_t2))
        for frequency in frequency_values:
            candidate = _linear_fit(times, observed, t2_star, float(frequency), power)
            if best is None or candidate[0] < best[0]:
                best = candidate
                best_log_t2 = float(log_t2)
                best_frequency = float(frequency)
    if best is None:
        raise RuntimeError(f"Ramsey fit failed for {model}")

    log_step = float(log_t2_values[1] - log_t2_values[0])
    frequency_step = float(frequency_values[1] - frequency_values[0])
    log_low = max(math.log(minimum_t2), best_log_t2 - log_step)
    log_high = min(math.log(maximum_t2), best_log_t2 + log_step)
    frequency_low = max(0.0, best_frequency - frequency_step)
    frequency_high = min(maximum_frequency, best_frequency + frequency_step)
    for _ in range(8):
        best_log_t2 = _golden_minimum(
            log_low,
            log_high,
            lambda value: _linear_fit(
                times, observed, math.exp(value), best_frequency, power
            )[0],
        )
        best_frequency = _golden_minimum(
            frequency_low,
            frequency_high,
            lambda value: _linear_fit(
                times, observed, math.exp(best_log_t2), value, power
            )[0],
        )
        log_radius = max((log_high - log_low) * 0.22, 1e-10)
        frequency_radius = max((frequency_high - frequency_low) * 0.22, 1e-12)
        log_low = max(math.log(minimum_t2), best_log_t2 - log_radius)
        log_high = min(math.log(maximum_t2), best_log_t2 + log_radius)
        frequency_low = max(0.0, best_frequency - frequency_radius)
        frequency_high = min(maximum_frequency, best_frequency + frequency_radius)

    t2_star = math.exp(best_log_t2)
    sse, floor, cosine_coefficient, sine_coefficient, _ = _linear_fit(
        times, observed, t2_star, best_frequency, power
    )
    amplitude = math.hypot(cosine_coefficient, sine_coefficient)
    phase = math.atan2(-sine_coefficient, cosine_coefficient)
    envelope = np.exp(-np.power(times / t2_star, power))
    fitted_array = floor + envelope * (
        cosine_coefficient * np.cos(2.0 * math.pi * best_frequency * times)
        + sine_coefficient * np.sin(2.0 * math.pi * best_frequency * times)
    )
    residual_array = observed - fitted_array
    total_sum_squares = float(np.sum((observed - np.mean(observed)) ** 2))
    r_squared = None if total_sum_squares <= 1e-30 else 1.0 - sse / total_sum_squares
    count = len(times)
    parameter_count = 5
    safe_sse = max(sse, 1e-30)
    aic = count * math.log(safe_sse / count) + 2.0 * parameter_count
    aicc = aic + (
        2.0 * parameter_count * (parameter_count + 1.0)
        / (count - parameter_count - 1.0)
    )
    return RamseyEnvelopeFit(
        model=model,
        envelope_power=power,
        amplitude=amplitude,
        observation_floor=floor,
        t2_star_us=t2_star,
        frequency_mhz=best_frequency,
        phase_rad=phase,
        fitted=tuple(float(value) for value in fitted_array),
        residuals=tuple(float(value) for value in residual_array),
        root_mean_square_error=math.sqrt(sse / count),
        maximum_absolute_error=float(np.max(np.abs(residual_array))),
        r_squared=r_squared,
        aicc=aicc,
    )


def _linear_fit(
    times: np.ndarray,
    observed: np.ndarray,
    t2_star: float,
    frequency: float,
    power: float,
) -> tuple[float, float, float, float, float]:
    envelope = np.exp(-np.power(times / t2_star, power))
    angle = 2.0 * math.pi * frequency * times
    design = np.column_stack((np.ones_like(times), envelope * np.cos(angle), envelope * np.sin(angle)))
    parameters, _, _, _ = np.linalg.lstsq(design, observed, rcond=None)
    floor, cosine_coefficient, sine_coefficient = (float(value) for value in parameters)
    amplitude = math.hypot(cosine_coefficient, sine_coefficient)
    predictions = design @ parameters
    sse = float(np.sum((observed - predictions) ** 2))
    physical = (
        0.0 <= floor <= 1.0
        and amplitude <= min(floor, 1.0 - floor) + 1e-8
        and np.all(predictions >= -1e-8)
        and np.all(predictions <= 1.0 + 1e-8)
    )
    if not physical:
        sse += 1e6
    return sse, floor, cosine_coefficient, sine_coefficient, amplitude


def _golden_minimum(
    low: float,
    high: float,
    objective: Callable[[float], float],
) -> float:
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    left = high - ratio * (high - low)
    right = low + ratio * (high - low)
    left_value = objective(left)
    right_value = objective(right)
    for _ in range(48):
        if left_value <= right_value:
            high = right
            right = left
            right_value = left_value
            left = high - ratio * (high - low)
            left_value = objective(left)
        else:
            low = left
            left = right
            left_value = right_value
            right = low + ratio * (high - low)
            right_value = objective(right)
    return (low + high) / 2.0

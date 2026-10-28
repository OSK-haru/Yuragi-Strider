"""Compare Python and Rust security-analysis kernels and adapter paths.

Run with a built ``yuragi_strider_rust`` extension. Output reports medians in
microseconds for binary statistics and milliseconds for Reset training input
sets. The binary kernel case excludes request construction and report creation;
the adapter case includes report construction and dictionary serialization.
The Reset case includes public-input validation, Python-to-Rust batch creation,
one PyO3 call (Rust backend), and report construction.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.attack_simulation.reset_leakage_analysis import analyze_public_training_results
from core.security_analysis import BinaryContrastRequest, ObservationSource, analyze_binary_contrast
from core.security_analysis.binary_contrast import (
    BINARY_CONTRAST_SCHEMA_VERSION,
    _python_statistics,
)


def _median(operation, warmups: int, repeats: int, scale: float) -> float:
    for _ in range(warmups):
        operation()
    samples = []
    for _ in range(repeats):
        started = time.perf_counter_ns()
        operation()
        samples.append((time.perf_counter_ns() - started) / scale)
    return statistics.median(samples)


def _max_numeric_difference(left: dict[str, object], right: dict[str, object]) -> float:
    differences = []
    for key, value in left.items():
        other = right[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            differences.append(abs(float(value) - float(other)))
    return max(differences, default=0.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", nargs="+", type=int, default=[1000, 10000])
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    if args.warmups < 0 or args.repeats < 1 or any(size < 2 or size % 2 for size in args.observations):
        parser.error("warmups must be non-negative; repeats positive; observation counts even and >= 2")

    module = importlib.import_module("yuragi_strider_rust")
    if not callable(getattr(module, "binary_contrast_statistics_v1", None)):
        parser.error("the Rust extension lacks binary_contrast_statistics_v1")

    request = BinaryContrastRequest(
        manifest_id="benchmark-manifest",
        mechanism_id="reset_leakage",
        mechanism_version="1",
        model_configuration_id="benchmark-model",
        dataset_id="benchmark-training",
        measurement_id="binary-readout-v1",
        class_0_id="label-0",
        class_1_id="label-1",
        outcome_id="measured-one",
        source=ObservationSource.ACTOR_VISIBLE,
        trials_0=100,
        outcome_one_0=20,
        trials_1=100,
        outcome_one_1=80,
    )
    python_kernel = lambda: _python_statistics(request)
    rust_kernel = lambda: module.binary_contrast_statistics_v1(
        BINARY_CONTRAST_SCHEMA_VERSION,
        request.source.value,
        request.trials_0,
        request.outcome_one_0,
        request.trials_1,
        request.outcome_one_1,
    )
    python_adapter = lambda: analyze_binary_contrast(request, backend="python").to_dict()
    rust_adapter = lambda: analyze_binary_contrast(request, backend="rust").to_dict()
    python_adapter_result = python_adapter()
    rust_adapter_result = rust_adapter()
    if python_adapter_result != rust_adapter_result:
        for key, value in python_adapter_result.items():
            other = rust_adapter_result[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isclose(float(value), float(other), rel_tol=0.0, abs_tol=2e-15):
                    raise RuntimeError(f"binary output differs at {key}: {value!r} != {other!r}")
            elif value != other:
                raise RuntimeError(f"binary output differs at {key}: {value!r} != {other!r}")

    binary = {
        "python_kernel_median_us": _median(python_kernel, args.warmups, args.repeats, 1_000),
        "rust_pyo3_kernel_median_us": _median(rust_kernel, args.warmups, args.repeats, 1_000),
        "python_adapter_median_us": _median(python_adapter, args.warmups, args.repeats, 1_000),
        "rust_pyo3_adapter_median_us": _median(rust_adapter, args.warmups, args.repeats, 1_000),
        "pyo3_calls_per_rust_adapter": 1,
        "numeric_output_max_abs_difference": _max_numeric_difference(
            python_adapter_result, rust_adapter_result
        ),
    }

    metadata = {
        "manifest_id": "benchmark-manifest",
        "mechanism_version": "1",
        "model_configuration_id": "benchmark-model",
        "dataset_id": "benchmark-training",
        "measurement_id": "binary-readout-v1",
    }
    reset_results = []
    for size in args.observations:
        observations = tuple(
            {
                "phase": "training",
                "known_label": label,
                "shots": 100,
                "counts": {"0": 100 - ones, "1": ones},
            }
            for index in range(size // 2)
            for label, ones in ((0, 18 + index % 5), (1, 78 - index % 5))
        )
        python_run = lambda: analyze_public_training_results(
            observations, backend="python", **metadata
        ).to_dict()
        rust_run = lambda: analyze_public_training_results(
            observations, backend="rust", **metadata
        ).to_dict()
        python_result = python_run()
        rust_result = rust_run()
        if python_result != rust_result:
            if _max_numeric_difference(python_result, rust_result) > 2e-15:
                raise RuntimeError(f"Reset analysis outputs differ for {size} observations")
        reset_results.append({
            "observations": size,
            "python_adapter_median_ms": _median(python_run, args.warmups, args.repeats, 1_000_000),
            "rust_pyo3_adapter_median_ms": _median(rust_run, args.warmups, args.repeats, 1_000_000),
            "pyo3_calls_per_rust_analysis": 1,
            "numeric_output_max_abs_difference": _max_numeric_difference(
                python_result, rust_result
            ),
        })

    print(json.dumps({
        "status": "ok",
        "warmups": args.warmups,
        "repeats": args.repeats,
        "binary_contrast": binary,
        "reset_training_analysis": reset_results,
        "notes": [
            "Python and Rust runs use identical validated input values.",
            "Rust timings include PyO3 conversion and return conversion.",
            "No API, queueing, or network time is included.",
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

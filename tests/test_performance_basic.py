import os
import tempfile
import time
import unittest
from pathlib import Path

from core.comparison import ComparisonConfig, run_comparison
from core.expert_data import build_expert_inspector_data
from core.io.config_io import load_config, save_config
from core.io.report_export import markdown_report_text
from core.io.result_export import result_to_csv_text, result_to_json_text
from core.results import EnvironmentConfig
from core.simulator import run_simulation
from tests.phase8_helpers import bell_config, one_qubit_gate_config


PERFORMANCE_NOTES_PATH = (
    Path(__file__).resolve().parents[1]
    / "docs"
    / "validation"
    / "performance_notes.md"
)
# Refreshing the tracked notes file is opt-in so that a normal test run leaves
# the working tree clean. Set this to "1" to rewrite docs/validation/performance_notes.md.
WRITE_NOTES_ENV = "YURAGI_WRITE_PERFORMANCE_NOTES"


class PerformanceBasicTest(unittest.TestCase):
    def test_basic_operations_complete_and_record_timings(self) -> None:
        timings: list[tuple[str, float]] = []

        h_result = _measure("1-qubit H", timings, lambda: run_simulation(one_qubit_gate_config("H")))
        _measure("1-qubit X", timings, lambda: run_simulation(one_qubit_gate_config("X")))
        _measure("2-qubit Bell", timings, lambda: run_simulation(bell_config()))
        _measure(
            "1-qubit comparison",
            timings,
            lambda: run_comparison(_comparison_config(one_qubit_gate_config("H"))),
        )
        _measure(
            "2-qubit comparison",
            timings,
            lambda: run_comparison(_comparison_config(bell_config())),
        )
        _measure("expert data generation", timings, lambda: build_expert_inspector_data(h_result))
        _measure("result JSON export", timings, lambda: result_to_json_text(h_result))
        _measure("result CSV export", timings, lambda: result_to_csv_text(h_result))
        _measure("Markdown export", timings, lambda: markdown_report_text(h_result))
        _measure("save/load config", timings, lambda: _save_load_roundtrip(one_qubit_gate_config("H")))

        if os.environ.get(WRITE_NOTES_ENV) == "1":
            _write_performance_notes(timings)

        self.assertEqual(len(timings), 10)
        self.assertTrue(all(elapsed >= 0.0 for _, elapsed in timings))


def _comparison_config(config):
    return ComparisonConfig(
        circuit=config.circuit,
        environment_a=EnvironmentConfig(noise_level=0.0),
        environment_b=EnvironmentConfig(noise_level=0.8),
        duration_us=config.duration_us,
        time_steps=config.time_steps,
        fidelity_threshold=config.fidelity_threshold,
        label_a="Low",
        label_b="High",
    )


def _measure(name: str, timings: list[tuple[str, float]], callback):
    start = time.perf_counter()
    value = callback()
    timings.append((name, time.perf_counter() - start))
    return value


def _save_load_roundtrip(config):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.qscope.json"
        save_config(config, path)
        return load_config(path)


def _write_performance_notes(timings: list[tuple[str, float]]) -> None:
    lines = [
        "# Performance Notes",
        "",
        "These timings are produced by the lightweight Phase 8 regression test.",
        "They are informational and intentionally not used as strict pass/fail thresholds.",
        "",
        "Scope: noise-free circuits over a 0.001 us window on the Python dense",
        "backend only. They are not comparable to the Rust vs Python benchmark in",
        "`formalweb/website/docs/performance/rust-acceleration.md`, which uses much",
        "longer windows with noise; run `scripts/benchmark_rust_dense.py` for that.",
        "",
        "| Operation | Elapsed seconds |",
        "| --- | ---: |",
    ]
    lines.extend(f"| {name} | {elapsed:.6f} |" for name, elapsed in timings)
    PERFORMANCE_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        PERFORMANCE_NOTES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except PermissionError:
        if not PERFORMANCE_NOTES_PATH.exists():
            raise


if __name__ == "__main__":
    unittest.main()

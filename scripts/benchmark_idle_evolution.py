"""Bounded, isolated-process idle-kernel timing and process peak RSS probe.

Physical parameters below are synthetic benchmark inputs, not adopted device
or scenario defaults. No clocks, transactions or serialization are benchmarked.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _peak_rss_bytes() -> int | None:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                )
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.PeakWorkingSetSize)
    if sys.platform in ("linux", "darwin"):
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(value if sys.platform == "darwin" else value * 1024)
    return None


def _worker(width: int, elapsed: float, iterations: int, repetitions: int) -> dict[str, object]:
    import numpy as np
    from core.experimental_isolation_physics.idle_evolution import IndependentIdleModel, IdleQubitSpec

    generator = np.random.default_rng(54)
    ket = generator.normal(size=1 << width) + 1j * generator.normal(size=1 << width)
    ket /= np.linalg.norm(ket)
    state = np.outer(ket, ket.conj())  # PSD by construction, no eigensolve in timing.
    specs = tuple(IdleQubitSpec(40.0 + q, 65.0 + q, 0.02) for q in range(width))
    model = IndependentIdleModel(specs, max_local_qubits=width)
    baseline_peak = _peak_rss_bytes()
    for _ in range(3):
        model.evolve(state, elapsed_us=elapsed)
    samples = []
    for _ in range(repetitions):
        started = perf_counter()
        for _ in range(iterations):
            model.evolve(state, elapsed_us=elapsed)
        samples.append((perf_counter() - started) * 1000 / iterations)
    return {
        "local_qubits": width, "elapsed_us": elapsed,
        "iterations": iterations, "repetitions": repetitions,
        "median_ms_per_call": statistics.median(samples),
        "input_matrix_bytes": state.nbytes,
        "baseline_process_peak_rss_bytes": baseline_peak,
        "process_peak_rss_bytes": _peak_rss_bytes(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--width", type=int, choices=range(1, 9), default=8)
    parser.add_argument("--elapsed-us", type=float, default=0.1)
    parser.add_argument("--iterations", type=int, choices=range(1, 101), default=20)
    parser.add_argument("--repetitions", type=int, choices=range(1, 11), default=5)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(_worker(args.width, args.elapsed_us, args.iterations, args.repetitions)))
        return
    results = []
    for width in (1, 4, 8):
        for elapsed in (0.1, 1e8):
            completed = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
                 "--width", str(width), "--elapsed-us", str(elapsed),
                 "--iterations", str(args.iterations), "--repetitions", str(args.repetitions)],
                capture_output=True, text=True, check=True, timeout=30,
            )
            results.append(json.loads(completed.stdout))
    print(json.dumps({"scope": "kernel_only; RSS includes Python/NumPy and input state",
                      "results": results}, indent=2))


if __name__ == "__main__":
    main()

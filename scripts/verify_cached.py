"""Run deterministic local verification checks with fingerprint caching."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / ".cache" / "yuragi-strider-verification.json"
PROJECT_PYTHON = (
    ROOT / ".venv" / "Scripts" / "python.exe"
    if os.name == "nt"
    else ROOT / ".venv" / "bin" / "python"
)
PYTHON_EXECUTABLE = str(PROJECT_PYTHON if PROJECT_PYTHON.exists() else Path(sys.executable))


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    cwd: Path
    source_roots: tuple[Path, ...]
    tools: tuple[tuple[str, ...], ...] = ()


PYTHON_TESTS = (
    "tests.test_gate_aware_cptp",
    "tests.test_gate_aware_hamiltonian_lindblad",
    "tests.test_api_simulate_input_modes",
    "tests.test_api_simulate_circuit_config",
    "tests.test_api_simulate_qubit_counts",
    "tests.test_snapshot_request_policy",
    "tests.test_ui_response_adapter",
    "tests.test_cptp_rust_parity",
    "tests.test_validation_qutip_comparison",
)


# Only the modules this check runs, plus the non-test helpers they may import
# (tests/__init__.py, *_helpers.py), so editing an unrelated test does not
# invalidate the cache.
PYTHON_TEST_INPUTS = (
    *(ROOT / "tests" / f"{module.rsplit('.', 1)[-1]}.py" for module in PYTHON_TESTS),
    *sorted(
        path
        for path in (ROOT / "tests").glob("*.py")
        if not path.name.startswith("test_")
    ),
)

# Inputs the Python tests read at run time besides the code under test. The Rust
# crate is listed by source and manifest only, because its target/ directory is
# build output of several hundred MB.
RUNTIME_DATA_AND_RUST_INPUTS = (
    ROOT / "data",
    ROOT / "validation_results",
    ROOT / "rust_kernels" / "yuragi_strider_rust" / "src",
    ROOT / "rust_kernels" / "yuragi_strider_rust" / "Cargo.toml",
    ROOT / "rust_kernels" / "yuragi_strider_rust" / "Cargo.lock",
    ROOT / "rust_kernels" / "yuragi_strider_rust" / "pyproject.toml",
)


CHECKS = {
    "frontend-build": Check(
        name="frontend-build",
        command=("npm.cmd", "run", "build"),
        cwd=ROOT / "frontend",
        source_roots=(ROOT / "scripts" / "verify_cached.py", ROOT / "frontend" / "src", ROOT / "frontend" / "package.json", ROOT / "frontend" / "package-lock.json", ROOT / "frontend" / "tsconfig.json", ROOT / "frontend" / "tsconfig.app.json", ROOT / "frontend" / "vite.config.ts", ROOT / "frontend" / "tsconfig.node.json", ROOT / "frontend" / "index.html", ROOT / "frontend" / "public"),
        tools=(("node", "--version"), ("npm.cmd", "--version")),
    ),
    "gate-aware-tests": Check(
        name="gate-aware-tests",
        command=(PYTHON_EXECUTABLE, "-m", "unittest", *PYTHON_TESTS),
        cwd=ROOT,
        source_roots=(ROOT / "scripts" / "verify_cached.py", ROOT / "core", ROOT / "api", *PYTHON_TEST_INPUTS, ROOT / "validation_pulse", ROOT / "validation_cptp", *RUNTIME_DATA_AND_RUST_INPUTS),
        tools=((PYTHON_EXECUTABLE, "--version"),),
    ),
}


def _iter_source_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and "node_modules" not in path.parts
        and "dist" not in path.parts
    )


def _tool_version(command: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            list(command),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable:{type(exc).__name__}"
    output = (completed.stdout or completed.stderr).strip()
    return output[-200:]


def fingerprint(check: Check) -> str:
    digest = hashlib.sha256()
    metadata = {
        "name": check.name,
        "command": check.command,
        "cwd": str(check.cwd.relative_to(ROOT)),
        "python": sys.version,
        "platform": platform.platform(),
        "tools": {
            " ".join(tool): _tool_version(tool)
            for tool in check.tools
        },
    }
    digest.update(json.dumps(metadata, sort_keys=True).encode("utf-8"))
    for source_root in check.source_roots:
        for path in _iter_source_files(source_root):
            relative = path.relative_to(ROOT).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_cache() -> dict[str, object]:
    if not CACHE_PATH.exists():
        return {"schema_version": 1, "checks": {}}
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "checks": {}}
    if not isinstance(payload, dict) or not isinstance(payload.get("checks"), dict):
        return {"schema_version": 1, "checks": {}}
    return payload


def _save_cache(payload: dict[str, object]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=CACHE_PATH.parent,
        prefix="verification-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.replace(temporary_path, CACHE_PATH)


def run_check(check: Check, *, force: bool = False) -> int:
    current_fingerprint = fingerprint(check)
    cache = _load_cache()
    entries = cache["checks"]
    assert isinstance(entries, dict)
    previous = entries.get(check.name)
    if (
        not force
        and isinstance(previous, dict)
        and previous.get("status") == "passed"
        and previous.get("fingerprint") == current_fingerprint
    ):
        print(f"SKIP {check.name}: fingerprint already passed")
        return 0

    print(f"RUN {check.name}: fingerprint changed or --force was supplied")
    completed = subprocess.run(list(check.command), cwd=check.cwd, check=False)
    entries[check.name] = {
        "command": list(check.command),
        "cwd": str(check.cwd.relative_to(ROOT)),
        "fingerprint": current_fingerprint,
        "status": "passed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    _save_cache(cache)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checks", nargs="*", choices=[*CHECKS, "all"])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        print("\n".join(CHECKS))
        return 0
    selected = list(CHECKS) if not args.checks or "all" in args.checks else args.checks
    for name in selected:
        result = run_check(CHECKS[name], force=args.force)
        if result != 0:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

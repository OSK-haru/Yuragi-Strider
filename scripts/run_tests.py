"""Run focused test profiles without repeating unrelated long audits."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PROFILE_MODULES: dict[str, tuple[str, ...]] = {
    "fast": (
        "tests.test_gate_execution",
        "tests.test_gate_measurement",
        "tests.test_config_models",
        "tests.test_circuit_state",
        "tests.test_validation",
        "tests.test_ui_response_adapter",
        "tests.test_api_simulate_circuit_config",
    ),
    "gate-aware": (
        "tests.test_gate_execution",
        "tests.test_gate_measurement",
        "tests.test_gate_aware_general_unitary",
        "tests.test_gate_aware_hamiltonian_lindblad",
        "tests.test_gate_aware_cptp",
        "tests.test_gate_compiler_rotations",
        "tests.test_gate_compiler_cp",
        "tests.test_gate_compiler_ccx",
        "tests.test_gate_compiler_qft",
        "tests.test_gate_oracle_grover",
    ),
    "pulse": (
        "tests.test_pulse_api_baseline_a",
        "tests.test_pulse_api_qutrit",
        "tests.test_pulse_api_transmon_network",
    ),
}
# Profiles selected by file-name pattern, so a new test file in the area joins the
# profile without editing this list.
PROFILE_GLOBS: dict[str, tuple[str, ...]] = {
    "attack": ("test_attack*.py", "test_*challenge*.py", "test_crosstalk*.py", "test_concurrent*.py"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile",
        choices=[*PROFILE_MODULES, *PROFILE_GLOBS, "full-audit"],
        help="Focused profile, or the complete unittest discovery run.",
    )
    args = parser.parse_args(argv)

    loader = unittest.TestLoader()
    if args.profile == "full-audit":
        suite = loader.discover(str(ROOT / "tests"))
    elif args.profile in PROFILE_GLOBS:
        files = sorted({path.stem for pattern in PROFILE_GLOBS[args.profile]
                        for path in (ROOT / "tests").glob(pattern)})
        suite = unittest.TestSuite(loader.loadTestsFromName(f"tests.{name}") for name in files)
    else:
        suite = unittest.TestSuite(
            loader.loadTestsFromName(module)
            for module in PROFILE_MODULES[args.profile]
        )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Machine-readable baseline for the physical models before tenant simulation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import subprocess
from typing import Any

from api.main import SimulateRequest, build_config_from_simulate_request
from api.pulse_models import CoupledTransmonNetworkPulseSimulateRequest
from api.pulse_transmon_network_service import _build_rates
from core.capabilities import (
    DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL,
    GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
)
from core.physical_environment import compute_environment_rates_per_qubit
from core.simulator import run_simulation


ROOT = Path(__file__).resolve().parents[1]
PHYSICAL_MODEL_BASELINE_FREEZE_ID = "yuragi_strider_physical_baseline_v1"

CRITICAL_SOURCE_FILES = (
    "core/capabilities.py",
    "core/classical_branching.py",
    "core/gates.py",
    "core/physical_environment.py",
    "core/results.py",
    "core/simulator.py",
    "core/ui_response.py",
    "core/validation.py",
    "api/main.py",
    "api/pulse_models.py",
    "api/pulse_qutrit_service.py",
    "api/pulse_transmon_network_service.py",
    "tests/test_per_qubit_calibration.py",
    "tests/test_physical_model_baseline.py",
    "tests/test_pulse_api_transmon_network.py",
    "validation_physics/model_baseline.py",
    "scripts/freeze_physical_model_baseline.py",
)

REQUIRED_EVIDENCE_FILES = (
    "docs_for_develop/physics/model_identity.md",
    "docs/validation/physical-model-baseline-v1.md",
    "validation_results/cptp_model_freeze.json",
    "validation_results/pulse_transmon_network_qutip_audit.json",
)


def build_freeze_report(root: Path = ROOT) -> dict[str, Any]:
    """Build the physical-baseline record without writing it."""

    sources = [_audit_file(root, item) for item in CRITICAL_SOURCE_FILES]
    evidence = [_audit_file(root, item) for item in REQUIRED_EVIDENCE_FILES]
    gate_aware = _gate_aware_smoke()
    network = _network_smoke()
    overall_pass = (
        all(item["exists"] for item in sources)
        and all(item["exists"] for item in evidence)
        and gate_aware["passed"]
        and network["passed"]
    )
    return {
        "validation": "PHYSICAL-MODEL-BASELINE-V1",
        "freeze_schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "decision": "PASS WITH RESTRICTIONS" if overall_pass else "FAIL",
        "overall_pass": overall_pass,
        "frozen_contract": {
            "freeze_id": PHYSICAL_MODEL_BASELINE_FREEZE_ID,
            "gate_aware_model_id": GATE_AWARE_HAMILTONIAN_LINDBLAD_MODEL,
            "pulse_network_model_id": (
                DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL
            ),
            "physical_input_extension": (
                "per_qubit_t1_tphi_and_independent_affine_readout_v1"
            ),
            "per_qubit_rules": {
                "input_mode": "physical_only",
                "coherence_entry_count": "exactly one per logical qubit or transmon",
                "readout_entry_count": "exactly one per logical qubit or transmon",
                "shared_values_when_omitted": True,
                "readout_assignment_span": "p10 + p01 < 1",
            },
        },
        "guarantee_boundary": {
            "guaranteed": [
                "Each local T1/Tphi pair generates its own local Lindblad rates.",
                "Each local rate set creates collapse operators on its own subsystem.",
                "Independent p10/p01 pairs affect observed probabilities only.",
                "Omitting per_qubit preserves the legacy shared-parameter path.",
            ],
            "not_claimed": [
                "calibrated real-hardware prediction",
                "tenant isolation or a shared-QPU scheduler",
                "state retention across jobs or imperfect reset",
                "unintended-drive crosstalk or residual ZZ coupling",
                "correlated readout or IQ-level measurement simulation",
                "job-to-job drift or non-Markovian dynamics",
            ],
        },
        "smoke_cases": {
            "gate_aware_per_qubit": gate_aware,
            "pulse_network_per_transmon": network,
        },
        "source_revision": {
            "git_head": _git_commit(root),
            "critical_source_tree_sha256": _tree_sha256(sources),
            "files": sources,
        },
        "evidence_files": evidence,
        "environment": {
            "python": platform.python_version(),
            "packages": {
                package: _package_version(package)
                for package in ("numpy", "fastapi", "pydantic")
            },
        },
    }


def write_freeze_report(output_path: Path, *, root: Path = ROOT) -> dict[str, Any]:
    """Write the canonical physical-baseline artifact."""

    report = build_freeze_report(root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def _gate_aware_smoke() -> dict[str, Any]:
    request = SimulateRequest.model_validate({
        "simulation_backend": "python_dense",
        "input_mode": "physical",
        "circuit_config": {
            "logical_qubits": 2,
            "initial_states": [0, 0],
            "columns": [],
        },
        "parameters": {
            "fidelity_threshold": 0.9,
            "device_quality": 1.0,
            "temperature_mk": 0.0,
            "flux_noise_phi0": 0.0,
            "qubit_frequency_ghz": 5.0,
            "t1_max_us": 100.0,
            "tphi_max_us": 100.0,
            "per_qubit": [
                {"t1_max_us": 20.0, "tphi_max_us": 40.0},
                {"t1_max_us": 200.0, "tphi_max_us": 400.0},
            ],
            "duration_us": 0.1,
            "time_steps": 5,
        },
        "readout_error": {
            "p10": 0.005,
            "p01": 0.013,
            "per_qubit": [
                {"p10": 0.01, "p01": 0.02},
                {"p10": 0.03, "p01": 0.04},
            ],
        },
    })
    config = build_config_from_simulate_request(request)
    local_rates = compute_environment_rates_per_qubit(config.environment, 2)
    result = run_simulation(config)
    derived = result.derived_parameters["per_qubit"]
    passed = (
        len(derived) == 2
        and local_rates[0].gamma_down_per_us > local_rates[1].gamma_down_per_us
        and derived[0]["t1_effective_us"] < derived[1]["t1_effective_us"]
        and config.readout_error.assignment_errors(2)
        == [(0.01, 0.02), (0.03, 0.04)]
    )
    return {
        "passed": passed,
        "local_t1_effective_us": [item["t1_effective_us"] for item in derived],
        "readout_assignment_errors": config.readout_error.assignment_errors(2),
    }


def _network_smoke() -> dict[str, Any]:
    request = CoupledTransmonNetworkPulseSimulateRequest.model_validate({
        "model_id": DRIVEN_COUPLED_TRANSMON_NETWORK_RWA_EXPERIMENTAL_MODEL,
        "local_levels": 2,
        "transmon_count": 2,
        "initial_state": "00",
        "frequencies_ghz": [5.0, 5.1],
        "anharmonicities_mhz": [-100.0, -100.0],
        "detunings_rad_per_us": [0.0, 0.0],
        "couplings": [{
            "left": 0,
            "right": 1,
            "exchange_coupling_rad_per_us": 1.0,
        }],
        "drives": [{
            "target": 0,
            "start_time_us": 0.0,
            "pulse": {
                "shape": "square",
                "amplitude_mode": "target_rotation_angle",
                "target_rotation_angle_rad": 0.01,
                "pulse_duration_us": 0.001,
                "phase_rad": 0.0,
                "detuning_rad_per_us": 0.0,
                "drag_beta_us": 0.0,
            },
        }],
        "total_simulation_time_us": 0.02,
        "environment": {
            "input_mode": "physical",
            "device_quality": 1.0,
            "temperature_mk": 0.0,
            "flux_noise_phi0": 0.0,
            "qubit_frequency_ghz": 5.0,
            "t1_max_us": 100.0,
            "tphi_max_us": 100.0,
            "per_qubit": [
                {"t1_max_us": 25.0, "tphi_max_us": 50.0},
                {"t1_max_us": 250.0, "tphi_max_us": 500.0},
            ],
        },
        "readout_error": {
            "p10": 0.005,
            "p01": 0.013,
            "per_qubit": [
                {"p10": 0.01, "p01": 0.02},
                {"p10": 0.03, "p01": 0.04},
            ],
        },
    })
    rates = _build_rates(request)
    assignments = request.readout_error.assignment_errors(2)
    passed = (
        len(rates) == 2
        and rates[0].gamma_10_down_per_us > rates[1].gamma_10_down_per_us
        and assignments == [(0.01, 0.02), (0.03, 0.04)]
    )
    return {
        "passed": passed,
        "gamma_10_down_per_us": [rate.gamma_10_down_per_us for rate in rates],
        "readout_assignment_errors": assignments,
    }


def _audit_file(root: Path, relative_path: str) -> dict[str, Any]:
    path = root / relative_path
    return {
        "file": relative_path,
        "exists": path.is_file(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
    }


def _tree_sha256(files: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        [{"file": item["file"], "sha256": item["sha256"]} for item in files],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "not-installed"


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"

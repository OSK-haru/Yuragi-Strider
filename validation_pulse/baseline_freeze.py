"""Consolidated audit helpers for the Pulse Baseline A freeze."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import math
from pathlib import Path
import platform
import subprocess
from typing import Any

from api.main import app
from api.pulse_models import PulseSimulateRequest
from api.pulse_service import (
    PULSE_API_CONTRACT_VERSION,
    PULSE_API_MAX_INTERNAL_STEPS,
    PULSE_API_STEP_POLICY_ID,
    run_pulse_request,
)
from core.capabilities import DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL
from core.pulse_step_policy import (
    PULSE_BASELINE_A_EPSILON_D,
    PULSE_BASELINE_A_EPSILON_H,
    PULSE_BASELINE_A_SAMPLES_PER_SIGMA,
)


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_VALIDATION_ARTIFACTS = (
    "validation1_zero_dissipation.json",
    "validation2_zero_temperature.json",
    "validation3_excited_state_decay.json",
    "validation4_pure_dephasing.json",
    "validation5_finite_temperature_equilibrium.json",
    "validation6_time_step_convergence.json",
    "validation7_qutip_comparison.json",
    "pulse_ba2_envelopes_analytic.json",
    "pulse_ba3_phase_detuning_gate_equivalence.json",
    "pulse_ba4_open_system_idle.json",
    "pulse_convergence_2level.json",
    "pulse_qutip_2level.json",
)


def build_freeze_report(root: Path = ROOT) -> dict[str, Any]:
    """Build the machine-readable BA-6 freeze audit without writing files."""

    validation_results = root / "validation_results"
    artifact_audit = [
        _audit_validation_artifact(validation_results / filename)
        for filename in REQUIRED_VALIDATION_ARTIFACTS
    ]
    openapi = app.openapi()
    pulse_contract = _pulse_openapi_contract(openapi)
    direct_response = run_pulse_request(
        PulseSimulateRequest.model_validate(_direct_rates_payload())
    )
    physical_response = run_pulse_request(
        PulseSimulateRequest.model_validate(_physical_payload())
    )
    required_paths = {
        "/api/simulate": "/api/simulate" in openapi["paths"],
        "/api/pulse/simulate": "/api/pulse/simulate" in openapi["paths"],
    }
    api_checks = {
        "required_paths": required_paths,
        "pulse_contract_sha256": _canonical_sha256(pulse_contract),
        "pulse_contract_version": direct_response["contract_version"],
        "model_id": direct_response["model"]["model_id"],
        "direct_rates_smoke": _response_summary(direct_response),
        "physical_smoke": _response_summary(physical_response),
    }
    all_artifacts_pass = all(
        item["exists"] and item["pass"] is True
        for item in artifact_audit
    )
    api_pass = (
        all(required_paths.values())
        and api_checks["pulse_contract_version"]
        == PULSE_API_CONTRACT_VERSION
        and api_checks["model_id"]
        == DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL
        and api_checks["direct_rates_smoke"]["input_mode"]
        == "direct_rates"
        and api_checks["physical_smoke"]["input_mode"] == "physical"
    )
    return {
        "validation": "PULSE-BASELINE-A-FREEZE",
        "freeze_schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_git_commit": _git_commit(root),
        "environment": {
            "python_version": platform.python_version(),
            "packages": {
                package: _package_version(package)
                for package in (
                    "numpy",
                    "scipy",
                    "qutip",
                    "fastapi",
                    "pydantic",
                )
            },
        },
        "frozen_contract": {
            "model_id": DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
            "contract_version": PULSE_API_CONTRACT_VERSION,
            "frame": "rotating",
            "approximation": "RWA",
            "time_unit": "us",
            "hamiltonian_unit": "rad/us",
            "rate_unit": "1/us",
            "detuning_convention": "drive_minus_qubit",
            "dephasing_collapse_operator": (
                "sqrt(gamma_phi / 2) sigma_z"
            ),
            "step_policy": {
                "policy_id": PULSE_API_STEP_POLICY_ID,
                "epsilon_h": PULSE_BASELINE_A_EPSILON_H,
                "epsilon_d": PULSE_BASELINE_A_EPSILON_D,
                "samples_per_sigma": (
                    PULSE_BASELINE_A_SAMPLES_PER_SIGMA
                ),
                "maximum_internal_steps": (
                    PULSE_API_MAX_INTERNAL_STEPS
                ),
            },
        },
        "artifact_audit": artifact_audit,
        "api_audit": api_checks,
        "overall_pass": all_artifacts_pass and api_pass,
        "scope_and_limitations": {
            "proves": [
                "all recorded V1-V7 and BA2-BA5 artifacts report pass",
                "the frozen pulse request executes in both input modes",
                "the dedicated pulse and existing gate API paths coexist",
                "the documented Pulse Baseline A OpenAPI shape is hashable",
            ],
            "does_not_prove": [
                "agreement with calibrated quantum hardware",
                "qutrit leakage or DRAG behavior",
                "multi-qubit pulse control",
                "CPTP evolution for arbitrary finite RK4 step sizes",
                "that recorded artifacts were regenerated in this audit",
            ],
        },
    }


def write_freeze_report(
    output_path: Path,
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Build and write the BA-6 freeze report."""

    report = build_freeze_report(root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _audit_validation_artifact(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "file": path.name,
            "exists": False,
            "validation": None,
            "pass": None,
            "sha256": None,
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    pass_value = data.get("overall_pass", data.get("passed"))
    return {
        "file": path.name,
        "exists": True,
        "validation": data.get("validation"),
        "pass": pass_value if isinstance(pass_value, bool) else None,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _pulse_openapi_contract(openapi: dict[str, Any]) -> dict[str, Any]:
    schemas = openapi.get("components", {}).get("schemas", {})
    pulse_schemas = {
        name: schema
        for name, schema in schemas.items()
        if name.startswith("Pulse")
    }
    return {
        "path": openapi["paths"]["/api/pulse/simulate"],
        "schemas": pulse_schemas,
    }


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _response_summary(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_mode": response["rates"]["input_mode"],
        "shape": response["input"]["shape"],
        "api_runtime_ms": response["diagnostics"]["api_runtime_ms"],
        "pulse_duration_us": response["input"]["pulse_duration_us"],
        "total_simulation_time_us": (
            response["input"]["total_simulation_time_us"]
        ),
        "sample_count": response["input"]["sample_count"],
        "estimated_internal_steps": (
            response["step_policy"]["estimated_internal_steps"]
        ),
        "pulse_end_population_1": (
            response["pulse_end"]["open_population_1"]
        ),
        "final_population_1": response["final"]["open_population_1"],
        "minimum_cleaned_eigenvalue": (
            response["diagnostics"]["minimum_cleaned_eigenvalue"]
        ),
    }


def _direct_rates_payload() -> dict[str, Any]:
    return {
        "model_id": DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
        "initial_state": "0",
        "pulse": {
            "shape": "square",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi,
            "pulse_duration_us": 0.2,
            "phase_rad": 0.0,
            "detuning_rad_per_us": 0.0,
        },
        "total_simulation_time_us": 0.6,
        "environment": {
            "input_mode": "direct_rates",
            "gamma_down_per_us": 0.1,
            "gamma_up_per_us": 0.02,
            "gamma_phi_per_us": 0.05,
        },
        "snapshot_options": {
            "uniform_count": 11,
            "custom_times_us": [0.2],
        },
    }


def _physical_payload() -> dict[str, Any]:
    return {
        "model_id": DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
        "initial_state": "0",
        "pulse": {
            "shape": "gaussian",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi / 2.0,
            "sigma_us": 0.05,
            "truncation_sigma": 4.0,
            "phase_rad": math.pi / 4.0,
            "detuning_rad_per_us": 0.2,
        },
        "total_simulation_time_us": 0.8,
        # Coherence times equal to the rates of the removed quality/flux inputs (D89), so frozen results still apply.
        "environment": {
            "input_mode": "physical",
            "temperature_mk": 20.0,
            "qubit_frequency_ghz": 5.0,
            "t1_zero_temperature_us": 63.09573444801933,
            "tphi_us": 54.19921423576098,
        },
        "snapshot_options": {
            "uniform_count": 11,
            "custom_times_us": [0.4],
        },
    }


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

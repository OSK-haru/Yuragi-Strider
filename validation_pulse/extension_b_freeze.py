"""Consolidated audit helpers for the Pulse Extension B freeze."""

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
from api.pulse_models import (
    PulseSimulateRequest,
    QutritPulseSimulateRequest,
)
from api.pulse_qutrit_service import (
    QUTRIT_API_CONTRACT_VERSION,
    QUTRIT_API_MAX_INTERNAL_STEPS,
    run_qutrit_pulse_request,
)
from api.pulse_service import (
    PULSE_API_CONTRACT_VERSION,
    PULSE_API_MAX_INTERNAL_STEPS,
    PulseExecutionLimitError,
    run_pulse_request,
)
from core.capabilities import (
    DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
    DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
    PULSE_MODEL_STATUSES,
)
from core.pulse_qutrit_contract import mhz_to_rad_per_us
from core.pulse_step_policy import PULSE_QUTRIT_EPSILON_H


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_VALIDATION_FILES = (
    "validation_results/pulse_baseline_a_freeze.json",
    "validation_results/pulse_b_closed_qutrit.json",
    "validation_results/pulse_b_closed_qutrit.csv",
    "validation_results/pulse_b_closed_qutrit_populations.png",
    "validation_results/pulse_b_closed_qutrit_leakage.png",
    "validation_results/pulse_b_qutrit_dissipation.json",
    "validation_results/pulse_b_qutrit_dissipation.csv",
    "validation_results/pulse_b_qutrit_thermal_equilibrium.png",
    "validation_results/pulse_b_qutrit_coherence_decay.png",
    "validation_results/pulse_b_qutrit_convergence.json",
    "validation_results/pulse_b_qutrit_convergence.csv",
    "validation_results/pulse_b_qutrit_convergence.png",
    "validation_results/pulse_b_drag.json",
    "validation_results/pulse_b_drag.csv",
    "validation_results/pulse_b_drag_convergence.png",
    "validation_results/pulse_b_drag_fidelity_phase.png",
    "validation_results/pulse_b_drag_leakage_sweep.png",
    "validation_results/pulse_b_qutip_qutrit.json",
    "validation_results/pulse_b_qutip_qutrit.csv",
    "validation_results/pulse_b_qutip_qutrit_error.png",
    "validation_results/pulse_extension_b_markdown_links.json",
    "validation_results/pulse_extension_b_regression.json",
)

REQUIRED_DOCUMENTS = (
    "docs_for_develop/physics/pulse-extension-b-qutrit-contract.md",
    "docs_for_develop/physics/pulse-extension-b-qutrit-model.md",
    "docs_for_develop/architecture/pulse-api-contract.md",
    "docs_for_develop/validation/pulse-extension-b-report.md",
    "docs_for_develop/validation/pulse-b-pulse-lab-ui.md",
    "docs_for_develop/development/pulse-extension-b/README.md",
    "docs_for_develop/development/pulse-extension-b/"
    "phase-b7-integration-and-freeze.md",
    "docs_for_develop/README.md",
    "frontend/README.md",
)

CRITICAL_SOURCE_FILES = (
    "core/pulse_qutrit_contract.py",
    "core/pulse_qutrit.py",
    "core/pulse_qutrit_open_system.py",
    "core/pulse_step_policy.py",
    "api/pulse_models.py",
    "api/pulse_service.py",
    "api/pulse_qutrit_service.py",
    "api/main.py",
    "frontend/src/types/pulse.ts",
    "frontend/src/utils/pulseLab.ts",
    "frontend/src/pages/PulseLabPage.tsx",
    "validation_pulse/extension_b_freeze.py",
    "scripts/validate_pulse_extension_b_freeze.py",
    "scripts/audit_pulse_extension_b_markdown_links.py",
    "scripts/validate_qutip_comparison.py",
    "tests/test_validation_qutip_csv_contract.py",
)


def build_freeze_report(root: Path = ROOT) -> dict[str, Any]:
    """Build the machine-readable Extension B freeze audit."""

    artifact_audit = [
        _audit_file(root / relative_path, root)
        for relative_path in REQUIRED_VALIDATION_FILES
    ]
    document_audit = [
        _audit_file(root / relative_path, root)
        for relative_path in REQUIRED_DOCUMENTS
    ]
    regression = _load_json(
        root / "validation_results/pulse_extension_b_regression.json"
    )
    openapi = app.openapi()
    pulse_contract = _pulse_openapi_contract(openapi)
    two_level = run_pulse_request(
        PulseSimulateRequest.model_validate(_two_level_payload())
    )
    qutrit_direct = run_qutrit_pulse_request(
        QutritPulseSimulateRequest.model_validate(
            _qutrit_direct_payload()
        )
    )
    qutrit_physical = run_qutrit_pulse_request(
        QutritPulseSimulateRequest.model_validate(
            _qutrit_physical_payload()
        )
    )
    over_budget = _over_budget_summary()
    source_revision = _source_revision(root)
    performance = _performance_summary(root)

    artifacts_pass = all(
        item["exists"] and item["pass"] is not False
        for item in artifact_audit
    )
    documents_pass = all(item["exists"] for item in document_audit)
    regression_pass = bool(
        regression
        and regression.get("overall_pass") is True
        and all(
            item.get("pass") is True
            for item in regression.get("commands", [])
        )
    )
    api_pass = (
        two_level["contract_version"] == PULSE_API_CONTRACT_VERSION
        and qutrit_direct["contract_version"]
        == QUTRIT_API_CONTRACT_VERSION
        and qutrit_physical["rates"]["input_mode"] == "physical"
        and over_budget["rejected"] is True
        and PULSE_MODEL_STATUSES[
            DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL
        ]
        == "available"
    )
    overall_pass = (
        artifacts_pass
        and documents_pass
        and regression_pass
        and api_pass
    )

    return {
        "validation": "PULSE-EXTENSION-B-FREEZE",
        "freeze_schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "decision": (
            "PASS WITH RESTRICTIONS" if overall_pass else "FAIL"
        ),
        "overall_pass": overall_pass,
        "frozen_contract": {
            "model_id": (
                DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL
            ),
            "contract_version": QUTRIT_API_CONTRACT_VERSION,
            "capability_status": "available",
            "frame": "rotating",
            "approximation": "RWA",
            "basis_order": ["0", "1", "2"],
            "subsystem_dimensions": [3],
            "time_unit": "us",
            "hamiltonian_unit": "rad/us",
            "rate_unit": "1/us",
            "detuning_convention": "drive_minus_transition_01",
            "anharmonicity_conversion": (
                "alpha_rad_per_us = 2*pi*anharmonicity_mhz"
            ),
            "leakage_definition": "population_2_without_renormalization",
            "dephasing_model": "number_operator_adjacent_rate_v1",
            "qutrit_core_work_ceiling": 25_000,
            "qutrit_api_work_ceiling": QUTRIT_API_MAX_INTERNAL_STEPS,
        },
        "baseline_a_compatibility": {
            "model_id": DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
            "contract_version": PULSE_API_CONTRACT_VERSION,
            "maximum_internal_steps": PULSE_API_MAX_INTERNAL_STEPS,
            "smoke": _two_level_summary(two_level),
        },
        "qutrit_api_audit": {
            "openapi_contract_sha256": _canonical_sha256(
                pulse_contract
            ),
            "direct_rates_smoke": _qutrit_summary(qutrit_direct),
            "physical_smoke": _qutrit_summary(qutrit_physical),
            "over_budget_smoke": over_budget,
        },
        "source_revision": source_revision,
        "environment": _environment(root),
        "artifact_audit": artifact_audit,
        "document_audit": document_audit,
        "regression_evidence": regression,
        "performance_budget": performance,
        "restrictions": [
            "single qutrit and one control pulse only",
            "three-level truncation only",
            "rotating-frame RWA only",
            "Markovian Lindblad environment only",
            "fixed-step RK4 has no strict finite-step CPTP guarantee",
            "no calibrated hardware prediction",
            "no multi-qubit or entangling pulse control",
            "no Rust time-dependent production backend",
            "Pulse Lab does not consume Circuit Studio state",
        ],
        "handoff": [
            "strict CPTP event or solver phase",
            "Rust time-dependent backend phase",
            "external observable validation V8",
            "separately designed circuit-to-pulse compilation phase",
        ],
    }


def write_freeze_report(
    output_path: Path,
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    """Build and write the Extension B freeze report."""

    report = build_freeze_report(root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _audit_file(path: Path, root: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "file": path.relative_to(root).as_posix(),
            "exists": False,
            "pass": None,
            "sha256": None,
        }
    pass_value: bool | None = None
    validation: str | None = None
    if path.suffix.lower() == ".json":
        data = _load_json(path)
        if data:
            candidate = data.get(
                "overall_pass",
                data.get("pass", data.get("passed")),
            )
            if isinstance(candidate, bool):
                pass_value = candidate
            validation = data.get(
                "validation",
                data.get("validation_id"),
            )
    return {
        "file": path.relative_to(root).as_posix(),
        "exists": True,
        "validation": validation,
        "pass": pass_value,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _pulse_openapi_contract(openapi: dict[str, Any]) -> dict[str, Any]:
    schemas = openapi.get("components", {}).get("schemas", {})
    pulse_schemas = {
        name: schema
        for name, schema in schemas.items()
        if name.startswith("Pulse") or name.startswith("QutritPulse")
    }
    return {
        "path": openapi["paths"]["/api/pulse/simulate"],
        "schemas": pulse_schemas,
    }


def _two_level_summary(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_mode": response["rates"]["input_mode"],
        "state_levels": response["model"]["state_levels"],
        "sample_count": len(response["trajectory"]),
        "estimated_internal_steps": (
            response["step_policy"]["estimated_internal_steps"]
        ),
        "final_population_1": response["final"]["open_population_1"],
        "minimum_cleaned_eigenvalue": (
            response["diagnostics"]["minimum_cleaned_eigenvalue"]
        ),
    }


def _qutrit_summary(response: dict[str, Any]) -> dict[str, Any]:
    final = response["final"]
    return {
        "input_mode": response["rates"]["input_mode"],
        "state_levels": response["model"]["state_levels"],
        "density_matrix_shape": [
            len(final["density_matrix"]),
            len(final["density_matrix"][0]),
        ],
        "sample_count": len(response["trajectory"]),
        "estimated_internal_steps": (
            response["step_policy"]["estimated_internal_step_count"]
        ),
        "final_population_sum": (
            final["population_0"]
            + final["population_1"]
            + final["population_2"]
        ),
        "final_leakage": final["leakage_probability"],
        "minimum_cleaned_eigenvalue": (
            response["diagnostics"]["minimum_cleaned_eigenvalue"]
        ),
        "api_runtime_ms": response["diagnostics"]["api_runtime_ms"],
    }


def _over_budget_summary() -> dict[str, Any]:
    """Submit a request that cannot fit in the qutrit work budget.

    The total time is derived from the current ceiling instead of being fixed,
    so raising the ceiling cannot silently turn this check into a request that
    the API happily executes.
    """

    anharmonicity_mhz = -250.0
    payload = _qutrit_direct_payload()
    payload["anharmonicity_mhz"] = anharmonicity_mhz
    payload["pulse"]["sigma_us"] = 0.02
    payload["total_simulation_time_us"] = _over_budget_total_time_us(
        anharmonicity_mhz
    )
    try:
        run_qutrit_pulse_request(
            QutritPulseSimulateRequest.model_validate(payload)
        )
    except PulseExecutionLimitError as exc:
        return {
            "rejected": True,
            "exception": type(exc).__name__,
            "message": str(exc),
        }
    return {
        "rejected": False,
        "exception": None,
        "message": "over-budget fixture unexpectedly executed",
    }


def _over_budget_total_time_us(anharmonicity_mhz: float) -> float:
    """Return a total time that exceeds the ceiling for any allowed step.

    The anharmonicity alone caps the internal step at
    ``epsilon_h / |alpha|``; every other limit in the step policy is smaller
    or equal, so a total time twice the ceiling times that cap needs more
    steps than the budget allows no matter how the policy is tuned.
    """

    largest_step_us = PULSE_QUTRIT_EPSILON_H / abs(
        mhz_to_rad_per_us(anharmonicity_mhz)
    )
    return 2.0 * (QUTRIT_API_MAX_INTERNAL_STEPS + 1) * largest_step_us


def _performance_summary(root: Path) -> dict[str, Any]:
    convergence = _load_json(
        root / "validation_results/pulse_b_qutrit_convergence.json"
    )
    drag = _load_json(root / "validation_results/pulse_b_drag.json")
    return {
        "measured_qutrit": convergence.get("performance"),
        "drag_validation_runtime_ms": drag.get("runtime_ms"),
        "api_policy": {
            "maximum_concurrent_requests": 2,
            "wait_timeout_seconds": 15,
            "qutrit_internal_step_ceiling": (
                QUTRIT_API_MAX_INTERNAL_STEPS
            ),
            "limit_decision": (
                "retained after B-7; not raised to fit demonstrations"
            ),
        },
    }


def _source_revision(root: Path) -> dict[str, Any]:
    source_files = [
        _audit_file(root / relative_path, root)
        for relative_path in CRITICAL_SOURCE_FILES
    ]
    digest = hashlib.sha256()
    for item in source_files:
        digest.update(item["file"].encode("utf-8"))
        digest.update((item["sha256"] or "missing").encode("ascii"))
    status = _git_output(root, ["status", "--porcelain"])
    return {
        "base_git_commit": _git_output(
            root, ["rev-parse", "HEAD"]
        ).strip()
        or "unavailable",
        "working_tree_dirty": bool(status.strip()),
        "working_tree_change_count": len(status.splitlines()),
        "critical_source_tree_sha256": digest.hexdigest(),
        "critical_source_files": source_files,
    }


def _environment(root: Path) -> dict[str, Any]:
    frontend = _load_json(root / "frontend/package.json")
    return {
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
        "node_version": _command_version(["node", "--version"]),
        "npm_version": _command_version(["npm.cmd", "--version"]),
        "frontend_declared_dependencies": {
            **frontend.get("dependencies", {}),
            **frontend.get("devDependencies", {}),
        },
    }


def _two_level_payload() -> dict[str, Any]:
    return {
        "model_id": DRIVEN_TWO_LEVEL_RWA_EXPERIMENTAL_MODEL,
        "initial_state": "0",
        "pulse": {
            "shape": "square",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi / 2.0,
            "pulse_duration_us": 0.02,
            "phase_rad": 0.0,
            "detuning_rad_per_us": 0.0,
        },
        "total_simulation_time_us": 0.03,
        "environment": {
            "input_mode": "direct_rates",
            "gamma_down_per_us": 0.01,
            "gamma_up_per_us": 0.001,
            "gamma_phi_per_us": 0.005,
        },
        "snapshot_options": {
            "uniform_count": 9,
            "custom_times_us": [0.02],
        },
    }


def _qutrit_direct_payload() -> dict[str, Any]:
    return {
        "model_id": DRIVEN_TRANSMON_QUTRIT_RWA_EXPERIMENTAL_MODEL,
        "initial_state": "0",
        "anharmonicity_mhz": -100.0,
        "pulse": {
            "shape": "gaussian",
            "amplitude_mode": "target_rotation_angle",
            "target_rotation_angle_rad": math.pi / 2.0,
            "sigma_us": 0.002,
            "truncation_sigma": 4.0,
            "phase_rad": 0.2,
            "detuning_rad_per_us": 0.1,
            "drag_beta_us": 0.001,
        },
        "total_simulation_time_us": 0.02,
        "environment": {
            "input_mode": "direct_rates",
            "gamma_10_down_per_us": 0.2,
            "gamma_01_up_per_us": 0.02,
            "gamma_21_down_per_us": 0.4,
            "gamma_12_up_per_us": 0.03,
            "gamma_phi_adjacent_per_us": 0.08,
        },
        "snapshot_options": {
            "uniform_count": 9,
            "custom_times_us": [0.016, 0.02],
        },
    }


def _qutrit_physical_payload() -> dict[str, Any]:
    payload = _qutrit_direct_payload()
    # Coherence times equal to the rates of the removed quality/flux inputs (D89), so frozen results still apply.
    payload["environment"] = {
        "input_mode": "physical",
        "temperature_mk": 15.0,
        "qubit_frequency_ghz": 5.0,
        "t1_zero_temperature_us": 39.810717055349734,
        "tphi_zero_temperature_us": 33.20178309308936,
        "readout_resonator_frequency_ghz": 7.0,
        "readout_resonator_kappa_mhz": 1.0,
        "readout_resonator_chi_mhz": 0.5,
    }
    return payload


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _package_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "not-installed"


def _git_output(root: Path, arguments: list[str]) -> str:
    try:
        return subprocess.check_output(
            ["git", *arguments],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""


def _command_version(command: list[str]) -> str:
    try:
        return subprocess.check_output(
            command,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"

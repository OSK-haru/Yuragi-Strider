"""Versioned metadata for internal-only tenant-isolation physics."""

from __future__ import annotations


EXPERIMENTAL_ISOLATION_PHYSICS_VERSION = "shared_qpu_isolation_physics_v0"


def experimental_isolation_physics_capabilities() -> dict[str, object]:
    """Return the internal contract without publishing a selectable model ID."""

    return {
        "version": EXPERIMENTAL_ISOLATION_PHYSICS_VERSION,
        "status": "internal_only",
        "public_api_selectable": False,
        "physical_baseline": "yuragi_strider_physical_baseline_v1",
        "mechanisms": {
            "independent_idle": {
                "stage": "explicit_interval_kernel_only",
                "model": "constant_rate_local_thermal_relaxation_and_dephasing",
                "local_dimensions": [2],
                "runtime_connected": False,
                "public_api_selectable": False,
            },
            "imperfect_reset": {
                "stage": "between_jobs_state_transition",
                "model": "cptp_success_replacement_failure_retention",
                "local_dimensions": [2, 3],
                "common_boundary": "ResetModel",
                "profile_source": "simulator_derived",
                "public_api_selectable": False,
            },
            "residual_zz": {
                "stage": "hamiltonian",
                "model": "cross_kerr_number_number",
                "units": "rad_per_us",
            },
            "drive_crosstalk": {
                "stage": "control_hamiltonian",
                "model": "sparse_complex_control_line_transfer",
            },
            "correlated_readout": {
                "stage": "observation",
                "model": "local_joint_column_stochastic_confusion_matrix",
            },
        },
        "limits": [
            "Internal single-QPU FIFO and in-memory cross-job state exist; no public authorization API or persistent store yet.",
            "No public scenario API or grader integration yet.",
            "No IQ-cloud or analog measurement-chain model.",
            "Reset target populations are phenomenological, not device calibrated.",
            "Control transfer is instantaneous and frequency independent.",
        ],
        "references": [
            {
                "topic": "active_reset",
                "doi": "10.1103/PhysRevLett.121.060502",
            },
            {
                "topic": "residual_zz",
                "arxiv": "2111.13292",
            },
            {
                "topic": "microwave_drive_crosstalk",
                "arxiv": "2207.08416",
            },
            {
                "topic": "readout_correlations",
                "doi": "10.1103/PRXQuantum.6.010307",
            },
            {
                "topic": "readout_correlation_characterization",
                "arxiv": "2104.04607",
            },
        ],
    }

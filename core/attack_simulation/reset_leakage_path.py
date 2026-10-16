"""Reset-leakage values for the declared attack path.

Challenge-specific authoring values; the generic path schema stays in attack_path.
"""

from __future__ import annotations

RESET_LEAKAGE_MECHANISM = "incomplete_reset_state_carryover"


def reset_leakage_attack_path_config() -> dict[str, str]:
    """Authoring values for the first shared-QPU profile, not a universal path."""

    return {
        "attacker_entry": "job_submission",
        "shared_resource": "same_physical_qubit",
        "mechanism_id": RESET_LEAKAGE_MECHANISM,
        "victim_asset": "nonpublic_output_bit",
        "attacker_observation": "own_measurement_counts",
    }

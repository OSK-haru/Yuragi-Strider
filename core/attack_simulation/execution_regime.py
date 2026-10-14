"""Execution regimes selected by device/service profiles.

The attack-simulation platform is not permanently bound to one regime.
Individual runtime plans must declare which regimes they can execute.
"""

from enum import Enum


class ExecutionRegime(str, Enum):
    """Known physical/logical execution layers for scenario profiles."""

    NISQ_PHYSICAL = "nisq_physical"
    FAULT_TOLERANT_LOGICAL = "fault_tolerant_logical"

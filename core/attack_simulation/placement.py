"""Physical-resource placement policies shared by scenario and runtime layers."""

from enum import Enum


class PlacementPolicy(str, Enum):
    GUARANTEED_SAME_PHYSICAL_QUBIT = "guaranteed_same_physical_qubit"
    SCHEDULER_DEPENDENT = "scheduler_dependent"
    ISOLATED_PHYSICAL_RESOURCE = "isolated_physical_resource"
    TENANT_DEDICATED_REGION = "tenant_dedicated_region"

"""Simulated connection-credential assumptions, never real credentials."""

from enum import Enum
from collections.abc import Mapping


class CredentialDelivery(str, Enum):
    PRECONFIGURED_PROFILE = "preconfigured_profile"
    MANUAL_SIMULATED_CREDENTIAL = "manual_simulated_credential"


class CredentialOrigin(str, Enum):
    SIMULATOR_ISSUED = "simulator_issued"


def validate_credential_policy(attacker: Mapping, service: Mapping) -> None:
    """Validate an optional credential assumption at the compile boundary."""

    mode = attacker.get("credential_delivery")
    origin = attacker.get("credential_origin")
    supported = service.get("supported_credential_deliveries")
    if mode is None and supported is None and origin is None:
        return  # Older templates remain valid until migrated.
    if not isinstance(mode, str) or not isinstance(supported, (list, tuple)) or not isinstance(origin, str):
        raise ValueError("credential delivery, origin, and service support must all be configured")
    try:
        selected = CredentialDelivery(mode)
        CredentialOrigin(origin)
        offered = tuple(CredentialDelivery(item) for item in supported)
    except ValueError as error:
        raise ValueError("unsupported credential delivery or origin") from error
    if not offered or len(set(offered)) != len(offered):
        raise ValueError("supported credential deliveries must be nonempty and unique")
    if selected not in offered:
        raise ValueError("credential delivery is not supported by the service")

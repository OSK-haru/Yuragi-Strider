"""Internal physical primitives for future shared-QPU isolation scenarios.

This package is intentionally absent from the public API capability registry.
Importing it is an explicit opt-in for tests and internal experiments; it does
not change Physical Baseline V1 or make a UI-selectable simulation model.
"""

from core.experimental_isolation_physics.capabilities import (
    EXPERIMENTAL_ISOLATION_PHYSICS_VERSION,
    experimental_isolation_physics_capabilities,
)
from core.experimental_isolation_physics.coherent_crosstalk import (
    DriveCrosstalkTransfer,
    ExtendedTransmonNetworkHamiltonian,
    ResidualZZCoupling,
)
from core.experimental_isolation_physics.correlated_readout import (
    CorrelatedReadoutChannel,
    apply_correlated_readout,
)
from core.experimental_isolation_physics.imperfect_reset import (
    ImperfectResetSpec,
    apply_local_imperfect_reset,
    imperfect_reset_channel,
)
from core.experimental_isolation_physics.idle_evolution import (
    IdleQubitSpec,
    IndependentIdleModel,
)
from core.experimental_isolation_physics.reset_calibration import (
    FrequencyThresholdCalibration,
    LeakageCalibrationPoint,
    ResetCalibrationResult,
    ResetCalibrationInputMode,
    ResetProfile,
    binary_input_mutual_information,
    calibrate_frequency_threshold,
    derive_reset_profile,
    evaluate_frequency_threshold,
    hidden_evaluation_pass_probability,
    run_reset_leakage_calibration,
)
from core.experimental_isolation_physics.reset_model import (
    PhenomenologicalResetModel,
    ResetModel,
    ResetModelMode,
    ResetModelRegistry,
    ResetModelRequest,
    ResetModelResult,
    RegisterResetResult,
)

__all__ = (
    "EXPERIMENTAL_ISOLATION_PHYSICS_VERSION",
    "CorrelatedReadoutChannel",
    "DriveCrosstalkTransfer",
    "ExtendedTransmonNetworkHamiltonian",
    "FrequencyThresholdCalibration",
    "ImperfectResetSpec",
    "IdleQubitSpec",
    "IndependentIdleModel",
    "LeakageCalibrationPoint",
    "PhenomenologicalResetModel",
    "ResetCalibrationResult",
    "ResetCalibrationInputMode",
    "ResetModel",
    "ResetModelMode",
    "ResetModelRegistry",
    "ResetModelRequest",
    "ResetModelResult",
    "RegisterResetResult",
    "ResetProfile",
    "ResidualZZCoupling",
    "apply_correlated_readout",
    "apply_local_imperfect_reset",
    "binary_input_mutual_information",
    "calibrate_frequency_threshold",
    "derive_reset_profile",
    "evaluate_frequency_threshold",
    "experimental_isolation_physics_capabilities",
    "imperfect_reset_channel",
    "hidden_evaluation_pass_probability",
    "run_reset_leakage_calibration",
)

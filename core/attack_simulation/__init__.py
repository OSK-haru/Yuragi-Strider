"""Shared runtime contracts for free and challenge attack-simulation modes."""

from core.attack_simulation.credential_policy import CredentialDelivery, CredentialOrigin
from core.attack_simulation.circuit_program import CircuitExecutionOperation, LocalCircuitCompiler
from core.attack_simulation.scenario_environment import ScenarioEnvironment

from core.attack_simulation.jobs import (
    AttackerMeasureOperation,
    FifoJobScheduler,
    JobCommand,
    JobRecord,
    JobStatus,
    VictimPrepareOperation,
)
from core.attack_simulation.job_authorization import JobAuthorizationPolicy
from core.attack_simulation.execution_regime import ExecutionRegime
from core.attack_simulation.observation_budget import (
    ObservationBudgetExceeded,
    ObservationBudgetLedger,
    ObservationBudgetUsage,
    ObservationQueryKind,
)
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.qpu_state import (
    QpuEventRecord,
    QpuStateSnapshot,
    QpuStateStore,
    QpuStateTransition,
    RevisionConflictError,
    single_qubit_zero_snapshot,
    local_zero_snapshot,
)
from core.attack_simulation.resource_binding import PhysicalQubitLayout
from core.attack_simulation.scenario_compiler import (
    AssumptionBasis,
    AssumptionRecord,
    InMemoryScenarioManifestStore,
    ResolvedScenarioManifest,
    SCENARIO_MANIFEST_SCHEMA_VERSION,
    ScenarioCompilationError,
    ScenarioCompiler,
    ScenarioConstraint,
    ScenarioConstraintViolation,
    ScenarioOverride,
)
from core.attack_simulation.reset_leakage_grader import (
    BinaryObservation,
    BinaryVictimEstimator,
    EstimatorExecutionError,
    FrequencyThresholdEstimator,
    HiddenEvaluationPolicy,
    HiddenEvaluationResult,
)
from core.attack_simulation.reset_leakage_analysis import (
    FrequencyThresholdTrainingError,
    FrequencyThresholdTrainingResult,
    LabeledBinaryObservation,
    fit_frequency_threshold_estimator,
)
from core.attack_simulation.reset_leakage_settings import (
    RESET_LEAKAGE_V0_SETTINGS,
    ResetLeakageV0Settings,
)
from core.attack_simulation.scenario_policy import (
    ObservationBudget,
    ScenarioMode,
    ScenarioPolicy,
    ScenarioPreset,
    ScenarioSession,
    ScenarioTemplate,
)
from core.attack_simulation.shared_qpu_runtime import (
    SharedQpuRuntime,
    SharedQpuRuntimeConfig,
)
from core.attack_simulation.job_service import (
    DeviceInfo,
    JobApplicationService,
    JobNotFoundError,
    JobResultNotReadyError,
    JobResultView,
    JobStatusView,
    PrincipalGrant,
)

__all__ = (
    "CircuitExecutionOperation",
    "LocalCircuitCompiler",
    "PhysicalQubitLayout",
    "local_zero_snapshot",
    "DeviceInfo",
    "AttackerMeasureOperation",
    "AssumptionBasis",
    "AssumptionRecord",
    "BinaryObservation",
    "BinaryVictimEstimator",
    "EstimatorExecutionError",
    "ExecutionRegime",
    "FifoJobScheduler",
    "FrequencyThresholdEstimator",
    "FrequencyThresholdTrainingError",
    "FrequencyThresholdTrainingResult",
    "HiddenEvaluationPolicy",
    "HiddenEvaluationResult",
    "InMemoryScenarioManifestStore",
    "JobCommand",
    "JobApplicationService",
    "JobAuthorizationPolicy",
    "JobNotFoundError",
    "JobRecord",
    "JobResultNotReadyError",
    "JobResultView",
    "JobStatus",
    "JobStatusView",
    "LabeledBinaryObservation",
    "ObservationBudget",
    "ObservationBudgetExceeded",
    "ObservationBudgetLedger",
    "ObservationBudgetUsage",
    "ObservationQueryKind",
    "PlacementPolicy",
    "PrincipalGrant",
    "QpuEventRecord",
    "QpuStateSnapshot",
    "QpuStateStore",
    "QpuStateTransition",
    "RevisionConflictError",
    "ResetLeakageV0Settings",
    "ScenarioCompilationError",
    "ScenarioCompiler",
    "ScenarioConstraint",
    "ScenarioConstraintViolation",
    "ScenarioOverride",
    "ScenarioMode",
    "ScenarioPolicy",
    "ScenarioPreset",
    "ScenarioSession",
    "ScenarioTemplate",
    "ResolvedScenarioManifest",
    "SCENARIO_MANIFEST_SCHEMA_VERSION",
    "SharedQpuRuntime",
    "SharedQpuRuntimeConfig",
    "RESET_LEAKAGE_V0_SETTINGS",
    "VictimPrepareOperation",
    "fit_frequency_threshold_estimator",
    "single_qubit_zero_snapshot",
)

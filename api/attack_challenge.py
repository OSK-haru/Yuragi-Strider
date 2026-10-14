"""Reset-leakage challenge: server-owned preset and the hidden-case evaluator.

The preset reuses the common product profile (same physics, same job service,
same interleaved observation operation). Only three things are challenge
specific and all of them are policy: the victim's secret bit is private and
random per environment, the approved v0 reset/shot/budget values are locked,
and a submitted estimator is graded on hidden cases that the participant can
never observe directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock
import hashlib
import hmac
import json
import random
import secrets
from typing import Any

from api.attack_device_snapshot import DeviceSnapshot, deterministic_connected_region, load_ibm_kingston_snapshot
from api.attack_product_factory import (
    IDLE_INPUT_CALIBRATION, ProductAssemblyLimits, _interleave_victims, _parse_frozen_inputs,
    build_service_stack, message_bits,
)
from api.attack_product_profiles import MAX_RETAINED_QUBITS, OPERATIONS, _profile, _topology
from api.attack_settings_resolver import (
    ChallengeDefinition, RESET_LEAKAGE_MESSAGE_PRESET_ID, RESET_LEAKAGE_PRESET_ID,
)
from core.attack_simulation.job_service import InterleaveVictimSpec
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.reset_leakage_grader import BinaryObservation, FrequencyThresholdEstimator
from core.attack_simulation.reset_leakage_settings import RESET_LEAKAGE_V0_SETTINGS, ResetLeakageV0Settings
from core.attack_simulation.scenario_assumptions import AssumptionBasis, AssumptionRecord
from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest
from core.attack_simulation.scenario_policy import (
    ObservationBudget, ScenarioMode, ScenarioPolicy, ScenarioPreset, ScenarioTemplate,
)
from core.experimental_isolation_physics.imperfect_reset import ImperfectResetSpec
from core.experimental_isolation_physics.reset_model import PerQubitPhenomenologicalResetModel

EVALUATOR_ID = "interleaved-threshold-v1"
CHALLENGE_TEMPLATE_ID = "reset_leakage_challenge"
EVALUATION_KEY = "evaluation"
MAX_SUBMISSIONS = 3
# One observation of the real (hidden) victim; the rest is training.
VICTIM_OBSERVATION_LIMIT = 1

MESSAGE_EVALUATOR_ID = "message-exact-v1"
MESSAGE_TEMPLATE_ID = "reset_leakage_message_challenge"
# Owner-chosen fixed message (Feynman's blackboard). Fixed on purpose: once
# known it spreads, which the owner accepted.
SECRET_MESSAGE = "WhatIcannotcreateIdonotunderstand"
# Victim observations allowed per message bit.
OBSERVATIONS_PER_MESSAGE_BIT = 5


def challenge_region(snapshot: DeviceSnapshot) -> tuple[int, ...]:
    """Only the shared qubit: the attack needs no other, and each retained qubit
    doubles the state that every recorded event copies. Free mode keeps the full
    region. The qubit is the first of the product's connected region, as before."""
    return tuple(sorted(deterministic_connected_region(snapshot, MAX_RETAINED_QUBITS)))[:1]


def reset_leakage_challenge(snapshot: DeviceSnapshot | None = None,
                            settings: ResetLeakageV0Settings = RESET_LEAKAGE_V0_SETTINGS,
                            ) -> ChallengeDefinition:
    """The approved v0 challenge on the qubit the hidden tenant shares."""
    snapshot = load_ibm_kingston_snapshot() if snapshot is None else snapshot
    region = challenge_region(snapshot)
    topology = _topology(snapshot)
    base = _profile(snapshot, topology, region, IDLE_INPUT_CALIBRATION, None,
                    PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT.value)

    def build() -> ScenarioPreset:
        evaluation = {
            "hidden_case_count": settings.hidden_case_count,
            "hidden_zero_case_count": settings.hidden_zero_case_count,
            "required_correct": settings.hidden_required_correct,
            "rounds_per_case": settings.shots_per_case,
            "max_submissions": MAX_SUBMISSIONS,
        }
        return _preset(base, region, settings, CHALLENGE_TEMPLATE_ID, EVALUATOR_ID,
                       {"secret_bit": secrets.randbelow(2)},  # Private to this environment.
                       evaluation, VICTIM_OBSERVATION_LIMIT)

    return ChallengeDefinition(
        RESET_LEAKAGE_PRESET_ID, "Presumption of reset bits",
        "同じ qubit を共有する被害者の秘密bitが、サービスの不完全なResetを通して漏れることを観測し、"
        "既知ラベルで学習した判定規則を提出して、見えない被害者のケースで採点されます。"
        "測定前にゲートで状態を変えると、Resetの残りは観測できません。",
        preset_factory=build,
    )


def reset_leakage_message_challenge(snapshot: DeviceSnapshot | None = None,
                                    settings: ResetLeakageV0Settings = RESET_LEAKAGE_V0_SETTINGS,
                                    message: str = SECRET_MESSAGE) -> ChallengeDefinition:
    """The same reset leakage, read out as a whole message one bit per observation."""
    snapshot = load_ibm_kingston_snapshot() if snapshot is None else snapshot
    region = challenge_region(snapshot)
    base = _profile(snapshot, _topology(snapshot), region, IDLE_INPUT_CALIBRATION, None,
                    PlacementPolicy.GUARANTEED_SAME_PHYSICAL_QUBIT.value)
    bit_count = len(message_bits(message))
    limit = bit_count * OBSERVATIONS_PER_MESSAGE_BIT

    def build() -> ScenarioPreset:
        return _preset(base, region, settings, MESSAGE_TEMPLATE_ID, MESSAGE_EVALUATOR_ID,
                       {"secret_message": message}, {"max_submissions": MAX_SUBMISSIONS}, limit)

    return ChallengeDefinition(
        RESET_LEAKAGE_MESSAGE_PRESET_ID, "Reset leakage",
        f"同じ qubit を共有する被害者は、秘密のメッセージ（{len(message)}文字）を ASCII 8 bit・"
        f"上位ビットから並べた {bit_count} bit を、先頭から1 bit ずつ繰り返し処理しています。"
        "交互観測（--interleave other）が k 回目に受理されると（k は 0 から数える）、"
        f"そのジョブの被害者は bit (k mod {bit_count}) を準備して測定し、サービスの不完全な Reset の"
        "残りが攻撃者の測定に漏れます。拒否された送信は数えません。"
        f"被害者の観測は {limit} 回（1 bit あたり {OBSERVATIONS_PER_MESSAGE_BIT} 回）まで、"
        "既知ラベルでの学習は別枠です。メッセージ全体を提出し、完全一致で採点されます。"
        f"提出は {MAX_SUBMISSIONS} 回までで、部分的な正誤は返りません。",
        preset_factory=build,
        not_reproduced=("被害者のメッセージ", "観測回数の上限", "採点と提出"),
    )


def _preset(base, region: tuple[int, ...], settings: ResetLeakageV0Settings, template_id: str,
            evaluator_id: str, secret: dict[str, object], evaluation: dict[str, object],
            victim_observation_limit: int) -> ScenarioPreset:
    template = base.template
    qpu = _thaw(template.qpu_service_config)
    # This challenge is built on the abstract interleaved observation, so the preset turns it on.
    qpu["abstract_commands"] = {"interleaved_observation": True}
    qpu["supported_operations"] = list(OPERATIONS)
    entries = qpu["reset_model"]["qubits"]
    entries[str(region[0])]["success_probability"] = settings.reset_success_probability
    model = PerQubitPhenomenologicalResetModel(
        region,
        tuple(ImperfectResetSpec(entries[str(qid)]["success_probability"], (1.0, 0.0)) for qid in region),
        tuple((entries[str(qid)]["observed_one_given_zero"], entries[str(qid)]["observed_zero_given_one"])
              for qid in region),
    )
    qpu["runtime_execution"]["reset_model_configuration_id"] = model.configuration_id
    qpu["runtime_execution"]["reset_count"] = settings.reset_count
    attacker = _thaw(template.attacker_config)
    attacker["interleaved_access"] = {"max_rounds_per_query": settings.shots_per_case}
    victim = _thaw(template.victim_config)
    victim["interleaved_run"] = {"program": "prepare_basis_state_then_measure", **secret}
    experiment = _thaw(template.experiment_config)
    experiment[EVALUATION_KEY] = dict(evaluation)
    challenge_template = ScenarioTemplate(
        template_id, "1", attacker, victim, qpu, _thaw(template.relation_config), experiment,
    )
    # Every configured section is locked: a challenge exposes no editable input.
    locked = tuple(
        f"{root}.{key}"
        for root, section in (("attacker", attacker), ("victim", victim), ("qpu_service", qpu),
                              ("experiment", experiment), ("relation", template.relation_config))
        for key in section)
    policy = ScenarioPolicy(
        ScenarioMode.CHALLENGE,
        observation_budget=ObservationBudget(
            shots_per_query=settings.shots_per_case,
            training_query_limit=settings.training_query_limit,
            victim_observation_limit=victim_observation_limit,
        ),
        locked_fields=locked,
        hidden_fields=("victim.interleaved_run", f"experiment.{EVALUATION_KEY}"),
        evaluator_id=evaluator_id,
        submission_enabled=True,
        allowed_operations=OPERATIONS,
    )
    secret_path = f"victim.interleaved_run.{next(iter(secret))}"
    # The base profile documents a secret bit; a message preset documents its message instead.
    records = tuple(record for record in base.assumptions
                    if not any(path.startswith("victim.interleaved_run.secret")
                               and path != secret_path for path in record.target_paths))
    if "secret_message" in secret:
        records += (AssumptionRecord(
            "challenge_victim_message", (secret_path,), AssumptionBasis.SIMULATOR_CHOICE,
            "The victim prepares the bits of a fixed ASCII message (8 bits per character, most "
            "significant first) on the shared qubit, one bit per accepted interleaved observation in "
            "order, and measures each. The message is an owner-chosen challenge text."),)
    records += (
        AssumptionRecord(
            "challenge_victim_grants", ("victim.operation_grants",), AssumptionBasis.SIMULATOR_CHOICE,
            "The victim holds the same ordinary job operations as any tenant; its interleaved runs are "
            "scheduled by the scenario, not requested through these grants."),
        AssumptionRecord(
            "challenge_service_operations", ("qpu_service.supported_operations",), AssumptionBasis.SIMULATOR_CHOICE,
            "The challenge service offers the product job operations, inherit-state Programs and the two "
            "interleaved observation operations (an abstract command that real services lack; this preset "
            "enables it explicitly); solution submission is a separate evaluator channel."),
        AssumptionRecord(
            "challenge_reset_success", ("qpu_service.runtime_execution.reset_count",),
            AssumptionBasis.PAPER_ASSUMPTION,
            f"Reset success probability {settings.reset_success_probability} and {settings.shots_per_case} "
            "shots per case are the owner-approved reset_leakage_v0 profile values for this challenge only.",
            evidence_ref="docs_for_develop/development/attack-simulation/reset-leakage-calibration-2026-09-28.md"),
    )
    return ScenarioPreset(challenge_template, policy, _unique(records))


def _unique(records):
    seen, kept = set(), []
    for record in records:
        if record.assumption_id not in seen:
            seen.add(record.assumption_id)
            kept.append(record)
    return tuple(kept)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class SubmissionResult:
    attempt: int
    attempts_remaining: int
    correct: int
    total: int
    required: int
    passed: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt, "attempts_remaining": self.attempts_remaining,
            "correct": self.correct, "total": self.total, "required": self.required,
            "passed": self.passed,
        }


class SubmissionRejected(ValueError):
    """The submitted estimator is malformed or no attempts remain."""


class HiddenCaseEvaluator:
    """Grade a threshold estimator on hidden victims run through the shared physics.

    Each hidden case is a fresh environment built from the same frozen manifest
    with only the victim's bit replaced, so the grader cannot differ from what
    the participant could observe. Measured counts are independent of the
    submitted rule, so they are generated once and re-scored on later attempts.
    """

    def __init__(self, manifest: ResolvedScenarioManifest, limits: ProductAssemblyLimits,
                 *, case_seed: int | None = None) -> None:
        """``case_seed`` fixes case order and measurement entropy for replayable tests."""
        config = manifest.experiment_config.get(EVALUATION_KEY)
        if not manifest.submission_enabled or manifest.evaluator_id != EVALUATOR_ID or config is None:
            raise ValueError("manifest does not enable this evaluator")
        self._manifest, self._limits = manifest, limits
        self._total = config["hidden_case_count"]
        zero = config["hidden_zero_case_count"]
        self._required = config["required_correct"]
        self._rounds = config["rounds_per_case"]
        self._max_submissions = config["max_submissions"]
        bits = [0] * zero + [1] * (self._total - zero)
        self._case_seed = secrets.randbits(64) if case_seed is None else case_seed
        random.Random(self._case_seed).shuffle(bits)
        self._fixed_entropy = case_seed is not None
        self._bits = tuple(bits)
        self._cases: tuple[BinaryObservation, ...] | None = None
        self._attempts = 0
        self._lock = Lock()

    @property
    def attempts_remaining(self) -> int:
        with self._lock:
            return self._max_submissions - self._attempts

    def submit(self, estimator: Mapping[str, object]) -> SubmissionResult:
        threshold, at_or_above = _parse_estimator(estimator)
        with self._lock:
            if self._attempts >= self._max_submissions:
                raise SubmissionRejected("no submissions remain")
            self._attempts += 1
            attempt = self._attempts
            if self._cases is None:
                self._cases = self._observe_cases()
            rule = FrequencyThresholdEstimator(threshold, at_or_above, "submitted_threshold", "1")
            correct = sum(int(rule.predict(case) == bit) for case, bit in zip(self._cases, self._bits, strict=True))
        return SubmissionResult(attempt, self._max_submissions - attempt, correct, self._total,
                                self._required, correct >= self._required)

    def _observe_cases(self) -> tuple[BinaryObservation, ...]:
        parsed = _parse_frozen_inputs(self._manifest, self._limits)
        program = CircuitProgram.from_editor_json(
            '{"logical_qubits":1,"initial_states":[0],"classical_bits":1,"columns":['
            '{"step":0,"gates":[{"type":"MEASURE","targets":[0],"classical_targets":[0]}]}]}')
        victim_principal = self._manifest.victim_config["principal_id"]
        attacker_principal = self._manifest.attacker_config["principal_id"]
        device = parsed["snapshot"].qpu_id
        observed = []
        for index, bit in enumerate(self._bits):
            victims = tuple(
                InterleaveVictimSpec(spec.reference, victim_principal, _bit_program(bit))
                if spec.reference == "victim" else spec
                for spec in _interleave_victims(self._manifest))
            runtime, service = build_service_stack(parsed, self._manifest, interleave_victims=victims)
            if self._fixed_entropy:
                service._sampling_salt = hashlib.sha256(f"{self._case_seed}:{index}".encode()).digest()
            service.submit_program(attacker_principal, ProgramSubmitRequest(
                program, device, self._rounds, f"hidden-{index}", "inherit", interleave="victim"))
            done = runtime.run_next()
            if done is None or done.result is None:
                raise RuntimeError("hidden case did not complete")
            counts = {label: int(done.result["counts"].get(label, 0)) for label in ("0", "1")}
            observed.append(BinaryObservation(shots=self._rounds, counts=counts))
        return tuple(observed)


@dataclass(frozen=True)
class MessageSubmissionResult:
    attempt: int
    attempts_remaining: int
    passed: bool

    def to_public_dict(self) -> dict[str, object]:
        # No partial-match hint: only whether the whole message matched.
        return {"attempt": self.attempt, "attempts_remaining": self.attempts_remaining,
                "passed": self.passed}


class MessageExactEvaluator:
    """Grade a submitted message by exact match with the victim's private message."""

    def __init__(self, manifest: ResolvedScenarioManifest) -> None:
        config = manifest.experiment_config.get(EVALUATION_KEY)
        run = manifest.victim_config.get("interleaved_run")
        if (not manifest.submission_enabled or manifest.evaluator_id != MESSAGE_EVALUATOR_ID
                or not isinstance(config, Mapping) or not isinstance(run, Mapping)
                or "secret_message" not in run):
            raise ValueError("manifest does not enable this evaluator")
        message_bits(run["secret_message"])
        self._expected = run["secret_message"].encode("ascii")
        self._max_submissions = config["max_submissions"]
        self._attempts = 0
        self._lock = Lock()

    @property
    def attempts_remaining(self) -> int:
        with self._lock:
            return self._max_submissions - self._attempts

    def submit(self, document: Mapping[str, object]) -> MessageSubmissionResult:
        if not isinstance(document, Mapping) or set(document) != {"message"}:
            raise SubmissionRejected("a submission needs exactly a message")
        message = document["message"]
        if type(message) is not str or not message.isascii() or len(message) > 256:
            raise SubmissionRejected("message must be ASCII text of at most 256 characters")
        with self._lock:
            if self._attempts >= self._max_submissions:
                raise SubmissionRejected("no submissions remain")
            self._attempts += 1
            attempt = self._attempts
        passed = hmac.compare_digest(message.encode("ascii"), self._expected)
        return MessageSubmissionResult(attempt, self._max_submissions - attempt, passed)


def challenge_evaluator(manifest: ResolvedScenarioManifest, limits: ProductAssemblyLimits):
    """The evaluator a challenge manifest names; unknown IDs are refused."""
    if manifest.evaluator_id == EVALUATOR_ID:
        return HiddenCaseEvaluator(manifest, limits)
    if manifest.evaluator_id == MESSAGE_EVALUATOR_ID:
        return MessageExactEvaluator(manifest)
    raise ValueError("unsupported challenge evaluator")


def _bit_program(bit: int) -> CircuitProgram:
    return CircuitProgram.from_editor_json(json.dumps({
        "logical_qubits": 1, "initial_states": [bit], "classical_bits": 1,
        "columns": [{"step": 0, "gates": [{"type": "MEASURE", "targets": [0], "classical_targets": [0]}]}],
    }))


def _parse_estimator(raw: Mapping[str, object]) -> tuple[float, bool]:
    if not isinstance(raw, Mapping) or set(raw) != {"threshold", "direction"}:
        raise SubmissionRejected("an estimator needs exactly a threshold and a direction")
    threshold, direction = raw["threshold"], raw["direction"]
    if type(threshold) not in (int, float) or not 0.0 <= float(threshold) <= 1.0:
        raise SubmissionRejected("threshold must be a number between 0 and 1")
    if direction not in ("ge", "le"):
        raise SubmissionRejected("direction must be ge or le")
    return float(threshold), direction == "ge"

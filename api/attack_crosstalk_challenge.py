"""Crosstalk-Grover challenge: server-owned preset and the disruption evaluator.

The preset only selects what the substrate already offers: dedicated regions that run side by side
(concurrent sharing), one declared leak strength, and a victim that repeats a four-qubit Grover search on a
public schedule. The attacker holds the ordinary job operations of any tenant. Grading lives here, outside
the substrate: after the victim's runs have finished, the evaluator compares the victim's success with the
success of the same program run without any attacker, and passes the submission when the drop is both
statistically significant and large enough.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
import json
import math
from typing import Any

from api.attack_challenge import _thaw, _unique
from api.attack_device_snapshot import DeviceSnapshot, deterministic_connected_region, load_ibm_kingston_snapshot
from api.attack_product_factory import (
    IDLE_INPUT_CALIBRATION, ProductAssemblyLimits, _parse_frozen_inputs, build_service_stack,
)
from api.attack_product_profiles import MAX_RETAINED_QUBITS, SERVICE_OPERATIONS, _profile, _topology
from api.attack_settings_resolver import ChallengeDefinition
from core.attack_simulation.concurrency_policy import BOTH, OVERLAP_DISJOINT_REGIONS
from core.attack_simulation.grover_workload import periodic_grover_plan, plan_parameters
from core.attack_simulation.placement import PlacementPolicy
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.scenario_assumptions import AssumptionBasis, AssumptionRecord
from core.attack_simulation.scenario_compiler import ResolvedScenarioManifest
from core.attack_simulation.scenario_policy import ScenarioMode, ScenarioPolicy, ScenarioPreset, ScenarioTemplate
from core.attack_simulation.workload_config import parse_workload_plan, serialize_workload_plan

PRESET_ID = "crosstalk-grover"
TEMPLATE_ID = "crosstalk_grover_challenge"
EVALUATOR_ID = "grover-disruption-v1"
EVALUATION_KEY = "evaluation"


@dataclass(frozen=True)
class CrosstalkGroverSettings:
    """The preset's declared values. The leak strength and the thresholds come from measurement.

    ``drive_crosstalk_strength`` is the challenge's difficulty value, a sample (the calibration data has no such
    number). With the product's detuned leak, a continuous drive over the whole victim run lowered the success of
    the two-iteration search from about 0.68 to about 0.28 at 0.25, while 0.15 left it within two standard
    deviations of the baseline (scripts/experiment_crosstalk_grover.py, 200 victim shots).
    """

    victim_shots: int = 50
    victim_visits: int = 8
    first_run_us: float = 30_000_000.0
    pause_between_runs_us: float = 10_000_000.0
    drive_crosstalk_strength: float = 0.25
    max_submissions: int = 3
    baseline_shots: int = 400
    significance_z: float = 3.0
    minimum_drop: float = 0.15


def crosstalk_grover_challenge(snapshot: DeviceSnapshot | None = None,
                               settings: CrosstalkGroverSettings = CrosstalkGroverSettings()) -> ChallengeDefinition:
    snapshot = load_ibm_kingston_snapshot() if snapshot is None else snapshot
    region = tuple(sorted(deterministic_connected_region(snapshot, MAX_RETAINED_QUBITS)))
    alternate = tuple(sorted(deterministic_connected_region(
        snapshot, MAX_RETAINED_QUBITS, excluded_qubit_ids=frozenset(region))))
    base = _profile(snapshot, _topology(snapshot), region, IDLE_INPUT_CALIBRATION, alternate,
                    PlacementPolicy.TENANT_DEDICATED_REGION.value)

    def build() -> ScenarioPreset:
        return _preset(base, region, snapshot, settings)

    return ChallengeDefinition(
        PRESET_ID, "Crosstalk against Grover",
        "被害者は、4 qubit・2反復の Grover 探索（16候補）を、結果を受け取るたびに一定の間隔で繰り返し実行します。"
        "あなたの領域は被害者の領域と結合1本だけでつながっていて、あなたの駆動パルスは、あなたのジョブが"
        "被害者のジョブと同じ時刻に実行されている間だけ、その結合の先の被害者の qubit に漏れます。"
        "被害者の領域とあなたの領域は別々に進み、あなたのジョブが被害者を待たせることも、被害者があなたを待たせることもありません。"
        f"被害者の最初の実行は、環境ができてから {settings.first_run_us / 1e6:g} 秒後に始まり、結果が返ってから"
        f"{settings.pause_between_runs_us / 1e6:g} 秒おいて、合計 {settings.victim_visits} 回行われます"
        f"（1回 {settings.victim_shots} shot）。被害者の実行をあなたのジョブの時刻に合わせて重ね、被害者の成功確率を"
        "攻撃のない場合から下げてください。すべての実行が終わってから提出すると、攻撃のない同じ被害者との差が、"
        "統計的に有意で、かつ十分大きいかどうかで採点されます。"
        f"提出は {settings.max_submissions} 回までで、差の大きさは返りません。",
        preset_factory=build,
        not_reproduced=("被害者の周期的な Grover 実行", "採点と提出"),
    )


def _preset(base, region: tuple[int, ...], snapshot: DeviceSnapshot, settings: CrosstalkGroverSettings) -> ScenarioPreset:
    template = base.template
    qpu = _thaw(template.qpu_service_config)
    qpu["abstract_commands"] = {"interleaved_observation": False, "victim_activity": False}
    qpu["supported_operations"] = list(SERVICE_OPERATIONS)
    execution = qpu["runtime_execution"]
    execution["concurrency_policy"] = OVERLAP_DISJOINT_REGIONS
    execution["drive_crosstalk_strength"] = settings.drive_crosstalk_strength
    execution["drive_crosstalk_direction"] = BOTH
    edges = qpu["topology"]["gate_edges"]
    clock_domain = _thaw(template.experiment_config)["world_clock"]["clock_domain"]
    # The same victim a free-mode author gets by enabling the periodic Grover setting with these values.
    schedule = {"enabled": True, "runs": settings.victim_visits, "shots_per_run": settings.victim_shots,
                "first_run_us": settings.first_run_us, "pause_between_runs_us": settings.pause_between_runs_us}
    plan, expected = periodic_grover_plan(
        region=region, edges=edges, clock_domain=clock_domain, device_id=snapshot.device_id,
        **plan_parameters(schedule))
    attacker = _thaw(template.attacker_config)
    attacker.pop("interleaved_access", None)
    attacker["operation_grants"] = list(SERVICE_OPERATIONS)
    victim = _thaw(template.victim_config)
    victim.pop("interleaved_run", None)
    victim["operation_grants"] = list(SERVICE_OPERATIONS)
    victim["periodic_grover"] = schedule
    victim["workload"] = serialize_workload_plan(plan)
    experiment = _thaw(template.experiment_config)
    experiment[EVALUATION_KEY] = {
        "expected_bitstring": expected, "max_submissions": settings.max_submissions,
        "baseline_shots": settings.baseline_shots, "significance_z": settings.significance_z,
        "minimum_drop": settings.minimum_drop,
    }
    challenge_template = ScenarioTemplate(
        TEMPLATE_ID, "1", attacker, victim, qpu, _thaw(template.relation_config), experiment)
    locked = tuple(
        f"{root}.{key}"
        for root, section in (("attacker", attacker), ("victim", victim), ("qpu_service", qpu),
                              ("experiment", experiment), ("relation", template.relation_config))
        for key in section)
    policy = ScenarioPolicy(
        ScenarioMode.CHALLENGE,
        locked_fields=locked,
        hidden_fields=("victim.workload", "victim.periodic_grover", f"experiment.{EVALUATION_KEY}"),
        evaluator_id=EVALUATOR_ID,
        submission_enabled=True,
        allowed_operations=SERVICE_OPERATIONS,
    )
    # The base profile also documents the interleaved observation, which this challenge does not have.
    removed = ("attacker.interleaved_access", "victim.interleaved_run")
    records = tuple(record for record in base.assumptions
                    if not any(path.startswith(removed) for path in record.target_paths))
    records += (
        AssumptionRecord(
            "challenge_victim_workload", ("victim.workload",), AssumptionBasis.SIMULATOR_CHOICE,
            "The victim repeats a four-qubit, two-iteration Grover search, routed onto its region with relative-phase "
            "Toffoli gates and SWAPs because the service has no multi-controlled gate and runs CNOT only on coupled "
            "pairs. The schedule is declared and public; the marked item is an owner-chosen challenge value."),
        AssumptionRecord(
            "challenge_concurrent_sharing", ("qpu_service.runtime_execution.concurrency_policy",
                                             "qpu_service.runtime_execution.drive_crosstalk_strength"),
            AssumptionBasis.SIMULATOR_CHOICE,
            f"The two regions run side by side and the leak strength {settings.drive_crosstalk_strength} is the "
            "challenge's difficulty value, a sample chosen from simulation: a continuous drive over a whole victim "
            "run roughly halves the search's success, a drive over a small part of it hardly changes it."),
        AssumptionRecord(
            "challenge_grants", ("attacker.operation_grants", "victim.operation_grants",
                                 "qpu_service.supported_operations"), AssumptionBasis.SIMULATOR_CHOICE,
            "Both tenants hold the ordinary job operations of any tenant. The attacker has no operation that "
            "reads the victim, and no budget beyond the service's job, shot and instruction limits."),
        AssumptionRecord(
            "challenge_evaluation", (f"experiment.{EVALUATION_KEY}",), AssumptionBasis.SIMULATOR_CHOICE,
            f"A submission passes when the victim's success is lower than that of the same program run without an "
            f"attacker by at least {settings.minimum_drop} and by more than {settings.significance_z} standard "
            "deviations; both values were set from the measured spread of the baseline."),
    )
    return ScenarioPreset(challenge_template, policy, _unique(records))


class DisruptionResult:
    __slots__ = ("attempt", "attempts_remaining", "passed", "ready")

    def __init__(self, attempt: int, attempts_remaining: int, passed: bool, ready: bool = True) -> None:
        self.attempt, self.attempts_remaining, self.passed, self.ready = attempt, attempts_remaining, passed, ready

    def to_public_dict(self) -> dict[str, object]:
        # Whether the victim's runs have finished is public (the schedule is); the size of the drop is not.
        return {"attempt": self.attempt, "attempts_remaining": self.attempts_remaining,
                "ready": self.ready, "passed": self.passed}


def disrupted(attacked: int, attacked_shots: int, baseline: int, baseline_shots: int,
              *, significance_z: float, minimum_drop: float) -> bool:
    """One-sided comparison of two success fractions: lower by enough, and by more than noise."""
    p_attack, p_base = attacked / attacked_shots, baseline / baseline_shots
    variance = p_attack * (1 - p_attack) / attacked_shots + p_base * (1 - p_base) / baseline_shots
    drop = p_base - p_attack
    if variance == 0.0:
        return drop >= minimum_drop
    return drop >= minimum_drop and drop / math.sqrt(variance) >= significance_z


class CrosstalkGroverEvaluator:
    """Grade a finished run: the victim's success with the attacker present against the same victim alone.

    ``observe`` returns the victim's finished Grover runs (label counts), read by the supervisor from the live
    world. The baseline is measured once, with the same frozen manifest and no attacker, and kept.
    """

    def __init__(self, manifest: ResolvedScenarioManifest, limits: ProductAssemblyLimits,
                 observe: Callable[[], tuple[Mapping[str, int], ...]],
                 *, measure_baseline: Callable[[], tuple[int, int]] | None = None) -> None:
        config = manifest.experiment_config.get(EVALUATION_KEY)
        workload = manifest.victim_config.get("workload")
        if not manifest.submission_enabled or manifest.evaluator_id != EVALUATOR_ID or config is None or workload is None:
            raise ValueError("manifest does not enable this evaluator")
        self._manifest, self._limits, self._observe = manifest, limits, observe
        self._plan = parse_workload_plan(workload)
        self._expected = config["expected_bitstring"]
        self._max_submissions = config["max_submissions"]
        self._baseline_shots = config["baseline_shots"]
        self._z, self._minimum_drop = config["significance_z"], config["minimum_drop"]
        self._runs = self._plan.steps[0].max_visits
        self._measure_baseline = measure_baseline or self._run_baseline
        self._baseline: tuple[int, int] | None = None
        self._attempts = 0
        self._lock = Lock()

    @property
    def attempts_remaining(self) -> int:
        with self._lock:
            return self._max_submissions - self._attempts

    def submit(self, document: Mapping[str, object]) -> DisruptionResult:
        from api.attack_challenge import EvaluationUnavailable, SubmissionRejected
        if not isinstance(document, Mapping) or document:
            raise SubmissionRejected("a submission is an empty object")
        with self._lock:
            if self._attempts >= self._max_submissions:
                raise SubmissionRejected("no submissions remain")
            try:
                runs = self._observe()
                if len(runs) < self._runs:
                    return DisruptionResult(self._attempts, self._max_submissions - self._attempts, False, False)
                if self._baseline is None:
                    self._baseline = self._measure_baseline()
                attacked = sum(int(run.get(self._expected, 0)) for run in runs)
                shots = sum(int(sum(run.values())) for run in runs)
                passed = disrupted(attacked, shots, *self._baseline, significance_z=self._z,
                                   minimum_drop=self._minimum_drop)
            except Exception as error:
                raise EvaluationUnavailable("evaluation is unavailable") from error
            self._attempts += 1
            return DisruptionResult(self._attempts, self._max_submissions - self._attempts, passed)

    def _run_baseline(self) -> tuple[int, int]:
        """Successes and shots of the victim's program alone, in a fresh environment from the same manifest."""
        parsed = _parse_frozen_inputs(self._manifest, self._limits)
        runtime, service = build_service_stack(parsed, self._manifest)
        step = self._plan.steps[0]
        program = self._plan.programs[0].program
        device = parsed["snapshot"].qpu_id
        successes = shots = 0
        index = 0
        while shots < self._baseline_shots:
            service.submit_program(self._plan.actor_id, ProgramSubmitRequest(
                program, device, step.shots, f"baseline-{index}", step.initialization))
            done = runtime.run_next()
            if done is None or done.result is None:
                raise RuntimeError("baseline run did not complete")
            successes += int(done.result["counts"].get(self._expected, 0))
            shots += step.shots
            index += 1
        return successes, shots

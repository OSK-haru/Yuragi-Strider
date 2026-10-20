"""A victim that repeats a routed Grover search on a declared schedule (a workload plan).

Shared by the free-mode setting and the crosstalk challenge, so loading the challenge's values into free mode
builds exactly the same victim. The marked item and the iteration count are fixed; the schedule is declared.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from core.attack_simulation.grover_victim import grover_circuit
from core.attack_simulation.program_request import CircuitProgram
from core.attack_simulation.victim_workload import (
    Branch, DeclaredDurations, Outcome, RegisteredProgram, TimedStart, WorkloadPlan, WorkloadStep,
)

MARKED_ITEM = "1011"
ITERATIONS = 2
PROGRAM_ID = "grover"
STEP_ID = "grover"
PLAN_ID = "periodic-grover-victim"
MAX_SHOTS_PER_RUN = 59            # the routed two-iteration circuit under the service's 32768 instruction-work limit


def periodic_grover_plan(*, region: Sequence[int], edges: Sequence[Sequence[int]], clock_domain: str, device_id: str,
                         shots: int, runs: int, first_run_us: float, pause_us: float,
                         actor_id: str = "victim") -> tuple[WorkloadPlan, str]:
    """The plan and the bitstring a correct run measures most often.

    A step waits its ``classical_us`` before submitting, so the first run is submitted at the start time plus
    that pause; the start is set back by the pause to put the first run at ``first_run_us``.
    """
    if first_run_us < pause_us:
        raise ValueError("the first run cannot come before the pause that precedes every run")
    document, expected = grover_circuit(MARKED_ITEM, ITERATIONS, region, edges)
    program = RegisteredProgram(PROGRAM_ID, CircuitProgram.from_editor_json(json.dumps(document)))
    step = WorkloadStep(STEP_ID, PROGRAM_ID, device_id, shots, DeclaredDurations(pause_us, 0.0, 0.0), runs,
                        (Branch(Outcome.SUCCEEDED, STEP_ID),))
    plan = WorkloadPlan(PLAN_ID, actor_id, clock_domain, (program,), (step,),
                        (TimedStart(STEP_ID, first_run_us - pause_us),), runs, 1)
    return plan, expected


def plan_parameters(config: Mapping[str, object]) -> dict[str, object]:
    """Validated schedule values from a ``periodic_grover`` block."""
    shots, runs = config["shots_per_run"], config["runs"]
    if type(shots) is not int or not 1 <= shots <= MAX_SHOTS_PER_RUN:
        raise ValueError(f"shots_per_run must be an integer from 1 to {MAX_SHOTS_PER_RUN}")
    if type(runs) is not int or not 1 <= runs <= 64:
        raise ValueError("runs must be an integer from 1 to 64")
    return {"shots": shots, "runs": runs, "first_run_us": float(config["first_run_us"]),
            "pause_us": float(config["pause_between_runs_us"])}

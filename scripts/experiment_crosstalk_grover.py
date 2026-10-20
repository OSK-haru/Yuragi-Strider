"""How much a continuous attacker drive lowers the victim's Grover success, through the product stack.

The environment is the product's free-mode profile with dedicated regions and overlapping jobs. The victim
runs a four-qubit, two-iteration Grover search routed onto its region; the attacker runs a train of X
pulses on its first qubit, which sits next to the victim's region (coupling edge 5-6). Both jobs are
registered with overlapping windows and run through the declared executor. The result is the victim's
success fraction with the attack and without it, for several leak strengths.

    python -m scripts.experiment_crosstalk_grover [--shots 60] [--strengths 0.02,0.1,0.3]
"""

from __future__ import annotations

import argparse
import json
import math
import time

from api.attack_device_snapshot import load_ibm_kingston_snapshot
from api.attack_product_factory import (
    ProductAssemblyLimits, _parse_frozen_inputs, build_service_stack, concurrent_regions_for,
)
from api.attack_product_profiles import product_free_scenarios
from api.attack_sessions import SessionHostLimits
from api.attack_settings_resolver import ScenarioSettingsResolver
from core.attack_simulation.declared_execution import DeclaredQpuExecutor
from core.attack_simulation.declared_service_timing import DeclaredServiceTiming
from core.attack_simulation.event_commit_boundary import EventPosition
from core.attack_simulation.grover_victim import grover_circuit
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.qpu_topology import TopologyLimits
from core.attack_simulation.request_receipts import ReceiptLimits

PREFIX = "qpu_service.runtime_execution."
LIMITS = ProductAssemblyLimits(
    session=SessionHostLimits(16384, 4, ReceiptLimits(16, 64), max_queued_jobs=8),
    topology=TopologyLimits(256, 2048, 4096),
    max_runtime_jobs=160, max_events_per_job=1100, max_unresolved_events=32,
    max_workload_pending=16, max_workload_history=64, retained_jobs=128,
)
WORLD = "world"
MARKED = "1011"


def pulse_train(pulses: int) -> dict:
    gates = [{"step": step, "gates": [{"type": "X", "targets": [0]}]} for step in range(pulses)]
    gates.append({"step": pulses, "gates": [{"type": "MEASURE", "targets": [0], "classical_targets": [0]}]})
    return {"logical_qubits": 1, "initial_states": [0], "classical_bits": 1, "columns": gates}


def run(strength: float, attack: bool, *, victim_shots: int, pulses: int, attacker_shots: int,
        attacker_jobs: int, direction: str = "both", victim_jobs: int = 1) -> dict[str, float]:
    resolver = ScenarioSettingsResolver(free_scenarios=product_free_scenarios(load_ibm_kingston_snapshot()))
    settings = {PREFIX + "placement_policy": "tenant_dedicated_region",
                PREFIX + "concurrency_policy": "overlap_disjoint_regions",
                PREFIX + "drive_crosstalk_strength": strength, PREFIX + "drive_crosstalk_direction": direction}
    environment = resolver.resolve(json.dumps(
        {"mode": "free", "idle_noise_mode": "calibration_values", "settings": settings}).encode())
    manifest = environment.establish()
    parsed = _parse_frozen_inputs(manifest, LIMITS)
    runtime, service = build_service_stack(parsed, manifest)
    executor = DeclaredQpuExecutor(
        runtime=runtime, clock_domain=WORLD, max_jobs=64, max_events_per_job=1100, max_unresolved=32,
        idle_model=parsed["idle_model"], concurrent_regions=concurrent_regions_for(parsed))
    timing = DeclaredServiceTiming(WORLD, 0.0, 0.0, 0.0)
    region = parsed["region"].physical_qubit_ids
    edges = parsed["region"].topology.gate_edges
    document, expected = grover_circuit(MARKED, 2, region, edges)

    def submit(principal, circuit, shots, key):
        request = ProgramSubmitRequest(CircuitProgram.from_editor_json(json.dumps(circuit)),
                                       "ibm_kingston", shots, key, "prepare")
        return service.submit_program(principal, request).job_id

    victims = [submit("victim", document, victim_shots, f"grover-{index}") for index in range(victim_jobs)]
    victim = victims[0]
    jobs = [("victim", job_id, 100.0) for job_id in victims]
    if attack:
        jobs += [("attacker", submit("attacker", pulse_train(pulses), attacker_shots, f"drive-{index}"), 0.0)
                 for index in range(attacker_jobs)]
    arrival = {}
    order = 0
    for tenant, job_id, at in jobs:
        order += 1
        arrival[job_id] = EventPosition(WORLD, at, order)
    anchors = {}
    for tenant, job_id, at in jobs:
        job = runtime.scheduler.get(job_id)
        key = executor.lane_key(job_id)
        previous, owner = anchors.get(key) or executor.plan_anchor(key)
        window = timing.plan_job_window(job, runtime.config, previous_finish=previous, previous_tenant_id=owner,
                                        arrival=arrival[job_id], taken=executor.taken_keys(job_id))
        executor.register_job(job_id, window)
        anchors[key] = window.finish, job.command.tenant_id
    horizon = EventPosition(WORLD, 1e9, 99)
    executor.frontier.set_target(horizon)
    executor.frontier.seal_ingress(horizon)
    started = time.perf_counter()
    while (step := executor.next_physical()) is not None:
        if step[0] == "start":
            executor.begin_next()
        else:
            executor.commit(executor.prepare_next())
    totals: dict[str, int] = {}
    for job_id in victims:
        for label, count in runtime.scheduler.get(job_id).result["counts"].items():
            totals[label] = totals.get(label, 0) + count
    windows = {job_id: (executor._windows[job_id].start.time_us, executor._windows[job_id].finish.time_us)
               for _, job_id, _ in jobs}
    success = totals.get(expected, 0) / sum(totals.values())
    return {"success": success, "shots": sum(totals.values()),
            "victim_window_us": (windows[victims[0]][0], windows[victims[-1]][1]), "seconds": time.perf_counter() - started,
            "attacker_windows": [windows[j] for t, j, _ in jobs if t == "attacker"]}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shots", type=int, default=60)
    parser.add_argument("--pulses", type=int, default=150)
    parser.add_argument("--attacker-shots", type=int, default=200)
    parser.add_argument("--attacker-jobs", type=int, default=1)
    parser.add_argument("--strengths", default="0.02,0.1,0.3")
    parser.add_argument("--victim-jobs", type=int, default=1)
    args = parser.parse_args()
    base = run(0.02, False, victim_shots=args.shots, pulses=args.pulses, attacker_shots=args.attacker_shots,
               attacker_jobs=args.attacker_jobs, victim_jobs=args.victim_jobs)
    print("baseline", base)
    for text in args.strengths.split(","):
        strength = float(text)
        result = run(strength, True, victim_shots=args.shots, pulses=args.pulses,
                     attacker_shots=args.attacker_shots, attacker_jobs=args.attacker_jobs,
                     victim_jobs=args.victim_jobs)
        sigma = math.sqrt(base["success"] * (1 - base["success"]) / base["shots"])
        print(f"strength {strength}: success {result['success']:.3f} (baseline {base['success']:.3f}, 1 sigma {sigma:.3f}) "
              f"victim window {result['victim_window_us']} attacker {result['attacker_windows']} {result['seconds']:.1f}s")


if __name__ == "__main__":
    main()

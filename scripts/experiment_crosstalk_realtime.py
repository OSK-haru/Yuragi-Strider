"""Can an attacker line a job up with the victim's Grover window on the real (host) clock?

The crosstalk-Grover preset runs on the product's host-clock world. The victim's window is about 1.3 ms and the
attacker can only choose *when it submits*; the job's window starts at the receipt. This script plays the real
preset (shortened schedule) in-process and, for each victim run, submits the attacker's pulse train at a chosen
offset from the predicted start of that run. It reports, per offset, whether the windows overlapped and how much
of the victim run was covered. That gives the half-width of the timing the attacker has to hit; compare it with
the jitter of the path the attacker really has (browser -> websocket -> server).

    python -m scripts.experiment_crosstalk_realtime [--offsets=-3,-2,-1,0,1] [--visits 10]
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import time

from api.attack_crosstalk_challenge import PRESET_ID, CrosstalkGroverSettings, crosstalk_grover_challenge
from api.attack_device_snapshot import load_ibm_kingston_snapshot
from api.attack_product_factory import ProductAssemblyLimits, ProductEnvironmentFactory
from api.attack_product_profiles import product_free_scenarios
from api.attack_sessions import SessionHostLimits, WebOwnerContext
from api.attack_settings_resolver import ScenarioSettingsResolver
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest
from core.attack_simulation.qpu_topology import TopologyLimits
from core.attack_simulation.request_receipts import ReceiptLimits

LIMITS = ProductAssemblyLimits(
    session=SessionHostLimits(16384, 4, ReceiptLimits(16, 64), max_queued_jobs=8),
    topology=TopologyLimits(256, 2048, 4096),
    max_runtime_jobs=160, max_events_per_job=1100, max_unresolved_events=32,
    max_workload_pending=16, max_workload_history=64, retained_jobs=128,
)
SNAPSHOT = load_ibm_kingston_snapshot()


def pulse_train(pulses: int) -> CircuitProgram:
    columns = [{"step": step, "gates": [{"type": "X", "targets": [0]}]} for step in range(pulses)]
    columns.append({"step": pulses, "gates": [{"type": "MEASURE", "targets": [0], "classical_targets": [0]}]})
    return CircuitProgram.from_editor_json(json.dumps(
        {"logical_qubits": 1, "initial_states": [0], "classical_bits": 1, "columns": columns}))


def assemble(settings: CrosstalkGroverSettings):
    resolver = ScenarioSettingsResolver(free_scenarios=product_free_scenarios(SNAPSHOT),
                                        challenges=(crosstalk_grover_challenge(SNAPSHOT, settings),))
    environment = resolver.resolve(json.dumps({"mode": "challenge", "preset_id": PRESET_ID, "settings": {}}).encode())
    owner = WebOwnerContext()
    return ProductEnvironmentFactory(LIMITS).assemble(environment, owner), owner


def run(settings: CrosstalkGroverSettings, offsets_ms: list[float | None], gap_us: float, pulses: int, shots: int,
        burst: int = 1):
    """Offsets: one per victim run (None = no attack). ``gap_us`` is the learned finish-to-next-start distance."""
    product, owner = assemble(settings)
    try:
        controller, credential = product.controller, product.attacker_credential
        world = controller._world
        executor = world._executor
        attacker_jobs: list[str] = []
        attacker_tenant_id = None
        windows: list[tuple[float, float]] = []
        # Predict run k's start: run 0 at first_run_us, later ones at the previous finish + gap.
        predicted = settings.first_run_us
        for index, offset in enumerate(offsets_ms):
            if index > 0:
                predicted = windows[index - 1][1] + gap_us
            if offset is not None:
                target = predicted + offset * 1000.0
                lead = 0.02                                             # stop sleeping 20 ms before the target
                while controller.now() < target - lead * 1e6:
                    time.sleep(0.005)
                while controller.now() < target:
                    pass
                request = ProgramSubmitRequest(pulse_train(pulses), "ibm_kingston", shots, f"drive-{index}")
                before = controller.now()
                for part in range(burst):
                    request = ProgramSubmitRequest(pulse_train(pulses), "ibm_kingston", shots, f"drive-{index}-{part}")
                    attacker_jobs.append(controller.submit(credential, request, owner_context=owner))
                print(f"  submit {index}: target {target:.0f} began {before:.0f} returned {controller.now():.0f}")
            # Wait for the victim's run `index` to be registered and finished.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                windows = _victim_windows(executor, attacker_jobs)
                if len(windows) > index and controller.now() > windows[index][1] + 5_000:
                    break
                time.sleep(0.01)
        counts = [dict(job.result["counts"]) for job in _victim_jobs(executor, attacker_jobs)
                  if job.result is not None]
        attacked = _attacker_windows(executor, attacker_jobs)
        return windows, attacked, counts, predicted
    finally:
        product.close()


def _jobs(executor, attacker: bool):
    jobs = sorted(executor.runtime.scheduler._jobs.values(), key=lambda job: job.acceptance_sequence)
    return [job for job in jobs if job.command.idempotency_key.startswith("drive-") == attacker]


def _victim_jobs(executor, receipts):
    return _jobs(executor, False)


def _victim_windows(executor, receipts):
    return [(executor._windows[job.job_id].start.time_us, executor._windows[job.job_id].finish.time_us)
            for job in _jobs(executor, False) if job.job_id in executor._windows]


def _attacker_windows(executor, receipts):
    return [(executor._windows[job.job_id].start.time_us, executor._windows[job.job_id].finish.time_us)
            for job in _jobs(executor, True) if job.job_id in executor._windows]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offsets", default="-3,-2,-1,0,1,2")
    parser.add_argument("--visits", type=int, default=0)
    parser.add_argument("--pulses", type=int, default=150)
    parser.add_argument("--shots", type=int, default=200)
    parser.add_argument("--burst", type=int, default=1)
    parser.add_argument("--pause-s", type=float, default=10.0)
    args = parser.parse_args()
    offsets = [float(text) for text in args.offsets.split(",")]
    visits = args.visits or len(offsets) + 2
    settings = replace(CrosstalkGroverSettings(), first_run_us=max(3_000_000.0, args.pause_s * 1e6 + 2_000_000.0), pause_between_runs_us=args.pause_s * 1e6,
                       victim_visits=visits)
    # Dry run without an attacker: learn the victim's window length and the finish-to-start distance.
    windows, _, counts, _ = run(settings, [None] * visits, 0.0, args.pulses, args.shots)
    gaps = [windows[i + 1][0] - windows[i][1] for i in range(len(windows) - 1)]
    print("victim windows (us):", [(round(a, 1), round(b - a, 1)) for a, b in windows])
    gap = sum(gaps) / len(gaps)
    print(f"finish-to-start gap: mean {gap:.1f} us, spread {max(gaps) - min(gaps):.1f} us")
    plan = [None] + offsets + [None] * (visits - 1 - len(offsets))
    windows, attacker, counts, _ = run(settings, plan, gap, args.pulses, args.shots, args.burst)
    print("attacker windows:", [(round(a, 1), round(b - a, 1)) for a, b in attacker])
    index = 0
    for run_index, offset in enumerate(plan):
        start, finish = windows[run_index]
        if offset is None:
            print(f"run {run_index}: no attack; success {counts[run_index].get('1011', 0) / sum(counts[run_index].values()):.2f}")
            continue
        group = attacker[index:index + args.burst]
        index += args.burst
        a_start, a_finish = group[0][0], group[-1][1]
        lag = a_start - (start + offset * 1000.0)
        covered = sum(max(0.0, min(finish, f) - max(start, a)) for a, f in group) / (finish - start)
        success = counts[run_index].get("1011", 0) / sum(counts[run_index].values())
        print(f"run {run_index}: offset {offset:+.1f} ms -> attacker starts {(a_start - start) / 1000:+.2f} ms from victim start; "
              f"covers {covered:.0%} of victim; lag after target {lag / 1000:+.2f} ms; success {success:.2f}")


if __name__ == "__main__":
    main()

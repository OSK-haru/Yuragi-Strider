"""Switch leakage channels on and off one at a time: residual ZZ, drive crosstalk, reset residue.

The ZZ is either a measured value (ZZ_KHZ) or follows from device values (J_MHZ, FREQUENCY_GHZ,
ANHARMONICITY_MHZ; D91). Only the static shift of the exchange coupling is in the runtime so far: the
drive it passes to the neighbour and the third level are not.

Two dedicated 2-qubit regions: V (physical 0,1) and A (physical 2,3). The coupled pair is 1 -- 2.

  victim   on V, starts VICTIM_START_US: X on physical 1 only if secret == 1 -> idle VICTIM_IDLE_US -> measure
  probe    on A, starts PROBE_START_US:  H on physical 2 -> wait WAIT_US -> H -> measure      (ZZ / crosstalk)
  takeover on V, TAKEOVER_GAP_US after the victim finishes: measure physical 1 right after the
           handover reset and its own initializing reset                                  (reset residue)

Both attacker jobs belong to the attacker. Output per condition and per attacker job:
P(1 | secret = 0), P(1 | secret = 1), and their difference (0 = cannot tell the secret apart).

Run from the repo root:  python scripts/research/leak_channels.py [shots]
Change only the numbers in the PARAMETERS block.
"""

import itertools
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.attack_simulation.coupled_regions import CoupledTransmonSettings, ResidualZZSettings  # noqa: E402
from core.attack_simulation.jobs import JobCommand  # noqa: E402
from core.attack_simulation.resource_binding import PhysicalQubitLayout  # noqa: E402
from core.experimental_isolation_physics.coherent_crosstalk import (  # noqa: E402
    DriveCrosstalkTransfer, ResidualZZCoupling,
)
from core.experimental_isolation_physics.transmon_cluster import TransmonCluster, TransmonSpec  # noqa: E402
from core.pulse_transmon_network import TransmonExchangeCoupling  # noqa: E402
from tests.test_coupled_regions import build, operation, register  # noqa: E402
from tests.test_concurrent_regions import run_in_order  # noqa: E402

# ---------------- PARAMETERS ----------------
ZZ_KHZ = 0.9               # residual ZZ zeta/2pi in kHz (H = zeta * n_1 * n_2), used when J_MHZ is None
J_MHZ = None               # exchange J/2pi in MHz between 1 and 2; when set, the ZZ follows from the device
                           # values below instead of ZZ_KHZ (D91)
FREQUENCY_GHZ = (5.10, 5.00)          # measured 0-1 frequencies of physical 1 and 2
ANHARMONICITY_MHZ = (-330.0, -310.0)  # measured f12 - f01 of physical 1 and 2
CROSSTALK = 0.05           # drive-crosstalk transfer 1 -> 2, real part (leaked drive along the same axis)
CROSSTALK_IMAG = 0.0       # imaginary part (leaked drive turned by 90 degrees); |real + i imag| <= 1
CROSSTALK_DETUNING_MHZ = 0.0   # 0 = resonant leak; the product default is 100 MHz
RESET_SUCCESS = 0.9        # success probability of one reset when the residue channel is on (off = 1.0)
RESET_US = 1.0             # duration of one reset (initializing and handover)
READOUT_DEPHASING_PER_US = None   # measurement-induced dephasing in a readout (1/us); None = strong-measurement
                                  # (Zeno) limit: a leak during a readout never changes the result
MEASUREMENT_US = 1.0       # a reset's measurement part (the rest is feedback before the conditional X), and
                           # the final measurement's duration; a leak in the feedback part survives the reset
T1_US = 100.0              # same T1 / T_phi on every qubit (math.inf = no relaxation)
TPHI_US = 150.0
PROBE_START_US = 1.0
WAIT_US = 60.0             # probe's Ramsey wait
VICTIM_START_US = 3.0      # the victim's X lands inside the probe's wait if this is < start + WAIT_US
VICTIM_IDLE_US = 10.0
TAKEOVER_GAP_US = 0.5      # time between the victim's finish and the takeover's start
SHOTS = int(sys.argv[1]) if len(sys.argv) > 1 else 400   # repetitions per (condition, secret)
# --------------------------------------------

V, A = (0, 1), (2, 3)


def device_specs():
    return tuple(TransmonSpec(frequency, anharmonicity)
                 for frequency, anharmonicity in zip(FREQUENCY_GHZ, ANHARMONICITY_MHZ))


def device_summary():
    """What the device values imply: the ZZ, and how strongly 1's control line drives 2 through J alone."""
    cluster = TransmonCluster(device_specs(), (TransmonExchangeCoupling(0, 1, 2 * math.pi * J_MHZ),))
    zz_khz = 1e3 * cluster.zz_rad_per_us(0, 1) / (2 * math.pi)
    return (f"J={J_MHZ} MHz -> ZZ={zz_khz:.3f} kHz; a drive on 1's line at 2's frequency turns 2 at "
            f"{cluster.drive_element(0, 1, (0, 0)).real:+.4f} (1 in |0>) / "
            f"{cluster.drive_element(0, 1, (1, 0)).real:+.4f} (1 in |1>) of its Rabi rate")


def job(runtime, tenant, physical, columns, key, seed):
    op = operation(columns, physical, seed=seed)
    return runtime.submit(JobCommand(tenant, op, key, layout=PhysicalQubitLayout("qpu-1", physical)))


def one_run(use_zz, use_xt, use_residue, secret, seed):
    zz = transmons = None
    if use_zz and J_MHZ is None:
        zz = ResidualZZSettings((ResidualZZCoupling(1, 2, 2 * math.pi * ZZ_KHZ * 1e-3),), model="exact")
    elif use_zz:
        transmons = CoupledTransmonSettings(tuple(zip((1, 2), device_specs())), ((1, 2, 2 * math.pi * J_MHZ),),
                                            model="exact")
    transfers = ([DriveCrosstalkTransfer(1, 2, CROSSTALK, CROSSTALK_IMAG, 2 * math.pi * CROSSTALK_DETUNING_MHZ)]
                 if use_xt else [])
    runtime, executor = build(transfers=transfers, zz=zz, t1=T1_US, tphi=TPHI_US,
                              reset_success=RESET_SUCCESS if use_residue else 1.0,
                              region_access={"attacker": (A, V)},    # the attacker may take over V
                              durations=(RESET_US, MEASUREMENT_US),
                              readout_dephasing=READOUT_DEPHASING_PER_US, transmons=transmons)
    probe = job(runtime, "attacker", A, [[{"type": "H", "targets": [0]}],
                                         [{"type": "I", "targets": [0], "params": {"duration_us": WAIT_US}}],
                                         [{"type": "H", "targets": [0]}]], f"p{seed}", seed)
    victim = job(runtime, "victim", V, ([[{"type": "X", "targets": [1]}]] if secret else []) + [
        [{"type": "I", "targets": [1], "params": {"duration_us": VICTIM_IDLE_US}}]], f"v{seed}", seed + 1)
    takeover = job(runtime, "attacker", (1,), [], f"t{seed}", seed + 2)
    register(executor, probe, PROBE_START_US, 0)
    register(executor, victim, VICTIM_START_US, 4)
    victim_finish = executor._windows[victim.job_id].finish.time_us
    register(executor, takeover, victim_finish + TAKEOVER_GAP_US, 8, previous_tenant="victim")
    run_in_order(executor)

    def ones(record):
        counts = runtime.scheduler.get(record.job_id).result["counts"]
        return sum(n for bits, n in counts.items() if bits[0] == "1")   # character k = program qubit k

    return ones(probe), ones(takeover)


def main():
    if J_MHZ is not None:
        print(device_summary())
    print(f"ZZ={'from J' if J_MHZ is not None else ZZ_KHZ} kHz  crosstalk={CROSSTALK}{CROSSTALK_IMAG:+}i (detuning {CROSSTALK_DETUNING_MHZ} MHz)  "
          f"reset success={RESET_SUCCESS}  T1={T1_US} Tphi={TPHI_US}  wait={WAIT_US} us  shots={SHOTS}")
    print(f"{'ZZ':<4}{'xt':<4}{'res':<5}| {'probe P(1|0)':>12}{'P(1|1)':>8}{'diff':>8} | "
          f"{'takeover P(1|0)':>15}{'P(1|1)':>8}{'diff':>8} | sd")
    for use_zz, use_xt, use_residue in itertools.product((False, True), repeat=3):
        start = time.time()
        totals = []
        for secret in (0, 1):
            runs = [one_run(use_zz, use_xt, use_residue, secret, 10 * k + 7) for k in range(SHOTS)]
            totals.append((sum(r[0] for r in runs) / SHOTS, sum(r[1] for r in runs) / SHOTS))
        (p0, t0), (p1, t1) = totals
        sd = math.sqrt(0.5 / SHOTS)    # worst case for the difference of two proportions
        flag = lambda on: "on" if on else "-"  # noqa: E731
        print(f"{flag(use_zz):<4}{flag(use_xt):<4}{flag(use_residue):<5}| {p0:>12.3f}{p1:>8.3f}{p1 - p0:>8.3f} | "
              f"{t0:>15.3f}{t1:>8.3f}{t1 - t0:>8.3f} | {sd:.3f}  ({time.time() - start:.1f}s)")


if __name__ == "__main__":
    main()

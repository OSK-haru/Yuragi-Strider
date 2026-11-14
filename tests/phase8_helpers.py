from __future__ import annotations

from core.circuit_model import CircuitConfig, GateColumn, GateOperation
from core.results import EnvironmentConfig, SimulationConfig


def environment() -> EnvironmentConfig:
    # The rates of the removed normalized default (10 mK, flux term 0.005/us), kept for the stored values.
    return EnvironmentConfig(temperature_mk=10.0, t1_zero_temperature_us=100.0, tphi_us=1.0 / 0.015)


def one_qubit_gate_config(gate_type: str) -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=1,
            initial_states=["0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(
                            type=gate_type,
                            targets=[0],
                            controls=[],
                            params={},
                        )
                    ],
                )
            ],
        ),
        environment=environment(),
        duration_us=0.001,
        time_steps=3,
        fidelity_threshold=0.9,
    )


def bell_config() -> SimulationConfig:
    return SimulationConfig(
        circuit=CircuitConfig(
            logical_qubits=2,
            initial_states=["0", "0"],
            columns=[
                GateColumn(
                    step=0,
                    gates=[
                        GateOperation(
                            type="H",
                            targets=[0],
                            controls=[],
                            params={},
                        )
                    ],
                ),
                GateColumn(
                    step=1,
                    gates=[
                        GateOperation(
                            type="CNOT",
                            targets=[1],
                            controls=[0],
                            params={},
                        )
                    ],
                ),
            ],
        ),
        environment=environment(),
        duration_us=0.001,
        time_steps=3,
        fidelity_threshold=0.9,
    )

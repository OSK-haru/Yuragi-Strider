"""Minimal FastAPI example endpoint for UI SimulationResponse data."""

from __future__ import annotations

from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
)
import logging
import math
import os
from threading import BoundedSemaphore
from typing import Literal
from time import perf_counter

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from api.attack_web_ownership import WebOwnershipSettings

from api.pulse_models import (
    PulseApiRequest,
    PulseApiResponse,
)
from api.pulse_service import (
    PulseExecutionLimitError,
    run_pulse_api_request as run_pulse_request,
)
from api.t1_sweep_service import run_t1_sweep
from api.ramsey_sweep_service import run_ramsey_sweep
from core.capabilities import DECLARED_PULSE_MODELS, PULSE_MODEL_STATUSES
from core.circuit_model import CircuitAnnotation, CircuitConfig, GateColumn, GateOperation
from core.physical_environment import INPUT_MODE_NORMALIZED, INPUT_MODE_PHYSICAL
from core.results import EnvironmentConfig, SimulationConfig
from core.gate_compiler import compile_gate_aware_circuit
from core.process_characterization import (
    MAX_MONTE_CARLO_SAMPLES,
    MIN_MONTE_CARLO_SAMPLES,
    characterize_process,
)
from core.pulse_toolbox_metrics import (
    MAX_PULSE_TOOLBOX_OBSERVABLES,
    MAX_TRANSMON_COUNT as PULSE_TOOLBOX_MAX_TRANSMONS,
    PulseToolboxSpec,
    compute_pulse_toolbox_metrics,
    density_from_upper_triangle,
)
from core.simulator import run_simulation
from core.toolbox_metrics import (
    MAX_TOOLBOX_OBSERVABLES,
    ToolboxSnapshotSpec,
    compute_toolbox_snapshot_metrics,
    validate_toolbox_spec,
)
from core.ui_response import compilation_response, simulation_result_to_ui_response


logger = logging.getLogger(__name__)

app = FastAPI(title="Yuragi-Strider API", version="0.1.0")
app.state.attack_simulator_enabled = False
app.state.attack_simulator_profile = None
app.state.attack_simulator_ownership_settings = None
app.state.attack_simulator_runtime = None


def configure_attack_simulator(
    *, profile: str, ownership_settings: WebOwnershipSettings | None = None,
    target_app: FastAPI | None = None,
) -> bool:
    """Mount the complete opt-in surface only for an explicit local profile.

    All dependencies and routers are built before one include_router call. A
    malformed attack-simulator configuration is logged and disables only this
    subsystem; the rest of the API remains available.
    """
    application = app if target_app is None else target_app
    if not isinstance(application, FastAPI):
        raise TypeError("target_app must be a FastAPI application")
    normalized = profile.strip().lower() if type(profile) is str else ""
    if normalized not in {"development", "desktop", "public"}:
        return False
    if normalized == "public" and _worker_count() > 1:
        # Environments and owner cookies live in this process; a second worker
        # would answer for environments it does not hold.
        logger.warning("Attack simulator disabled: the public profile needs exactly one worker process.")
        return False
    if getattr(application.state, "attack_simulator_enabled", False):
        current = getattr(application.state, "attack_simulator_runtime", None)
        if (getattr(application.state, "attack_simulator_profile", None) == normalized and current is not None
                and (ownership_settings is None or current.settings == ownership_settings)):
            return True
        logger.warning("Attack simulator already configured; refusing a second mount.")
        return False
    try:
        settings = ownership_settings
        if settings is None:
            if normalized == "desktop":
                raise ValueError("desktop profile requires launcher-provided ownership settings")
            settings = WebOwnershipSettings.from_environment()
        if normalized == "public" and (
                not settings.creates_per_minute or not settings.max_concurrent_evaluations
                or any(not origin.startswith("https://") for origin in settings.allowed_origins)):
            raise ValueError("the public profile requires WebOwnershipSettings.public() limits and HTTPS origins")
        from api.attack_product_runtime import build_attack_simulator_runtime

        runtime = build_attack_simulator_runtime(settings)
        application.include_router(runtime.router)
    except Exception:
        logger.warning(
            "Attack simulator disabled because its local profile is invalid or unavailable.",
            exc_info=True,
        )
        application.state.attack_simulator_enabled = False
        application.state.attack_simulator_profile = None
        application.state.attack_simulator_ownership_settings = None
        application.state.attack_simulator_runtime = None
        return False
    application.state.attack_simulator_enabled = True
    application.state.attack_simulator_profile = normalized
    application.state.attack_simulator_ownership_settings = settings
    application.state.attack_simulator_runtime = runtime
    return True


def _worker_count() -> int:
    """Worker processes requested by the usual launcher variables (default one)."""
    counts = []
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        try:
            counts.append(int(os.environ.get(name, "1") or "1"))
        except ValueError:
            counts.append(2)  # An unparseable value cannot be proven to be one worker.
    return max(counts)


def _configure_attack_simulator_from_environment() -> None:
    profile = os.environ.get("ATTACK_SIMULATOR_PROFILE", "generic").strip().lower()
    if profile == "generic":
        return
    if profile not in {"development", "public"}:
        logger.warning(
            "Attack simulator disabled: ATTACK_SIMULATOR_PROFILE must be generic, development or public."
        )
        return
    configure_attack_simulator(profile=profile)


app.router.on_startup.append(_configure_attack_simulator_from_environment)

# The API only accepts compact circuit descriptions.  This limit is enforced
# while the ASGI body is being received, including chunked requests, so a
# client cannot exhaust memory before Pydantic validates the JSON structure.
MAX_API_REQUEST_BODY_BYTES = 64 * 1024
# The Pulse toolbox takes the density matrices of a finished run (up to
# PULSE_TOOLBOX_MAX_MATRIX_ELEMENTS complex entries), which a compact circuit
# description never needs. Only that route gets the larger budget; every other
# route keeps the default.
PULSE_TOOLBOX_PATH = "/api/analysis/pulse-toolbox"
# The toolbox is cheap per request, but a request can carry millions of floats.
# A small dedicated budget keeps a burst of them from exhausting memory or
# starving the simulation slots.
PULSE_TOOLBOX_MAX_CONCURRENT_REQUESTS = 2
_PULSE_TOOLBOX_EXECUTION_SLOTS = BoundedSemaphore(PULSE_TOOLBOX_MAX_CONCURRENT_REQUESTS)
MAX_PULSE_TOOLBOX_REQUEST_BODY_BYTES = 6 * 1024 * 1024


class _RequestBodyTooLarge(Exception):
    """Raised internally when an HTTP request body exceeds the API budget."""


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before they reach JSON parsing."""

    def __init__(
        self,
        app: object,
        max_body_bytes: int,
        path_limits: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.path_limits = dict(path_limits or {})

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        if not isinstance(scope, dict) or scope.get("type") != "http":
            await self.app(scope, receive, send)  # type: ignore[misc]
            return

        max_body_bytes = self.path_limits.get(str(scope.get("path", "")), self.max_body_bytes)
        headers = dict(scope.get("headers", []))
        raw_content_length = headers.get(b"content-length")
        if raw_content_length is not None:
            try:
                if int(raw_content_length) > max_body_bytes:
                    await self._send_too_large(scope, receive, send, max_body_bytes)
                    return
            except ValueError:
                await self._send_too_large(scope, receive, send, max_body_bytes)
                return

        received_bytes = 0

        async def limited_receive() -> object:
            nonlocal received_bytes
            message = await receive()  # type: ignore[misc]
            if isinstance(message, dict) and message.get("type") == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > max_body_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)  # type: ignore[misc]
        except _RequestBodyTooLarge:
            await self._send_too_large(scope, receive, send, max_body_bytes)

    async def _send_too_large(
        self,
        scope: object,
        receive: object,
        send: object,
        max_body_bytes: int,
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "detail": {
                    "message": "Request body exceeds the API size limit.",
                    "maximum_bytes": max_body_bytes,
                }
            },
        )
        await response(scope, receive, send)  # type: ignore[arg-type]


app.add_middleware(
    RequestBodyLimitMiddleware,
    max_body_bytes=MAX_API_REQUEST_BODY_BYTES,
    path_limits={PULSE_TOOLBOX_PATH: MAX_PULSE_TOOLBOX_REQUEST_BODY_BYTES},
)

# Local dev (Vite) and the desktop-app launcher serve the frontend from the
# same origin as this API, so they never need CORS. The hosted web demo
# serves the frontend from a separate origin (Cloudflare Pages) and this API
# from another (Render), so that origin must be allow-listed explicitly via
# env var rather than left open to "*" (this endpoint runs real compute).
_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if _ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_ALLOWED_ORIGINS,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )


# JSON API responses never need to load or run anything.
API_CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"

# The desktop launcher serves the built UI from this same app. Mirror the
# hosted worker's policy (frontend/src/worker.ts), but allow connections only
# to this same origin: the local UI never talks to the hosted API.
LOCAL_UI_CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "connect-src 'self'",
))


# The attack simulator runs user scripts in a dedicated Worker with Pyodide
# (frontend/src/scripting/pythonWorker.ts). Pyodide compiles WebAssembly and
# evaluates its own generated JS, and loads extra packages such as numpy from the
# official CDN (each checked against the bundled lock file's SHA-256). Only the
# Worker and Pyodide files under this prefix get the looser policy; the page
# itself keeps LOCAL_UI_CONTENT_SECURITY_POLICY.
PYTHON_RUNTIME_PREFIX = "/assets/python-runtime/"
PYTHON_RUNTIME_PACKAGE_ORIGIN = "https://cdn.jsdelivr.net"
PYTHON_RUNTIME_CONTENT_SECURITY_POLICY = "; ".join((
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "script-src 'self' 'wasm-unsafe-eval' 'unsafe-eval'",
    f"connect-src 'self' {PYTHON_RUNTIME_PACKAGE_ORIGIN}",
))
# SharedArrayBuffer, which the script runner needs, requires a cross-origin
# isolated page. The Vite dev server sends the same pair (frontend/vite.config.ts).
CROSS_ORIGIN_ISOLATION_HEADERS = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
}


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


# FastAPI's developer docs load Swagger/ReDoc from a CDN with inline scripts;
# they keep their previous behaviour rather than a policy that would blank them.
_DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})


@app.middleware("http")
async def add_api_security_headers(request: Request, call_next: object):
    """Apply browser-hardening headers to every API and desktop UI response."""

    response = await call_next(request)  # type: ignore[misc]
    path = request.url.path
    if path not in _DOCS_PATHS:
        response.headers.setdefault(
            "Content-Security-Policy",
            API_CONTENT_SECURITY_POLICY if _is_api_path(path)
            else PYTHON_RUNTIME_CONTENT_SECURITY_POLICY if path.startswith(PYTHON_RUNTIME_PREFIX)
            else LOCAL_UI_CONTENT_SECURITY_POLICY,
        )
        if not _is_api_path(path):
            for name, value in CROSS_ORIGIN_ISOLATION_HEADERS.items():
                response.headers.setdefault(name, value)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), geolocation=(), microphone=()",
    )
    return response

def _optional_int_env(name: str) -> int | None:
    """Read an optional integer env var without crashing the app at import.

    A typo in a deploy dashboard should degrade to "cap not set" and say so in
    the logs, not take the whole service down with an import-time traceback.
    """

    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r; the cap stays unset.", name, raw)
        return None


# Dense simulation cost is O(8^n); the desktop app and test suite rely on the
# full le=18 validation range, but a publicly reachable server shares memory
# across every visitor, so it gets its own, stricter cap via env var. Unset
# by default so existing (non-public) deployments are unaffected.
# A cross-origin deployment is the public web demo.  It is intentionally
# restricted to the Circuit Studio's documented 2-8 qubit range.  Operators
# may lower the cap, but cannot accidentally raise the public limit above 8.
PUBLIC_WEB_MAX_LOGICAL_QUBITS = 8
_configured_public_qubit_cap = _optional_int_env("PUBLIC_MAX_LOGICAL_QUBITS")
if _ALLOWED_ORIGINS:
    PUBLIC_MAX_LOGICAL_QUBITS = min(
        PUBLIC_WEB_MAX_LOGICAL_QUBITS,
        _configured_public_qubit_cap or PUBLIC_WEB_MAX_LOGICAL_QUBITS,
    )
else:
    # Keep the desktop launcher and local development capable of exercising
    # their broader supported range unless an operator explicitly constrains it.
    PUBLIC_MAX_LOGICAL_QUBITS = _configured_public_qubit_cap

# str(exc) can carry file paths and internal state. The hosted web demo is the
# deployment that sets ALLOWED_ORIGINS (frontend and API on separate origins),
# so the same signal defaults public deploys to type-only errors, while local
# dev and the desktop launcher keep the full message their detail-mode
# diagnostics panel displays.  Error details are opt-in only; a missing or
# misconfigured CORS variable must never make a public deployment leak them.
_EXPOSE_INTERNAL_ERRORS_ENV = os.environ.get("EXPOSE_INTERNAL_ERRORS", "").strip()
EXPOSE_INTERNAL_ERRORS = (
    _EXPOSE_INTERNAL_ERRORS_ENV.lower() in {"1", "true", "yes", "on"}
    if _EXPOSE_INTERNAL_ERRORS_ENV
    else False
)


def _failure_detail(message: str, exc: Exception) -> dict[str, object]:
    """Build a 500 detail body, withholding the raw message off-origin."""

    detail: dict[str, object] = {
        "message": message,
        "error_type": exc.__class__.__name__,
    }
    if EXPOSE_INTERNAL_ERRORS:
        detail["error"] = str(exc)
    return detail


PULSE_API_TIMEOUT_SECONDS = 90.0
PULSE_API_MAX_CONCURRENT_REQUESTS = 2
_PULSE_EXECUTOR = ThreadPoolExecutor(
    max_workers=PULSE_API_MAX_CONCURRENT_REQUESTS,
    thread_name_prefix="yuragi-strider-pulse",
)
_PULSE_EXECUTION_SLOTS = BoundedSemaphore(
    PULSE_API_MAX_CONCURRENT_REQUESTS
)

# Circuit simulation gets the same shape of budget the pulse endpoint already
# has. Without it a single request with a large time_steps pins a shared host
# indefinitely: the logical-qubit cap above bounds the matrix size, not the
# number of steps taken through it.
SIMULATE_API_TIMEOUT_SECONDS = 120.0
SIMULATE_API_MAX_CONCURRENT_REQUESTS = 2
_SIMULATE_EXECUTOR = ThreadPoolExecutor(
    max_workers=SIMULATE_API_MAX_CONCURRENT_REQUESTS,
    thread_name_prefix="yuragi-strider-simulate",
)
_SIMULATE_EXECUTION_SLOTS = BoundedSemaphore(
    SIMULATE_API_MAX_CONCURRENT_REQUESTS
)
# Generous next to real use (the validation suite tops out at 101 steps and
# 200 us) but finite, so an absurd request is rejected before any compute.
MAX_API_TIME_STEPS = 2001
MAX_API_DURATION_US = 10_000.0
MAX_API_CIRCUIT_COLUMNS = 200
MAX_API_GATES_PER_COLUMN = 16
MAX_API_CIRCUIT_GATES = 800
MAX_API_CIRCUIT_ANNOTATIONS = 200
MAX_API_GATE_QUBIT_REFERENCES = 18


class GateDurationDefaultsRequest(BaseModel):
    H: float = Field(default=0.02, gt=0.0)
    X: float = Field(default=0.02, gt=0.0)
    Y: float = Field(default=0.02, gt=0.0)
    Z: float = Field(default=0.0, ge=0.0)
    S: float = Field(default=0.0, ge=0.0)
    T: float = Field(default=0.0, ge=0.0)
    RX: float = Field(default=0.02, gt=0.0)
    RY: float = Field(default=0.02, gt=0.0)
    RZ: float = Field(default=0.02, gt=0.0)
    CNOT: float = Field(default=0.2, gt=0.0)
    CZ: float = Field(default=0.2, gt=0.0)
    SWAP: float = Field(default=0.2, gt=0.0)
    CP: float = Field(default=0.2, gt=0.0)
    CCX: float = Field(default=0.4, gt=0.0)
    # QFT and ORACLE span a variable register, so these are per spanned qubit.
    QFT: float = Field(default=0.2, gt=0.0)
    ORACLE: float = Field(default=0.2, gt=0.0)
    MEASURE: float = Field(default=0.0, ge=0.0)
    MESSAGE: float = Field(default=0.04, gt=0.0)


class SnapshotOptionsRequest(BaseModel):
    enabled: bool = True
    uniform_count: int = Field(default=0, ge=0, le=100)
    custom_times_us: list[float] = Field(default_factory=list, max_length=100)
    include_initial: bool = True
    include_final: bool = True
    include_column_boundaries: bool = True
    include_after_circuit: bool = True

    @field_validator("uniform_count")
    @classmethod
    def validate_uniform_count(cls, value: int) -> int:
        if value == 1:
            raise ValueError("uniform_count must be 0 or an integer from 2 to 100")
        return value

    @field_validator("custom_times_us")
    @classmethod
    def validate_custom_times(cls, value: list[float]) -> list[float]:
        for time_us in value:
            if not math.isfinite(time_us) or time_us < 0.0:
                raise ValueError("custom_times_us must contain finite, non-negative times")
        return value


class MeasurementOptionsRequest(BaseModel):
    shots: int = Field(default=1024, ge=1, le=100_000)
    seed: int = Field(default=0, ge=0, le=2 ** 32 - 1)


class ReadoutErrorQubitRequest(BaseModel):
    p10: float = Field(default=0.0, ge=0.0, le=1.0)
    p01: float = Field(default=0.0, ge=0.0, le=1.0)


class ReadoutErrorRequest(BaseModel):
    """Affine two-point readout model applied to observation probabilities only."""

    p10: float = Field(default=0.0, ge=0.0, le=1.0)
    p01: float = Field(default=0.0, ge=0.0, le=1.0)
    per_qubit: list[ReadoutErrorQubitRequest] = Field(default_factory=list)


class QubitCoherenceRequest(BaseModel):
    """Per-qubit coherence maxima overriding the shared physical defaults."""

    t1_max_us: float = Field(gt=0.0)
    tphi_max_us: float = Field(gt=0.0)


class SimulationParametersRequest(BaseModel):
    normalized_temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    normalized_magnetic_field: float | None = Field(default=None, ge=0.0, le=1.0)
    noise_level: float | None = Field(default=None, ge=0.0, le=1.0)
    device_quality: float | None = Field(default=None, ge=0.0, le=1.0)
    temperature_mk: float | None = Field(default=None, ge=0.0)
    flux_noise_phi0: float | None = Field(default=None, ge=0.0)
    qubit_frequency_ghz: float | None = Field(default=None, gt=0.0)
    t1_max_us: float | None = Field(default=None, gt=0.0)
    tphi_max_us: float | None = Field(default=None, gt=0.0)
    per_qubit: list[QubitCoherenceRequest] = Field(default_factory=list, max_length=18)
    ideal_reference: bool = False
    duration_us: float | None = Field(default=None, gt=0.0, le=MAX_API_DURATION_US)
    time_steps: int | None = Field(default=None, ge=2, le=MAX_API_TIME_STEPS)
    fidelity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class CircuitGateParamsRequest(BaseModel):
    duration_us: float | None = None
    theta_rad: float | None = None
    control_value: Literal[0, 1] | None = None
    control_state: int | None = Field(default=None, ge=0)
    animation_parameter_t: float | None = None
    marked_index: float | None = None

    @field_validator("marked_index")
    @classmethod
    def validate_marked_index(cls, value: float | None) -> float | None:
        if value is None:
            return value
        if not math.isfinite(value) or value != int(value) or value < 0:
            raise ValueError("marked_index must be a non-negative integer.")
        return value

    @field_validator("duration_us")
    @classmethod
    def validate_duration_us(cls, value: float | None) -> float | None:
        if value is None:
            return value
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("duration_us must be finite and greater than or equal to 0.")
        return value

    @field_validator("theta_rad")
    @classmethod
    def validate_theta_rad(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("theta_rad must be finite.")
        return value

    @field_validator("animation_parameter_t")
    @classmethod
    def validate_animation_parameter_t(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("animation_parameter_t must be finite.")
        return value


class ClassicalConditionRequest(BaseModel):
    bit: int = Field(ge=0, le=31)
    value: Literal[0, 1]


class CircuitGateRequest(BaseModel):
    type: Literal["H", "X", "Y", "Z", "S", "T", "RX", "RY", "RZ", "CNOT", "CZ", "CP", "CCX", "SWAP", "QFT", "ORACLE", "MEASURE", "MESSAGE", "RECEIVED"]
    targets: list[int] = Field(max_length=MAX_API_GATE_QUBIT_REFERENCES)
    controls: list[int] = Field(
        default_factory=list,
        max_length=MAX_API_GATE_QUBIT_REFERENCES,
    )
    params: CircuitGateParamsRequest = Field(default_factory=CircuitGateParamsRequest)
    classical_targets: list[int] = Field(default_factory=list, max_length=32)
    condition: ClassicalConditionRequest | None = None
    conditions: list[ClassicalConditionRequest] = Field(
        default_factory=list,
        max_length=32,
    )

    @model_validator(mode="after")
    def validate_gate_shape(self) -> CircuitGateRequest:
        gate_type = self.type
        targets = self.targets or []
        controls = self.controls or []

        if gate_type == "CNOT":
            if len(controls) < 1:
                raise ValueError("CNOT requires at least one control qubit.")
            if len(targets) != 1:
                raise ValueError("CNOT requires exactly one target qubit.")
            if len(set(controls)) != len(controls) or targets[0] in controls:
                raise ValueError(
                    "CNOT controls and target must all be different qubits."
                )
            control_state = self.params.control_state
            if control_state is not None and control_state >= 2 ** len(controls):
                raise ValueError("CNOT control_state is outside the control-bit range.")
        elif gate_type in {"CZ", "CP"}:
            if len(controls) != 1:
                raise ValueError(f"{gate_type} requires exactly one control qubit.")
            if len(targets) != 1:
                raise ValueError(f"{gate_type} requires exactly one target qubit.")
            if controls[0] == targets[0]:
                raise ValueError(f"{gate_type} control and target must be different qubits.")
        elif gate_type == "CCX":
            if len(controls) != 2:
                raise ValueError("CCX requires exactly two control qubits.")
            if len(targets) != 1:
                raise ValueError("CCX requires exactly one target qubit.")
            if len({*controls, targets[0]}) != 3:
                raise ValueError("CCX controls and target must be different qubits.")
        elif gate_type == "SWAP":
            if controls:
                raise ValueError("SWAP does not accept control qubits.")
            if len(targets) != 2:
                raise ValueError("SWAP requires exactly two target qubits.")
            if targets[0] == targets[1]:
                raise ValueError("SWAP target qubits must be different.")
        elif gate_type in {"QFT", "ORACLE"}:
            if controls:
                raise ValueError(f"{gate_type} does not accept control qubits.")
            if not targets:
                raise ValueError(f"{gate_type} requires at least one target qubit.")
            if len(set(targets)) != len(targets):
                raise ValueError(f"{gate_type} target qubits must all be different.")
            if gate_type == "ORACLE":
                marked = self.params.marked_index
                if marked is not None and not 0 <= int(marked) < 2 ** len(targets):
                    raise ValueError(
                        "ORACLE marked_index must be inside the register range "
                        f"0..{2 ** len(targets) - 1}."
                    )
        else:
            if len(targets) != 1:
                raise ValueError(f"{gate_type} requires exactly one target qubit.")
            if controls:
                raise ValueError(f"{gate_type} does not accept control qubits.")

        return self


class CircuitColumnRequest(BaseModel):
    step: int = Field(default=0, ge=0)
    gates: list[CircuitGateRequest] = Field(
        default_factory=list,
        max_length=MAX_API_GATES_PER_COLUMN,
    )


class CircuitAnnotationRequest(BaseModel):
    kind: Literal["MESSAGE", "RECEIVED"]
    id: str | None = Field(default=None, max_length=128)
    source_id: str | None = Field(default=None, max_length=128)
    column_index: int = Field(ge=0)
    qubits: list[int] = Field(
        default_factory=list,
        max_length=MAX_API_GATE_QUBIT_REFERENCES,
    )


class AnimationParameterRequest(BaseModel):
    """Explicit Quirk-style parameter override, separate from physical time."""

    name: Literal["theta_rad", "animation_parameter_t"]
    value: float
    column_index: int = Field(ge=0)
    gate_index: int = Field(ge=0)

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("animation parameter value must be finite")
        return value


class CircuitConfigRequest(BaseModel):
    logical_qubits: int = Field(ge=1, le=18)
    classical_bits: int = Field(default=0, ge=0, le=32)
    initial_states: list[int | str] = Field(max_length=18)
    columns: list[CircuitColumnRequest] = Field(
        default_factory=list,
        max_length=MAX_API_CIRCUIT_COLUMNS,
    )
    annotations: list[CircuitAnnotationRequest] = Field(
        default_factory=list,
        max_length=MAX_API_CIRCUIT_ANNOTATIONS,
    )

    @model_validator(mode="after")
    def validate_circuit_config(self) -> CircuitConfigRequest:
        logical_qubits = self.logical_qubits
        initial_states = self.initial_states or []
        columns = self.columns or []
        classical_bits = self.classical_bits or 0

        if len(initial_states) != logical_qubits:
            raise ValueError("initial_states must match logical_qubits.")

        if sum(len(column.gates) for column in columns) > MAX_API_CIRCUIT_GATES:
            raise ValueError(
                "circuit exceeds the maximum number of API gate operations."
            )

        for index, initial_state in enumerate(initial_states):
            if str(initial_state) not in {"0", "1", "+", "-"}:
                raise ValueError(
                    f"initial_states[{index}] must be one of 0, 1, +, or -."
                )

        for column in columns:
            for gate in column.gates:
                for target in gate.targets:
                    if target < 0 or target >= logical_qubits:
                        raise ValueError(
                            "Gate target is outside the logical qubit range."
                        )
                for control in gate.controls or []:
                    if control < 0 or control >= logical_qubits:
                        raise ValueError(
                            "Gate control is outside the logical qubit range."
                        )
                for classical_target in gate.classical_targets:
                    if classical_target >= classical_bits:
                        raise ValueError(
                            "classical target is outside the classical register range."
                        )
                if gate.condition is not None and gate.condition.bit >= classical_bits:
                    raise ValueError(
                        "classical condition bit is outside the classical register range."
                    )
                for condition in gate.conditions:
                    if condition.bit >= classical_bits:
                        raise ValueError(
                            "classical condition bit is outside the classical register range."
                        )
                if gate.classical_targets and gate.type != "MEASURE":
                    raise ValueError(
                        "classical_targets are only valid for MEASURE gates."
                    )
                if gate.type == "MEASURE" and gate.classical_targets and len(
                    gate.classical_targets
                ) != len(gate.targets):
                    raise ValueError(
                        "MEASURE classical_targets must match the measured targets."
                    )

        for annotation in self.annotations or []:
            for qubit in annotation.qubits:
                if qubit < 0 or qubit >= logical_qubits:
                    raise ValueError("Annotation qubit is outside the logical qubit range.")

        return self


class ToolboxRequest(BaseModel):
    """Extra per-snapshot metrics requested before the run.

    Each field is off when omitted. Which subsets are meaningful (and which
    would only repeat a value the explorer already shows) is decided by
    core.toolbox_metrics, so the request model only bounds sizes.
    """

    entropy_qubits: list[int] | None = Field(default=None, max_length=18)
    concurrence_qubits: list[int] | None = Field(default=None, max_length=2)
    negativity_subsystem: list[int] | None = Field(default=None, max_length=18)
    mutual_information_a: list[int] | None = Field(default=None, max_length=18)
    mutual_information_b: list[int] | None = Field(default=None, max_length=18)
    trace_distance: bool = False
    observables: list[str] = Field(default_factory=list, max_length=MAX_TOOLBOX_OBSERVABLES)

    def to_spec(self) -> ToolboxSnapshotSpec:
        def subset(value: list[int] | None) -> tuple[int, ...] | None:
            return None if value is None else tuple(value)

        return ToolboxSnapshotSpec(
            entropy_qubits=subset(self.entropy_qubits),
            concurrence_qubits=subset(self.concurrence_qubits),
            negativity_subsystem=subset(self.negativity_subsystem),
            mutual_information_a=subset(self.mutual_information_a),
            mutual_information_b=subset(self.mutual_information_b),
            trace_distance=self.trace_distance,
            observables=tuple(self.observables),
        )



# Pulse trajectories keep one density matrix per sample. The cap matches the
# Pulse network response budget, so a result the API produced always fits.
PULSE_TOOLBOX_MAX_POINTS = 2000
PULSE_TOOLBOX_MAX_MATRIX_ELEMENTS = 250_000


class PulseToolboxMatrix(BaseModel):
    """One Hermitian matrix as its upper triangle, row by row, diagonal included."""

    upper_real: list[float]
    upper_imag: list[float]


class PulseToolboxRequest(BaseModel):
    """Extra metrics for a finished Pulse run, computed from its density matrices.

    The run itself is not repeated. The client sends the density matrices it
    already holds, so the metrics also work for an imported result and can be
    changed without running the simulation again. Which subsets are meaningful
    is decided by core.pulse_toolbox_metrics; this model only bounds sizes.
    """

    local_levels: Literal[2, 3]
    transmon_count: int = Field(ge=1, le=PULSE_TOOLBOX_MAX_TRANSMONS)
    times_us: list[float] = Field(min_length=1, max_length=PULSE_TOOLBOX_MAX_POINTS)
    density_matrices: list[PulseToolboxMatrix] = Field(
        min_length=1, max_length=PULSE_TOOLBOX_MAX_POINTS
    )
    entropy_transmons: list[int] | None = Field(default=None, max_length=4)
    concurrence_transmons: list[int] | None = Field(default=None, max_length=2)
    negativity_subsystem: list[int] | None = Field(default=None, max_length=4)
    mutual_information_a: list[int] | None = Field(default=None, max_length=4)
    mutual_information_b: list[int] | None = Field(default=None, max_length=4)
    observables: list[str] = Field(
        default_factory=list, max_length=MAX_PULSE_TOOLBOX_OBSERVABLES
    )

    def to_spec(self) -> PulseToolboxSpec:
        def subset(value: list[int] | None) -> tuple[int, ...] | None:
            return None if value is None else tuple(value)

        return PulseToolboxSpec(
            entropy_transmons=subset(self.entropy_transmons),
            concurrence_transmons=subset(self.concurrence_transmons),
            negativity_subsystem=subset(self.negativity_subsystem),
            mutual_information_a=subset(self.mutual_information_a),
            mutual_information_b=subset(self.mutual_information_b),
            observables=tuple(self.observables),
        )


class SimulateRequest(BaseModel):
    circuit_preset: Literal["bell", "teleportation", "bit_flip_repetition"] | None = None
    simulation_backend: Literal["python_dense", "rust_dense_preview"]
    evolution_method: Literal["fixed_step_rk4", "explicit_cptp"] = (
        "fixed_step_rk4"
    )
    compilation_mode: Literal["logical_direct", "auto_decompose"] = (
        "logical_direct"
    )
    input_mode: Literal["normalized", "physical"] = INPUT_MODE_NORMALIZED
    gate_duration_defaults: GateDurationDefaultsRequest = Field(
        default_factory=GateDurationDefaultsRequest
    )
    circuit_config: CircuitConfigRequest | None = None
    animation_parameter: AnimationParameterRequest | None = None
    snapshot_options: SnapshotOptionsRequest | None = None
    measurement_options: MeasurementOptionsRequest = Field(
        default_factory=MeasurementOptionsRequest
    )
    readout_error: ReadoutErrorRequest | None = None
    parameters: SimulationParametersRequest
    toolbox: ToolboxRequest | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_parameters_for_input_mode(cls, values: object) -> object:
        # A "before" validator sees the raw input, which is a mapping for the
        # JSON request path but not for e.g. model_validate on an instance.
        if not isinstance(values, dict):
            return values
        if values.get("circuit_config") is None and values.get("circuit_preset") is None:
            raise ValueError("circuit_preset or circuit_config is required")
        input_mode = values.get("input_mode", INPUT_MODE_NORMALIZED)
        if input_mode not in {INPUT_MODE_NORMALIZED, INPUT_MODE_PHYSICAL}:
            return values
        parameters = values.get("parameters")
        if not isinstance(parameters, dict):
            return values
        required_fields = ["duration_us", "time_steps", "fidelity_threshold"]
        required_fields.extend(
            ["normalized_temperature", "normalized_magnetic_field", "noise_level"]
            if input_mode == INPUT_MODE_NORMALIZED
            else ["device_quality", "temperature_mk", "flux_noise_phi0", "qubit_frequency_ghz", "t1_max_us", "tphi_max_us"]
        )
        missing_fields = [field_name for field_name in required_fields if parameters.get(field_name) is None]
        if missing_fields:
            raise ValueError(f"parameters missing required fields for {input_mode} input_mode: {', '.join(missing_fields)}")
        snapshot_options = values.get("snapshot_options")
        if isinstance(snapshot_options, dict):
            duration_us = parameters.get("duration_us")
            if duration_us is not None and any(isinstance(time_us, (int, float)) and time_us > duration_us for time_us in (snapshot_options.get("custom_times_us") or [])):
                raise ValueError("snapshot_options.custom_times_us must not exceed parameters.duration_us")
        return values

    @model_validator(mode="after")
    def validate_per_qubit_calibration(self) -> "SimulateRequest":
        qubit_count = (
            self.circuit_config.logical_qubits
            if self.circuit_config is not None
            else {"bell": 2, "teleportation": 3, "bit_flip_repetition": 3}.get(
                self.circuit_preset or "bell", 2
            )
        )
        if self.parameters.per_qubit:
            if self.input_mode != INPUT_MODE_PHYSICAL:
                raise ValueError("parameters.per_qubit requires physical input_mode")
            if len(self.parameters.per_qubit) != qubit_count:
                raise ValueError(
                    "parameters.per_qubit must match circuit logical_qubits "
                    f"({qubit_count})"
                )
        if self.readout_error is not None and self.readout_error.per_qubit:
            if len(self.readout_error.per_qubit) != qubit_count:
                raise ValueError(
                    "readout_error.per_qubit must match circuit logical_qubits "
                    f"({qubit_count})"
                )
            for index, entry in enumerate(self.readout_error.per_qubit):
                if entry.p10 + entry.p01 >= 1.0:
                    raise ValueError(
                        f"readout_error.per_qubit[{index}] assignment span must be positive"
                    )
        elif self.readout_error is not None:
            if self.readout_error.p10 + self.readout_error.p01 >= 1.0:
                raise ValueError("readout_error assignment span must be positive")
        if self.toolbox is not None:
            validate_toolbox_spec(self.toolbox.to_spec(), qubit_count)
        return self


class ProcessCharacterizationRequest(SimulateRequest):
    """The same circuit and environment as /api/simulate, plus estimator controls."""

    monte_carlo_samples: int = Field(
        default=64,
        ge=MIN_MONTE_CARLO_SAMPLES,
        le=MAX_MONTE_CARLO_SAMPLES,
    )
    monte_carlo_seed: int = Field(default=0, ge=0, le=2 ** 32 - 1)
    include_process_matrix: bool = True


class T1SweepRequest(BaseModel):
    simulation_backend: Literal["python_dense", "rust_dense_preview"] = "python_dense"
    evolution_method: Literal["fixed_step_rk4", "explicit_cptp"] = "fixed_step_rk4"
    maximum_delay_us: float = Field(default=300.0, gt=0.0, le=MAX_API_DURATION_US)
    point_count: int = Field(default=31, ge=5, le=101)
    parameters: SimulationParametersRequest
    readout_error: ReadoutErrorRequest | None = None

    @model_validator(mode="after")
    def validate_t1_sweep(self) -> "T1SweepRequest":
        required = (
            "device_quality",
            "temperature_mk",
            "flux_noise_phi0",
            "qubit_frequency_ghz",
            "t1_max_us",
            "tphi_max_us",
        )
        missing = [name for name in required if getattr(self.parameters, name) is None]
        if missing:
            raise ValueError(
                "parameters missing required physical fields for T1 sweep: "
                + ", ".join(missing)
            )
        if self.readout_error is not None:
            entries = self.readout_error.per_qubit
            if len(entries) > 1:
                raise ValueError(
                    "T1 sweep readout_error.per_qubit accepts exactly one entry"
                )
            selected = entries[0] if entries else self.readout_error
            if selected.p10 + selected.p01 >= 1.0:
                raise ValueError("readout assignment span must be positive")
        return self


class T1SweepPointResponse(BaseModel):
    delay_us: float
    true_p1: float
    observed_p1: float
    fitted_observed_p1: float
    residual: float


class T1SweepFitResponse(BaseModel):
    amplitude: float
    observation_floor: float
    t1_us: float
    root_mean_square_error: float
    maximum_absolute_error: float
    r_squared: float | None


class T1SweepReferenceResponse(BaseModel):
    t1_effective_us: float | None
    gamma_population_relaxation_per_us: float | None
    temperature_mk: float


class T1SweepReadoutResponse(BaseModel):
    enabled: bool
    p10: float
    p01: float


class T1SweepResponse(BaseModel):
    schema_version: Literal["t1-sweep-v1"]
    model: Literal["single_exponential_with_observation_floor"]
    points: list[T1SweepPointResponse]
    fit: T1SweepFitResponse
    reference: T1SweepReferenceResponse
    readout: T1SweepReadoutResponse
    diagnostics: dict[str, object]


class RamseySweepRequest(BaseModel):
    simulation_backend: Literal["python_dense", "rust_dense_preview"] = "python_dense"
    evolution_method: Literal["fixed_step_rk4", "explicit_cptp"] = "fixed_step_rk4"
    maximum_delay_us: float = Field(default=300.0, gt=0.0, le=MAX_API_DURATION_US)
    point_count: int = Field(default=61, ge=9, le=101)
    detuning_mhz: float = Field(default=0.01, gt=0.0, le=1.0)
    parameters: SimulationParametersRequest
    readout_error: ReadoutErrorRequest | None = None

    @model_validator(mode="after")
    def validate_ramsey_sweep(self) -> "RamseySweepRequest":
        required = (
            "device_quality",
            "temperature_mk",
            "flux_noise_phi0",
            "qubit_frequency_ghz",
            "t1_max_us",
            "tphi_max_us",
        )
        missing = [name for name in required if getattr(self.parameters, name) is None]
        if missing:
            raise ValueError(
                "parameters missing required physical fields for Ramsey sweep: "
                + ", ".join(missing)
            )
        sample_interval = self.maximum_delay_us / (self.point_count - 1)
        if self.detuning_mhz >= 0.5 / sample_interval:
            raise ValueError(
                "detuning_mhz must stay below the sweep Nyquist frequency; "
                "increase point_count or reduce maximum_delay_us"
            )
        if self.readout_error is not None:
            entries = self.readout_error.per_qubit
            if len(entries) > 1:
                raise ValueError(
                    "Ramsey sweep readout_error.per_qubit accepts exactly one entry"
                )
            selected = entries[0] if entries else self.readout_error
            if selected.p10 + selected.p01 >= 1.0:
                raise ValueError("readout assignment span must be positive")
        return self


class RamseySweepPointResponse(T1SweepPointResponse):
    pass


class RamseyFitResponse(BaseModel):
    model: Literal["exponential_envelope", "gaussian_envelope"]
    envelope_power: Literal[1.0, 2.0]
    amplitude: float
    observation_floor: float
    t2_star_us: float
    frequency_mhz: float
    phase_rad: float
    root_mean_square_error: float
    maximum_absolute_error: float
    r_squared: float | None
    aicc: float
    delta_aicc: float


class RamseyReferenceResponse(BaseModel):
    t2_effective_us: float | None
    detuning_mhz: float
    temperature_mk: float


class RamseySweepResponse(BaseModel):
    schema_version: Literal["ramsey-sweep-v1"]
    selected_model: Literal["exponential_envelope", "gaussian_envelope"]
    points: list[RamseySweepPointResponse]
    fit: RamseyFitResponse
    candidate_ranking_by_aicc: list[RamseyFitResponse]
    reference: RamseyReferenceResponse
    readout: T1SweepReadoutResponse
    diagnostics: dict[str, object]


class CompilePreviewRequest(BaseModel):
    """Everything the gate compiler needs, and nothing the simulator needs.

    Compilation is a pure circuit rewrite, so the preview deliberately does not
    accept environment parameters, backends, or snapshot options: the diagram it
    returns depends only on the circuit, the mode, and the gate durations.
    """

    circuit_preset: Literal["bell", "teleportation", "bit_flip_repetition"] | None = None
    compilation_mode: Literal["logical_direct", "auto_decompose"] = (
        "logical_direct"
    )
    gate_duration_defaults: GateDurationDefaultsRequest = Field(
        default_factory=GateDurationDefaultsRequest
    )
    circuit_config: CircuitConfigRequest | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_circuit_source(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        if values.get("circuit_config") is None and values.get("circuit_preset") is None:
            raise ValueError("circuit_preset or circuit_config is required")
        return values


def build_bell_circuit(
    gate_durations: GateDurationDefaultsRequest | None = None,
) -> CircuitConfig:
    durations = gate_durations or GateDurationDefaultsRequest()
    return CircuitConfig(
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
                        params={"duration_us": durations.H},
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
                        params={"duration_us": durations.CNOT},
                    )
                ],
            ),
        ],
    )


def build_teleportation_circuit(
    gate_durations: GateDurationDefaultsRequest | None = None,
) -> CircuitConfig:
    """Three-qubit teleportation with explicit two-bit feed-forward."""
    durations = gate_durations or GateDurationDefaultsRequest()
    return CircuitConfig(
        logical_qubits=3,
        initial_states=["+", "0", "0"],
        classical_bits=2,
        columns=[
            GateColumn(0, [GateOperation("H", [1], params={"duration_us": durations.H})]),
            GateColumn(1, [GateOperation("CNOT", [2], [1], params={"duration_us": durations.CNOT})]),
            GateColumn(2, [GateOperation("CNOT", [1], [0], params={"duration_us": durations.CNOT})]),
            GateColumn(3, [GateOperation("H", [0], params={"duration_us": durations.H})]),
            GateColumn(4, [GateOperation("MEASURE", [0], params={"duration_us": durations.MEASURE}, classical_targets=[0])]),
            GateColumn(5, [GateOperation("MEASURE", [1], params={"duration_us": durations.MEASURE}, classical_targets=[1])]),
            GateColumn(6, [GateOperation("X", [2], params={"duration_us": durations.X}, condition={"bit": 1, "value": 1})]),
            GateColumn(7, [GateOperation("Z", [2], params={"duration_us": durations.Z}, condition={"bit": 0, "value": 1})]),
        ],
    )


def build_bit_flip_repetition_circuit(
    gate_durations: GateDurationDefaultsRequest | None = None,
) -> CircuitConfig:
    """Five-qubit 3-bit repetition code with two-bit syndrome correction.

    q0-q2 are data, q3-q4 are syndrome ancillas. An X on q1 is injected as a
    deterministic fault so the preset demonstrates the complete encode,
    syndrome-measure, conditional-correction flow.
    """
    durations = gate_durations or GateDurationDefaultsRequest()
    def op(gate_type: str, targets: list[int], controls: list[int] | None = None, **kwargs):
        return GateOperation(
            gate_type,
            targets,
            controls or [],
            params={"duration_us": getattr(durations, gate_type)},
            **kwargs,
        )
    return CircuitConfig(
        logical_qubits=5,
        initial_states=["1", "0", "0", "0", "0"],
        classical_bits=2,
        columns=[
            GateColumn(0, [op("CNOT", [1], [0])]),
            GateColumn(1, [op("CNOT", [2], [0])]),
            GateColumn(2, [op("X", [1])]),
            GateColumn(3, [op("CNOT", [3], [0])]),
            GateColumn(4, [op("CNOT", [3], [1])]),
            GateColumn(5, [op("CNOT", [4], [1])]),
            GateColumn(6, [op("CNOT", [4], [2])]),
            GateColumn(7, [op("MEASURE", [3], classical_targets=[0])]),
            GateColumn(8, [op("MEASURE", [4], classical_targets=[1])]),
            GateColumn(9, [op("X", [0], conditions=[{"bit": 0, "value": 1}, {"bit": 1, "value": 0}])]),
            GateColumn(10, [op("X", [1], conditions=[{"bit": 0, "value": 1}, {"bit": 1, "value": 1}])]),
            GateColumn(11, [op("X", [2], conditions=[{"bit": 0, "value": 0}, {"bit": 1, "value": 1}])]),
        ],
    )


def build_custom_circuit(
    circuit_config: CircuitConfigRequest,
    gate_durations: GateDurationDefaultsRequest,
) -> CircuitConfig:
    duration_defaults = {
        "H": gate_durations.H,
        "X": gate_durations.X,
        "Y": gate_durations.Y,
        "Z": gate_durations.Z,
        "S": gate_durations.S,
        "T": gate_durations.T,
        "RX": gate_durations.RX,
        "RY": gate_durations.RY,
        "RZ": gate_durations.RZ,
        "CNOT": gate_durations.CNOT,
        "CZ": gate_durations.CZ,
        "SWAP": gate_durations.SWAP,
        "CP": gate_durations.CP,
        "CCX": gate_durations.CCX,
        "MEASURE": gate_durations.MEASURE,
        "MESSAGE": gate_durations.MESSAGE,
    }

    def default_duration_us(gate: CircuitGateRequest) -> float:
        # QFT and ORACLE declare their default per spanned qubit.
        if gate.type == "QFT":
            return gate_durations.QFT * len(gate.targets)
        if gate.type == "ORACLE":
            return gate_durations.ORACLE * len(gate.targets)
        return duration_defaults[gate.type]

    return CircuitConfig(
        logical_qubits=circuit_config.logical_qubits,
        classical_bits=circuit_config.classical_bits,
        initial_states=[str(state) for state in circuit_config.initial_states],
        columns=[
            GateColumn(
                step=column.step,
                gates=[
                    GateOperation(
                        type=gate.type,
                        targets=list(gate.targets),
                        controls=list(gate.controls),
                        params={
                            **gate.params.model_dump(exclude_none=True),
                            **(
                                {"duration_us": default_duration_us(gate)}
                                if gate.params.duration_us is None
                                else {}
                            ),
                        },
                        classical_targets=list(gate.classical_targets),
                        condition=(
                            None
                            if gate.condition is None
                            else {
                                "bit": gate.condition.bit,
                                "value": gate.condition.value,
                            }
                        ),
                        conditions=[
                            {"bit": item.bit, "value": item.value}
                            for item in gate.conditions
                        ],
                    )
                    for gate in column.gates
                    if gate.type != "RECEIVED"
                ],
            )
            for column in circuit_config.columns
        ],
        annotations=[
            CircuitAnnotation(
                kind=annotation.kind,
                id=annotation.id,
                source_id=annotation.source_id,
                column_index=annotation.column_index,
                qubits=list(annotation.qubits),
            )
            for annotation in circuit_config.annotations
        ] + [
            CircuitAnnotation(
                kind="RECEIVED",
                id=f"received-column-{column.step}-gate-{gate_index}",
                column_index=column.step,
                qubits=list(gate.targets),
            )
            for column in circuit_config.columns
            for gate_index, gate in enumerate(column.gates)
            if gate.type == "RECEIVED"
        ],
    )


def build_example_config() -> SimulationConfig:
    return SimulationConfig(
        circuit=build_bell_circuit(),
        environment=EnvironmentConfig(),
        duration_us=2.0,
        time_steps=11,
        fidelity_threshold=0.9,
        simulation_backend="python_dense",
    )


def _build_requested_circuit(
    circuit_config: CircuitConfigRequest | None,
    circuit_preset: str | None,
    gate_duration_defaults: GateDurationDefaultsRequest,
) -> CircuitConfig:
    """Resolve the circuit a request asked for, by config or by preset name."""

    if circuit_config is not None:
        return build_custom_circuit(circuit_config, gate_duration_defaults)
    if circuit_preset == "teleportation":
        return build_teleportation_circuit(gate_duration_defaults)
    if circuit_preset == "bit_flip_repetition":
        return build_bit_flip_repetition_circuit(gate_duration_defaults)
    return build_bell_circuit(gate_duration_defaults)


def build_config_from_simulate_request(request: SimulateRequest) -> SimulationConfig:
    parameters = request.parameters
    if request.input_mode == INPUT_MODE_PHYSICAL:
        environment = EnvironmentConfig(
            input_mode=INPUT_MODE_PHYSICAL,
            device_quality=_required(parameters.device_quality, "device_quality"),
            temperature_mk=_required(parameters.temperature_mk, "temperature_mk"),
            flux_noise_phi0=_required(parameters.flux_noise_phi0, "flux_noise_phi0"),
            qubit_frequency_ghz=_required(
                parameters.qubit_frequency_ghz,
                "qubit_frequency_ghz",
            ),
            t1_max_us=_required(parameters.t1_max_us, "t1_max_us"),
            tphi_max_us=_required(parameters.tphi_max_us, "tphi_max_us"),
            per_qubit=[
                {
                    "t1_max_us": entry.t1_max_us,
                    "tphi_max_us": entry.tphi_max_us,
                }
                for entry in parameters.per_qubit
            ],
            ideal_reference=parameters.ideal_reference,
        )
    else:
        environment = EnvironmentConfig(
            input_mode=INPUT_MODE_NORMALIZED,
            temperature=_required(
                parameters.normalized_temperature,
                "normalized_temperature",
            ),
            magnetic_field=_required(
                parameters.normalized_magnetic_field,
                "normalized_magnetic_field",
            ),
            noise_level=_required(parameters.noise_level, "noise_level"),
        )

    circuit = _build_requested_circuit(
        request.circuit_config,
        request.circuit_preset,
        request.gate_duration_defaults,
    )

    if request.animation_parameter is not None:
        circuit = apply_animation_parameter(circuit, request.animation_parameter)

    return SimulationConfig(
        circuit=circuit,
        environment=environment,
        duration_us=_required(parameters.duration_us, "duration_us"),
        time_steps=_required(parameters.time_steps, "time_steps"),
        fidelity_threshold=_required(
            parameters.fidelity_threshold,
            "fidelity_threshold",
        ),
        simulation_backend=request.simulation_backend,
        evolution_method=request.evolution_method,
        compilation_mode=request.compilation_mode,
        native_gate_durations_us={
            name: float(value)
            for name, value in request.gate_duration_defaults.model_dump().items()
        },
        measurement_shots=request.measurement_options.shots,
        measurement_seed=request.measurement_options.seed,
        readout_error=(
            None
            if request.readout_error is None
            else {
                "p10": request.readout_error.p10,
                "p01": request.readout_error.p01,
                "per_qubit": [
                    {"p10": entry.p10, "p01": entry.p01}
                    for entry in request.readout_error.per_qubit
                ],
            }
        ),
        snapshot_options=(
            None
            if request.snapshot_options is None
            else {
                "enabled": request.snapshot_options.enabled,
                "uniform_count": request.snapshot_options.uniform_count,
                "custom_times_us": request.snapshot_options.custom_times_us,
                "include_initial": request.snapshot_options.include_initial,
                "include_final": request.snapshot_options.include_final,
                "include_column_boundaries": request.snapshot_options.include_column_boundaries,
                "include_after_circuit": request.snapshot_options.include_after_circuit,
            }
        ),
    )


def apply_animation_parameter(
    circuit: CircuitConfig,
    animation_parameter: AnimationParameterRequest,
) -> CircuitConfig:
    """Apply a named demo parameter before entering the stable core boundary."""

    column_index = animation_parameter.column_index
    gate_index = animation_parameter.gate_index
    if column_index >= len(circuit.columns):
        raise ValueError("animation parameter column_index is outside the circuit")
    target_column = circuit.columns[column_index]
    if gate_index >= len(target_column.gates):
        raise ValueError("animation parameter gate_index is outside the column")
    target_gate = target_column.gates[gate_index]
    target_type = str(target_gate.type).upper()
    if animation_parameter.name == "theta_rad" and target_type not in {"RX", "RY", "RZ", "CP"}:
        raise ValueError("theta_rad animation requires RX, RY, RZ, or CP")
    if animation_parameter.name == "animation_parameter_t" and target_type != "MESSAGE":
        raise ValueError("animation_parameter_t requires a MESSAGE gate")
    parameter_name = animation_parameter.name
    parameter_value = animation_parameter.value % 1.0 if parameter_name == "animation_parameter_t" else animation_parameter.value
    updated_columns = list(circuit.columns)
    updated_gates = list(target_column.gates)
    updated_gates[gate_index] = GateOperation(
        type=target_gate.type,
        targets=list(target_gate.targets),
        controls=list(target_gate.controls or []),
        params={**(target_gate.params or {}), parameter_name: parameter_value},
        classical_targets=list(target_gate.classical_targets or []),
        conditions=[
            {"bit": item.bit, "value": item.value}
            for item in target_gate.conditions
        ],
    )
    updated_columns[column_index] = GateColumn(
        step=target_column.step,
        gates=updated_gates,
    )
    return CircuitConfig(
        logical_qubits=circuit.logical_qubits,
        initial_states=list(circuit.initial_states),
        classical_bits=circuit.classical_bits,
        columns=updated_columns,
        annotations=list(circuit.annotations),
    )


def _required(value: float | int | None, field_name: str) -> float | int:
    if value is None:
        raise ValueError(f"{field_name} is required")
    return value


@app.get("/api/health")
def health() -> dict[str, object]:
    """Report liveness plus the pulse contract this build actually accepts.

    The frontend and API deploy separately, so a stale API can serve a UI that
    sends model ids it has never heard of.  That surfaces only as a 422 at run
    time, which reads to the user as "my settings are wrong" rather than "this
    server is old".  Listing the accepted ids here lets the client detect the
    mismatch before anyone presses run.  An older build omits `pulse_models`
    entirely, which is itself the signal.
    """

    return {
        "status": "ok",
        "pulse_models": sorted(DECLARED_PULSE_MODELS),
        "pulse_model_statuses": dict(sorted(PULSE_MODEL_STATUSES.items())),
    }


@app.get("/api/simulation/example")
def simulation_example() -> dict[str, object]:
    result = run_simulation(build_example_config())
    return simulation_result_to_ui_response(result)


@app.post(
    "/api/circuit/compile",
    responses={
        422: {"description": "Invalid or over-budget circuit."},
    },
)
def compile_circuit_preview(request: CompilePreviewRequest) -> dict[str, object]:
    """Compile a circuit without simulating it, for the pre-run preview.

    This is the same rewrite `/api/simulate` performs internally, returned in
    the same shape, so the editor can show the decomposed circuit before anyone
    pays for a run. It never touches the simulator, so it stays cheap enough to
    call on every circuit edit and needs no execution slot.
    """

    if (
        PUBLIC_MAX_LOGICAL_QUBITS is not None
        and request.circuit_config is not None
        and request.circuit_config.logical_qubits > PUBLIC_MAX_LOGICAL_QUBITS
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "logical_qubits exceeds the public demo limit.",
                "maximum_logical_qubits": PUBLIC_MAX_LOGICAL_QUBITS,
            },
        )

    try:
        circuit = _build_requested_circuit(
            request.circuit_config,
            request.circuit_preset,
            request.gate_duration_defaults,
        )
        compilation = compile_gate_aware_circuit(
            circuit,
            request.compilation_mode,
            {
                name: float(value)
                for name, value in request.gate_duration_defaults.model_dump().items()
            },
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=_failure_detail("Circuit compilation failed.", exc),
        ) from exc
    except Exception as exc:
        logger.exception("Circuit compilation preview failed.")
        raise HTTPException(
            status_code=500,
            detail=_failure_detail("Circuit compilation failed.", exc),
        ) from exc

    return {"compilation": compilation_response(compilation.diagnostics)}


@app.post(
    "/api/pulse/simulate",
    response_model=PulseApiResponse,
    responses={
        422: {"description": "Invalid or over-budget pulse request."},
        503: {"description": "Pulse execution capacity is busy."},
        504: {"description": "Pulse execution exceeded the API timeout."},
    },
)
def pulse_simulate(request: PulseApiRequest) -> dict[str, object]:
    if not _PULSE_EXECUTION_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Pulse execution capacity is busy.",
                "maximum_concurrent_requests": (
                    PULSE_API_MAX_CONCURRENT_REQUESTS
                ),
            },
        )
    try:
        future = _PULSE_EXECUTOR.submit(run_pulse_request, request)
    except Exception:
        _PULSE_EXECUTION_SLOTS.release()
        raise
    future.add_done_callback(
        lambda completed: _PULSE_EXECUTION_SLOTS.release()
    )
    try:
        return future.result(timeout=PULSE_API_TIMEOUT_SECONDS)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise HTTPException(
            status_code=504,
            detail={
                "message": "Pulse simulation timed out.",
                "timeout_seconds": PULSE_API_TIMEOUT_SECONDS,
                "previous_results_preserved": True,
            },
        ) from exc
    except PulseExecutionLimitError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Pulse request exceeds execution limits.",
                "error": str(exc),
            },
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Pulse simulation failed.")
        raise HTTPException(
            status_code=500,
            detail=_failure_detail("Pulse simulation failed.", exc),
        ) from exc


@app.post(
    "/api/analysis/t1-sweep",
    response_model=T1SweepResponse,
    responses={
        422: {"description": "Invalid T1 sweep request."},
        503: {"description": "Simulation capacity is busy."},
        504: {"description": "T1 sweep exceeded the API timeout."},
    },
)
def t1_sweep(request: T1SweepRequest) -> dict[str, object]:
    """Run a bounded excited-state decay sweep and fit A exp(-t/T1) + B."""

    if not _SIMULATE_EXECUTION_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Simulation capacity is busy.",
                "maximum_concurrent_requests": SIMULATE_API_MAX_CONCURRENT_REQUESTS,
            },
        )
    try:
        future = _SIMULATE_EXECUTOR.submit(
            run_t1_sweep,
            parameters=request.parameters,
            maximum_delay_us=request.maximum_delay_us,
            point_count=request.point_count,
            simulation_backend=request.simulation_backend,
            evolution_method=request.evolution_method,
            readout_error=request.readout_error,
        )
    except Exception:
        _SIMULATE_EXECUTION_SLOTS.release()
        raise
    future.add_done_callback(lambda completed: _SIMULATE_EXECUTION_SLOTS.release())
    try:
        return future.result(timeout=SIMULATE_API_TIMEOUT_SECONDS)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise HTTPException(
            status_code=504,
            detail={
                "message": "T1 sweep timed out.",
                "timeout_seconds": SIMULATE_API_TIMEOUT_SECONDS,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=_failure_detail("T1 sweep request is invalid.", exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("T1 sweep failed.")
        raise HTTPException(
            status_code=500,
            detail=_failure_detail("T1 sweep failed.", exc),
        ) from exc


@app.post(
    "/api/analysis/ramsey-sweep",
    response_model=RamseySweepResponse,
    responses={
        422: {"description": "Invalid Ramsey sweep request."},
        503: {"description": "Simulation capacity is busy."},
        504: {"description": "Ramsey sweep exceeded the API timeout."},
    },
)
def ramsey_sweep(request: RamseySweepRequest) -> dict[str, object]:
    """Run a bounded Ramsey sweep and compare exponential/Gaussian envelopes."""

    if not _SIMULATE_EXECUTION_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Simulation capacity is busy.",
                "maximum_concurrent_requests": SIMULATE_API_MAX_CONCURRENT_REQUESTS,
            },
        )
    try:
        future = _SIMULATE_EXECUTOR.submit(
            run_ramsey_sweep,
            parameters=request.parameters,
            maximum_delay_us=request.maximum_delay_us,
            point_count=request.point_count,
            detuning_mhz=request.detuning_mhz,
            simulation_backend=request.simulation_backend,
            evolution_method=request.evolution_method,
            readout_error=request.readout_error,
        )
    except Exception:
        _SIMULATE_EXECUTION_SLOTS.release()
        raise
    future.add_done_callback(lambda completed: _SIMULATE_EXECUTION_SLOTS.release())
    try:
        return future.result(timeout=SIMULATE_API_TIMEOUT_SECONDS)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise HTTPException(
            status_code=504,
            detail={
                "message": "Ramsey sweep timed out.",
                "timeout_seconds": SIMULATE_API_TIMEOUT_SECONDS,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=_failure_detail("Ramsey sweep request is invalid.", exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Ramsey sweep failed.")
        raise HTTPException(
            status_code=500,
            detail=_failure_detail("Ramsey sweep failed.", exc),
        ) from exc


def _run_process_characterization(request: ProcessCharacterizationRequest) -> dict[str, object]:
    return characterize_process(
        build_config_from_simulate_request(request),
        monte_carlo_samples=request.monte_carlo_samples,
        monte_carlo_seed=request.monte_carlo_seed,
        include_process_matrix=request.include_process_matrix,
    )


@app.post(
    "/api/analysis/process-characterization",
    responses={
        422: {"description": "The circuit has no unitary ideal process or is too large."},
        503: {"description": "Simulation capacity is busy."},
        504: {"description": "Process characterization exceeded the API timeout."},
    },
)
def process_characterization(request: ProcessCharacterizationRequest) -> dict[str, object]:
    """Average gate fidelity, plus the chi matrix up to three qubits."""

    if (
        PUBLIC_MAX_LOGICAL_QUBITS is not None
        and request.circuit_config is not None
        and request.circuit_config.logical_qubits > PUBLIC_MAX_LOGICAL_QUBITS
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "logical_qubits exceeds the public demo limit.",
                "maximum_logical_qubits": PUBLIC_MAX_LOGICAL_QUBITS,
            },
        )
    if not _SIMULATE_EXECUTION_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Simulation capacity is busy.",
                "maximum_concurrent_requests": SIMULATE_API_MAX_CONCURRENT_REQUESTS,
            },
        )
    try:
        future = _SIMULATE_EXECUTOR.submit(_run_process_characterization, request)
    except Exception:
        _SIMULATE_EXECUTION_SLOTS.release()
        raise
    future.add_done_callback(lambda completed: _SIMULATE_EXECUTION_SLOTS.release())
    try:
        return future.result(timeout=SIMULATE_API_TIMEOUT_SECONDS)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise HTTPException(
            status_code=504,
            detail={
                "message": "Process characterization timed out.",
                "timeout_seconds": SIMULATE_API_TIMEOUT_SECONDS,
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=_failure_detail("Process characterization request is invalid.", exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Process characterization failed.")
        raise HTTPException(
            status_code=500,
            detail=_failure_detail("Process characterization failed.", exc),
        ) from exc


@app.post(
    PULSE_TOOLBOX_PATH,
    responses={
        422: {"description": "Invalid Pulse toolbox request."},
    },
)
def pulse_toolbox(request: PulseToolboxRequest) -> dict[str, object]:
    """Entropy, entanglement and Pauli expectations on a Pulse run's density matrices.

    The work is a handful of small eigenvalue problems per point and the request
    size is bounded, so it runs inline instead of taking a simulation slot.
    """

    dimension = request.local_levels**request.transmon_count
    if len(request.times_us) != len(request.density_matrices):
        raise HTTPException(
            status_code=422,
            detail={"message": "times_us and density_matrices must have the same length."},
        )
    if len(request.density_matrices) * dimension * dimension > PULSE_TOOLBOX_MAX_MATRIX_ELEMENTS:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Too many density-matrix elements for one toolbox request.",
                "maximum_matrix_elements": PULSE_TOOLBOX_MAX_MATRIX_ELEMENTS,
            },
        )
    if not _PULSE_TOOLBOX_EXECUTION_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Pulse toolbox capacity is busy.",
                "maximum_concurrent_requests": PULSE_TOOLBOX_MAX_CONCURRENT_REQUESTS,
            },
        )
    try:
        matrices = [
            density_from_upper_triangle(matrix.upper_real, matrix.upper_imag, dimension)
            for matrix in request.density_matrices
        ]
        return compute_pulse_toolbox_metrics(
            request.to_spec(),
            request.local_levels,
            request.transmon_count,
            request.times_us,
            matrices,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=_failure_detail("Pulse toolbox request is invalid.", exc),
        ) from exc
    except Exception as exc:
        logger.exception("Pulse toolbox failed.")
        raise HTTPException(
            status_code=500,
            detail=_failure_detail("Pulse toolbox failed.", exc),
        ) from exc
    finally:
        _PULSE_TOOLBOX_EXECUTION_SLOTS.release()


def _run_simulate_request(request: SimulateRequest) -> dict[str, object]:
    request_started_at = perf_counter()
    try:
        config_started_at = perf_counter()
        config = build_config_from_simulate_request(request)
        build_config_ms = (perf_counter() - config_started_at) * 1000.0

        run_started_at = perf_counter()
        result = run_simulation(config)
        run_simulation_ms = (perf_counter() - run_started_at) * 1000.0

        ui_response_started_at = perf_counter()
        response = simulation_result_to_ui_response(result)
        ui_response_ms = (perf_counter() - ui_response_started_at) * 1000.0

        if request.toolbox is not None:
            comparison = (response.get("run") or {}).get("comparison") or {}
            response["toolbox"] = compute_toolbox_snapshot_metrics(
                request.toolbox.to_spec(),
                int(config.circuit.logical_qubits),
                response.get("state_snapshots") or [],
                comparison.get("ideal_state_snapshots") or [],
            )

        total_request_ms = (perf_counter() - request_started_at) * 1000.0
        diagnostics = response.get("diagnostics")
        if isinstance(diagnostics, dict):
            diagnostics.update({
                "api_build_config_ms": round(build_config_ms, 3),
                "api_run_simulation_ms": round(run_simulation_ms, 3),
                "api_ui_response_ms": round(ui_response_ms, 3),
                "api_total_request_ms": round(total_request_ms, 3),
                "api_logical_qubits": int(config.circuit.logical_qubits),
                "api_circuit_column_count": len(config.circuit.columns),
                "api_circuit_gate_count": sum(
                    len(column.gates) for column in config.circuit.columns
                ),
                "api_time_steps": int(config.time_steps),
                "api_duration_us": float(config.duration_us),
            })
            if request.animation_parameter is not None:
                diagnostics["animation_parameter"] = {
                    "name": request.animation_parameter.name,
                    "value": float(request.animation_parameter.value),
                    "column_index": int(request.animation_parameter.column_index),
                    "gate_index": int(request.animation_parameter.gate_index),
                }

        return response
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Simulation failed.")
        raise HTTPException(
            status_code=500,
            detail=_failure_detail("Simulation failed.", exc),
        ) from exc


@app.post(
    "/api/simulate",
    responses={
        422: {"description": "Invalid or over-budget simulation request."},
        503: {"description": "Simulation capacity is busy."},
        504: {"description": "Simulation exceeded the API timeout."},
    },
)
def simulate(request: SimulateRequest) -> dict[str, object]:
    if (
        PUBLIC_MAX_LOGICAL_QUBITS is not None
        and request.circuit_config is not None
        and request.circuit_config.logical_qubits > PUBLIC_MAX_LOGICAL_QUBITS
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "message": "logical_qubits exceeds the public demo limit.",
                "maximum_logical_qubits": PUBLIC_MAX_LOGICAL_QUBITS,
            },
        )

    if not _SIMULATE_EXECUTION_SLOTS.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Simulation capacity is busy.",
                "maximum_concurrent_requests": (
                    SIMULATE_API_MAX_CONCURRENT_REQUESTS
                ),
            },
        )
    try:
        future = _SIMULATE_EXECUTOR.submit(_run_simulate_request, request)
    except Exception:
        _SIMULATE_EXECUTION_SLOTS.release()
        raise
    future.add_done_callback(
        lambda completed: _SIMULATE_EXECUTION_SLOTS.release()
    )
    try:
        return future.result(timeout=SIMULATE_API_TIMEOUT_SECONDS)
    except FuturesTimeoutError as exc:
        future.cancel()
        raise HTTPException(
            status_code=504,
            detail={
                "message": "Simulation timed out.",
                "timeout_seconds": SIMULATE_API_TIMEOUT_SECONDS,
                "previous_results_preserved": True,
            },
        ) from exc

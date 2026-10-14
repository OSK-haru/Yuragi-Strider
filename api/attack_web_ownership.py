"""Opt-in browser ownership for attack-simulation environments.

The opaque environment cookie is a bearer capability. Only its SHA-256 digest
is retained by the process; environment assembly remains an injected trusted
callback and no route is mounted by this module.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from threading import RLock
from typing import Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, WebSocket
from starlette.responses import JSONResponse, Response

from api.attack_live_websocket import PausedWorldWebSocketRegistry
from api.attack_paused_world import PausedWorldController
from api.attack_host_clock_world import HostClockWorldController
from api.attack_product_factory import ProductAttackEnvironment
from api.attack_sessions import SessionCapacityError, WebOwnerContext


DEFAULT_IDLE_TIMEOUT_SECONDS = 30 * 60
DEFAULT_ABSOLUTE_TIMEOUT_SECONDS = 24 * 60 * 60
DEFAULT_MAX_ENVIRONMENTS_PER_BROWSER = 1
DEFAULT_MAX_ENVIRONMENTS_TOTAL = 8
DEFAULT_COOKIE_PREFIX = "quanyu_attack"
# The public profile is deliberately small: every environment holds a live
# numerical world in this one process. Operators may lower these, never raise
# them past the ceilings below.
PUBLIC_MAX_ENVIRONMENTS_TOTAL = 4
PUBLIC_MAX_ENVIRONMENTS_TOTAL_CEILING = 8
PUBLIC_IDLE_TIMEOUT_SECONDS = 15 * 60
PUBLIC_ABSOLUTE_TIMEOUT_SECONDS = 60 * 60
PUBLIC_CREATES_PER_MINUTE = 6
PUBLIC_CONCURRENT_EVALUATIONS = 1
CSRF_HEADER = "x-attack-simulator-request"
CSRF_HEADER_VALUE = "1"
COOKIE_PATH = "/api/attack"
MAX_SCENARIO_CREATE_BYTES = 64 * 1024
_ENVIRONMENT_ID = re.compile(r"[a-f0-9]{32}\Z")
_HOST_LABEL = re.compile(r"[a-z0-9.-]+\Z", re.IGNORECASE)


class EnvironmentNotFound(PermissionError):
    """Missing, expired, malformed and not-owned environments are equivalent."""


class RequestOriginHostRejected(PermissionError):
    """The browser request did not match the explicit origin/host policy."""


class EnvironmentConfigurationRejected(ValueError):
    """Untrusted scenario-selection input failed its public schema checks."""


@dataclass(frozen=True)
class WebOwnershipSettings:
    allowed_origins: tuple[str, ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    max_environments_per_browser: int = DEFAULT_MAX_ENVIRONMENTS_PER_BROWSER
    max_environments_total: int = DEFAULT_MAX_ENVIRONMENTS_TOTAL
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS
    absolute_timeout_seconds: int = DEFAULT_ABSOLUTE_TIMEOUT_SECONDS
    cookie_prefix: str = DEFAULT_COOKIE_PREFIX
    startup_nonce: str = field(default_factory=lambda: secrets.token_hex(8))
    # Global (not per-client) admission limits; zero disables a limit. Behind a
    # proxy the client address is not trustworthy, so these cap total load.
    creates_per_minute: int = 0
    max_concurrent_evaluations: int = 0
    # Non-browser clients (a Python script on the owner's machine) may act as the
    # owner with a script token issued from the browser. Local profiles only.
    script_access: bool = False

    def __post_init__(self) -> None:
        if type(self.script_access) is not bool:
            raise ValueError("script_access must be a boolean")
        for name in ("creates_per_minute", "max_concurrent_evaluations"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 1000:
                raise ValueError(f"{name} must be between 0 and 1000")
        if type(self.max_environments_per_browser) is not int or not 1 <= self.max_environments_per_browser <= 64:
            raise ValueError("max_environments_per_browser must be between 1 and 64")
        if type(self.max_environments_total) is not int or not 1 <= self.max_environments_total <= 64:
            raise ValueError("max_environments_total must be between 1 and 64")
        for name, value in (("idle_timeout_seconds", self.idle_timeout_seconds),
                            ("absolute_timeout_seconds", self.absolute_timeout_seconds)):
            if type(value) is not int or not 1 <= value <= 31_536_000:
                raise ValueError(f"{name} must be between 1 second and one year")
        if self.idle_timeout_seconds > self.absolute_timeout_seconds:
            raise ValueError("idle timeout cannot exceed absolute timeout")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", self.cookie_prefix):
            raise ValueError("cookie_prefix contains unsupported characters")
        if not re.fullmatch(r"[a-f0-9]{16,32}", self.startup_nonce):
            raise ValueError("startup_nonce must be a 16-32 character lowercase hex value")
        for origin in self.allowed_origins:
            if not _parse_origin(origin):
                raise ValueError("allowed_origins must contain exact HTTP(S) origins")
        for host in self.allowed_hosts:
            if not _valid_host_name(host):
                raise ValueError("allowed_hosts must contain host names without ports")

    @classmethod
    def local_development(cls, *, api_port: int = 8001, ui_port: int = 5173) -> "WebOwnershipSettings":
        if any(type(port) is not int or not 1 <= port <= 65535 for port in (api_port, ui_port)):
            raise ValueError("local server ports must be valid TCP ports")
        origins = tuple(f"http://{host}:{port}" for host in ("127.0.0.1", "localhost")
                        for port in (api_port, ui_port))
        return cls(allowed_origins=origins, allowed_hosts=("127.0.0.1", "localhost"),
                   max_environments_per_browser=4, script_access=True)

    @classmethod
    def desktop(cls, host: str, port: int) -> "WebOwnershipSettings":
        normalized = host.lower().strip("[]")
        if normalized not in {"127.0.0.1", "localhost"}:
            raise ValueError("desktop ownership must bind to loopback")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("desktop port must be a valid TCP port")
        return cls(allowed_origins=(f"http://{normalized}:{port}",),
                   allowed_hosts=(normalized,), max_environments_per_browser=4, script_access=True)

    @classmethod
    def public(cls, *, allowed_origins: tuple[str, ...], allowed_hosts: tuple[str, ...],
               max_environments_total: int = PUBLIC_MAX_ENVIRONMENTS_TOTAL,
               idle_timeout_seconds: int = PUBLIC_IDLE_TIMEOUT_SECONDS,
               absolute_timeout_seconds: int = PUBLIC_ABSOLUTE_TIMEOUT_SECONDS) -> "WebOwnershipSettings":
        """Internet-facing settings: HTTPS origins only, small caps, admission limits."""
        if not allowed_origins or not allowed_hosts:
            raise ValueError("the public profile requires explicit origin and host allowlists")
        if any(not origin.startswith("https://") for origin in allowed_origins):
            raise ValueError("the public profile accepts HTTPS origins only")
        if not 1 <= max_environments_total <= PUBLIC_MAX_ENVIRONMENTS_TOTAL_CEILING:
            raise ValueError("public environment capacity is above the allowed ceiling")
        if idle_timeout_seconds > PUBLIC_ABSOLUTE_TIMEOUT_SECONDS or absolute_timeout_seconds > PUBLIC_ABSOLUTE_TIMEOUT_SECONDS:
            raise ValueError("public environments may not live longer than one hour")
        return cls(
            allowed_origins=tuple(allowed_origins), allowed_hosts=tuple(allowed_hosts),
            max_environments_per_browser=1, max_environments_total=max_environments_total,
            idle_timeout_seconds=idle_timeout_seconds, absolute_timeout_seconds=absolute_timeout_seconds,
            creates_per_minute=PUBLIC_CREATES_PER_MINUTE,
            max_concurrent_evaluations=PUBLIC_CONCURRENT_EVALUATIONS,
        )

    @classmethod
    def from_environment(cls) -> "WebOwnershipSettings":
        """Resolve an explicit profile; never infer publication from ALLOWED_ORIGINS."""
        profile = os.environ.get("ATTACK_SIMULATOR_PROFILE", "generic").strip().lower()
        if profile == "public":
            return cls.public(
                allowed_origins=_environment_list("ATTACK_SIMULATOR_ALLOWED_ORIGINS"),
                allowed_hosts=_environment_list("ATTACK_SIMULATOR_ALLOWED_HOSTS"),
                max_environments_total=_environment_int(
                    "ATTACK_SIMULATOR_MAX_ENVIRONMENTS_TOTAL", PUBLIC_MAX_ENVIRONMENTS_TOTAL),
                idle_timeout_seconds=_environment_int(
                    "ATTACK_SIMULATOR_IDLE_TIMEOUT_SECONDS", PUBLIC_IDLE_TIMEOUT_SECONDS),
                absolute_timeout_seconds=_environment_int(
                    "ATTACK_SIMULATOR_ABSOLUTE_TIMEOUT_SECONDS", PUBLIC_ABSOLUTE_TIMEOUT_SECONDS),
            )
        if profile == "development":
            return cls.local_development(
                api_port=_environment_int("ATTACK_SIMULATOR_API_PORT", 8001),
                ui_port=_environment_int("ATTACK_SIMULATOR_UI_PORT", 5173),
            )
        if profile != "generic":
            raise ValueError("ATTACK_SIMULATOR_PROFILE must be generic, development or public")
        return cls(
            allowed_origins=_environment_list("ATTACK_SIMULATOR_ALLOWED_ORIGINS"),
            allowed_hosts=_environment_list("ATTACK_SIMULATOR_ALLOWED_HOSTS"),
            max_environments_per_browser=_environment_int(
                "ATTACK_SIMULATOR_MAX_ENVIRONMENTS_PER_BROWSER", DEFAULT_MAX_ENVIRONMENTS_PER_BROWSER),
            max_environments_total=_environment_int(
                "ATTACK_SIMULATOR_MAX_ENVIRONMENTS_TOTAL", DEFAULT_MAX_ENVIRONMENTS_TOTAL),
            idle_timeout_seconds=_environment_int(
                "ATTACK_SIMULATOR_IDLE_TIMEOUT_SECONDS", DEFAULT_IDLE_TIMEOUT_SECONDS),
            absolute_timeout_seconds=_environment_int(
                "ATTACK_SIMULATOR_ABSOLUTE_TIMEOUT_SECONDS", DEFAULT_ABSOLUTE_TIMEOUT_SECONDS),
        )


def _environment_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None


def _environment_list(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


def _valid_host_name(value: str) -> bool:
    if type(value) is not str or not value or len(value) > 253 or not _HOST_LABEL.fullmatch(value):
        return False
    if value.startswith(".") or value.endswith(".") or ".." in value:
        return False
    return True


def _parse_origin(value: str):
    if type(value) is not str:
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment):
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return parsed


def _request_host_only(request: Request | WebSocket, settings: WebOwnershipSettings) -> None:
    hosts = request.headers.getlist("host")
    if len(hosts) != 1:
        raise RequestOriginHostRejected
    try:
        parsed_host = urlsplit("//" + hosts[0])
        port = parsed_host.port
    except ValueError:
        raise RequestOriginHostRejected from None
    hostname = (parsed_host.hostname or "").lower().rstrip(".")
    if (not _valid_host_name(hostname) or parsed_host.username is not None
            or parsed_host.password is not None or parsed_host.path
            or port is not None and not 1 <= port <= 65535
            or hostname not in {host.lower().rstrip(".") for host in settings.allowed_hosts}):
        raise RequestOriginHostRejected


def _request_origin_and_host(request: Request | WebSocket, settings: WebOwnershipSettings) -> str:
    origins = request.headers.getlist("origin")
    hosts = request.headers.getlist("host")
    if len(origins) != 1 or len(hosts) != 1:
        raise RequestOriginHostRejected
    origin = origins[0]
    parsed_origin = _parse_origin(origin)
    if parsed_origin is None or origin not in settings.allowed_origins:
        raise RequestOriginHostRejected
    try:
        parsed_host = urlsplit("//" + hosts[0])
        port = parsed_host.port
    except ValueError:
        raise RequestOriginHostRejected from None
    hostname = (parsed_host.hostname or "").lower().rstrip(".")
    if (not _valid_host_name(hostname) or parsed_host.username is not None
            or parsed_host.password is not None or parsed_host.path
            or port is not None and not 1 <= port <= 65535
            or hostname not in {host.lower().rstrip(".") for host in settings.allowed_hosts}
            or parsed_origin.hostname is None
            or parsed_origin.hostname.lower().rstrip(".") not in {
                host.lower().rstrip(".") for host in settings.allowed_hosts
            }):
        raise RequestOriginHostRejected
    return origin


@dataclass
class _EnvironmentOwner:
    environment_id: str
    cookie_name: str
    token_hash: bytes
    context: WebOwnerContext
    created_at: float
    last_seen: float
    created_wall_at: str
    mode: str | None
    attacker_credential: str | None
    closing: bool = False
    evaluator: object | None = None
    script_token_hash: bytes | None = None


class BrowserEnvironmentManager:
    """Owner-cookie records plus trusted environment lifecycle callbacks."""

    def __init__(self, websocket_registry: PausedWorldWebSocketRegistry, *,
                 environment_factory: Callable[
                     [WebOwnerContext], PausedWorldController | HostClockWorldController | ProductAttackEnvironment
                 ] | None = None,
                 request_environment_factory: Callable[
                     [WebOwnerContext, bytes], ProductAttackEnvironment
                 ] | None = None,
                 settings: WebOwnershipSettings, clock: Callable[[], float] = time.monotonic):
        if type(websocket_registry) is not PausedWorldWebSocketRegistry:
            raise TypeError("a paused-world registry is required")
        if ((environment_factory is None) == (request_environment_factory is None)
                or environment_factory is not None and not callable(environment_factory)
                or request_environment_factory is not None and not callable(request_environment_factory)
                or not callable(clock)):
            raise TypeError("configure exactly one trusted factory and a monotonic clock")
        if not isinstance(settings, WebOwnershipSettings):
            raise TypeError("explicit web ownership settings are required")
        if not settings.allowed_origins or not settings.allowed_hosts:
            raise ValueError("explicit origin and host allowlists are required")
        self._registry = websocket_registry
        self._factory = environment_factory
        self._request_factory = request_environment_factory
        self.settings = settings
        self._clock = clock
        self._records: dict[str, _EnvironmentOwner] = {}
        self._reservations: Counter[WebOwnerContext] = Counter()
        self._global_reservations = 0
        self._lock = RLock()
        self._expiry_handles: dict[str, asyncio.TimerHandle] = {}
        self._create_times: list[float] = []
        self._evaluations_running = 0

    def cookie_name(self, environment_id: str) -> str:
        return f"{self.settings.cookie_prefix}_{self.settings.startup_nonce}_{environment_id}"

    def resolve_owner(self, websocket: WebSocket) -> WebOwnerContext:
        """Trusted callable for create_live_attack_websocket_router."""
        if not isinstance(websocket, WebSocket):
            raise TypeError("WebSocket ownership resolution requires an ASGI WebSocket")
        environment_id = websocket.path_params.get("world_id")
        bearer = self._resolve_script_bearer(websocket, environment_id)
        if bearer is not None:
            return bearer
        _request_origin_and_host(websocket, self.settings)
        cookies = websocket.cookies
        context, expired = self._resolve_cookie(environment_id, cookies)
        self._schedule_discard(expired)
        if context is None:
            raise EnvironmentNotFound
        return context

    def touch_owner(self, websocket: WebSocket) -> WebOwnerContext:
        """Revalidate and extend idle lifetime when a WebSocket command arrives."""
        return self.resolve_owner(websocket)

    def resolve_http_owner(self, request: Request) -> WebOwnerContext:
        """Trusted resolver for owned HTTP operations; unsafe verbs require CSRF header."""
        if not isinstance(request, Request):
            raise TypeError("HTTP ownership resolution requires an ASGI Request")
        params = request.path_params
        environment_id = (params.get("world_id") or params.get("environment_id")
                          or params.get("session_id"))
        bearer = self._resolve_script_bearer(request, environment_id)
        if bearer is not None:
            return bearer
        _request_origin_and_host(request, self.settings)
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            _require_csrf_header(request)
        context, expired = self._resolve_cookie(environment_id, dict(request.cookies))
        self._schedule_discard(expired)
        if context is None:
            raise EnvironmentNotFound
        return context

    def _resolve_script_bearer(self, connection: Request | WebSocket, environment_id: object):
        """Owner context for a script token, or None when no Authorization header is sent.

        A header that is present but wrong never falls back to cookies."""
        values = connection.headers.getlist("authorization")
        if not values:
            return None
        if not self.settings.script_access:
            raise EnvironmentNotFound
        _request_host_only(connection, self.settings)
        scheme, _, token = values[0].partition(" ")
        if (len(values) != 1 or scheme != "Bearer" or not token
                or type(environment_id) is not str or not _ENVIRONMENT_ID.fullmatch(environment_id)):
            raise EnvironmentNotFound
        supplied = hashlib.sha256(token.encode("utf-8")).digest()
        now = self._clock()
        expired = []
        with self._lock:
            record = self._records.get(environment_id)
            if record is not None and self._is_expired(record, now):
                record.closing = True
                expired.append(environment_id)
            valid = (record is not None and not record.closing and record.script_token_hash is not None
                     and hmac.compare_digest(record.script_token_hash, supplied))
            if valid:
                record.last_seen = now
                context = record.context
        self._schedule_discard(expired)
        if not valid:
            raise EnvironmentNotFound
        self._schedule_expiry(environment_id)
        return context

    async def issue_script_token(self, request: Request, environment_id: str) -> str:
        """Issue (and replace) the script token for the cookie-proven owner."""
        if not self.settings.script_access:
            raise EnvironmentNotFound
        _request_origin_and_host(request, self.settings)
        _require_csrf_header(request)
        context, expired = self._resolve_cookie(environment_id, dict(request.cookies))
        await self._dispose_many(expired)
        if context is None:
            raise EnvironmentNotFound
        token = secrets.token_urlsafe(32)
        with self._lock:
            record = self._records.get(environment_id)
            if record is None or record.closing or record.context is not context:
                raise EnvironmentNotFound
            record.script_token_hash = hashlib.sha256(token.encode("utf-8")).digest()
        self._schedule_expiry(environment_id)
        return token

    def _resolve_cookie(self, environment_id: object, cookies: dict[str, str]):
        expired = []
        if type(environment_id) is not str or not _ENVIRONMENT_ID.fullmatch(environment_id):
            return None, expired
        name = self.cookie_name(environment_id)
        candidate = cookies.get(name)
        now = self._clock()
        with self._lock:
            record = self._records.get(environment_id)
            if record is not None and self._is_expired(record, now):
                record.closing = True
                expired.append(environment_id)
            if record is None or record.closing or type(candidate) is not str:
                return None, expired
            supplied_hash = hashlib.sha256(candidate.encode("utf-8")).digest()
            if not hmac.compare_digest(record.token_hash, supplied_hash):
                return None, expired
            record.last_seen = now
            context = record.context
        self._schedule_expiry(environment_id)
        return context, expired

    def _is_expired(self, record: _EnvironmentOwner, now: float) -> bool:
        return (now - record.last_seen >= self.settings.idle_timeout_seconds
                or now - record.created_at >= self.settings.absolute_timeout_seconds)

    def _schedule_discard(self, environment_ids: list[str]) -> None:
        if not environment_ids:
            return
        with self._lock:
            for environment_id in environment_ids:
                handle = self._expiry_handles.pop(environment_id, None)
                if handle is not None:
                    handle.cancel()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for environment_id in environment_ids:
            loop.create_task(self._discard_registered(environment_id))

    def _schedule_expiry(self, environment_id: str) -> None:
        """Schedule real lifecycle cleanup without advancing simulated time."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with self._lock:
            record = self._records.get(environment_id)
            if record is None or record.closing:
                return
            previous = self._expiry_handles.pop(environment_id, None)
            if previous is not None:
                previous.cancel()
            absolute_due = record.created_at + self.settings.absolute_timeout_seconds
            idle_due = record.last_seen + self.settings.idle_timeout_seconds
            delay = max(0.0, min(absolute_due, idle_due) - self._clock())
            self._expiry_handles[environment_id] = loop.call_later(
                delay, lambda: loop.create_task(self._expire_if_due(environment_id))
            )

    async def _expire_if_due(self, environment_id: str) -> None:
        with self._lock:
            self._expiry_handles.pop(environment_id, None)
            record = self._records.get(environment_id)
            if record is None:
                return
            if not self._is_expired(record, self._clock()):
                reschedule = True
            else:
                record.closing = True
                reschedule = False
        if reschedule:
            self._schedule_expiry(environment_id)
        else:
            await self._discard_registered(environment_id)

    async def _discard_registered(self, environment_id: str) -> None:
        try:
            await self._registry.discard(environment_id)
        except LookupError:
            pass
        with self._lock:
            self._records.pop(environment_id, None)

    def _valid_browser_context(self, cookies: dict[str, str], now: float):
        expired = []
        prefix = f"{self.settings.cookie_prefix}_{self.settings.startup_nonce}_"
        contexts: dict[int, tuple[WebOwnerContext, int]] = {}
        with self._lock:
            for environment_id, record in tuple(self._records.items()):
                if self._is_expired(record, now):
                    record.closing = True
                    expired.append(environment_id)
            for name, candidate in cookies.items():
                if not name.startswith(prefix):
                    continue
                environment_id = name[len(prefix):]
                if not _ENVIRONMENT_ID.fullmatch(environment_id):
                    continue
                record = self._records.get(environment_id)
                if record is None:
                    continue
                if self._is_expired(record, now):
                    record.closing = True
                    expired.append(environment_id)
                    continue
                if not hmac.compare_digest(record.token_hash,
                                           hashlib.sha256(candidate.encode("utf-8")).digest()):
                    continue
                if not record.closing:
                    record.last_seen = now
                key = id(record.context)
                context, count = contexts.get(key, (record.context, 0))
                contexts[key] = (context, count + 1)
            if len(contexts) > 1:
                return None, 0, expired, True
            if not contexts:
                return None, 0, expired, False
            context, _ = next(iter(contexts.values()))
            owned_count = sum(1 for record in self._records.values() if record.context is context)
            return context, owned_count, expired, False

    async def create_environment(self, request: Request) -> tuple[str, str, bool, str | None]:
        origin = _request_origin_and_host(request, self.settings)
        _require_csrf_header(request)
        request_document = await self._read_creation_request(request) if self._request_factory else None
        expired: list[str] = []
        now = self._clock()
        context, _owned_count, expired, ambiguous = self._valid_browser_context(dict(request.cookies), now)
        await self._dispose_many(expired)
        if ambiguous:
            raise HTTPException(status_code=400, detail={"code": "OWNER_COOKIE_AMBIGUOUS"})
        context = context or WebOwnerContext()
        with self._lock:
            if self.settings.creates_per_minute:
                cutoff = now - 60.0
                self._create_times = [item for item in self._create_times if item > cutoff]
                if len(self._create_times) >= self.settings.creates_per_minute:
                    raise HTTPException(status_code=429, detail={"code": "CREATE_RATE_LIMIT"},
                                        headers={"Retry-After": "30"})
            current_count = sum(1 for record in self._records.values() if record.context is context)
            current_count += self._reservations[context]
            if current_count >= self.settings.max_environments_per_browser:
                raise HTTPException(status_code=429, detail={"code": "ENVIRONMENT_LIMIT"})
            if len(self._records) + self._global_reservations >= self.settings.max_environments_total:
                raise HTTPException(status_code=503, detail={"code": "ENVIRONMENT_CAPACITY"})
            self._reservations[context] += 1
            self._global_reservations += 1
            self._create_times.append(now)

        environment_id = secrets.token_hex(16)
        controller = None
        registered = False
        product = None
        try:
            if self._request_factory is not None:
                assembled = await asyncio.to_thread(self._request_factory, context, request_document)
            else:
                assembled = await asyncio.to_thread(self._factory, context)
            if type(assembled) is ProductAttackEnvironment:
                product = assembled
                controller = product.controller
                attacker_credential = product.attacker_credential
                if (type(controller) not in {PausedWorldController, HostClockWorldController}
                        or type(attacker_credential) is not str or not attacker_credential):
                    raise TypeError("product factory returned an incomplete attack environment")
            elif type(assembled) in {PausedWorldController, HostClockWorldController}:
                # Retain the owner-only adapter for lifecycle tests and existing
                # internal callers; product routes return credentials only for
                # the full frozen-manifest assembly result.
                controller = assembled
                attacker_credential = None
            else:
                raise TypeError("environment factory must return a product environment")
            self._registry.register(environment_id, controller)
            registered = True
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("ascii")).digest()
            created = self._clock()
            record = _EnvironmentOwner(environment_id, self.cookie_name(environment_id), token_hash,
                                       context, created, created,
                                       datetime.now(timezone.utc).isoformat(),
                                       product.mode.value if product is not None else None,
                                       attacker_credential,
                                       evaluator=product.evaluator if product is not None else None)
            with self._lock:
                self._records[environment_id] = record
            self._schedule_expiry(environment_id)
            return environment_id, token, origin.startswith("https://"), attacker_credential
        except EnvironmentConfigurationRejected:
            with self._lock:
                self._records.pop(environment_id, None)
                handle = self._expiry_handles.pop(environment_id, None)
                if handle is not None:
                    handle.cancel()
            if registered:
                await self._discard_registered(environment_id)
            elif product is not None:
                await asyncio.to_thread(product.close)
            elif type(controller) in {PausedWorldController, HostClockWorldController}:
                await asyncio.to_thread(controller.close)
            raise HTTPException(status_code=422, detail={"code": "INVALID_SETTINGS"}) from None
        except SessionCapacityError:
            with self._lock:
                self._records.pop(environment_id, None)
                handle = self._expiry_handles.pop(environment_id, None)
                if handle is not None:
                    handle.cancel()
            if registered:
                await self._discard_registered(environment_id)
            elif product is not None:
                await asyncio.to_thread(product.close)
            elif type(controller) in {PausedWorldController, HostClockWorldController}:
                await asyncio.to_thread(controller.close)
            raise HTTPException(status_code=503, detail={"code": "ENVIRONMENT_CAPACITY"}) from None
        except Exception:
            with self._lock:
                self._records.pop(environment_id, None)
                handle = self._expiry_handles.pop(environment_id, None)
                if handle is not None:
                    handle.cancel()
            if registered:
                await self._discard_registered(environment_id)
            elif product is not None:
                await asyncio.to_thread(product.close)
            elif type(controller) in {PausedWorldController, HostClockWorldController}:
                await asyncio.to_thread(controller.close)
            raise HTTPException(status_code=503, detail={"code": "ENVIRONMENT_UNAVAILABLE"}) from None
        finally:
            with self._lock:
                self._reservations[context] -= 1
                if self._reservations[context] <= 0:
                    del self._reservations[context]
                self._global_reservations -= 1

    async def _read_creation_request(self, request: Request) -> bytes:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(status_code=415, detail={"code": "JSON_REQUIRED"})
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
                if declared_length < 0:
                    raise ValueError
                if declared_length > MAX_SCENARIO_CREATE_BYTES:
                    raise HTTPException(status_code=413, detail={"code": "SETTINGS_TOO_LARGE"})
            except ValueError:
                raise HTTPException(status_code=400, detail={"code": "INVALID_SETTINGS"}) from None
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunks) + len(chunk) > MAX_SCENARIO_CREATE_BYTES:
                raise HTTPException(status_code=413, detail={"code": "SETTINGS_TOO_LARGE"})
            chunks.extend(chunk)
        return bytes(chunks)

    async def delete_environment(self, request: Request, environment_id: str) -> None:
        _request_origin_and_host(request, self.settings)
        _require_csrf_header(request)
        context, expired = self._resolve_cookie(environment_id, dict(request.cookies))
        await self._dispose_many(expired)
        if context is None:
            raise EnvironmentNotFound
        with self._lock:
            record = self._records.get(environment_id)
            if record is None or record.closing or record.context is not context:
                raise EnvironmentNotFound
            record.closing = True
            handle = self._expiry_handles.pop(environment_id, None)
            if handle is not None:
                handle.cancel()
        await self._discard_registered(environment_id)

    async def list_environments(self, request: Request) -> list[dict[str, str]]:
        """List only cookie-proven, live product environments; never expose secrets."""
        _request_origin_and_host(request, self.settings)
        _require_csrf_header(request)
        now = self._clock()
        context, _, expired, ambiguous = self._valid_browser_context(dict(request.cookies), now)
        await self._dispose_many(expired)
        if ambiguous or context is None:
            return []
        with self._lock:
            records = tuple(record for record in self._records.values()
                            if record.context is context and not record.closing and record.mode is not None
                            and record.cookie_name in request.cookies
                            and hmac.compare_digest(record.token_hash, hashlib.sha256(
                                request.cookies[record.cookie_name].encode("utf-8")).digest()))
        for record in records:
            self._schedule_expiry(record.environment_id)
        return [{"environment_id": record.environment_id, "mode": record.mode,
                 "created_at": record.created_wall_at} for record in records]

    async def redeliver_attacker_credential(self, request: Request,
                                             environment_id: str) -> str:
        """Return the already-issued virtual credential to the proven owner only."""
        _request_origin_and_host(request, self.settings)
        _require_csrf_header(request)
        context, expired = self._resolve_cookie(environment_id, dict(request.cookies))
        await self._dispose_many(expired)
        if context is None:
            raise EnvironmentNotFound
        with self._lock:
            record = self._records.get(environment_id)
            if (record is None or record.closing or record.context is not context
                    or record.attacker_credential is None):
                raise EnvironmentNotFound
            return record.attacker_credential

    async def submit_solution(self, request: Request, environment_id: str) -> dict[str, object]:
        """Grade an estimator for the proven owner; hidden cases never leave the server."""
        context = self._resolve_script_bearer(request, environment_id)
        if context is None:
            _request_origin_and_host(request, self.settings)
            _require_csrf_header(request)
            context, expired = self._resolve_cookie(environment_id, dict(request.cookies))
            await self._dispose_many(expired)
        if context is None:
            raise EnvironmentNotFound
        with self._lock:
            record = self._records.get(environment_id)
            evaluator = None if record is None or record.closing or record.context is not context else record.evaluator
        if evaluator is None:
            raise EnvironmentNotFound
        body = await request.body()
        if not body or len(body) > 1024:
            raise HTTPException(status_code=422, detail={"code": "INVALID_SUBMISSION"})
        limit = self.settings.max_concurrent_evaluations
        with self._lock:
            if limit and self._evaluations_running >= limit:
                raise HTTPException(status_code=503, detail={"code": "EVALUATION_BUSY"},
                                    headers={"Retry-After": "5"})
            self._evaluations_running += 1
        try:
            document = json.loads(body.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            result = await asyncio.to_thread(evaluator.submit, document)
        except (ValueError, UnicodeError) as error:
            code = "SUBMISSIONS_EXHAUSTED" if "no submissions remain" in str(error) else "INVALID_SUBMISSION"
            raise HTTPException(status_code=422 if code == "INVALID_SUBMISSION" else 409,
                                detail={"code": code}) from None
        finally:
            with self._lock:
                self._evaluations_running -= 1
        self._schedule_expiry(environment_id)
        return result.to_public_dict()

    async def _dispose_many(self, environment_ids: list[str]) -> None:
        for environment_id in environment_ids:
            with self._lock:
                handle = self._expiry_handles.pop(environment_id, None)
                if handle is not None:
                    handle.cancel()
            await self._discard_registered(environment_id)

    def _set_cookie(self, response: Response, environment_id: str, token: str,
                    secure: bool) -> None:
        response.set_cookie(
            key=self.cookie_name(environment_id), value=token,
            max_age=self.settings.absolute_timeout_seconds,
            httponly=True, secure=secure, samesite="strict", path=COOKIE_PATH,
        )


def _require_csrf_header(request: Request) -> None:
    values = request.headers.getlist(CSRF_HEADER)
    if values != [CSRF_HEADER_VALUE]:
        raise HTTPException(status_code=403, detail={"code": "CSRF_HEADER_REQUIRED"})


def create_browser_environment_router(manager: BrowserEnvironmentManager) -> APIRouter:
    """Build an opt-in create/delete API; callers decide whether to mount it."""
    if type(manager) is not BrowserEnvironmentManager:
        raise TypeError("a browser environment manager is required")
    router = APIRouter(prefix="/api/attack/environments", tags=["attack-environments-candidate"])

    @router.post("/owned")
    async def list_owned_environments(request: Request):
        try:
            environments = await manager.list_environments(request)
        except RequestOriginHostRejected:
            raise HTTPException(status_code=403, detail={"code": "ORIGIN_HOST_REJECTED"}) from None
        return JSONResponse({"environments": environments}, headers={"Cache-Control": "no-store"})

    @router.post("/{environment_id}/attacker-credential")
    async def redeliver_attacker_credential(request: Request, environment_id: str):
        try:
            credential = await manager.redeliver_attacker_credential(request, environment_id)
        except RequestOriginHostRejected:
            raise HTTPException(status_code=403, detail={"code": "ORIGIN_HOST_REJECTED"}) from None
        except EnvironmentNotFound:
            raise HTTPException(status_code=404, detail={"code": "ENVIRONMENT_NOT_FOUND"}) from None
        return JSONResponse({"attacker_credential": credential}, headers={"Cache-Control": "no-store"})

    @router.post("/{environment_id}/script-token")
    async def issue_script_token(request: Request, environment_id: str):
        try:
            token = await manager.issue_script_token(request, environment_id)
        except RequestOriginHostRejected:
            raise HTTPException(status_code=403, detail={"code": "ORIGIN_HOST_REJECTED"}) from None
        except EnvironmentNotFound:
            raise HTTPException(status_code=404, detail={"code": "ENVIRONMENT_NOT_FOUND"}) from None
        return JSONResponse({"script_token": token}, headers={"Cache-Control": "no-store"})

    @router.post("/{environment_id}/submission")
    async def submit_solution(request: Request, environment_id: str):
        try:
            result = await manager.submit_solution(request, environment_id)
        except RequestOriginHostRejected:
            raise HTTPException(status_code=403, detail={"code": "ORIGIN_HOST_REJECTED"}) from None
        except EnvironmentNotFound:
            raise HTTPException(status_code=404, detail={"code": "ENVIRONMENT_NOT_FOUND"}) from None
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("")
    async def create_environment(request: Request):
        try:
            environment_id, token, secure, attacker_credential = await manager.create_environment(request)
        except RequestOriginHostRejected:
            raise HTTPException(status_code=403, detail={"code": "ORIGIN_HOST_REJECTED"}) from None
        payload = {"environment_id": environment_id}
        if attacker_credential is not None:
            payload["attacker_credential"] = attacker_credential
        response = JSONResponse(payload, status_code=201,
                                headers={"Cache-Control": "no-store"})
        manager._set_cookie(response, environment_id, token, secure)
        return response

    @router.delete("/{environment_id}", status_code=204)
    async def delete_environment(request: Request, environment_id: str):
        try:
            await manager.delete_environment(request, environment_id)
        except RequestOriginHostRejected:
            raise HTTPException(status_code=403, detail={"code": "ORIGIN_HOST_REJECTED"}) from None
        except EnvironmentNotFound:
            raise HTTPException(status_code=404, detail={"code": "ENVIRONMENT_NOT_FOUND"}) from None
        response = Response(status_code=204)
        response.delete_cookie(
            key=manager.cookie_name(environment_id), path=COOKIE_PATH,
            httponly=True, secure=_request_origin_and_host(request, manager.settings).startswith("https://"),
            samesite="strict",
        )
        return response

    return router

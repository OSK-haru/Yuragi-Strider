"""Opt-in HTTP adapter candidate for LiveWorkloadWorld; not default-mounted.

Each accepted request is captured into the world's ingress immediately, on the
event loop, so a busy worker pool never shifts the host receipt sample. The
response is only an opaque request ID; the declared reply is fetched later
once its release position has passed. No receipt, host or world time, actor
role, tenant or QPU trace is returned. Actual browser ownership (cookie, CSRF,
expiry) is still the trusted ``resolve_owner`` callable's responsibility.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
import hashlib
import json
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request

from api.attack_http import ProgramBody, _REQUEST_ID, _body, _error
from api.attack_live_driver import LiveWorldDriver
from api.attack_live_world import LiveProgramEnvelope, LiveReplyExpiredError, LiveWorkloadWorld
from api.attack_sessions import SessionCapacityError, WebOwnerContext
from api.attack_workloads import WorkloadInfrastructureError
from core.attack_simulation.job_service import JobResultNotReadyError


@dataclass
class _Handle:
    receipt: object
    principal_id: str


@dataclass
class _LiveEntry:
    world: LiveWorkloadWorld
    driver: LiveWorldDriver | None
    handle_limit: int
    lock: Lock = field(default_factory=Lock)
    handles: OrderedDict = field(default_factory=OrderedDict)
    idempotency: dict = field(default_factory=dict)  # (principal, key) -> (request_id, fingerprint)
    closed: bool = False


class LiveWorldRegistry:
    """Bounded in-process world table. Assembly/discard stay supervisor-only."""

    def __init__(self, *, max_worlds: int):
        if type(max_worlds) is not int or not 1 <= max_worlds <= 64:
            raise ValueError("max_worlds must be between 1 and 64")
        self._max_worlds = max_worlds
        self._entries: dict[str, _LiveEntry] = {}
        self._lock = Lock()

    def register(self, world_id: str, world: LiveWorkloadWorld,
                 driver: LiveWorldDriver | None = None) -> None:
        if type(world_id) is not str or not _REQUEST_ID.fullmatch(world_id):
            raise ValueError("a bounded world identifier is required")
        if type(world) is not LiveWorkloadWorld:
            raise TypeError("a live world is required")
        if driver is not None and (type(driver) is not LiveWorldDriver or driver._world is not world):
            raise ValueError("the driver must belong to this world")
        limits = world._controls.config.ingress
        with self._lock:
            if world_id in self._entries or any(e.world is world for e in self._entries.values()):
                raise ValueError("world is already registered")
            if len(self._entries) >= self._max_worlds:
                raise SessionCapacityError("live world capacity reached")
            # Beyond pending + reply history the world has expired the reply anyway.
            self._entries[world_id] = _LiveEntry(world, driver, limits.max_pending + limits.max_history)

    def _get(self, world_id: str) -> _LiveEntry | None:
        with self._lock:
            return self._entries.get(world_id)

    def discard(self, world_id: str) -> bool:
        """Stop the driver, then fence the world. Return False on driver join timeout."""
        with self._lock:
            entry = self._entries.pop(world_id, None)
        if entry is None:
            raise LookupError("world is not registered")
        with entry.lock:
            entry.closed = True
            entry.handles.clear()
            entry.idempotency.clear()
        if entry.driver is not None:
            return entry.driver.close()
        entry.world.close()
        return True


def _public(value: Any) -> Any:
    if is_dataclass(value):
        return _public(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


def _fingerprint(operation: str, payload: Any) -> str:
    encoded = json.dumps([operation, payload], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def create_live_attack_router(registry: LiveWorldRegistry, *,
                              resolve_owner: Callable[[Request], WebOwnerContext] | None = None) -> APIRouter:
    """Call only after integration review; ``resolve_owner`` must be trusted."""
    if type(registry) is not LiveWorldRegistry:
        raise TypeError("a bounded live world registry is required")
    if resolve_owner is not None and not callable(resolve_owner):
        raise TypeError("resolve_owner must be callable")
    router = APIRouter(prefix="/api/attack/live", tags=["attack-live-adapter-candidate"])

    def resolve(request: Request, world_id: str):
        authorization = request.headers.getlist("authorization")
        if len(authorization) != 1:
            raise _error(401, "UNAUTHENTICATED")
        parts = authorization[0].split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise _error(401, "UNAUTHENTICATED")
        credential = parts[1]
        try:
            owner = None if resolve_owner is None else resolve_owner(request)
            if resolve_owner is not None and type(owner) is not WebOwnerContext:
                raise PermissionError("owner is unavailable")
        except PermissionError:
            raise _error(401, "UNAUTHENTICATED") from None
        entry = registry._get(world_id)
        if entry is None:
            # Same as a wrong owner or bad credential: no environment existence oracle.
            raise _error(401, "UNAUTHENTICATED")
        session = entry.world._runner._session
        try:
            session.authenticate(credential, _web_owner_context=owner)
            principal = session._authority.resolve(credential)
        except PermissionError:
            raise _error(401, "UNAUTHENTICATED") from None
        return entry, credential, owner, principal

    def idempotency_key(request: Request) -> str | None:
        keys = request.headers.getlist("idempotency-key")
        if len(keys) > 1 or keys and not _REQUEST_ID.fullmatch(keys[0]):
            raise _error(422, "INVALID_REQUEST")
        return keys[0] if keys else None

    def capture(entry, principal, key, fingerprint, call):
        with entry.lock:
            if entry.closed:
                raise _error(401, "UNAUTHENTICATED")
            if key is not None:
                known = entry.idempotency.get((principal, key))
                if known is not None:
                    if known[1] != fingerprint:
                        raise _error(422, "IDEMPOTENCY_CONFLICT")
                    if known[0] in entry.handles:
                        return {"request_id": known[0]}
            try:
                receipt = call()
            except HTTPException:
                raise
            except SessionCapacityError:
                raise _error(429, "HOST_CAPACITY_REACHED") from None
            except WorkloadInfrastructureError:
                raise _error(503, "WORLD_UNAVAILABLE") from None
            except PermissionError:
                raise _error(401, "UNAUTHENTICATED") from None
            except (ValueError, TypeError):
                raise _error(422, "INVALID_REQUEST") from None
            except Exception:
                raise _error(500, "INTERNAL_ERROR") from None
            request_id = uuid4().hex
            entry.handles[request_id] = _Handle(receipt, principal)
            if key is not None:
                entry.idempotency[(principal, key)] = (request_id, fingerprint)
            while len(entry.handles) > entry.handle_limit:
                evicted, _ = entry.handles.popitem(last=False)
                entry.idempotency = {k: v for k, v in entry.idempotency.items() if v[0] != evicted}
        if entry.driver is not None:
            entry.driver.wake()
        return {"request_id": request_id}

    async def empty_body(request: Request) -> None:
        async for chunk in request.stream():
            if chunk:
                raise _error(422, "INVALID_REQUEST")

    @router.post("/{world_id}/programs", status_code=202)
    async def submit(request: Request, world_id: str):
        entry, credential, owner, principal = resolve(request, world_id)
        payload = await _body(request, ProgramBody)
        key = idempotency_key(request)
        try:
            # Shape/size only. Circuit parsing happens at the declared arrival,
            # so host parse time never precedes the receipt sample.
            submission = LiveProgramEnvelope(
                json.dumps(payload.circuit_config, allow_nan=False, ensure_ascii=False),
                payload.device_id, payload.shots, key or uuid4().hex, payload.initialization,
                payload.interleave)
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise _error(422, "INVALID_REQUEST") from None
        fingerprint = _fingerprint("program.submit", payload.model_dump())
        return capture(entry, principal, key, fingerprint,
                       lambda: entry.world.submit(credential, submission, owner_context=owner))

    def job_route(suffix: str, method_name: str, operation: str):
        @router.post("/{world_id}/jobs/{job_id}/" + suffix, status_code=202, name="live_" + suffix)
        async def job_operation(request: Request, world_id: str, job_id: str):
            entry, credential, owner, principal = resolve(request, world_id)
            await empty_body(request)
            key = idempotency_key(request)
            method = getattr(entry.world, method_name)
            return capture(entry, principal, key, _fingerprint(operation, job_id),
                           lambda: method(credential, job_id, owner_context=owner))
        return job_operation

    job_route("cancel", "cancel", "job.cancel")
    job_route("status", "status", "job.status.read")
    job_route("result", "result", "job.result.read")

    @router.get("/{world_id}/replies/{request_id}")
    async def reply(request: Request, world_id: str, request_id: str):
        entry, credential, owner, principal = resolve(request, world_id)
        with entry.lock:
            handle = None if entry.closed else entry.handles.get(request_id)
        if handle is None or handle.principal_id != principal:
            # Unknown, evicted and another principal's requests look the same.
            raise _error(404, "REQUEST_NOT_FOUND")
        try:
            value = entry.world.reply(credential, handle.receipt, owner_context=owner)
        except JobResultNotReadyError:
            raise _error(409, "REPLY_NOT_READY") from None
        except LiveReplyExpiredError:
            raise _error(410, "REPLY_EXPIRED") from None
        except PermissionError:
            raise _error(401, "UNAUTHENTICATED") from None
        except WorkloadInfrastructureError:
            raise _error(503, "WORLD_UNAVAILABLE") from None
        except Exception:
            raise _error(500, "INTERNAL_ERROR") from None
        return {"value": _public(value.value), "error_code": value.error_code}

    return router

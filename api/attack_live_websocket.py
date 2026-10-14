"""Opt-in WebSocket transport for product host-clock and test paused worlds.

This router is intentionally not mounted by the product application. Browser
credentials are sent in the first protocol message, never in a URL or a
WebSocket subprotocol. The trusted owner resolver remains server-side.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
import hashlib
import json
import math
import re
from threading import Lock
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from api.attack_http import ProgramBody, _REQUEST_ID
from api.attack_live_world import LiveProgramEnvelope, LiveReplyExpiredError
from api.attack_host_clock_world import HostClockWorldController
from api.attack_paused_world import PausedWorldController, WaitLimitError
from api.attack_sessions import SessionCapacityError, WebOwnerContext
from api.attack_workloads import WorkloadInfrastructureError
from core.attack_simulation.job_service import JobResultNotReadyError
from core.attack_simulation.live_time_policy import EXPLICIT_WAIT, HOST_CLOCK


PROTOCOL = "quanyu.attack.v1"
MAX_WEBSOCKET_MESSAGE_BYTES = 64 * 1024
DEFAULT_MAX_WORLDS = 8
DEFAULT_MAX_CONNECTIONS_PER_WORLD = 4
DEFAULT_MAX_RETAINED_REQUESTS = 256
REPLY_POLL_INTERVAL_S = 0.02  # Host event-loop cadence only; never enters world time.
# Clock frames drive a page re-render; sending one per reply poll overloads the page.
CLOCK_FRAME_INTERVAL_S = 0.1
_ORIGIN_RE = re.compile(r"https?://[^/\s?#]+\Z")


@dataclass
class _RequestHandle:
    receipt: object
    principal_id: str
    kind: str
    job_id: str | None
    issued_at_world_us: float
    delivered: bool = False
    accepted_sent: bool = False
    # The connection that issued the request. Its reply goes back to that
    # connection while it is open, so two connections of one owner (the page and
    # a script) do not take each other's replies. Once it closes, any connection
    # of the owner may collect the reply (reload and resume).
    origin: object | None = None


@dataclass
class _WorldEntry:
    controller: PausedWorldController | HostClockWorldController
    max_connections: int
    max_requests: int
    lock: asyncio.Lock
    send_lock: asyncio.Lock
    handles: OrderedDict
    idempotency: OrderedDict
    sockets: set[WebSocket]
    closed: bool = False
    close_task: asyncio.Task | None = None


class PausedWorldWebSocketRegistry:
    """Bounded registry for host-clock products and explicit-wait tests."""

    def __init__(self, *, max_worlds: int = DEFAULT_MAX_WORLDS,
                 max_connections_per_world: int = DEFAULT_MAX_CONNECTIONS_PER_WORLD,
                 max_retained_requests: int = DEFAULT_MAX_RETAINED_REQUESTS):
        for name, value, maximum in (
            ("max_worlds", max_worlds, 64),
            ("max_connections_per_world", max_connections_per_world, 128),
            ("max_retained_requests", max_retained_requests, 4096),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} is outside its supported range")
        self._max_worlds = max_worlds
        self._max_connections = max_connections_per_world
        self._max_requests = max_retained_requests
        self._entries: dict[str, _WorldEntry] = {}
        self._lock = Lock()

    def register(self, world_id: str, controller: PausedWorldController | HostClockWorldController) -> None:
        if type(world_id) is not str or not _REQUEST_ID.fullmatch(world_id):
            raise ValueError("a bounded world identifier is required")
        if type(controller) not in {PausedWorldController, HostClockWorldController}:
            raise TypeError("a supported live-world controller is required")
        if controller.live_time_policy.mode not in {EXPLICIT_WAIT, HOST_CLOCK}:
            raise ValueError("unsupported WebSocket world clock")
        entry = _WorldEntry(controller, self._max_connections, self._max_requests,
                            asyncio.Lock(), asyncio.Lock(), OrderedDict(), OrderedDict(), set())
        with self._lock:
            if world_id in self._entries or any(
                    item.controller.controls_same_world(controller)
                    or controller.controls_same_world(item.controller)
                    for item in self._entries.values()):
                raise ValueError("world is already registered")
            if len(self._entries) >= self._max_worlds:
                raise SessionCapacityError("WebSocket world capacity reached")
            self._entries[world_id] = entry

    def _get(self, world_id: str) -> _WorldEntry | None:
        with self._lock:
            return self._entries.get(world_id)

    async def discard(self, world_id: str) -> None:
        with self._lock:
            entry = self._entries.get(world_id)
            if entry is not None:
                entry.closed = True
                entry.controller.request_close()
                if entry.close_task is None:
                    entry.close_task = asyncio.create_task(self._close_entry(world_id, entry))
        if entry is None:
            raise LookupError("world is not registered")
        await asyncio.shield(entry.close_task)

    async def _close_entry(self, world_id: str, entry: _WorldEntry) -> None:
        # Fence the world/session while an active wait still owns entry.lock.
        # Closing sockets and returning capacity happen only after the drain.
        await asyncio.to_thread(entry.controller.close)
        async with entry.lock:
            sockets = tuple(entry.sockets)
            entry.sockets.clear()
            entry.handles.clear()
            entry.idempotency.clear()
        for websocket in sockets:
            try:
                await websocket.close(code=4404, reason="WORLD_UNAVAILABLE")
            except RuntimeError:
                pass
        with self._lock:
            if self._entries.get(world_id) is entry:
                del self._entries[world_id]


def _reject_constant(_: str):
    raise ValueError("non-finite JSON values are not allowed")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError("JSON nesting exceeds the parser limit") from None
    if type(value) is not dict:
        raise ValueError("a JSON object is required")
    return value


def _wire(value: Any) -> Any:
    if is_dataclass(value):
        return _wire(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    return value


def _fingerprint(operation: str, payload: Any) -> str:
    encoded = json.dumps([operation, payload], sort_keys=True, separators=(",", ":"),
                         allow_nan=False, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _valid_origin(origin: object) -> bool:
    if type(origin) is not str or not _ORIGIN_RE.fullmatch(origin):
        return False
    try:
        parsed = urlsplit(origin)
        parsed.port  # Force validation of an explicitly supplied port.
    except ValueError:
        return False
    return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not parsed.path and not parsed.query and not parsed.fragment)


async def _read_frame(websocket: WebSocket) -> dict[str, Any]:
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    raw = message.get("text")
    if type(raw) is not str:
        raise TypeError("text frames are required")
    if len(raw.encode("utf-8")) > MAX_WEBSOCKET_MESSAGE_BYTES:
        raise OverflowError("message is too large")
    return _json_object(raw)


def _error(code: str, *, command_id: str | None = None) -> dict[str, Any]:
    result = {"type": "error", "code": code}
    if command_id is not None:
        result["command_id"] = command_id
    return result


def _remember(entry: _WorldEntry, principal: str, command_id: str,
              fingerprint: str, frames: list[dict[str, Any]]) -> None:
    key = (principal, command_id)
    entry.idempotency[key] = (fingerprint, frames)
    entry.idempotency.move_to_end(key)
    while len(entry.idempotency) > entry.max_requests:
        entry.idempotency.popitem(last=False)


def create_live_attack_websocket_router(
    registry: PausedWorldWebSocketRegistry, *,
    resolve_owner: Callable[[WebSocket], WebOwnerContext],
    allowed_origins: Iterable[str],
) -> APIRouter:
    """Build an opt-in router; callers decide whether/where it is mounted."""
    if type(registry) is not PausedWorldWebSocketRegistry:
        raise TypeError("a bounded WebSocket world registry is required")
    if not callable(resolve_owner):
        raise TypeError("a trusted WebOwnerContext resolver is required")
    origins = frozenset(allowed_origins)
    if not origins or any(not _valid_origin(origin) for origin in origins):
        raise ValueError("one or more exact HTTP(S) origins must be configured")

    router = APIRouter(tags=["attack-live-websocket-candidate"])

    async def close_generic(websocket: WebSocket) -> None:
        await websocket.close(code=4401, reason="AUTHENTICATION_FAILED")

    async def send_error(websocket: WebSocket, code: str, *, command_id: str | None = None) -> None:
        await websocket.send_json(_error(code, command_id=command_id))

    async def release_frames(entry: _WorldEntry, credential: str, owner: WebOwnerContext,
                             principal: str, request_ids: Iterable[str], *,
                             include_delivered: bool = False,
                             websocket: WebSocket | None = None) -> list[dict[str, Any]]:
        frames = []
        for request_id in request_ids:
            handle = entry.handles.get(request_id)
            if (handle is None or handle.principal_id != principal
                    or not handle.accepted_sent
                    or handle.delivered and not include_delivered):
                continue
            if (websocket is not None and handle.origin is not None
                    and handle.origin is not websocket and handle.origin in entry.sockets):
                continue
            try:
                if not await asyncio.to_thread(entry.controller.is_reply_released, handle.receipt):
                    continue
                reply = await asyncio.to_thread(entry.controller.reply, credential,
                                                handle.receipt, owner_context=owner)
            except (PermissionError, WorkloadInfrastructureError):
                raise
            except LiveReplyExpiredError:
                entry.handles.pop(request_id, None)
                continue
            frames.append({"type": "reply", "request_id": request_id,
                           "value": _wire(reply.value), "error_code": reply.error_code})
        return frames

    async def command(websocket: WebSocket, entry: _WorldEntry, credential: str,
                      owner: WebOwnerContext, principal: str, frame: dict[str, Any]) -> None:
        command_id = frame.get("command_id")
        if (set(frame) != {"type", "command_id", "operation", "payload"}
                or frame.get("type") != "command" or type(command_id) is not str
                or not _REQUEST_ID.fullmatch(command_id) or type(frame.get("operation")) is not str
                or type(frame.get("payload")) is not dict):
            await send_error(websocket, "INVALID_MESSAGE",
                             command_id=command_id if type(command_id) is str else None)
            return
        operation = frame["operation"]
        payload = frame["payload"]
        try:
            if operation == "program.submit":
                required = {"device_id", "shots", "circuit_config"}
                if not required <= set(payload) <= required | {"initialization", "interleave"}:
                    raise ValueError
                body = ProgramBody.model_validate(payload, strict=True)
                fingerprint = _fingerprint(operation, body.model_dump())
                normalized = {
                    "device_id": body.device_id,
                    "shots": body.shots,
                    "circuit_config": body.circuit_config,
                    "initialization": body.initialization,
                    "interleave": body.interleave,
                }
            elif operation in {"job.cancel", "job.status.read", "job.result.read"}:
                if set(payload) != {"job_id"} or type(payload["job_id"]) is not str:
                    raise ValueError
                job_id = payload["job_id"]
                if not 1 <= len(job_id) <= 128 or not job_id.isascii() or any(c.isspace() for c in job_id):
                    raise ValueError
                fingerprint = _fingerprint(operation, payload)
                normalized = payload
            elif operation == "wait.duration":
                if set(payload) != {"duration_us"} or type(payload["duration_us"]) not in {int, float}:
                    raise ValueError
                duration = payload["duration_us"]
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError
                fingerprint = _fingerprint(operation, payload)
                normalized = payload
            elif operation == "wait.request":
                if set(payload) != {"request_id"} or type(payload["request_id"]) is not str \
                        or not _REQUEST_ID.fullmatch(payload["request_id"]):
                    raise ValueError
                fingerprint = _fingerprint(operation, payload)
                normalized = payload
            elif operation == "wait.any":
                if payload:
                    raise ValueError
                fingerprint = _fingerprint(operation, payload)
                normalized = payload
            elif operation == "request.pending.list":
                if payload:
                    raise ValueError
                fingerprint = _fingerprint(operation, payload)
                normalized = payload
            else:
                await send_error(websocket, "OPERATION_NOT_SUPPORTED", command_id=command_id)
                return
        except (ValidationError, ValueError, TypeError, OverflowError, RecursionError):
            await send_error(websocket, "INVALID_REQUEST", command_id=command_id)
            return

        async with entry.lock:
            if entry.closed:
                await send_error(websocket, "WORLD_UNAVAILABLE", command_id=command_id)
                return
            previous = entry.idempotency.get((principal, command_id))
            if previous is not None:
                old_fingerprint, old_frames = previous
                if old_fingerprint != fingerprint:
                    await send_error(websocket, "IDEMPOTENCY_CONFLICT", command_id=command_id)
                    return
                entry.idempotency.move_to_end((principal, command_id))
                frames = old_frames
            elif operation == "request.pending.list":
                frames = [{"type": "pending.list", "command_id": command_id,
                           "requests": [{"request_id": request_id, "kind": handle.kind,
                                         "job_id": handle.job_id,
                                         "issued_at_world_us": handle.issued_at_world_us}
                                        for request_id, handle in entry.handles.items()
                                        if handle.principal_id == principal and not handle.delivered]}]
                _remember(entry, principal, command_id, fingerprint, frames)
            elif operation.startswith("wait.") and entry.controller.live_time_policy.mode == HOST_CLOCK:
                frames = [_error("OPERATION_NOT_SUPPORTED", command_id=command_id)]
                _remember(entry, principal, command_id, fingerprint, frames)
            elif operation.startswith("wait."):
                try:
                    if operation == "wait.duration":
                        report = await asyncio.to_thread(entry.controller.wait, credential,
                            normalized["duration_us"], owner_context=owner)
                        request_ids = tuple(entry.handles)
                    elif operation == "wait.request":
                        request_id = normalized["request_id"]
                        handle = entry.handles.get(request_id)
                        if handle is None or handle.principal_id != principal:
                            frames = [_error("REQUEST_NOT_FOUND", command_id=command_id)]
                            _remember(entry, principal, command_id, fingerprint, frames)
                            await websocket.send_json(frames[0])
                            return
                        report = await asyncio.to_thread(entry.controller.wait_reply, credential,
                            handle.receipt, owner_context=owner)
                        request_ids = (request_id,)
                    else:
                        report = await asyncio.to_thread(entry.controller.wait_any, credential,
                                                        owner_context=owner)
                        request_ids = tuple(entry.handles)
                    frames = [{"type": "wait.completed", "command_id": command_id,
                               "outcome": report.outcome.value,
                               "world_time_us": report.world_time_us}]
                    # wait.request names one request explicitly, so it may collect a
                    # reply issued by another connection of the same owner.
                    frames.extend(await release_frames(entry, credential, owner, principal, request_ids,
                        include_delivered=operation == "wait.request",
                        websocket=None if operation == "wait.request" else websocket))
                except PermissionError:
                    frames = [_error("UNAUTHENTICATED", command_id=command_id)]
                except WaitLimitError:
                    frames = [_error("WAIT_LIMIT_REACHED", command_id=command_id)]
                except (WorkloadInfrastructureError, RuntimeError):
                    frames = [_error("WORLD_UNAVAILABLE", command_id=command_id)]
                except LookupError:
                    frames = [_error("REQUEST_NOT_FOUND", command_id=command_id)]
                except ValueError:
                    frames = [_error("INVALID_REQUEST", command_id=command_id)]
                _remember(entry, principal, command_id, fingerprint, frames)
            else:
                try:
                    if len(entry.handles) >= entry.max_requests:
                        oldest_delivered = next((key for key, handle in entry.handles.items()
                                                 if handle.delivered), None)
                        if oldest_delivered is None:
                            raise SessionCapacityError("retained request capacity reached")
                        entry.handles.pop(oldest_delivered)
                    if operation == "program.submit":
                        body = normalized
                        envelope = LiveProgramEnvelope(
                            json.dumps(body["circuit_config"], allow_nan=False,
                                       ensure_ascii=False, separators=(",", ":")),
                            body["device_id"], body["shots"], hashlib.sha256(
                                f"{principal}\0{command_id}".encode("utf-8")).hexdigest(),
                            body["initialization"], body["interleave"],
                        )
                        receipt = await asyncio.to_thread(entry.controller.submit, credential,
                            envelope, owner_context=owner)
                    elif operation == "job.cancel":
                        receipt = await asyncio.to_thread(entry.controller.cancel, credential,
                            normalized["job_id"], owner_context=owner)
                    elif operation == "job.status.read":
                        receipt = await asyncio.to_thread(entry.controller.status, credential,
                            normalized["job_id"], owner_context=owner)
                    else:
                        receipt = await asyncio.to_thread(entry.controller.result, credential,
                            normalized["job_id"], owner_context=owner)
                    request_id = uuid4().hex
                    issued_at_world_us = await asyncio.to_thread(
                        entry.controller.request_world_time_us, receipt)
                    kind = {"program.submit": "submit", "job.cancel": "cancel",
                            "job.status.read": "status", "job.result.read": "result"}[operation]
                    entry.handles[request_id] = _RequestHandle(
                        receipt, principal, kind, normalized.get("job_id"), issued_at_world_us,
                        origin=websocket)
                    frames = [{"type": "accepted", "command_id": command_id,
                               "request_id": request_id,
                               "issued_at_world_us": issued_at_world_us}]
                except SessionCapacityError:
                    frames = [_error("HOST_CAPACITY_REACHED", command_id=command_id)]
                except (PermissionError, WorkloadInfrastructureError):
                    frames = [_error("UNAUTHENTICATED", command_id=command_id)]
                except (TypeError, ValueError):
                    frames = [_error("INVALID_REQUEST", command_id=command_id)]
                except Exception:
                    frames = [_error("INTERNAL_ERROR", command_id=command_id)]
                _remember(entry, principal, command_id, fingerprint, frames)
        for item in frames:
            async with entry.send_lock:
                await websocket.send_json(item)
            if item.get("type") in {"accepted", "reply"}:
                async with entry.lock:
                    handle = entry.handles.get(item["request_id"])
                    if handle is not None and handle.principal_id == principal:
                        if item.get("type") == "accepted":
                            handle.accepted_sent = True
                        else:
                            handle.delivered = True

    async def push_released_replies(websocket: WebSocket, entry: _WorldEntry,
                                    credential: str, owner: WebOwnerContext,
                                    principal: str) -> None:
        last_sent_world_time, last_clock_frame = None, -math.inf
        while not entry.closed:
            await asyncio.sleep(REPLY_POLL_INTERVAL_S)
            if entry.controller.live_time_policy.mode != HOST_CLOCK:
                continue
            try:
                world_time_us = await asyncio.to_thread(entry.controller.now)
            except (RuntimeError, WorkloadInfrastructureError):
                return
            async with entry.lock:
                if entry.closed or websocket not in entry.sockets:
                    return
                loop_time = asyncio.get_running_loop().time()
                if (world_time_us != last_sent_world_time
                        and loop_time - last_clock_frame >= CLOCK_FRAME_INTERVAL_S):
                    last_clock_frame = loop_time
                    async with entry.send_lock:
                        await websocket.send_json({"type": "clock", "world_time_us": world_time_us})
                    last_sent_world_time = world_time_us
                try:
                    frames = await release_frames(
                        entry, credential, owner, principal, tuple(entry.handles), websocket=websocket)
                except (PermissionError, WorkloadInfrastructureError, RuntimeError):
                    return
                for item in frames:
                    async with entry.send_lock:
                        await websocket.send_json(item)
                    handle = entry.handles.get(item["request_id"])
                    if handle is not None and handle.principal_id == principal:
                        handle.delivered = True

    @router.websocket("/api/attack/live/{world_id}/socket")
    async def live_socket(websocket: WebSocket, world_id: str):
        origin_headers = websocket.headers.getlist("origin")
        offered = websocket.scope.get("subprotocols", ())
        if len(origin_headers) != 1 or origin_headers[0] not in origins or PROTOCOL not in offered:
            await websocket.close(code=4403, reason="ORIGIN_OR_PROTOCOL_REJECTED")
            return

        entry = registry._get(world_id)
        try:
            owner = resolve_owner(websocket)
            if type(owner) is not WebOwnerContext:
                raise PermissionError
        except Exception:
            owner = None
        await websocket.accept(subprotocol=PROTOCOL)
        try:
            auth = await _read_frame(websocket)
        except (ValueError, TypeError, OverflowError, UnicodeError):
            await close_generic(websocket)
            return
        if (set(auth) != {"type", "credential"} or auth.get("type") != "authenticate"
                or type(auth.get("credential")) is not str):
            await close_generic(websocket)
            return
        credential = auth["credential"]
        if entry is None or owner is None:
            await close_generic(websocket)
            return
        try:
            principal = await asyncio.to_thread(entry.controller.principal_for,
                credential, owner_context=owner)
        except (PermissionError, WorkloadInfrastructureError, RuntimeError):
            await close_generic(websocket)
            return
        async with entry.lock:
            if entry.closed:
                await close_generic(websocket)
                return
            if len(entry.sockets) >= entry.max_connections:
                await websocket.send_json(_error("CONNECTION_LIMIT"))
                await websocket.close(code=4429, reason="CONNECTION_LIMIT")
                return
            entry.sockets.add(websocket)
            # A new authenticated socket can recover the request via
            # request.pending.list even if the previous accepted frame failed.
            for handle in entry.handles.values():
                if handle.principal_id == principal:
                    handle.accepted_sent = True
        try:
            policy = entry.controller.live_time_policy
            await websocket.send_json({"type": "ready", "protocol": PROTOCOL,
                                       "world_time_us": await asyncio.to_thread(entry.controller.now),
                                       "clock_mode": policy.mode,
                                       "pause_while_catching_up": policy.pause_while_catching_up,
                                       "max_program_qubits": await asyncio.to_thread(
                                           entry.controller.max_program_qubits, principal)})
            push_task = asyncio.create_task(push_released_replies(
                websocket, entry, credential, owner, principal))
            try:
                while True:
                    try:
                        frame = await _read_frame(websocket)
                    except OverflowError:
                        await send_error(websocket, "MESSAGE_TOO_LARGE")
                        await websocket.close(code=4409, reason="MESSAGE_TOO_LARGE")
                        break
                    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                        await send_error(websocket, "INVALID_JSON")
                        await websocket.close(code=4400, reason="INVALID_JSON")
                        break
                    try:
                        refreshed_owner = resolve_owner(websocket)
                        if type(refreshed_owner) is not WebOwnerContext or refreshed_owner is not owner:
                            raise PermissionError
                    except Exception:
                        await close_generic(websocket)
                        break
                    await command(websocket, entry, credential, owner, principal, frame)
            finally:
                push_task.cancel()
        except WebSocketDisconnect:
            pass
        finally:
            async with entry.lock:
                entry.sockets.discard(websocket)

    return router

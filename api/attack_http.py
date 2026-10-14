"""Reviewed opt-in adapter candidate, not a default-mounted product API.

Only simulator-issued virtual credentials are accepted. No actor role, tenant,
layout, seed, supervisor command or environment administration enters the body.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from collections.abc import Callable
import json
from typing import Any, Literal
from uuid import uuid4
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from api.attack_sessions import AttackSessionRegistry, SessionBusyError, SessionCapacityError, WebOwnerContext
from api.attack_terminal import TerminalAction, TerminalSession, parse_terminal_command
from core.attack_simulation.job_service import JobNotFoundError, JobResultNotReadyError
from core.attack_simulation.program_request import CircuitProgram, ProgramSubmitRequest


MAX_HTTP_BODY_BYTES = 64 * 1024
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProgramBody(_Body):
    device_id: str = Field(min_length=1, max_length=128)
    shots: int = Field(ge=1, le=4096)
    circuit_config: dict[str, Any]
    initialization: Literal["prepare", "inherit"] = "prepare"
    interleave: Literal["victim", "training:0", "training:1"] | None = None


class CircuitFileBody(_Body):
    # UTF-8 byte and symbolic-name checks remain at the shared workspace boundary.
    name: str = Field(min_length=1, max_length=256)
    circuit_json: str = Field(max_length=64 * 1024)


class TerminalBody(_Body):
    line: str = Field(min_length=1, max_length=4096)


def _error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _wire(value: Any) -> Any:
    # Only receives public job/device views or terminal help, never runtime objects.
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, tuple):
        return [_wire(item) for item in value]
    return value


def _reject_nonfinite(_: str) -> None:
    raise ValueError("nonfinite JSON number")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


async def _body(request: Request, model: type[_Body]) -> _Body:
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > MAX_HTTP_BODY_BYTES:
            raise _error(413, "REQUEST_TOO_LARGE")
        buffer.extend(chunk)
    try:
        value = json.loads(buffer, parse_constant=_reject_nonfinite, object_pairs_hook=_unique_object)
        return model.model_validate(value)
    except (ValueError, ValidationError, RecursionError, UnicodeDecodeError):
        # Do not echo raw input, unknown identity fields or validation context.
        raise _error(422, "INVALID_REQUEST") from None


def create_attack_router(registry: AttackSessionRegistry, *,
                         resolve_owner: Callable[[Request], WebOwnerContext] | None = None) -> APIRouter:
    """Opt-in actor routes, optionally behind a trusted real-owner resolver.

    The resolver must authenticate the actual site owner and supply its retained
    in-process identity. This adapter does not implement cookies/authentication
    or infer ownership from participant JSON, environment IDs or virtual Bearer.
    Owned entries fail closed if the resolver is absent. No default mount.
    """
    if resolve_owner is not None and not callable(resolve_owner):
        raise TypeError("real-owner resolution must be a trusted callable")
    router = APIRouter(prefix="/api/attack/sessions", tags=["attack-adapter-candidate"])

    def resolve(request: Request, session_id: str):
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
                raise PermissionError("real-owner resolver returned no authenticated context")
            session = registry.authenticated(session_id, credential, owner_context=owner)
        except PermissionError:
            raise _error(401, "UNAUTHENTICATED") from None
        except Exception:
            raise _error(500, "INTERNAL_ERROR") from None
        return session, credential, owner

    async def perform(session, credential, operation, *, operation_id, owner_context=None):
        receipt = None
        try:
            # Complete envelope/authentication first; reserve before worker wait
            # and program conversion. Never publish host timestamps/sequences.
            receipt = session.receive(credential, operation_id, _web_owner_context=owner_context)
            # A bounded per-session ingress gate, not a worker waiting for an
            # earlier request which might itself still need a worker slot.
            result = await session.request_async(credential, operation, receipt=receipt, worker=run_in_threadpool,
                                                 _web_owner_context=owner_context)
            return _wire(result)
        except HTTPException:
            raise
        except JobNotFoundError:
            raise _error(404, "JOB_NOT_FOUND") from None
        except JobResultNotReadyError:
            raise _error(409, "RESULT_NOT_READY") from None
        except SessionCapacityError:
            raise _error(429, "HOST_CAPACITY_REACHED") from None
        except SessionBusyError:
            raise _error(503, "HOST_BUSY") from None
        except PermissionError:
            # Authentication is checked again during the locked operation.
            try:
                session.authenticate(credential, _web_owner_context=owner_context)
            except PermissionError:
                raise _error(401, "UNAUTHENTICATED") from None
            raise _error(403, "OPERATION_DENIED") from None
        except FileNotFoundError:
            raise _error(404, "WORKSPACE_FILE_NOT_FOUND") from None
        except LookupError:
            raise _error(404, "RESOURCE_NOT_FOUND") from None
        except (ValueError, TypeError):
            raise _error(422, "INVALID_OPERATION") from None
        except Exception:
            raise _error(500, "INTERNAL_ERROR") from None
        finally:
            if receipt is not None:
                # Worker cancellation/failure must not strand a pending slot.
                session.abandon_receipt(receipt)

    @router.get("/{session_id}/devices")
    async def devices(request: Request, session_id: str):
        session, credential, owner = resolve(request, session_id)
        return await perform(session, credential, lambda client, workspace: client.list_devices(),
                             operation_id="device.list", owner_context=owner)

    @router.get("/{session_id}/devices/{device_id}")
    async def device(request: Request, session_id: str, device_id: str):
        session, credential, owner = resolve(request, session_id)
        return await perform(session, credential, lambda client, workspace: client.get_device(device_id),
                             operation_id="device.show", owner_context=owner)

    @router.post("/{session_id}/programs", status_code=202)
    async def submit(request: Request, session_id: str):
        session, credential, owner = resolve(request, session_id)
        payload = await _body(request, ProgramBody)
        keys = request.headers.getlist("idempotency-key")
        if len(keys) > 1:
            raise _error(422, "INVALID_REQUEST")
        key = keys[0] if keys else str(uuid4())
        def operation(client, workspace):
            try:
                program = CircuitProgram.from_editor_json(json.dumps(payload.circuit_config, allow_nan=False))
                submission = ProgramSubmitRequest(program, payload.device_id, payload.shots, key,
                                                  payload.initialization, payload.interleave)
            except (ValueError, TypeError, OverflowError, RecursionError):
                raise _error(422, "INVALID_REQUEST") from None
            return client.submit_program(submission)
        return await perform(session, credential, operation, operation_id="program.submit", owner_context=owner)

    @router.get("/{session_id}/jobs/{job_id}")
    async def status(request: Request, session_id: str, job_id: str):
        session, credential, owner = resolve(request, session_id)
        return await perform(session, credential, lambda client, workspace: client.get_status(job_id),
                             operation_id="job.status.read", owner_context=owner)

    @router.get("/{session_id}/jobs/{job_id}/result")
    async def result(request: Request, session_id: str, job_id: str):
        session, credential, owner = resolve(request, session_id)
        return await perform(session, credential, lambda client, workspace: client.get_result(job_id),
                             operation_id="job.result.read", owner_context=owner)

    @router.post("/{session_id}/jobs/{job_id}/cancel")
    async def cancel(request: Request, session_id: str, job_id: str):
        session, credential, owner = resolve(request, session_id)
        # Cancellation has no participant body/identity fields.
        async for chunk in request.stream():
            if chunk:
                raise _error(422, "INVALID_REQUEST")
        return await perform(session, credential, lambda client, workspace: client.cancel(job_id),
                             operation_id="job.cancel", owner_context=owner)

    @router.post("/{session_id}/workspace/circuits", status_code=204)
    async def circuit_file(request: Request, session_id: str):
        session, credential, owner = resolve(request, session_id)
        payload = await _body(request, CircuitFileBody)
        await perform(session, credential,
                      lambda client, workspace: workspace.put_circuit(payload.name, payload.circuit_json),
                      operation_id="workspace.circuit.put", owner_context=owner)

    @router.post("/{session_id}/terminal")
    async def terminal(request: Request, session_id: str):
        session, credential, owner = resolve(request, session_id)
        payload = await _body(request, TerminalBody)
        keys = request.headers.getlist("idempotency-key")
        if len(keys) > 1 or keys and not _REQUEST_ID.fullmatch(keys[0]):
            raise _error(422, "INVALID_REQUEST")
        key = keys[0] if keys else None
        try:
            action = parse_terminal_command(payload.line).action
        except (ValueError, TypeError):
            raise _error(422, "INVALID_OPERATION") from None
        # Use the shared parser, not a second grammar. Observation/cancellation
        # must not queue behind another participant's host compilation.
        operation_id = "program.submit" if action is TerminalAction.PROGRAM_SUBMIT else "terminal." + action.value
        return await perform(session, credential, lambda client, workspace:
                             TerminalSession(client, workspace).execute(payload.line, request_id=key),
                             operation_id=operation_id, owner_context=owner)

    return router

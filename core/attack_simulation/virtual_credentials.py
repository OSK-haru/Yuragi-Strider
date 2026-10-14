"""Simulator-only per-environment credentials for the job boundary."""

from __future__ import annotations

import hashlib
import secrets
from threading import Lock

from core.attack_simulation.job_service import DeviceInfo, JobApplicationService, JobResultView, JobStatusView
from core.attack_simulation.program_request import ProgramSubmitRequest
from core.attack_simulation.jobs import JobOperation, VictimPrepareOperation
from core.attack_simulation.resource_binding import PhysicalQubitRef


class VirtualCredentialAuthority:
    """Issue opaque credentials; retain only hashes until environment close."""

    def __init__(self) -> None:
        self._principals: dict[bytes, str] = {}
        self._closed = False
        self._session_owner: object | None = None
        self._allowed_principals: frozenset[str] | None = None
        self._lock = Lock()

    def issue(self, principal_id: str) -> str:
        with self._lock:
            return self._issue(principal_id)

    def _issue(self, principal_id: str) -> str:
        if self._closed:
            raise RuntimeError("virtual credential authority is closed")
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise ValueError("principal_id must be nonempty")
        if self._allowed_principals is not None and principal_id not in self._allowed_principals:
            raise ValueError("principal does not belong to the bound session")
        credential = f"vqc_{secrets.token_urlsafe(32)}"
        self._principals[_digest(credential)] = principal_id
        return credential

    def resolve(self, credential: str) -> str:
        with self._lock:
            return self._resolve(credential)

    def _resolve(self, credential: str) -> str:
        if self._closed:
            raise PermissionError("virtual credential is no longer valid")
        if (
            not isinstance(credential, str)
            or len(credential) != 47
            or not credential.startswith("vqc_")
            or not credential.isascii()
        ):
            raise PermissionError("invalid virtual credential")
        try:
            return self._principals[_digest(credential)]
        except KeyError as error:
            raise PermissionError("invalid virtual credential") from error

    def close(self) -> None:
        with self._lock:
            self._principals.clear()
            self._closed = True

    def bind_session(self, owner: object, principal_ids: frozenset[str]) -> None:
        """Supervisor-only, single-world ownership without returning credentials."""
        with self._lock:
            if self._closed or self._session_owner is not None:
                raise ValueError("credential authority is closed or already bound")
            if not principal_ids or not set(self._principals.values()) <= principal_ids:
                raise ValueError("credential authority contains an unregistered principal")
            allowed_principals = frozenset(principal_ids)
            self._session_owner = id(owner)
            self._allowed_principals = allowed_principals


class CredentialedJobConnection:
    """Authenticate each request before ordinary principal/tenant authorization."""

    def __init__(
        self,
        *,
        authority: VirtualCredentialAuthority,
        service: JobApplicationService,
        principal_id: str,
        preconfigured_credential: str | None = None,
    ) -> None:
        self._authority = authority
        self._service = service
        self._principal_id = principal_id
        self._credential: str | None = None
        if preconfigured_credential is not None:
            self.configure(preconfigured_credential)

    def configure(self, credential: str) -> None:
        if self._authority.resolve(credential) != self._principal_id:
            raise PermissionError("virtual credential belongs to another principal")
        self._credential = credential

    def require_authenticated(self) -> str:
        if self._credential is None:
            raise PermissionError("virtual credential has not been configured")
        principal = self._authority.resolve(self._credential)
        if principal != self._principal_id:
            raise PermissionError("virtual credential belongs to another principal")
        return principal

    def submit(
        self,
        *,
        operation: JobOperation,
        idempotency_key: str,
        target: PhysicalQubitRef | None = None,
        scenario_manifest_id: str | None = None,
    ) -> JobStatusView:
        return self._service.submit(
            self.require_authenticated(), operation=operation,
            idempotency_key=idempotency_key, target=target,
            scenario_manifest_id=scenario_manifest_id,
        )

    def submit_scenario_workload(
        self,
        *,
        operation: VictimPrepareOperation,
        idempotency_key: str,
        target: PhysicalQubitRef | None = None,
        scenario_manifest_id: str | None = None,
    ) -> JobStatusView:
        return self._service._submit_scenario_workload(
            self.require_authenticated(), operation=operation,
            idempotency_key=idempotency_key, target=target,
            scenario_manifest_id=scenario_manifest_id,
        )

    def get_status(self, job_id: str) -> JobStatusView:
        return self._service.get_status(self.require_authenticated(), job_id)

    def list_devices(self) -> tuple[DeviceInfo, ...]:
        return self._service.list_devices(self.require_authenticated())

    def get_device(self, device_id: str) -> DeviceInfo:
        return self._service.get_device(self.require_authenticated(), device_id)

    def submit_program(self, request: ProgramSubmitRequest) -> JobStatusView:
        return self._service.submit_program(self.require_authenticated(), request)

    def get_result(self, job_id: str) -> JobResultView:
        return self._service.get_result(self.require_authenticated(), job_id)

    def cancel(self, job_id: str) -> JobStatusView:
        return self._service.cancel(self.require_authenticated(), job_id)


def _digest(credential: str) -> bytes:
    return hashlib.sha256(credential.encode("utf-8")).digest()

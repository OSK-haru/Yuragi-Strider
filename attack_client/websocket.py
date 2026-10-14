"""手元のPython用の transport: サービスのWebSocketとHTTP提出を直接使う。"""

from __future__ import annotations

import json
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import requests
from websockets.sync.client import connect

from attack_client.errors import AttackError

PROTOCOL = "quanyu.attack.v1"


class WebSocketTransport:
    def __init__(self, base_url: str, environment_id: str, token: str, credential: str, timeout: float):
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("base_url must look like http://127.0.0.1:8001")
        self.environment_id, self._timeout = environment_id, timeout
        self._token, self._base = token, base_url.rstrip("/")
        self._lock = threading.Condition()
        self._accepted: dict[str, str] = {}
        self._errors: dict[str, str] = {}
        self._replies: dict[str, dict[str, Any]] = {}
        self._closed = False
        self.world_time_us = 0
        ws_url = urlunsplit(("ws" if parts.scheme == "http" else "wss", parts.netloc,
                             f"/api/attack/live/{environment_id}/socket", "", ""))
        try:
            self._socket = connect(ws_url, subprotocols=[PROTOCOL],
                                   additional_headers={"Authorization": f"Bearer {token}"},
                                   open_timeout=timeout)
            self._socket.send(json.dumps({"type": "authenticate", "credential": credential}))
            ready = json.loads(self._socket.recv(timeout=timeout))
        except Exception as error:
            raise AttackError("could not open the attack session (token, credential or URL)", "CONNECT") from error
        if ready.get("type") != "ready":
            raise AttackError("the service refused the session", "REFUSED")
        self.world_time_us = int(ready.get("world_time_us", 0))
        self._reader = threading.Thread(target=self._read, name="attack-session-reader", daemon=True)
        self._reader.start()

    def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """要求を送り、受理された要求の reply フレームを返す。"""
        return self._reply(self._send(operation, payload))

    def answer(self, document: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            f"{self._base}/api/attack/environments/{self.environment_id}/submission",
            json=document,
            headers={"Authorization": f"Bearer {self._token}"}, timeout=self._timeout)
        if response.status_code != 200:
            detail = response.json().get("detail", {}) if response.content else {}
            code = detail.get("code") if isinstance(detail, dict) else None
            raise AttackError(f"submission refused ({response.status_code} {code})", code)
        return response.json()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def close(self) -> None:
        self._closed = True
        try:
            self._socket.close()
        except Exception:
            pass

    def _send(self, operation: str, payload: dict[str, Any]) -> str:
        command_id = uuid4().hex
        self._socket.send(json.dumps({"type": "command", "command_id": command_id,
                                      "operation": operation, "payload": payload}))
        with self._lock:
            if not self._lock.wait_for(lambda: command_id in self._accepted or command_id in self._errors or self._closed,
                                       self._timeout):
                raise AttackError(f"{operation} was not acknowledged", "TIMEOUT")
            if command_id in self._errors:
                raise AttackError(f"{operation} was refused ({self._errors[command_id]})", self._errors[command_id])
            if command_id not in self._accepted:
                raise AttackError("the session closed", "CLOSED")
            return self._accepted.pop(command_id)

    def _reply(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            if not self._lock.wait_for(lambda: request_id in self._replies or self._closed, self._timeout):
                raise AttackError("no reply from the service", "TIMEOUT")
            reply = self._replies.pop(request_id, None)
        if reply is None:
            raise AttackError("the session closed", "CLOSED")
        return reply

    def _read(self) -> None:
        try:
            for raw in self._socket:
                frame = json.loads(raw)
                with self._lock:
                    kind = frame.get("type")
                    if kind == "accepted":
                        self._accepted[frame["command_id"]] = frame["request_id"]
                    elif kind == "reply":
                        self._replies[frame["request_id"]] = frame
                    elif kind == "error" and isinstance(frame.get("command_id"), str):
                        self._errors[frame["command_id"]] = str(frame.get("code"))
                    elif kind == "clock":
                        self.world_time_us = int(frame.get("world_time_us", self.world_time_us))
                    self._lock.notify_all()
        except Exception:
            pass
        finally:
            with self._lock:
                self._closed = True
                self._lock.notify_all()

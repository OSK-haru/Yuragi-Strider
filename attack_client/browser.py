"""ブラウザのスクリプト実行環境（Pyodide の Web Worker）用の transport。

Worker が登録する `_attack_bridge` モジュールの同期関数を呼ぶ。各関数は JSON 文字列を
受け取って返し、画面側の応答が届くまで Worker を止めて待つ。資格情報は画面側だけが持ち、
ここには環境IDと公開の応答しか来ない。
"""

from __future__ import annotations

import json
from typing import Any

from attack_client.errors import AttackError


def _bridge():
    try:
        import _attack_bridge  # type: ignore[import-not-found]
    except ImportError as error:
        raise AttackError("this Python is not the browser script runner", "NO_BRIDGE") from error
    return _attack_bridge


class BrowserTransport:
    def __init__(self, timeout: float):
        self._bridge = _bridge()
        self._timeout_ms = int(timeout * 1000)
        self._closed = False
        opened = self._call("open", {"timeout_ms": self._timeout_ms})
        self.environment_id = str(opened["environment_id"])
        self.world_time_us = int(opened.get("world_time_us", 0))

    def request(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """要求を送り、受理された要求の reply（value と error_code）を返す。"""
        if self._closed:
            raise AttackError("the session closed", "CLOSED")
        reply = self._call("request", {"operation": operation, "payload": payload,
                                       "timeout_ms": self._timeout_ms})
        self.world_time_us = int(reply.get("world_time_us", self.world_time_us))
        return reply

    def answer(self, document: dict[str, Any]) -> dict[str, Any]:
        return self._call("answer", document)

    def sleep(self, seconds: float) -> None:
        if self._bridge.sleep(max(0, int(seconds * 1000))):
            raise KeyboardInterrupt

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._call("close", {})

    def _call(self, method: str, arguments: dict[str, Any]) -> dict[str, Any]:
        outcome = json.loads(self._bridge.call(method, json.dumps(arguments)))
        if "error" in outcome:
            error = outcome["error"]
            if error.get("code") == "INTERRUPTED":
                raise KeyboardInterrupt
            raise AttackError(str(error.get("message", "bridge error")), error.get("code"))
        value = outcome.get("value")
        return value if isinstance(value, dict) else {}

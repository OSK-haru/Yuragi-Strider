"""サービスの公開操作を呼ぶセッション。通信は transport に任せる。

ブラウザのスクリプト実行環境（Pyodide）で動き、画面が持つ接続へ同期ブリッジ経由で
要求を渡す。資格情報は画面の外に出ない。
"""

from __future__ import annotations

from dataclasses import dataclass
import sys
import time
from typing import Any

from attack_client.circuit import Circuit
from attack_client.errors import AttackError

_FAILED = {"failed", "cancelled", "rejected", "internal_error"}
# 交互観測（抽象コマンド）。現実のサービスには無く、環境設定で有効にしたときだけ使える。
_INTERLEAVE = {"other": "victim", "ref:0": "training:0", "ref:1": "training:1"}


@dataclass(frozen=True)
class Result:
    status: str
    shots: int
    counts: dict[str, int]

    def ones(self, classical_bit: int = 0) -> int:
        """古典bit c{n} が1だった回数。ビット列は左端が c0（"10" は c0=1, c1=0）。"""
        return sum(count for bits, count in self.counts.items()
                   if len(bits) > classical_bit and bits[classical_bit] == "1")

    def probability_of_one(self, classical_bit: int = 0) -> float:
        return self.ones(classical_bit) / self.shots if self.shots else 0.0


class Job:
    def __init__(self, session: "AttackSession", job_id: str):
        self._session = session
        self.job_id = job_id

    def result(self, timeout: float = 120.0, poll: float = 0.4) -> Result:
        """完了するまで結果照会を繰り返す。"""
        deadline = time.monotonic() + timeout
        while True:
            try:
                value = self._session._query("job.result.read", self.job_id)
            except AttackError as error:
                # 結果の公開前に照会すると、サービスは RESULT_NOT_READY を返す。待って照会し直す。
                if error.code != "RESULT_NOT_READY":
                    raise
                value = {}
            status = value.get("status")
            body = value.get("result") if isinstance(value.get("result"), dict) else value
            if status == "succeeded" and isinstance(body.get("counts"), dict):
                return Result(status, int(body.get("shots", 0)), {str(k): int(v) for k, v in body["counts"].items()})
            if status in _FAILED:
                raise AttackError(f"job {self.job_id} ended as {status}", status)
            if time.monotonic() >= deadline:
                raise AttackError(f"job {self.job_id} did not finish within {timeout} seconds", "TIMEOUT")
            self._session._transport.sleep(poll)

    def status(self) -> str:
        return str(self._session._query("job.status.read", self.job_id).get("status"))

    def cancel(self) -> None:
        self._session._query("job.cancel", self.job_id)


class AttackSession:
    """公開サービス操作のセッション。画面のスクリプト実行で、その画面の環境へ接続する。"""

    def __init__(self, *, device: str = "ibm_kingston", timeout: float = 30.0):
        if sys.platform != "emscripten":
            raise AttackError("AttackSession runs only in the browser script runner "
                              "(the terminal's python command)", "NO_BRIDGE")
        from attack_client.browser import BrowserTransport
        self.device, self._timeout = device, timeout
        self._transport = BrowserTransport(timeout)
        self.environment_id = self._transport.environment_id

    @property
    def world_time_us(self) -> int:
        """サービスが最後に知らせた世界時刻（µs）。"""
        return self._transport.world_time_us

    # ---- 公開操作 -----------------------------------------------------------

    def run(self, circuit: Circuit | dict[str, Any], *, shots: int = 32,
            initialization: str | None = None, interleave: str | None = None) -> Job:
        """回路を送信してジョブを返す。結果は job.result() で待つ。"""
        document = circuit.to_dict() if isinstance(circuit, Circuit) else circuit
        payload: dict[str, Any] = {"device_id": self.device, "shots": shots, "circuit_config": document}
        if interleave is not None:
            if interleave not in _INTERLEAVE:
                raise ValueError("interleave must be one of: " + ", ".join(_INTERLEAVE))
            payload["interleave"] = _INTERLEAVE[interleave]
            initialization = initialization or "inherit"
        if initialization is not None:
            payload["initialization"] = initialization
        value = self._call("program.submit", payload)
        job_id = value.get("job_id")
        if not isinstance(job_id, str):
            raise AttackError(f"the service did not accept the program: {value.get('reason', value)}", "REJECTED")
        return Job(self, job_id)

    def answer(self, threshold: float | None = None, direction: str | None = None, *,
               message: str | None = None) -> dict[str, Any]:
        """課題モードの解答を提出する（回数制限あり）。

        判定規則の課題は threshold と direction、メッセージの課題は message を渡す。
        返るのは提出回数と合否（判定規則の課題では正解数の集計も）だけ。
        """
        if message is not None:
            if threshold is not None or direction is not None:
                raise ValueError("pass either message or threshold and direction, not both")
            return self._transport.answer({"message": message})
        if threshold is None or direction is None:
            raise ValueError("pass message, or both threshold and direction")
        return self._transport.answer({"threshold": threshold, "direction": direction})

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "AttackSession":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- 内部 ---------------------------------------------------------------

    def _query(self, operation: str, job_id: str) -> dict[str, Any]:
        return self._call(operation, {"job_id": job_id})

    def _call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        reply = self._transport.request(operation, payload)
        if reply.get("error_code"):
            value = reply.get("value")
            reason = value.get("reason") if isinstance(value, dict) else None
            raise AttackError(f"{reply['error_code']}" + (f": {reason}" if reason else ""), reply["error_code"])
        value = reply.get("value")
        return value if isinstance(value, dict) else {}

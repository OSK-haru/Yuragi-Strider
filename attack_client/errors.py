"""クライアント共通の例外。"""

from __future__ import annotations


class AttackError(RuntimeError):
    """サービスが要求を拒否した、または通信・待機に失敗した。"""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code

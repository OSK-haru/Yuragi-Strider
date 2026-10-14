"""攻撃シミュレーターの公開サービス操作を使うクライアント。

ブラウザのターミナルと同じ操作（回路の送信、結果・状態の照会、取消、課題の提出）
だけを呼ぶ。隠れた値や環境の内部状態に届く経路はない。画面のスクリプト実行
（ターミナルの python コマンド）で使う。

    from attack_client import AttackSession, Circuit

    session = AttackSession()
    job = session.run(Circuit(1).measure(0, 0), shots=32, initialization="inherit")
    print(job.result().counts)
"""

from attack_client.circuit import Circuit
from attack_client.errors import AttackError
from attack_client.session import AttackSession, Job, Result

__all__ = ["AttackError", "AttackSession", "Circuit", "Job", "Result"]

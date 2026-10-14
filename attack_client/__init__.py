"""攻撃シミュレーターの公開サービス操作を使うクライアント。

ブラウザのターミナルと同じ操作（回路の送信、結果・状態の照会、取消、課題の提出）
だけを呼ぶ。隠れた値や環境の内部状態に届く経路はない。手元のPythonでは、画面の
「Pythonから接続」で発行するスクリプト用トークンと攻撃者の資格情報を渡す。画面の
スクリプト実行（ターミナルの python コマンド）では引数を省略する。

    from attack_client import AttackSession, Circuit

    session = AttackSession("http://127.0.0.1:8001", environment_id, token, credential)
    # 画面のスクリプト実行では: session = AttackSession()
    job = session.run(Circuit(1).measure(0, 0), shots=32, initialization="inherit")
    print(job.result().counts)
"""

from attack_client.circuit import Circuit
from attack_client.errors import AttackError
from attack_client.session import AttackSession, Job, Result

__all__ = ["AttackError", "AttackSession", "Circuit", "Job", "Result"]

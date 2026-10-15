# Pythonから攻撃する（スクリプトアクセス）

状態: 設計と実装（画面のスクリプト実行 2026-10-14）。画面のスクリプト実行はサーバーに新しい経路を足さず、cross-origin isolated な配信（開発サーバー・デスクトップ版・公開デモ）で動く。

**手元のPythonからの接続は削除した（2026-10-14、利用者の決定）。** 画面のスクリプト実行と機能が重複するため。2026-10-13 に足したスクリプト用トークン（`POST /environments/{id}/script-token`）、`Authorization: Bearer` による所有者認証、Origin の無い WebSocket の受け入れ、設定 `script_access`、`attack_client/websocket.py`、画面の「Pythonから接続」を取り除いた。認証の経路は、所有者 cookie だけに戻った。

## 目的

実際の攻撃者は、ターミナルを手で叩く代わりにスクリプトで自動化する。画面の情報量を増やさず、**公開されているサービス操作をそのままPythonから使える**ようにする。自由度の源泉はPython側（回路の自作、複数ジョブの並列投入、待機と条件分岐、numpy等による学習）に置き、サービス側の制限（shots、命令数、観測予算、提出回数）は変えない。

## 見える範囲

スクリプトが持つ権限は、そのブラウザの所有者がターミナルで持つ権限と同じ。隠れた値（被害者の秘密bit、隠しケース、環境内部の状態）に届く経路はない。課題モードの観測予算と提出回数は、サーバー側で同じ仕組みで検査される。

## Pythonクライアント

`attack_client/` パッケージ。Qiskit Runtimeに近い書き味にする。

```python
from attack_client import AttackSession, Circuit

session = AttackSession()                 # 画面のスクリプト実行でだけ開ける
circuit = Circuit(1).measure(0, 0)
job = session.run(circuit, shots=32, interleave="ref:0")
print(job.result().counts)
```

- `Circuit` は編集画面のJSONを組み立てる小さなビルダー。任意のゲート列を自作できる。結果のビット列は左端が c0（`"10"` は c0=1, c1=0）。
- `session.run(...)` は送信してジョブを返す。送信の返事（ジョブID）は待たず、`job_id`・`result()`・`status()`・`cancel()` を最初に使ったときに待つ。だから続けて何本も送れる（サービスの待ち行列は8本まで、超えた送信は `HOST_CAPACITY_REACHED`）。送信の拒否もその時点で `AttackError` になる。`job.result()` は完了まで結果照会を繰り返す（公開前の照会に返る `RESULT_NOT_READY` は待つ合図として扱う）。ブリッジには、送るだけの `issue` と、その要求の返事を待つ `reply` がある。`reply` で待てるのは、そのスクリプトが出した要求だけ。複数ジョブを先に投げてから結果を集められる。
- `job.victim_activity()` は、抽象コマンド「被害者の動き」（自由モードの設定で有効にした環境だけ）。ジョブの裏で動いた「被害者の実行 → サービスのReset → 攻撃者の測定」の順番と世界時刻を dict で返す。状態や被害者の測定値は含まない。無効な環境では `OPERATION_DENIED` の `AttackError`。
- `session.answer(threshold, direction)` が判定規則の課題の提出、`session.answer(message=...)` がメッセージの課題の提出。通信部分の `answer` は提出本文の dict をそのまま送る。
- サンプルは `examples/reset_leakage_attack.py`（収集 → 学習 → 観測 → 推定 → 提出）。

## 画面のスクリプト実行（疑似シェル）

2026-10-14に利用者が、ブラウザ内で利用者のコードを実行することを認めた（下の「しないこと」の旧方針を改めた）。依存 `pyodide`（npm、MPL-2.0）も承認済み。

攻撃者ワークスペースの編集欄に「スクリプト」タブを置き、ファイルを書いてターミナルで `python <ファイル> [引数...]` と打つと実行する。SDKはリポジトリの `attack_client` をそのまま組み込む。

```python
from attack_client import AttackSession, Circuit

with AttackSession() as session:          # 画面の環境へ接続する
    job = session.run(Circuit(1).measure(0, 0), shots=32)
    print(job.result().counts)
```

- **実行場所**: 専用の Web Worker の中の Pyodide（WebAssembly の CPython 3.14）。画面のDOM・cookie・資格情報には触れない。サーバー側では利用者のコードを実行しない。
- **同期の書き方**: Worker の Python は同期で書く。待っている間 Worker のイベントループは回らないため、応答を message で受け取れない。要求は postMessage で画面へ送り、応答は SharedArrayBuffer に書いてもらって `Atomics.wait` で待つ（`frontend/src/scripting/syncChannel.ts`）。呼出番号を照合し、中断で見捨てた古い応答は書かない。
- **接続**: スクリプトは画面とは別のWebSocket接続を1本持つ。資格情報はクライアントの中に留まり、Worker へは環境IDと公開の応答だけを返す。使える操作は回路送信・状態照会・結果照会・取消・提出だけ（`scriptServices.ts` の許可一覧）。
- **reply の配送**: 同じ所有者の接続が2本あると、reply は先に取りに来た接続へ渡っていた。reply は**要求を出した接続へ返す**ように直した。その接続が閉じた後は、所有者のどの接続でも受け取れる（再読み込みと復帰のため）。`wait.request` は要求IDを名指しするので、別の接続の要求でも受け取れる（`api/attack_live_websocket.py` の `origin`）。
- **シェルとしての振る舞い**: 1本ずつ実行する。標準出力と標準エラーはターミナルへ出る。実行中に打った行は `input()` へ渡り、プロンプトは入力欄に出る。Ctrl+C は KeyboardInterrupt（終了コード130）。3秒で止まらなければ Worker ごと止める（137、次の実行で読み込み直す）。トレースバックは利用者のファイルからの行だけを見せる。`ls` や `cd` のような、シェルのふりをするだけの機能は足さない。
- **ファイル**: このブラウザの localStorage にだけ保存する（最大32ファイル・合計512K文字、`.py` の平らな名前）。保存できないときは画面を開いている間だけ使える。最初のファイルは `main.py` と `examples/reset_leakage_attack.py` の写し。
- **配信**: SharedArrayBuffer には cross-origin isolation（`COOP: same-origin`、`COEP: require-corp`）が要る。開発サーバー（`vite.config.ts`）、デスクトップ版（`api/main.py`）、公開デモの配信 Worker（`frontend/src/worker.ts`、`run_worker_first`）が全画面に付ける。Google Fonts は `crossorigin` で読む。Pyodide 本体（約13MB）はアプリが `/assets/python-runtime/` から配る。Pyodide は WebAssembly のコンパイルと自前の `eval` を使うため、**このパスだけ** CSP を `'wasm-unsafe-eval' 'unsafe-eval'` に緩める。画面自体の CSP は変えない。
- **追加パッケージ**: numpy 等は `import` を見て公式CDN（`cdn.jsdelivr.net/pyodide/v<版>/full/`）から読み、同梱の lock ファイルの SHA-256 で検査する。Worker の CSP の `connect-src` はこのCDNだけを足す。オフラインでは追加パッケージを使えない（[未決事項](open-questions.md#画面のスクリプト実行の残件)）。
- **使えない場所**: cross-origin isolated でない配信では、実行ボタンとコマンドが理由を出して止まる。

## 抽象コマンドの扱い

現実のサービスに無い操作（交互観測 `--interleave`）は**抽象コマンド**として分け、既定では無効にした。

- 環境設定の `qpu_service.abstract_commands.interleaved_observation`（自由モードの設定項目、初期値オフ）。オフの環境は、サービスの対応操作の一覧にその操作を載せず、要求は `OPERATION_DENIED` になる。
- 課題プリセットは、その課題が必要とするときだけ明示的にオンにする（Reset課題が該当）。出所は仮定の記録（`challenge_service_operations`）に残す。
- 自由モードは、通常のジョブ送信だけでも試せる。抽象コマンドはその上に足す便宜として選ぶ。

## 課題を自由モードで再現する

課題カードに「この課題を自由モードで再現」を置く。目録の `free_equivalent` が、課題の**公開パラメーター**を自由モードの設定値として返す（領域、Reset成功確率、1回の繰り返し回数、抽象コマンドのオン）。隠れた値（秘密bit、隠しケース）は含めない。観測回数の上限と隠しケースによる採点は課題モード専用なので `not_reproduced` に載せ、画面にも明記する。

## しないこと

- ブラウザの画面に、学習・推定・自動化のコマンドや解析ビューは置かない。学習や推定は利用者のスクリプトに書く。画面に足すのは、スクリプトを書いて実行する場所だけ。
- サーバー側では、利用者のコードを実行しない（ブラウザ内の Worker では実行する。2026-10-14 に方針を改めた）。

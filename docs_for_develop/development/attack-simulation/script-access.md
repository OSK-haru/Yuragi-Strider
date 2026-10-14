# Pythonから攻撃する（スクリプトアクセス）

状態: 設計と実装（手元のPython 2026-10-13、画面のスクリプト実行 2026-10-14）。**手元のPython（トークン）はローカルの開発・デスクトップ用で、公開デプロイ（`public`）には入れない。** 画面のスクリプト実行はサーバーに新しい経路を足さず、cross-origin isolated な配信（開発サーバーとデスクトップ版）でだけ動く。

## 目的

攻撃者は実際にはブラウザのターミナルを手で叩くのではなく、手元のPythonで自動化する。画面の情報量を増やさず、**公開されているサービス操作をそのままPythonから使える**ようにする。自由度の源泉はPython側（回路の自作、複数ジョブの並列投入、待機と条件分岐、numpy等による学習）に置き、サービス側の制限（shots、命令数、観測予算、提出回数）は変えない。

## 方式

- 既存のWebSocketプロトコルと提出HTTPをそのまま使う。新しいサービス操作は作らない。
- 足すのは**認証経路だけ**。ブラウザのowner cookieはHttpOnlyで手元のPythonに渡せないため、所有者が画面から**スクリプト用トークン**を発行する。トークンは環境ごとに1つで、再発行すると前のものは無効になる。
- 所有者cookieの代わりに `Authorization: Bearer <token>` で所有者として認証する。対象は、WebSocket、提出HTTP（`/submission`）。トークンの発行自体はcookieとCSRFヘッダを要求する。
- サーバーにはトークンのSHA-256だけを保持する（owner cookieと同じ扱い）。環境の破棄・期限切れでトークンも消える。
- Bearer認証では `Origin` は要求しない（ブラウザ向けの防御で、非ブラウザには意味がない）。`Host` の許可リストは維持する。CSRFヘッダは不要（cookieを使わないため）。
- 設定 `script_access` は `local_development` と `desktop` だけが有効にする。`generic` と `public` では無効で、発行も認証も404／拒否になる。

## 見える範囲

Bearerで得られる権限は、そのブラウザの所有者がターミナルで持つ権限と同じ。隠れた値（被害者の秘密bit、隠しケース、環境内部の状態）に届く経路はない。課題モードの観測予算と提出回数は、サーバー側で同じ仕組みで検査される。

## Pythonクライアント

`attack_client/` パッケージ。Qiskit Runtimeに近い書き味にする。

```python
from attack_client import AttackSession, Circuit

session = AttackSession("http://127.0.0.1:8001", environment_id, token, credential)
circuit = Circuit(1).measure(0, 0)
job = session.run(circuit, shots=32, interleave="ref:0")
print(job.result().counts)
```

- `Circuit` は編集画面のJSONを組み立てる小さなビルダー。任意のゲート列を自作できる。結果のビット列は左端が c0（`"10"` は c0=1, c1=0）。
- `session.run(...)` は送信してジョブを返し、`job.result()` は完了まで結果照会を繰り返す。複数ジョブを先に投げてから結果を集められる。
- `session.answer(threshold, direction)` が判定規則の課題の提出、`session.answer(message=...)` がメッセージの課題の提出。通信部分の `answer` は提出本文の dict をそのまま送る。
- サンプルは `examples/reset_leakage_attack.py`（収集 → 学習 → 観測 → 推定 → 提出）。

## 画面のスクリプト実行（疑似シェル）

2026-10-14に利用者が、ブラウザ内で利用者のコードを実行することを認めた（下の「しないこと」の旧方針を改めた）。依存 `pyodide`（npm、MPL-2.0）も承認済み。

攻撃者ワークスペースの編集欄に「スクリプト」タブを置き、ファイルを書いてターミナルで `python <ファイル> [引数...]` と打つと実行する。SDKは手元のPythonと**同じ `attack_client`** で、接続情報を省略する。

```python
from attack_client import AttackSession, Circuit

with AttackSession() as session:          # 画面の環境へ接続する
    job = session.run(Circuit(1).measure(0, 0), shots=32)
    print(job.result().counts)
```

- **実行場所**: 専用の Web Worker の中の Pyodide（WebAssembly の CPython 3.14）。画面のDOM・cookie・資格情報には触れない。サーバー側では利用者のコードを実行しない。
- **同期の書き方**: Worker の Python は同期で書く。待っている間 Worker のイベントループは回らないため、応答を message で受け取れない。要求は postMessage で画面へ送り、応答は SharedArrayBuffer に書いてもらって `Atomics.wait` で待つ（`frontend/src/scripting/syncChannel.ts`）。呼出番号を照合し、中断で見捨てた古い応答は書かない。
- **接続**: スクリプトは画面とは別のWebSocket接続を1本持つ（手元のPythonと同じ）。資格情報はクライアントの中に留まり、Worker へは環境IDと公開の応答だけを返す。使える操作は回路送信・状態照会・結果照会・取消・提出だけ（`scriptServices.ts` の許可一覧）。
- **reply の配送**: 同じ所有者の接続が2本あると、reply は先に取りに来た接続へ渡っていた（画面と手元のPythonを同時に使っても同じ）。reply は**要求を出した接続へ返す**ように直した。その接続が閉じた後は、所有者のどの接続でも受け取れる（再読み込みと復帰のため）。`wait.request` は要求IDを名指しするので、別の接続の要求でも受け取れる（`api/attack_live_websocket.py` の `origin`）。
- **シェルとしての振る舞い**: 1本ずつ実行する。標準出力と標準エラーはターミナルへ出る。実行中に打った行は `input()` へ渡り、プロンプトは入力欄に出る。Ctrl+C は KeyboardInterrupt（終了コード130）。3秒で止まらなければ Worker ごと止める（137、次の実行で読み込み直す）。トレースバックは利用者のファイルからの行だけを見せる。`ls` や `cd` のような、シェルのふりをするだけの機能は足さない。
- **ファイル**: このブラウザの localStorage にだけ保存する（最大32ファイル・合計512K文字、`.py` の平らな名前）。保存できないときは画面を開いている間だけ使える。最初のファイルは `main.py` と `examples/reset_leakage_attack.py` の写しで、サンプルは接続情報を省略すると画面の環境へ接続する。
- **配信**: SharedArrayBuffer には cross-origin isolation（`COOP: same-origin`、`COEP: require-corp`）が要る。開発サーバー（`vite.config.ts`）とデスクトップ版（`api/main.py`）が全画面に付ける。Google Fonts は `crossorigin` で読む。Pyodide 本体（約13MB）はアプリが `/assets/python-runtime/` から配る。Pyodide は WebAssembly のコンパイルと自前の `eval` を使うため、**このパスだけ** CSP を `'wasm-unsafe-eval' 'unsafe-eval'` に緩める。画面自体の CSP は変えない。
- **追加パッケージ**: numpy 等は `import` を見て公式CDN（`cdn.jsdelivr.net/pyodide/v<版>/full/`）から読み、同梱の lock ファイルの SHA-256 で検査する。Worker の CSP の `connect-src` はこのCDNだけを足す。オフラインでは追加パッケージを使えない（[未決事項](open-questions.md#画面のスクリプト実行の残件)）。
- **使えない場所**: cross-origin isolated でない配信（現在の公開デモ）では、実行ボタンとコマンドが理由を出して止まる。

## 抽象コマンドの扱い

現実のサービスに無い操作（交互観測 `--interleave`）は**抽象コマンド**として分け、既定では無効にした。

- 環境設定の `qpu_service.abstract_commands.interleaved_observation`（自由モードの設定項目、初期値オフ）。オフの環境は、サービスの対応操作の一覧にその操作を載せず、要求は `OPERATION_DENIED` になる。
- 課題プリセットは、その課題が必要とするときだけ明示的にオンにする（Reset課題が該当）。出所は仮定の記録（`challenge_service_operations`）に残す。
- 自由モードは、通常のジョブ送信だけでも試せる。抽象コマンドはその上に足す便宜として選ぶ。

## 課題を自由モードで再現する

課題カードに「この課題を自由モードで再現」を置く。目録の `free_equivalent` が、課題の**公開パラメーター**を自由モードの設定値として返す（領域、Reset成功確率、1回の繰り返し回数、抽象コマンドのオン）。隠れた値（秘密bit、隠しケース）は含めない。観測回数の上限と隠しケースによる採点は課題モード専用なので `not_reproduced` に載せ、画面にも明記する。

## しないこと

- ブラウザの画面に、学習・推定・自動化のコマンドや解析ビューは置かない。学習や推定は利用者のスクリプトに書く。画面に足すのは、接続情報の案内と、スクリプトを書いて実行する場所だけ。
- サーバー側では、利用者のコードを実行しない（ブラウザ内の Worker では実行する。2026-10-14 に方針を改めた）。

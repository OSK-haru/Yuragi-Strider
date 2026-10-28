# Yuragi-Strider

Yuragi-Strider は、仮想量子サービスを攻撃者の立場から操作する攻撃シミュレーターです。ホーム画面では攻撃シミュレーターを主な入口に置き、量子回路の Gate-aware 実行と制御パルスの Pulse 実行は「物理エンジンを試す」にまとめています。

Gate-aware と Pulse は、温度・ノイズなどの環境が量子状態へ与える影響や、制御波形への応答を観察する独立した物理シミュレーターです。攻撃シミュレーターの体験と物理モデルの検証は別の目的として扱います。

現在の実装状況と文書の優先順位は
[`docs_for_develop/README.md`](docs_for_develop/README.md)を参照してください。
Rust移行と共通セキュリティ基盤の進捗・測定条件は
[`docs/migration/phase-0-audit.md`](docs/migration/phase-0-audit.md)と
[`docs/migration/rust-api-contract.md`](docs/migration/rust-api-contract.md)に記録しています。

## 標準構成

現行 UI は React/Vite、運用中の計算 API は FastAPI、標準シミュレーションコアはRustへ移行中です。Rust/Axum APIは `/api/health` と `/api/circuit/compile` の両モードを契約テストする並行実装で、まだ運用トラフィックを受けていません。その他のAPI経路はFastAPIが引き続き担当します。QuTiP と Python 実装は独立検証に使用します。Circuit Studio の既定backendは現在 `python_dense` です。`rust_dense_v1` は明示選択でき、未対応の実行経路ではPythonへ黙ってフォールバックせずエラーにします。既存の保存済み `.qscope` は記録されたbackendを維持します。攻撃用の新規共有QPU profileはRust register runtimeを選び、過去manifestでbackend指定がないものと汎用runtimeはPythonを維持します。測定乱数、読み出し誤り、状態イベント、API orchestrationなどは段階移行中です。

| 層 | 実装 | 役割 |
|---|---|---|
| Web UI | React 19、TypeScript 6、Vite 8 | 回路編集、パラメータ入力、結果表示 |
| API | FastAPI、Pydantic、Uvicorn | 入力検証とシミュレーション API |
| 計算コア | Python、NumPy、Rust | 密度行列、Lindblad 型時間発展、Reset、指標計算（Rustへ移行中） |
| 独立検証 | QuTiP、SciPy、Matplotlib | 物理モデルの比較検証と plot（任意） |
| Rust 拡張 | Rust、PyO3、maturin | Crosstalk 評価、選択式 dense kernel |

対応規模は、論理量子ビット 1〜18、ノイズありの密度行列計算は 8 まで、
明示的 CPTP は 5 までです。理想環境かつ測定なしの回路は 5 を超えると
状態ベクトル経路へ自動的に切り替わります。Circuit Studio の編集範囲は 2〜8 です。

## 必要なツール

- Git
- Python `3.14.4`（監査済み環境。`.python-version` に記録）
- Node.js `24.15.0` / npm `11.x`（`.nvmrc` と `frontend/package-lock.json` を使用）
- Rust `1.99.1` と maturin（Rust 拡張のビルド用）

Windows PowerShell の例を以下に示します。macOS/Linux では仮想環境の Python を `.venv/bin/python` に読み替えてください。

## セットアップ

```powershell
git clone https://github.com/OSK-haru/Yuragi-Strider.git
cd Yuragi-Strider
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
.\.venv\Scripts\python.exe -m pip install -e .\rust_kernels\yuragi_strider_rust
cd frontend
npm ci
cd ..
```

監査時と同じ Python パッケージ群を入れる場合は、`requirements-runtime.txt` の代わりに次を使います。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

## 標準 UI の起動

ターミナルを2つ開き、リポジトリ直下から API と UI をそれぞれ起動します。

```powershell
# Terminal 1: API (http://127.0.0.1:8001)
$env:ATTACK_SIMULATOR_PROFILE = 'development'
.\.venv\Scripts\python.exe -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8001
```

```powershell
# Terminal 2: UI (http://127.0.0.1:5173)
cd frontend
npm run dev
```

Vite は `/api` を `127.0.0.1:8001` にプロキシします。接続確認は次で行えます。

```powershell
.\.venv\Scripts\python.exe scripts\dev_server_doctor.py
```

## テストと静的チェック

```powershell
# Python テスト（標準ライブラリ unittest）
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

変更箇所に応じた短いテストプロファイルは次で実行できます。`full-audit` は
Pulse と凍結監査を含むため、リリース確認時だけ使用します。

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py fast
.\.venv\Scripts\python.exe scripts/run_tests.py gate-aware
.\.venv\Scripts\python.exe scripts/run_tests.py pulse
.\.venv\Scripts\python.exe scripts/run_tests.py full-audit
```

```powershell
# Frontend
cd frontend
npm run lint
npm run build
```

測定フィードフォワードの監査用プリセットとして API は
`bell`、`teleportation`、`bit_flip_repetition` を受け付けます。
後者2つは古典レジスタと条件付き補正を含み、分岐ごとの Gate-aware ノイズも
結果へ反映します。5量子ビットの Explicit CPTP は Choi 監査の計算量を抑えるため
RK4へ明示的にフォールバックします。

QuTiP を使う独立検証も含める場合:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-validation.txt
.\.venv\Scripts\python.exe -m unittest tests.test_validation_qutip_comparison
```

## Rust 拡張と dense preview backend

Rust toolchain が利用できる環境で、Python 仮想環境へローカル拡張をビルドします。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\rust_kernels\yuragi_strider_rust
.\.venv\Scripts\python.exe -c "import yuragi_strider_rust; print(yuragi_strider_rust.backend_name())"
```

Circuit Studio の dense backend は現在 `python_dense` を既定にしています。`rust_dense_v1` は厳格な選択肢で、Rust拡張がない場合、または statevector・post-circuit degradation など未移植経路を要求した場合は明示的に失敗します。既定切替はこれらの経路をRustへ移した後に行います。攻撃用の新規共有QPU profileは `rust_register_v1` を選び、Rust拡張が未導入・旧版の場合や未対応resetモデルでは明示的に失敗します。未移植機能と境界は [Rust register execution contract](docs/migration/rust-register-contract.md) を参照してください。Crosstalk 課題の評価はRust拡張を使用し、Docker イメージは拡張をビルドして同梱します。

## ディレクトリ構成

- `docs/submission/`: 作品概要、システム構成図、掲載用の図版（最初に読む資料）
- `frontend/`: React/Vite UI
- `api/`: FastAPI endpoint
- `core/`: UI 非依存のシミュレーション本体
- `data/`: preset と参照データ
- `tests/`: unittest テスト群
- `validation_pulse/`: Pulse Baseline A の再利用可能な検証helper
- `validation_cptp/`: 明示的 CPTP 経路の検証 script
- `validation_hardware/`: 実機データとの比較検証
- `validation_results/`: 機械可読な検証結果
- `scripts/`: 独立検証、benchmark、診断 script
- `rust_kernels/`: Pythonに依存しないRustコアとPyO3/maturin拡張
- `formalweb/website/`: Docusaurus 製の公開ドキュメントサイト
- `packaging/`, `desktop_app.py`: 任意の Windows desktop 配布用ランチャー
- `docs/development/`: 配布・運用手順
- `docs_for_develop/`: 要求・設計・物理モデル・検証レポート
- `docs_for_develop/environment.md`: 詳細な環境・依存ライブラリ台帳

## 再現性に関する注意

- secret や `.env` は不要です。`.env` 系ファイルは Git 管理対象外です。
- npm は `npm install` ではなく `npm ci` を使い、`frontend/package-lock.json` の固定版を再現してください。
- 通常導入は直接依存だけを固定した `requirements-runtime.txt`、監査済み環境の完全再現は `requirements-lock.txt` を使います。
- 従来の `requirements.txt` は既存環境との互換用に残しています。新規構築には上記2ファイルのどちらかを使ってください。
- 対応 OS はコード上 cross-platform ですが、この台帳は Windows で実測・検証しています。

詳細は [実行環境・技術スタック台帳](docs_for_develop/environment.md) を参照してください。

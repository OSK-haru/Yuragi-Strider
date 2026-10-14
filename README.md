# Yuragi-Strider

Yuragi-Strider は、温度・磁場・ノイズなどの物理環境が小規模量子回路の状態と有効寿命に与える影響を、対話的に理解するためのシミュレーターです。

現在の実装状況と文書の優先順位は
[`docs_for_develop/README.md`](docs_for_develop/README.md)を参照してください。

## 標準構成

現行 UI は React/Vite、計算 API は FastAPI、シミュレーション本体は Python/NumPy です。QuTiP は本番計算には使わず、独立ソルバーとの検証にだけ使用します。Rust カーネルは任意の preview 機能で、未導入でも Python backend で動作します。

| 層 | 実装 | 役割 |
|---|---|---|
| Web UI | React 19、TypeScript 6、Vite 8 | 回路編集、パラメータ入力、結果表示 |
| API | FastAPI、Pydantic、Uvicorn | 入力検証とシミュレーション API |
| 計算コア | Python、NumPy | 密度行列、Lindblad 型時間発展、指標計算 |
| 独立検証 | QuTiP、SciPy、Matplotlib | 物理モデルの比較検証と plot（任意） |
| 高速化 preview | Rust、PyO3、maturin | 任意の dense kernel |

対応規模は、論理量子ビット 1〜18、ノイズありの密度行列計算は 8 まで、
明示的 CPTP は 5 までです。理想環境かつ測定なしの回路は 5 を超えると
状態ベクトル経路へ自動的に切り替わります。Circuit Studio の編集範囲は 2〜8 です。

## 必要なツール

- Git
- Python `3.14.4`（監査済み環境。`.python-version` に記録）
- Node.js `24.15.0` / npm `11.x`（`.nvmrc` と `frontend/package-lock.json` を使用）
- Rust `1.96.0` は preview backend をビルドする場合のみ

Windows PowerShell の例を以下に示します。macOS/Linux では仮想環境の Python を `.venv/bin/python` に読み替えてください。

## セットアップ

```powershell
git clone https://github.com/OSK-haru/Yuragi-Strider.git
cd Yuragi-Strider
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
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

## 任意の Rust preview backend

Rust toolchain が利用できる環境で、Python 仮想環境へローカル拡張をビルドします。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\rust_kernels\yuragi_strider_rust
.\.venv\Scripts\python.exe -c "import yuragi_strider_rust; print(yuragi_strider_rust.backend_name())"
```

標準 backend は `python_dense` です。Rust 拡張がなくても通常利用とテストは可能で、Rust 専用テストは自動的に skip されます。

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
- `rust_kernels/`: 任意の PyO3/maturin 拡張
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

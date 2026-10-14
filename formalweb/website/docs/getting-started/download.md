---
title: クイックスタート
sidebar_position: 2
---

# クイックスタート

Yuragi-Striderを始める方法は3つあります。インストールせずに公開アプリを試す方法、Windows向けのローカルアプリをダウンロードして使う方法、GitHubからリポジトリをクローンして開発環境として起動する方法です。

## ブラウザですぐに試す

このドキュメントサイトの右上にある**「アプリを試してみる」**をクリックすると、公開中のYuragi-Striderアプリへ移動できます。環境構築やインストールは必要ありません。

ボタンが見つからない場合は、[Yuragi-Striderアプリを開く](https://yuragi-strider-app.23sam55781.workers.dev)から直接アクセスできます。

アプリが開いたら、まず「通常モードを開始」を選ぶとGate-awareシミュレーションを試せます。操作を確認しながら進めたい場合は、ホーム画面のガイドツアーを利用してください。

## Windowsアプリをダウンロードして使う

ローカル環境で動かしたいだけであれば、ビルド済みのWindowsアプリを使うのが最も簡単です。PythonやNode.jsのインストールは必要ありません。

このアプリは、Web版と同じReact製の画面とFastAPIの計算APIを1つの実行ファイルにまとめたものです。**シミュレーションはすべて手元のPCで実行され、外部のサーバーへは一切通信しません。** ネットワークに接続していない環境でも動作します。

### ダウンロード

[**最新版をダウンロード（GitHub Releases）**](https://github.com/OSK-haru/Quantsim/releases/latest)

Releasesページの **Assets** から `Yuragi-Strider-<バージョン>-windows-x64.zip` を選んでください。

| 項目 | 内容 |
|---|---|
| 対応OS | Windows 10 / 11（64-bit） |
| 形式 | ZIP（インストーラーではありません） |
| ダウンロードサイズ | 約 26 MB |
| 展開後サイズ | 約 58 MB |
| 事前準備 | 不要（Python・Node.js・Rustいずれも不要） |

### 使い方

1. ダウンロードしたZIPを右クリックし、**「すべて展開」** を選んで任意の場所（例: `ドキュメント\Yuragi-Strider`）に展開します。
2. 展開してできた `Yuragi-Strider` フォルダを開き、**`Yuragi-Strider.exe`** をダブルクリックします。
3. 数秒待つと、既定のブラウザでホーム画面（通常は `http://127.0.0.1:8765`）が自動的に開きます。

フォルダ内には `YuragiStriderBackend.exe` も入っていますが、これは計算を担う本体で、`Yuragi-Strider.exe` から自動的に起動されます。**直接実行する必要はありません。**

### 終了のしかた

起動中は、画面右下の通知領域（タスクトレイ）にYuragi-Striderのアイコンが表示されます。

- **終了する**: アイコンを右クリックし、**「Exit Yuragi-Strider」** を選びます。計算バックエンドも一緒に停止します。
- **画面を開き直す**: アイコンをダブルクリックすると、ブラウザでホーム画面を再表示できます。タブを誤って閉じてしまった場合に使えます。

ブラウザのタブを閉じるだけではアプリは終了しません。完全に終了するときは、必ずトレイアイコンから終了してください。

:::caution[初回起動時の警告について]
このアプリにはコード署名証明書を付けていないため、初回起動時にWindows SmartScreenの警告（「WindowsによってPCが保護されました」）が表示されることがあります。実行する場合は **「詳細情報」→「実行」** を選んでください。不安な場合は、下記のSHA256チェックサムで配布ファイルが改変されていないことを確認できます。
:::

:::info[ZIPは展開してから実行してください]
ZIPの中身を直接ダブルクリックすると、アプリに必要なファイルが揃わず起動に失敗します。必ず展開してから `Yuragi-Strider.exe` を実行してください。
:::

### うまく起動しないとき

診断ログが次の場所に出力されます。

```
%LOCALAPPDATA%\Yuragi-Strider\launcher.log
```

エクスプローラーのアドレス欄に上のパスを貼り付けると開けます。

ブラウザが自動的に開かない場合でも、アプリ自体は起動していることがあります。トレイアイコンをダブルクリックすると開き直せます。なお8765番ポートが他のソフトに使われている場合は自動的に空いているポートへ切り替わるため、アドレスが `8765` 以外になることがあります。実際に使われたアドレスは上記のログに記録されており、トレイアイコンからの再表示はこのアドレスを参照します。

### 配布ファイルの検証

ダウンロードしたZIPが正しいものかを確認するには、PowerShellで次を実行し、Releasesページに記載されたSHA256と一致することを確かめてください。

```powershell
Get-FileHash .\Yuragi-Strider-v0.1.0-windows-x64.zip -Algorithm SHA256
```

## GitHubからクローンして使う

コードを確認・変更したい場合や、テストを実行したい場合は、リポジトリを取得して開発環境を構築します。計算APIとWeb UIの2つのプロセスを起動します。単に使いたいだけであれば、上記のWindowsアプリのほうが手軽です。

## 必要なツール

| ツール | バージョン | 用途 |
|---|---|---|
| Git | — | リポジトリの取得 |
| Python | `3.14.4` | 計算コアとAPI(監査済みの版。`.python-version` に記録) |
| Node.js / npm | `24.15.0` / `11.x` | Web UI(`.nvmrc` と `package-lock.json` に記録) |
| Rust | `1.96.0` | 任意。[Rustによる高速化](../performance/rust-acceleration.md)を使う場合のみ |

Rustは必須ではありません。導入しなくても標準のPython backend (`python_dense`) で全機能が動作します。

## セットアップ

以下はWindows PowerShellでの例です。macOS / Linuxでは仮想環境のPythonを `.venv/bin/python` に読み替えてください。

```powershell
git clone <repository-url>
cd Quantum-sim
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
cd frontend
npm ci
cd ..
```

監査時とまったく同じPythonパッケージ群を再現する場合は、`requirements-runtime.txt` の代わりに `requirements-lock.txt` を使います。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
```

## 起動

ターミナルを2つ開き、リポジトリ直下からAPIとUIをそれぞれ起動します。**両方が動いている必要があります。**

```powershell
# ターミナル 1: 計算API (http://127.0.0.1:8001)
.\.venv\Scripts\python.exe -m uvicorn api.main:app --reload --host 127.0.0.1 --port 8001
```

```powershell
# ターミナル 2: Web UI (http://127.0.0.1:5173)
cd frontend
npm run dev
```

ブラウザで `http://127.0.0.1:5173` を開くとホーム画面が表示されます。Viteが `/api` へのリクエストを `127.0.0.1:8001` にプロキシします。

接続がうまくいかない場合は、診断スクリプトで確認できます。

```powershell
.\.venv\Scripts\python.exe scripts\dev_server_doctor.py
```

## 動作確認

```powershell
.\.venv\Scripts\python.exe scripts/run_tests.py fast
```

`fast` のほかに `gate-aware`、`pulse`、`full-audit` のプロファイルがあります。`full-audit` はPulseと凍結監査を含むため時間がかかります。

## 任意: Rust preview backend

Rust toolchainがある環境では、高速化カーネルをPython仮想環境にビルドできます。

```powershell
.\.venv\Scripts\python.exe -m pip install -e .\rust_kernels\yuragi_strider_rust
.\.venv\Scripts\python.exe -c "import yuragi_strider_rust; print(yuragi_strider_rust.backend_name())"
```

適用範囲と実測値は[Rustによる高速化](../performance/rust-acceleration.md)を参照してください。

## 次に読む

起動できたら、[画面別チュートリアル](../tutorials/index.md)から操作を確認してください。まずはGate-awareモードの[IDEワークスペース](../tutorials/gate-aware/index.md)が入口になります。

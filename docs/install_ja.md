# インストールガイド

[English](install.md) | 日本語

コマンドはリポジトリのルートで実行します。このガイドは OS 別の導入、ツールの
利用可否、2つのローカル Web アプリを扱います。AWS 認証とモデルアクセスは
[認証と環境構築](setup.md)、Python API の例は [Quickstart](quickstart.md) と
[README のコード例](../README_ja.md#クイックスタート)を参照してください。

## 1. 前提条件

- **Python 3.11 以上**、`pip`、Git。CI の対象は Python 3.11/3.12 です。
  それ以降のバージョンは継続検証していません。任意の Sokudan 分類器には
  **Python 3.11** が必要です（3.12 以上では `jev` の依存はスキップ）。
- この GitHub リポジトリへのアクセス権。通常の Git 認証を使用し、clone URL に
  トークンを埋め込まないでください。
- ライブチャットには、選択した Bedrock モデルを利用できる AWS アカウントが必要です。
  AWS CLI v2・認証・IAM・リージョン設定は [setup.md](setup.md) を参照してください。
  観測コンソールには AWS エージェントは不要です。
- ローカル設定・状態・ログを書き込めるチェックアウト。認証情報、非公開の記憶、
  生成された状態ファイルをコミットしないでください。

依存関係の正本は [pyproject.toml](../pyproject.toml) です。Flask は**基本依存に
含まれません**。どちらの Web アプリにも `dashboard` extra が必要です
（`dev` extra にも Flask が含まれ、テスト・lint ツールも入ります）。
`harness` extra は公式 `make_subagent` アダプターを使う場合に限り必要です。
`ja-rules` extra（ML-Ask と pip 版の IPADIC。システムの MeCab は不要）はオプトインの日本語ルール分類器を
使う場合に限り必要です: [ja-rules-classifier_ja.md](ja-rules-classifier_ja.md)。

## 2. Linux（Ubuntu / Debian）

CI は `ubuntu-latest` で実行します。Debian や他のディストリビューションは CI
対象ではありません。Ubuntu 24.04 の Python 3.12、Debian 12 の Python 3.11 は
要件を満たします。古いディストリビューションでは仮想環境作成前にバージョンを確認します。

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip git
python3 --version  # 3.11 以上であること

git clone https://github.com/Resolver-TNG/StiraCore.git
cd StiraCore
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
# Web アプリと検証ツール:
python -m pip install -e '.[dev]'
```

システム全体への `sudo pip` は避け、仮想環境を使用します（ディストリビューションの
Python は externally managed の場合があります）。依存のソースビルドが必要な場合は、
対応する Python の開発用ヘッダーと `build-essential` を導入してください。
任意の `cron` ツールにはシステムの `crontab` コマンドが必要で、ジョブ実行には cron
サービスの稼働も必要です。最小構成のコンテナでは両方とも存在しない場合があります。

## 3. macOS

Apple のシステム Python ではなく Homebrew の Python を使用します。Homebrew が
導入済みの環境で実行してください。

```bash
brew install python@3.11 git

git clone https://github.com/Resolver-TNG/StiraCore.git
cd StiraCore
"$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
# Web アプリと検証ツール:
python -m pip install -e '.[dev]'
```

python.org の Python 3.11 インストーラでも構いません。その場合は venv を
`python3.11 -m venv .venv` で作ります。macOS では venv を有効化するまで `python`
コマンドが無いことがあるため、最初の手順は `python3`/`python3.11` を使います。

Apple Silicon ではターミナル、Python、ネイティブ依存のアーキテクチャを統一します
（通常は arm64）。arm64 の環境に Rosetta/x86_64 のパッケージを混在させないでください。
`python -c "import platform; print(platform.machine())"` で確認できます。
pip がネイティブのソースビルドへ切り替わる場合は、Xcode Command Line Tools
（`xcode-select --install`）とビルドエラーに示されたライブラリを導入します。
対応 wheel を優先し、警告を消すためだけにすべての任意依存を入れないでください。

## 4. Windows（PowerShell）

> `dashboard` extra と起動スモーク（`scripts/smoke_server.py`）は **Windows では
> 未検証**です。以下の手順は変えていません。CI が動けば `windows-latest` のセルで
> 同じスモークを実行します。

Python ランチャー（`py`）付きの Python 3.11 以上と Git for Windows を導入し、
PowerShell を開き直します。以下は Python 3.11 を明示的に選ぶ例です。

```powershell
py -3.11 --version
git --version

git clone https://github.com/Resolver-TNG/StiraCore.git
cd StiraCore
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
# Web アプリと検証ツール:
python -m pip install -e '.[dev]'
```

### PowerShell 実行ポリシー

有効化スクリプトがブロックされても、**仮想環境の有効化は必須ではありません**。
ポリシーを変更せず、インタープリターを直接指定してインストール・起動できます。

```powershell
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe dashboard/stira_server.py
```

組織のポリシーでレビュー済みのローカルスクリプトの実行が許可されている場合は、まず
`Get-ExecutionPolicy -List` で確認します。
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned` を使用すると、
その PowerShell セッションだけに適用して再度有効化できます。マシン全体のポリシーを
変更したり、Group Policy を回避したりせず、直接インタープリターを使ってください。

### UTF-8 とネイティブ依存

日本語 Windows は既定のエンコーディングが **cp932** になる場合があります。
Python 起動前に同じ PowerShell セッションで UTF-8 モードと標準ストリームの
エンコーディングを設定します。

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
```

ドキュメントや設定は UTF-8 で保存してください。`chcp 65001` だけでは Python の
ファイル入出力・リダイレクト先のすべての問題は解消しません。シンボリックリンクを
使う一部のテストには Windows の開発者モードが必要です。Windows ネイティブ環境には
POSIX の `termios`/`fcntl`/PTY や `crontab` がありません。後述の対応ツールを使うか、
別の WSL 環境で Linux 手順を実行します（Windows の venv を WSL と共有しないこと）。

### Windows での POSIX 専用ガード (#540)

Windows の Python 3.11 には POSIX の関数がいくつか無い。Windows での動作
(`src/stiracore/_platform.py`):

- 監査チェーンとスケジューラのログのヘッドに所有者のみ `0600` を**付けない**
  (`os.fchmod` が無く、Windows の権限ビットは読み取り専用だけを表す)。データ
  ディレクトリはユーザープロファイル内に置き、その ACL でファイルを守ること。
- 削除した記憶の復元で時刻を設定するとき、`os.utime` がリンク拒否できない環境では
  先に `lstat` でシンボリックリンクを拒否する。
- YOLO のホスト**コマンド**経路(Unix ソケット・相手の資格情報)は提供しない。
  fail-closed で、ホスト確認の操作は拒否される(`host_channel_unavailable`)。
  ダッシュボード(ブラウザ)での確認を使う。
- `run_checks` と `page_check` は実行を拒否する(`*_platform`)。プロセスグループの kill は行わない。

### 任意のプロセス制御スクリプト

[`scripts/stira_ctl.ps1`](../scripts/stira_ctl.ps1) でチャットサーバーを起動・確認・
停止・再起動できます。**参照先は `.venv` ではなく、固定の
`.venv311\Scripts\python.exe` です。** 使用する場合は別の環境を一度作成します。

```powershell
py -3.11 -m venv .venv311
.\.venv311\Scripts\python.exe -m pip install --upgrade pip
.\.venv311\Scripts\python.exe -m pip install -e '.[dev]'
# 任意の日本語 Sokudan 分類器（Python 3.11 のみ）:
# .\.venv311\Scripts\python.exe -m pip install -e '.[jev]'

.\scripts\stira_ctl.ps1 start
.\scripts\stira_ctl.ps1 status
# 使用終了後:
.\scripts\stira_ctl.ps1 stop
# 設定変更後の再起動:
# .\scripts\stira_ctl.ps1 restart
```

ローカルスクリプトの実行が許可されている場合にのみ使用してください。それ以外は
`.venv` のインタープリターでフォアグラウンド起動し、Ctrl+C で停止します。
`start` は起動要求のみ、`status` が `/api/gateway/status` を確認します。
ログは `.local/stira.out.log` と `.local/stira.err.log` です。スクリプトは
`STIRA_PORT` 未設定なら 5555 を使用し、gateway 設定のポートは直接読みません。
分類器の初回初期化には通常より時間がかかる場合があります。

## 5. AWS と環境変数

> AWS が必要なのは **Bedrock** プロバイダ（既定）を使う場合だけです。AWS 無しで動かす場合は
> [providers_ja.md](providers_ja.md) の Ollama / llama.cpp / API プロバイダを使い、この節の
> `AWS_*` 変数は不要です。

ライブチャット前に [認証と環境構築](setup.md) を完了してください。認証情報、
プロファイル/SSO、IAM、モデルアクセスの手順は同ガイドに集約しています。

**Bedrock には AWS 認証が必須ですが、すべての `AWS_*` 環境変数の設定が必須では
ありません。** プロファイル、SSO、実行環境のロールを使えばアクセスキーの環境変数は
不要です。ツール登録は import 時に行うため、任意のツール用変数もプロセス起動前に設定します。

| 変数 | 必須か | 用途・注意点 |
|------|--------|-------------|
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | 環境変数によるキー認証時のみ | 認証キーの組。プロファイル/SSO/ロール利用時は不要。setup.md 参照。 |
| `AWS_SESSION_TOKEN` | 一時認証情報を環境変数で指定する場合 | 長期キーの場合は未設定にします。 |
| `AWS_PROFILE` | 名前付きプロファイル/SSO プロファイル選択時 | boto3 の認証チェーンを使用します。 |
| `AWS_REGION` | 任意の上書き。有効なリージョンは必要 | stira_server.py が明示的に参照。未設定時は gateway 設定（既定 ap-northeast-1）を使用。 |
| `AWS_DEFAULT_REGION` | SDK/CLI の任意の既定値 | 明示的なリージョンや model factory の既定値を上書きしません。チャットサーバーの明示的な上書き変数ではありません。 |
| `PERPLEXITY_API_KEY` | 任意 | 空でなければ perplexity_search を登録。Perplexity に通信し、料金が発生する場合があります。 |
| `STRANDS_KNOWLEDGE_BASE_ID` | 任意 | 標準の Bedrock Knowledge Base memory ツールを有効化。対応する AWS 権限が必要。ローカル Stira 記憶には不要。 |
| `STIRA_WEB_SEARCH_ENDPOINT` | 任意 | 汎用 web_search 用。未設定時は設定エラーを返します。Perplexity とは独立。[endpoint 契約](tool-audit.md#web検索endpoint契約)参照。 |
| `STIRA_MODEL` | 任意 | チャットサーバーのモデル ID の上書き（既定は Bedrock のモデル/推論プロファイル ID）。 |
| `STIRA_PROVIDER`, `STIRA_BASE_URL`, `STIRA_API_KEY_ENV` | 任意 | Bedrock 以外のプロバイダ、その接続先、API キーを持つ環境変数の名前を指定。[providers_ja.md](providers_ja.md) 参照。 |
| `STIRA_PORT` | 任意 | チャットサーバー/PowerShell 補助スクリプトのポート（既定 5555）。Flask CLI のポート指定とは別。 |
| `STIRA_STATE_PATH` | 任意 | 観測コンソールの状態ファイル。指定だけでチャットサーバーに自動接続するわけではありません。 |
| `PYTHONUTF8`, `PYTHONIOENCODING` | Windows で推奨 | cp932 問題を避けるため、それぞれ 1、utf-8 を指定します。 |

実際の認証情報をドキュメント、シェルスクリプト、Git コミットに含めないでください。

## 6. OS 別ツール互換性

情報源は[ツール棚卸し](tool-audit.md)、現行の
[`READ_TOOLS` / `WRITE_TOOLS` registry](../src/stiracore/tools/tool_registry.py)、
および上流 `strands-agents-tools` 0.8.9 の依存・ソース確認です。棚卸しは採否の記録であり、
**OS ごとの動作試験結果ではありません**。以下はネイティブ OS で期待される対応状況で、
全 OS でのエンドツーエンド実行成功を示しません。インストールはバージョン固定ではないため、
依存の更新で変わる場合があります。

- ✅ = 登録候補で、今回の確認では OS 固有の障害が見つかっていないもの。
  依存の import 成功、権限、サービス設定などは別途必要です。
- ⚠️ = 追加依存・サービス・モード制約あり。備考を参照してください。
- ❌ = このハーネス/OS の組み合わせでは利用不可。OS 制約か意図的な未登録かを区別します。
- READ/WRITE は registry のポリシー分類であり、外部通信の有無を保証しません。
  新規 `ToolConfig` は `trust_all` ですが breaker が優先されます。
  未対応/不足依存による import 失敗はログに記録してスキップするため、実際の登録一覧を確認します。

| Tool | Linux | macOS | Windows | 備考 |
|------|-------|-------|---------|------|
| shell | ✅ | ✅ | ❌ | WRITE。POSIX termios/PTY 依存（pty/tty 経由で fcntl にも依存）。PowerShell ツールではありません。 |
| file_read | ✅ | ✅ | ✅ | READ。サイズ制限付きローカル UTF-8/BOM テキスト読み取り。 |
| view_image | ✅ | ✅ | ✅ | READ。Pillow による PNG/JPEG/GIF/WebP 読み取り。サイズ制限あり。 |
| image_reader | ✅ | ✅ | ✅ | READ。上流の互換ツール。制限付き view_image を推奨。 |
| current_time | ✅ | ✅ | ✅ | READ。ローカル時刻の参照。 |
| stop | ✅ | ✅ | ✅ | READ。対話制御。 |
| handoff_to_user | ✅ | ✅ | ✅ | READ。対応する対話入力チャネルが必要。 |
| file_write | ✅ | ✅ | ✅ | WRITE。ファイル権限・パスの制約に従います。 |
| editor | ✅ | ✅ | ✅ | WRITE。ファイル権限・パスの制約に従います。 |
| http_request | ✅ | ✅ | ✅ | WRITE。ネットワーク接続が必要。 |
| web_fetch | ✅ | ✅ | ✅ | WRITE。制限付き HTTP(S) テキスト取得。ブラウザ操作ではありません。 |
| web_search | ⚠️ | ⚠️ | ⚠️ | WRITE。STIRA_WEB_SEARCH_ENDPOINT が必要。 |
| perplexity_search | ⚠️ | ⚠️ | ⚠️ | 外部通信しますが現行 registry では READ。PERPLEXITY_API_KEY が必要。 |
| memory | ⚠️ | ⚠️ | ⚠️ | WRITE。STRANDS_KNOWLEDGE_BASE_ID と Bedrock Knowledge Base へのアクセスが必要。 |
| journal | ✅ | ✅ | ✅ | WRITE。ローカル永続化。 |
| cron | ⚠️ | ⚠️ | ❌ | WRITE。システムの crontab が必要。Windows ネイティブにはありません。import できても実行できない場合があります。 |
| rss | ⚠️ | ⚠️ | ⚠️ | WRITE。任意依存の feedparser とネットワーク接続が必要。 |
| diagram | ⚠️ | ⚠️ | ⚠️ | WRITE。任意依存の diagrams/graphviz/matplotlib/networkx。Graphviz 出力には dot 実行ファイルも必要。 |
| speak | ⚠️ | ⚠️ | ⚠️ | WRITE。fast 再生は macOS say、Polly 再生は macOS afplay を使用。Linux/Windows は Polly を play_audio=false で使用し、AWS Polly 権限を用意。 |
| batch | ✅ | ✅ | ✅ | WRITE。呼び出す各ツールの OS/サービス要件は残ります。 |
| think | ✅ | ✅ | ✅ | WRITE・exec 系(`STIRA_ENABLE_EXEC_TOOLS=1` 以外では非公開)。親のツールを NXP フック無しの内側 agent で再実行します。 |
| graph | ✅ | ✅ | ✅ | WRITE。子 agent/model/tool の設定が必要。 |
| use_agent | ✅ | ✅ | ✅ | WRITE。子 agent/model/tool の設定が必要。 |
| swarm | ✅ | ✅ | ✅ | WRITE。子 agent/model/tool の設定が必要。 |
| browser | ❌ | ❌ | ❌ | 全 OS で未登録。上流の browser backend には別途ブラウザ/セッション設定が必要。 |
| use_computer | ❌ | ❌ | ❌ | 未登録。任意の GUI/OpenCV（cv2）依存は基本パッケージに含まれません。 |

`browser` は**単なる Windows の cv2 エラーではありません**。確認した 0.8.9 では
OpenCV は `use-computer` extra の依存で、browser backend は Playwright を使用します。
どちらもこのハーネスの allowlist にないため、cv2 を追加するだけでは有効になりません。
棚卸しの他の「保留/不要」項目も全 OS で未登録です。`trust_all` は49モジュール全体を
自動 import する設定ではありません。

agent ごとのローカル記憶ツール（`memory_read`、`memory_search`、`memory_list`、
`memory_write`）は `create_memory_tools(store)` で生成し、チャットサーバーが別途接続します。
AWS Knowledge Base ではなくローカル Markdown/SQLite を使います。
MCP ツールは接続先サーバーの設定に依存し、インストールだけでは有効になりません。

任意の RSS/diagram 機能は必要な場合のみ導入してください。

```bash
python -m pip install 'strands-agents-tools[rss,diagram]'
# Graphviz レンダラーが必要な場合（OS に合わせて選択）:
# Ubuntu/Debian: sudo apt install graphviz
# macOS: brew install graphviz
# Windows: Graphviz を導入し bin ディレクトリを PATH に追加
```

ツールを実行せず、登録名を確認できます（スキップされたものは警告に表示されます）。

```bash
python -c "from stiracore.tools.tool_registry import READ_TOOLS, WRITE_TOOLS; print('READ:', sorted(READ_TOOLS)); print('WRITE:', sorted(WRITE_TOOLS))"
```

## 7. Web アプリの起動

仮想環境を有効化して `.[dashboard]`（または `.[dev]`）を導入し、リポジトリのルートから
実行します。
**ポート 5555 ではどちらか一方だけを起動してください。** 同じ agent の別 URL ではなく、
独立した Flask アプリです。loopback にバインドし、別途認証・デプロイ設計を行わずに
開発サーバーを公開しないでください。

### 観測コンソール（ライブチャット agent なし）

```bash
python -m flask --app dashboard/app.py run --host 127.0.0.1 --port 5555
```

<http://127.0.0.1:5555> を開きます。状態・刺激の確認と設定操作を行うアプリで、
既定では **Bedrock agent を生成せず、ライブチャットも提供しません**。
独自の状態ファイル（`STIRA_STATE_PATH` で変更可能）を使い、チャットサーバーへ自動接続しません。
`python -m flask --app dashboard/app.py run` だけでは Flask 既定の **5000 番**に
なるため、このガイドの URL を使う場合は `--port 5555` を付けます。
代わりに `python -m dashboard.app` を使うと、127.0.0.1:5555 に明示的にバインドします。

### チャット UI（ライブ Bedrock agent）

[AWS 設定](setup.md)を完了してから実行します。

```bash
python dashboard/stira_server.py
```

<http://127.0.0.1:5555/chat> を開きます（ルート URL からもリダイレクトされます）。
このアプリは `NXPAgent` を生成し、サンプルの SOUL（リゾルバ） を読み込み、
`.local/stira` にローカル記憶と状態を保存します。チャット送信は Bedrock を呼び、
登録ツールを実行できるため、モデル/外部サービスの料金が発生する場合があります。
ポートは `STIRA_PORT`、gateway 設定の順に決まり、既定値は 5555 です。
観測コンソールの設定ルートすべてを提供するわけではありません。

サーバーは AWS の認証情報が無くても起動し、HTTP に応答します。モデルに接続するのは
チャット送信時だけです。ポートで待ち受けできない場合（使用中、起動中に使われた場合も含む）、配信を始める前に
`Stira: port 5555 is already in use …`（終了コード 4）を表示して終了し、
`STIRA_PORT` を案内します。同じ checkout の 2 台目のサーバーは、ポートに関係なく
データディレクトリのロックで先に拒否されます（終了コード 3）。

フォアグラウンド起動は Ctrl+C で停止します。Windows のバックグラウンド補助スクリプトは
`stira_ctl.ps1 stop` で停止してください。同一ポートに2つ目のサーバーを起動しないこと。

## 8. ヘルスチェックとトラブルシューティング

1. <http://127.0.0.1:5555> を開き、意図したアプリが表示されることを確認します。
   共通の `/health` ルートはありません。以下のアプリ別 endpoint を使います。
2. 観測コンソール: GET `/api/state` が HTTP 200 と JSON を返すことを確認します。
3. チャットサーバー: GET `/api/gateway/status` が HTTP 200 と JSON を返すことを確認します。
   これはアプリの応答確認であり、**Bedrock 呼び出し成功の証明ではありません**。
   認証を確認したうえで短いチャットを送信し、モデルの動作を確認します。

```bash
# Linux / macOS: 起動したアプリに対応する endpoint を選択
curl --fail http://127.0.0.1:5555/api/state
# チャットサーバーの場合はこちら:
curl --fail http://127.0.0.1:5555/api/gateway/status
```

```powershell
# Windows: 起動したアプリに対応する endpoint を選択
Invoke-RestMethod http://127.0.0.1:5555/api/state
# チャットサーバーの場合はこちら:
Invoke-RestMethod http://127.0.0.1:5555/api/gateway/status
```

新しい checkout が起動できるかは、ポートを選ばず・モデルを呼ばずに確認できます。

```bash
python scripts/smoke_server.py   # 空きポートでチャットサーバーを起動し、
                                 # GET /api/config と /api/gateway/status が 200 なら停止
```

成功すると `smoke: PASS` を表示して 0 で終了し、失敗時はサーバーの stderr の末尾 20 行を
マスクして表示し 1 で終了します。PASS には、起動したプロセス自身であると識別できること
（ランダムな名前の自分の一時ディレクトリ内の workspace を報告する）と、SIGTERM で強制 kill
なしに自分で停止することも必要です。データディレクトリ・ログ・`HOME`・一時ディレクトリは
1 つの一時ディレクトリにまとめ、終了後に削除します。checkout の `.local/` は使わないため、
動いているサーバーの横でも実行できます。サーバーに渡す環境変数は許可リストのみです
（シェルの認証情報・`AWS_*`・プロキシ設定は渡しません）。通信先は 127.0.0.1 のみで、
プロキシもリダイレクトも使いません。

CI は `smoke` ジョブで同じコマンドを ubuntu・macOS・Windows で実行します。トークンは
読み取り専用（`permissions: contents: read`）、`persist-credentials: false`、secrets は
使わず、起動契機は `push`/`pull_request` です（`pull_request_target` は使いません）。
残課題: action はテストジョブと同じくタグ指定です。リポジトリ公開後に commit SHA で固定します。

- **No module named flask:** 起動に使う Python で `.[dashboard]`（または `.[dev]`）を
  インストールしてください。
- **Address already in use / `Stira: port … is already in use`:** もう一方のアプリを停止するか、
  別ポートを指定します（Flask CLI は `--port`、チャットは `STIRA_PORT`）。確認 URL も変更して
  ください。macOS/Linux では `lsof -nP -iTCP:5555 -sTCP:LISTEN` で使用中のプロセスが分かります。
  `scripts/stira_ctl.sh start` もそれを表示して起動を中止します。
- **認証/モデルのエラー:** [setup.md](setup.md) を参照します。HTTP の応答だけでは
  IAM/モデルアクセスを証明できません。調査時に秘密情報を出力しないでください。
- **Skipping strands_tools…:** 互換表と任意依存の警告を確認します。登録できたツールも、
  実行ファイルや外部サービスが不足していれば実行時に失敗する場合があります。
- **Windows の文字コードエラー:** 起動前に2つの UTF-8 変数を設定します。すでに動作中の
  プロセスは停止・再起動し、変更した環境変数を引き継がせてください。

### モデルにつながらないときのチャットのエラー

モデル呼び出しが失敗すると、チャット・Stage・ターン記録(`turn.completed` の `failure`、
`failure_http_status` / `failure_provider` / `failure_host`)に種類が出ます。表示するのは
コード・ステータス・プロバイダ名・ホスト名だけで、プロバイダのエラー本文やリクエスト内容は出しません。
それ以外の失敗は従来どおり「応答処理に失敗しました」です。

| コード (`failure`) | メッセージ | 次の一手 |
|---|---|---|
| `refused` | モデルのサーバーに接続できません(接続拒否)。 | サーバーが起動しているか、設定のエンドポイントを確認してください |
| `dns_failed` | モデルの接続先のホスト名を解決できません。 | 設定のエンドポイントを確認してください |
| `unreachable` | モデルの接続先に到達できません。 | ネットワークと設定のエンドポイントを確認してください |
| `timeout` | モデルの応答がタイムアウトしました。 | サーバーの負荷と接続先を確認してください |
| `auth_failed` | モデルの認証に失敗しました(401/403)。 | API キーまたは権限を確認してください |
| `model_not_found` | 指定したモデルが見つかりません(404)。 | 設定のモデル名を確認してください |
| `throttled` | レート制限に達しました(429)。 | しばらく待ってから再送してください |
| `server_error` | モデル側のサーバーエラーです(5xx)。 | 時間をおいて再送してください |
| `missing_credential` | モデルの認証情報がありません。 | API キーの環境変数または AWS の認証情報を設定してください |
| `provider_unavailable` | このプロバイダの追加パッケージが入っていません。 | インストールしてから再起動してください |
| `bad_request` | モデルがリクエストを受け付けませんでした(4xx)。 | モデル名と設定を確認してください |
| `context_overflow` | 会話がモデルの入力上限を超えました。 | 新しい会話を始めてください |
| `ends_with_assistant` | モデルへの入力がモデル自身の返答で終わったため拒否されました。ツール呼び出しが文章として返され、ツール結果が続かなかった場合などに起きます(Bedrock: assistant の prefill 非対応) | もう一度送るか、別のモデルを選んでください(#256) |
| `context_window_too_small` | SOUL・Identity Anchors・ツール定義だけで既知のコンテキスト窓の 9 割を超えるため送信しませんでした(サーバーが SOUL を黙って捨てる恐れがあるため) | 窓を増やす(`context_window_tokens` / サーバーの設定)、SOUL を短くする、窓の大きいモデルを使う、ツール定義が大きい場合は `context_profile=small`/`tiny` を試す([token-estimate.md](token-estimate.md)) |

llama.cpp の 503(モデル読み込み中・混雑)は `throttled` として出ます。

## 9. 開発用の検証

先に `.[dev]` を導入します。有効化済みの仮想環境で実行してください。

```bash
python -m pytest tests/ -x
ruff check .
ruff format --check .
```

Linux/macOS で有効化しない場合は `.venv/bin/python -m pytest tests/ -x`、
`.venv/bin/ruff check .`、`.venv/bin/ruff format --check .` を使います。
Windows では `.\.venv\Scripts\python.exe -m pytest tests/ -x` と
`.\.venv\Scripts\ruff.exe` による2つの Ruff コマンドを使用します
（プロセス制御用環境なら `.venv311` に置き換え）。ローカルテストの成功は、
全 OS での任意ツール・外部サービスすべての動作保証ではありません。

## 10. macOS（Apple Silicon）でチャットサーバーを使う簡易手順

Python 3.11 の venv を使います（Sokudan の `jev` extra は 3.11 が必要です）。
Flask は `dashboard` extra に含まれます。チャットサーバーの起動だけなら `dev` は不要です。

```bash
"$(brew --prefix python@3.11)/bin/python3.11" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[jev,dashboard]"
```

python.org のインストーラの場合、1 行目は `python3.11 -m venv .venv` にします。
sokudan は torch 不要です。サーバー自体は数秒で HTTP に応答します。Sokudan のモデル
（約 1.2 GB、Hugging Face のキャッシュに保存）は **最初のチャット** で取得するため、
そのターンはネットワーク次第で約 30 秒以上かかります。取得後の推論は CPU で約 20 ms です。

bash 補助スクリプトで起動・確認・停止します。

```bash
scripts/stira_ctl.sh start
scripts/stira_ctl.sh status
scripts/stira_ctl.sh stop      # restart, distill も利用可
```

使用する Python は `${STIRA_PYTHON:-.venv/bin/python}` です（無ければ `python3`）。
PID は `.local/stira/stira_server.pid` に記録します。`stop` が停止するのはこの PID だけで、
停止前にこの checkout の `dashboard/stira_server.py` を実行中であることを確認します。
ポート 5555 が使用中の場合、`start` は使用中のプロセスを表示して起動を中止します。
その場合は別ポートを指定してください（例: `STIRA_PORT=5556 scripts/stira_ctl.sh start`）。
`status` にも同じ `STIRA_PORT` を指定します。


## 11. 環境の比較：EC2 / Mac / Windows

同じソフトウェアが、典型的な 3 つの構成で動きます。どれかを優先するものではありません。**Mac** や **Windows** の PC は
パートナー AI の通常の構成（機械の前に人が 1 人いる、ローカル稼働。このガイドの主な経路）です。常時稼働・ヘッドレスにしたいときの選択肢として、
**EC2** のような Linux ホストがあります。EC2 の列は [deploy-ec2_ja.md](deploy-ec2_ja.md) の参考構成の要約で、非公開での待受けやインスタンスロールの
注意はその構成にだけ当てはまります。*未検証* と書いたものは、そのプラットフォームでは実行していません。

| | Mac（パートナー AI） | Windows（パートナー AI） | EC2（任意・常時稼働ホスト） |
|---|---|---|---|
| 起動 | `scripts/stira_ctl.sh start`（`nohup` でバックグラウンド、PID ファイル）、または launchd（§12） | `scripts\stira_ctl.ps1 start`（別の `.venv311` が必要）、またはタスクスケジューラ（§12、未検証） | `systemctl start stiracore`（`Type=simple` のユニット。[deploy-ec2_ja.md §5](deploy-ec2_ja.md#5-サービスとして動かすsystemd)）。`stira_ctl.sh` は使わない |
| 動き続けるか | ログイン時に launchd の LaunchAgent（§12 は自動再起動なし）。それ以外は止めるかログアウトするまで | ログオン時にタスクスケジューラ（未検証） | systemd: `Restart=on-failure`、ブート時に起動 |
| 停止 | `stira_ctl.sh stop`（SIGTERM）か `launchctl bootout`: 正常停止 | `stira_ctl.ps1 stop`: `taskkill /F`。**正常停止の処理は走らない**（[operations_ja.md §1](operations_ja.md#1-起動停止状態確認)） | `systemctl stop`: SIGTERM、正常停止の処理が走る |
| 到達できる相手 | ブラウザから `127.0.0.1` | ブラウザから `127.0.0.1` | ループバックのみ。SSM ポートフォワードなどのトンネルで到達（未検証）。公開アドレスは使わない |
| Bedrock の認証 | プロファイル / SSO / 環境変数（[setup.md](setup.md)）。SSO セッションは期限切れになる | プロファイル / SSO / 環境変数（[setup.md](setup.md)） | **インスタンスロール**。ホストに鍵を置かない（[deploy-ec2_ja.md §4](deploy-ec2_ja.md#4-bedrock-へのアクセス-インスタンスロール)） |
| 秘密の置き場 | `~/.aws`（プロファイルと SSO のキャッシュ）。launchd の plist には**絶対に**書かない | `%USERPROFILE%\.aws`。タスクスケジューラの操作には書かない | AWS 用には不要。任意の `/etc/stiracore/stira.env` は `root:root 0600` |
| データディレクトリ | チェックアウト内の `.local/stira` | チェックアウト内の `.local\stira` | `.local/stira`。専用の暗号化ボリュームをそのパスにマウント（シンボリックリンクだとサーバーが止まる） |
| ツール（[§6](#6-os-別ツール互換性)） | macOS 列。`speak` は `say` / `afplay` で再生 | Windows 列: `shell` と `cron` は無い | Linux 列。実行系ツールは非公開のまま |
| パス・エンコーディング・時刻 | POSIX パス。UTF-8 が既定 | バックスラッシュのパス。**既定は cp932**: `PYTHONUTF8=1` と `PYTHONIOENCODING=utf-8` を設定（[§4](#4-windowspowershell)） | POSIX パス。`PYTHONUTF8=1` を設定（ユニットは設定済み）。ゾーンを設定しなければ UTC |
| バックアップ | 停止してから `.local/stira` をコピー（[operations_ja.md §8](operations_ja.md#8-バックアップと復元)）。サーバー稼働中にコピーするバックアップツールはこれに当たらない | Mac と同じ | 停止してから、データボリュームのスナップショットかコピー（[deploy-ec2_ja.md §6](deploy-ec2_ja.md#6-データバックアップ復元)） |
| 復元 | ルールが古いまま戻る: 先に**ロック**する（[operations_ja.md §8](operations_ja.md#8-バックアップと復元)、#273） | 同じ | 同じ |
| 更新 | 停止 → 取得 → `pip install -e` → `smoke_server.py` → `stira_ctl.sh restart` | 同じ。`stira_ctl.ps1 restart` を使う | 同じ。`systemctl start` を使う |
| ここでの検証 | 開発環境。launchd は仕組みのみ（§12） | **未検証**（[§4](#4-windowspowershell)） | ユニット、停止・再起動、サンドボックス、データのマウント、復元時のロック: Linux VM（systemd 255）。**AWS 部分は未検証** |
| 有効にしない（どの列でも） | `STIRA_ALLOW_REMOTE`、`STIRA_ENABLE_EXEC_TOOLS` | 同じ | 同じ |

## 12. 常駐させる：launchd とタスクスケジューラ

パートナー AI 用の短い例です。ログイン時にチャットサーバーを起動し、きれいに止めます。スケジューラの下でインタプリタを
フォアグラウンドで動かします（PID ファイルも `stira_ctl` も使いません）。以下のファイルには秘密を書かないでください。どちらも
自分のアカウントから読めます。

### macOS（launchd の LaunchAgent）

`~/Library/LaunchAgents/com.example.stiracore.plist` として保存します。`/Users/YOU/StiraCore` は自分のチェックアウトの
絶対パスに置き換え（launchd は `~` も変数も展開しません）、venv は §10 のものを使います:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.example.stiracore</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/StiraCore/.venv/bin/python</string>
        <string>/Users/YOU/StiraCore/dashboard/stira_server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/YOU/StiraCore</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUTF8</key>
        <string>1</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <!-- A profile NAME is not a secret; add AWS_PROFILE here if you use one. -->
    </dict>
    <!-- Start at login. No KeepAlive: a crash or a refused start is read from the log, not retried. -->
    <key>RunAtLoad</key>
    <true/>
    <key>Umask</key>
    <integer>63</integer>
    <key>StandardOutPath</key>
    <string>/Users/YOU/StiraCore/.local/stiracore.launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/StiraCore/.local/stiracore.launchd.err.log</string>
</dict>
</plist>
```

```bash
mkdir -p -m 700 /Users/YOU/StiraCore/.local        # launchd はログのディレクトリを作らない
plutil -lint ~/Library/LaunchAgents/com.example.stiracore.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.stiracore.plist   # 起動する（RunAtLoad）
launchctl print gui/$(id -u)/com.example.stiracore | grep -E "state|last exit"
curl --fail http://127.0.0.1:5555/api/gateway/status
launchctl bootout gui/$(id -u)/com.example.stiracore   # 停止: SIGTERM、続いて正常停止の処理
```

次回ログインから起動させないには、ファイルを削除します。ジョブを読み込んでいる間は `stira_ctl.sh start` を実行しないでください
（2 台目のサーバーはデータディレクトリのロックで拒否されます）。launchd のジョブにはシェルの環境が見えません。サーバーに必要な
もの（たとえば `AWS_PROFILE`）は `EnvironmentVariables` に書き、SSO セッションが切れたらログインし直します。

macOS（arm64）での実測: `plutil -lint` は通り、`STIRA_HOST=0.0.0.0` を設定した変種（サーバーは起動を拒否して何も bind しない）で
`launchctl bootstrap` はロード時にジョブを実行し、作業ディレクトリと環境は正しく、拒否メッセージは `StandardErrorPath` のファイルに
出て、`last exit code = 1` になり、`bootout` で消えました。**未実施:** launchd の下での実サーバー（停止の経路そのものは Linux の
systemd で実測）。

### Windows（タスクスケジューラ）— 未検証

以下は Windows では何も実行していません。`stira_ctl.ps1` は UTF-8 の変数を自分では設定しない（§4）ので、小さなラッパー、たとえば
`C:\Users\YOU\bin\start-stira.ps1` から起動します:

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
& "C:\Users\YOU\StiraCore\scripts\stira_ctl.ps1" start
```

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\YOU\bin\start-stira.ps1"'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
Register-ScheduledTask -TaskName "StiraCore" -Action $action -Trigger $trigger `
  -Description "Start the StiraCore chat server at logon"
```

`-ExecutionPolicy Bypass` はそのプロセスだけに効きます。組織のポリシーが禁じているなら上書きせず、2 つの変数を設定して
`.venv311\Scripts\python.exe dashboard\stira_server.py` を実行する `.cmd` ラッパーを使ってください（§4）。停止は
`.\scripts\stira_ctl.ps1 stop`（これは `taskkill /F` で、正常停止の処理は走らず、次の起動で復旧します）。タスクの削除は
`Unregister-ScheduledTask -TaskName "StiraCore" -Confirm:$false`。

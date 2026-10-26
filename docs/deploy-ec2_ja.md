# 常駐ホスト（EC2 など）へのデプロイ指針（参考構成）

[English](deploy-ec2.md) | 日本語

StiraCore は Mac、Windows、EC2 のような Linux サーバーのどれでも動かせます。どれかが本来の置き場所というわけではありません。
**Mac や Windows PC でのローカル稼働が主な経路です**（まず [install_ja.md](install_ja.md)）。このページは、チャットサーバーを専用のホストで
動かし続けたい場合のためのもので、**例として EC2 を取り上げます**。Stira を使うのに必須ではなく、EC2 を推奨環境にするものでもありません。

EC2 インスタンス上でチャットサーバー（`dashboard/stira_server.py`）を動かし続けるための指針です（systemd・データ・バックアップの部分は
どの Linux ホストにも当てはまります）。
**1 インスタンスにつき運用者 1 人・エージェント 1 体**を前提にした手引きで、マネージドサービスではありません。
サーバーには認証も TLS も権限の段階もありません（[admin-api-permissions.md](admin-api-permissions.md)）。
以下の手順は既定値を保ち、何も開けません。権限や入口を増やす手順には、そのことを書いてあります。

同じソフトウェアは Mac や Windows PC でも、個人用のパートナー AI として EC2 と並べて動きます。違い（起動・データ・認証・
バックアップ・更新）は [install_ja.md §11](install_ja.md#11-環境の比較ec2--mac--windows) の表にあります。
設定とデータディレクトリの構成は [configuration_ja.md](configuration_ja.md)、復旧と復元は
[operations_ja.md](operations_ja.md) を参照してください。

## 確認したこと・していないこと

このガイドのために AWS のリソースを作成・操作してはいません。systemd の挙動は、**Mac 上の Linux VM
（systemd 255、Ubuntu 24.04、aarch64、Python 3.12.3）**で、このページを書いた時点のリポジトリを使って実測しました。
AWS 自体が要る事項には **未検証** と書いてあります。AWS のドキュメントや設計から書いたもので、実行していません。
「未検証」を「動く」と読まないでください。まず使い捨てのインスタンスで試してください。全記録は
[検証記録](#検証記録)にあります。

## 1. 既定のままにするもの

常駐ホストでは、開けた入口の一つ一つが重くなります。次はすべて既定のままにします。動くインスタンスにはどれも不要です。

| 設定 | 既定 | EC2 での扱い |
|---|---|---|
| `STIRA_HOST` | `127.0.0.1` | 設定しない。サーバーはループバックだけで待ち受ける。 |
| `STIRA_ALLOW_REMOTE` | 拒否 | 設定しない（[§3](#3-サーバーを開けずに使う)）。 |
| `STIRA_ENABLE_EXEC_TOOLS` | 非公開 | 設定しない。実行系ツール（`shell`、`cron`、`use_agent`、`swarm`、`graph`、`think`、`batch`）は非公開のまま。サンドボックスはまだない（#167）。[§9](#9-実行系ツールacp自律は有効にしない) を参照。 |
| `STIRA_SCHEDULER`、`STIRA_HEARTBEAT` | オフ | 決めない限りオフ。どちらも単独ではモデルを呼ばない。 |
| `distiller_mode` | `"rule"` | そのまま（モデル呼び出しなし）。`"llm"` はトークンを使う。 |
| `harvest_stage_auto` | `true` | そのまま（Harvest が staging に回したものを人間が見る）。 |
| チャットボット・Webhook の秘密（`SLACK_*`、`DISCORD_BOT_TOKEN`、`WEBHOOK_SECRET_*`） | 未設定 | 設定しない。入口か出口が増える。 |

`GET /api/tools/list` は `exec_tools_enabled` を返します（下のサービスで実測: `false`）。

## 2. このガイドで使う構成

| 項目 | 値（好みで置き換える） |
|---|---|
| サービスユーザー | `stira`（システムユーザー、ログインシェルなし、以下すべてを所有） |
| チェックアウトと venv | `/opt/stira/StiraCore`（中に `.venv`） |
| データディレクトリ | `/opt/stira/StiraCore/.local/stira`（**コードで固定**: `<リポジトリ>/.local/stira`。移す環境変数はない） |
| サービスの環境ファイル | `/etc/stiracore/stira.env`（`root:root`、モード `0600`） |
| ポート | `127.0.0.1` の `5555`（`STIRA_PORT` で変更） |

Python は 3.11 以上（[install_ja.md §1](install_ja.md#1-前提条件)）。上の VM では 3.12 でした。Ubuntu の 3.12 と
Debian の 3.11 は条件を満たします。Amazon Linux はそのリリースが提供する Python パッケージを確認してください（**未検証**）。
OS のアップグレードで Python のマイナーバージョンが変わったら `.venv` を作り直します。

```bash
sudo useradd --system --create-home --home-dir /var/lib/stira --shell /usr/sbin/nologin stira
sudo mkdir -p /opt/stira && sudo chown stira:stira /opt/stira
# チェックアウトを /opt/stira/StiraCore に置く（ホストへの持ち込み方は §8）。その後:
sudo -u stira python3 -m venv /opt/stira/StiraCore/.venv
sudo -u stira /opt/stira/StiraCore/.venv/bin/python -m pip install --upgrade pip
sudo -u stira /opt/stira/StiraCore/.venv/bin/python -m pip install -e '/opt/stira/StiraCore[dashboard]'
sudo install -d -o stira -g stira -m 0700 /opt/stira/StiraCore/.local /opt/stira/StiraCore/.local/stira
```

**データの置き場所は最初の起動の前に決めます。** 専用ボリュームに置くなら、今ここでボリュームを準備してマウントし
（[§6「データ専用のボリューム」](#データ専用のボリューム推奨)）、それから §5 のサービスを起動します。稼働中のサーバーが開いている
データディレクトリの上にボリュームをマウントするのは安全ではありません。VM での実測: サーバーが作った 23 個のファイルが見えなくなり、
サーバーは古いファイルシステム上でロックとデータベースを開いたままで、新しい空のディレクトリで起動した 2 台目のサーバーが同時に
動きました（データディレクトリのロックでは排除されなくなる）。既存の環境を移す手順は §6 にあります。

チェックアウトと `.local/stira` は実ディレクトリでなければなりません。**データのパスのどこかにシンボリックリンクがあると
サーバーは止まります**（実測: `.local/stira` を別ディレクトリへのシンボリックリンクにすると、起動時に
`MemorySymlinkError: Memory paths must not contain symbolic links.` とトレースバックを出して終了）。データを専用ボリュームに
置くには、そのパスにマウントします（[§6](#6-データバックアップ復元)）。

## 3. サーバーを開けずに使う

サーバーには **ユーザー認証も TLS もありません**。ポートに届く相手は誰でも、会話を読み、設定を変え、**ツール呼び出しを承認し**、
モデルのトークンを使うチャットを送れます。したがってインスタンスは外からの接続を受けてはならず、公開アドレスで待ち受ける
ものがあってはなりません。

### ネットワークの土台（すべて **未検証**: AWS のリソースは作っていません）

- **セキュリティグループ:** インバウンドルールなし。管理は SSM Session Manager（インスタンスからの外向き HTTPS）で行うので、
  22 番ポートは不要。
- インスタンスに**パブリック IP を付けない**（プライベートサブネット）。少なくともインターネットからの経路を作らない。
  インバウンドなしのセキュリティグループは二重目の防御であり、一重目ではない。
- **IMDSv2 必須**（メタデータの取得ごとにセッショントークンが要り、トークンなしの単純な `GET` ではロールの認証情報が返らない）と
  ホップ制限 1（トークンの応答が追加のネットワークホップを越えないので、独自のネットワーク名前空間を持つ典型的なコンテナからの取得を制限する。
  プロセス単位の認可ではなく、IMDS は link-local アドレス上の平文 HTTP のままです）:
  `aws ec2 modify-instance-metadata-options --instance-id i-0123456789abcdef0 --http-tokens required --http-put-response-hop-limit 1 --http-endpoint enabled`
  （ID はダミー）。ホストでの確認: 認証なしの
  `curl -s -o /dev/null -w '%{http_code}' http://169.254.169.254/latest/meta-data/` が `401` を返すこと。
- **アウトバウンド:** サーバーに必要なのは、リージョンの Bedrock ランタイムのエンドポイントへの HTTPS だけです（VM から実測:
  §5 のユニットのサンドボックスの中で `bedrock-runtime.<region>.amazonaws.com` への DNS と TLS が通る）。出口を絞るには、
  `bedrock-runtime` のインターフェース VPC エンドポイントと、443 だけのセキュリティグループルールを使います。グローバル推論
  プロファイルが VPC エンドポイント経由で使えるかは**未検証**。パッケージのインストール（PyPI）と SSM エージェントには
  それぞれ経路（NAT、または SSM 用の VPC エンドポイント）が要ります。
- **同一ホストのユーザー:** CSRF トークンはループバックのポートに来た任意のクライアントに渡されます（`/api/csrf`）。つまり
  インスタンス上のどのローカルアカウントもこの API を使えます。インスタンスはこの 1 つの目的に使い、信頼できる管理者だけにします。

### 画面への到達方法

方法 2〜4 は案であり、検証済みの手順ではありません。

| 方法 | インバウンドポート | 誰が認証されるか | Stira 側の追加設定 | 状況 |
|---|---|---|---|---|
| **1. SSM ポートフォワード**（既定） | なし | `ssm:StartSession` を許された IAM プリンシパル | なし | **未検証**。Host/Origin の部分は実測（下） |
| 2. SSM 経由の SSH（`ProxyCommand`）+ `ssh -L` | なし | SSH 鍵と IAM | なし | 未検証 |
| 3. インスタンス上の Tailscale（`tailscale serve` で `127.0.0.1:5555` へ） | なし（外向き） | tailnet のメンバーと ACL | tailnet 名を `STIRA_ALLOWED_HOSTS`、その HTTPS オリジンを `STIRA_ALLOWED_ORIGINS` に | 未検証。ホストに到達できる第三者のエージェントが増える |
| 4. 認証と TLS を持つリバースプロキシ | 443 | プロキシ | プロキシが別マシンのときだけ `STIRA_ALLOW_REMOTE=1`。`…_HOSTS` / `…_ORIGINS` も同様 | ここでは推奨しない。下を参照 |

方法 1 の実際（手元の Session Manager プラグイン、インスタンス側の SSM エージェント、インスタンスロールの
`AmazonSSMManagedInstanceCore` が必要。**未検証**）:

```bash
aws ssm start-session --target i-0123456789abcdef0 --region ap-northeast-1 \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["5555"],"localPortNumber":["5555"]}'
# 手元のブラウザで http://127.0.0.1:5555/chat を開く
```

手元の 5555 が使用中（たとえば個人用の Stira が動いている）なら、別のローカルポートへ転送します。下のサービスでの実測:
`Host: 127.0.0.1:8080` や `localhost:8080` のリクエストは受け付けられ（ポートは無視される）、`Origin: http://127.0.0.1:8080`
と CSRF トークンを付けた状態変更リクエストは成功します。**別のホスト名**は拒否されます: `Host: stira.internal.example` は
`STIRA_ALLOWED_HOSTS` に載せるまで `403`（載せると `200`、その `Origin` も一致）。ほかのオリジンやトークンなしも `403`。

**転送したポートを共有しないでください。** 転送は手元マシンのループバックに束縛されます。`0.0.0.0` に束縛したり、別の転送や
画面共有、チームのプロキシでさらに渡したりしないでください。届く相手は、上のすべてについて運用者と同じ権限になります。
セッションを閉じれば入口も閉じます。

### `STIRA_ALLOW_REMOTE=1` を勧めない理由

`STIRA_ALLOW_REMOTE=1` は「認証と TLS がサーバーの**前段**にある」という運用者の宣言で、サーバーはそれを確認できません。
`STIRA_HOST=0.0.0.0 STIRA_ALLOW_REMOTE=1` で実測した結果: `0.0.0.0:5555` で待ち受け、認証・TLS が組み込まれていないという
WARNING を記録し、マシン自身のアドレスから、既定の `Host` のリクエストは拒否されます（`403`）が、**偽装した
`Host: 127.0.0.1` を付けた同じリクエストは受け付けられ（`200`）、CSRF トークンが渡され、それを使った状態変更 POST も
成功しました（`200`）**。Host / Origin / CSRF の検査はブラウザを他のウェブページ（DNS リバインディング、クロスサイト
リクエスト）から守るもので、ログインではありません。したがって EC2 では:

- 「VPC の中だけ」「自分の IP だけ」でも設定しない。認証のない承認エンドポイントとインターネットの間に残るのは
  セキュリティグループだけになる。
- どうしても公開するなら、**転送前に認証し** TLS を終端するリバースプロキシが前提で、`STIRA_ALLOWED_HOSTS` /
  `STIRA_ALLOWED_ORIGINS` に公開名を設定します（[admin-api-permissions.md](admin-api-permissions.md)）。その構成は
  ここでは扱わず、テストもしていません。

ループバック以外の `STIRA_HOST` で起動が拒否されると、原因を示すメッセージを出して終了コード 1 で終わります（実測:
`0.0.0.0` と LAN アドレス。`STIRA_ALLOW_REMOTE=true` も拒否され、ちょうど `1` のときだけ有効）。

## 4. Bedrock へのアクセス: インスタンスロール

**インスタンスロール**（インスタンスプロファイル）を使います。boto3 がロールの認証情報を自分で見つけます
（[setup.md](setup.md)）。サーバーは認証情報なしで起動し、`credentials.kind: "aws_chain"`、`configured: null`
（「SDK がまだ見つけるかもしれない」、実測）を返します。アクセスキーを `/etc/stiracore/stira.env`、ユニット、
チェックアウト、`~/.aws` のどこにも置きません。

1. [bedrock-iam-policy.md](bedrock-iam-policy.md) から呼び出しポリシーを作る（推論プロファイルとその裏のファウンデーション
   モデルへの `bedrock:InvokeModel` と `bedrock:InvokeModelWithResponseStream` だけ。アカウント ID `123456789012` は自分のものに
   置き換える）。`gateway-config.json` / `STIRA_MODEL` のプロファイル ID がポリシーに載っているものと一致することを確認する。
2. EC2 が引き受けられるロールを作り、ポリシーをアタッチする:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}
     ]
   }
   ```

3. 上の方法 1 なら、AWS 管理ポリシー `AmazonSSMManagedInstanceCore` もアタッチする。
4. ロール用のインスタンスプロファイルを作り、インスタンスに関連付ける。

手順 1〜4 は**未検証**です（AWS 管理者の作業で、実行していません）。ホストでは `aws sts get-caller-identity`（`assumed-role/…`
の ARN）と、[bedrock-iam-policy.md](bedrock-iam-policy.md#verify-with-non-admin-credentials) の 1 ターンの確認で確かめます。

セキュリティ上の意味: **インスタンス上のどのプロセスもロールを使えます**（IMDS 経由）。実行系ツールを有効にした場合に
エージェントが動かすものも含みます。これも実行系を切ったままにする理由の一つです（[§9](#9-実行系ツールacp自律は有効にしない)）。
ロールは呼び出し専用にし、便利だからと S3・EC2・IAM の権限を足さないでください（バックアップは外から取ります。
[§6](#6-データバックアップ復元)）。

## 5. サービスとして動かす（systemd）

`scripts/stira_ctl.sh` は端末の前の人のためのものです。`nohup` で起動し、PID ファイルを持ち、ログを `.local/stira.out.log`
に書きます。systemd では、**systemd にサーバーをフォアグラウンドで動かさせ**（`Type=simple`）、`systemctl` / `journalctl` を
使います。ctl スクリプトは、読み取りや CLI のサブコマンドのために、サービスユーザーとして使えます。

ユニットは [`examples/deploy-ec2/stiracore.service`](../examples/deploy-ec2/stiracore.service) にもあります
（テストで 2 つが同一であることを確認しています）:

<!-- unit:begin -->
```ini
# StiraCore chat server as a systemd system service (docs/deploy-ec2.md).
# Copy to /etc/systemd/system/stiracore.service and adjust the paths below:
#   /opt/stira/StiraCore = the checkout (also the data directory's parent)
#   User=stira            = a dedicated, non-login user that owns the checkout's .local/
# Then: sudo systemctl daemon-reload && sudo systemctl enable --now stiracore
# The server binds 127.0.0.1 only (STIRA_HOST is not set). Do not add STIRA_ALLOW_REMOTE here.
[Unit]
Description=StiraCore chat server (stira_server)
Documentation=https://github.com/Resolver-TNG/StiraCore/blob/main/docs/deploy-ec2.md
After=network-online.target
Wants=network-online.target
# If the data directory is a mount point of its own volume, never start without it
# (an unmounted volume would otherwise start on an empty directory of the root disk).
RequiresMountsFor=/opt/stira/StiraCore/.local/stira
# Stop retrying after 5 failed starts in 5 minutes.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=stira
Group=stira
# Configuration files are read relative to the working directory (.local/stira/).
WorkingDirectory=/opt/stira/StiraCore
# Optional; root:root 0600. No AWS key belongs in it when an instance role is attached.
EnvironmentFile=-/etc/stiracore/stira.env
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1
SyslogIdentifier=stiracore
ExecStart=/opt/stira/StiraCore/.venv/bin/python /opt/stira/StiraCore/dashboard/stira_server.py

# SIGTERM runs the server's bounded stops (<= 9 s in total); then exit status 0.
KillSignal=SIGTERM
TimeoutStopSec=20
# Restart after a crash or a kill, not after a clean stop. Exit status 2 (credentials
# missing), 3 (data directory owned by another server) and 4 (port in use) are
# configuration problems: retrying cannot fix them.
Restart=on-failure
RestartSec=5
RestartPreventExitStatus=2 3 4

# Files created by the server are owner-only.
UMask=0077

# Hardening. The only writable path is the checkout's .local/ (data directory, configuration).
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/stira/StiraCore/.local
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
ProtectProc=invisible
ProcSubset=pid
RestrictSUIDSGID=true
RestrictRealtime=true
RestrictNamespaces=true
LockPersonality=true
SystemCallArchitectures=native
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
```
<!-- unit:end -->

インストール:

```bash
sudo install -d -m 0755 /etc/stiracore
sudo install -m 0600 /opt/stira/StiraCore/examples/deploy-ec2/stira.env.example /etc/stiracore/stira.env
sudo cp /opt/stira/StiraCore/examples/deploy-ec2/stiracore.service /etc/systemd/system/
systemd-analyze verify /etc/systemd/system/stiracore.service
sudo systemctl daemon-reload && sudo systemctl enable --now stiracore
curl -s http://127.0.0.1:5555/api/gateway/status      # ブレーカー、モデル、認証情報のヒントなど
journalctl -u stiracore -f
```

### 各部分の目的と実測

- **停止は SIGTERM で、正常停止です（#285）。** `systemctl stop` は即座に終わり、ジャーナルに
  `StiraCore: stopped (approvals: closed)` が出て（スケジューラとハートビートは、オンのときだけこの行に出る）、終了コードは 0、
  `server.lock` はカーネルが解放しました。サーバー自身の停止処理は最大 9 秒なので、`TimeoutStopSec=20` は待ち時間ではなく
  余裕です。承認待ちのターンは拒否され、要求はストアに pending のまま残り、次の起動後に復元として提示されます（#240）。
- **再起動ポリシー。** サーバーを `kill -9` すると、systemd は `RestartSec` 後に再起動しました（`NRestarts=1`、新しい
  プロセスがデータディレクトリのロックを取得）。正常な `systemctl stop` では再起動しません（`on-failure`）。観測した終了コード:
  `3` は 2 台目のサーバー（ここでは手で起動）がディレクトリの所有者を見つけたとき（`Stira: another server (pid N) owns the data
  directory …`）。`4` はポートが使用中のとき（`Stira: port 5555 is already in use …`）。`2` は API プロバイダの鍵の環境変数が
  無いとき（Bedrock では起きない）。この 3 つは `RestartPreventExitStatus` に入れてあるので、ユニットはループせず一度だけ
  `failed` になります。`STIRA_HOST` の拒否は終了コード `1` で、再試行されます: 実測で約 6 秒おきに 5 回再起動し、その後
  `Start request repeated too quickly` でユニットは `failed` のままになります（`StartLimitBurst`）。原因を直してから
  `systemctl reset-failed stiracore`。
- **非 root、書き込めるパスは 1 つ。** `ProtectSystem=strict` では `ReadWritePaths` 以外のファイルシステム全体がサービスから読み取り
  専用です。同じオプションで動かしたプローブでの実測: `.local/` とプライベートな `/tmp` には書けます。リポジトリ直下や `/etc` への
  書き込みは `Read-only file system` で失敗します。`/home` と `0600` の環境ファイルは読めません。Bedrock エンドポイントへの DNS と
  HTTPS は通ります。`systemd-analyze security` はこのユニットを `2.9 OK` と評価しました（このユニットの測定値であって、
  認証ではありません）。プロセスは権限を持たず（`CapEff` 0）、`NoNewPrivs` が立っています。**設定していない**もの（依存パッケージでの動作は未確認）:
  `SystemCallFilter=`、`MemoryDenyWriteExecute=`、`IPAddressAllow=`。スコアをさらに下げたいなら、1 つずつ足してジャーナルを見てください。
- **設定は `WorkingDirectory` からの相対で読まれます。** `WorkingDirectory` はチェックアウトにしてください。別のディレクトリだと
  `gateway-config.json` などを別の場所に探します（`config_location.matches_data_dir: false`、
  [configuration_ja.md](configuration_ja.md#設定の出どころ)）。
- **`EnvironmentFile`** は systemd が（root として）読むので、ファイルは `root:root 0600` のままにでき、サービスユーザーは
  読めません（実測）。`-` は任意の意味で、中身に必須のものはありません。
- **`PYTHONDONTWRITEBYTECODE=1`** は、チェックアウトがサービスから読み取り専用だからです。サーバー自身の
  `Skipping strands_tools.rss …` という行は、任意の extra を入れていなければ正常です
  （[install_ja.md §6](install_ja.md#6-os-別ツール互換性)）。
- **出る警告:** `WARNING: This is a development server. Do not use it in a production deployment.` チャットサーバーは設計上
  Flask 組み込みのサーバーで、ループバック上の運用者 1 人向けです。この構成はまさにそれを満たします。

### systemd と並べて使う `stira_ctl.sh`

**サービスユーザーとして**、チェックアウトから実行します: `sudo -u stira scripts/stira_ctl.sh status`（`stira` でも root でも
動くことを実測）、`staging list|show|approve|reject`、`procedural …`、`distill`。CLI はデータディレクトリに書き込み、
所有者ロックの検査はロックファイルが実行ユーザーのものであることを要求します。実測: サービスユーザーのデータディレクトリに対して
root で `distill --owner-lock` を実行すると拒否されます（`E_LOCK_FILE_UNSAFE`、終了コード 3）。サービス稼働中の
`distill --owner-lock .local/stira` も拒否されます（実測: `E_DATA_DIR_OWNED`）。先にサービスを止めるか、[§8](#8-運用) のタイマー
ユニットを使います。systemd の下で `stira_ctl.sh start|stop|restart` は使わないでください（監督者が 2 つになって衝突します。
`stop` は `lsof` も要り、最小構成のイメージには無く、無いと `stop` は設計どおり拒否します）。`STIRA_PORT` を変えたなら
`status` にも同じ値を設定します。

## 6. データ・バックアップ・復元

サーバーが保持するものはすべて `.local/stira/` にあります（[configuration_ja.md §6](configuration_ja.md#6-データディレクトリリポジトリlocalstira--検査対象)）。
記憶（Markdown）、`neural-state.json`、ツールルール、承認、チェックポイント、`turns.db`、スケジューラのログ、`workspace/` です。
**プライベートな会話データが平文のファイルで**入っています（記憶はプレーンな Markdown、忘却アーカイブには元の全文が入る）。ボリュームも
すべてのスナップショットも、元と同じように扱ってください。

### データ専用のボリューム（推奨）

データディレクトリのパスそのものに EBS ボリュームをマウントすると、インスタンスを入れ替えてもデータが残り、単独で
スナップショットを取れます。シンボリックリンクは使えません（§2）。**最初の `systemctl start` の前に行います**（§2）。次のブロックは
新しいホスト向けです。ルートディスク上のデータですでに動いているホストは、下の「既存のデータをボリュームへ移す」を使います。

```bash
# 初回のみ。ボリュームは /dev/nvme1n1 として接続済み（lsblk で確認）: フォーマットして UUID でマウント
sudo mkfs.ext4 -L stira-data /dev/nvme1n1
sudo blkid /dev/nvme1n1                        # UUID を /etc/fstab に書く:
#   UUID=<uuid>  /opt/stira/StiraCore/.local/stira  ext4  defaults,noatime,nofail  0  2
sudo mount /opt/stira/StiraCore/.local/stira
sudo chown stira:stira /opt/stira/StiraCore/.local/stira && sudo chmod 0700 /opt/stira/StiraCore/.local/stira
```

ループバック接続の ext4 イメージをボリュームの代わりにして実測: マウント直後のファイルシステムのルートは `root:root` で
`lost+found` があり、上のように `chown` / `chmod` するまでそのままです。その後サーバーは起動して書き込めます。`fstab` の行が
あれば、ユニットの **`RequiresMountsFor=` により、ボリュームが無いとき `systemctl start` は `A dependency job … failed` で
失敗します**（実測）。ルートディスクの空のディレクトリで黙って起動することがありません。`fstab` の項目が無ければ、この防御は
待つ対象がありません。ボリュームの種類・サイズ・IOPS・デバイス名はそれぞれ自分で決めます（EBS 上では**未検証**）。

#### 既存のデータをボリュームへ移す

データディレクトリに書き込むものをすべて先に止め（[§8 のメンテナンス手順](#サーバーをわざと止める前に)）、その後で:

```bash
D=/opt/stira/StiraCore/.local        # 絶対パスを使う: .local は 0700 stira で、管理者は cd できない
sudo systemctl stop stiracore                    # §8 の手順の後: タイマー停止済み、実行中の処理なし
sudo mv $D/stira $D/stira.old
sudo install -d -o stira -g stira -m 0700 $D/stira    # マウントポイント
# 前のブロックと同様にボリュームをフォーマットし /etc/fstab に行を足してから:
sudo systemctl daemon-reload && sudo mount $D/stira
sudo chown stira:stira $D/stira && sudo chmod 0700 $D/stira
# コピーと比較の両方が成功したときだけサーバーを起動する:
sudo cp -a $D/stira.old/. $D/stira/ \
  && sudo diff -r -x lost+found $D/stira.old $D/stira \
  && sudo systemctl start stiracore
curl -s http://127.0.0.1:5555/api/gateway/status
```

通常の管理者として VM で実測（`.local` への `cd` は `Permission denied` で拒否され、絶対パスの形は動いた）: ファイルをコピーし、`diff` は同一、
コピーに失敗した場合（元が無い）は `start` の前で止まりました。ループバック接続のイメージでの先の実行では、サーバーはボリューム上で起動して
応答しました（`findmnt` で確認）。実際に使って確認するまで `stira.old` を残し、その後は削除します。ボリュームと同じ非公開データが入っています。

**暗号化:** 暗号化した EBS ボリュームを使います（方針が許せば、アカウントの既定で EBS 暗号化も）。Stira はファイルを暗号化しません。
暗号化ボリュームのスナップショットは暗号化されます。公開共有は絶対にしないでください。

### バックアップ

[operations_ja.md §8](operations_ja.md#8-バックアップと復元) に従います。サーバーを止めて SQLite の WAL を閉じ、`.head`、`-wal`、
`-shm`、`.locked` を含めてすべてコピーします。ボリュームのスナップショットも同じ扱いです（EBS 上では**未検証**）。取得は
インスタンスの外（手元のマシンやライフサイクルポリシー）から行い、インスタンスロールに EC2 や S3 の権限を持たせないようにします。
場所は 2 つあり、この順に行います。

**1. インスタンス上で**（管理者としての SSM またはコンソールのセッション）: [§8 のメンテナンス手順](#サーバーをわざと止める前に)を行い、
サーバーが止まっていることを確認します。

```bash
sudo systemctl stop stira-distill.timer          # 有効にしていなければ何も起きない
until systemctl show -p ActiveState --value stira-distill.service | grep -qE '^(inactive|failed)$'; do sleep 5; done
sudo systemctl stop stiracore
systemctl is-active stiracore                # inactive と表示されること
sudo sync -f /opt/stira/StiraCore/.local/stira && echo synced   # OS のキャッシュを書き出す。"synced" と表示されたときだけ先へ進む
```

プロセスを止めるとファイルは閉じられますが、書いたデータが OS のキャッシュに残っていることがあり、スナップショットに入るのは
ボリュームに届いたものだけです。`sync -f` は、そのパスを含むファイルシステム（そこにマウントしたデータボリューム。データがルートディスク上なら
ルートファイルシステム）を書き出し、失敗すると 0 以外で終了します。VM での実測: 成功は終了コード 0、存在しないパスは終了コード 1。
ディレクトリが `0700 stira` なので `sudo` が要ります。**`synced` と表示されない限り、スナップショットを要求しないでください。**
要求の間ファイルシステムを静止させたいなら、手順 2 の前に `sudo fsfreeze -f <マウントポイント>`、後に `sudo fsfreeze -u <マウントポイント>`
（手順 2 が失敗したときも）を実行します。マウントしたボリュームで動作します（ループバックのマウントで実測）。サーバーが止まっていれば
その間に書き込むものはないので、任意です。これらの手順なしの場合や、サーバー稼働中の場合、スナップショットは良くてクラッシュ整合
（`kill -9` と同じ）にとどまります（次の段落）。

**2. 手元のマシンで**（`ec2:CreateSnapshot` を呼べる IAM プリンシパル。インスタンスロールでは行わない）:

```bash
aws ec2 create-snapshot --volume-id vol-0123456789abcdef0 --description "stira data" \
  --query SnapshotId --output text               # ID はダミー。要求が成功したときだけ snap-… を表示する
```

**3. インスタンスに戻り、手順 2 がスナップショット ID を表示したときだけ。しかも単独のバックアップの場合だけ。**
バックアップが更新・復元・データ移動の最初の部分なら、手順 1 と 2 だけを行います。サーバーとタイマーは、その手順の最後まで止めたままにします。（AWS のドキュメント上、スナップショットは要求時点のボリュームなので、
`pending` の間にサーバーを起動してよい。**未検証**）: `sudo systemctl start stiracore`、タイマーを使っているなら続けて
`sudo systemctl start stira-distill.timer`。手順 2 が失敗したら、原因を直して先に手順 2 をやり直します。整合したコピーがまだ必要なら、
「念のため」サーバーを起動しないでください。

サーバー稼働中に取ったスナップショットはクラッシュ整合にとどまり、`kill -9` と同じ状態です。サーバーはこの場合から復旧する
ように設計されています（承認とログの連鎖が復旧を記録し、最後のトランザクションが欠けることがある。
[operations_ja.md §5〜§7](operations_ja.md#5-再起動をまたぐ承認)）が、そのスナップショットからの復元は**ここではテストしていません**。

### 復元と、#273 の既知の制限

> **既知の制限（#273、未解決）。**
> 整合した古いコピーを復元すると、**古い、より緩いかもしれないツールルール**が戻り、データディレクトリ内の整合性検査はすべて
> 通ります。何もそれを自動では検知しません。データボリュームのスナップショットからの復元は、まさにこの場合です。

順序（[operations_ja.md §8](operations_ja.md#8-バックアップと復元) が正本です）:

1. [§8](#サーバーをわざと止める前に) のとおりタイマーとサービスを止め、手順 3 が終わるまで止めたままにします。その間に保守の実行が
   発火すると、復元したルールをロックする前にサーバーが起動してしまいます。復元したボリュームを、運用中のパスにそのままマウントしないでください。別の場所（たとえば `/mnt/restore` に
   読み取り専用）に接続してそこからコピーし、運用中の承認ストアと操作台帳が上書きされないようにします。
2. ツールルールのファイルを一緒に復元する（`tool-config.json`、`.head`、`.locked`、`settings-history.jsonl`）。承認ストアは
   復元**しない**（`hitl-approvals.db*` と `.head` は退避）。古い `hitl-operations.db` も復元**しない**。記憶と
   `neural-state.json` は必要に応じて。
3. **復元したルールをロックしてから起動する:** `cd /opt/stira/StiraCore && sudo -u stira .venv/bin/stira tool-rules lock`
   （VM で実測: `tool rules locked (restored_backup)`。その後 `stira tool-rules status` は終了コード 1 で `LOCKED` を表示）。
   `/settings#sec-tools` で人が復旧語 `RECOVER` を付けて保存するまで、全ツールが Never です。
4. ボリューム自体を失い、復元したコピーしかないなら、古い操作台帳しかありません。その影響（一時停止中のバッチを再生すると、
   実行済みの呼び出しをもう一度尋ねることがある）は [ADR 0006](adr/0006-hitl-persistence.md#addendum-273-a-ledger-restored-from-a-backup)
   にあります。承認ストアを復元しなければ再生は起きません。

**データボリュームの外のアンカー（任意、既定オフ）。** `STIRA_TOOL_RULES_ANCHOR_DIR` を使うと、ルールの最高世代がデータボリュームの
バックアップに含まれないディレクトリにも保存され、古い・初期化されたデータディレクトリは自動でロックされます
（`anchor_rollback`）。EC2 では `StateDirectory=` が、`ProtectSystem=strict` の下でも書き込めるルートディスク上のディレクトリを
用意します。ドロップイン（`/etc/systemd/system/stiracore.service.d/anchor.conf`）:

```ini
[Service]
StateDirectory=stiracore
StateDirectoryMode=0700
# 次の行は `stira tool-rules init-anchor` でアンカーを作った「後」に足す。下を参照:
Environment=STIRA_TOOL_RULES_ANCHOR_DIR=/var/lib/stiracore/anchor
```

順序が重要です（実測）: 変数を設定したまま、アンカーが無い状態で最初に起動すると、ルールは**ロック**されて読み込まれます
（`anchor_missing`）。したがって:

1. `StateDirectory=` だけのドロップインを作り、**`sudo systemctl daemon-reload`**、サービスを一度起動し（ルールが作られる）、停止する。
2. `sudo -u stira env STIRA_TOOL_RULES_ANCHOR_DIR=/var/lib/stiracore/anchor .venv/bin/stira tool-rules init-anchor`
   （`anchor created at generation 1.`）。
3. `Environment=` の行を足し、もう一度 **`sudo systemctl daemon-reload`** して、サービスを起動する。
4. **効いたことを確認する**（思い込まない）:
   `pid=$(systemctl show -p MainPID --value stiracore); sudo cat "/proc/$pid/environ" | tr '\0' '\n' | grep -Fx STIRA_TOOL_RULES_ANCHOR_DIR=/var/lib/stiracore/anchor`
   が変数を表示し（このファイルは `stira` のプロセスのもので、`sudo` に開かせる必要があります。`sudo tr … < /proc/$pid/environ` は
   通常の管理者では失敗します。実測）、`systemctl show stiracore -p NeedDaemonReload` が `NeedDaemonReload=no` を表示すること。

systemd はドロップインを自分では読み直しません。実測: ユニットが動いている間にドロップインを足し、`daemon-reload` なしで
`systemctl restart` すると、`Warning: The unit file, source configuration file or drop-ins of stiracore.service changed on
disk. Run 'systemctl daemon-reload'` が出て、新しいサーバーには `STIRA_TOOL_RULES_ANCHOR_DIR` も `/var/lib/stiracore` も
ありませんでした（保護が黙ってオフのまま）。`daemon-reload` して再起動すると両方ありました。（同じテストで、停止してから起動する
場合はファイルが読み込まれました。非稼働のユニットはディスクから読み直されるためで、これに頼らないでください。）その後データディレクトリを
消した実測結果: `LOCKED (anchor_rollback)`。アンカーが守るのは**データボリュームの復元だけ**で、ルートディスクも一緒に戻すもの（AMI、
ルートボリュームのスナップショット）では、アンカーも一緒に巻き戻ります。詳細: [operations_ja.md §8](operations_ja.md#8-バックアップと復元)。

### 容量・保持・ローテーション

- 最初の起動直後のデータディレクトリは 336 KB でした（実測）。実利用での増え方は**測っていません**。
  `df -h /opt/stira/StiraCore/.local/stira` で見て、自分のアラームを設定してください。
- `turns.db`、チェックポイント、ログは**既定ではローテーションされません**。`stira memory retention --data-dir .local/stira`
  （`stira` として実行）は読み取り専用のレポートを出します。上限はオプトイン（`memory_retention`。`--apply` なしでは何も消えず、
  `--apply` が消すものは退避される。[memory-maintenance_ja.md](memory-maintenance_ja.md)、#294 は未解決）。
- サーバーのログはジャーナルに出ます（`SyslogIdentifier=stiracore`）。秘密の値はマスクされますが、会話はどこでも完全にマスク
  されるわけではありません（[operations_ja.md §13](operations_ja.md#13-ログと保存先のマスク291)）。`/etc/systemd/journald.conf` の
  `SystemMaxUse=` で上限を設け、再起動をまたいで残すか（`Storage=persistent`。既定はディストリビューションで違うので確認）を決めます。

## 7. 時刻

systemd のタイマーはホストのローカルタイムゾーンを使います。EC2 インスタンスは通常 UTC なので、ゾーンを設定しない限り
`04:30` のタイマーは UTC の 04:30 に動きます（`timedatectl set-timezone Asia/Tokyo`）。スケジューラのジョブは自分の `timezone`
フィールドを持ちます（[scheduler.md](scheduler.md)）。時計の同期を保ってください（Amazon Linux と最近の Ubuntu AMI は
Amazon Time Sync Service を使う chrony を使う: AWS ドキュメント、ここでは**未検証**）。Neural State の減衰も承認の期限も時計を
使います（減衰は経路に依らず、時計が戻ると再同期する。[README](../README_ja.md)）。

## 8. 運用

### サーバーをわざと止める前に

バックアップ、復元、更新、データの移動（§6）はどれも、サーバーを止め、**止めたままにする**必要があります。任意の保守タイマー
（下）を有効にしていると、その間に発火することがあります。そうなると、コピー中や変更中のデータに対してラッパーを実行し、さらに
サーバーを起動し直すので、作業の途中でサーバーが立ち上がりかねません（復元なら、ルールをロックする前に）。そのため、これらの手順はすべて
インスタンス上で次のように始めます:

```bash
sudo systemctl stop stira-distill.timer          # 有効にしていなければ何も起きない
# すでに実行中の処理は終わらせる（"activating" は実行中。is-active --quiet ではそれが検知できない）
until systemctl show -p ActiveState --value stira-distill.service | grep -qE '^(inactive|failed)$'; do sleep 5; done
sudo systemctl stop stiracore
systemctl is-active stiracore                # inactive と表示されること
```

終わりは `sudo systemctl start stiracore`、続けて `sudo systemctl start stira-distill.timer` です（`systemctl is-enabled
stira-distill.timer` が `enabled` のときだけ）。VM での実測: 実行中のユニットの状態は `activating` で、ループはその終了を待ち（3 秒）、
実行は終了時にサーバーを再起動し、続く `stop` でサーバーもタイマーも停止したままになりました。

**更新（停止 → 取得 → テスト → 起動）。** モデルの変更もサーバーの更新も再起動です:

```bash
# 最初に: 上のメンテナンス手順（タイマー停止、実行中の処理なし、サーバー停止）、続けてバックアップ（§6 の手順 1 と 2 だけ。
# 手順 3 は実行しない。サーバーとタイマーはこの更新の最後まで止めたままにする）
sudo -u stira git -C /opt/stira/StiraCore fetch --tags origin
sudo -u stira git -C /opt/stira/StiraCore checkout <タグまたはレビュー済みのコミット>
sudo -u stira /opt/stira/StiraCore/.venv/bin/python -m pip install -e '/opt/stira/StiraCore[dashboard]'
sudo -u stira /opt/stira/StiraCore/.venv/bin/python /opt/stira/StiraCore/scripts/smoke_server.py   # smoke: PASS
sudo systemctl start stiracore
sudo systemctl start stira-distill.timer         # 使っている場合だけ（上を参照）
```

スモークテスト（`stira` として実測、1.0 秒）は、使われていないポートで使い捨てのサーバーを一時ディレクトリに起動して止めます。
データディレクトリには触れず、モデルも呼びません。チェックアウトをホストに載せる方法は各自で決めます。リポジトリは非公開なので、
インスタンスに長期の Git トークンを置くと、それが守るべき秘密になります。読み取り専用のデプロイキーや、コピーして持ち込む
アーカイブ（`git archive <rev> | … | tar -x`、テスト用 VM で使った方法）が代案です（EC2 上では**未検証**）。依存関係はリポジトリで
固定されていません（[install_ja.md §6](install_ja.md#6-os-別ツール互換性)）。うまくいったデプロイの後に `python --version` と
`pip freeze` を記録しておくと、作り直しを揃えられます。

**記憶の整理は任意で、既定ではオフです。** [memory-maintenance_ja.md](memory-maintenance_ja.md) のパターン B（OS のタイマーが CLI を
実行する。ルール蒸留器、モデル呼び出しなし）は、自動化したいなら常駐ホストでの通常の選択肢です。既定では何もインストールされず、
何も動きません（パターン A: `distill` を実行し、`staging` を見るのを好きなときに自分で行う）。ラッパーはデータディレクトリの
ロックを取る必要があるので、サーバーが止まらないホストでは、実行中はサーバーを止める必要があります。
[`examples/deploy-ec2/stira-distill.service`](../examples/deploy-ec2/stira-distill.service) と `.timer` はそれを行い（サーバーを止め、
ラッパーを実行し、失敗したときも含めて再びサーバーを起動する。起動し直すのは**実行の開始時にサーバーが動いていた場合だけ**で、`/run` の目印ファイルが
それを覚えている）、既定では**有効化されません**。VM での実測: `systemd-analyze verify`
は問題なし。サーバー稼働中の実行はサーバーを止め、ラッパーを実行し（終了コード 0、`distill-runs.jsonl` に 1 行）、再び起動した。
不正な設定で失敗した実行でもサーバーは再び起動した。サーバーを止めた状態で始めた実行は、止めたままにした。`STIRA_DISTILL_MAX_CALLS` の既定 `0` が
モデル呼び出しなしを保証します。このユニットはバックアップ・更新・復元を知りません。それらの前に、上のメンテナンス手順を使ってください。

**費用。** 見積もりはここでは示しません。費用の種類は、インスタンス、ボリュームとスナップショット、VPC エンドポイントや NAT、
そして何より Bedrock のトークンで、使い方次第です（実測のレバーは [prompt-cache.md](prompt-cache.md) と
[context-management.md](context-management.md)）。AWS の料金計算ツールに自分の数値を入れてください。ここで実測したサイズ関連の
事実は 1 つだけです: アイドル状態のサーバーは Python 3.12 / aarch64 でオプションの Sokudan 分類器なしの状態で常駐メモリ 136 MB でした
（Sokudan のモデルはディスク上で約 1.2 GB。[install_ja.md §10](install_ja.md#10-macosapple-siliconでチャットサーバーを使う簡易手順)）。
実利用でのメモリは測っていません。

## 9. 実行系ツール・ACP・自律は有効にしない

EC2 では次を、**有効にせず、有効にする予定もありません**:

- **実行系ツール**（`STIRA_ENABLE_EXEC_TOOLS`）。有効にすると、エージェントのシェルはサーバーと同じ OS ユーザーで動きます。そのユーザーとして
  承認 CLI（`staging approve`、`procedural_cli approve`）を実行したりデータディレクトリに書いたりでき、インスタンスロールの
  認証情報も見えます。Stira 自身の承認は、その場合 OS ユーザーの境界以上には強くなりません
  （[tool-execution-paths.md](tool-execution-paths.md)、[ADR 0010](adr/0010-tool-policy-gate-stack.md)）。将来必要になったら、最低限
  ドキュメントにある規則 — エージェントを承認する人とは別の OS ユーザーで動かし、`episodic/` 以外の `memory/` への書込み権限を
  与えない — と、まだ存在しない #167 のサンドボックスが要ります。これは設計判断であり、切り替える設定ではありません。
- **ACP**（クライアント、サーバー、委譲）は `main` に実装されていません（#210〜#212、
  [README のロードマップ](../README_ja.md)）。有効にするものはありません。独自の承認設計ができるまでそのままにします。
- **自律。** `agent-config.json` の `autonomy_boundary` は保存されるだけで**強制されません**
  （[configuration_ja.md](configuration_ja.md)）。スケジューラとハートビートはオフで、単独ではモデルを実行せず、エージェントの
  自己スケジューリング（ADR 0011）は提案段階にすぎません。スケジューラをオンにする場合、ジョブは人が 1 つずつ作り、復旧後は人が
  再開します（[operations_ja.md §6](operations_ja.md#6-スケジューラ)）。
- **権限の追加。** インスタンスロール（§4）は呼び出し専用です。それ以上の権限を与えたり、`write_roots` /
  `allow_broad_write_roots` を広げたりすることは、このガイドの範囲外です。

## 検証記録

2026-10-18 に実施（Ubuntu 24.04 aarch64 の VM、systemd 255、Python 3.12.3、サービスは `stira` ユーザー。コマンドは上の各節）。
AWS 上のものは**実施していません**。

| 主張 | 確認方法 | 結果 |
|---|---|---|
| ユニットの構文が正しい | `systemd-analyze verify` | 出力なし、終了コード 0 |
| 硬化スコア | `systemd-analyze security stiracore.service` | `2.9 OK` |
| AWS 認証情報なしで起動し、ループバックだけで待ち受ける | `ss -ltn`、`/api/gateway/status` | `127.0.0.1:5555`、`credentials.configured: null` |
| `systemctl stop` は SIGTERM による正常停止（#285） | ジャーナルと `systemctl show` | `StiraCore: stopped (approvals: closed)`、終了コード 0、即時 |
| `kill -9` 後の再起動 | `kill -9`、`NRestarts` | `RestartSec` 後に再起動、`NRestarts=1` |
| 同じデータディレクトリの 2 台目のサーバー | 2 つ目の `stira_server.py` | 終了コード 3、所有者の pid を表示 |
| ポート使用中 | 5555 で `python -m http.server` | 終了コード 4、再起動なし |
| `STIRA_ALLOW_REMOTE=1` なしの非ループバックホスト | `STIRA_HOST=0.0.0.0`、`192.0.2.10`、`STIRA_ALLOW_REMOTE=true` | 終了コード 1、`Refusing to bind …`（macOS ホストでも同じ） |
| サンドボックスの効果 | 同じオプションのプローブユニット | `.local` は書ける。リポジトリ直下と `/etc` は読み取り専用。環境ファイルと `/home` は読めない。Bedrock エンドポイントへの DNS+TLS は通る |
| トンネル越しの Host / Origin / CSRF | `Host` / `Origin` を指定した `curl` | [§3](#画面への到達方法) |
| `STIRA_ALLOW_REMOTE=1` はログインではない | `0.0.0.0` で待ち受け、偽装 `Host: 127.0.0.1` | 200、トークン発行、POST 200 |
| データボリュームのマウント。ボリュームが無いと起動しない | ループバック ext4 + `fstab` + `RequiresMountsFor` | 動く。無いとき `A dependency job … failed` |
| スナップショット前のフラッシュ。通常の管理者としての移行と `environ` の確認 | `sync -f`（終了コード 0 / 不正パスで 1）、`fsfreeze -f/-u`、`0700` の `.local` への `cd`、`sudo tr < …/environ` と `sudo cat` | `sync -f` と絶対パスの形は動き、リダイレクトの形は拒否される |
| 稼働中のサーバーの上にボリュームをマウント | 起動してから `mount` | ファイルが見えなくなり、古いファイルシステムが開いたままで 2 台目が動いた: 最初の起動前にマウント。既存データの移動（コピー + `diff`）は動く |
| `daemon-reload` なしのアンカーのドロップイン | 稼働中のユニットにドロップインを足し `restart` | 警告が出て、変数も状態ディレクトリも無い。`daemon-reload` すると両方ある |
| シンボリックリンクのデータディレクトリ | `.local/stira` をシンボリックリンクに | サーバーが停止: `MemorySymlinkError` |
| 復元時のロック | `stira` として `stira tool-rules lock` / `status` | `locked (restored_backup)`、終了コード 1 |
| `StateDirectory=` を使ったルール巻き戻し防止のアンカー | `init-anchor`、その後データディレクトリを消去 | `LOCKED (anchor_rollback)`。アンカー作成前に変数を設定 → `anchor_missing` |
| `stira` としての `smoke_server.py` | 実行 | `smoke: PASS in 1.0s` |
| 任意の distill ユニット | `systemd-analyze verify`、サーバー稼働中・停止中・失敗時の実行、実行中の処理の待機 | 実行開始時に動いていたときだけサーバーを再起動。停止中なら停止のまま。`activating` の状態には `ActiveState` のループが必要 |
| launchd（Mac） | [install_ja.md §12](install_ja.md#12-常駐させるlaunchd-とタスクスケジューラ) | `plutil -lint` OK。拒否する変種を plist から実行 |
| AWS 上のすべて（IMDSv2、SSM、VPC エンドポイント、ロール、EBS、スナップショット、Time Sync） | — | **未検証** |
| macOS / Windows での launchd / タスクスケジューラ下の実サーバー | — | 未実施（macOS は仕組みのみ）。Windows は**未検証** |

## 関連

#285（所有者ロック・SIGTERM）、
#294（保持）、
#273（復元でルールが巻き戻る）、
#317（記憶整理の例）、
#185（Host / Origin / CSRF）、
#167（実行系のサンドボックス）、
#254（プロンプトキャッシュ）、
#107（公開リポジトリ）。このガイドは
[README](../README_ja.md#ドキュメント索引) と[ドキュメント索引](README.md)から辿れます。

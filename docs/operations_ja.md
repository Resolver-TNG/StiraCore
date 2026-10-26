# 運用手順書

[English](operations.md) | 日本語

チャットサーバー（`dashboard/stira_server.py`）の日常の運用と、fail-closed の状態から抜ける手順です。設定・環境変数・データディレクトリの構成は [configuration_ja.md](configuration_ja.md) を参照してください。以下の手順はどれも既定値を変えず、ゲートを弱めません。迷ったときの安全な結果は「人間が決め直すまで何も動かない」です。

## 1. 起動・停止・状態確認

```bash
cd <リポジトリ>                       # 設定はカレントディレクトリ基準で読まれる
scripts/stira_ctl.sh start         # status | stop | restart | distill（Windows: stira_ctl.ps1）
curl -s http://127.0.0.1:5555/api/gateway/status
```

`STIRA_PORT` でポートを変えられます。`start` は使用中のポートを拒否します。**データディレクトリ 1 つにサーバーは 1 台** が強制されます（#285）。サーバーはディレクトリ内の何かを開く前に `.local/stira/server.lock` の OS ロックを取り、同じディレクトリでの 2 台目の `stira_server` は（ポートに関係なく）終了コード 3 と `Stira: another server (pid N) owns the data directory …; not starting` で終了します。クラッシュや強制終了で残ったロックはカーネルが解放するので、次の起動で取れます。`stira_ctl.sh` も同じ所有者に従います。`start` は所有者が生きている間は（PID ファイルが無くても）起動せず、サーバーが起動を拒否した場合は死んだ PID を残さずに理由を表示します。`status` は `STIRA_PORT` で応答が無いときに所有者の PID を示し、`stop` は PID ファイルが無ければ所有者を止めます。

ロックファイルはシンボリックリンクを辿らずに開き（`O_NOFOLLOW`）、通常ファイル・リンク数 1・サーバーを動かすユーザーの所有でなければなりません。違えばサーバーは起動を拒否し（終了コード 3、`… lock file … is unsafe (…)`）、そのファイルには何も書きません。サーバー停止中に削除してください。**Windows には `O_NOFOLLOW` がありません**。Windows ではシンボリックリンクは辿られ、通常ファイル・リンク数 1 の検査だけが効きます。Windows でも起動は許可します（拒否すると Windows ではサーバーを使えなくなるため）。symlink 保護と所有者検査は Windows では未検証で、Windows 実機で検証した後に見直します。`heartbeat.lock` と `scheduler.lock` も同じ実装（`stiracore.owner_lock`、#322）を使います。検査も、拒否したファイルに何も書かないことも、Windows の制限も同じです。これらが拒否されてもサーバーは止まらず、ハートビートまたはスケジューラだけが起動しません（`Heartbeat: ⚠️ heartbeat.lock is unsafe (…); not started`、状態は `last_error: UnsafeLockFileError`）。これらのロックで防げないもの: データディレクトリ自体（ロックファイルの親）を差し替えられる主体は、2 台目のサーバーに別のファイルをロックさせられます。`.local/stira/` を運用者だけが書ける状態に保つのは運用者の責任です（ホストの手引き、#318）。

`stira_ctl.sh` が PID を所有者とみなすのは、この checkout のチャットサーバープロセスで、**かつ** `server.lock` を開いている（`lsof`）場合だけです。PID メモとコマンドラインだけでは古い・再利用された PID の可能性があります。ファイルを開いていることはカーネルロックそのものではありません（起動を拒否されるサーバーも一瞬は開いています）。所有を示せないとき（`lsof` が無い、`server.lock` が無い（#285 より古いサーバーなど）、ロックファイルが symlink か通常ファイルでない）は、`status`/`start` はメモを信用せず、**`stop` は理由を表示して停止を拒否**します。`scripts/stira_ctl.sh stop --force-unverified` は #285 以前の動作（PID ファイルとコマンドラインだけで止める）を明示的に行う別の操作で、`restart` はこれを使いません。

起動と停止は `SIGTERM` に対してすべての命令で原子的ではありません。資源は作った直後（ロック、ストア）または開始する直前（ハートビート、スケジューラ）に停止処理へ登録されます。その間に届いたシグナルは、その資源自身のエラー処理とプロセス終了に任せます。

`stop` は `SIGTERM` を送ります。サーバーはこれを通常の終了に変え、先に有界停止（承認ブローカーを閉じる、スケジューラとハートビートを止める。それぞれ最大 4 秒、合計最大 9 秒、何度呼ばれても 1 回だけ）を実行してから、`.local/stira.out.log` に `StiraCore: stopped (approvals: closed, scheduler: …, heartbeat: …)` を出します。起動途中の `SIGTERM` でも同じで、その時点で存在するものを止め、それがきれいに終われば各ストアとロックも解放します。承認待ちのターンは拒否されますが、要求はストアに保留のまま残り、次の起動後に「復元された承認」として出ます（#240）。10 秒経っても終わらなければ `stop` は従来どおり `SIGKILL` にします（そのときは上の処理は走らず、再起動時の復旧に任せます）。Windows の `stira_ctl.ps1` は `taskkill /F` でプロセスツリーを止めるので、有界停止は走りません（それ以外は未検証。再起動時の復旧は同じ）。

`.local/stira/` の中を移動・削除・復元する前にはサーバーを止めてください。

`/api/gateway/status`（読取り専用）には、Breaker、プロバイダと認証情報の有無（名前のみ）、`heartbeat`、`prompt_cache`、`context_management`、`diagnostics`（doctor の要約、開いている権限カード）が出ます。

## 2. オプトイン機能

すべて既定 OFF で、起動時に読みます（変えたら再起動）。

| 機能 | 有効にする | 確認 | ドキュメント |
|---|---|---|---|
| ハートビート tick（モデル非依存） | `"hb_enabled": true` または `STIRA_HEARTBEAT=1` | `/api/gateway/status` → `heartbeat.running` | [heartbeat-daemon.md](heartbeat-daemon.md) |
| スケジューラ | `STIRA_SCHEDULER=1` | `GET /api/scheduler`、`/schedules` | [scheduler.md](scheduler.md) |
| プロンプトキャッシュ（Bedrock Claude） | `"prompt_cache": true` または `STIRA_PROMPT_CACHE=1` | `/api/gateway/status` → `prompt_cache.active` | [prompt-cache.md](prompt-cache.md) |
| コンテキスト管理 | `"context_management": true` または `STIRA_CONTEXT_MANAGEMENT=1` | `/api/gateway/status` → `context_management.mode` | [context-management.md](context-management.md) |
| 実行系ツール | `STIRA_ENABLE_EXEC_TOOLS=1`（呼び出しは毎回確認） | `/settings#sec-tools` | [ADR 0010](adr/0010-tool-policy-gate-stack.md) |
| Stage ページ（読み取り専用の表示。承認は既存 API 経由） | `STIRA_STAGE=1` | `/stage/`（ナビに「ステージ」） | [stage.md](stage.md) |

`STIRA_HEARTBEAT`、`STIRA_PROMPT_CACHE`、`STIRA_CONTEXT_MANAGEMENT` に `0`（またはそれ以外の空でない値）を入れると、設定ファイルの `true` を OFF にします。

どれも記憶の蒸留・レビュー・忘却はしません。記憶の整理は手動のコマンドで、OS のタイマーから実行することもできます: [memory-maintenance_ja.md](memory-maintenance_ja.md)。

## 3. `stira doctor`

```bash
stira doctor                 # または: python -m stiracore.diagnostics doctor
stira doctor --json
stira doctor --root ~/work --no-write-test
```

`.local/stira/agent-config.json`（または `--config`）の書込み先ごとに、ロックフラグ、SIP、読取り専用マウント、ACL の deny、quarantine、クラウドプロバイダの属性を調べ、ディレクトリ記述子経由で一時ファイルを 1 つ作成・改名・削除します（経路に symlink があれば書込みテストはスキップ）。TCC の場所は警告するだけで読みません。doctor は何も変更しません。権限を直すのは常に人間です。既定の設定（`.local/stira/agent-config.json`）で `workspace_dir` が無いとき、`@workspace` は `.local/stira/workspace/` で、データディレクトリはサーバーと同じく床にあります（#271）: その中の root は `floor` と報告され、書込みテストはしません。終了コード: 0 エラーなし（警告はあり得る）、1 エラーあり、2 設定不正（設定ファイルを読めない、または書込みポリシーを組み立てられない）。感情分類がルール検出器だけで動いている場合（sokudan 未導入: `pip install -e ".[jev]"`）も警告します。設定ページの「診断」セクション（読み取り専用）でレポートを見られます。サーバーは起動時に実行します。チャットのサイドバーか `POST /api/diagnostics/doctor` で再実行できます。詳細: [diagnostics.md](diagnostics.md)。

## 4. ツールルールがロックされたとき

**症状:** 全ツールが Never。設定画面にルールがロックされていると出る（`GET /api/tools/list` → `tools_locked` に理由。例: `head_unreadable`、古い世代、ダイジェスト不一致）。

**原因:** `tool-config.json` と `tool-config.json.head` が食い違っている（手編集、古いコピー、ファイルの削除、読めない履歴）、復元後にルールをロックした（`restored_backup`、§8）、またはオプトインのアンカーが古い／異なる世代を検出した（`anchor_*`、§8）。ロックは固定されます。古いファイルを戻しても、`tool-config.json.locked` を消しても **解除されません**。

**復旧:**

1. ファイルが変わった理由を調べる（誰がデータディレクトリを編集したか）。
2. `/settings#sec-tools` で望むルールにして保存する。画面が復旧語 `RECOVER` の入力を求める。API: `POST /api/tools/config` にルール、`"confirm": true`、`"recover": "RECOVER"` を付ける（空の保存や復旧語無しの保存は 409 `tool_rules_locked` で拒否）。
3. 復旧は「緩める変更」として扱われる: Breaker が NORMAL、確認、Always にする書込み／外部送信ツールの名前の入力、履歴への記録（`recovered_lock`）の成功が必要。

ルールを保存すると、開いている承認はすべて失効します（別の世代で求められたものだから）。詳細: [ADR 0010 §3–§6](adr/0010-tool-policy-gate-stack.md)。

### ツールルールを保存できない: `secret_command_saved`（#270）

保存済みのシェル許可リストに、秘密らしい値を含むコマンドがあります（この検査より前の版が書いたもの）。これは二度と書き込まれません: 削除するまで、ツールルールを書く保存はすべて拒否されます（400 `secret_command_saved`、件数のみで値は出さない）。`/settings#sec-tools` ではその行が伏せ字と印付きで表示されます。「秘密らしいコマンドを削除」（`shell` が OFF でも押せる）→ 保存。他のグループ（感情分類・ブレーカー・書込み先）は保存できます。その秘密はディスクに残っていたので、失効・再発行してください。

## 5. 再起動をまたぐ承認

- 保留中の承認は再起動後も残ります（`hitl-approvals.db`）。`/approvals` で承認すると、止まっていたツールのバッチを再開し、現在のルール・Breaker・床で再確認します。チェックポイントが無い／新しい、束縛の不一致、エージェントが使用中、のいずれかなら再開を拒否し承認を取り消します。
- head の書込みとコミットの間でサーバーが止まった場合（または末尾の行が削除された場合）、次の起動時にストアは **未完了の要求をすべて取り消します**（「人間による新しい判断が必要」）。もう一度依頼してください。勝手に使える状態に戻るものはありません。
- 承認を与える環境変数はありません（`STIRA_YOLO_ALLOW` は YOLO のボタンを出すだけで、承認するのは人間が確認した窓だけ — [yolo-quickstart_ja.md](yolo-quickstart_ja.md)）。

### 承認ストアが使えないとき

**症状:** 承認カードと決定が 503 `store_unavailable` を返し、新しい承認要求はすべて拒否される（fail-closed）。`GET /api/hitl/pending` → `store` にエラーが出る。

**復旧:** サーバーを止め、調査用にファイルを残したうえで、**4 つすべて** — `hitl-approvals.db`、`hitl-approvals.db-wal`、`hitl-approvals.db-shm`、`hitl-approvals.db.head` — を一緒に退避して起動し直す。新しいストアは空で、保留中の承認はすべて無くなり、もう一度依頼が必要です。head だけを消したり編集したりしないでください（ストアが unhealthy のままになる）。`hitl-operations.db`（操作台帳）はそのまま残します。調査のために Python でストアを開くと（`ApprovalStore(path).verify_chain()`）、それ自体が復旧を記録しうるので、コピーに対して行ってください。詳細: [ADR 0006](adr/0006-hitl-persistence.md)。

## 6. スケジューラ

- ジョブは、人間が API で `"confirm": true` を付けて作ったものだけです。読取り専用のツールしか使えず、チャットサーバーに登録されているアクションは `health_check` と `memory_harvest` です（`memory_harvest` は rule 蒸留器だけでモデルを使わず、出力は staging だけです。[memory-maintenance_ja](memory-maintenance_ja.md) パターン C）。
- **復旧の後**（head と追記の間でサーバーが止まった、または最後の行が削除された）は、全ジョブが `system` により一時停止されます。`/schedules` でジョブを確認し、必要なものを再開してください（`POST /api/scheduler/jobs/<id>/resume` に `"confirm": true`）。WARNING 以上では再開は拒否されます。
- **ログが使えない**（`GET /api/scheduler` → `status.error: log_unavailable`。ログの検証に失敗）: 何も動かず、変更はすべて 503。サーバーを止め、調査用にファイルを残したうえで、`scheduler-events.jsonl` **と** `scheduler-events.jsonl.head` を一緒に退避して起動し直すと、ジョブの無い状態で起動します。片方だけ動かすと使えないままです。

詳細: [ADR 0008](adr/0008-scheduler.md)。

## 7. EventStore（`events.db`）

ハッシュ連鎖の観測ストアは権限を何も生みません。連鎖が壊れても記録が止まるだけです（ターンは動き続ける）。`/api/gateway/status` には出ないので、コピーに対して `SQLiteEventStore(path).verify_chain()` / `chain_status()` で確認します。新しい連鎖から始めるには、サーバーを止めて `events.db`、`events.db-wal`、`events.db-shm`、`events.db.head` を一緒に退避します。クラッシュ後の復旧イベント（`chain.recovered`）は想定どおりで、最後のトランザクションが欠けている可能性を記録したものです。詳細: [ADR 0009](adr/0009-eventstore-chain.md)。

**ターンのイベントの置き場。** チャットサーバーでは `turns.db`（`TurnEventRecorder` が書く）がターンのイベント（ターン・ツール呼び出し・承認・ブレーカー遷移）の唯一の正本です。`/chat`、`/api/turns`、`/api/turns/<id>/events`、Stage、`/observability`（`/api/observability/events` と `/api/observability/snapshot`）はすべてこれを読むので、件数は一致します。ハッシュ連鎖の `events.db` はそのコピーではなく、チャットサーバーが書くのは監査記録（`/new`、#350）だけです。汎用の `create_sse_blueprint` は連鎖ストアを書くアプリ用で、今はどのアプリも使っていません（チャットサーバーは `turns.db` を読み、観測ダッシュボードはストリームを配信しません）。チャットサーバーの `/observability` はターンごとのトークンとモデルを表示します。モデルの料金は設定されていないので、費用は無料ではなく未算出（`*` 付き）と表示されます（#286）。観測ダッシュボード（`dashboard/app.py`）はイベントを配信しないので、その `/observability` は接続せずにその旨を表示します（#278）。

## 8. バックアップと復元

**バックアップ:** サーバーを止め（SQLite の WAL が開いていない状態にする）、`.local/stira/` ディレクトリ全体を、すべての `.head`、`-wal`、`-shm`、`.locked` を含めてコピーします。別のディレクトリから起動している場合は設定ファイルも。コピーには会話の私的なデータ（チェックポイント、ターン、記憶、退避領域）が入っているので、元と同じように保管してください。

**復元は権限の巻戻しです。** 同じ時点のファイル・head・履歴をまとめて戻した古いコピーは、データディレクトリ内の完全性検査をすべて通ります。そのため古い（より緩いかもしれない）ツールルールが黙って戻り、データディレクトリ内に置いた目印も一緒に戻ります（#273）。そこで復元手順では、何かが動く前に人間がルールを承認し直します。次の手順で復元してください:

1. サーバーを止める。
2. `tool-config.json`、`tool-config.json.head`、`tool-config.json.locked`（あれば）、`settings-history.jsonl` は全部一緒に戻すか、どれも戻さない。
3. 承認ストアは **戻さない**: 現在の `hitl-approvals.db*` と `.head` を §5 のとおり退避し、保留中・承認済みのものが戻らないようにする。
4. `hitl-operations.db` は **巻き戻さない**: 現在の台帳を残す。実測（[ADR 0006](adr/0006-hitl-persistence.md#addendum-273-a-ledger-restored-from-a-backup)）: 古い台帳では、止まっていたバッチを再開すると実行済みの呼び出しについてもう一度承認を求め、それを承認すると副作用が 2 回目に実行される。
5. 記憶（`memory/`）、`neural-state.json` などは必要に応じて戻す。`events.db` と `scheduler-events.jsonl` はそれぞれの `.head` と一緒にだけ戻す（スケジューラは戻した後に全ジョブを確認する）。
6. **戻したルールをロックする**（サーバーを起動するディレクトリで）:

   ```bash
   stira tool-rules lock          # または: python -m stiracore.diagnostics tool-rules lock
   stira tool-rules status        # 終了コード 1 = ロック中（ここでは想定どおり）。--file <tool-config.json>
   ```

   `tool-config.json.locked`（`restored_backup`）を書きます。人間が復旧語を入力してルールを保存するまで（§4）全ツールが Never になります。ルールの値は変えません。
7. サーバーを起動し、最初のターンの前に `/settings#sec-tools` で望むルールにして `RECOVER` で保存する（§4）。

**任意: データディレクトリ外のアンカー。** サービスの環境変数に `STIRA_TOOL_RULES_ANCHOR_DIR=<ディレクトリ>` を設定すると（データディレクトリ内のファイルではなく）、ルールの最大世代とダイジェストをそこにも保ち、手順 6 を忘れても次の場合は自動でロックします:

| 理由 | 条件 |
|---|---|
| `anchor_rollback` | ルールがアンカーより古い世代、またはデータディレクトリを作り直した |
| `anchor_mismatch` | 同じ世代で中身が違う |
| `anchor_foreign_install` | ルールと head を別のインストール（別のパス、または `init-anchor` より前）が書いた: インストール ID がアンカーと違う（世代に関係なく） |
| `anchor_generation_jump` | ルールがアンカーより 2 世代以上新しい |
| `anchor_unconfirmed_generation` | 1 世代だけ新しいが、アンカーが記録したルールより緩い（またはアンカーにルールの記録かインストール ID が無い） |
| `anchor_missing` | 変数は設定済みだがアンカーが無い（未作成、または削除された） |
| `anchor_unsafe` | アンカーが symlink、2 つ目のハードリンクがある、所有者が違う |
| `anchor_unreadable` | アンカーの内容が不正 |
| `anchor_misconfigured` | ディレクトリがデータディレクトリの中にある（symlink 経由も含む） |
| `anchor_unwritable` | 必要なアンカー更新に失敗した |

アンカーの更新に失敗した保存（500 `persist_failed`）はルールを 1 世代先に残します。次のロードは、それが何も緩めない場合だけ（すべての保存と同じ緩和判定）完了させ、緩める保存は `RECOVER` までロックしたままにします。各アンカーの隣にはロックファイル（`<アンカー>.lock`）があり、アンカーを変えるプロセスは必ずこれを取るので、並行して書いてもアンカーは後退しません。5 秒以内に取れない場合、保存は失敗し、アンカーを引き上げる必要のあるロード（1 世代先）はロックします（`anchor_unwritable`）。ルールがアンカーと一致するロードはロックファイルを取りません。ロックの下で読み直したアンカーが無い／使えない場合、ロードと通常の保存はアンカーを書き直さず、ロックします（`anchor_missing` / `anchor_unreadable` / `anchor_unsafe`）。書けるのは `init-anchor` と `RECOVER` の保存だけです。

ロードがアンカーを **作ることはありません**。有効にする手順（人間が 1 回だけ）:

```bash
# サーバー停止中。ルールが存在し、ロックされていないこと（新規インストールでは一度変数なしで起動する）
export STIRA_TOOL_RULES_ANCHOR_DIR="$HOME/.config/stira/anchor"   # データディレクトリの外
stira tool-rules init-anchor [--data-dir <データディレクトリ>]   # 中を指すと拒否
stira tool-rules status                                          # anchor generation: N
```

その後、サーバーの環境にも同じ変数を設定して起動します。`init-anchor` は既存のアンカー、ロック中のルール、データディレクトリ内のディレクトリを拒否します。アンカーが無くなった後（または `init-anchor` の前に変数を設定した場合）はルールがロックされ、アンカーを書き直せるのは **`RECOVER` での保存だけ**です（§4）。アンカーを書けない保存は失敗し（500 `persist_failed`。ディレクトリに書けない場合は何も置き換えない）、ロックは残ります。データディレクトリのバックアップに含まれず、サーバーの OS ユーザーが書けるディレクトリを選んでください。アンカーを同じバックアップから戻すと保護は無くなります。守るのは復元であって、アンカーも書ける人に対してではありません。既定は OFF です。

## 9. してはいけないこと

- ロックを「直す」ために `tool-config.json`、`.head`、データベースを手で編集しない — ロックが固定されるか、ストアが unhealthy になる。
- 前段に認証を置かずに `STIRA_ALLOW_REMOTE=1` にしない。
- データディレクトリへの書込みをエージェントに与えようとしない: #271 以降、`workspace/`、`memory/agents/stira/memory/episodic/`、`context-stash/` を除いて書込みの床にあり、書込み root や承認でも変わりません（設計どおり）。

## 10. #271 以降への更新（エージェントの作業場所の移動）

#271 より前は、既定の `@workspace` が `.local/stira/` そのものでした。現在は `.local/stira/workspace/`（起動時に作成）で、データディレクトリの残りは `file_write` / `editor` から読み取り専用です。何も移動・削除しません: エージェントがストアの隣に書いたファイルはその場に残り、読めますが、エージェントはもう変更できません。また `notes/x.md` のような相対パスは `workspace/notes/x.md` に書かれます。続けて使うには、サーバーを止めてから *自分の* ファイル（[configuration_ja.md](configuration_ja.md#6-データディレクトリリポジトリlocalstira--検査対象) の表にあるストア・ログ・`.head`/`-wal`/`-shm`・`checkpoints/` ではないもの）を `.local/stira/workspace/` へ移してください。あるいは `workspace_dir` をデータディレクトリの外のフォルダにします（このとき episodic 層と context-stash はファイルツールの書込み root になりません。`memory_write` は引き続き episodic 記憶を書きます）。

**起動時に「@workspace is disabled」と出たとき。** `.local/stira/workspace/` が実ディレクトリではありません（symlink、ファイル、または作成できなかった）。サーバーはそれをたどらず、作業領域なしで起動します（`@workspace` は書込み root から外れ、相対パスは拒否。episodic 層と退避先は同じ検査に通れば書ける）。サーバーを止めてからリンクやファイルを取り除き、サーバーにディレクトリを作らせるか、`.local/stira/agent-config.json` の `workspace_dir` で作業場所を明示してください（設定ハブか手編集。このファイルは床にあるのでエージェントは変更できません）。明示した `workspace_dir` は人間が選んだ場所として扱いますが、データディレクトリの中にある場合は床が優先され、そこには何も書けません。

## 11. #312 以降への更新（ファイルツールはデータディレクトリを読まない）

データディレクトリは **読取り** の床にも入りました（[ADR 0010 の追記 #312](adr/0010-tool-policy-gate-stack.md#addendum-312-the-read-floor)）。`.local/stira/` の中で `file_read`、`view_image`、`image_reader`、`editor` の `view`、`lsp_*` が読めるのは `workspace/`、`memory/agents/<agent>/memory/episodic/`、`context-stash/` だけです。それ以外 — ツールルール、承認、チェックポイント、`neural-state.json`、設定ファイル、semantic / procedural 層、ログとストア、#271 より前にエージェントがその隣に書いたファイルも — は承認の有無にかかわらず `floor` で拒否され、即時に有効です（警告だけの移行期間はありません）。データディレクトリの外のファイルは従来どおり読めます。

実際に変わること: データディレクトリ内のファイルをパスで読むようエージェントに指示していたプロンプト・スキル・スケジュール（例: `../neural-state.json`、ストアの隣に置いた自分のメモ）は `floor` の拒否を受けます。自分のファイルは（§10 と同じくサーバーを止めて）`.local/stira/workspace/` へ移すか、`workspace_dir` をデータディレクトリの外のフォルダに向けてください。記憶はメモリツール（`memory_read`、`memory_search`）で、退避したツール結果は `retrieve_offloaded_content` で引き続き使えます（どちらもファイルツールを通りません）。設定ページ（`/settings#sec-tools` の床）には、書ける場所の隣に読める場所を表示します。

## 12. 忘却は退避付き、取り消しは明示の経路だけ（#294）

`distill`（および `python -m stiracore.control.distillation_cli`）は、既定では Semantic の忘却をプレビューするだけです。候補ごとに `forgetting.items` にキー、現在と次の段階、`last_accessed`、経過日数、文字数、選ばれた理由を表示し、何も変更しません。`forgetting.selection` に選定の根拠を書きます: **`last_accessed` だけ** です。Semantic 記憶はプロンプトに入らないので、`last_accessed` が更新されるのはメモリツール（`memory_read`、`memory_search`）か Harvest の重複検索が触れたときだけです。つまり「有用だが検索されていない」記憶から圧縮されます。適用する前にプレビューを確認してください。

経過日数はエントリの半減期（`retention_half_life_days`、既定 30 日）で段階の閾値に換算し、`retention: invariant` の記憶は選ばずに `forgetting.protected` に出します。タグの無い記憶は今までと同じ日数で選ばれます。タグを付けるのは人間の `stira memory tag --apply` だけです（#173、[memory-maintenance_ja.md](memory-maintenance_ja.md#重要度タグretention-invariant--derivedと半減期173)）。

`--apply-forgetting` は表示された記憶を圧縮します（先頭と末尾を残す）。上書きの前に、元の Markdown 全文（frontmatter を含む）を `memory/agents/<agent>/memory/forgotten/<id>.json` に、キー・出所（`source_id`）・段階・時刻・元と圧縮後の SHA-256 とともに書き、読み戻して確かめます。**退避できなければ忘却しません:** このディレクトリが実ディレクトリでない、監査ログが壊れている、記録を書いて無傷で読み戻せない、のいずれかなら、その記憶（とそれ以降の全部）はそのまま残り、コマンドは 1 で終わります。圧縮後の記憶には `forgetting_archive_id` が付きます。

元に戻す経路は明示の操作だけです:

```bash
.venv/bin/python -m stiracore.control.distillation_cli \
  --base-dir .local/stira/memory --list-forgotten            # id・キー・ハッシュ（本文なし）
.venv/bin/python -m stiracore.control.distillation_cli \
  --base-dir .local/stira/memory --undo-forgetting <id>
```

取り消しは元の内容をバイト単位で戻します。ただし記憶がその段階で書いた内容のままのときだけです。その後に編集された記憶や、後の段階でさらに圧縮された記憶は拒否します（後の段階を先に取り消す）。戻した記憶は「今読まれた」扱いになるので、次のプレビューですぐ再び選ばれることはありません。取り消しが成功すると記録は削除されます。Harvest の印はそのままです: 元の episode は harvest 済みのまま、戻した記憶は元の `source_id` / `harvest_version` を持つので、二重に蒸留されません。

Harvest の印はメタデータで、Episodic の並びを変えません（#292）: episode は更新時刻を保ったまま書き直されます（開いている一時ファイルに時刻を付けてから fsync し、アトミックに置き換える）。`distill` を何度実行しても、staging の承認・却下や取り消しを挟んでも、`list_entries` とプロンプトの Episodic 欄には最新のターンが並びます。並びは作成順ではなく更新時刻の順です: Harvest の印以外の方法で episode を書き直すと、その episode は先頭に移ります。古い版で印を付けた episode はその実行時刻のままですが、新しいターンが来ればそれが先頭に並びます。制約: `distill` が書き直している間の episode の変更は検出しないので、`distill` はサーバーを止めて実行してください（データディレクトリごとに書き手は1つ、§1 / #285）。書き込み中に強制終了すると `episodic/` に `.memory-*.tmp` が残ることがあります。これは記憶ではなく（一覧・検索・索引・Harvest の対象外）、自動では消えません。サーバーを止めて削除してください。

各操作は `forgotten/forgetting-log.jsonl`（ハッシュ連鎖の JSONL）に記録されます。中身は id・キー・段階・ハッシュだけで本文は含みません（`forget.started` / `forget.applied` / `forget.refused`、`undo.started` / `undo.applied` / `undo.refused`）。証跡専用で、連鎖が壊れていれば以後の忘却と取り消しを拒否します。変更の *後* の 1 行を書けなかった場合も、変更は「済み」と報告します（`applied` と `forgetting.audit_incomplete`・項目の `audit_error`、または `"restored": true` と `audit_error`）。コマンドは 1 で終わり、記録は残します。次の `--apply-forgetting` か `--undo-forgetting` の実行が最初にログを補い（`forget.recovered` / `undo.recovered`、`reconciled` に表示）、不要になった記録を消します（`forget.abandoned`: 記憶は上書きされていなかった）。

退避領域はファイルツールの読取り・書込みの床にあり（データディレクトリの中で、`workspace/`・`episodic/`・`context-stash/` の外）、記憶の層ではないので、メモリツール・Harvest・Semantic の索引からは見えません。所有者だけが使える権限で作られます（POSIX で `0700` / `0600`。Windows ではデータディレクトリの ACL に従う）。記録とログはリンクをたどらずに開き（ディレクトリ基準、`O_NOFOLLOW`）、あなたが所有しグループ・その他の権限のない通常ファイルでなければ忘却と取り消しを拒否します。記録の作成・削除の後はディレクトリも fsync します。記録が戻せるのは同じエージェントの Semantic 記憶だけです: `..`、絶対パス、バックスラッシュ、`semantic/` 内のリンクを含むキーは拒否します。Windows（`O_NOFOLLOW` なし）では開く前の検査でリンクを拒否します（ベストエフォート）。

**記憶を完全に消す: `stira memory forget`（#294）。** 退避記録は元の全文を持つので（秘密も含みうる）、忘却は削除ではありません。削除要求は、その文を全コピー — エントリ本体、そこから派生したもの、`staging/`、検索索引、`semantic/index.md`、却下台帳、チェックポイント、`context-stash/`、`turns.db`、**そしてこの退避記録** — から探し、`--apply` で全部を取り消し不能で消します（[memory-maintenance_ja.md](memory-maintenance_ja.md#保持期限削除要求出所タグ294)）。バックアップとハッシュ連鎖のログは編集しません（一覧に出します）。監査ログに残るのはハッシュとキーだけです。

**既知の残存リスク（修正しない）。**
- 退避先には原文が平文で残ります。新しい種類の露出ではなく（記憶本体も平文の Markdown）、同じ方法で守られています: `0700` / `0600` とファイルツールの床。保存時の暗号化は将来の課題です。
- ハッシュの確認から書込みまでの間に別プロセスが同じ記憶を編集することは排除していません。単一の書き手の保証はデータディレクトリの所有者ロック（#285）に依存します。サーバーや別のコマンドが書いているデータディレクトリに対して `--apply-forgetting` / `--undo-forgetting` を実行しないでください。
- 監査ログには head アンカーも鍵もありません。書ける人は書き換えて連鎖を計算し直せ（サイズが変わらない改変を含む）、最後の 1 行の削除も検出できません。事故や途中で止まった実行は分かりますが、意図的なローカルの攻撃者に対する証拠にはなりません。
- 記憶ストア自身のパス検査（`MemoryStore.read_text` / `write_text` など記憶のパス）と open は別の操作です。検査の後に末端をリンクに差し替えられる場合や、祖先ディレクトリにリンクを置かれる場合は、そこでは排除していません。どちらもデータディレクトリへの書込み権限が要ります。その権限の境界を保つのは運用者の責任です（データディレクトリごとに所有者は 1 つ、#285。ホストの手引き、#318）。ディレクトリ記述子基準のリンク非追従 open を使うのは退避領域とそのログだけで、記憶の全経路への拡大は後続の課題です。#291 以降、エントリの読み出し（`read`・`list_entries`・`search`）は末端のリンクを追従せずに開き、開いたファイルとパスを照合し直します。差し替えられた名前は一覧では飛ばし、読み出しでは拒否します。
- 書込みが途中で止まって（ディスク満杯など）監査ログに部分行が残ると、以後の apply/undo はすべて拒否されます（fail-closed）。補完の処理では直せません。空きを作り、ログを確認し、記録を確認したうえでログと記録を一緒に脇へ移してください。

## 13. ログと保存先のマスク（#291）

秘密のマスク（`observability/masking.py`）がかかるもの: stderr のサーバーログ（`.local/stira.out.log`。メッセージ・引数・トレースバック。`stira_server` の `main()` がログレコードを作成時にマスクするので、`logging.basicConfig` やライブラリが起動前後に付けたハンドラにもマスク済みの文字列が渡る）、ターンイベント（SSE の `ui_event`、`/api/turns`、`turns.db`）、ツール入出力のページ、`events.db`、承認ストア、エピソード記憶（Slack/Discord ボットのものも）、チャット履歴（`/api/chat/history` と外部の `memory_store` に渡すターン。メッセージと応答を、ターンを保持する時と返す時の両方でマスク）。チャットのクライアントには例外の本文を返さず（固定のエラーメッセージ）、Slack/Discord ボットは例外の型だけをログに出します。起動バナーはパスを出しません。

**今はマスクしないもの**（設計判断、#291）: チャットに流す応答（SSE の `delta`。このため再読み込みの後は応答の表示が変わることがある）、Slack/Discord への返信、チェックポイント（`checkpoints/*.json` は一時停止したターンを再実行できるよう、モデルが見た会話をそのまま持つ）。`tests/test_output_masking.py` が出力先を全部並べ、マスクしないものは strict な expected failure になっています。`.local/stira/` は機密として扱ってください。

記憶エントリの読み出しの既知の制約（#291、#330 のレビューで受け入れ済み）:

- `MemoryStore._open_entry` は末端を `O_NOFOLLOW` で開き、その後にパスを照合し直します（どの階層にもリンクが無いこと、開いたファイルと同じ inode であること）。ディレクトリを記述子（`dir_fd`）基準でたどる開き方はしていないため、open の前後で親ディレクトリをリンクに差し替えて元に戻す競合は排除しません。これにはデータディレクトリへの書込み権限が要るので、上と同じ運用の境界（データディレクトリごとに所有者は 1 つ、#285。ホストの手引き、#318）に依存します。
- `O_NOFOLLOW` が無い環境（Windows）ではフラグを 0 にします。そこで効くのは open 後の照合だけです。

## 14. 画面のポーリングとアクセスログ（#369）

画面は読み取り専用のいくつかのエンドポイント（`/api/turns`、`/api/hitl/pending`、`/api/gateway/status`、`/api/scheduler` など）を一定間隔で読みます。読み方は `static/js/poll.js` に集約しています。非表示のタブは読みに行かず（表示に戻ったときに 1 回だけ読む）、サーバーが失敗している間は間隔を倍々に伸ばし（最大 30 秒）、変化が無い間は段階的に間隔を伸ばし（Stage は 1 秒→10 秒、承認キューは 1 秒→5 秒、状態は 5 秒→15 秒）、何かが変わったとき・ターンが走っている間・承認が待っている間・このページ自身のチャットストリームに承認要求が届いたときは、すぐ短い間隔に戻ります。そのため、別のタブやチャネルで出た承認要求は 1〜2 秒ではなく、アイドル時の最大間隔（5 秒）以内に見えます。偽モデルの新しいサーバーでの実測（1 タブ、アイドル）は #369 の Pull Request にあります。

アクセスログ（`.local/stira.out.log`）には、これらのポーリングが成功した（`200`/`304`）`GET` の行を出さなくなりました。失敗したポーリングとそれ以外のリクエストは今までどおり出ます。デバッグ用に `STIRA_ACCESS_LOG_POLLING=1` で戻せます（[configuration_ja.md](configuration_ja.md)）。ルートも権限も増えていません。

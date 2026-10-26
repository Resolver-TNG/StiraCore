# ガイド: スケジューラーとハートビート — 何をオンにし、どう確かめ、どう止めるか

[English](guide-scheduler-heartbeat.md) | 日本語

チャットサーバーには任意で使える裏方の部品が 2 つあります。どちらも**既定はオフ**です。

* **ハートビート** — タイマーで動く、モデルを使わない見回り（減衰・状態の記録・ブレーカーの
  照会）。詳しくは [heartbeat-daemon.md](heartbeat-daemon.md)。
* **スケジューラー** — 人が作ったジョブを動かします。詳しくは [scheduler.md](scheduler.md)。

このガイドは、手順の順番、それぞれのコスト、動いていることの確かめ方、止め方をまとめたものです。
下のコマンドと出力はすべて 2026-10-26 にスクラッチのインスタンスで実行しました（macOS arm64、
専用の `STIRA_DATA_DIR`、空の `HOME`、AWS の認証情報なし。したがってモデルは一度も呼んでいません）。
出力は省略して載せています。ポートは既定の `5555` で書いています。自分のポートに読み替えてください。

## 1. どれが必要か

| 構成 | 裏で動くもの | トークン | 触れるもの | スイッチ | 既定 |
|---|---|---|---|---|---|
| **何もなし** | なし。減衰は次の会話でまとめて計算されます | 0 | — | — | **これ** |
| **ハートビートだけ** | 間隔（下記）ごとに見回りを 1 回 | **0**（モデルを呼びません） | `neural-state.json` を保存し、メモリ上の状態履歴を持ちます。記憶・承認・ネットワークには触れません | `gateway-config.json` の `"hb_enabled": true`（設定画面のセクション「ハートビート」）または `STIRA_HEARTBEAT=1` | オフ |
| **スケジューラー**（運用者のジョブ） | 人が作ったジョブ: `health_check`、`memory_harvest`（rule 蒸留器・staging だけ） | この 2 つのアクションは **0** | `health_check`: ハートビートとブレーカーの状態を読むだけ。`memory_harvest`: `semantic/staging/` だけに書きます（[記憶のガイド](guide-memory-routines_ja.md)） | `STIRA_SCHEDULER=1`（環境変数だけ。設定ファイルの項目はありません） | オフ |
| **スケジューラー + ハートビート** | 上記に加えて、モデルを呼ぶジョブが開始できるようになり（正常なハートビートが必要）、グラントのハートビート開始も可能になります（[autonomy_ja.md](autonomy_ja.md)） | ハートビートは 0。モデルを呼ぶジョブは上限の範囲でトークンを使います | モデルを呼ぶジョブ: 記憶の計画ファイルだけ、またはエージェント層の読み取り系ツールだけ | 上の 2 つと、各機能そのもののスイッチ | オフ |

モデルを呼ぶジョブは、さらに別のスイッチがあるときだけ存在します。記憶の種類
`memory_rate_backfill` / `memory_consolidate` には `agent-config.json` の `memory_autonomy` が
必要です（[memory-autonomy_ja.md](memory-autonomy_ja.md)）。エージェント層のジョブ（運用者の
プロンプト）には `gateway-config.json` の `agent_jobs.enabled` が必要です（設定画面
「定期実行のエージェントジョブ」。プール `agent_daily_token_budget` は既定で UTC の 1 日あたり
50,000 トークン。[configuration_ja.md](configuration_ja.md) のセクション `agent_jobs`）。
このガイドではどちらもオンにしません。

**ハートビートの間隔。** `間隔 = 最長 × stability²` を、最短の間隔と最長の間隔の間に収めます
（`hb_interval_floor_sec` 既定 300 秒、`hb_interval_ceil_sec` 既定 10800 秒。設定画面で受け付ける
のは 1 分〜24 時間）。基準値の stability 0.7 では 10800 × 0.49 = 5292 秒、約 88 分です（状態の表示は
`"interval_sec": 5291.99…` でした）。最初の見回りは起動時に動きます。

**外には何も出ません。** アラートと通知が表示されるのは `GET /api/scheduler` とページ
`/schedules`（定期タスク）だけです。`notify_targets` は保存して表示するだけで、そこへ何かを送る
ものはありません。

## 2. 手順

スイッチはどれも起動時に読みます。サーバーを止め、変更し、リポジトリの直下から起動し直して
ください。環境変数はサーバーの環境に入れます。例:

```bash
STIRA_HEARTBEAT=1 STIRA_SCHEDULER=1 scripts/stira_ctl.sh start
```

### 2.1 ハートビートをオンにする

次のどれか 1 つ:

* 設定画面 → セクション **ハートビート**（`/settings#sec-heartbeat`）→ **ハートビートを使う**、
  保存して再起動（保存した変更は再起動後に反映されると画面に出ます）
* `.local/stira/gateway-config.json`: `"hb_enabled": true`
* `STIRA_HEARTBEAT=1`（`0` で強制的にオフ。設定されている間、画面のスイッチは変数名つきで固定表示に
  なります）

確認 — 起動時の行と状態:

```text
   Heartbeat: on (zero-model tick)
```

```bash
curl -s http://127.0.0.1:5555/api/gateway/status | python3 -m json.tool
```

```json
"heartbeat": {
  "consecutive_failures": 0, "degraded": false, "enabled": true,
  "interval_sec": 5291.999999999999, "last_error": null, "last_level": "normal",
  "last_tick_at": 1793021488.738549, "next_tick_at": 1793026780.738549,
  "running": true, "state": "running", "tick_count": 1
}
```

オフのときは同じオブジェクトが `{"enabled": false, "running": false}` です。

### 2.2 スケジューラーをオンにする

`STIRA_SCHEDULER=1`（オンになるのは `1`、`true`、`yes`、`on` だけ）。起動時の行:
`   Scheduler: running`（オフ: `   Scheduler: off`）。

```bash
curl -s http://127.0.0.1:5555/api/scheduler | python3 -m json.tool
```

```json
"status": {
  "actions": ["health_check", "memory_harvest"], "agent_jobs": false, "alerts": [],
  "breaker_level": "normal", "enabled": true, "error": null, "heartbeat_healthy": true,
  "memory_job_kinds": [], "running": true, "state": "running", "tokens_today": 0
}
```

オフ: `{"jobs":[],"notifications":[],"runs":[],"status":{"enabled":false,"running":false}}`。
起動の直後は、スケジューラーがハートビートの最初の見回りを読むまで `heartbeat_healthy` が
`false` のことがあります（1 回観測。次に読んだときは `true` でした）。

### 2.3 最初のジョブを作る: 1 時間ごとのヘルスチェック（モデルを使わない）

**制約: `/schedules` からジョブは作れません。作成は API だけです**（`POST /api/scheduler/jobs`）。
ページにあるのはジョブの一覧と、一時停止・再開・取消です。

ジョブの作成・再開・変更は、人がいないときに後で何かを動かすことになるので、次が必要です。

1. ダッシュボードの CSRF トークン（`GET /api/csrf`）と同一オリジンのリクエスト
2. `"confirm": true` と、ブレーカーが NORMAL であること（WARNING 以上: 409）
3. YOLO の許可がオンの間（既定）は、種類 `scheduler_loosen` の**確認つき操作**を**人が**確認する
   こと: 最初のリクエストは 428 `host_confirmation_required` で操作（`id`、`short`、`channel`）を
   返します。人がそれを確認し、そのあと**同じ**リクエストをヘッダー
   `X-Stira-Host-Operation: <id>` をつけてもう一度送ります。

> **エージェントや無人のスクリプトに、自分の操作を自分で確認できる力を渡さないでください。**
> 確認こそが「人が関わる」部分です。リクエストを送るものが確認もしてはいけません。

人がどこで確認するかはチャネル（`STIRA_YOLO_HOST_CONFIRM`、起動時に読みます）で決まります。
チャネルは確認つきの操作すべて（ツールルールを緩める保存、YOLO の要求、グラント）に効きます。

| チャネル | 設定 | 人の確認のしかた | ここでの確認（現在の `main`） |
|---|---|---|---|
| `command` | `STIRA_YOLO_HOST_CONFIRM=command` | サーバーのコンピューターの端末で `stira approvals decide <short>`（TTY、同じ OS ユーザー、サーバーが起動したプロセスでないこと）。照合コードを確かめて `APPROVE` と入力。`--deny` で拒否 | **確認済み** — API での作成と再開、`/schedules` の **再開**（ページに「Confirm on the host: run stira approvals decide … then type the word」と出て、確認後に自動で完了） |
| `dialog` / `auto` | `STIRA_YOLO_HOST_CONFIRM=dialog` または `auto`（macOS。`auto` は macOS のデスクトップセッションの外ではコマンドを使う） | OS の認証つきデスクトップダイアログ | **未確認**（マシンのデスクトップにダイアログが開くため） |
| `browser` | 既定（変数なし） | ダッシュボード自身の確認ダイアログ — UI が使う仕組みで、**スクリプトにするものではありません** | 作成: ページがないので、人が確認する場所がありません。`/schedules` の再開: **確認済み**（ヘッドレス Chrome）— ページの再開ダイアログのあとに確認ダイアログが開き（チェックを入れて確認）、ジョブが再開されます。そこで取り消すと何も適用されません。#650 の修正より前はこの確認ダイアログが開かず、「Refused: Cancelled; nothing was applied」になっていました。**スケジューラーのジョブには `command` を使ってください**（作成には確認するページがありません）。 |

`STIRA_YOLO_ALLOW=off` は確認つき操作そのものをなくし（コードから。ここでは未実行）、緩める操作
すべてからこの人の手順を外します — 推奨しません。

したがって、このガイドで勧める起動は次のとおりです:

```bash
STIRA_HEARTBEAT=1 STIRA_SCHEDULER=1 STIRA_YOLO_HOST_CONFIRM=command scripts/stira_ctl.sh start
```

リクエストを送り、操作を表示して**人を待つ**補助スクリプトです — 自分では何も確認しません:

```bash
#!/bin/bash
# send-and-wait.sh <path> <json>: send a scheduler request; on 428 print the operation and wait
# until a PERSON has confirmed it on the host, then send the same request again with its id.
# It never confirms anything itself.
B=${B:-http://127.0.0.1:5555}
T=$(curl -s $B/api/csrf | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
H=(-H "X-CSRF-Token: $T" -H "Origin: $B" -H 'Content-Type: application/json')
R=$(curl -s -w '\n%{http_code}' -X POST "$B$1" "${H[@]}" -d "$2")
code=$(echo "$R" | tail -1); body=$(echo "$R" | sed '$d')
[ "$code" = 428 ] || { echo "$code $body"; exit 0; }
read -r OP SHORT CHANNEL < <(echo "$body" | python3 -c 'import json,sys;o=json.load(sys.stdin).get("operation") or {};print(o.get("id","-"),o.get("short","-"),o.get("channel","-"))')
[ "$OP" != - ] || { echo "$code $body"; exit 1; }
echo "Operation $SHORT ($CHANNEL channel) waits for a person."
[ "$CHANNEL" = command ] && echo "In a terminal of this computer: stira approvals decide $SHORT"
[ "$CHANNEL" = dialog ] && echo "Confirm it in the desktop dialog."
read -r -p "Press Enter after you confirmed it (Ctrl-C to give up) " _
curl -s -w '\n%{http_code}\n' -X POST "$B$1" "${H[@]}" -H "X-Stira-Host-Operation: $OP" -d "$2"
```

端末 1（送って待つ）:

```bash
./send-and-wait.sh /api/scheduler/jobs '{"job": {"job_id": "health-hourly",
  "name": "hourly health", "action": "health_check",
  "schedule": {"type": "cron", "expr": "0 * * * *", "timezone": "Asia/Tokyo"}},
  "confirm": true, "actor": "you"}'
```

```text
Operation 0007762a (command channel) waits for a person.
In a terminal of this computer: stira approvals decide 0007762a
Press Enter after you confirmed it (Ctrl-C to give up)
```

端末 2（人。待っているものは `stira approvals pending` で一覧できます）:

```text
$ stira approvals decide 0007762a
Stira: スケジュールの作成・再開・変更 / create / resume / change a job
要求 / request: 0007762a
内容 / what: operation=create job=-
照合コード / code: eef30307
残り / expires in: 99 s
自分で要求していなければ拒否してください / Deny it if you did not ask for this.
確認するには APPROVE と入力 / Type APPROVE to confirm: APPROVE
Stira: confirmed; the page applies it now
```

端末 1 に戻って Enter:

```text
{"action":"health_check", ... "created_by":"local:you", ... "job_id":"health-hourly",
 "layer":"gateway", "missed_policy":"skip", ... "next_run_at":1793026800.0, ... "state":"active", ...}
200
```

操作には期限があります（最初の答えでは `expires_in` が 120 秒）。確認したらすぐに送り直すか、
新しい操作のためにリクエストを送り直してください。`timezone` のない cron のスケジュールは UTC です。

### 2.4 動いたことを確かめる

次の正時を待つか、1 分ごとに動くテスト用のジョブを作り（最短の間隔は 60 秒）、確かめたら一時
停止します。

```bash
./send-and-wait.sh /api/scheduler/jobs '{"job": {"job_id": "health-test",
  "name": "health test (every minute)", "action": "health_check",
  "schedule": {"type": "interval", "every_sec": 60}}, "confirm": true, "actor": "you"}'
# 1 分後
curl -s http://127.0.0.1:5555/api/scheduler | python3 -c \
  'import json,sys; print(json.dumps(json.load(sys.stdin)["runs"][:1], indent=1))'
```

```json
[{"job_id": "health-test", "run_id": "run-16a2b2d0bdba4bda",
  "run_key": "health-test@1793021627", "status": "completed", "reason": null,
  "summary": "breaker=normal, heartbeat=running", "tokens": 0}]
```

`/schedules` にも同じジョブ・実行・通知の一覧が出ます。

### 2.5 失敗したときの見え方

拒否（すべて観測したもの）:

| 答え | 本文 | 原因 |
|---|---|---|
| 503 | `{"code":"unavailable","error":"the scheduler is not enabled"}` | サーバーの環境に `STIRA_SCHEDULER` がない |
| 403 | `{"error":"Missing or invalid CSRF token."}` | `X-CSRF-Token` がない |
| 428 | `{"code":"host_confirmation_required", … "operation":{… "kind":"scheduler_loosen" …}}` | 確認つき操作（2.3 の 3） |
| 428 | `{"code":"confirmation_required","error":"operator confirmation required"}` | `"confirm": true` がない |
| 400 | `{"code":"invalid","error":"every_sec must be between 60 and 31622400"}` | 間隔が 60 秒未満 |
| 403 | `{"code":"scope","error":"unknown gateway action 'send_mail'"}` | あるのは `health_check` と `memory_harvest` だけ |
| 409 | `{"code":"conflict","error":"the job is active"}` | 一時停止していないジョブの再開 |
| 409 | `{"code":"host_operation_denied", …}` | 人が `stira approvals decide <short> --deny` を実行した。何も適用されていない |
| 409 | `{"code":"host_confirm_on_host", …}` | チャネルが `command` なのにブラウザーでの確認を試みた。確認できるのはホストだけ |

実行時には、スケジューラーが自分のループで**アラート**を出し（ジョブの有無にかかわらず）、
**通知**を残します。各コードの文言はページが組み立てます（[scheduler.md](scheduler.md#codes-the-ui-words-279)）。
ここでは起動の前に `heartbeat.lock` をシンボリックリンクに差し替えて再現しました
（[heartbeat-daemon.md](heartbeat-daemon.md#guarantees)）:

```text
   Heartbeat: ⚠️ heartbeat.lock is unsafe (a symbolic link); not started
   Heartbeat: enabled, not running (UnsafeLockFileError)
```

```json
"status": {"alerts": ["heartbeat_stalled"], "heartbeat_healthy": false, ...},
"notifications": [{"code": "heartbeat_stalled", "params": {}, "severity": "warning",
  "state": "raised",
  "message": "heartbeat is enabled but not ticking (stopped, degraded or overdue)"}]
```

ヘルスチェックのジョブの次の実行は `"summary": "breaker=normal, heartbeat=stopped"` でした。
ページには「ハートビートが止まっています（停止・劣化・遅延）」と出ます。

サーバーが止まっている間に来た予定時刻は、後から実行しません（既定の `missed_policy: "skip"`）。
記録されるのは `<データディレクトリ>/scheduler-events.jsonl` だけで、`runs` には出ません。約 3 分
止めた後:

```bash
grep '"kind":"run_missed"' .local/stira/scheduler-events.jsonl | tail -1
```

```json
{"body":{"count":2,"first":1793021987.209831,"job_id":"health-test","last":1793022047.209831,
 "policy":"skip","truncated":false}, ... "kind":"run_missed", ...}
```

### 2.6 止める

* **ジョブを一時停止する**（いつでも、どのブレーカーの段階でも可。確認つき操作は不要）:
  `/schedules` の **一時停止**、または

  ```bash
  B=http://127.0.0.1:5555; T=$(curl -s $B/api/csrf | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
  curl -s -X POST $B/api/scheduler/jobs/health-test/pause -H "X-CSRF-Token: $T" \
    -H "Origin: $B" -H 'Content-Type: application/json' -d '{"reason": "test done"}'
  ```

  答えは `"state": "paused"` のジョブです。一時停止したジョブは再起動しても一時停止のままです
  （観測）。**再開**には作成と同じく `"confirm": true` と確認つき操作が必要です:
  `./send-and-wait.sh /api/scheduler/jobs/health-test/resume '{"confirm": true}'` と人の
  `stira approvals decide <short>`、または `command` チャネルでの `/schedules` の **再開**
  （どちらも確認済み。ブラウザーチャネルは 2.3 を参照）。
* **実行中の実行を取り消す:** `/schedules` の **取消**、または `POST
  /api/scheduler/runs/<run_id>/cancel {}`。
* **削除はありません。** API にあるのは作成・変更・一時停止・再開・取消だけです。要らないジョブは
  一時停止のままにします（`once` のジョブは自分で終わります）。
* **すべて止める:** `STIRA_SCHEDULER` なしで（または `STIRA_SCHEDULER=0` で）サーバーを起動します。
  ジョブは `scheduler-events.jsonl` に残り、次にスケジューラーをオンにしたときに戻ってきます。
  ハートビートを止めるには、設定画面のスイッチ、`"hb_enabled": false` または `STIRA_HEARTBEAT=0`
  にして再起動します。

停止時、サーバーは例えば次の行を書きます:
`StiraCore: stopped (progress: stopped, chat_turn: off, approvals: closed, scheduler: stopped, memory_jobs: closed, heartbeat: stopped, yolo_host: stopped)`
（`scripts/stira_ctl.sh` を使ったときは `.local/stira.out.log` の最後の行）。

## 3. 個人の Mac に勧める構成

| 設定 | 推奨 | 理由 |
|---|---|---|
| ハートビート | **オン**、間隔は既定（5 分 / 3 時間） | トークンは 0。減衰と時間によるブレーカーの解除が、会話なしでも見えるようになります。モデルを呼ぶジョブすべての開始条件でもあります。間隔は回復の速さを変えません（減衰は時間に比例）。変わるのは解除が表示されるまでの遅れで、最大 1 間隔です（[neural-saturation_ja.md](neural-saturation_ja.md)。30 分の例もあります）。減衰がブレーカーに及ぼす効果: [heartbeat-daemon.md](heartbeat-daemon.md#breaker-interaction-active-decay)。 |
| スケジューラー | **オン** | どのジョブにも、グラントのハートビート開始にも必要です |
| ジョブ | `health-hourly`（2.3）。必要なら[記憶のガイド](guide-memory-routines_ja.md#3-推奨のプロファイル)の週 1 回の `memory_harvest` | どちらもモデルを使いません（トークン 0）。アラートはスケジューラー自身が出します。1 時間ごとのジョブは日時つきの実行の行を足すだけで、スケジューラーが生きていることが目に見える印になります。 |
| エージェント層のジョブ、モデルを呼ぶ記憶ジョブ | **オフ**（既定） | トークンを使い、人がいないときに動く範囲を広げます。目的があるときだけ、上限つきでオンにしてください |
| 確認のチャネル | `STIRA_YOLO_HOST_CONFIRM=command` | 作成と再開を人が端末で確認します（2.3）。既定のブラウザーチャネルでは作成を確認する方法がありません |
| `missed_policy` | `skip`（既定） | スリープする Mac では、起きたときに実行がまとめて走らず `run_missed` として記録されます |

調べなくて済むように、効いている上限を並べます。

* **スケジュール:** `cron`（5 項目、`timezone`、既定は UTC）、`interval`（`every_sec` 60〜31,622,400）、
  `once`（`at`、エポック秒）。**静かな時間帯の設定はありません**: cron の式に書いてください
  （`0 9-22 * * *` = 9 時から 22 時まで毎正時）。
* **連発の上限:** 1 つのジョブの実行は同時に 1 つ（`overlap_policy: "skip"`）、同じ予定時刻は
  2 度始めない（`run_key`）、取りこぼした分はまとめて流さない（`skip`。モデルを呼ぶジョブでは
  `catch_up` を拒否）。グラントのハートビート開始では、ハートビートの起床で 60 秒に最大 5 回、
  反復の間は `autonomy.tick_min_spacing_sec`（既定 300 秒、下限 30 秒）以上
  （[grants_ja.md](grants_ja.md)）。
* **予算:** ジョブごとの `budget.max_runs_per_day` / `max_tokens_per_run` / `max_tokens_per_day`
  （達するとジョブは一時停止、`budget_exhausted`）。エージェント層のプールは UTC の 1 日あたり
  50,000 トークン。記憶ジョブは合わせて UTC の 1 日あたり 100,000（[memory-autonomy_ja.md](memory-autonomy_ja.md)）。
  `health_check` と `memory_harvest` が報告するトークンは 0 です。
* **ブレーカー:** WARNING / COHERENCE では予定時刻を 1 回分、最長 `hold_max_sec`（3600）保留して
  回復したら動かします。CRITICAL では何も始まらず、実行中のものは取り消されます
  （`breaker_critical`）。作成・変更・再開は WARNING 以上で拒否されます。
* **ハートビートの健全さ**（モデルを呼ぶジョブ用）: オン、動作中、劣化していない、最後の見回りが
  `max(3 × 間隔, 120 秒)` 以内 — 基準値の間隔では約 4.4 時間です。

## 4. 確認チェックリスト

- [ ] 起動時: `Heartbeat: on (zero-model tick)` と `Scheduler: running`。
- [ ] `GET /api/gateway/status` → `heartbeat.running: true`、`consecutive_failures: 0`、
      `degraded: false`。`tick_count` は間隔ごとに 1 ずつ増えます。
- [ ] `GET /api/scheduler` → `status.running: true`、`heartbeat_healthy: true`、`alerts: []`。
- [ ] 作ったジョブが `"state": "active"` で `next_run_at` を持っている。動いた後は `runs[0].status`
      が `completed`、`"tokens": 0`。
- [ ] データディレクトリに `heartbeat.lock`、`scheduler.lock`、`scheduler-events.jsonl`、
      `scheduler-events.jsonl.head` がある。
- [ ] 停止の行に `scheduler: stopped` と `heartbeat: stopped` がある。

## 5. 困ったとき

| 見えるもの | コード | 原因 | 対処 |
|---|---|---|---|
| `Scheduler: off`。POST が 503 | `unavailable` | サーバーのプロセスに `STIRA_SCHEDULER` がない | `STIRA_SCHEDULER=1` で起動 |
| `Heartbeat: off` | — | `hb_enabled: true` も `STIRA_HEARTBEAT=1` もない、または `STIRA_HEARTBEAT=0` | 2.1 |
| `Heartbeat: enabled, not running (<クラス>)`、`heartbeat.last_error` | `HeartbeatLockError`、`UnsafeLockFileError`、`HeartbeatIntervalsError` | 別のプロセスが `heartbeat.lock` を持っている / ファイルがシンボリックリンクか自分のものでない / ファイルの最短の間隔が最長より長い | [heartbeat-daemon.md](heartbeat-daemon.md#guarantees)、[operations_ja.md §1](operations_ja.md)。間隔の値を直す（[configuration_ja.md](configuration_ja.md)） |
| アラート / 通知 `heartbeat_stalled` | `heartbeat_stalled` | オンだが、止まっている・劣化している（3 回続けて失敗）・遅れている | `/api/gateway/status` の `heartbeat`（`last_error`、`degraded`）を見る。原因を直して再起動 |
| ログに `run_skipped(heartbeat)`、アラート `heartbeat_required` | `heartbeat_required` | ハートビートが正常でないときのモデルを呼ぶジョブ | ハートビートをオンにするか、承知のうえでそのジョブに `"requires_heartbeat": false` |
| 起動時の警告 `grants: heartbeat_start unavailable (reason=scheduler_off)` | `scheduler_off` | `autonomy.heartbeat_start` がオンでスケジューラーがオフ | `STIRA_SCHEDULER=1` |
| `… (reason=heartbeat_off)`、グラントのカードに `heartbeat_unhealthy` の見送り | `heartbeat_off`、`heartbeat_unhealthy` | ハートビートがオフ、または後で正常でなくなった | 2.1。[autonomy_ja.md](autonomy_ja.md) |
| 通知 `jobs_paused_at_start` | `jobs_paused_at_start` | モデルを呼ぶ記憶ジョブとエージェント層のジョブは、起動のたびに一時停止で始まる | 必要なものを人が再開（確認つき） |
| `job_failing` | `job_failing` | 3 回続けて失敗 | そのジョブの `runs[].reason` |
| `budget_exhausted` | `budget_exhausted` | ジョブか 1 日の予算に達し、ジョブが一時停止した | 予算を上げるか待つ。再開 |
| `approval_required` | `approval_required` | エージェント層のジョブが人の承認の要るツールを呼んだ | ジョブが使えるのは読み取り系のツールだけ |
| `breaker_critical`。作成・再開で 409 | `breaker_critical` | ブレーカーが CRITICAL（409 は NORMAL でないとき） | NORMAL に戻るまで待つ。[neural-saturation_ja.md](neural-saturation_ja.md) |
| `log_recovered` | `log_recovered` | ログが途中で切れた。すべてのジョブを一時停止した | 確認して再開（[operations_ja.md §6](operations_ja.md)） |
| `stira approvals decide` が `not confirmed (nothing was decided; it expires by itself)` と言う | — | 入力した語が `APPROVE` でなかった（または TTY でない） | 端末でもう一度実行し、語を入力する |
| `/schedules` の再開で「Refused: Cancelled; nothing was applied」、確認ダイアログが出ない | — | #650 の修正より前の版の既定のブラウザーチャネル | 更新する。または `STIRA_YOLO_HOST_CONFIRM=command` で起動する（2.3） |
| `status.error: log_unavailable`、すべて 503 | `log_unavailable` | イベントログが検証に通らなかった | [operations_ja.md §6](operations_ja.md) |
| 「その他（コード: harvest_failed）」 | ジョブ自身のコード | `memory_harvest` のコード（`harvest_failed`、`harvest_cancelled`、`harvest_stopped`）にはページの文言がない | [記憶のガイド](guide-memory-routines_ja.md#7-おかしいと思ったら) |

## 6. 自律のグラント

グラントのハートビート開始には、スケジューラー、ハートビート、`autonomy.heartbeat_start`
（`stage_c_exec` とともに）、そしてそのフラグつきで発行したグラントの**すべて**が必要です。手順、
無人の反復のためのツールルール、制約は [autonomy_ja.md §2](autonomy_ja.md) の手順 2 にあり、ここでは
繰り返しません。グラントの発行・再開・確認には**ホストでの確認**が要ります。ホストのチャネルが
なければ拒否されます（`host_channel_required`。起動時の行は
`issue / resume / verify refused: no host confirmation channel (STIRA_YOLO_HOST_CONFIRM, or autonomy.allow_browser_confirm)`）。
ハートビートとスケジューラーがオンであることは、それだけでは何も承認せず、どのグラントも始めません。

## 7. サーバーを動かし続けるには

このガイドの範囲外です: `scripts/stira_ctl.sh`（[operations_ja.md §1](operations_ja.md)）、Mac の
launchd（[install_ja.md §12](install_ja.md)）、Linux の systemd（[deploy-ec2_ja.md §5](deploy-ec2_ja.md)）。
`STIRA_*` の変数は、それらがサーバーを起動するときの環境に入れてください。

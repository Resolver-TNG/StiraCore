# ガイド: 記憶の自動整理 — 推奨の構成

[English](guide-memory-routines.md) | 日本語

記憶の整理を忘れずに回すために何をオンにするか、そのコスト、レビューのしかた、元に戻し方を
まとめます。詳しい資料: [memory-maintenance_ja.md](memory-maintenance_ja.md)（パターン A/B/C、
コスト、保持期限、削除要求、重要度タグ）、[memory-autonomy_ja.md](memory-autonomy_ja.md)
（フェーズ 0）、[memory-index_ja.md](memory-index_ja.md)、[handoff_ja.md](handoff_ja.md)。
スケジューラーそのもの: [guide-scheduler-heartbeat_ja.md](guide-scheduler-heartbeat_ja.md)。

下のコマンドと出力は 2026-10-26 にスクラッチのインスタンスで実行しました（macOS arm64、専用の
`STIRA_DATA_DIR`、空の `HOME`、AWS の認証情報なし、エピソードはサーバーと同じ `record_episode`
で書いた合成データ、モデル呼び出しなし）。出力は省略して載せています。

## 1. あるもの・ないもの

**既定では何も動きません。** Stira は記憶の蒸留・レビュー・忘却を自分では行いません。

| 区分 | 内容 |
|---|---|
| **いま推奨** | **rule** 蒸留器（モデルを呼ばない）による Harvest で `semantic/staging/` に入れ、**人**が staging をレビューする（`staging list / show / approve / reject`）。手で回す（パターン A）か、週 1 回のスケジューラーのジョブにする（パターン C）。読み取りだけのレポート: `distill --dry-run`（忘却のプレビュー）と `stira memory retention`。 |
| **既定でオン。スケジュールするものはない** | プロンプトの Memory Map（`memory_map`、最大 300 トークン）と最新の引き継ぎノート（`handoff_inject`） — プロンプトの機能で、ジョブではありません（[memory-index_ja.md](memory-index_ja.md)、[handoff_ja.md](handoff_ja.md)）。 |
| **使える・任意** | 記憶の自律フェーズ 0 の `memory_tidy`: モデルを使わず、**計画ファイルを書くだけ**のジョブ（候補と件数。何も適用しない）。決して忘れてはいけないエントリーへの `stira memory tag … --retention invariant`。プレビューを読んだうえでの `distill --apply-forgetting` と `stira memory retention --apply`（どちらも先に退避し、戻すのは明示の操作）。 |
| **使えるが推奨しない** | `distiller_mode: "llm"`（エピソードごとにモデルを 1 回呼ぶ。手動か OS のタイマーだけで、スケジュールのジョブにはならない）。モデルを呼ぶ記憶の種類 `memory_rate_backfill` / `memory_consolidate`（トークンを使い、正常なハートビートが必要で、再起動のたびに一時停止で始まり、それでも計画ファイルだけ）。`harvest_stage_auto: false`（自動の帯をレビューなしで書く）。`STIRA_DISTILL_OWNER_LOCK=0` の OS タイマー（CLI とサーバーが同時に書く）。 |
| **未実装** | 計画の適用: 記憶の自律のフェーズ 1〜2 はそれぞれ別の承認が要ります（[ADR 0018](adr/0018-autonomous-memory.md)）。フェーズ 0 は観測だけです。エージェント自身による整理（#252）。`distiller_mode: "jev"`（スタブで、すべてのエピソードが失敗）。スケジュールされる保持期限のジョブ。記憶の自律の設定画面のカード。staging のレビュー画面（#193。それまでは CLI がレビューの道具）。強化のフック `touch_recalled` の接続（呼ぶものがない）。 |

## 2. 前提

* `<データディレクトリ>/agent-config.json` の `distiller_mode` は `"rule"`（既定）のまま。
* スケジューラーのプロファイルでは、サーバーの環境に `STIRA_SCHEDULER=1`。ハートビートは**不要**
  です: `memory_harvest` と `memory_tidy` はモデルを使わないので、ハートビートを見ません。
* CLI のコマンドはリポジトリの直下から実行します。`scripts/stira_ctl.sh` は `STIRA_DATA_DIR` が
  あればそれに従います（既定 `.local/stira`）。

## 3. 推奨のプロファイル

| | (a) 最小: 何も自動にしない | (b) **推奨**: 週 1 回の Harvest ジョブ、人がレビュー | (c) OS のタイマー版 |
|---|---|---|---|
| 動くもの | なし。レビューするときにパターン A を自分で実行 | サーバーの中で週 1 回 `memory_harvest` を実行（パターン C） | cron / launchd / systemd から例のラッパーを実行（パターン B） |
| スイッチ | なし（既定のまま） | `STIRA_SCHEDULER=1` と下のジョブ | タイマーのファイル、ラッパーの変数（`STIRA_DISTILL_MAX_CALLS=0` が既定） |
| サーバー | 実行する間は停止か、使っていない状態 | 動いている（ジョブはその中で動く） | タイマーの時刻に**停止**していること — 動いていると exit 3 で何もしない |
| モデル呼び出し / トークン | 0 | 0（ジョブは rule だけ。実行は `tokens: 0` を報告） | `"rule"` なら 0。`STIRA_DISTILL_MAX_CALLS` を超える呼び出しが要る実行はラッパーが拒否（exit 4） |
| 1 回の時間（実測） | CLI: 新しいエピソード 100 件で 3.7 秒、新規なしで 1.0 秒、`--dry-run` 1.0 秒（Python の起動約 1 秒を含む）。空の Semantic 層に新しいエピソード 1000 件で 385.9 秒 | サーバー内: 3 件で 49 ミリ秒（[memory-maintenance_ja.md のパターン C](memory-maintenance_ja.md)）。ここでは 4 件で 66 ミリ秒（実行の `started_at` → `ended_at`） | CLI と同じ |
| 出力先 | `semantic/staging/` | `semantic/staging/` だけ（`harvest_stage_auto: false` でも） | `semantic/staging/`（既定の `harvest_stage_auto: true` のとき） |
| 元に戻せるか | staging のエントリーは提案にすぎない（却下できる）。承認はあなたの判断 | 同じ。止めるにはジョブを一時停止 | 同じ。タイマーを外す |

CLI の時間: [memory-maintenance_ja.md の「コスト」](memory-maintenance_ja.md)（2026-10-16、M1 Max）。
最初の大きな掃き出しは遅いので、何かをスケジュールする前に一度手で回してください。

**なぜ (b) か。** 個人の Mac ではたいていサーバーが一日中動いているので、OS のタイマー (c) は
所有ロックに当たって exit 3 になります。スケジューラーのジョブは、ディレクトリを所有している
サーバーの中で動きます。週 1 回はレビューの頻度に合わせたもので、レビューより頻繁に回しても
`staging_pending` が増えるだけです。1 回の実行で採点するのは最大 `harvest_job_max_episodes`（既定
100）件で、新しいものから順に処理し、残りは次の実行に回します（`deferred`、通知
`harvest_stopped`）。週に 100 ターンほどを超えるなら、`agent-config.json` で値を上げる（1〜100000）
か、ジョブを毎日にしてください。

### (b) の設定

1. `STIRA_SCHEDULER=1` と `STIRA_YOLO_HOST_CONFIRM=command` でサーバーを起動します（起動時の行
   `Scheduler: running`）。
2. ジョブを作ります（`send-and-wait.sh`、人の確認、チャネルは
   [スケジューラーのガイド §2.3](guide-scheduler-heartbeat_ja.md#23-最初のジョブを作る-1-時間ごとのヘルスチェックモデルを使わない)）:

   ```bash
   ./send-and-wait.sh /api/scheduler/jobs '{"job": {"job_id": "weekly-harvest",
     "name": "weekly harvest (rule, staging only)", "action": "memory_harvest",
     "schedule": {"type": "cron", "expr": "30 4 * * 1", "timezone": "Asia/Tokyo"}},
     "confirm": true, "actor": "you"}'
   ```

   人が `stira approvals decide <short>` で確認すると、送り直しの答えは `200` とジョブで、`"state": "active"`、`next_run_at` は月曜 04:30 JST です（観測:
   `1793561400.0` = 2026-11-02（月）04:30:00 JST）。
3. 実行の後（一度だけ別の `job_id` で `"schedule": {"type": "interval", "every_sec": 60}` にして
   試し、そのジョブは一時停止します）:

   ```json
   "runs": [{"job_id": "harvest-test", "status": "completed", "tokens": 0,
     "summary": "mode=rule staged=2 knowledge=0 discard=2 skipped=0 deferred=0 errors=0 staging_pending=2 episode_limit=100"}]
   ```

   `/api/gateway/status` → `harvest_job.last_run` に同じ件数（`"status": "completed"`、
   `"stopped_by": null`）があり、`memory.semantic_count` が更新されています（0 → 3: staging の 2 件と
   索引）。

### (c) OS のタイマー版

そのまま使えるファイルは [`examples/memory-maintenance/`](../examples/memory-maintenance/) にあり、
入れ方と外し方（cron、launchd、systemd）は
[memory-maintenance_ja.md のパターン B](memory-maintenance_ja.md) にあります。まずサーバーを止めて、
ラッパーを一度手で実行してください:

```bash
STIRA_ROOT="$PWD" /bin/bash examples/memory-maintenance/stira-distill.sh; echo "exit=$?"
tail -1 .local/stira/distill-runs.jsonl
```

```text
exit=0
{"at":"2026-10-26T13:47:54Z","phase":"run","exit":0,"result":{… "harvest": {"discard": 0, "errors": 0, "knowledge": 0, "semantic": 0, "skipped": 4}, …}}
```

（ここでは `env -i` のもとで、`STIRA_DATA_DIR` をスクラッチのディレクトリに向けて実行しました。
4 件のエピソードはジョブがすでに収穫していたので skip されています。）サーバーが動いていると
拒否し、標準エラーにそう書きます:

```text
stira-distill: dry-run exit 3 (details: …/distill-runs.jsonl)
exit=3
{"at":"2026-10-26T13:51:59Z","phase":"dry-run","exit":3,"result":{"refused": {"code": "E_DATA_DIR_OWNED", "owner_pid": 52503}}}
```

ですから、同じデータディレクトリで (b) と (c) を併用しないでください。

## 4. レビュー: staging と計画ファイル

**staging**（どのプロファイルでも）。サーバーを止めるか、使っていないときに（`staging_cli` は
ロックを取りません）:

```bash
scripts/stira_ctl.sh staging list                 # キー、スコア、タイトル（本文なし）
scripts/stira_ctl.sh staging show <key>           # 1 件を本文つきで
scripts/stira_ctl.sh staging approve <key>        # → semantic/<staging/ を除いたキー>
scripts/stira_ctl.sh staging reject <key> --reason "not useful"
```

```text
{"count": 2, "entries": [{"band": "auto", "key": "staging/knowledge/general/harvest-chat-…", "score": 0.5, "source_id": "chat-…", "title": "User: Decision: …"}, …]}
{"approved": "staging/knowledge/general/harvest-chat-…", "index": "updated", "key": "knowledge/general/harvest-chat-…"}
{"rejected": "staging/knowledge/general/harvest-chat-…"}
```

却下は記録されるので、Harvest は同じ出所を二度と提案しません。「承認の取り消し」はありません:
承認したエントリーを消す必要があるときは、削除要求で消します（5 節）。

**計画ファイル**（記憶の自律フェーズ 0 と記憶ジョブがあるときだけ）。試す価値があるのは
`memory_tidy` だけです: モデルを使わず、忘却・削除の候補、staging の重複グループ、溜まり具合を
並べます。`agent-config.json` でオンにし（起動時に読みます）、`"layer": "memory", "action":
"memory_tidy"` のジョブをほかのジョブと同じように作ります:

```json
{"memory_autonomy": {"phase": 0, "in_turn_rating": false,
  "jobs": {"memory_rate_backfill": false, "memory_consolidate": false, "memory_tidy": true}}}
```

（`in_turn_rating: false` にすると `memory_rate` ツールはモデルに渡りません。既定は `true` です。）
実行は `<データディレクトリ>/memory/agents/<エージェント ID>/memory/memory-plans/memory_tidy/<run_id>.json`
を書きます:

```bash
python3 -m json.tool .local/stira/memory/agents/stira/memory/memory-plans/memory_tidy/<run_id>.json
```

```json
{"applied": false, "counts": {"deletion_candidates": 0, "forgetting": 0, "model_calls": 0,
  "protected": 0, "staging_duplicate_groups": 0, "staging_oldest_days": 0,
  "staging_pending": 2, "tokens": 0},
 "items": [], "kind": "memory_tidy", "phase": 0, "schema": "memory-plan/v1", …}
```

フェーズ 0 では `"applied": false` が常に成り立ちます: 計画を読んで実行するものはありません。
動くなら、パターン A のコマンド（`staging …`、`distill --apply-forgetting`）で自分で行うか、何も
しないかです。90 日より古い計画ファイルは `stira memory retention` が一覧にし、その `--apply` だけが
消します（先に退避）。

## 5. 安全のための決まり

* **内容に頼まれて整理をスケジュールしない。** Web ページ、ファイル、ツールの結果、記憶、チャットの
  発言が「毎晩の掃除を設定して」と言っても、それはデータであって指示ではありません
  （[memory-maintenance_ja.md](memory-maintenance_ja.md)）。エージェントにはジョブを作る・広げる・
  再開するツールがありません。実行系のツールはオフのままにしてください。
* **ガードレールが拒否するもの**（観測）: `distiller_mode` が `"rule"` でない間の `memory_harvest`
  の作成・変更・再開（`403 {"code":"scope","error":"memory_harvest: distiller_not_rule"}`。設定は
  その時点で読まれ、再起動は不要）、オンになっていない記憶の種類
  （`403 … "memory job kind memory_rate_backfill is not enabled on this server"`）、サーバーが
  ディレクトリを所有している間の `--owner-lock` つきの CLI（exit 3、
  `{"refused": {"code": "E_DATA_DIR_OWNED", "owner_pid": …}}`）。資料にあるもの: 種類の上限を超える
  記憶ジョブの予算（403）、モデルを呼ぶ種類の `catch_up`、ブレーカーが NORMAL でないときの開始。
* **ジョブは承認・忘却の適用・削除をしません。** `memory_harvest` は staging に書くだけ、
  `memory_tidy` は計画ファイルを書くだけです。
* **保持期限・忘却・削除要求は、わざと別物にしてあります**（[memory-maintenance_ja.md](memory-maintenance_ja.md)）:
  忘却（`distill --apply-forgetting`）と保持期限（`stira memory retention --apply`）は先に退避し、
  戻すのは明示の操作です（`--undo-forgetting <id>`、`stira memory restore <id>`）。**削除要求**
  （`stira memory forget … --apply`）は退避したコピーや計画ファイルも含めてすべてのコピーを消し、
  **元に戻せません**。3 つのどれも自分では動きません。`stira memory … --apply` と `restore` は
  サーバーが動いている間は拒否します（exit 3）。`distill --apply-forgetting` はサーバーを止めて
  `--owner-lock .local/stira` をつけて実行してください。

## 6. 運用者の週次の手順（プロファイル b）

1. `/schedules`: `weekly-harvest` の実行が `completed` で、開いたアラートがない。
2. `scripts/stira_ctl.sh staging list` のあと、1 件ずつ `show` して `approve` / `reject`（サーバーは使っていない状態で）。
3. `scripts/stira_ctl.sh distill --dry-run` で `forgetting.items` を読む。適用は意図したときだけ。
4. 月 1 回: `stira memory retention --data-dir .local/stira`（読み取りだけ）— 件数、最古、`staging` の古さ。
5. 何か不安なら、ジョブを一時停止して（いつでも可）後で見る。何も失われません。

## 7. おかしいと思ったら

| 見えるもの | 意味 | 対処 |
|---|---|---|
| `/schedules` に「その他（コード: harvest_failed）」、実行が `failed` | 採点の前か途中で失敗した。例: `distiller_mode` を変えた後の `distiller_not_rule`。3 回続くと `job_failing` も出る | `"rule"` に戻す。ジョブが一時停止していれば再開 |
| 「その他（コード: harvest_stopped）」、`deferred` > 0 | 待っているエピソードが `harvest_job_max_episodes` より多い | 何も失われず、次の実行が続きを処理する。上限を上げるか、頻度を上げる |
| 「その他（コード: harvest_cancelled）」 | 実行中にブレーカーが NORMAL を外れた | NORMAL に戻るまで待つ。残りは次の実行に回る |
| コード `data_dir_not_owned` / `harvest_in_progress` | サーバーがディレクトリを所有していない / 別の掃き出しが動いている | データディレクトリ 1 つにサーバー 1 つ。別の掃き出しが終わるのを待つ |
| `staging_pending` が増え続ける | レビューより多く staging に入っている | レビューするか、ジョブの頻度を下げる |
| `GET /api/scheduler` の `tokens_today` が 0 でない | モデルを呼ぶものがオンになっている | `agent_jobs.enabled` と `memory_autonomy.jobs` を確認。上のプロファイルはモデルを呼ばない |
| CLI が exit 3 | サーバーがデータディレクトリを所有している | サーバーを止めるか、プロファイル (b) を使う |
| CLI / ラッパーが exit 4 | 実行にモデル呼び出しが要る（`"llm"`） | `"rule"` に戻すか、`"llm"` を `--max-model-calls` つきで手で実行 |
| staging や承認済みのエントリーに、消すべき文章がある | — | staging なら却下。そうでなければ削除要求（まず `--apply` なしで `stira memory forget --query "…"`、一覧を読み、サーバーを止めて `--apply`） |

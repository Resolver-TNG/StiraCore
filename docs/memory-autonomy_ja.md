# 記憶の自律 — フェーズ 0（観測のみ）

設計: [ADR 0018](adr/0018-autonomous-memory.md)（2026-10-20 承認。対象はフェーズ 0 のみ。フェーズ 1・2
はそれぞれ指揮官の別の承認が要る）。Issue #251、#252、#506 の「front matter の偽装」の点。英語版:
[memory-autonomy.md](memory-autonomy.md)（詳細はこちらが正）。

記憶は 3 種類（エピソード / 意味 / 手続き）のまま、感情の値は生の値のまま
（[ADR 0017](adr/0017-emotion-proximity-recall.md)）。フェーズ 0 は「エージェントが何を決めるか」「ジョブが
何をするか」を記録するだけで、**覚える・思い出す・忘れる内容は何も変えない。**

## 有効にする

既定は OFF。`agent-config.json`（起動時に読む）:

```json
"memory_autonomy": {
  "phase": 0,
  "in_turn_rating": true,
  "jobs": {"memory_rate_backfill": false, "memory_consolidate": false, "memory_tidy": false},
  "daily_token_budget": 100000,
  "undo_window_days": 30
}
```

* 無し / `{}`: **OFF**。`memory_rate` ツールは出ない、記憶ジョブの種類も無い、何も束縛しない。
  `/api/gateway/status` の `memory_autonomy` は `null`。
* `"phase"` は必須で `0` のみ。1・2、不明なキー、10 万を超える予算は読み込みで拒否。
* `STIRA_MEMORY_AUTONOMY=0`（`false` / `no` / `off`）で、ファイルに関係なく強制 OFF。ON にする値は無い。
* 設定画面のカードはまだ無い（後の Issue）。マニフェストでは `agent.memory_autonomy`（クラス A、`ui: no`）。

## あるもの・わざと効かせていないもの

| 部品 | フェーズ 0 で動くこと | フェーズ 0 では効かないこと |
|---|---|---|
| `memory_rate(importance, reason, half_life_days?)` | `in_turn_rating` が ON なら出る。採点はそのターンのエピソードに束縛 | **何も読まない**: 振り分け・想起の重み・半減期・保持に影響しない |
| `index.sqlite3` の `memory_ratings` 表 | ハーネスの刻印つきの行。エピソードの SHA-256 に束縛 | `effect_of`（振り分け `max(rule, agent)`、信頼の上限 0.5）はフェーズ 1 の式。どこからも呼ばない |
| front matter の偽 `importance`（#506） | Harvest と連想想起が無視する | — |
| 記憶ジョブ | 人が作り、専用の実行器で動き、計画ファイルを書く | 計画は適用しない。束縛・staging・圧縮・アーカイブ・削除はしない |
| `harvest_stage_auto: false` | 変更なし | フェーズ 1 で削除（指揮官の決定 Q6） |

## `memory_rate`

モデルが渡すのは最大 3 つ: `importance`（0〜1）、`reason`（`decision` / `error_fix` / `preference` /
`fact` / `procedure_hint` / `routine` / `other` のどれか）、任意の `half_life_days`。残り
（`rated_by: agent_turn`、`rated_at`、サーバーの `turn_id`、チャットの `model_ref`、ターンの `trust`、
`capped`）はハーネスが付ける。本文・キー・ターン・保持の引数は無く、記憶の本文もファイルも書かない。

* **束縛:** サーバーがそのターンのエピソードを書いた後（`record_episode`）、そのファイルの全文の
  SHA-256 に束縛する。後から（ファイルツール・手作業・`memory_write` で）変われば、その行は「無い」扱い。
  Harvest 自身の印付けは、書き換え前に一致していた行だけを新しい本文に移す（`origin` と同じ）。
* **範囲:** 有限の数でないもの・真偽値・文字列は拒否（`E_AGENT_IMPORTANCE_INVALID`）。[0, 1] の外、
  [7, 180] 日の外は丸めて `capped: clamped` を記録。`half_life_days` が無ければ値から導く
  （< 0.2 → 7、< 0.4 → 14、< 0.6 → 30、< 0.8 → 60、それ以上 → 120 日）。
* **`invariant` は受けない:** `reason: "invariant"` は拒否（`E_INVARIANT_NOT_ALLOWED`）。人の決定のまま（Q2）。
* **回数:** 1 ターン 1 回（`E_ONE_PER_TURN`）。`>= 0.8` は UTC の 1 日 10 件まで、それ以降は 0.79・
  `capped: quota` で保存。
* **ブレーカー:** NORMAL 以外では拒否（`E_BREAKER_NOT_NORMAL`）。ターンの外でも拒否（`E_NO_TURN`）。
* **信頼（Q8、#458 まで）:** 人のメッセージで始まったターンで、文脈の会話に `current_time` / `stop` /
  `handoff_to_user` / `memory_rate` / `memory_write` / `memory_list` / `journal` / `propose_procedure`
  以外のツール結果が無いときだけ `trusted`。Web・ファイル・LSP・MCP・子エージェントの結果、記憶の読み戻し、
  未知のツール、Webhook・ボット・自動継続のターンは `untrusted`（`capped: untrusted`）。値はそのまま保存し、
  0.5 の上限はフェーズ 1 からの「効果」に掛かる。
* **ツールのルール（Q3）:** ツールのルールに**読み取り**の分類で載る。新規インストールと `dev` / `research`
  プロファイルでは既定 **Always**、`minimal` は Never。これより前に保存されたルールには項目が無いので、
  人が ON にするまで **Never**（後から増えたツールと同じ）。YOLO の分類は **internal**（パス無し）。
* **状態:** `/api/gateway/status` → `memory_autonomy.ratings`（`total`、`today`、`high_today`、
  `untrusted_today`。件数のみ）。

## front matter の偽 `importance`（#506）

エピソード層はエージェントのファイルツールの書込み先なので、front matter は「申告」にすぎない。Harvest
（`RuleBasedDistiller` の下限、`distillation_cli` の予告）と連想想起は、エピソードがサーバーの
エピソード書込み器の書いたとおり（検証済みの origin が `extracted`。全文の SHA-256 をデータディレクトリの
床にある `index.sqlite3` に記録）のときだけ `importance` を使う。そうでなければ Harvest は保存本文を
採点し直し（既定のルールの点）、想起は中立の 0.5 を使う。`importance_error` はそのまま尊重する
（レビューに回すだけなので）。

再現と修正: 本文の点が 0.25 のターンの front matter を `importance: 0.95` に書き換えると、以前は staging
に入った。今は本文の点で discard。サーバーが記録し誰も編集していないエピソードは、記録した事例の再生
（`tests/harvest_replay_cases.py`、前の main で記録）で**バイト単位で同じ**判定になる。挙動の変化:
検証済みの origin の無いエピソード（#294 より前のもの、手で編集したもの）は記録された下限を失い、
保存本文で採点される。

## 記憶ジョブ（計画ファイルのみ）

スケジューラ（`STIRA_SCHEDULER=1`）の固定の 3 種類。`memory_autonomy.jobs` でそれぞれ有効にする。上限と
内容は英語版の表のとおり。作り方は普通のジョブと同じ（`POST /api/scheduler/jobs`、`"confirm": true`、
人、ブレーカー NORMAL。`"layer": "memory"`、`"action": "<種類>"`）。記憶ジョブにはプロンプトもツールも
無い（種類が決める）。種類の上限を超える予算は拒否、省略すれば上限。エージェントにはジョブを作る・広げる
ツールは無く、実行中のジョブはスケジュールを変えられない。

* **計画ファイルだけ:** `<memory_dir>/memory-plans/<種類>/<run_id>.json`（`0600`、データディレクトリの床、
  `applied: false`）。テストでは各実行の前後で、全記憶層・検索索引・感情の記録・忘却アーカイブのハッシュが
  変わらないことを確かめる。
* **計画ファイルは削除に従い、期限で消える:** 計画はエントリをキーで名指しする（`memory_consolidate`
  の計画はそこから作った要約も持つ）。削除要求（`stira memory forget`、`--id` だけでも）は、忘れる
  エントリを名指しするか本文を含む計画ファイルと、その退避コピーを消す。削除後にどの計画ファイルも
  削除したエントリを名指ししないことをテストで確かめる。`stira memory retention` は
  `memory_retention.memory_plans.max_age_days`（**既定 90**）より古い計画ファイルを一覧にし、
  `--apply` のときだけ退避してから消す（`stira memory restore <id>` で戻る）。勝手に消えることはない
  （[memory-maintenance_ja.md](memory-maintenance_ja.md#保持期限削除要求出所タグ294)）。
* **専用の実行器:** スケジューラのジョブワーカーから、記憶実行器の専用スレッド（`stira-memory-job`）へ。
  ハートビートの tick からは届かない。
* **モデルを作れるのは `MODEL_CALLING_KINDS` だけ**（`memory_rate_backfill`、`memory_consolidate`）。
  モデルは蒸留器の小型モデル（Q5）。送るのは `mask_secrets` を通した保存本文。答えは種類ごとの JSON
  オブジェクト 1 つだけ（違えば項目ごとに `E_OUTPUT_SCHEMA`）。
* **トークン:** 各呼び出しの前に最悪値（プロンプトの文字数 + 出力上限 + 64）が `max_tokens_per_run` に
  収まらなければ止まる（`stopped_by: token_cap`）。報告された使用量が上限を超えれば失敗
  （`budget_exceeded`）し、ジョブは一時停止。開始時に `max_tokens_per_run` を、ジョブの日次上限と、全記憶
  ジョブ合計の `daily_token_budget`（UTC の 1 日 10 万）に対して予約する。超える開始は拒否し、人が再開する
  まで停止。
* **ブレーカー NORMAL のみ:** 開始時と各項目の前に確認。外れたら項目の切れ目で止まる（`stopped_by: breaker`）。
* **ハートビート**（[ADR 0021](adr/0021-scheduler-job-base.md) §8、S1）: モデルを呼ぶ種類は、ハートビート
  （#238）が正常なとき（有効・動作中・劣化なし・最後の tick が `max(3 × 間隔, 120 秒)` 以内）だけ開始する。
  そうでなければ `run_skipped(heartbeat)` を記録し、`heartbeat_required` を 1 回だけ出す。**ハートビートが
  OFF なら、これらの種類は動かない**。ジョブごとに `"requires_heartbeat": false` で外せる。`memory_tidy` は
  対象外。モデルを呼ぶ種類では `missed_policy: "catch_up"` を拒否する。
* **再起動（Q4）:** モデルを呼ぶ種類は**一時停止**で立ち上がり（`by: system`）、人が再開（`confirm: true`）
  するまで動かない（起動ごとに件数付きの `jobs_paused_at_start` 通知を 1 回）。`memory_tidy` は通常の取りこぼし規則のまま。記憶の自律が OFF のサーバーでは、保存済みの
  記憶ジョブはすべて一時停止。
* **所有者 1 つ:** `server.lock`、ストアごとの Harvest のロック、記憶ジョブの排他（ストアごとに 1 つ）。
  どれも待たない。
* **監査:** `audit.memory.planned` / `.refused` / `.rated` の行は、種類・ジョブ / 実行 / ターンの ID・
  キーの鍵付きハッシュ・0.05 刻みの点・理由コード・整数の件数だけ。本文、平文のキー、パスは残さない。

## tick はモデルを呼ばない（証明できる形で）

既存の 3 つのテストに加えて、`stiracore.heartbeat` のモジュールレベルの import の閉包が `strands`、
`providers`、`agent`、`scheduler`、`control`（記憶ジョブ）、`memory_autonomy`、`memory_rating` に届かない
ことの検査（import を 1 つ足すと見つかる変異検査つき）、記憶の自律・全種類・スケジューラ・ハートビートを
ON にしたチャットサーバーでの tick（記憶実行器・モデルのファクトリ・Strands `Agent`・モデルに罠）、
`MODEL_CALLING_KINDS` の固定の一覧。

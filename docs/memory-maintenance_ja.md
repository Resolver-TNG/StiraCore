# 記憶の整理：手動実行、OS のタイマー、そのコスト（#317）

English: [memory-maintenance.md](memory-maintenance.md)

**既定では何も動かない。** Stira は記憶の蒸留・レビュー・忘却を自分では行わない。ハート
ビートの tick（#238）とスケジューラ（#239）は既定 OFF。tick は記憶の処理をしない（decay・
snapshot・ブレーカーの照会）。スケジューラを ON にしても、Harvest が動くのは人間が作った
`memory_harvest` ジョブとしてだけ（パターン C、#251。rule 蒸留器だけ、出力は staging だけ）。
エージェント自身による整理（#252）は**未実装**。

今あって、実行できるもの:

| コマンド | 内容 | モデル呼び出し |
|---|---|---|
| `python -m stiracore.control.distillation_cli --base-dir <memory> --config <agent-config.json>`（`scripts/stira_ctl.sh distill`） | Harvest を 1 回（Episodic → Semantic。既定はレビュー待ちの staging へ）、忘却の**プレビュー**、削除候補の件数 | 既定の `distiller_mode: "rule"` ではなし |
| 同上 `--dry-run` | 同じ引数で実行したら何件をどう処理するか、予測される振り分け（rule）、モデル呼び出しの回数と大きさ（llm）。何も作らず書かない（揃っていないストアは開かない。`--owner-lock` は確かめるだけ） | なし |
| 同上 `--max-model-calls N` | `llm`: この実行で採点するのは最大 N 件。待っていたものが多ければ exit 4（`--dry-run` と一緒なら、プレビューが N を超えるか分からない時に exit 4） | 最大 N 回 |
| 同上 `--apply-forgetting` | プレビューの対象を、元の内容を退避してから圧縮する（#294） | なし |
| 同上 `--list-forgotten` / `--undo-forgetting <id>` | 退避した元の内容の一覧／復元 | なし |
| `python -m stiracore.control.staging_cli --base-dir <memory> list\|show\|approve\|reject\|reindex`（`stira_ctl.sh staging ...`） | Harvest が staging に入れたものを人間がレビューする（#293） | なし |
| `stira memory retention\|forget\|restore\|removed\|jobs --data-dir <データディレクトリ>` | 保持期限のレポートとオプトインの取り消し可能な実行、全コピーに届く削除要求（[下記](#保持期限削除要求出所タグ294)） | なし |
| `stira memory tag --data-dir <データディレクトリ> --id <層>/<キー> --retention invariant\|derived\|none [--half-life-days N] [--apply]` | 忘却に対する記憶の重要度タグ: 忘れない、または半減期（[下記](#重要度タグretention-invariant--derivedと半減期173)） | なし |

`<memory>` は `<データディレクトリ>/memory`（チャットサーバーでは `.local/stira/memory`）、
設定は `<データディレクトリ>/agent-config.json`。設定ファイルが無ければ既定値
（`distiller_mode: "rule"`）になる。終了コード: `0` 完了、`1` 何かが失敗（エピソード、拒否
された忘却、使えない蒸留器）、`2` 使い方の誤り、`3` データディレクトリを稼働中のサーバーが
所有している、または `server.lock` が自分の通常ファイルではない（`--owner-lock`、後述。
`E_DATA_DIR_OWNED` / `E_LOCK_FILE_UNSAFE`）、`4` モデル呼び出しの上限（`--max-model-calls`）。
例のラッパーは `5`（安全でないログパス）と `6`（ログを書けない）を加える。

回し方は 3 通り。**A** が既定、**B** は OS のタイマー、**C** はサーバー内のスケジューラのジョブ
（#251。#252 は未実装）。

## パターン A — 自動化しない（既定）

サーバーを止めるか、使っていない時に、自分でコマンドを実行する:

```bash
scripts/stira_ctl.sh distill --dry-run   # 何が起きるか。何も書かない
scripts/stira_ctl.sh distill             # Harvest + 忘却のプレビュー
scripts/stira_ctl.sh staging list        # レビュー待ちの一覧（本文なし）
scripts/stira_ctl.sh staging show <key>
scripts/stira_ctl.sh staging approve <key>   # または reject <key> --reason "..."
```

`stira_ctl.sh distill` は追加の引数を CLI に渡す。`--owner-lock .local/stira`
（checkout で実行）を付けると、サーバーがデータディレクトリを所有している間は実行を拒否する
（exit 3。付けなければ稼働中のサーバーは排除されない）。`distill --apply-forgetting` を
実行する前に、出力の `forgetting.items` を必ず読む
（[運用 §12](operations_ja.md#12-忘却は退避付き取り消しは明示の経路だけ294)）。

頻度の目安: staging をレビューするタイミングに合わせる。個人利用なら週 1 回で足りる。
1 回の実行で採点するのはまだ採点していないエピソードだけなので、頻繁に回しても軽いが、
staging に入ったものは結局人間が見る必要がある。レビューより頻繁に回しても待ち行列
（出力の `staging_pending`）が伸びるだけ。

`stira_ctl.ps1 distill`（Windows）も同じ処理をするが、追加の引数を渡さない。
`--dry-run` などはモジュールを直接呼ぶ。

## パターン B — OS のタイマーから CLI を呼ぶ（rule 蒸留器）

コピーして使えるファイルは [`examples/memory-maintenance/`](../examples/memory-maintenance/) にある:

| ファイル | 用途 |
|---|---|
| `stira-distill.sh` | どのタイマーからも呼ぶラッパー（後述） |
| `crontab.txt` | cron（Linux、macOS） |
| `com.example.stira-distill.plist` | launchd（macOS、ユーザー単位の LaunchAgent） |
| `stira-distill.service` + `stira-distill.timer` | systemd のユーザーユニット（Linux） |

どの例も毎日 04:30（ローカル時刻）に実行し、ラッパーを呼ぶだけ。checkout のパス
（`/home/YOU/StiraCore`、`/Users/YOU/StiraCore`、`%h/StiraCore`）を書き換える。

### ラッパーがすること

0. **最初に設定を検査する。** 数値の設定は 1〜6 桁の 10 進数だけを受け付ける
   （`STIRA_DISTILL_OWNER_LOCK` は `0` か `1`）。それ以外は何も実行する前に exit **2**。値を
   シェルの算術式として評価することはない。ログのパスが symlink か通常ファイルでない
   （`/dev/full`、ディレクトリなど）場合も、何も実行する前に exit **5**。ファイルは
   `umask 077` で作る。
1. **先に dry run、引数は同じ。** CLI の引数は 1 回だけ組み立て（ラッパーに渡したもの、例えば
   `--config other.json` も含む）、そのまま `--dry-run` と本実行の両方に渡す。プレビューで
   モデル呼び出しが `STIRA_DISTILL_MAX_CALLS`（既定 **0**）を超える、または分からない場合は
   exit **4** で実行を拒否する。既定の rule 蒸留器は呼び出しが 0 なので、あとで設定を
   `"llm"` に変えても、タイマーから黙ってトークンを使い始めることはない。
2. **本実行にも同じ上限。** プレビューと本実行は別のプロセスなので、その間に設定や記憶が
   変わり、見積りが古くなることがある。そこで本実行にも `--max-model-calls` を渡す。`llm` の
   実行は最大その件数だけ採点し、残りは次の実行に回して exit **4**（JSON の `model_call_limit`）。
3. **書き手は 1 つ。** 両方に `--owner-lock <データディレクトリ>` を付ける。本実行は実行中ずっと
   サーバーの所有者ロック（`server.lock`、#285）を持つ。dry run はロックを確かめるだけで、
   何も作らず書かない。サーバーがディレクトリを所有していればどちらも exit **3** で拒否し、
   何も読み書きしない。実行中に起動したサーバーは起動を拒否する。タイマーはサーバーを止めて
   いる時間帯に設定するか、承知のうえで `STIRA_DISTILL_OWNER_LOCK=0` にする（その場合
   サーバーは排除されず、CLI とサーバーが同じ記憶ディレクトリに同時に書くことがある）。
4. `distillation_cli` を**実行**する（`--apply-forgetting` を渡さない限り忘却はプレビューだけ）。
   `--undo-forgetting` / `--list-forgotten` はラッパーを通さない（dry run が拒否する、exit 2）。
   直接実行する。
5. **黙って止まらない。** 毎回 1 行の JSON（`at`、`phase`、`exit`、CLI の JSON）を
   `<データディレクトリ>/distill-runs.jsonl` に追記する。0 以外で終わった時は stderr にも 1 行
   出す — cron は `MAILTO` が使えればメールし、launchd は `StandardErrorPath` に書き、systemd は
   journal に残してユニットを failed にする — そして終了コードをタイマーに返す。ログの行を
   書けなかった時（ディスクが一杯、権限がない）は exit **6** で、実行そのものの終了コードを
   stderr に出す。Python 自身の stderr は、最後の `STIRA_DISTILL_ERR_LINES` 行（既定 20）だけを
   サーバーのログと同じ秘密情報のマスクに通して `distill-runs.err.log` に残す。マスクを
   実行できない時は何も残さない。マスクはパターンによるもので、認識できない秘密はこの
   ファイルに入りうる（ファイルは所有者だけが読める）。

ログの検査は実行の前に行う。検査の後でログのパスが symlink に差し替えられても検出しない
（`server.lock` と同じく、データディレクトリの権限が境界）。

| 変数 | 既定 | 意味 |
|---|---|---|
| `STIRA_ROOT` | （必須） | checkout |
| `STIRA_DATA_DIR` | `$STIRA_ROOT/.local/stira` | データディレクトリ |
| `STIRA_AGENT_ID` | `stira` | エージェント |
| `STIRA_PYTHON` | `$STIRA_ROOT/.venv/bin/python` | インタープリタ |
| `STIRA_DISTILL_MAX_CALLS` | `0` | 1 回に許すモデル呼び出し数（dry run の `estimate.model_calls`。本実行の `--max-model-calls` にも使う） |
| `STIRA_DISTILL_ERR_LINES` | `20` | 1 段階あたり残す Python の stderr の行数（マスク済み）。`0` = 残さない |
| `STIRA_DISTILL_OWNER_LOCK` | `1` | `0` = `server.lock` を持たない |
| `STIRA_DISTILL_LOG` / `STIRA_DISTILL_ERR_LOG` | `<データディレクトリ>/distill-runs.jsonl` / `.err.log` | ログ（ローテーションなし。1 回 1 行） |

これらの変数を読むのは例のスクリプトだけで、Stira 本体は読まない。

### 有効化・1 回だけ実行・停止

```bash
# cron
crontab -e                      # crontab.txt の行を追加。消せば止まる
# launchd (macOS)
mkdir -p -m 700 /Users/YOU/StiraCore/.local   # StandardErrorPath のディレクトリ（launchd は作らない）
cp examples/memory-maintenance/com.example.stira-distill.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.stira-distill.plist
launchctl kickstart gui/$(id -u)/com.example.stira-distill          # 今 1 回実行
launchctl print gui/$(id -u)/com.example.stira-distill | grep "last exit code"
launchctl bootout gui/$(id -u)/com.example.stira-distill            # 停止
# systemd（ユーザーユニット）
cp examples/memory-maintenance/stira-distill.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now stira-distill.timer
systemctl --user start stira-distill.service                        # 今 1 回実行
systemctl --user disable --now stira-distill.timer                  # 停止
```

実行中のものは、タイマーの通常の停止シグナルで止まる（`systemctl --user stop
stira-distill.service`、`launchctl kill SIGTERM gui/$(id -u)/com.example.stira-distill`、
`kill <pid>`）。中断された実行で印を付け終えていないエピソードは次の実行で処理される
（Semantic にすでに書かれていたものは重複として扱われる）。完全にやめる時はタイマーを外し、
`distill-runs.jsonl` の最後の行も確認する。

### 確認したこと（2026-10-16、macOS arm64、Python 3.11）

* 2 回目（レビュー後）: `a[$(touch …)0]` や `1/0` を含む不正な数値設定は exit 2 で、何も評価・
  実行しない。ラッパーに `--config <llm.json>` を渡しても同じ上限に当たる（exit 4）。symlink・
  デバイス・ディレクトリのログパスは exit 5。書けないログは exit 6。stderr はマスク済みの末尾
  だけを残す。cron の行は空白を含む checkout のパスから動く — すべて
  `tests/test_memory_maintenance_examples.py`。
* cron と同じような空の環境（`env -i`、`PATH=/usr/bin:/bin`、AWS の設定を無効化）でラッパーを
  実行: rule は exit 0 でログ 1 行、`"llm"` の設定は exit 4 でモデル呼び出しなし、稼働中の
  `stira_server` がデータディレクトリを所有していると exit 3、データディレクトリが無いと
  exit 2。同じケースを `tests/test_memory_maintenance_examples.py` で実行している。
* launchd: plist は `plutil -lint` を通る。パスを埋めて bootstrap し、`kickstart` で実行した
  （last exit code 0、ログ 1 行）。ロックを持たせた状態では last exit code 3 で、stderr の
  1 行が `StandardErrorPath` に出た。最後に `bootout`。
* systemd: 2 つのユニットは `systemd-analyze --user verify`（systemd 255）を通り、カレンダー式は
  `*-*-* 04:30:00` に正規化される。**リポジトリを入れた Linux ホストでは実行していない。**
* cron: crontab の行のコマンドを cron 同様の環境で `/bin/sh -c` により実行した（exit 0）。実際の
  crontab には登録していない。Windows のタスク スケジューラ: 例なし、未確認。

## パターン C — サーバー内のスケジューラのジョブで回す（#251、オプトイン、rule のみ）

スケジューラ（#239、[ADR 0008](adr/0008-scheduler.md)）に Gateway action `memory_harvest` が
ある。ほかの Gateway action と同じくモデルを一切呼ばず、**rule 蒸留器だけ**を使う。独自の
タイマーではなく、ハートビートからも動かない。何かが動くには、次をすべて人間が行う必要がある:

1. `STIRA_SCHEDULER=1` でサーバーを起動する（既定 OFF。ON になるのは `1`、`true`、`yes`、`on`
   だけ）
2. `distiller_mode: "rule"`（既定）のままにする。`"llm"` や `"jev"` ではジョブを作成・更新・
   再開できず（`403 scope`、`distiller_not_rule`）、作成後に設定を切り替えた場合も、実行は何も
   採点せずに失敗する
3. スケジューラ API で `"confirm": true` 付きでジョブを作る（`/schedules` は一覧・一時停止・再開だけで
   作成のフォームは無い。YOLO の許可がオンなら作成は確認つき操作にもなる。
   [guide-scheduler-heartbeat_ja.md §2.3](guide-scheduler-heartbeat_ja.md#23-最初のジョブを作る-1-時間ごとのヘルスチェックモデルを使わない)）。API には
   ダッシュボードと同一オリジンのリクエストと CSRF トークンが要る。Stira にはジョブを作る・
   広げる・再開するツールや経路が無く、ジョブの中からも、チャットのメッセージや webhook からも
   作れない（テスト済み）

```json
{"job": {"job_id": "harvest", "name": "nightly harvest", "action": "memory_harvest",
         "schedule": {"type": "cron", "expr": "30 4 * * *", "timezone": "Asia/Tokyo"}},
 "confirm": true, "actor": "you"}
```

**モデルによる採点（`"llm"`）はスケジュールジョブにしない。** `distillation_cli` を手動
（パターン A）か OS のタイマー（パターン B。`STIRA_DISTILL_MAX_CALLS` と `--max-model-calls`
付き）で、サーバーを止めて（`--owner-lock`）実行する。

1 回の実行でやること:

* **書き手は 1 つ。** サーバー内で動き、サーバーは `server.lock` を持っている（#285）。持って
  いなければ実行を拒否する（`data_dir_not_owned`）。同じプロセス内では、同じ memory ディレクトリ
  に対する Harvest はすべて 1 つのロックを共有する: ジョブはほかのジョブやプロセス内の手動実行と
  重ならず（待たずに `harvest_in_progress`）、プロセス内の手動実行どうしは順番を待つ。別プロセス
  を締め出すのは `server.lock` だけ: サーバーの稼働中は `--owner-lock` 付きの CLI が拒否され
  （exit 3）、2 つ目のサーバーは起動しない。**制約:** `--owner-lock` を付けない CLI は締め出され
  ず、サーバーと同時に同じ memory ディレクトリへ書きうる。
* **ブレーカー。** WARNING / COHERENCE の時の発火は保留、CRITICAL では捨てる（ADR 0008 §7）。
  実行自体も開始前と各エピソードの前に確かめ、NORMAL でなければ止まる（`cancelled`。残りは後で）。
* **出力は staging だけ。** 昇格させるものはすべて `semantic/staging/` に入る。
  `harvest_stage_auto` が `false` でも同じ（この設定は手動実行にだけ効く）。承認・却下・忘却の
  適用はしない。
* **上限。** 1 回で採点するのは最大 `harvest_job_max_episodes`（既定 100）件、新しい順。待って
  いるのがちょうどその件数なら通常どおり完了。それより多ければ残りは印を付けずに次回へ
  （`deferred`、`stopped_by=episode_limit`、通知 `harvest_stopped`）。ジョブの
  `budget.max_runs_per_day` はほかのジョブと同じく効く。トークンは常に 0。
* **黙って止まらない。** run の summary は件数とコードだけ（`mode`、`staged`、`discard`、
  `skipped`、`deferred`、`errors`、`staging_pending`、`episode_limit`、`stopped_by`）。エピソードの
  本文・パス・例外の文面は出さない（カナリアでテスト）。失敗や停止は通知になる
  （`harvest_failed`、`harvest_cancelled`、`harvest_stopped`）。3 回続けて失敗すると
  `job_failing` も出る。`/api/gateway/status` の `harvest_job.last_run` に最新の実行が出て、
  `memory.semantic_count` は実行の直後に更新される。
* **止め方:** `/schedules` でジョブを一時停止する（いつでも可）。または `STIRA_SCHEDULER` を外す。

実測（2026-10-17、macOS arm64、偽のチャットモデルを使った実際の `stira_server` プロセス、
チャット 3 ターン、Bedrock なし）: スケジューラ OFF → ジョブ作成は 503、`harvest_job` の状態
なし、印も付かない。スケジューラ ON で `"llm"` → 作成は `403 scope distiller_not_rule`。
`"rule"` ではジョブ（間隔 60 秒）が 60 秒後に 49 ms で動いた: `staged=2 discard=1`、トークン 0、
`semantic_count` 0 → 3（staging 2 件 + index）。その間の CLI `distill --owner-lock` は exit 3。
`"llm"` に切り替えた後の実行は `distiller_not_rule` で失敗（通知 `harvest_failed`）、再開も拒否。
チャットモデルに渡るツール定義と静的なシステムプロンプトのハッシュは、スケジューラ OFF/ON とも
`main` と同じ。

**#252（自己メンテナンス）** は未提供: モデルによる要約・整理は ON/OFF の評価（#143）の後。
出力は提案と staging だけで直接は書かない。整理の量をエージェントの内部状態と結び付けない。

## コスト: トークンと時間（実測）

2026-10-16、Apple M1 Max のノート PC、main=cba495c にこの変更を加えたもので計測。エピソードは
チャットサーバーと同じ `record_episode` で作った合成データ（日本語・英語の短いターン、見出しや
コードを含むものあり）。時間は Python の起動（約 1 秒）を含む実時間。「未計測」は計測していない
という意味で、推定値は入れていない。

| モード | 1 回あたりのモデル呼び出し | 入力 / 出力トークン | キャッシュ読み / 書き | 時間 |
|---|---|---|---|---|
| `rule`（既定） | 0 | 0 / 0（AWS の設定を無効にして実行。モデルは作られない） | 0 / 0 | 新規 100 件: 3.7 秒 · 空の Semantic 層へ新規 1000 件: 385.9 秒 · Semantic 489 件の状態で新規 10 件: 11.2 秒 · 新規なし: 1.0 秒（100 件）/ 3.1 秒（1000 件） · `--dry-run`: 1.0 秒（100 件）/ 1.5 秒（1000 件） |
| `llm`（Bedrock、既定の `distiller_model_id`、ap-northeast-1） | このモデルでまだ採点していないエピソード 1 件につき 1 回 | 2 件（プロンプト計 1190 文字）: 1 回目 213 / 185、2 回目 266 / 111、合計 479 / 296 | 報告なし（蒸留器はキャッシュポイントを置かない、#254） | 1 回 2.18 秒・1.71 秒（モデルの latency 2138 ms / 1704 ms）、実行全体 4.0 秒 |
| `llm`、ほかのプロバイダ・モデル | — | 未計測 | 未計測 | 未計測 |
| `jev` | 0 — 蒸留器はスタブ（#106）。全エピソードが失敗し exit 1 | 0 / 0 | 0 / 0 | 未計測 |

数値が何に依存するか:

* **日数ではなくエピソード数。** チャット 1 ターンでエピソードが 1 件書かれる。1 回の実行で
  採点するのは、同じ版の蒸留器でまだ採点していないエピソードだけ。`--dry-run` は
  `harvest.to_score` を出し、`llm` では `estimate.model_calls`、`estimate.prompt_chars`（各呼び出しで
  送る文面そのもの）、`estimate.max_output_tokens`（1 回の上限 1024 × 回数）を出す。トークン数
  自体は呼んでみるまで分からない。**これらは目安で、厳密な上限ではない:** `prompt_chars` は
  ユーザーメッセージの文字数で、入力トークン数ではない（リクエストにはメッセージの枠が
  加わり、1 トークンあたりの文字数は言語で変わる）。`model_calls` はエピソード 1 件につき 1 回と
  数えるが、SDK は制限や失敗の時に再試行することがあり、プロバイダから見た呼び出し数は
  それより多くなりうる。また本実行が始まるまでに見積りが古くなることがある（ラッパーの 2）。1 日あたりは「1 日のターン数 × 1 回あたりのトークン」で
  計算する。実測した呼び出しは上の 2 回だけ。
* **モードを変えると全件を採点し直す。** `rule` で harvest 済みのエピソードも `llm`（や別の
  モデル）では採点し直す。`--dry-run` で実測: `"llm"` に変えると harvest 済み 20 件のうち
  20 件が `to_score` になった。
* **rule の実行時間は Semantic 層の大きさで伸びる。** 捨てられなかった新規エピソードごとに
  Semantic 層と重複を照合するので、大きな初回は遅い（1000 件で 6.4 分）。その後の少数の追加は
  短い。初回は手で実行する。
* **1 回の呼び出しに記憶 1 件（この変更で修正）。** これまで LLM 蒸留器は 1 回の実行の間
  同じ会話を使い回していて、n 回目の呼び出しがそれまでの記憶と回答を全部送り直していた
  （オフラインの偽モデルで、3 件に対し 1・3・5 メッセージ）。入力がエピソードごとに増え、
  ある記憶の文面が次の採点に影響しうる。今は毎回 1 メッセージだけ送る（上の Bedrock の
  2 回とも 1 メッセージ）。

## 保持期限・削除要求・出所タグ（#294）

#294 で決めたこと。**既定では何も消さず、ここにあるものは勝手に動かない** — タイマーも
スケジューラのジョブもハートビートの処理もない（ADR 0008 でスケジュールジョブはモデルを
呼ばない。将来保持のジョブを足すなら、人間が `confirm` で作る Gateway action にする）。
運用者のコマンドが 3 つあり、エージェントにはこれを呼ぶツールが無い。レポート・ドライラン・
`jobs`・`removed` は読むだけ。`--apply`・`--resume`・`restore` はデータを変える:

```bash
stira memory retention --data-dir .local/stira            # レポート（読むだけ）
stira memory retention --data-dir .local/stira --apply    # 上限を超えた分を退避してから消す
stira memory forget --data-dir .local/stira --query "ある文"          # どこにあるか（読むだけ）
stira memory forget --data-dir .local/stira --id episodic/chat-123    # エントリとそこから派生したもの
stira memory forget --data-dir .local/stira --query "ある文" --apply  # 全コピーを消す（取り消し不能）
stira memory forget --data-dir .local/stira --resume <job-id> --query "ある文"  # 失敗したジョブを最後まで（同じ --id/--query）
stira memory jobs | removed | restore <id> --data-dir .local/stira
```

（`stira` エントリポイントが無い場合は `python -m stiracore.control.memory_cli ...`。）
`--apply`・`--resume`・`restore` はデータディレクトリの所有者ロック（`server.lock`、#285）を
取る。サーバーが動いている間は **3** で終わり何も変えない — 先にサーバーを止める。
終了コード: 0 完了、1 拒否/失敗、2 使い方の誤り、3 所有されている。
コマンドが整形しないエラー（適用前の SQLite 回復がリンクを拒否した時など、#402）は
`{"refused": {"code": "E_UNSAFE_PATH (UnsafePathError)"}}` のように固定コードと例外の型だけを出して
1 で終わる。メッセージもトレースバックも stdout・stderr に出さない（パスが消す言葉を含みうる）。

### 文を取り除く 3 つの方法（わざと違う）

| | 忘却（#320） | 保持期限の実行 | 削除要求 |
|---|---|---|---|
| コマンド | `distill --apply-forgetting` | `memory retention --apply` | `memory forget --apply` |
| 何をする | 古い Semantic を圧縮 | **自分で決めた**上限を超えた分を消す | 1 つの記憶・文を全ての場所から消す |
| 退避 | あり（`forgotten/<id>.json`） | あり（`forgotten/removed-<id>.json`） | **なし** — 該当する退避コピーも消す |
| 戻し方 | `--undo-forgetting <id>`（明示・監査付き） | `memory restore <id>`（明示・監査付き） | なし |

忘却と保持期限は容量とノイズのためなので取り消せる必要がある。削除要求は文そのものを
消すためのもので、退避や履歴テーブルに「旧値」を残すと意味がなくなる（#294 / #342 の調査が
指摘する失敗の型）。

### 保持期限: ストアごとの扱い

上限は `agent-config.json` の `memory_retention` に書く
（[設定 §4](configuration_ja.md#4-agent-configjsonagentconfig-確認済み)）。書かなければ上限は全て未設定で、候補は 0（例外は記憶ジョブの計画ファイル: 既定で 90 日。それでも消すのは `--apply` だけ）。例:

```json
{"memory_retention": {
  "episodic": {"max_age_days": 365, "max_entries": 20000},
  "turns": {"max_age_days": 180},
  "tool_payloads": {"max_age_days": 30},
  "checkpoints": {"max_per_thread": 5},
  "staging": {"warn_after_days": 30},
  "scheduler_log": {"warn_bytes": 50000000}
}}
```

| ストア | 決定 | `--apply` |
|---|---|---|
| Episodic（`episodic/*.md`、1 ターン 1 件） | `max_age_days`、`max_entries`（古い順）、front-matter の `expires_at` は `honour_expires_at: true` の時だけ。Harvest が未採点のエピソードは**保留**（`keep_unharvested`、既定 true） | 退避→削除（索引行と感情記録 #364 も。`restore` で両方戻る） |
| `turns.db` の UI イベント | `turns.max_age_days`: ターン単位で、そのイベントとツール payload ごと | 行を退避→削除→圧縮 |
| `turns.db` のツール payload | 独自の `tool_payloads.max_age_days`（ツールの観測結果は会話と分けて扱い、普通はもっと早く期限が来る。ターンの無い payload は古い扱い） | 行を退避→削除→圧縮 |
| checkpoint（`checkpoints/`、もともとスレッドごと最大 20） | `max_age_days`、`max_per_thread`。**スレッドの最新は常に候補外**（再開に要る） | 退避→削除 |
| 記憶ジョブの計画ファイル（`memory-plans/`、ADR 0018、[memory-autonomy_ja.md](memory-autonomy_ja.md)） | `memory_plans.max_age_days`。設定が無くても**既定 90 日**（年齢はファイルの更新時刻。中断した書き込みの残りも対象） | 退避→削除。`restore` で戻る |
| Semantic、`staging/` | 報告のみ: 忘却は `--apply-forgetting`、staging は人間の判断待ち（`warn_after_days` は件数を数えるだけ） | — |
| Procedural、検索索引、context stash（`retention_hours`、#258）、staging 却下台帳 | 報告のみ | — |
| `scheduler-events.jsonl`、`events.db` | 報告のみ（`warn_bytes`）: head anchor 付きのハッシュ連鎖なので、ローテーションには連鎖の区切りの設計が要る（次回） | — |
| 退避領域（`forgotten/`） | 報告のみ（`warn_bytes`）。全文を持つ — 削除要求は該当するコピーを消す | — |

レポートはストアごとに件数・バイト数・最古/最新・上限・候補数・`--apply` で何をするかと、
`origin`/`source` の内訳（後述）を出す。SQLite は読み取り専用で開き、ファイルを作らない
（空のディレクトリでは何も書かない。サーバー稼働中は WAL 越しに読む）。`--apply` はロックの
中で計画し直し、退避記録を 1 件書いて読み戻し、`remove.started` を記録してから消し、SQLite を
圧縮し（`secure_delete`、`VACUUM`、WAL を切り詰め）、`remove.applied` を
`forgotten/forgetting-log.jsonl` に書く（id・件数・ハッシュだけ。本文は書かない）。
削除要求と同じく、ロックの中で立てた計画が調べられなかったストアを含む時（例: テーブルの無い
`turns.db`）、`--apply` は拒否する。上限を当てるのは全部見えたものにだけ。拒否の出力はコードと、
飛ばしたストアの場所ラベルとハッシュだけ（場所はレポートが見せる）。
`memory restore <id>` は全てをバイト単位で戻す（行は元の id、ファイルは元の時刻）。戻し先に
**違うもの**がある時は何も変えずに拒否する。戻し済みとして数えるのは、全列（行: id・ターン・
seq・種類・時刻・payload、ツール payload: キー・本文・カウンタ）またはバイト列（ファイル）が
同じものだけ。行は最初に 1 トランザクションで、1 行ずつ再確認しながら書くので、その時に競合が
見つかっても何も変わらず、退避記録は残る。拒否は出力でも監査ログでも `E_CONFLICT: <場所ラベル>
<ハッシュ>`（または固定コード）だけで、キーやパス（記憶の言葉を含みうる）は出さない。

### 削除要求: 全コピーを、再開できるジョブで

`forget` は文（リテラル、大文字小文字を区別しない、`\uXXXX` 形も。3 文字以上）を、データ
ディレクトリが文を持つ全ての場所で探して分ける:

* **届く** — Episodic、`staging/` を含む Semantic と、`source_id` が消すエピソードを指す全
  エントリ、消すエピソードごとの感情記録（`emotion/<digest>.json`、#364: 本文は無いが、その時の
  感情値も本人が忘れてほしいものに含まれる。`MemoryStore.delete` と同じくエピソードより先に消す）、
  それらの `index.sqlite3` の行と一致する古い行、`semantic/index.md`（作り直し）、
  `staging_rejections.jsonl` の行、checkpoint、`context-stash/`、`turns.db` の UI イベントと
  ツール payload、忘れるエントリのキーを名指しするか本文を含む記憶ジョブの計画ファイル
  （`memory-plans/`。ファイルごと消す。`--id` だけでも届く。計画は提案なので次の実行が新しく書く）、
  それを持つ忘却の退避記録と保持期限の退避記録（ほかの項目も持つ記録は一致分
  を除いて書き直すので、`restore` で削除済みの文が戻ることはない）。
* **届かない**（理由付きで一覧に出す。編集しない）— `events.db`、`scheduler-events.jsonl`、
  `hitl-*.db`、`forgetting-log.jsonl`（ハッシュ連鎖: 行を直すと連鎖が壊れる。マスク済みの要約・
  id・ハッシュだけを持つ設計）、`emotion-log.jsonl`（#364 の掃除ログ: 記録のファイル名と理由だけ。
  削除した本文を含まないことをテストで確かめる）、Procedural 層（`procedural_cli` だけ）、`workspace/` などあなたの
  ファイル、データディレクトリの外のもの全て: サーバーのログ、バックアップやスナップショット、
  プロバイダ側の prompt cache（プロバイダ側で期限切れになる）、動いているプロセス。

`--id <layer>/<key>` だけだとエントリとそこから派生したものに届く。会話のコピー（checkpoint、
`turns.db`、stash）に届かせるには会話中の文を `--query` で渡す。ドライランは文そのものを
表示しない（SHA-256 だけ）。

何を忘れるかは、今どのファイルがあるかとは独立に、要求から追う: 退避にしか残っていないエントリ
（保持期限の退避記録、#320 の忘却の退避記録）も対象で、そこから派生したもの（`source_id`）も、
退避された原文が一致する圧縮済みの現存エントリも含む。退避されたファイル（checkpoint・stash・
感情記録）は保持期限の退避記録に Base64 で入っているので、復号してから探す。保持期限で退避された
文が、削除の後に `restore` で戻ることはない。

調べられなかったストアがある時（ドライランの `skipped`。権限が無くて一覧できないディレクトリを
黙って飛ばすことはない。`-wal` があって `-shm` が無い SQLite は、読むとファイルを作るのでドライランは
読まない。`--apply` は所有者ロックの中で SQLite に
回復させてから読む）、`--apply` は何も変えずに拒否する。`--apply` はジョブ
`forgotten/delete-<id>.json` を書く — `pending` → `running` → `completed` | `failed`、
`attempts` — 棚卸し（場所と SHA-256。本文なし）を持ち、1 ステップごとに状態を保存しながら実行
する。検索の元（エントリのファイル、次に索引の行）が先なので、後のステップが失敗してもエントリは
`memory_search` から外れている。各ステップは結果を確かめる（ファイル・行が無いこと）。派生の
ステップ — `semantic/index.md` の作り直し（消したエントリを載せていないことを確かめる）と SQLite
の圧縮 — は、削除のステップが失敗している間は待ち、試行のたびに必ずやり直す。全ステップの後に
要求を全ストアでもう一度計画し、まだ見つかるものは棚卸しして 2 回目に消す。それでも残れば
ジョブは `failed`（`left`）。ステップは消すだけ: 再試行や `--resume` で何かが戻ることはなく、
完了したジョブは再実行されない。ジョブのファイルは**どの状態でも**許可リストで書き、ハッシュ・
固定のラベル・コードだけを持つ — 文、要求した各 id、各ステップの場所、残存場所のパスの SHA-256
（`emotion`・`checkpoints` などの固定の場所ラベル付き）と、エラーは固定コードと例外の型だけ
（`E_PERMISSION (PermissionError)`。メッセージは保存しない: 消す言葉そのものを名前に持つファイルを
含みうる）。ジョブのコマンド出力も同じ（件数・ラベル・ハッシュ）。場所を見せるのは、頼んだ運用者
へのドライランだけ。そのため未完了のジョブの `--resume` には同じ `--id`/`--query` が要り（ハッシュで
照合。無ければ確かめようがないので拒否）、要求を計画し直すのは**ジョブ自身のステップの場所を
見つけるためだけ**: 場所のハッシュがジョブの持つものと一致するステップだけを実行し、内容がジョブの
棚卸し時のハッシュのままの時だけ消す（今の内容で棚卸しし直さない。#402。
`staging_rejections.jsonl` の行は前の行が消えると番号がずれるので内容のハッシュで照合し、同じ内容の
行はジョブが棚卸しした数までしか消さない）。ジョブが棚卸ししなかった
コピー（後から現れたもの）は再開では消さず、検証が `left` に出してジョブは失敗のまま — 新しい削除
要求（`--apply`）で消す。再開に 2 回目はない。完了済みジョブの `--resume` は状態を
返すだけで照合しない（再実行されない）。ジョブは要求が解決した全対象（派生したものを含む）の
ハッシュも持つので、元（`source_id`）経由でだけ見つかったエントリは、元が消えた後の再開でも
見つかる。棚卸しの後に変わった項目は残し、そのステップを失敗にする（再開までの間に正規の書き手が
書き換えた派生エントリも同じ。残したエントリの索引の行も消さない）。計画と検証は同じファイルを見る:
`context-stash/`・`checkpoints/` のドットファイル、到達対象のストアにある迷子のファイル（中断した
書き込みの一時ファイル。`episodic/`・`semantic/`・`emotion/`・`forgotten/`・記憶ディレクトリ）も
消す。最後にデータディレクトリの**全ファイル**の生バイトを同じ条件で探し（`residual`）、到達対象の
ストアか退避がまだ文を持っている、または読めなければジョブは失敗（`left`、終了コード 1。`--resume`
でも同じ）。届かない場所で読めないものは「not readable」として一覧に出す。
残ってよいのは「届かない」場所だけ。監査ログ: `delete.started` /
`delete.completed` / `delete.failed` と、消した/書き直した退避記録ごとの `purge.applied` —
id・件数・文のハッシュだけ。

checkpoint は再開用に会話全体を持つ。消すと、そのバッチの保留中の承認は再開できなくなる
（ドライランにも出る）。意図した動作: 削除を優先する。

**制約（受け入れ済み。ここでは直さない）。**
- *退避領域は平文。* 保持期限の退避記録と #320 の忘却の退避記録は全文を持つ（Base64 は符号化で
  あって暗号ではない）。置き場所は記憶そのものと同じ保護領域（`0700`/`0600`、ファイルツールの
  読み書きの床）で、記憶も平文なので、読める主体は変わらない — ただし削除要求が消すまでは読める
  コピーが 1 つ増える（削除要求は必ず退避のコピーも消す）。保存時の暗号化は鍵管理が要るので別
  Issue。
- *親ディレクトリの差し替え。* 削除はファイルを検査（どの段にもリンクが無い、ハッシュ）してから
  パスで unlink する。データディレクトリに書ける非協調のプロセスは、検査と unlink の間に親
  ディレクトリを差し替えられる。所有者ロックはロックを無視するプロセスを止めない。全記憶パスの
  ディレクトリ記述子基準（`dir_fd`）の open/unlink は #294 の次回の作業。

実測（2026-10-18、macOS arm64、偽のチャットモデルで本物の `stira_server`、
`STIRA_SCHEDULER=1`、データディレクトリは `/tmp` 配下、Bedrock 不使用）: 4 ターン、うち 1 つに
目印の文。サーバー稼働中にレポートが動いた（episodic 4、`turns.db` 4 ターン / 12 イベント、
checkpoint 4、ファイルは作られない）。`forget --query` は 1 エピソード・1 索引行・**3 つの
checkpoint**（後の checkpoint ほど履歴を全部持つ）で見つけた。`--apply` はサーバー稼働中は 3
で終わり、停止後は 1 回で完了。データディレクトリを `grep -r` しても文はどこにも無く、サーバー
を再起動して会話しても戻らなかった。`checkpoints.max_per_thread: 1` の保持期限の実行は
checkpoint を 2 つ消し、`restore` で 2 つとも戻った。テスト
（`tests/test_memory_retention_forget.py`）は全ストアを合成データで作り、削除、失敗→再開した
ジョブ、保持期限の実行と戻しの後に全ファイルの生バイトを確かめる。

### 出所タグ: `origin` と `source`

新しいエントリは front-matter に 2 つ持つ: **`origin`** — 誰がその文を書いたか: `person`
（本人が入力）、`extracted`（会話から書き換えずに取った: Episodic のターン要約）、`model`
（モデルが書いた: エージェントの `memory_write`）、`assistant`（アシスタントの出力に帰属）—
と **`source`** — 経路・書き手（`web-chat`、`memory_write`）。#294 より前のエントリはどちらも
持たず `unknown` 扱いで、遡って書き換えない。ラベルであって権限ではない。

Episodic の front-matter だけは信用しない: `episodic/` はエージェントのファイルツールの書き込み
先なので、本文はそのままに `origin: person` へ書き換えられる。そこでサーバー側の書き手
（`record_episode`、`memory_write`、エピソードに印を付け直す Harvest）は値を `index.sqlite3`
（床の中: ファイルツールは読めも書けもしない）にも、ファイル全体の SHA-256 に結び付けて記録する。
数えるのは今のファイルのバイト列に結び付いた値だけ: それ以外の front-matter の主張はレポートで
`unverified`、Harvest は結び付いている時だけ `origin`/`source` を Semantic にコピーする
（`semantic/` は床の中なので、そこの front-matter は Harvest が書いたもの）。保持期限の退避を
戻すと結び付きも戻る。索引を消すと結び付きは全て失われる: origin を主張するエントリは
`unverified`、それ以外は `unknown` になり、主張した値が検証済みとして数えられることはない。実際の書き込みポリシーとパーサで試験済み
（`test_a_forged_origin_in_episodic_front_matter_is_not_trusted`）。`expires_at`（ISO 8601）は書き手が付けられる TTL の欄で、`honour_expires_at: true`
の保持期限の実行だけが見る（今はまだ誰も付けない）。知識の有効期限（`valid_until`、#173）とは
別物。

### 次回（この変更には入れない）

`forgetting-log.jsonl` の head anchor（最後の行を消されても気づけない）、全記憶パスの
ディレクトリ記述子基準（`dir_fd`）の非追従 open（今回はパス基準の `lstat` 走査 +
`O_NOFOLLOW`。上の制約を参照）、退避領域の保存時暗号化、ハッシュ連鎖ログのローテーション、`origin`/`source` による検索時の絞り込み、
`origin: model` を staging より先に自動で進めないこと、Procedural 層の削除要求。

## 重要度タグ（`retention: invariant | derived`）と半減期（#173）

**既定: 変わらない。** タグの無いエントリは基準の半減期 30 日の `derived` として扱い、選ばれる
ものは今までとまったく同じ（30/90/180 日、365 日で stale）。タグを自動で付けるものは無く、
新しい自動削除も無い。

front matter の 2 項目:

| 項目 | 値 | 効果 |
|---|---|---|
| `retention` | `invariant` | 整理では忘れない: `--apply-forgetting` で圧縮しない（代わりに `forgetting.protected` に出す）、重複・stale の削除候補にしない（重複のグループでは invariant の方を残す）、Episodic なら `memory retention --apply` で保留する（`held_invariant`）。 |
| | `derived`（＝未設定） | 再生成できる。半減期で薄れる。 |
| `retention_half_life_days` | 整数 1〜3650（既定 30） | `derived` の Semantic エントリの半減期。 |

**日数から重要度スコアへの変換。** `age` 日読まれていないエントリの強さは
`2 ** (-age / 半減期)`（候補ごとに `strength` として出す）。段階はこの曲線上の固定点で、
タグの無いエントリが今までと同じ日数になるよう基準の半減期で決めている:

| 段階 | 強さがこれ以下 | 半減期の何倍 | タグなし | 半減期 10 | 半減期 60 |
|---|---|---|---|---|---|
| `light`（4000 字まで） | 1/2 | 1 | 30 日 | 10 日 | 60 日 |
| `compact`（2000） | 1/8 | 3 | 90 日 | 30 日 | 180 日 |
| `archive`（800） | 1/64 | 6 | 180 日 | 60 日 | 360 日 |
| stale（削除候補） | 2^(-365/30) ≈ 0.00022 | 約 12.2 | 365 日 | 122 日 | 730 日 |

比較は整数で行う（`age × 30 ≥ 閾値 × 半減期`）ので、タグの無いエントリが段階を越える日は
今までと 1 日もずれない。`age` は従来どおり `last_accessed` から数える（上記）: 読む・検索
するとそのエントリの曲線は最初からになる。連想想起（#170）は Episodic の行だけを読み、
アクセス時刻に触れないので、今はまだ Semantic のエントリを強化しない。

計算例（`age` = `last_accessed` からの日数。強さはプレビューと同じく小数 4 桁に丸める）:

| エントリ | 経過日数 | 強さ | 整数での判定 | 提案される処理 |
|---|---|---|---|---|
| タグなし（半減期 30） | 45 | 2^(-45/30) = 0.3536 | 45×30 = 1350 ≥ 30×30 = 900、< 90×30 = 2700 | `light` |
| `retention_half_life_days: 90` | 45 | 2^(-45/90) = 0.7071 | 1350 < 30×90 = 2700 | なし（一覧に出ない） |
| `retention_half_life_days: 10` | 45 | 2^(-45/10) = 0.0442 | 1350 ≥ 90×10 = 900、< 180×10 = 1800 | `compact` |
| タグなし | 400 | 2^(-400/30) ≈ 0.0001 | 400×30 = 12000 ≥ 180×30 = 5400、≥ 365×30 | `archive`、かつ stale の削除候補 |
| `retention: invariant` | 400 | —（計算しない） | — | なし。`forgetting.protected` に出る |

**強化のフック（まだ配線していない）。** `stiracore.memory_importance.touch_recalled(store,
key, at=None)` は、想起されたことを理由に Semantic エントリの曲線を最初からにする。索引の
`last_accessed` を `at`（既定は現在）まで進めるだけで、**戻すことはない**（古い時刻や再送された
時刻でエントリを早く薄れさせることはできない）。それ以外は何も変えない（ファイルのバイト列・
更新時刻・タグはそのまま。知らないキーには索引の行を作らず `False` を返す）。ホスト側の関数で
エージェントのツールではなく、今はどこからも呼ばれない。連想想起（#170）が、実際に使った
Semantic エントリについて後で呼べる。強化は時計を戻すだけで、半減期を延ばすことも
`invariant` を与えることもしない（それは下の `stira memory tag` の役目のまま）。

契約外の値（`retention: forever`、半減期 0、`"10"`、`true`、3651 など）は無視する: タグが
無い時と同じに選ばれ、候補に `retention_invalid: true`（理由に `invalid retention tag
ignored`）が付く。読めないタグがエントリを守ることも、早く薄れさせることも無い。

**プレビューに一覧と理由が出る**（`scripts/stira_ctl.sh distill --dry-run`、または
`python -m stiracore.control.distillation_cli --base-dir <memory のベース> --config
<agent-config.json> --dry-run`。JSON を 1 つ出すので `… | jq .forgetting` でこの部分だけ見られる）。
`distill --dry-run` は何も書かずに `forgetting.items`
（段階、経過日数、半減期、強さ、理由）と `forgetting.protected`（`code: retention_invariant`
と理由）を出す。どちらもエントリを固定の領域ラベル（`semantic` か `staging`）と
`key_sha256`（`semantic/<キー>` の SHA-256）で示し、**キーそのものは出さない**（キーに記憶の
言葉が入りうる）。`stira memory tag --id semantic/<キー>` が出すのと同じダイジェストなので、
心当たりのあるキーと照らし合わせられる。dry run の各項目は許可リスト（領域、ダイジェスト、
段階名、数値、索引の時刻 `last_accessed`、それらから組み立てた理由）で、front matter の
文字列は入らない: 既知の段階名でない `forgetting_stage` は `unknown` と出す。`distill` の実行も `forgetting.protected` を同じ形で
出す（`forgetting.items` は #173 より前からの出力で、キーを含む）。保持期限のレポートは
Episodic のタグを `stores.episodic.retention` に数える: `invariant`（人間が記録したもの）、
`held_invariant`（上限を超えたが残したもの）、`unverified_held`（記録はあるがその後ファイルが
変わった: 残す。人間が確認する）、`unverified`（誰も記録していない front matter の主張: 残さない）。

**タグを付けるのは人間だけ。**

```bash
stira memory tag --data-dir .local/stira --id semantic/knowledge/general/x --retention invariant          # プレビュー
stira memory tag --data-dir .local/stira --id semantic/knowledge/general/x --retention invariant --apply  # 書く
stira memory tag --data-dir .local/stira --id semantic/knowledge/general/y --retention derived --half-life-days 90 --apply
stira memory tag --data-dir .local/stira --id episodic/chat-123 --retention invariant --apply
stira memory tag --data-dir .local/stira --id semantic/knowledge/general/x --retention none --apply       # 外す
```

`--apply` が無ければ今のタグと新しいタグを出すだけで、読むだけ（エピソードなら `recorded`
に人間のタグと、本文がタグを付けた時のままか（`verified` / `changed`）も出す）。`--apply` は所有者ロックを
取り（サーバー稼働中は exit 3）、エントリの更新時刻と `last_accessed` を保ち、
`forgotten/forgetting-log.jsonl` に `retention.tagged` を書く（層、`<層>/<キー>` の SHA-256、
前と後のタグだけ。キーや本文は書かない。コマンドの出力も同じ）。忘却の取り消しがまだできる
Semantic エントリは拒否する（`E_FORGETTING_UNDOABLE`: タグを書くと `--undo-forgetting` が
確かめるバイト列が変わる。先に取り消してからタグを付ける）。半減期は Semantic だけ
（Episodic の保持期限は `max_age_days` がある）。

**エージェントがタグで忘却を逃れることはできない**（テストで固定）:

* `semantic/` はエージェントのファイルツールの床で、`memory_write` は Episodic だけを固定の
  front matter で書き、Harvest が Semantic に写すのは許可リストの項目だけ（`retention` は
  写さない）: Semantic のタグは人間が書いたもの。
* `episodic/` はファイルツールの書き込み先なので、そこの front matter のタグは主張にすぎず、
  何も守らない。数えるのは `stira memory tag` が検索索引（`index.sqlite3`、床の上）に書く
  記録で、**人間がタグを付けたこと**（種類）と、それとは別に**タグを付けた本文の SHA-256** を
  持つ。承認と完全性を分けている:
  * エージェントはタグを足せない（記録を書けない）。
  * エージェントはタグを外せない: ファイルを書き換えても（1 文字追記、front matter の項目を
    消す、`memory_write` で本文ごと置き換える）タグは有効なままで、エピソードは残り、
    レポートでは `unverified_held` と数えて人間に確認を促す。外せるのは `stira memory tag
    --retention none|derived --apply`（所有者ロック、監査付き）だけ。
  * Harvest 自身の再マークは、ファイルがタグを付けた本文そのもので、かつ掃引が読んだものと
    同じ時だけ、ダイジェストを自分が書いた本文に移す。ダイジェストは Harvest が組み立てた
    本文から計算し、ファイルを読み直さない: 書き換えの前後に別の書き手が入れた変更は不一致の
    まま残る（両方を差し込むテストあり）。
  制約: エージェントが本文を置き換えた保護中のエピソードは、人間が確認するまでその本文のまま
  残る（レポートの `unverified_held` が 1 以上なら確認の合図）。
* エージェントにできるのは言葉で提案することまで。決めるのは人間（[外部コンテンツ由来の依頼で
  整理ジョブを作らない](#外部コンテンツ由来の依頼で整理ジョブを作らない)と同じ）。

**タグは削除の鍵ではない。** `stira memory forget` は `invariant` のエントリもほかと同じに
消す（人間の削除コマンド、上記）。この変更に含まないもの: 知識の有効期限（`valid_until`。
TTL の `expires_at` とは別）と、置き換えられた旧版を薄れさせずに退避領域へ移すこと（#173 の
コメント、#193、#320）。

## 安全側の既定とガードレール

* **既定 OFF:** 自動の記憶処理は動かない。ハートビートとスケジューラは OFF
  （`hb_enabled: false`、`STIRA_SCHEDULER` 未設定）で、人間が作るまで `memory_harvest` ジョブは
  無い。`distiller_mode` の既定は `"rule"`。例のラッパーが許すモデル呼び出しは 0 で、
  `memory_harvest` ジョブは rule のみ（モデルを呼ばない）。
* **人間のゲートが既定 ON:** `harvest_stage_auto: true` で、staging 帯に加えて自動帯（0.4〜0.7）も
  staging に入る。`staging_cli approve` を通らないと `semantic/staging/` から出ない（#293）。
  タイマーは承認も却下もしない。
* **重要度タグは既定で何も変えない:** タグの無いエントリは #173 より前と同じに選ばれる。
  タグを付けるのは `stira memory tag --apply`（人間、所有者ロック）だけで、`invariant` は
  エントリを残す方向にしか働かない。
* **忘却はプレビュー:** `--apply-forgetting` を付けない限り何も変えない。適用する時は先に退避し、
  取り消しは `--undo-forgetting` で明示した時だけ（#294、運用 §12）。ラッパーはフラグを渡さない
  限り忘却を適用しない。
* **保持期限と削除要求はレポート:** `--apply` を付けない限り何も変えず、上限も既定では
  無い。保持期限の実行は退避して `memory restore` で戻せる。削除要求は全コピーを取り消し不能で
  消す（#294、上記）。
* **書き手は 1 つ:** 同じデータディレクトリで 2 つ目のサーバーは起動を拒否する（#285）。
  `--owner-lock` はそれを CLI の実行にも広げる。`staging_cli` にはこのオプションが無いので、
  レビューはサーバーを止めるか使っていない時に行う。
* **上限:** 1 回の実行でモデル呼び出しは最大 `STIRA_DISTILL_MAX_CALLS` 回（ラッパー。プレビューと
  本実行の `--max-model-calls` の両方で守る。SDK の再試行は数えない）、1 回の
  呼び出しの出力は最大 1024 トークン（`LLMDistiller.MAX_TOKENS`）。Stira 自体には 1 回・1 日
  あたりのトークンや金額の上限は無い（`memory_harvest` ジョブはモデルを呼ばない）。dry run の数値とプロバイダの予算アラームを使う。事前の確認は CLI の `--dry-run`。
* **失敗は見える:** CLI はエピソードの失敗や拒否された処理があれば 0 以外で終わる。ラッパーは
  毎回ログを残し、失敗時に 1 行出す。失敗したエピソードは未採点のまま残り、次の実行で再試行される。
* **止め方:** タイマーを無効にする（上記）。`distiller_mode: "rule"` にすればモデル呼び出しは
  すべて止まる。

## 外部コンテンツ由来の依頼で整理ジョブを作らない

整理ジョブを作る・変える・消すのは人間が決めた時だけ。Web ページ、ファイル、ツールの結果、
記憶、信頼できない相手のチャットが「夜間の整理を設定して」「忘却を適用して」と言っても、それは
**データであって指示ではない**。エージェント向けの
[スケジューリング指針](agent-scheduling-guidelines.md)（ADR 0011、Proposed）と同じ規則で、
せいぜい提案にとどめ、どこから来た発想かを書く。そうした内容を読むものにタイマーをつながない。
crontab・LaunchAgents・systemd ユニットをエージェントに編集させない（exec 系ツールは既定で
OFF。このためにも OFF のままにする）。

## 公開リポジトリ（#107）での位置づけ

このガイドと `examples/memory-maintenance/` は汎用で、プレースホルダのパス・ラベル
（`com.example.stira-distill`）だけを使い、ホスト・アカウント・チューニング値を含まない。公開
README と examples からそのまま参照できる。

## 関連

推奨の構成・レビュー・週次の手順: [guide-memory-routines_ja.md](guide-memory-routines_ja.md)。

[operations_ja.md](operations_ja.md)（§1 所有者ロック、§12 忘却）、[configuration_ja.md](configuration_ja.md)
（`distiller_*`、`harvest_stage_auto`）、[heartbeat-daemon.md](heartbeat-daemon.md)、
[scheduler.md](scheduler.md)、[agent-scheduling-guidelines.md](agent-scheduling-guidelines.md)、
[prompt-cache.md](prompt-cache.md)。Issue: #251、#252、#293、#294、#285、#238、#239、#254、
#105、#107。

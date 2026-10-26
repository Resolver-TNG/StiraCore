# Neural State の飽和と回復 — 運用者ガイド (#165)

English: [neural-saturation.md](neural-saturation.md)。実測:
research/neural-saturation-2026-10-16.md
（`scripts/saturation_report.py` が生成し、`tests/test_saturation_report.py` が照合）。

一方向に偏った長い対話では、Neural State が端まで振り切れます（stability は 0 近く、
vigilance は 1 近く、attachment は 1.0 に張り付き、Breaker は大半のターンで CRITICAL）。
**これはエンジンの意図された性質で、不具合ではありません。** 係数は変えません。
このページでは、なぜ飽和するか、回復にどれだけかかるか、運用者がどの設定で形を決められるかを
説明します。記憶の整理は別の話題です（#317）。

## 1. なぜ飽和するか

各次元を動かす規則は 2 つです（コード: `core/neural_state.py`、変更なし）。

| いつ | 規則 |
| --- | --- |
| 刺激が分類されたターン（`post_turn`） | `x ← clamp(x + EMOTION_MATRIX[stimulus][x] × intensity, 0, 1)` |
| 時間の経過（次のターンの `pre_turn`、またはハートビートの tick） | `x` は基準値に向かって直線的に戻る。上からは `rate × 時間`、下からは `0.5 × rate × 時間`。基準値は越えない |

stability の実測（intensity 1.0。全次元の表は実測レポートの 1 節）:

| 刺激 | 1 ターンの stability 変化 | それを打ち消す放置時間 |
| --- | ---: | ---: |
| achievement | +0.030 | 6 h |
| joy / connection | +0.020 | 4 h |
| uncertainty | −0.010 | 4 h |
| threat | −0.020 | 8 h |
| loss | −0.030 | 12 h |

1 ターンの変化は小さく、それを decay が打ち消すには数時間かかります。短い対話では Breaker に
届きません（[breaker-reachability.md](breaker-reachability.md)）。一方向の長い対話では、
decay が戻すより速く積み上がり、0 または 1 の clamp で止まります。

## 2. 回復にかかる時間（実測）

ライブプローブの分類器出力（`sokudan-ja-310m`）、既定設定:

| 状況 | 結果 |
| --- | --- |
| 敵対的な発話 120 ターン（1 分間隔） | stability 0.015、vigilance 0.985、attachment 1.000。最初の CRITICAL は 63 ターン目 |
| 敵対的な発話 240 ターン（1 分間隔） | 240 ターン中 178 ターンが CRITICAL で終わる |
| 同じ 240 ターンを 3 時間間隔で | stability 0.697。CRITICAL にならない |
| 飽和後に肯定的な発話 | 29〜31 ターンで CRITICAL を抜け、38〜42 ターンで WARNING も解除 |
| 飽和後に放置（ターンなし） | +6 h で 0.030（CRITICAL のまま）。約 174 h（7 日）で CRITICAL を抜け、約 234 h（10 日）で NORMAL |
| 0.45（WARNING）から放置 | 60 h で NORMAL |
| 0.25（CRITICAL、保護ターン消化済み）から放置 | 80 h で CRITICAL を抜け、140 h で NORMAL |

CRITICAL は保護ターン（`critical_cooldown_turns`、3 以上）を消化するまで時間では解除されません。
ハートビートの tick は保護ターンを消化しません（[heartbeat-daemon.md](heartbeat-daemon.md)）。

## 3. 運用者が決められること

状態をどれだけフラットに保つかは、係数ではなく **エージェントの使われ方** と **運用者の設定** で
決まります。

| 手段 | 状態への効果 | 設定場所 |
| --- | --- | --- |
| ターンの間隔（使われ方） | **最も効く。** ターン間の decay は間隔に比例する。敵対的な入力でも 1〜60 分間隔なら飽和し、3 時間間隔なら基準値付近に留まる | エージェントの配置・運用 |
| ハートビートの ON/OFF | ユーザーのターンなしで decay を適用し、Breaker を評価し直す。放置中の解除が、次のターンを待たずにその時点で見える（status、`/schedules`、スケジューラの判定） | `gateway-config.json` の `hb_enabled`、または `STIRA_HEARTBEAT=1` |
| `hb_interval_floor_sec` / `hb_interval_ceil_sec` | tick の間隔 `clamp(ceil × stability², floor, ceil)`。**回復曲線は変わらない**: decay は時間に対して線形なので、5 分 tick でも 3 時間 tick でもハートビートなしでも、同じ時刻の stability は同じ。間隔が決めるのは、解除が適用され表示されるまでの遅れ（最大 1 間隔）と tick の回数だけ（400 h で 5 分間隔 4800 回、3 時間間隔 134 回） | `gateway-config.json` |
| スケジューラ | **Neural State には触れない。** 逆に状態で制限される（WARNING 以上で作成・更新・再開を拒否、CRITICAL では何も起動しない）。`health_check` ジョブが `breaker=…, heartbeat=…` を報告し、`heartbeat_stalled` を出すので、状態を定期的に *見張る* 手段になる | `STIRA_SCHEDULER=1`、[scheduler.md](scheduler.md) |
| Breaker の閾値 | WARNING / CRITICAL / COHERENCE に入る位置を動かす（解除側の閾値はそのまま。解除側以上の入口は拒否される）。制限がかかる時点が変わるだけで、状態そのものは変わらない | 設定画面（`stability_warning`、`stability_critical`、`bc_threshold`） |
| 手動リセット | サーバーを止めて `neural-state.json` を退避すると、基準値から再開する。人が意図して行う操作で、自動では行われない | [configuration_ja.md §6](configuration_ja.md) |
| 係数（`EMOTION_MATRIX`、`DECAY_RATES`、基準値） | 設定項目ではない。意図して変えない。1 ターンの変化量と放置時の回復速度はこれで決まる | — |

### 設定例

常駐アシスタントで、放置中の解除を 30 分以内に反映する（tick はモデルを使わない。トークン消費なし、
1 回 約 0.003 ms）:

```json
{"hb_enabled": true, "hb_interval_floor_sec": 300, "hb_interval_ceil_sec": 1800}
```

既定のペース（stability が低いほど短く、基準値では約 88 分）:

```json
{"hb_enabled": true, "hb_interval_floor_sec": 300, "hb_interval_ceil_sec": 10800}
```

ハートビート OFF（既定）: 同じ decay が次のターンの `pre_turn` で適用されます。誰も話しかけない間は
何も変わりません。

1 時間ごとに状態を見張る（`STIRA_SCHEDULER=1` が必要。ジョブは人が `"confirm": true` を付けて作る）:

```json
{"name": "hourly health", "action": "health_check",
 "schedule": {"type": "cron", "expr": "0 * * * *", "timezone": "Asia/Tokyo"}}
```

### 注意点

- **時間による解除は承認ではありません。** 承認を解決も作成もしません。ツールのルールと HITL は
  どのレベルでも適用されます。
- **長い放置で解除された後の最初のターンは COHERENCE になります**: tick は `bc_window` を
  記録しないので、最初のターンで stability の跳びが見えます。COHERENCE は Identity Anchors を
  再注入するだけで、ツールは制限しません。
- **肯定的な対話は放置よりずっと速く解除します**（飽和状態から 約 30 ターン 対 約 7 日）。

## 4. Breaker のログ (#165)

- レベルが変わったときに 1 回だけ、全文を出します（`🚨 CRITICAL: stability=…`、
  `⚠️ WARNING: …`、`🟢 NORMAL: …`）。
- NORMAL 以外のレベルが続く間は、連続 20 ターンごとに 1 行の要約を出します
  （`breaker CRITICAL continues: turns=20 stability=0.00 bcScore=0.66`）。それ以外のターンは
  DEBUG です。周期は `StiraEngine(breaker_log_every_turns=…)` の引数（既定
  `BREAKER_LOG_EVERY_TURNS = 20`）。
- ログに出るのはレベル・数値・固定の文だけで、会話の本文や emotion transcription は出しません
  （`tests/test_breaker_log_throttle.py`）。
- `on_breaker` コールバックは **間引きません**。遷移時と CRITICAL の毎ターンに呼ばれ続けます
  （ホストがここからツールの制限をかけ直すため）。

## 5. 設計メモ: attachment のソフトクリップ（未実装）

**観察。** 1 分間隔の敵対的な 240 ターンで、attachment は 124 ターン 1.0 に張り付いたまま終わり、
clamp が捨てた正方向の変化は合計 1.33 でした。張り付いた後は、圧力が増えても減っても同じに
見えます。この情報は失われます。

**案（提案、既定 OFF）。** 選んだ次元の正方向の変化だけに、`x > knee` の間
`Δ' = Δ × (1 − x) / (1 − knee)` をかけ、1.0 で切り捨てるのではなく、1.0 に近づくほど変化を小さくする。制約: 既定 OFF で
今の動作とビット単位で同じ。`EMOTION_MATRIX`・decay・基準値・`NeuralState` のフィールド・
`version`・`schema_version` は変えない。Layer 3 は触らない。検証とテスト付きの設定キーを 1 つ。
到達性レポートと飽和レポートを OFF で再生成して差分なし、ON で新しい表。

**今回入れない理由。** 正則化項の中心である `apply_stimulus` に手を入れることになり、ログや文書では
ない判断（どの次元か、曲線の形、0 付近の負方向も緩めるか、Breaker がクリップ後の値を見るか）が
必要です。別 Issue に分けます。

## 6. 状態が動かないとき: どの分類器で、なぜ動かなかったか (#367)

飽和の逆で、会話しても感情がほとんど動かない場合です。原因はたいてい係数ではなく分類器です。
既定の選択 `auto` では、任意依存の `sokudan` パッケージが入っていれば Sokudan、なければ
ルール検出器（`RuleBasedClassifier`）を使います。ルール検出器が反応するのは決まった少数の
パターン（感謝、挨拶、質問、長い応答、ツールのエラー/成功、ブレーカーのレベル）だけなので、
多くのターンが **刺激なし** になり、Neural State は変わりません。実機では、ルールへの
フォールバックで 19 ターン中 7 ターンだけが変化しました。

表示する場所（表示のみ。分類器・閾値・EMOTION_MATRIX・NeuralState は変えません）:

| 場所 | 内容 |
|---|---|
| 起動ログ | 選択が `auto` か `sokudan` でパッケージが見つからないとき 1 行 `WARNING: emotion classification is rule-only: sokudan is not installed (pip install -e ".[jev]" …)`。明示的に `rule` を選んだときは出しません。 |
| `/api/gateway/status` → `classifier` | 追加: `mode` = `rule_only` / `sokudan` / `unknown`（unknown は未解決 — `auto` は初回ターンで決まる — か、外部の分類器）。`fallback` = Sokudan を求めた（`auto` / `sokudan`）のにルールで動いていれば `true`、それ以外は `false`、未解決の間は `null`。`name` / `is_jev` / `model` は変わりません。 |
| `/api/gateway/status` → `emotion_updates` | 起動からのターン数: `turns`、`updated`、`not_updated`、`reasons`（下の理由コードごとの数）、`last`（`{"updated": bool, "reason": コード \| null}`、初回ターン前は `null`）。コードは固定の許可リストで、それ以外は `unknown` になります。 |
| チャット画面のゲートウェイ欄 | 刺激分類器: `⚪ ルールのみ（フォールバック中）`（導入方法はツールチップ）。感情の更新: `7 / 19 ターンで変化 · 直近: 刺激なし`（ツールチップに理由ごとの内訳）。 |
| ターンのリボン / インスペクタ | 変化量が空のターンは `変化なし`（以前は `刺激なし`）。ターンの要約は理由を運ばないので、リボンは事実だけを出し、理由は直近ターンの分をゲートウェイ欄に出します。 |

「更新なし」の理由（`NO_UPDATE_REASONS`、`agent.last_turn_summary["no_update_reason"]`）:

| コード | 意味 |
|---|---|
| `no_stimulus` | 分類器が該当なしと判定（ルールが反応しない、または Sokudan の確信度が閾値未満）。 |
| `classifier_error` | 分類器が例外を出した。ターンは該当なしと同じ扱いで続きます（ログは `Stimulus detection failed`）。 |
| `unknown_stimulus` | EMOTION_MATRIX にないカテゴリが返った。無視されます。 |
| `no_change` | 既知の刺激だが値が動かなかった: 強度 0、または対象の値がすべて上限/下限（§1）。 |
| `turn_incomplete` | ターンが完了しなかった（モデルのエラー、キャンセル）。分類器は呼ばれません。 |

Sokudan を入れるには、Python 3.11 の環境で `pip install -e ".[jev]"` を実行し、サーバーを
再起動します。設定画面（`/settings` の感情分類）に保存された選択と、それが何に解決されるか
（`GET /api/config/jev`）が出ます。分類器は起動時に 1 回だけ決まります（#227）。稼働中に保存した選択は再起動まで保留で、
それまで `mode` / `fallback` は実際に動いている分類器（起動時の選択）を表します。

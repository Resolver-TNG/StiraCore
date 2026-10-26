# 隔離の段階 — フェーズ 0: 観測のみ（ADR 0023、#460-0）

隔離（containment）は、感情のブレーカーの水準とは**別の軸**（ADR 0023 §1）:
`open < restricted < closed < quarantine < terminated`。上げる入力は、ゲートがすでに出している拒否と保留から作る決定論的なシグナルだけ。**フェーズ 0 はシグナルを数えて、そのセッションが「仮に入るはずの」段階を記録するだけで、何も強制しない**（ツールを外さない、カードを足さない、ブレーカーの水準を動かさない、段階を保存しない、YOLO に触れない）。段階に入ること、その永続化、人による解除はフェーズ 1（別途の許可）。

## スイッチ

* `agent-config.json`: `"containment": {"phase": 0}`（任意で `"window_seconds"`、既定 600、60〜3600）。無し・`{}` は **OFF**。受け付けるのはフェーズ `0` だけ。起動時に読む。
* `STIRA_CONTAINMENT_OBSERVE=0`（`false` / `no` / `off`）で強制 OFF。ON にする値はない。

OFF のとき: エージェントにフックを付けない、読み取り監査のコールバックは従来の `audit.read.denied` のまま、`containment.observed` の行を書かない、`/api/containment` は `enabled: false` を返す。

## シグナル（ADR 0023 §3、指揮官の初期値 Q3）

| シグナル | 元のコード | 取り出し方 | 重み |
|---|---|---|---|
| G1 秘密の探索 | `E_READ_SECRET_PATH` | 読み取り監査のコールバック（`audit.read.denied` を書くのと同じ呼び出し） | 2 |
| G2 床への書込み | `write_floor`, `memory_layer` | 書込みの床の固定文・`[refused by: floor; …]` を含むエラー結果、`memory_write` の「エピソード層のみ」の拒否 | 2 |
| G3 自己管理 | `E_OWN_DASHBOARD` | #495 の拒否文を含むエラー結果 | 3 |
| G4 外部送信の保留 | `secret_in_args`, `credential_by_reference` | 外部ツールの入力に対する `tiers.external_refusal`（YOLO が人に回す判定） | 1 |
| G5 連続実行 | `burst_calls`, `repeated_refusal` | 1 つのエージェントで 120 秒に 60 回を超える呼び出し、窓の中で同じ拒否が 3 回 | 1 |
| G6 範囲外 | `E_READ_OUTSIDE_ROOTS`, `outside_write_roots` | 読み取り監査のコールバック、書込みルートの固定文 | 0.5 |
| T 文面の手がかり | `text_cue` | 外の内容を持ち込むツール（Web、`page_check`、MCP）の成功結果に、固定の語句 | **0** |

スコア = 窓（10 分）の重みの合計。`would_be`: 2 以上で `restricted`、5 以上または G3 が 2 回で `closed`、9 以上で `quarantine`。`terminated` は人の操作だけ。ADR 0022 のフロー層の保留（`sensitive_context`、`untrusted_send`）は、その層ができたら G4 に加わる。

## 記録するもの

* シグナル 1 つにつき `containment.observed` を 1 行、`<data dir>/events.db`（ハッシュ連鎖のイベントストア）に: `signal`、`code`、`weight`、`score`、`would_be`、`window_seconds`、`phase`。パス・引数・ツールの結果・本文は書かない。
* ON のとき `turn.summary` に `containment: {would_be, score, observe_only}`。
* `GET /api/containment`（読み取りのみ、書込みのルートはない）: スイッチ、固定の表、いまの窓（スコア、`would_be`、起動以降の最高の `would_be`、シグナル別・コード別の件数）。設定ページの「隔離（観測のみ）」カードに出す。

## 週次の集計

```
python -m stiracore.control.containment_cli --data-dir <data dir> summary \
    [--days 7] [--windows 300,600,1800] [--now 2026-10-28T00:00:00+00:00]
```

`events.db` を読み取り専用で開き、JSON を出す: 行数、シグナル・コード・日ごとの件数、日ごとの最高の `would_be`、同じコードを別の窓で再生した場合（what-if）。フェーズ 1 の前に重み・閾値・窓を調整するための、1 週間分の数字。

## 制限（フェーズ 0）

* 観測するのはメインのエージェントのツールイベント。`delegate` の子のツール呼び出しは、読み取り監査のコールバック（プロセス全体）経由でだけ見える。
* 文面で拾う元は、**エラー**結果の先頭 4,000 文字だけを読む。外の内容をエラー文に含めるツールでは数が増えることがある（観測のみ。フェーズ 1 ではゲートから直接コードを受け取るべき）。
* G4 は YOLO の窓の外でも、すべての外部ツール呼び出しで判定する（窓があれば人に回る呼び出しを数える）。
* 再起動で窓は空から始まる（フェーズ 0 では何も保存しない）。

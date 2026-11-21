# 失敗ダイジェスト

[English](failure-digest.md) | 日本語

どの失敗が何回起きたか、どこを見ればよいかを出します。1 つのデータディレクトリに記録された失敗を
決定的に数え、代表イベントの ID を付けます（#695）。モデルは呼びません。運用者が実行したときだけ
動き、データディレクトリには何も書き込みません（記憶にもプロンプトにも入れません）。

```bash
python -m stiracore.control.failure_digest_cli --data-dir <データディレクトリ> \
    [--days 7] [--now 2026-11-10T00:00:00+00:00] [--format md|json] [--top 3] [--examples 3]
```

標準出力に Markdown（または JSON）を出します。終了コード: 0 完了、1 データディレクトリまたは
2 つの読み出し元のどちらかが読めない（読めた側の集計は注記つきで出します）、2 使い方の誤り。

## 数えるもの

| 読み出し元 | 行 | パターン |
|---|---|---|
| `turns.db`（一時ディレクトリへのコピーを読む） | `status` が `error` か `denied` の `tool_call.updated` | `tool:<status>:<error_class>[:<deny_code>]` |
| `authority-lifecycle*.jsonl`（ローテーションせずに読む） | グラント・目標・反復・ハートビート起動のイベントのうち、`reason_code` が失敗のもの | `grant:<subject_kind>:<reason_code>` |

* `error_class` / `deny_code` は #696 の固定集合です（`unknown_tool`、`invalid_input`、`refused`、
  `tool_failed`、`exception` / `rule_never`、`declared`、`other`）。この欄が無い行（#696 より前の
  記録）と、集合の外の値は `unclassified` として数えます。
* エージェントが自分で宣言したチェックポイント（`status: denied` かつ `deny_code: declared`）は
  失敗に数えません。進捗の宣言であり、ゲートが拒否したものではないためです。除外するのはこの組
  だけで、件数はヘッダに出します（"Excluded (not failures)"）。`declared` の付いたそれ以外の行
  （`error`）は失敗として数えます。
* グラントの失敗コード: 反復の終了コードから、成功（`iteration_done`、`iteration_claimed`）と
  中断（`iteration_yielded_user_turn`、`iteration_shutdown`、`iteration_stopped`、
  `iteration_permit_stale`）を除いたもの。これに `blocked_*`、`halted_*`、`tick_skipped_*` を
  加えます。

## 出力

失敗の件数、上位 `--top` パターンの合計（件数と、全失敗に占める割合）、パターンごとの件数・割合・
scope・最初と最後の時刻・代表イベント（新しい順に最大 `--examples` 件）。代表イベントは、ツール呼び出しでは
`event_id` と `turn_id`、グラントでは `event_id` と `subject_id`（グラント ID。グラントの反復には
ターンがありません）です。

**出すのはコード・件数・時刻・ID だけです。** `turns.db` にはマスク済みのプレビューやツール名も
入っているため、読み出した時点で許可リスト（`failure_digest.TOOL_FIELDS`）の欄だけに絞ります。
ツール名も読みません（未登録のツールの場合、モデルが選んだ文字列だからです）。

### scope（`invariant` / `domain` / `unknown`）

各パターンに scope を 1 つ付けます。コード内の固定の対応表（`failure_digest.SCOPE_BY_ERROR_CLASS`、
`SCOPE_BY_DENY_CODE`、`SCOPE_BY_REASON_CODE`）から引くだけで、モデルは何も読みません（#730）。

| scope | 意味 | コード |
|---|---|---|
| `invariant` | タスクの分野によらない、手続きの欠陥 | `unknown_tool`、`invalid_input`、`rule_never` の拒否、`iteration_tool_refused`、`iteration_no_action`、`blocked_loop_detected` |
| `domain` | 呼び出しや手順の形は正しく、タスクの対象そのものが原因で止まった | `tool_failed`、`blocked_needs_decision`、`blocked_out_of_scope`、`blocked_missing_input` |
| `unknown` | コードからはどちらか決められない、またはどちらでもない（ハーネス自身の状態） | 上記以外のすべてのコード、`unclassified`、対応表に無い値 |

拒否（`refused`、または分類の無い行）は `deny_code` で決め、それ以外のツールの行は `error_class` で
決めます。対応表に載せずにコードを追加すると、テストが落ちます。

**これは翻案であり、効果は未検証です。** この区分は Harness2（arXiv 2610.14224）の診断にならった
ものです。論文はモデルを使って、手続きの欠陥と分野知識の不足を分けています。ここでは、その目的で
設計されていないコードに静的な対応表を当てているだけなので、1 つのコードに両方が混ざることが
あります（`tool_failed` は `domain` に数えますが、外部サービスの停止も含みます）。この列が運用の
役に立つかどうかは測っていません。論文の効果量は、Stira での効果を示す根拠にはなりません。

## 制約

* **データディレクトリには何も書き込みません。** `turns.db`（と、あれば `-wal`）を専用の一時
  ディレクトリへコピーし、コピーを読み出し専用で開いて、終わったら消します。コピーにはその間だけ
  マスク済みのプレビューが入ります。ロックを取らないスナップショットなので、コピー中にチェック
  ポイントが走って壊れたコピーは `turns_db_unreadable` として報告します（もう一度実行してください）。
* **件数は下限値です。** 記録できなかったターンイベントは捨てられます。その欠落カウンタ
  （`TurnEventRecorder.drop_counts`）は動作中のサーバープロセスの中にあり、このコマンドからは
  読めません。理由は `no_active_turn`、`invalid_event`、`record_failed`、`no_call_id`、
  `sink_failed`（ツールやモデルのイベントの変換が例外を出した）、`payload_failed`（ツールの
  ペイロードを保存できなかった。イベント自体は書かれます）です。
  ローテーションで消えたライフサイクルログの区間は数えられません。`tick_skipped_*` は
  プロセスごとにグラントと理由の組につき 1 回だけ書かれます。
* ターンイベントのシンクは、ツールの状態 `pending` と `timeout` を書きません。リトライで終わった
  ツール呼び出しは `running` のまま残ります。最後の状態が `running` の呼び出しは未完として扱い、
  失敗には数えません。
* 1 つの出来事が複数のパターンに現れることがあります（反復の終了と、その目標のブロック）。
* 純関数は `stiracore.observability.failure_digest.build` です。時計もストアも読まないので、
  欠落カウンタを持っている呼び出し側はそれを渡せます（`drop_counts=`）。

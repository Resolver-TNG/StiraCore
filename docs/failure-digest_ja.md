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
最初と最後の時刻・代表イベント（新しい順に最大 `--examples` 件）。代表イベントは、ツール呼び出しでは
`event_id` と `turn_id`、グラントでは `event_id` と `subject_id`（グラント ID。グラントの反復には
ターンがありません）です。

**出すのはコード・件数・時刻・ID だけです。** `turns.db` にはマスク済みのプレビューやツール名も
入っているため、読み出した時点で許可リスト（`failure_digest.TOOL_FIELDS`）の欄だけに絞ります。
ツール名も読みません（未登録のツールの場合、モデルが選んだ文字列だからです）。

## 制約

* **データディレクトリには何も書き込みません。** `turns.db`（と、あれば `-wal`）を専用の一時
  ディレクトリへコピーし、コピーを読み出し専用で開いて、終わったら消します。コピーにはその間だけ
  マスク済みのプレビューが入ります。ロックを取らないスナップショットなので、コピー中にチェック
  ポイントが走って壊れたコピーは `turns_db_unreadable` として報告します（もう一度実行してください）。
* **件数は下限値です。** 記録できなかったターンイベントは捨てられます。その欠落カウンタ
  （`TurnEventRecorder.drop_counts`）は動作中のサーバープロセスの中にあり、このコマンドからは
  読めません。ローテーションで消えたライフサイクルログの区間は数えられません。`tick_skipped_*` は
  プロセスごとにグラントと理由の組につき 1 回だけ書かれます。
* 1 つの出来事が複数のパターンに現れることがあります（反復の終了と、その目標のブロック）。
* 純関数は `stiracore.observability.failure_digest.build` です。時計もストアも読まないので、
  欠落カウンタを持っている呼び出し側はそれを渡せます（`drop_counts=`）。

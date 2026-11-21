# ATIF エクスポート（1 ターン、コードのみ）

[English](atif-export.md) | 日本語

記録済みの 1 ターンを ATIF（Agent Trajectory Interchange Format）の JSON ファイルに書き出します。
エージェントの軌跡を読む外部ツールをオフラインで試すためのものです（#700）。片方向で、読み戻しは
しません。内部のターンイベントの形式は変えず、対応付けは 1 つのアダプタ
（`stiracore.observability.atif_export`）に閉じています。

**運用者が実行したときだけ動きます。** 有効にする設定や環境変数はなく、エージェントのツールからは
呼べず、サーバーも呼びません。モデルを呼ばず、ネットワークに何も送らず、データディレクトリには
何も書き込みません。

```bash
python -m stiracore.control.atif_export_cli --data-dir <データディレクトリ> --out <新しいファイル> \
    [--turn-id <ターン ID>] [--include-untrusted]
```

* `--out` はまだ存在しないパスで、データディレクトリの外である必要があります（モード 0600 で
  作成。上書きしません）。
* `--turn-id` を省くと、終了済みの最後のターンを対象にします。
* 終了コード: 0 書き出した、1 書き出さなかった（理由コードを標準エラーに出します）、2 使い方の誤り。

## ファイルの中身

**ID・数値・コードだけです。本文は入りません。** ATIF はプロンプト、応答、ツールの引数と結果を
想定した形式です。Stira のターンイベントはプロンプトと応答を持たず、引数と結果はマスク済みの
プレビューだけを持ちますが、このエクスポートはそのどれも読みません。

| ATIF の欄 | 値 |
|---|---|
| `schema_version` | `ATIF-v1.4`（「限界」を参照） |
| `session_id` | ターン ID（ターンイベントにセッション ID が無いため） |
| `agent` | `name: stiracore`、パッケージのバージョン、`turn.completed` のモデル ID |
| `steps[].source` | `turn.started` は `user`（自動継続のターンは `system`）、`model.completed` ごとに `agent` |
| `steps[].message` | 常に `""` |
| `steps[].metrics` | モデル呼び出しの `prompt_tokens` / `completion_tokens` |
| `steps[].tool_calls[]` | `tool_call_id`、`function_name`、`arguments: {}`（常に空） |
| `steps[].observation.results[]` | `source_call_id` と、`content` に呼び出しの**状態コード**（`done`、`error`、`denied` など）。出力ではありません |
| `final_metrics` | `turn.completed` のトークン合計、`total_steps` |
| `extra.stira`（最上位・各ステップ・`final_metrics`） | `prompt_chars`、`image_count`、モデル呼び出しの ID・所要時間・停止コード・状態、ツール呼び出しごとの `status` / `error_class` / `deny_code` / `duration_ms`、承認リクエストの ID と決定コード、ブレーカーの遷移、ターンの停止コードと理由コード、`context_trust`、対応付けなかったイベントと読み飛ばしたイベントの件数 |

すべて許可リスト方式です（`atif_export.build`）。値を取るのは、数値、固定のコード集合の要素、
ハーネスが書いた 1 トークンの文字列のいずれかの場合だけです。1 トークンでない（または
`mask_secrets` が書き換える）ツール呼び出し ID は `call-<n>` に置き換えます。識別子の形でない
ツール名は `unnamed_tool` に、存在しないツール（`error_class: unknown_tool`）の名前は
`unknown_tool` に置き換えます（モデルが選んだ文字列だからです）。結果のすべての文字列は、最後に
もう一度 `mask_secrets` を通します。

## 未信頼のターン

信頼ラベルの観測（ADR 0022 フェーズ 0、[taint_ja.md](taint_ja.md)）がそのターンの文脈を
`untrusted` と記録している場合、`--include-untrusted` を付けない限り書き出しません
（`turn_untrusted`）。付けた場合も出力はコードのみです。信頼ラベルの観測は既定で OFF のため、
多くのターンにはラベルがありません。その場合は `context_trust: unknown` として書き出します。

## 理由コード（終了コード 1）

`data_dir_missing`、`turns_db_missing`、`turns_db_unreadable`、`turn_not_found`、`out_exists`、
`out_inside_data_dir`、`out_unwritable`。アダプタ側: `no_events`、`turn_id_mismatch`、
`turn_id_unsafe`、`turn_not_completed`、`turn_untrusted`。

## 限界

* **ATIF の形式は未検証です。** 欄の名前は、仕様を参照できない状態で書いた最小限のものです。
  ATIF の検証器や外部ツールには通していません。メッセージ本文を必須とする読み手には、空文字列が
  渡ります。
* **本文つきの書き出しはありません。** #700 は人の明示的な操作に限って提案していますが、前提と
  なる「学習・外部提供用エクスポートの統制」の設計判断記録がまだありません。実装する際は、信頼
  ラベルの無いターンを未信頼として扱う必要があります。
* **グラントの反復は書き出しません。** 反復はターンイベントではなくライフサイクルログに記録され、
  反復とターンを結ぶ ID がありません。
* `turns.db` は一時ディレクトリへのコピー（終了時に削除）を読みます。ロックなしのスナップショット
  なので、コピーが壊れていた場合は `turns_db_unreadable` になります。
* 記録できずに捨てられたターンイベント（`TurnEventRecorder.drop_counts`）は、ファイルにも
  ありません。

## 軌跡にするうえでターンイベントに欠けている項目（#277 への入力）

1 ターンを手で対応付けて分かったものです。

| 欠けている項目 | エクスポートへの影響 |
|---|---|
| セッション（会話）ID | `session_id` はターン ID。同じ会話のターンをつなげられない |
| 主体（ターンを始めたのが人・スケジューラ・グラントのどれか） | 分かるのは `auto_continue` だけ。それ以外のターンはすべて `user` になる |
| ターンとグラントの反復・ライフサイクルイベントを結ぶ相関 ID | グラントの反復を軌跡に置けない |
| ツール呼び出しと、それを要求したモデル呼び出しの対応 | 順序をもとに、直前の `model.completed` に付けている |
| 承認とツール呼び出しの対応 | 承認はステップではなく最上位に並べている |
| サブエージェント（委任）の軌跡への参照 | 書き出さない |
| モデル呼び出しごとのキャッシュトークン数と費用 | `metrics` はトークン数 2 つだけ |

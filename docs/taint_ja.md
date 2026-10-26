# 信頼ラベルと taint — フェーズ 0（観測のみ）

ADR 0022（[adr/0022-trust-labels-and-taint.md](adr/0022-trust-labels-and-taint.md)）、#458。
フェーズ 0 は**観測だけ**です。プロンプト・ツール結果・ゲートの判定・プロンプトキャッシュのバイト列は変わりません。

## スイッチ

* `agent-config.json` の `"taint": {"phase": 0}` で ON。無し / `{}` は OFF。
* `STIRA_TAINT_OBSERVE=0`（`false` / `no` / `off`）で強制 OFF。ON にする値はありません。
* 起動時に読みます。

## 何を算出するか

* 入力ごとの**出どころ**（`stiracore/labels.py` の固定表。ツール名、ターンの起点、プロンプト内の想起記憶ブロック）。文面からラベルは変えられません。表に無いツールは `external`。
* ターンごとの**文脈ラベル**: 会話全体の和集合。出どころが全て `system` / `operator` / `model` の時だけ `trusted`。ツール結果に `mask_secrets` が反応する値がある（S1）か、認証付きの呼び出しが返った（S2）時に `sensitive`。
* **`untrusted_recall_only`**（ADR 0022 Q3）: 想起記憶だけが理由で untrusted になった文脈（フェーズ 0 では記憶に信頼記録が無い）。
* **`flow_would_hold`**: 外部送信の呼び出し（外部ツール・MCP・未分類）ごとに、ADR 0022 §6 の flow 層が付けていたコード。宛先リストはまだ無いので、どれも未登録扱い。何も保留しません。

## どこで見るか

* ターンごとに `taint.observed` イベント 1 行（コードと件数のみ。本文・引数・URL・ホストは入らない）。
* `turn.summary` に `taint`（同じ項目）。
* `GET /api/taint`（読み取り専用）。
* 週次集計: `python -m stiracore.control.taint_cli --data-dir DIR summary [--days 7]`。

## フェーズ 0 の制約

* ボットと Webhook のターンはどちらも `external`（どちらにしても untrusted）。
* 圧縮の要約と退避の読み戻しは、まだラベルを引き継がない（フェーズ 1）。

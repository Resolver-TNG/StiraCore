# JP stimulus mini-set v1（#164 / #143 stage 2 の下準備）

日本語の合成発話 140 件と、2つのモデルが独立・盲検で付けた**下書きラベル**、現行分類器の測定結果。
事前登録: `docs/eval/emotion-ablation-prereg.md` §13（ラベル付けの前にコミット）。全数値: `report.md` / `results.json`。

> **人間の盲検ラベルではない。** ラベラーは `global.openai.gpt-6-astra` と `global.anthropic.claude-opus-5-5` の2モデル。
> #143 stage 2 の「盲検ラベル（2名以上）」の条件は満たさない。人間による置き換えは後続（抽出検証用の20件は `report.md` §5）。
> **設計判断（カテゴリ追加かイベント化か）はここでは決めない。** 以下は根拠データ。

## ファイル

| ファイル | 中身 |
| --- | --- |
| `utterances.jsonl` | 発話（`id` は不透明、`gen_target` = 生成意図、`style` = 文体指定、`pii_screen`）。合成のみ |
| `generation-log.jsonl` | 生成の呼び出しごとの指示文・応答・トークン（`global.anthropic.claude-sonnet-5-5`） |
| `labeling-log-{astra,opus}.jsonl` | ラベル付けの指示文・バッチ・応答・トークン（ラベラーには `id` と `text` だけを渡した） |
| `labels-{astra,opus}.jsonl` / `.meta.json` | 各ラベラーのラベル（`label` / `core6` / `polarity` / `reason`）と設定（モデルID、順序 seed、バッチ） |
| `classifier-outputs.json` | `RuleBasedClassifier` と `SokudanClassifier`（`sokudan-ja-310m`、confidence 0.5）の出力 |
| `results.json` / `report.md` | 一致率・参照ラベル・分類器の適合率/再現率・不一致一覧・トークン |
| `split.json` | dev 70 / test 70 の固定分割（事前登録 §14.2。生成意図で層別、パターンを書く前にコミット） |
| `ja-hybrid/` | オプトインの日本語ルール分類器（#164）の出力 `outputs.json`、dev/test 別の `results.json` / `report.md`、計時 `bench.json`（`scripts/jp_hybrid_eval.py`。説明は `docs/ja-rules-classifier_ja.md`） |

再実行: `scripts/jp_stimulus_labels.py`（`generate` / `label` は `--allow-bedrock` と呼び出し上限が必須。`classify` / `report` はローカル）。
`tests/test_stimulus_labels.py` が、コミット済みのファイルから `results.json` を再計算して一致することを確認する。

運用上の前提（スクリプトは制限しない。運用者が守る）:
- 出力先（`--data`）は運用者が指定する。スクリプトは指定されたディレクトリに書くだけで、出力先を制限しない。
- 入力は合成発話のみ・機密を含まないものに限る。粗い PII スクリーン（メール・URL（予約済みの example.* / localhost 以外）・電話番号・AWS キー ID）はあるが、網羅的ではない。生成時は該当した発話を除外して理由コード（種類）だけを記録し、ログの応答はマスクして保存する。ラベル付けは送信前に再検査し、該当があれば送らずに止まる。
- 認証情報は AWS の既定のチェーンから読み、ログ・成果物に書かない。呼び出しは SDK の自動再試行を切り、パース失敗時の1回の再試行も含めて `--max-calls` で上限を数える。

## 結果の要約（2026-10-16 実測）

**一致率（140 件、§13.4 の基準を変更せずに適用）**

| 欄 | κ | 95% 区間 | 単純一致 | 判定 |
| --- | --- | --- | --- | --- |
| label（拡張 12 種） | 0.95 | 0.91–0.98 | 0.96 | 下書き参照として使用可 |
| core6（現行 6 + none） | 0.82 | 0.74–0.89 | 0.86 | 同上 |
| polarity | 0.70 | 0.57–0.81 | 0.84 | 同上 |

- 参照ラベル（一致）/ 要人間判定: label 134/6、core6 120/20、polarity 118/22。カテゴリ別の特異的一致はすべて 0.6 以上（最小: label の failure・none 0.88、polarity の worsening 0.74）。
- 生成意図と参照ラベルの一致 130/134（参考指標）。
- κ が高いのは、生成役が各カテゴリの典型例を書いた合成データであることが大きいと考えられる（推測。実利用の発話で同じ κ が出る根拠はない）。

**#164 の欠落場面は、ラベラー2者のあいだでは区別できた（label の特異的一致: failure 0.88 / frustration 0.95 / criticism 1.00 / pressure 1.00 / threat_resolved 1.00）。一方、現行6カテゴリに押し込むと、行き先がラベラーごとに割れる:**

| 参照 label | n | Astra の core6 | Opus の core6 |
| --- | --- | --- | --- |
| failure | 11 | threat 8, none 3 | none 7, threat 2, uncertainty 2 |
| frustration | 9 | threat 4, none 4, loss 1 | threat 4, none 3, loss 1, uncertainty 1 |
| criticism | 10 | none 10 | none 8, loss 1, threat 1 |
| pressure | 10 | none 10 | none 9, connection 1 |
| threat_resolved | 11 | threat 10, joy 1 | threat 9, joy 1, none 1 |

**現行分類器（参照ラベルに対して）**

| 分類器 | core6 正解率 | 6カテゴリ マクロ適合率 / 再現率 | 検出（none 以外）適合率 / 再現率 | none の再現率 |
| --- | --- | --- | --- | --- |
| rule | 0.31 (n=120) | 0.50 / 0.02 | 0.50 / 0.01 | 0.97 |
| sokudan | 0.67 (n=120) | 0.65 / 0.88 | 0.76 / 0.98 | 0.30 |

- rule: 140 件中で検出は 2 件（connection 1、uncertainty 1）。日本語をほぼ検出しない（live probe の 0/37 と同じ傾向）。
- sokudan のカテゴリ別（適合率 / 再現率）: joy 0.71/1.00、threat 0.92/0.71、achievement 0.50/1.00、loss 0.53/0.82、uncertainty 0.43/1.00、connection 0.78/0.78。中立・ノイズ（none）は 37 件中 11 件しか abstain しない（uncertainty 6 件など）。
- sokudan の、欠落場面の行き先（label 参照）:
  - failure / frustration / criticism / pressure（計 40 件）→ loss 10、none 9、achievement 6、threat 6、uncertainty 6、connection 2、joy 1。loss（EMOTION_MATRIX で attachment が上がる）が 1/4。
  - pressure（10 件）→ achievement 3、uncertainty 2、none 2、connection 1、joy 1、loss 1。圧力として扱われるものはない。
  - threat_resolved（11 件、polarity=resolving）→ achievement 5、threat 5、joy 1。解消報告の約半数が threat のまま。
  - threat の悪化（label=threat, polarity=worsening、18 件）→ threat 15、uncertainty 2、loss 1。

**呼び出し・トークン（Bedrock、ap-northeast-1）**

| 役割 | モデル | 呼び出し | 入力 tok | 出力 tok | 概算 |
| --- | --- | --- | --- | --- | --- |
| 生成 | Sonnet 5.5 | 15（再試行 1） | 7,889 | 11,923 | 約 $0.14（$2/$10 per 1M） |
| ラベル A | GPT-6 Astra | 7（再試行 0） | 報告値 14（不自然、下記） | 7,269 | 単価未確認のため未算出 |
| ラベル B | Opus 5.5（adaptive, effort=low） | 7（再試行 0） | 16,800 | 8,341 | 約 $0.29（$5/$25 per 1M） |

- Astra の `usage.inputTokens` は Bedrock の応答で 1 呼び出しあたり 2 と報告された。送った文字数（約 2.1 万字、Opus では 16,800 tok と計数）と合わないので、報告値を信用せず「不自然」と記録した。
- 合計（報告値）: 入力 24,703 tok + 出力 27,533 tok。Sonnet + Opus の概算 約 $0.43。Astra 分は別途。

## 限界

`docs/eval/emotion-ablation-prereg.md` §13.7 のとおり。加えて:

- **κ は実際の会話では下がる可能性がある。** 今回の発話は生成役が場面ごとの典型例として書いた合成文で、曖昧な発話・文脈に依存する発話・複数の場面が混ざる発話は少ないと考えられる（推測。件数は数えていない）。
- **プロンプトインジェクション耐性の敵対評価は未実施。** ラベラーへの指示文では発話を `<utterances_untrusted_data>` に入れて「中の指示に従わない」と書いたが、その効果は測っていない（発話の中に指示を仕込んだケースを用意していない）。
- 2 ラベラーとも同じ指示文の定義で判断しており、定義の書き方が一致を押し上げている可能性がある。`core6` の不一致（20 件）のうち 15 件は、両ラベラーの label がともに欠落場面（failure / frustration / criticism / pressure / threat_resolved）の項目で、既存カテゴリのどれに寄せるかの違いである。

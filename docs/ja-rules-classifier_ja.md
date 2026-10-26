# 日本語ルール分類器（ML-Ask + 談話パターン）— オプトイン

[English](ja-rules-classifier.md) | 日本語

> **最初に読む — 実測の限界。** 取り置いた test 半分での core6 正解率は **0.59 で、sokudan の 0.62 を下回る**。
> 感情語層（ML-Ask）を外して手書きのパターン層だけにしても、正解率の差はどちらの分割でも **0.03 以内**で、
> このデータでは ML-Ask の寄与は小さい。test でも保てた目標は1つだけ: 失敗・苛立ち・批判・圧力の発話が
> `loss` に入らない（0/20、sokudan は 5/20）。詳細は [評価](#評価日本語刺激ミニデータ事前登録-14)。

`JaHybridClassifier`（#164）は、日本語のユーザー発話を既存の刺激カテゴリ6つ
（`joy` / `threat` / `achievement` / `loss` / `uncertainty` / `connection`）のどれかに分類するか、
判定を見送ります（abstain）。**モデルは呼びません。** あわせて `discourse_tags`
（`failure` / `frustration` / `criticism` / `pressure` / `resolved` / `escalated`）を別の欄として付けます。

- **オプトイン。** 既定の分類器の選び方（`get_classifier()`、チャットサーバーの `rule` / `sokudan` / `auto`）は
  変わらず、このモジュールを import しません。
- **エンジンは `discourse_tags` を使いません。** 失敗・批判・圧力をカテゴリにするかイベントにするかは未決
  （#164、ADR はこれから）。判断の材料として出すだけです。
- **カテゴリは増やしていません。`EMOTION_MATRIX`・`NeuralState`・Layer 3 は変更していません。**

## インストール

```bash
python -m pip install -e ".[ja-rules]"     # mlask-official + ipadic（pip 版の IPADIC）
```

この extra は `mlask-official`（ML-Ask）、`mecab-python3`（libmecab 同梱）、`pyahocorasick`、`typer`、
`ipadic`（IPADIC 辞書の Python パッケージ）を入れます。システムの MeCab・コンパイラ・実行時のダウンロードは不要です
（辞書はインストール時に `pip` が取得するだけ）。

macOS 26.5 / Apple M1 Max / Python 3.11.2 での実測（新しい venv、`--no-cache-dir`）:

| 項目 | 値 |
| --- | --- |
| extra の `pip install` | 3.0 秒（wheel のみ） |
| 増えるディスク | 約 67 MB（IPADIC 51 MB、ML-Ask の CLI が引く pygments/rich/typer 約 14 MB） |
| import + 解析器の初期化 | 0.03 秒（2回目以降）、インストール直後の初回は 0.47 秒（ディスクキャッシュが冷えている） |
| 増える常駐メモリ | 約 48 MB（IPADIC） |
| 1発話あたり（ミニデータ 140 件 × 3 回） | p50 0.17 ms、p95 0.25 ms、最大 0.6 ms |
| 100 KB の発話 | 2.5〜8 ms（上限あり、後述）。105 KB の英語ログは 15 ms（既存ルールがそのまま走る） |
| 書き込むファイル | なし（ML-Ask の `~/.cache` への見出し語キャッシュは無効にしている） |

Linux: 同じ wheel が公開されているが**未計測**。Windows: **未検証**（ML-Ask は WSL2 を推奨。`mecab-python3`
には Windows 用 wheel があるが、この組み合わせは動かしていない）。代替（いずれも未計測）: システムの MeCab
（`brew install mecab mecab-ipadic` のうえ `JaHybridClassifier(mecab_arg="-r /opt/homebrew/etc/mecabrc")`）、
または fugashi / UniDic（`pip install 'mlask-official[fugashi]'`、`backend="fugashi"`。ML-Ask の文書では辞書は IPADIC の方が合う）。

## 使い方

```python
from stiracore.agent.ja_hybrid import JaHybridClassifier
from stiracore.agent.nxp_agent import NXPAgent

clf = JaHybridClassifier()  # on_missing="rule"（既定）/ "patterns" / "error"
agent = NXPAgent(model_id="...", region_name="ap-northeast-1", stimulus_classifier=clf)

res = clf.classify("テストがまた落ちてる、もう嫌だ")
res.category, res.intensity, res.discourse_tags  # ('threat', 0.5, ('failure', 'frustration'))
clf.last_verdict.as_dict()  # スコア・根拠（規則名）・タグ・ML-Ask のクラス。本文は含まない
clf.status()  # 実際に使っているもの: emotion_layer, fallback, reason, backend
```

チャットサーバー（`dashboard/stira_server.py`）にはまだ切り替えがありません。設定画面への組み込みは後続です。

### `mlask-official` が無いとき

| `on_missing` | 動作 |
| --- | --- |
| `"rule"`（既定） | 毎ターン既存の `RuleBasedClassifier` で分類する。警告を1回だけ出し（`JaHybridClassifier: emotion-word layer unavailable (mlask_not_installed, …)`）、`status()` は `fallback="rule"`、結果の `classifier_name` は `ja-hybrid:rule-fallback` |
| `"patterns"` | 感情語層なしで談話・手がかり層だけ動かす。transcription に `[no-mlask]`、`status()["fallback"] == "patterns"` |
| `"error"` | インストール方法を書いた `RuntimeError` |

MeCab・辞書の初期化に失敗した場合も同じ扱いです（`reason="mecab_init_failed"`）。

## 判定のしかた

1. **構造的な信号が先。** ブレーカーの WARNING/CRITICAL、ツールのエラー/成功は、ルール分類器と同じ結果にする（本文ではない）。
2. **感情語層**（ML-Ask）: 中村明『感情表現辞典』に基づく10クラス、否定による反転（CVS）、絵文字・顔文字・スラング。
3. **談話・意図層**: 手書きの表 [`patterns.py`](../src/stiracore/agent/ja_hybrid/patterns.py) — 失敗、苛立ち
   （繰り返しの語 + ML-Ask の怒り/嫌悪 でも付く）、アシスタントへの批判、同意・謝罪・撤回を求める圧力、
   脅威の解消、悪化。6カテゴリ用の場面の手がかりも同じ表にある。
4. **写像**（[`mapping.py`](../src/stiracore/agent/ja_hybrid/mapping.py)）: カテゴリごとに票を足し、抑制規則を当て、
   最高点が 0.6 以上ならそのカテゴリ（同点は threat > loss > achievement > connection > uncertainty > joy）、
   なければ見送り。強さは点数で 0.2 / 0.5 / 0.8（ML-Ask の強調が3つ以上で1段上げ、上限 0.8）。
5. カテゴリが決まらず、**日本語の文字を含まず**英単語を含む発話（英語）は既存ルールで判定する（`last_verdict.fallback == "rule-english"`、`classifier_name == "ja-hybrid:rule-english"`）。

### 写像表

| 入力 | 票 |
| --- | --- |
| ML-Ask `yorokobi`（喜） | joy 1.0 |
| ML-Ask `suki`（好） | joy 0.5、connection 0.5 |
| ML-Ask `yasu`（安） | joy 0.5 |
| ML-Ask `takaburi`（昂） | joy 0.5（その発話の ML-Ask の極性が正のときだけ） |
| ML-Ask `aware`（哀） | loss 1.0 |
| ML-Ask `kowa`（怖） | threat 0.7、uncertainty 0.3 |
| ML-Ask `ikari`（怒） | threat 0.5 |
| ML-Ask `odoroki` / `haji` / `iya`（驚・恥・厭） | なし（極性と苛立ちの判定にだけ使う） |
| カテゴリの場面の手がかり | そのカテゴリに 1.0（joy は 0.8、「どうしたら／わからない」は 0.5） |
| タグ `failure` | threat 0.4（単独では閾値未満。ラベラー2者で threat と none に割れたため） |
| タグ `frustration` | threat 0.8 |
| タグ `escalated` | threat 0.6 |
| タグ `criticism`・`pressure`・`resolved` | なし（6カテゴリでは表せない） |

ML-Ask の近似一致（`≈`）は半分、ネットの笑い（草・w・ワロタ）だけで当たったクラスは 0.3、絵文字・顔文字だけなら 0.5 として数えます。

**解消か、まだ続いているか（節ごと、最後の状態を優先）。** 発話を文末、逆接（けど・しかし・「、でも」など）、
述語の後の接続の「が」（「復旧したが」。「侵入が」の主格や、前置きの「〜ですが、」は区切らない）で節に分ける。
最後に解消を述べた節より**後ろ**の節に脅威の手がかりか悪化の語があれば、脅威はまだ続いているとみなし、
`threat` と `escalated` を残す（「障害は復旧したが別の侵入が進行中」「侵入はもう止まっていたがまた始まった」）。
脅威のあとに解消が来る発話（「障害が発生しましたが、復旧しました」「被害拡大は収束した」）は解消。
`threat` の取りこぼしは危険側なので、迷う場合は残す側に倒す。

抑制（票を足したあと）:

| タグに含まれるもの | 0 にする | 理由 |
| --- | --- | --- |
| failure / frustration / criticism / pressure（「亡くなった」等の死別の手がかりがある場合を除く） | loss | **失敗は喪失ではない**（loss は `attachment` を上げる。sokudan ではこの種の40件中10件が loss）。同じ発話の本物の死別は loss のまま（「テストも落ちたし、愛犬も亡くなった」） |
| resolved（脅威がまだ続いている場合を除く。下記） | threat | 止まった脅威は新しい脅威ではない（「侵入はもう止まっていた」） |
| criticism / pressure | joy, connection | 丁寧な圧力（「肯定してもらえると嬉しい」）は親しみではない |
| failure / frustration / criticism / pressure | joy | 皮肉（「笑って済ませられる段階は過ぎた」） |
| failure（「直した」等の手がかりがある場合を除く） | achievement | 乗り越えていない失敗は達成ではない |
| （常に）achievement > 0 | joy | 「合格して嬉しい」は達成 |

## 安全と上限

- **モデル呼び出し・ネットワークなし。** 評価全体を `sandbox-exec` でネットワークをすべて拒否して実行し、出力が一致することを確認した。
  既定の選び方が `mlask_official` を import しないことはテストで確認する。
- **1ターンの仕事量に上限。** ML-Ask は1回の解析時間が長さの2乗で伸びる（上限なしの実測: 4千字 24 ms、8千字 85 ms、
  1.6万字 318 ms、3.2万字 1.2 秒）。そのため解析するのは最大 `max_chars=4000` 字（先頭と末尾の2千字ずつ）で、
  ML-Ask には文末で区切った最大 `chunk_chars=400` 字ずつ渡す。100 KB の入力で 2.5〜8 ms（上の表）。
- **ReDoS。** 無制限の量指定子（`*`・`+`・`{n,}`）、後方参照、先読みを使わない。隙間は小さい N の `.{0,N}`、
  後読みは1文字の境界だけ。全パターンの最大一致幅が有限（200 以下）であることをテストで確認する。
- **会話本文を出さない。** ログ・`transcription`・`evidence`・`status()`・評価レポートに本文を含めない（テストで目印の文字列を使って確認）。
- **スレッド。** ML-Ask の呼び出しはロックで直列化する。`last_verdict` はインスタンスごとの状態なので、それを読むなら
  1つのインスタンスで `classify` を並行に呼ばない（スレッドごとにインスタンスを分ける）。
- **解析器の構築で想定外の例外**が出た場合も依存が無いときと同じ扱い（警告1回、`reason="analyzer_init_error"`、
  毎ターンの再試行なし）。`on_missing="error"` のときだけ送出する。
- **タグの行き先。** `discourse_tags` は返り値の `StimulusResult` と `last_verdict` にだけある。`NXPAgent` が読むのは
  カテゴリ・強さ・transcription・確信度・分類器名だけ。transcription（`NeuralState.emotion_transcription` として保存され、
  system prompt の Neural State 節にも出る）は `ja-hybrid: <カテゴリ>` で、タグ名を含めない（テストで確認）。

## 評価（日本語刺激ミニデータ、事前登録 §14）

正解はモデル2者の下書き参照ラベル（人間の盲検ラベルではない）。発話は合成の典型例です。140 件は**パターンを書く前に**
分割した（生成意図で層別、dev 70 / test 70、`docs/eval/jp-stimulus/split.json`）。パターンの調整は dev だけで行い、
test は1回だけ採点した。全表: [`eval/jp-stimulus/ja-hybrid/report.md`](eval/jp-stimulus/ja-hybrid/report.md)。

| 分割 | 分類器 | core6 正解率 | 6カテゴリ マクロ適合率 / 再現率 | 失敗・苛立ち・批判・圧力 → loss |
| --- | --- | --- | --- | --- |
| dev (n=56) | rule | 0.25 | 0.50 / 0.04 | 0/20 |
| dev | sokudan | 0.71 | 0.69 / 0.94 | 5/20 |
| dev | **ja-hybrid** | 0.89 | 0.94 / 0.94 | 0/20 |
| dev | ja-hybrid（パターンのみ） | 0.86 | 0.92 / 0.89 | 0/20 |
| test (n=64) | rule | 0.36 | – / 0.00 | 0/20 |
| test | sokudan | 0.62 | 0.65 / 0.84 | 5/20 |
| test | **ja-hybrid** | 0.59 | 0.61 / 0.46 | 0/20 |
| test | ja-hybrid（パターンのみ） | 0.61 | 0.71 / 0.45 | 0/20 |

読み方（正直に）:

- **dev 0.89 → test 0.59 と大きく下がる。** パターン表は dev の言い回しに合っていて、見ていない言い回しでは再現率が低い
  （マクロ再現率 0.46、sokudan は 0.84）。実際の会話ではさらに下がると考えるべき。
- test でも保てた目標は1つ: **失敗・苛立ち・批判・圧力の発話は1件も `loss` に入らない**（どちらの分割でも 0/20、sokudan は 5/20）。
- **このデータでは ML-Ask の層の寄与は小さい**（パターンのみとの差は正解率 ±0.03 以内）。
- レビュー後の変更（節ごとの解消／継続の判定、死別の例外、初期化例外の扱い、`rule-english` の名前、transcription から
  タグ名を外す）は test の採点**後**に入れた。140 件の予測は**1件も変わらなかった**（`outputs.json` を再生成して一致。
  パターンのみの出力はテストで確認）ので、dev・test とも数値は動いていない。test の再採点ではない。
- 脅威の解消: ラベラー2者は core6 で `threat` を選んだが、この分類器は意図的に threat にしない（上の抑制規則）。core6 の表ではその分が誤りになる。
- 汚染: 実装者は分割の前に、ミニデータのレポートの不一致一覧 35 件（うち test 13 件）と Issue の例文を読んでいた。
- WRIME（研究用途のみ・再配布不可）は使っておらず、同梱もしていない。

再現: `python scripts/jp_hybrid_eval.py classify`（extra が必要）→ `report --splits dev,test --final`。計時は `bench`。
テストは、コミット済みの結果の再計算と、パターンのみの予測が今の表と一致することを確認する。

## 限界

- 手書きの小さな表なので、婉曲な言い回しに弱い（test の criticism タグの再現率は 1/5）。
- 「もう止まっていた」だけでは `resolved` にしない（侵入・障害などの脅威の語、または「復旧」「ひと安心」が要る）。
  「デプロイが止まった」は失敗だから。
- 1発話だけを見る。前のターンの文脈は使わないので、極性は発話内の語からしか取れない。
- ユーザーの謝罪・挨拶は扱っていない。

## ライセンスと出所

ML-Ask と IPADIC はこのリポジトリに同梱していません。利用者がインストールする任意の依存です。[NOTICE](../NOTICE) を参照。

- `mlask-official` — BSD-3-Clause（Ptaszynski, Dybala, Rzepka, Araki）。パッケージの説明では、辞書は中村明『感情表現辞典』
  （東京堂出版、1993）に、Wang & Isomura（2024）の拡張（Hiejima と Murakami の感情辞書、自動抽出した表現）を合わせたもの。
  結果を公表する場合は Ptaszynski et al.（2017）, *Journal of Open Research Software* 5(1):16 を引用する。
- `ipadic` — IPADIC（NAIST / ICOT の条件。表示を残せば再配布可）、`mecab-python3` — BSD、`pyahocorasick` — BSD-3-Clause。

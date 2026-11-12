# A/B 実行のガード（指紋・計画照合・漏洩検査） / A/B run guards

- Issue: #729。#698 の事前登録文書（ハーネス変更の A/B）から参照するための部品の説明。
- 実装: `src/stiracore/eval/ab_guards.py`。テスト: `tests/test_eval_ab_guards.py`。
- **部品だけ**。A/B を走らせる実行器、ファイルの読み込み、held-out の隠し方は含まない（#698 の範囲）。
  既存のアブレーション用ランナー（`python -m stiracore.eval`）からは呼ばれず、既定の挙動は何も変わらない。

> English summary: four pure functions for harness A/B runs — a fingerprint of the harness files
> and task inputs, a check that the two arms differ only by the declared change, a check that the
> finished runs are the planned ones, and a fail-closed check that no grading term or held-out path
> is in the run-side context. No clock, randomness, network, model or file I/O. Results carry
> fixed reason codes only. They make an invalid comparison visible; they are not evidence that a
> harness change helps, and `leak_check` is a tripwire on text, not an access boundary.

## 1. 使い方

呼び出し側（#698 の実行器）がファイルを読み、バイト列と文字列を渡す。

```python
from stiracore.eval import ab_guards as g

# 実行ごとに記録する
fp = g.run_fingerprint(harness_files, inputs)  # sha256 の 16 進（64 文字）
detail = g.fingerprint_detail(harness_files, inputs)  # {"harness": {名前: sha256}, "inputs": {...}}

# 実行前: 実行側に渡すコンテキストを検査する
leak = g.leak_check(context_parts, heldout_globs, banned_terms)

# 集計前: 2 群の差が申告どおりか、計画どおりに走ったか
diff = g.diff_matches_declared_change(detail_a, detail_b, {"agent/prompt.md"})
plan = g.check_plan({"A": 10, "B": 10}, labels)  # labels: "A/0", "A/1", ..., "B/9"

if not (leak.ok and diff.ok and plan.ok):
    ...  # 結果を無効として扱う。reasons（固定コード）を記録する
```

| 関数 | 入力 | 拒否する場合 |
|---|---|---|
| `run_fingerprint(harness_files, inputs)` | `Mapping[str, bytes]` × 2 | 型が違えば `TypeError`（`str` の内容も不可。符号化は呼び出し側が決める） |
| `fingerprint_detail(harness_files, inputs)` | 同上 | 同上 |
| `diff_matches_declared_change(a, b, declared)` | `fingerprint_detail` の結果 × 2、申告したファイル名の `set` | 下の表 |
| `check_plan(planned, actual)` | 群名→計画回数、実績ラベル `"<群>/<実行 ID>"` の列 | 下の表 |
| `leak_check(context_parts, heldout_globs, banned_terms)` | いずれも `str` の列 | 下の表 |

`Result` は `ok`（`reasons` が空のときだけ `True`）と `reasons`（固定コードのタプル、重複なし）。
ファイル名・語・パスなどの自由文は入らないので、そのままログに書ける。コードの一覧は `REASON_CODES`。

### 理由コード

| コード | 意味 |
|---|---|
| `E_FINGERPRINT_INVALID` | 指紋の詳細の形が不正（`fingerprint_detail` の結果ではない） |
| `E_DECLARED_INVALID` / `E_DECLARED_EMPTY` | 申告が `set[str]` でない / 空（同一の 2 群は通さない） |
| `E_INPUTS_DIFFER` | 2 群で課題入力が違う |
| `E_CHANGE_NOT_DECLARED` | 申告していないハーネスファイルが違う |
| `E_DECLARED_NOT_CHANGED` | 申告したファイルが実際には同じ |
| `E_PLAN_INVALID` / `E_PLAN_EMPTY` | 計画の型が不正（回数は 1 以上の `int`、群名は空でなく `/` を含まない） / 空 |
| `E_ACTUAL_INVALID` | 実績が `str` の列でない（`str` 1 個を渡した場合を含む） |
| `E_LABEL_INVALID` | ラベルが `"<群>/<実行 ID>"` の形でない |
| `E_LABEL_DUPLICATE` | 同じラベルが 2 回ある（2 回目は回数に数えない） |
| `E_ARM_UNPLANNED` | 計画にない群のラベル |
| `E_COUNT_MISMATCH` | 群ごとの回数が計画と違う（多い・少ない・0 回） |
| `E_CONTEXT_INVALID` / `E_CONTEXT_EMPTY` | コンテキストが `str` の列でない / 1 件もない |
| `E_HELDOUT_GLOBS_INVALID` / `E_HELDOUT_GLOBS_EMPTY` | glob の型が不正・空文字を含む / 1 件もない |
| `E_BANNED_TERMS_INVALID` / `E_BANNED_TERMS_EMPTY` | 禁止語の型が不正・空白だけの語を含む / 1 件もない |
| `E_LEAK_BANNED_TERM` | 禁止語がコンテキストにある |
| `E_LEAK_HELDOUT_PATH` | held-out の glob に合うパスがコンテキストにある |

## 2. 各部品の規則

**指紋。** 各ファイルの sha256 を、`harness` と `inputs` の区分・件数・長さ付きの名前と一緒に sha256 にかける
（スキーマ名 `stira-eval-ab-fingerprint/1` を先頭に含む）。同じ名前とバイト列なら Mapping の順序によらず同じ値。
1 バイトの差、改名、追加、削除、区分の入れ替えで値が変わる。符号化を変えるときはスキーマ名を上げる
（記録済みの指紋と比較できなくなるため。`test_fingerprint_is_a_fixed_value` が固定している）。

**申告した変更との照合。** ハーネス側で違うファイルの集合（片方にしか無いファイルを含む）が `declared` と
完全に一致し、かつ入力が同一のときだけ `ok`。1 つの変更が複数ファイルに及ぶ場合は、その全ファイルを申告する。
通らなかった結果は A/B の比較として無効（指紋が違う実行を同じ群として扱わない）。

**計画照合。** 実績ラベルは最初の `/` で群名と実行 ID に分ける。重複・計画外の群・回数の過不足をそれぞれ拒否する。

**漏洩検査（fail-closed）。** 禁止語は NFKC・大文字小文字の畳み込み・空白の圧縮のあと部分一致で探す。
held-out の glob（`fnmatch` の書式、大文字小文字を区別、`*` は `/` もまたぐ）は、コンテキスト中のパスらしい
トークンと、その `/` 以降の各部分（絶対パスの末尾側）に当てる。`\` は `/` として読む。
型の不正、空の語・glob、コンテキスト・glob・禁止語のいずれかが 0 件のときは、判定せずに拒否する
（「調べるものが無い」を「調べて問題なし」として通さない）。どの語・パスが当たったかは返さない（それ自体が評価情報）。

## 3. 保証すること・しないこと

保証すること（テストで固定）:

- 同じ入力→同じ指紋。1 バイトの差で変わる。Mapping の順序に依存しない。
- 回数の不一致、ラベルの重複、指紋の差と申告の不一致、漏洩、判定不能な設定は `ok=False` になる。
- 時計・乱数・ネットワーク・モデル・ファイル I/O を使わない。`reasons` は固定コードだけ。

保証しないこと:

- **ハーネス変更の効果の証拠ではない。** 比較が無効であることを見えるようにするだけ。
- **パスの正規化、`..`、シンボリックリンク、ファイル名の大文字小文字は扱わない。** 名前は不透明な文字列で、
  `a/b.py` と `./a/b.py` は別の名前。両群で同じ規則で名前を作り、渡すファイルの集合を決めるのは呼び出し側。
  渡されなかったファイル（環境変数、依存パッケージの版、モデル ID など）の差は検出できない。
  必要なものは呼び出し側がバイト列にして `harness_files` に入れる。
- **`leak_check` はアクセス境界ではない。** 渡された文字列を調べるだけで、実行側がツールでファイルを読むことは
  止めない。held-out を読ませない仕組みは読み取り床（#315）と #698 の事前登録文書の手順に委ねる。
  言い換え、符号化（base64 など）、分割された語、大文字小文字だけ違うパスは検出しない。
  逆に、禁止語を部分文字列として含む無関係な語は拒否される（安全側）。禁止語は具体的なものを選ぶ。
- 実績ラベルが実際の実行に対応しているか（ラベルの偽造）は調べない。

## 4. 設計メモ: 評価時の egress 許可（未実装）

課題セットを配布元から取得する評価では、実行側の egress 許可に配布元のホストを足す必要がある。
この部品では実装しない。別 Issue で扱うときの前提だけを書いておく:

- 足すのは**配布元ホストだけ**を、**その評価の間だけ**。既存の許可を広げる既定値は作らない（オプトイン、人間の承認つき）。
- 取得は実行側ではなく準備段階で済ませ、取得したバイト列を `inputs` として指紋に入れる。
  実行中の群がネットワークから課題や採点情報を取り直せる構成にしない。
- held-out と採点情報の配布元は実行側の許可に入れない。
- 許可したホストの一覧は自由文ではなくホスト名の許可リストとして記録し、`harness_files` に含めて群間の差を検出できるようにする。

# 一貫性スコア（`bc_score`）の定義（#168）

English: [coherence.md](coherence.md)。参照実装: `stiracore.core.compute_bc_score`
（`src/stiracore/core/coherence.py`）、挙動は `tests/test_coherence_score.py` で固定しています。
このページの数値はすべて現行コードでの実測値です。

## 何を測り、何を測らないか

`bc_score` が測るのは **直近のターンで `stability` がどれだけ動いたか** だけです。他の 5 次元、
会話の内容、SOUL、Identity Anchors はどれも入力に入りません。スコアが低いことは「状態が速く
変化している」ことを示すだけで、エージェントが「何であるか」については何も言いません。この
ページが定義するのは計算式であって、人格ではありません。アンカーは運用者が書く文章のままで、
ハーネスがそれを数値に置き換えることはありません。

## 定義

| | |
|---|---|
| 標本 | `StiraEngine.pre_turn` のたびに読む `stability`。**減衰の適用後、そのターンの刺激の前** の値（＝前のターンまでの刺激を反映）。 |
| 標本を取る時 | 試行したターンごとに 1 回（`pre_turn`）。読み取り専用・停止中のターンも数える。`breaker_status()` の照会、ハートビートの tick、`post_turn` では取らない。 |
| 窓 | 直近 `bc_window_size` 件、古い順（`NeuralState.bc_window`）。`neural-state.json` に保存され、再起動後も続く。 |
| 最小件数 | `BC_MIN_SAMPLES` 件未満は **未計測** で、スコアは `1.0`。 |
| 式 | `bc_score = round(max(0, 1 − 5 · σ), 4)`。σ は手元の標本の母標準偏差（`sqrt(Σ(x − mean)² / n)`）。 |
| 値域 | `[0, 1]`。σ = 0 → 1.0、σ ≥ 0.2 → 0.0。効くのは散らばりだけで、標本の順序や全体を同じだけずらすことは結果を変えない。 |
| 再読込後 | 保存された窓から計算し直す。保存された `bc_score` の値は使わない。 |

<!-- coherence-defaults:begin -->
| 設定・定数 | 既定値 | 場所 |
|---|---:|---|
| `bc_window_size` | 20 | `StiraConfig` |
| `BC_MIN_SAMPLES` | 5 | `core/coherence.py` |
| `BC_STDDEV_SCALE` | 5.0 | `core/coherence.py` |
| `bc_threshold`（これ未満で COHERENCE に入る） | 0.6 | `StiraConfig` |
| `bc_exit_threshold`（これを超えると COHERENCE を出る） | 0.7 | `StiraConfig` |
<!-- coherence-defaults:end -->

既定値を σ で言い直すと、σ > 0.08 で COHERENCE に入り、σ < 0.06 で出ます。

## 数値例（窓 → `bc_score`）

| 窓 | σ | `bc_score` | ブレーカー（NORMAL から） |
|---|---:|---:|---|
| `[0, 1, 0, 1]`（4 件） | — | 1.0（未計測） | NORMAL |
| `[0, 1, 0, 1, 0]` | 0.49 | 0.0 | COHERENCE |
| `[0.7] × 5` | 0 | 1.0 | NORMAL |
| `[0.7, 0.7, 0.7, 0.7, 0.6]` | 0.04 | 0.8 | NORMAL |
| `[0.7, 0.7, 0.7, 0.7, 0.5]` | 0.08 | 0.6 | NORMAL（入るのは 0.6 未満のとき） |
| `[0.9, 0.9, 0.9, 0.9, 0.69]` | 0.084 | 0.58 | COHERENCE |
| `[0.62] × 10 + [0.78] × 10` | 0.08 | 0.6 | NORMAL |
| `[0.64] × 10 + [0.76] × 10` | 0.06 | 0.7 | 入っていれば COHERENCE のまま（出るのは 0.7 を超えたとき） |
| `0.70, 0.72, … 0.88`（10 件、+0.02 ずつ） | 0.057 | 0.7128 | NORMAL |
| `0.60, 0.62, … 0.98`（20 件、+0.02 ずつ） | 0.115 | 0.4234 | COHERENCE |

```python
from stiracore.core import compute_bc_score

compute_bc_score([0.7, 0.7, 0.7, 0.7, 0.5])  # 0.6
compute_bc_score([0.7, 0.7, 0.7, 0.7])  # 1.0 — 5 件未満
```

別の実装（Stira の外の自己監視など）は、自分で stability の標本の窓を持ち、それを
`compute_bc_score` に渡せば同じ値になります。標本は `[0, 1]` の有限の数でなければならず、
それ以外は `ValueError` になります。

## 窓の初期充填 — 確定した仕様（#142）

以下は実測しテストで固定した、仕様としての挙動です。ここでは変更しません。

1. **標本 1〜4 件目: COHERENCE にならない。** 標本が何であってもスコアは `1.0` のまま
   （新しい `NeuralState` は 1.0 から始まり、5 件未満で再読込すると 1.0 に戻る）。
   スコアだけでは「まだ計測していない」と「計測して動きがない」（どちらも 1.0）を区別できないので、
   各ステータス表示がスコアの隣にそれを示します（#337、「未計測と完全安定」を参照）。
2. **5 件目以降、窓が埋まるまで: 手元の標本だけで計算する。** 窓を埋め草で補わず、外挿も
   しません。結果として次の 2 つが起きます（どちらも実測）。
   - 1 回きりの変化は `1/n` の重みになる。他より 0.1 低い標本が 1 つあると、n = 5 で 0.8、
     n = 10 で 0.85、n = 20 で 0.891、その標本が窓から出ると 1.0 に戻る。
   - 一定方向の変化は、窓が埋まるほど *重く* なる。1 ターンあたり `s` の変化が `n` 件続くと
     σ = `s · sqrt((n² − 1) / 12)`。20 件の窓が埋まると、1 ターンあたり約 0.014 より速い一定の
     変化は COHERENCE に至る。
3. **向きは問わない。** 上昇も下降と同じに数える。[breaker-reachability.md](breaker-reachability.md)
   で肯定的な刺激の連続が COHERENCE に届くのはこのためです。強度 1.0 の `achievement` は
   1 ターンに stability を 0.03 上げ、10 ターン目に COHERENCE になります（`joy` / `connection` は
   +0.02 で 14 ターン目）。stability が 1.0 で止まると古い標本が窓から抜け、NORMAL に戻ります。
   この連続で stability 自体は動いており（0.70 → 1.00）、スコアはその動きを報告しています。
4. **保存される。** 窓は `neural-state.json` の一部なので、再起動しても続きから数え、充填を
   やり直しません。

## 未計測と完全安定（#337）

どちらもスコアは `1.0` です。ステータス表示は `bc_score` の **隣に** 3 つのフィールドを足します。
既存のものは何も変わりません（追加のみ。保存された窓から導出し、`NeuralState` は読むだけで変えません）。

| フィールド | 意味 |
|---|---|
| `bc_samples` | スコアの根拠になっている stability の標本数（`len(bc_window)`）。 |
| `bc_measured` | `bc_samples >= bc_min_samples` なら `true`。`false` は「まだ計測していない」で、`1.0` は結果ではなく初期値。 |
| `bc_min_samples` | `BC_MIN_SAMPLES`（5）。 |

| `bc_score` | `bc_measured` | 読み方 |
|---:|---|---|
| 1.0 | `false` | まだ計測していない（標本が 5 件未満）。 |
| 1.0 | `true` | 計測済み。5 件以上の標本に差がまったくない（小数 4 桁で）。 |
| < 1.0 | `true` | 計測済み。状態が動いている。 |

`stiracore.core.bc_is_measured(n)` / `bc_measurement(n)` がフィールドを作るので、ほかのホスト
も同じ形で足せます。

多くのターンで計測済みの 1.0（またはそれに近い値）が続くのは、分類器がほとんど刺激を出して
いない場合もあります（例: Sokudan なしのルールへのフォールバック）。どの分類器で動いているか、
何ターンで状態が変わったかはステータスに出ます（`classifier.mode`、`emotion_updates`。
[neural-saturation_ja.md §6](neural-saturation_ja.md)、#367）。

## しきい値・優先順位・効果

- 入る: `bc_score < bc_threshold`。出る: `bc_score > bc_exit_threshold`（その間は直前の状態を保つ）。
  `StiraConfig` は `bc_exit_threshold > bc_threshold` を要求します。
- 優先順位: CRITICAL > WARNING > COHERENCE。上位のレベルの間も一貫性のラッチは評価され続けるので、
  上位が解けたときにスコアがまだ低ければ COHERENCE が表に出ます。
- 効果: `INJECT_IDENTITY` のみ。設定された Identity Anchors を含む identity notice を system
  prompt の先頭に置き、末尾は "COHERENCE: Identity drift detected. Reaffirm these anchors."。
  **ツールは制限しません**（ADR 0010 §2）。

## どこに現れるか（チャネル）

| チャネル | 内容 |
|---|---|
| エンジンのログ | COHERENCE に入ったとき: レベルのメッセージと `bcWindow analysis: samples=… score=… window=[…]`（数値のみ）。続いている間は 20 ターンごとに数値だけの要約（#165）。 |
| `on_breaker` コールバック | レベルが変わるたび（ホストはこれで制限を掛け直す）。 |
| `/api/gateway/status`（チャットサーバー） | `bc_score`。レベルは `breaker` の中。`bc_samples` / `bc_measured` / `bc_min_samples`（下記）。`bc_window_clamped`（下限 5 の節）。 |
| チャット UI | 状態表示の「整合性スコア」。最小標本数までは「未計測（3 / 5 サンプル）」、以降は数値（ちょうど 1.0 のときは「1.000 (変動なし)」）。 |
| 観測コンソール（`dashboard/app.py`、`/api/state`） | `bc_threshold` に対する一貫性のダイヤルと標本数。最小標本数までダイヤルは `—` / 「未計測」。同じ 3 フィールド。COHERENCE は 🟡 HOLD。 |
| 設定ハブ | `bc_threshold` を編集できる（`bc_exit_threshold` 未満であること。0.3 未満は危険として扱う）。 |

## `bc_window_size` の下限は 5（#337）

`BC_MIN_SAMPLES`（5）未満の窓は計測できません。スコアは 1.0 のままで COHERENCE は発火せず、
そのことを誰も教えてくれません。そのため不正な `bc_window_size`（5 未満、または `int` でないもの。
`bool`・`5.0`・`"20"`・`null` も）はそのままでは使われません。どうなるかは出どころで決まります。

- **起動・読み込み**（すでに不正な値が入った `agent-config.json`。多くは手編集で、
  `bc_window_size` は設定 UI では編集できません。チャットサーバーは丸めた後の値を起動時にエンジンへ渡します、#272）: `AgentConfig.load` が **5 に丸めて起動します**。
  警告を 1 プロセスにつき 1 回ログに出し（`bc_window_size in the agent configuration was not an
  integer >= 5; rounded to 5 … [bc_window_clamped]`。値そのものは出しません）、
  `/api/gateway/status`・`/api/config`・設定ハブの状態に `bc_window_clamped: true` を出します
  （追加のみ。丸めていなければ `false`）。3 つとも今のファイルを読むので、常に一致します。読み込みでは
  ファイルを書き換えません。次に設定を保存すると 5 が書かれ、以降は 3 つとも `false` になります
  （再起動は不要。実行中のサーバーで確認済み）。同じファイルにほかの不正値があれば、読み込みは従来どおり止まります。
- **直接生成と保存**（`StiraConfig(bc_window_size=3)`、
  `AgentConfig(stira_config_overrides={"bc_window_size": 3})`。設定の保存はすべてここを通ります）:
  従来どおり `ValueError: bc_window_size must be an integer >= 5 …` で拒否し、何も書きません。
  ここでは誰も手で値を直さないので、黙って変えるよりエラーのほうが分かりやすいためです。

この種の値はすべて丸めます（`"20"`・`20.0`・`null` は既定の 20 ではなく 5 になります）。敏感な
側の窓に倒すためで、起きたことはフラグが示します。警告は 1 プロセスにつき 1 回で、ファイルごとではありません。

起動時に失敗でなく丸めるのは、手編集のファイルでサーバーが止まらないようにするためです。
フラグ・ログ・`/api/config` の丸めた値があるので、補正が黙って行われることはありません。

## 既知の制限（ここでは変更しない）

- ハートビートは標本を取らない。スコアが変わるのはターンのときだけ。

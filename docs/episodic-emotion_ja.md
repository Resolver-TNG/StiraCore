# エピソードの感情スナップショットと、オプトインの感情近傍想起（#364）

English: [episodic-emotion.md](episodic-emotion.md)

**既定で変わること:** 新しいチャットのエピソードは `episodic-summary/v2` になり、保存時点の
感情を記録したサイドカーが付く。**変わらないこと:** エピソードの本文、`memory_read` /
`memory_search` の出力と一致判定、prompt、Harvest、Neural State、`EMOTION_MATRIX`、分類器。
感情による想起は、どこからも呼ばれないライブラリ関数としてだけある。モデル呼び出し・トークン消費なし。

**オプトアウト:** `agent-config.json` の `episodic_emotion_snapshot: false`、または
`STIRA_EPISODIC_EMOTION=0`（空でない値は設定より優先: `1`/`true`/`yes`/`on` で ON、それ以外は
OFF）。チャットサーバーはエピソードを書くたびにスイッチを読み、`agent-config.json` が読めなければ
OFF とみなす。Discord/Slack の runtime は会話の開始時に設定を、毎ターン環境変数を読む。OFF では新しい
エピソードは `episodic-summary/v1` で、記録は書かない。書き済みの記録は削除するまで残る（後述）。

## 記録する内容

ターンが返った直後（`post_turn` の後）に `capture_emotion_snapshot(agent, turn_id=...)` が読み取りだけで写す:

| 項目 | 出所 | 保存形式 |
|---|---|---|
| `state.trust` … `state.satisfaction`（6次元） | `agent.engine.state`（`engine.state_lock` の中で写す） | [0, 1] の数値・**生の値**（写したまま、丸めない。ADR 0017 Q11）、または `null`。この変更より前の記録は小数3桁 |
| `category` | そのターンの分類結果（`last_turn_summary`） | `EMOTION_MATRIX` のキー。それ以外（棄権・失敗・未知ラベル）は `null` |
| `intensity` | 同上 | 0 以上の有限数・小数3桁、または `null` |
| `breaker` | `post_turn` が評価した水準 | `normal` / `warning` / `critical` / `coherence` または `null` |
| `turn` | エンジンのターン数 | 0 以上の整数または `null` |
| `turn_id` | チャット画面のターン（`turns.db`）。チャット画面から来たターンだけ | `[A-Za-z0-9_-]{1,64}` または `null` |

`stability` は6次元の一つ（`snapshot.stability`）。各項目は個別に検査し、不正なものは推測せず
`null`。記録しないもの: `bc_score`、`confidence`、`emotion_transcription`、差分、時刻（エピソード
ファイル自身の時刻がある）。

配線は2か所、引数1つずつ: チャットサーバーの runner（`dashboard/stira_server.py`）と
Discord/Slack の `integrations/runtime.ChatSession`（`turn_id` なし）。例外で終わったターンは
エピソードを書かないので何も記録しない。スナップショットが取れなければ、従来どおり v1 で書く。

## 置き場所（フロントマターに入れない理由）

エピソード層はデータ領域の床の例外で、エージェントのファイルツールが `episodic/` を読み書き
できる（#312）。`memory_read` / `memory_search` はメタデータをモデルに表示し、文字列検索の索引に
も入れる。感情値は個人情報として、データ領域の他の部分と同じ扱い（エージェントは読めない）に
する。そのため値はエピソードに**入れない**。エピソードには `format: episodic-summary/v2` だけが入る。

記録は `<memory_dir>/emotion/<sha256(key)[:32]>.json`（`forgotten/` の隣）。データ領域の中なので、
ファイルツールの読み取り・書き込みの床に入る。ディレクトリは `0700`（既存のものに group/other の
ビットがあれば `0700` に戻す）、ファイルは `0600`（POSIX）。一時ファイル → `fsync` → `os.replace`
で原子的に書き、読むのは最大 4 KiB。**シンボリックリンク:** POSIX では `O_NOFOLLOW` で開くので
辿らない。Windows には `O_NOFOLLOW` が無いため、開く前の `lstat` で拒否するだけ（ベストエフォート、
競合には弱い）。権限はデータ領域の ACL に従う。

記録は `key` と保存本文の SHA-256 が一致するときだけ、そのエピソードのものとみなす。
`read_emotion_snapshot(store, entry)` は、一致しなければ全項目 `null` と `status` を返す:

| `status` | 意味 |
|---|---|
| `stored` | 一致する記録がある |
| `v1` | v1・バージョンなしのエピソード（何も開かない） |
| `missing` | v2 だが読める記録がない（書き込み失敗・symlink・サイズ超過・破損） |
| `mismatch` | 別の本文の記録（エピソードが書き換えられた） |

エージェントはエピソードを書き換えられるが、記録は書き換えられない。本文を書き換えると一致し
なくなるので、他のターンの感情を借りることはできない。

**束縛が保証する範囲.** 記録が結び付くのはエピソードの**キーと本文**だけ。フロントマターの他の項目
（`source`、`importance`、`method`、Harvest の印など）は対象外で、本文が同じままフロントマターだけ
編集されたエピソード（エージェントのファイルツール、Harvest の印付け）は `stored` のまま読める。
つまり保証するのは「この本文が保存されたときにこの値が記録された」ことだけで、他のメタデータについて
は何も言わない。値そのものはエージェントには変えられない（記録は書き込みの床の中）。データ領域に
書き込める者は何でも変えられる。

## v1 との互換

既存のエピソードは書き換えも後埋めもしない。全項目 `null` の v1 として読め、感情想起には加わら
ない。スナップショットを渡さない呼び出し側の `FORMAT_VERSION`（v1）は変わらない。

## オプトインの想起: `recall_by_emotion(store, current, *, limit=5, window=50)`

新しい順に `window` 件（上限 1000）のエピソードを、保存された状態と `current`（`NeuralState`
または6次元のマッピング）との近さで並べる。完全なスナップショットが無いものは除く。順序は
距離 → 新しい方 → キー。返すのは `(entry, distance)` だけで、保存値は返さない。`window` 件と
その記録を読むだけで、書き込み・アクセス時刻の更新・モデル呼び出しはない。
**Stira のどこからも呼ばない**（テストで確認）。`memory_search`、prompt、Harvest は変わらない。

### 距離

6次元の差の二乗平均平方根:

```
d(a, b) = sqrt( Σ_i (a_i − b_i)² / 6 ),   i ∈ {trust, curiosity, vigilance, attachment, stability, satisfaction}
```

各次元は [0, 1] なので `d` も [0, 1]（0: 同じ状態、1: 正反対の隅）。全次元同じ重み。1/6 以外の
正規化はしない。

**許容誤差.** 感情近接の配線（ADR 0017 Q11「感情値は生の値で入れる」）以降、保存値は生の値なので、
新しい記録は誤差を足さない。それより前の記録は小数3桁なので、各次元の誤差は最大 `e = 0.0005`。RMS はノルム（スケール
したユークリッド距離）なので三角不等式より `|d(a', b) − d(a, b)| ≤ d(a', a) ≤ e = 0.0005`
（`DISTANCE_TOLERANCE`）。ランダムな 5000 組でテストしている。

### 実装しないもの

- **加点としての極性の近さ**（同じ感情価の記憶を優先）: 採らないと決定（ADR 0017 Q2）。感情価の
  指標（`valence_index`、`polarity`）はあるが、下の想起シグナルのガードと指標にだけ使う。
- 次元ごとの重み、prompt やツールでの利用。

**ADR 0017 で配線済み（オプトイン、既定 OFF）:** 連想想起の `emotion_proximity` シグナルが
`mood_similarity`（同じ RMS を気分の4次元 curiosity・vigilance・stability・satisfaction だけで
取る。trust/attachment は想起の bond 項に残す）を `s = max(0, 1 − d / 0.2)` として使い、
すでに関連のあるエピソードを並べ替える。ガードは [associative-recall_ja.md](associative-recall_ja.md)
を参照。`recall_by_emotion` そのものは今も呼ばれない。

連想想起への配線（距離・極性・上限・評価）は [ADR 0017](adr/0017-emotion-proximity-recall.md)
で決定済み（2026-10-20 Accepted。フェーズ1、既定 OFF）。

## プライバシーと削除

- 値は Stira 経由でモデルに届かない（prompt、記憶ツール、検索索引、LLM 蒸留器の prompt のどれにも
  入らない）。ファイルツールでも読めない。
- 運用者は読める（運用者のデータ領域なので）。
- ログに出るのは固定コードと例外の型だけ: `E_EMOTION_CAPTURE`、`E_EMOTION_TURN_ID`、
  `E_EMOTION_WRITE_FAILED`、`E_EMOTION_READ`、`E_EMOTION_INVALID`。
- **エピソードを削除すると記録も消える.** `MemoryStore.delete` は記録を先に消してからエピソードを
  消す。記録を消せなければ呼び出しは失敗してエピソードは残るので、やり直せば両方消える。今のところ
  エピソードを消す・移す経路は他に無い: 忘却（`--apply-forgetting`、`forgotten/`）は Semantic だけ、
  Harvest はエピソードに印を足すだけ（本文が変わらないので記録は一致したまま）。
- **孤児**（`MemoryStore` を通さずに消されたエピソード、書き換えられて一致しなくなったもの、壊れた
  ファイル、古い一時ファイル）は運用者のコマンドで探して消す:

  ```bash
  python -m stiracore.control.emotion_cli --base-dir <data dir>/memory orphans          # ドライラン。何も書かない
  python -m stiracore.control.emotion_cli --base-dir <data dir>/memory orphans --apply  # 削除
  ```

  理由: `no-episode`、`stale`、`misnamed`、`unreadable`、`temporary`（5 分より古い）、`not-a-file`
  （ディレクトリ。報告するだけで消さない）。読めないエピソードは判定しない。`--apply` は 1 ファイルに
  つき 1 行を `<memory_dir>/emotion-log.jsonl`（時刻・ファイル名・理由。`0600`）に**消す前に**追記する。
  ログが書けなければその時点で止まり（終了コード 1）、それ以上は消さない。ログは普通の JSONL で、
  ハッシュ連鎖ではない。出力はファイル名と理由だけで、値やエピソードのキーは出さない。サーバーを止める
  か、何もしていないときに実行する（所有者ロックは取らない）。
- オプトアウトしても、記録済みのものは消えない。全部消すなら `emotion/` を削除する（失われるのは
  スナップショットだけ。エピソードは `missing` として読める）。

## コスト

1ターンあたり小さなファイル（約 400 バイト）の書き込みが1回増える。モデル呼び出し・トークンなし。
想起は呼んだときだけ、最大 `window` 件とその記録を読む。

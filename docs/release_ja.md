# 公開版スナップショット

[English](release.md) | 日本語

公開リポジトリは**履歴なしのスナップショット**から作ります(#107)。追跡ファイルから [release-exclude.txt](../release-exclude.txt) を除き、新しい `git init` で1回だけコミットします。スクリプトはローカルで作るだけで、**push しません**。

```bash
python scripts/make_release_snapshot.py --persona-report        # リポ外の一時ディレクトリ
python scripts/make_release_snapshot.py --out /tmp/rel --no-smoke  # 速い。venv なし
python scripts/check_links.py                                   # 公開対象のリンク検査
```

処理の順番:

1. `git ls-files` から除外リストを引いてコピー(追跡外のローカル状態は入らない)。除外ファイルへの Markdown リンクは、コピー側だけプレーンテキストにする。
2. `git init -b main` と初期コミット「Initial public release」。
3. 秘密・内部 IP・ホームディレクトリの絶対パス・メールアドレス・内部ホストのスキャン。個人名・社名はリポジトリ外の `~/.config/stira-release/denylist.txt`(1行1語)から読み、無ければ警告して省略。レポートは種類・件数・ファイルパスだけで、**一致した値は出さない**。`tests/` の一致はマスクや SSRF テスト用のダミー値。次の2種類は**失敗**にする: 拒否リストの語(`denylist_word_N`)と `tracker_link`(所有者のリポジトリの Issue・Pull Request へのリンク。`github.com/Resolver-TNG/<repo>/issues/N` または `/pull/N`)。Issue 番号はプレーンな `#N` で書く(非公開の開発トラッカーの ID。README を参照)。
4. スナップショットの相対リンク検査。
5. クリーンな一時 venv で `pip install -e .[dev]` → `pytest tests/attack tests/test_server_startup_smoke.py`(クラウド・AWS 変数なし、空の `HOME`)。[agent-setup_ja.md](agent-setup_ja.md) も同じコマンド。その後、スモークが作ったものをすべて消す(スナップショットで `git clean -fdx` と `git checkout -- .`。`__pycache__`・`*.egg-info`・`.pytest_cache`・`.ruff_cache`・`node_modules` など)。`--no-smoke` でも実行する。
6. 以前のペルソナの語: サンプル人格リゾルバに差し替える前の、ゲーム由来のペルソナの名前をスナップショットに含めない(一覧は `scripts/release_scan.py` の `PERSONA_WORDS`。ファイル自身が一致しないよう符号化して保存)。レポートの `persona_words`(件数とパス)に1件でもあれば失敗。
7. `--persona-report`: 以前のペルソナ名を含む行(ファイル:行)。ペルソナ差し替えの計画用。`examples/personas/resolver/` はそのまま含める。
8. ファイル集合の検査: スナップショットは `git ls-files` から除外リストを引いたものと完全に一致しなければならない。差分はレポートの `file_set.extra` / `file_set.missing` に出て、1件でもあれば失敗。レポートには開発側のコミット(`source_commit`・`source_date`・`source_dirty`)も記録し、`scripts/publish_snapshot.py` が使う。

**StiraCore への改名(移行コードなし)。** チャットサーバーの既定のデータディレクトリは `.local/stira/`、エージェント ID は `stira` になりました。既存のディレクトリを使い続けるには、サーバーを止めて移動します: `mv .local/<以前の名前> .local/stira`、続けて `mv .local/stira/memory/agents/<以前の ID> .local/stira/memory/agents/stira`。

## 拒否リスト(個人名・社名・内部名)

リポジトリの外に置き、コミットしません。最初に1回作ります:

```bash
mkdir -p ~/.config/stira-release
$EDITOR ~/.config/stira-release/denylist.txt
```

1行1語で書きます(個人名・社名・内部のプロジェクト名・ホスト名・チーム名)。大文字小文字は区別せず、空行と `#` で始まる行は無視されるので、コメントだけのファイルはテンプレートとして有効です。レポートには読み込んだ語数 `denylist_words` と、一致を `denylist_word_N`(語の順番)で出し、語そのものは出しません。`denylist_words: 0`(警告あり)はテンプレートが未記入という意味です。秘密は書かないでください。

JSON レポートは表示され、スナップショット隣の `release-report.json` にも保存されます。終了コード 1 はリンク切れ、以前のペルソナの語、失敗にするスキャンの一致、スモーク失敗、ファイル集合の差のいずれか。公開リポジトリの作成・最初の push・タグは保守者の手作業です。

## 公開側の更新手順(Publishing updates)

`scripts/publish_snapshot.py` はスナップショットを公開リポジトリ `Resolver-TNG/StiraCore` の**ローカルクローン**にコピーします。標準ライブラリと git だけを使います。既定は dry-run で、検査と比較だけをしてクローンは変えません。`--commit` でコピーしてコミット、`--push`(`--commit --yes` と同時にだけ指定可)で、push する内容を表示してから `git push origin main` を実行します。ネットワークを使うのはこの手順だけです。force push はしません。

次をすべて満たさなければ拒否します(終了コード 1、理由を表示): `release-report.json` にリンク切れ・以前のペルソナの語がない、スモークが成功(`--no-smoke` のレポートは `--allow-no-smoke` が要る)、`denylist_words` ≥ 1、失敗にするスキャンの一致がない、ファイル集合の差がない、開発側の作業ツリーに未コミットの変更がない。スナップショットのディレクトリがクリーンでレポートのコミットにある。クローンの `origin` が `Resolver-TNG/StiraCore`(`--expect-remote` で変更可)、作業ツリーがクリーン、ブランチが `main`。削除されたファイルはクローンからも消します。コミットメッセージは `Sync from development snapshot <短い SHA> (<日付>)` で、本文は変更件数だけ。author と committer はクローンの git config から取り、GitHub の noreply アドレスでなければ拒否します(スクリプトがアドレスを作ることはありません)。

**初回の公開(保守者の明示の go が出てから):**

1. GitHub で空の公開リポジトリを作る(README・ライセンス・`.gitignore` は付けない)。
2. クローンし、必要なら `git switch -c main`、`user.name` と noreply の `user.email` を設定。
3. `python scripts/make_release_snapshot.py --out /tmp/rel`(スモークあり)でレポートを確認。
4. `python scripts/publish_snapshot.py --snapshot /tmp/rel --public-clone <クローン>`、続けて同じコマンドに `--commit`。
5. コミットを確認し(`git show --stat`、ツリーの目視)、手で push する(`git push origin main`)。
6. 設定: **Private vulnerability reporting** を有効化([SECURITY_ja.md](../SECURITY_ja.md) はこれを前提にしている)、説明とトピックを設定。タグとリリースノートは手作業。

**2回目以降の定期同期:** クローンで `git pull` → 新しいスナップショット → dry-run → `--commit` と push(手で、または `--commit --push --yes`)。

**公開リポジトリへの Pull Request** は開発用リポジトリに取り込み直し、`Co-authored-by:` で著者を残して、次の同期で公開側に戻します([CONTRIBUTING_ja.md](../CONTRIBUTING_ja.md))。同期に含まれたら、公開側の Pull Request は短い説明を付けて閉じます。

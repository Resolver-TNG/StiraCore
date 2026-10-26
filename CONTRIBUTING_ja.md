# StiraCore への貢献

[English](CONTRIBUTING.md) | 日本語

関心を持っていただきありがとうございます。StiraCore はプレアルファで、インターフェースはまだ変わります。

## 受け付け方

- **Issue は歓迎します**: バグ、ドキュメントの不足、質問、提案。
- **Pull Request も歓迎します。** 当面このリポジトリはメンテナの開発用リポジトリのスナップショットなので、
  採用した Pull Request は**開発用リポジトリに取り込み直し**、次の同期で戻ってきます。
  著者の表示は `Co-authored-by:` で残します。
- 大きな変更や新機能は、コードを書く前に Issue で設計を相談してください。脆弱性は非公開で報告してください:
  [SECURITY_ja.md](SECURITY_ja.md)。

## 開発環境

Python 3.11 以降([インストール](docs/install_ja.md) に Linux・macOS・Windows の手順)。venv の中で
`python -m pip install -e '.[dashboard,dev]'`。

Pull Request の前に通す検査(CI と同じ):

```bash
python -m pytest tests/ dashboard/test_multimodal.py
node --test tests/*_test.cjs
ruff check .
ruff format --check .
python scripts/i18n_extract.py --check
npm install --no-save --no-package-lock jsdom axe-core
STIRA_REQUIRE_AXE=1 python -m pytest tests/test_a11y_axe.py tests/test_ui_contrast.py -q
```

## 設計のルール

- **費用や権限を増やす動作は既定でオフ。** トークンを使う動作・権限を広げる動作は利用者が有効にするまで動かさない（ADR がコストを測ったうえで決めた例外を除く。例: [ADR 0026](docs/adr/0026-memory-index-handoff-small-windows.md) の Memory Map と引き継ぎメモ）。**見せて選ばせる:** 押す/選ぶまで何も承認・実行しない「見せるだけ」の機能（例: YOLO ボタン、Stage ボードの静的 SVG）は、オフにする手段を残したうえで既定で見せてよい（[ADR 0012 追記 2026-10-24](docs/adr/0012-yolo-mode.md)）。
- **権限は利用者の承認なしに広がらない**(ツールルール・承認・YOLO)。
- **未信頼のテキストはデータのまま。** ツール結果・取得したページ・記憶を指示や制御の権限に昇格させない
  ([ADR 0022](docs/adr/0022-trust-labels-and-taint.md))。
- **秘密をログ・プロンプト・テストに入れない。** 資格情報は環境変数の名前で参照する。
- **設計判断には ADR を添える**([docs/adr/](docs/adr/)、索引は [docs/README.md](docs/README.md))。
  背景は [設計思想](docs/design-philosophy.md)。
- **ドキュメントを同期させる。** 設定を足したら [configuration_ja.md](docs/configuration_ja.md) /
  [configuration.md](docs/configuration.md) の表を更新し、UI の文言は i18n カタログ(ja / en)の両方に入れる。
  `tests/test_docs_sync.py` が照合します。

## コミット・Pull Request・Signed-off-by

- 小さく、目的を絞り、テストを添える。PR の本文に理由と、実行した検査とその結果を書く。
  テストに書くダミーの秘密は明らかなダミー(`sk-test-…`)にする。
- ドキュメントやコードの `#123` のような番号はメンテナの非公開の開発トラッカーの番号で、
  このリポジトリの Issue ではありません(README を参照)。
- コードは Apache-2.0(inbound = outbound)、`dashboard/stage/assets/` は CC BY 4.0。CLA は無く、
  [DCO](https://developercertificate.org/) を採用: すべてのコミットに `git commit -s` で `Signed-off-by:` 行を付ける。

## 行動について

敬意を持って建設的に。善意を前提にし、議論は作業の内容に留め、嫌がらせはしないでください。

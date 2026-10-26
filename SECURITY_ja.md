# セキュリティポリシー

[English](SECURITY.md) | 日本語

## 脆弱性の報告

GitHub の **Private vulnerability reporting** を使ってください: リポジトリの **Security** タブ →
**Report a vulnerability**。脆弱性を公開の Issue・Discussion・Pull Request に書かないでください。
次の情報をできる範囲で含めてください:

- 試したバージョンまたはコミット
- 再現手順(最小の設定と、使ったプロンプトやリクエスト)
- 影響: どの境界を迂回でき、攻撃者が何を得るか
- 環境(OS、Python のバージョン、モデルプロバイダ、関係するチャットサーバーの設定)

本物の秘密・キー・個人情報は貼らないでください(明らかなダミー `sk-test-…` を使う)。
協調開示をお願いします: 修正か、合意した日付まで公開を待ってください。

## 対象バージョン

StiraCore は**プレアルファ**です。サポートするのは最新の `main` だけで、修正のバックポートはしません。
対応はベストエフォートで、返信や修正までの期限は約束しません。

## 対象となる問題

ハーネスが守ると言っている境界を迂回できること。例: 秘密のマスク、秘密パスの床と
`protected_dirs`、IdentityGuard、ツールの同意と承認(Always / Ask / Never、承認の束縛)、
YOLO の窓の制限、サーキットブレーカー、SSRF・外向き通信の制限、ローカル Web UI の
Host / Origin / CSRF 検査と API の権限。[セキュリティモデル](README_ja.md#セキュリティモデル)、
[ツール実行経路](docs/tool-execution-paths.md)、[API の権限](docs/admin-api-permissions.md)、
[隔離の段階](docs/containment_ja.md) を参照。

## 対象外・既知の限界

- ハーネスは **OS のサンドボックスではありません**([セキュリティモデル](README_ja.md#セキュリティモデル)
  に実測の制約、[隔離の段階](docs/containment_ja.md) のフェーズ 0 は観測のみ)。
- 同じ OS ユーザーで任意のコードをすでに実行できる攻撃者、または OS の侵害。
- 境界の迂回を伴わない、モデルの出力の品質や正しさそのもの。

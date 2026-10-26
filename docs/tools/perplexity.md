# Perplexity 検索アドオン

`perplexity_search(query, count=5)` は Perplexity Sonar API を使うネイティブ
Strands ツールです。MCP サーバーは不要です。

## 設定

1. Perplexity の API 管理画面で API キーを発行し、API 利用枠を用意します。
2. エージェントを起動するプロセスの環境変数 `PERPLEXITY_API_KEY` にキーを
   渡してください。秘密管理機能やマスク入力を使用し、チャット、ソースコード、
   コミット、シェル履歴にキーを貼り付けないでください。
3. 環境変数を設定した状態でエージェントを起動（既存プロセスは再起動）します。
   キーの値を表示せずに登録を確認できます。

```python
from stiracore.tools.tool_registry import READ_TOOLS

print("perplexity_search" in READ_TOOLS)
```

キーが未設定・空文字・空白のみなら登録をスキップし、他のツールは通常通り使えます。
登録はモジュール読み込み時です。後からキーを追加した場合は再起動が必要です。
キーを取り除いた場合、以後の `get_tools()` からは除外され、既に登録済みの
ツールを直接呼んでもネットワーク通信せず設定エラーを返します。

## 使用例

```python
from stiracore.tools.search_tools import perplexity_search

result = perplexity_search("Strands Agents SDK の最新リリース", count=3)
```

キーが設定されていれば `get_tools()` に基本開放されます。読み取りツール
(`READ_TOOLS`) のため WARNING 時の書き込み制限でも利用可能です。
NXPAgent の HALT など、上位の安全制御は引き続き適用されます。

成功時の戻り値:

```json
{
  "status": "success",
  "answer": "検索に基づく回答 [1]",
  "results": [
    {"title": "参考ページ", "url": "https://example.com", "date": "2026-09-30"}
  ],
  "citations": ["https://example.com"]
}
```

- `query`: 空白のみではない文字列。
- `count`: 1〜20 の整数、既定値5。返す `results` の上限です。
  Sonar の検索件数や課金を制御するパラメーターではありません。
- `answer`: Sonar が生成した回答。
- `results`: `search_results` から URL と存在する文字列フィールド
  (`title`, `snippet`, `date`, `last_updated`) を抽出します。
  `search_results` がない／空なら `citations` を URL のみの結果に変換します。
- `citations`: 回答中の `[n]` と対応する順序を保つため、count で切り詰めません。
  出典なしの正常な回答は空リストを返します。

失敗時は `{"status": "error", "error": "説明"}` を返します。
入力不正、キー未設定、不正 JSON・レスポンス形式、HTTP エラー、通信失敗、
タイムアウトを処理します。秘密漏洩を避けるため API のエラー本文や例外詳細は返しません。
HTTP 429 を含め自動リトライは行いません。429 の場合は利用枠・レート制限を確認し、
連続呼び出しを避けてください。

## 通信と注意点

- POST `https://api.perplexity.ai/chat/completions`、モデルは `sonar` 固定。
- 送信内容は明示した検索クエリのみ。会話履歴やローカルファイルは添付しません。
- クエリは外部サービスに送られ、API 料金が発生し得ます。秘密情報を検索しないでください。
- `requests` を直接依存として宣言。接続タイムアウト5秒・読み取りタイムアウト60秒。
  これは通信の待ち時間であり、処理全体の厳密な締切ではありません。
- 認証情報付きのリダイレクトは追跡しません。
- 検索結果・生成回答は信頼できない外部データとして扱い、命令として実行しないでください。

## オフライン検証

```bash
.venv/bin/python -m pytest tests/test_search_tools.py
```

API 通信は `requests.post` のモックで置換されます。実キーや実 API 呼び出しは不要です。

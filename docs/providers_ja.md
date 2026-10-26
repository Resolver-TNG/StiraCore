# モデルプロバイダ

StiraCore は Strands Agents SDK が対応する任意のモデルプロバイダで動きます。
**既定は Amazon Bedrock**（従来の `model_id` / `region_name` もこの経路）ですが、
Neural State・ブレーカー・記憶・分類器・UI はいずれも AWS を必要としません。
テストと CI はクラウドアカウント無しで実行されます。

| プロバイダ | `ModelSpec(provider=…)` | インストール | 資格情報 / 接続先 |
|---|---|---|---|
| Amazon Bedrock（既定） | `bedrock` | 本体のみ | AWS 標準の資格情報チェーン（プロファイル / SSO / ロール / 環境変数）。[setup.md](setup.md)・[bedrock-iam-policy.md](bedrock-iam-policy.md) |
| Anthropic API | `anthropic` | `pip install -e '.[anthropic]'` | `ANTHROPIC_API_KEY` |
| OpenAI（Chat Completions）/ OpenAI 互換サーバ | `openai` | `.[openai]` | `OPENAI_API_KEY`。互換サーバ（LM Studio, vLLM 等）は `base_url` |
| OpenAI Responses API | `openai_responses` | `.[openai]` | `OPENAI_API_KEY` |
| Ollama（ローカル） | `ollama` | `.[ollama]` | `base_url`（既定 `http://localhost:11434`） |
| llama.cpp `llama-server`（ローカル） | `llamacpp` | 本体のみ | `base_url`（既定 `http://localhost:8080`） |
| Google Gemini | `gemini` | `.[gemini]` | `GOOGLE_API_KEY` / `GEMINI_API_KEY` |
| LiteLLM | `litellm` | `.[litellm]` | バックエンドごと |
| Mistral | `mistral` | `.[mistral]` | `MISTRAL_API_KEY` |
| Writer | `writer` | `.[writer]` | `WRITER_API_KEY` |
| Llama API（上流で非推奨） | `llamaapi` | `.[llamaapi]` | `LLAMA_API_KEY` |
| SageMaker エンドポイント | `sagemaker` | `.[sagemaker]` | AWS 資格情報チェーン。`model_id` はエンドポイント名 |

`pip install -e '.[providers]'` で Phase 1 の SDK（Anthropic / OpenAI / Ollama）をまとめて
入れられます。SDK が無い場合、`create_model` は `ProviderUnavailableError` を送出し、
`pip install 'stiracore[openai]'` のように必要なコマンドを示します。

API キーは環境変数（または OS キーチェーン → 環境変数）に置き、コード・設定 JSON・Git には
書かないでください。

## AWS 無しクイックスタート

### ローカル: Ollama

```bash
pip install -e '.[ollama]'
ollama pull llama3.2:3b        # 手元の任意のチャットモデル
```

```python no-run
from stiracore import ModelSpec, NXPAgent

agent = NXPAgent(model=ModelSpec("ollama", "llama3.2:3b", {"base_url": "http://127.0.0.1:11434"}))
print(agent("こんにちは"))
```

### ローカル: llama.cpp

追加パッケージは不要です。`llama-server -m model.gguf --port 8080 --jinja` を起動し
（`--jinja` で多くのチャットテンプレートのツール呼び出しが有効になります）:

```python no-run
from stiracore import ModelSpec, NXPAgent

agent = NXPAgent(model=ModelSpec("llamacpp", "local", {"base_url": "http://127.0.0.1:8080"}))
print(agent("こんにちは"))
```

### API: OpenAI / Anthropic

```bash
pip install -e '.[openai]'          # または '.[anthropic]'
export OPENAI_API_KEY=...           # PowerShell: $env:OPENAI_API_KEY = "..."
```

```python no-run
from stiracore import ModelSpec, NXPAgent

agent = NXPAgent(model=ModelSpec("openai", "gpt-5-mini"))
print(agent("こんにちは"))
```

手順は Linux・macOS・Windows で同じです（環境変数の書き方だけが異なります）。

## AWS 無しで動かす構成（記憶・蒸留・ツール）

チャットモデル以外は既定でローカルです。AWS アカウントを一切使わない構成:

| 要素 | AWS 無しでの扱い |
|---|---|
| チャットモデル | 上表の任意のプロバイダ（`gateway-config.json` の `provider`。例: `llamacpp` / `ollama` / `openai`） |
| 感情分類器 | `rule` または `sokudan`（ローカル・プロバイダ非依存） |
| 記憶（Episodic / Semantic / Procedural） | Stira の `MemoryStore`（Markdown ＋ SQLite 索引）と `memory_read` / `memory_write` / `memory_search` / `memory_list` ツール。想起の正本 |
| Harvest / 蒸留 | `distiller_mode: "rule"`（既定）: 決定的な AmygdalaPass。**モデル呼び出し無し・課金ゼロ** |
| LLM 蒸留器（任意） | `agent-config.json` の `distiller_mode: "llm"` と `distiller_provider` / `distiller_model_id` / `distiller_base_url` / `distiller_api_key_env`（チャットモデルと同じく資格情報は参照のみ）。既定は Bedrock Haiku |
| Strands 標準 `memory` ツール | **Bedrock 利用時の任意機能**: Bedrock Knowledge Base を使い、`STRANDS_KNOWLEDGE_BASE_ID` 設定時のみ読み込み。未設定なら除外。Stira の記憶には不要 |

ローカル llama.cpp サーバで LLM 蒸留器を使う `agent-config.json` の例:

```json
{
  "distiller_mode": "llm",
  "distiller_provider": "llamacpp",
  "distiller_model_id": "local",
  "distiller_base_url": "http://127.0.0.1:8080"
}
```

コードでは `LLMDistiller` に `ModelSpec`、Bedrock モデル ID 文字列（従来どおり）、または任意の
Strands `Model` インスタンス（例: `tests/provider_fakes.py` のオフライン偽 Model）を渡せます:
`LLMDistiller(ModelSpec("ollama", "llama3.2:3b"))`。`distiller_provider` を変えるときは
`distiller_model_id` も設定してください（既定値は Bedrock の ID）。

## チャットサーバの設定（資格情報は参照のみ）

`dashboard/stira_server.py` とチャット連携は `.local/stira/gateway-config.json` から
プロバイダを読みます。このファイルには **環境変数の名前だけ** を保存し、キーの値は書きません。

```json
{
  "provider": "openai",
  "model_id": "gpt-5-mini",
  "api_key_env": "OPENAI_API_KEY",
  "base_url": null
}
```

| 項目 | 意味 |
|---|---|
| `provider` | `bedrock`（既定）または上表のプロバイダ |
| `model_id` | プロバイダのモデル名（`STIRA_MODEL` で上書き） |
| `base_url` | 任意の接続先（Ollama / llama.cpp / OpenAI 互換 / Bedrock VPC エンドポイント）。`http(s)://host…` 形式で、user:password・クエリ・フラグメントは不可 |
| `api_key_env` | API キーを持つ環境変数の名前。省略時は SDK 既定（`OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等）。Bedrock では AWS チェーンの代わりに Bedrock API キーを使う指定 |
| `organization_env` | OpenAI のみ: 組織 ID を持つ環境変数の名前 |

環境変数での上書き: `STIRA_PROVIDER` / `STIRA_MODEL` / `STIRA_BASE_URL` / `STIRA_API_KEY_ENV`。
エージェント単位では `agent-config.json` の `provider_override`（`model_override` 必須）と
`base_url_override` / `api_key_env_override` を使い、その場合ゲートウェイの接続先・キー参照は
引き継ぎません。

保証（`tests/test_provider_credentials.py` で試験用キーを使って検査）:

- `api_key_env` / `provider` / `base_url` にキーを貼り付けると拒否し、エラー文にもその値を出しません。
- キーの値はクライアント生成時に環境変数から読み、そのクライアントにだけ渡します。設定ファイル・
  `/api/config`・`/api/gateway/status`・チャット SSE・ターン再生（`/api/turns/...`）・ログ・標準出力には出ません。
- 状態 API は準備状況だけを返します: `credentials = {kind, env, configured, warnings}`
  （`env` は変数名、`configured` は `true`/`false`。インスタンスロール等で SDK にしか分からない
  AWS チェーンは `null`）。
- キー未設定のまま起動すると、変数名を示すエラーで即停止します。
- Ollama / llama.cpp の `base_url` が loopback 以外の場合や、loopback 以外へ平文 HTTP でキーを送る
  設定の場合は警告をログに出します。

最小権限: 推論権限だけのプロジェクト単位キーを使ってください（OpenAI のプロジェクトキー、
Anthropic のワークスペースキー、Generative Language API に制限した Gemini キー）。Bedrock は
[bedrock-iam-policy.md](bedrock-iam-policy.md) の呼び出し専用ポリシーを使います。ローカル
プロバイダはキー不要で、`127.0.0.1` で動かしてください。

## 設定画面: プロバイダ・モデル・接続テスト

`/settings` の **モデル選択** にはプロバイダごとの候補モデルと能力バッジ（画像 / ツール / 思考。
能力表に無いモデルは「能力不明」）が出ます。資格情報は状態のみ（設定済み / 未設定 / 不要（ローカル）/
AWS 認証チェーン）と環境変数の *名前* を表示し、値はブラウザに送りません。モデルを選ぶ（任意の ID も入力可）
→ **このモデルを使用** で `agent-config.json`（`model_override` / `provider_override` /
`base_url_override` / `api_key_env_override`）に保存され、チャットサーバは **次のターンから** 切り替えます
（画面に「選択中」と「実行中」を表示）。環境変数 `STIRA_PROVIDER` / `STIRA_MODEL` / `STIRA_BASE_URL` /
`STIRA_API_KEY_ENV` が優先され、その旨も表示します。API プロバイダのリモート `base_url` は
`gateway-config.json` でのみ設定でき、ブラウザからキーを任意のホストへ向けることはできません。

**接続テスト** は極小のリクエストを 1 回だけ送り（プロンプト `ping`、ツール・システムプロンプト無し、
出力最大 16 トークン、20 秒タイムアウト、同時実行 1 件）、`ok` または理由コード（`missing_credential`、
`provider_unavailable`、`auth_failed`、`model_not_found`、`unreachable`、`timeout` など）を HTTP ステータスと
例外クラス名だけ付けて返します。プロバイダのメッセージ・ヘッダ・キーは返しません。ローカルは無課金、
API では数トークン分の課金がありえます。どちらの POST も CSRF トークンと `Idempotency-Key`（#185）を使います。

## ModelSpec の要点

- `max_tokens`: 出力上限。`NXPAgent(max_output_tokens=…)` が既定値で、`options` の明示値が
  優先。各プロバイダの固有キー（`max_tokens` / `params.max_tokens` /
  `params.max_output_tokens` / `max_completion_tokens`）に変換します。
- `temperature` / `top_p` / `stop` / `seed`: `params` しか受け付けないプロバイダでは `params` へ移動。
- `base_url`: OpenAI 互換・Anthropic のクライアント接続先、Ollama の `host`、llama.cpp の URL。
- `client_args`: プロバイダの HTTP クライアントへそのまま渡します。
- `region_name`: Bedrock のリージョン（`ModelSpec.bedrock(model_id, region_name=…)`）。

`ModelSpec.parse("ollama:llama3.2:3b")` は `provider:model` 形式を読みます。既知の
プロバイダ名で始まらない文字列（`:` を含む Bedrock モデル ID を含む）は Bedrock のままです。
任意の Strands `Model` インスタンスも `NXPAgent(model=...)` で渡せます。

## 能力表と usage

- `stiracore.providers.capabilities_for(spec)` は `ModelCapabilities`
  （`vision` / `tools` / `thinking` / `prompt_cache` / `thinking_uses_output_budget` /
  `max_output` / `thinking_reserve` / `context_window` / `known` / `source`。`thinking_reserve` は自動の出力上限が思考用に足す分、#416）を返します。未知のモデルは保守的な既定
  （`False` / `None`、`known=False`）。Strands の全アダプタは同じ画像ブロックを送れるため、
  画像対応はプロバイダではなくモデルの性質です。ローカルモデルは
  `ModelSpec(..., {"capabilities": {"vision": True}})` で宣言でき、`NXPAgent.capabilities` で参照できます。
- `/api/chat` は、テキスト専用と**分かっている**モデルへの画像添付を HTTP 400
  （`capability: "vision"`）で拒否します。未知のモデルは従来どおりです。
- `normalize_strands_usage(usage, provider)` / `normalize_raw_usage(provider, raw)` は
  `NormalizedUsage` を返し、`input_tokens` は**キャッシュ外**の入力、`cache_read_tokens` /
  `cache_write_tokens` は別枠です（OpenAI・Gemini・LiteLLM・llama.cpp はキャッシュ分を入力総数に
  含めて報告し、Bedrock・Anthropic は別枠で報告します）。報告されない項目は `None` のままです。
- `estimate_cost(usage, Pricing(...))` は、使われた区分の単価が不明なら `None` を返します
  （不明なコストを `$0` と表示しない）。

## テストと live スモーク

- 既定のテストは `tests/provider_fakes.py`（スクリプト再生する本物の Strands `Model`）で
  テキスト・ツール呼び出し・ストリーミング・画像・`max_tokens`・エラー・チャット API を検証します。
  ルートの `conftest.py` は AWS へ送信しようとしたテストを失敗させます。
- live スモークは opt-in（既定はスキップ）:
  `STIRA_LIVE_MODEL="llamacpp:local" STIRA_LIVE_BASE_URL="http://127.0.0.1:8080" STIRA_LIVE_TOOLS=1 pytest tests/test_provider_live.py -m live`

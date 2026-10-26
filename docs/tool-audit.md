# ツール棚卸し (#92 / #88 / #89)

調査日: 2026-09-30。基準: main `4733bd9`。

## 調査方法と方針

ローカルに導入済みの `strands-agents 1.57.1` / `strands-agents-tools 0.8.9` /
`strands-harness 0.1.2` のモジュール一覧・ソース・SDK registry 実登録を比較した。
外部サービスへリクエストは送らず、ネットワーク系テストはモックを使う。

```python
from importlib.metadata import version
from pkgutil import iter_modules
from strands.tools import tools
import strands_tools

print(version("strands-agents"), version("strands-agents-tools"))
print(dir(tools))
print(sorted(module.name for module in iter_modules(strands_tools.__path__)))
```

`strands.tools.tools` は AgentTool / PythonAgentTool 等の実行基盤であり、
ユーザー向けツールカタログではない。カタログは別配布 `strands_tools` にある。
SDK の MCP/registry は既に利用中。Harness は optional extra のまま維持する。

**基本解放:** 新規/設定未作成時の `ToolConfig.mode` は `trust_all`。
既存ファイルに明示された `custom` は変更しない。custom の初期選択では従来通り
shell を外し、モード変更時の意図しないシェル許可を避ける。
trust_all は現在登録された全ツールを許可するが、未登録ツールの自動 import はしない。

- 入れる = 現在採用（継続・置換・今回追加を区別）。
- 保留 = 接続先/依存/適切な実行境界を決めた後に追加。永久禁止ではない。
- 不要 = 重複 API、内部ヘルパー、既存 registry 経由で扱う方が適切。
- READ = ローカル参照/対話制御。WRITE = 書込に加え外部通信・子agent実行。
- breaker は trust_all より優先。WARNING は WRITE 非公開、ToolConfig は
  CRITICAL/HALT で全非公開。NXP の実行時フィルタと既存 HALT 動作は変更しない。

## 導入済みカタログ全件判定

| モジュール | 判定 | 現在 → 今回 | 理由 / 導入条件 |
|---|---|---|---|
| a2a_client | 保留 | 未登録 | 接続先・相手agentの権限契約が必要。導入時WRITE |
| agent_core_memory | 保留 | 未登録 | AgentCoreサービスの設定と記憶の正本方針が必要 |
| agent_graph | 不要 | 未登録 | graph / SDK Graph と重複する旧経路 |
| batch | 入れる | WRITE継続 | 任意ツールの一括実行 |
| bright_data | 保留 | 未登録 | 外部検索サービス設定が必要。汎用web_searchを先行 |
| browser | 保留 | 未登録 | ブラウザ実行環境・セッション管理の設計が必要 |
| calculator | 保留 | 未登録 | 計算専用候補。今回のファイル/Web範囲外、shellで代替可能 |
| chat_video | 保留 | 未登録 | TwelveLabs設定が必要 |
| code_interpreter | 保留 | 未登録 | ローカル/リモート実行環境とファイル共有方式を選定 |
| cron | 入れる | WRITE継続 | スケジューラ変更を伴う |
| current_time | 入れる | READ継続 | 時刻参照 |
| diagram | 入れる | WRITE継続 | 図ファイル出力 |
| editor | 入れる | WRITE継続 | ファイル編集 |
| elasticsearch_memory | 保留 | 未登録 | 外部ストア設定と記憶の正本方針が必要 |
| environment | 保留 | 未登録 | 環境変数変更/認証情報露出を含むため専用UIとの契約が必要 |
| exa | 保留 | 未登録 | provider設定が必要。汎用web_searchを先行 |
| file_read | 入れる | 標準READ → ローカルREAD置換 | 同名重複を避け、UTF-8/1MiB上限を保証。旧高機能引数は非互換 |
| file_write | 入れる | WRITE継続 | ファイル書込 |
| generate_image | 保留 | 未登録 | 生成provider・保存先・費用管理を選定 |
| generate_image_stability | 保留 | 未登録 | Stabilityサービス設定が必要 |
| graph | 入れる | WRITE継続 | 子agent/任意tool実行 |
| handoff_to_user | 入れる | READ継続 | 人間への制御返却 |
| http_request | 入れる | WRITE継続 | 任意HTTP通信。web_fetchはテキスト調査向けの限定インターフェース |
| image_reader | 入れる | READ継続 | 既存呼出互換。上限付きview_imageを推奨 |
| journal | 入れる | WRITE継続 | 日誌永続化 |
| load_tool | 不要 | 未登録 | 動的ロードを分類・権限フィルタの外から行わずSDK registryへ集約 |
| mcp_client | 不要 | 未登録 | 既存register_mcp_tools/close_mcp_toolsとSDK ToolProviderで管理 |
| mem0_memory | 保留 | 未登録 | 外部記憶backend選定が必要 |
| memory | 入れる | 条件付きWRITE継続 | STRANDS_KNOWLEDGE_BASE_ID設定時のみ。未設定時はスキップ |
| mongodb_memory | 保留 | 未登録 | 外部記憶backend選定が必要 |
| nova_reels | 保留 | 未登録 | Bedrock動画生成・保存先・費用管理が必要 |
| python_repl | 保留 | 未登録 | shellと用途重複。永続REPLの寿命・隔離を定義してから導入 |
| retrieve | 保留 | 未登録 | Knowledge Baseの読み取り契約を決定後追加。外部通信なのでWRITE |
| rss | 入れる | WRITE継続 | 外部フィード通信・状態変更 |
| search_video | 保留 | 未登録 | TwelveLabs設定が必要 |
| shell | 入れる | WRITE継続 | trust_allでは初期から許可。breakerでは停止 |
| slack | 保留 | 未登録 | ワークスペース・送信権限設定が必要 |
| sleep | 保留 | 未登録 | 長時間占有を許す実行時間契約が必要 |
| speak | 入れる | WRITE継続 | 音声生成・外部サービス/デバイス操作 |
| stop | 入れる | READ継続 | 対話終了 |
| swarm | 入れる | WRITE継続 | 子agentの委任実行 |
| tavily | 保留 | 未登録 | provider設定が必要。汎用web_searchを先行 |
| think | 入れる | READ → WRITE修正 | 実装は再帰agentを生成し親のtoolsを渡す。純粋なローカル思考ではない |
| use_agent | 入れる | WRITE継続 | 子agentの委任実行 |
| use_aws | 保留 | 未登録 | 汎用AWS操作、利用アカウント/リージョン/実行権限を決める必要 |
| use_computer | 保留 | 未登録 | GUI実行環境・デスクトップセッション設定が必要 |
| use_llm | 保留 | 未登録 | use_agentと重複。任意モデル通信は導入時WRITE |
| utils | 不要 | 内部ヘルパー | toolとして登録しない |
| workflow | 保留 | 未登録 | graph/swarmと重複。ワークフロー永続化が必要になった時に選定 |

標準カタログ49項目を全件分類した。上の表は任意依存の実行成功を意味せず、
実登録では import/SDK登録に失敗した候補を従来通りログへ出す。

## 今回の追加・既存ローカルツール

| ツール/基盤 | 判定 | 登録 | 内容 |
|---|---|---|---|
| file_read(path) | 入れる | READ | UTF-8/BOM対応、1MiB、通常ファイルのみ、バイナリ拒否 |
| view_image(path) | 入れる | READ | PNG/JPEG/GIF/WebPを実データから判定、3.75MB/8000px上限 |
| web_fetch(url) | 入れる | WRITE | requestsによるbounded stream取得、HTML本文抽出、2MiB/5万文字。失敗は分類コード（[tools/web-fetch.md](tools/web-fetch.md)、#584） |
| web_search(query) | 入れる | WRITE | 未設定時は通信なしのスタブ。設定時はJSON endpointをGET |
| create_memory_tools(store) | 入れる | agent scoped | READ/WRITEは既存のagent別toolとbreaker契約を維持 |
| SDK MCP/ToolProvider | 入れる | 既存 | 未分類はWRITE、接続寿命と名前衝突処理を維持 |
| Harness file_tools | 保留 | 未登録 | agent.sandbox前提。今回は必須依存にせず小さいlocal wrapperを採用 |
| Harness web_fetch/web_search | 保留 | 未登録 | 今回は明示されたrequests/設定可能endpoint契約を採用 |
| Harness subagent | 入れる | optional adapter継続 | 既存make_subagent統合を変更しない |
| Harness programmatic_tool_caller | 保留 | 未登録 | 任意tool呼出。分類・実行境界を確認してから採用 |

### 画像出力の重要な契約

view_imageの返値は `status`, `content`, `base64`, `mime_type` を持つ。
`base64` はJSON用途の直接呼出向け。モデルに渡す `content` は
`{"image": {"format": "png", "source": {"bytes": ...}}}` というSDKネイティブ形式。
base64文字列だけをtextに返すとモデルが画像を見られないため、bytesを保持する。
画像の実SDK tool実行でこのcontentが維持されることをテストする。

### Web検索endpoint契約

プロセス環境変数 `STIRA_WEB_SEARCH_ENDPOINT` にHTTP(S) URLを設定する。
URL内の認証情報は拒否し、ambient .netrc/proxy設定は引き継がない。
サービス固有の認証・レスポンス変換は接続先adapterに任せる。

- Request: `GET <endpoint>?q=<URLエンコードされたquery>`
- Response: `{"results": [{"title": "...", "url": "https://example.test/page", "snippet": "..."}]}`
- queryは空白のみ不可、2000文字以下。返値は先頭10件、各field最大4000文字。
- endpoint未設定は明示的なerror。架空の検索結果を生成しない。
- connect/read timeoutは5/20秒。HTTP redirectは自動追跡せずerror。
- ローカルHTTP endpointも利用可。このツールは公開Web限定のSSRFサンドボックスではない。
- 取得テキストは信頼しない資料。Web内容を実行したり、追加URLへ自動接続しない。

## 検証項目

- ファイル: UTF-8/BOM、tilde、欠損/ディレクトリ/権限、サイズ境界、binary拒否。
- 画像: 4形式、偽拡張子、未対応/壊れた画像、サイズ/寸法、base64往復、実SDK実行。
- Web: plain/HTML/UTF-8、timeout/HTTP error、URL検証、redirect、サイズ/切詰め。
- 検索: 未設定オフライン、query検証、endpoint契約、JSON schema、件数上限、実SDK実行。
- 権限: trust_all初期値/保存/ロード、明示custom維持、WARNING/CRITICAL、engine WRITE分類。
- 全tests、ruff check、ruff format --checkを最終ゲートにする。

実行結果: `pytest tests/ -x` は **465 passed / 1 skipped**、
`pytest tests/ dashboard/test_settings_api.py -x` は **491 passed / 1 skipped**。
既存skipはcontext assemblerの未実装API契約テスト。ruff check / formatはclean、
pip checkも正常。ネットワーク系はモックのみで外部への実送信なし。

## Exec-class tools (#163 / #167)

`shell`, `batch`, `cron`, `use_agent`, `swarm`, `graph` and `think` can run arbitrary commands
(directly or by delegating to a tool-using agent). `think` joined the class in #145: it
re-runs the parent's tools inside a bare inner Agent without NXP hooks (IdentityGuard/HITL). They stay **registered** but are
**not exposed** by `get_tools()` / `ToolConfig.get_allowed_tools()` unless the operator
opts in with `STIRA_ENABLE_EXEC_TOOLS=1` (or `get_tools(include_exec=True)`). Enabling
logs a warning. This is Phase 1 of the exec-tools epic (#167): approval policy,
non-interactive runner, sandbox and Breaker integration follow.

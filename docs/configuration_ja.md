# 設定リファレンス

[English](configuration.md) | 日本語

チャットサーバー（`dashboard/stira_server.py`）と観測コンソール（`dashboard/app.py`）が読む設定の一覧です。環境変数、`gateway-config.json`、`agent-config.json`、`tool-config.json`、データディレクトリのファイルを扱います。既定値は `main` のコードの値で、下の *検査対象* の表は `tests/test_docs_sync.py` がコードと照合します。運用（復旧・バックアップ・`stira doctor`）は [operations_ja.md](operations_ja.md) を参照してください。

**トークンを使う機能・権限を広げる機能はすべてオプトインで、既定は OFF です**: ハートビート（#238）、スケジューラ（#239）、プロンプトキャッシュ（#254）、コンテキスト管理（#258）、実行系ツール（#163/#167）。押す/選ぶまで何も承認・実行しない「見せるだけ」の機能は既定で見せます（『見せて選ばせる』、[ADR 0012 追記 2026-10-24](adr/0012-yolo-mode.md)）: YOLO ボタンと Stage ボードの静的 SVG（`ui.board_svg`）。承認を与える・迂回する・自動承認する環境変数はありません。`STIRA_HITL_AUTO_APPROVE` や `BYPASS_TOOL_CONSENT` のような名前が Stira のゲートに影響しないことはテストで固定しています。（YOLO の許可はそれ自体では何も承認しない。ADR 0012 の YOLO ボタンを出すだけで、窓を開くのは人間が押して確認したとき。`STIRA_YOLO_ALLOW` か `gateway-config.json` の `yolo.allow` で変えない限り `external` で、`off` にするとボタンが消える。許可がある間は、緩める保存・`RECOVER`・スケジューラの作成/再開に人間の確認が加わる。既定はページ上のチェックボックス、`STIRA_YOLO_HOST_CONFIRM` を設定するとホスト側。カードの承認はボタン 1 回のまま。）

## 設定の出どころ

優先順位: **環境変数 > `agent-config.json` > `gateway-config.json` > コードの既定値**（`config/resolver.py`）。ハートビート・プロンプトキャッシュ・コンテキスト管理のスイッチはサーバー起動時に一度だけ読みます。モデル／プロバイダの選択とツールルールは、設定ハブで保存した次のターンから効きます。

場所（`stira_server`）:

- 設定ファイル: `.local/stira/gateway-config.json`、`agent-config.json`、`tool-config.json` — **カレントディレクトリ基準**。
- データディレクトリ: `<リポジトリ>/.local/stira/` — カレントディレクトリに関係なく **リポジトリ基準**。
- `STIRA_DATA_DIR`（絶対パス）を設定した場合: データディレクトリ**と** 3 つの設定ファイル（`.head`/`.locked`、`settings-history.jsonl` を含む）はそのディレクトリになり、カレントディレクトリに左右されません。未設定または空なら上の 2 行のまま（変更なし）。移動・コピーは一切しません。既存のファイルは元の場所に残ります。移す場合はサーバーを止め、ディレクトリ全体（全ファイル一緒）を移してください（`.head` の無い `tool-config.json` はルールをロックします。[operations_ja.md §8](operations_ja.md)）。

両者が同じディレクトリを指すよう、サーバーはリポジトリ直下から起動してください（`scripts/stira_ctl.sh` / `.ps1` はそうします）。別の場所から起動すると、設定は `./.local/stira/` から、データはリポジトリから読み、`tool-config.json` が無い場合は初回用の安全なルールで起動します（#272）。このときサーバーは起動時に `WARNING: the configuration files are not in the data directory`（パスは出さない）を表示し、`/api/gateway/status` は `config_location.matches_data_dir: false` を返します（`config_location.data_dir` は `"default"` か `"STIRA_DATA_DIR"`、パスは出さない）。既定の場所そのものは変えていません。

設定画面から変えられる設定・保存した値がいつ効くか・どの環境変数が勝つかは [settings-surface.md](settings-surface.md)（#468。`config/settings_surface.json` から生成。下の表の全フィールド・全変数を分類済み）。設定画面にも同じ情報を出します（#469）: 保存済みで再起動待ちの変更があるときのバナーと、読み取り専用の「設定の場所・整合」カード（上の場所の検査と、設定を固定している環境変数。名前だけで値は出さない）。

## 1. 環境変数（サーバー） — *検査対象*

`src/` と `dashboard/` が読むもの（テストは含みません）。「未設定」は変数が無いときの動作です。真偽のスイッチは `1` / `true` / `yes` / `on`（大文字小文字は区別しない）で ON。`STIRA_HEARTBEAT`、`STIRA_PROMPT_CACHE`、`STIRA_CONTEXT_MANAGEMENT`、`STIRA_EPISODIC_EMOTION`、`STIRA_ASSOCIATIVE_RECALL`、`STIRA_RECALL_EMOTION_PROXIMITY`、`STIRA_MEMORY_MAP` は、それ以外の空でない値で強制 OFF、空文字は「未設定」です。

<!-- env-vars:begin -->
| 変数 | 未設定時 / 既定 | 効果 | Issue / ADR |
|---|---|---|---|
| `STIRA_PORT` | gateway `port`（5555） | チャットサーバーのポート。`stira_ctl` も使う。整数であること。 | — |
| `STIRA_HOST` | `127.0.0.1` | 待受アドレス。ループバック以外は `STIRA_ALLOW_REMOTE=1` が無いと起動時に拒否。どのアドレスでも、エージェントのツールはこのダッシュボードに届かない: サーバーのプロセスは自分のポートへのループバック・未指定アドレス・自分のアドレスへの接続を拒否し（wildcard で待ち受けるときはこのホストのすべてのアドレス）、`http_request` はそうした URL を名前解決したうえで（リダイレクト先も 1 回ごとに）送信前に `E_OWN_DASHBOARD` で拒否する。YOLO の有無・ツール規則に関係なく常に有効。ほかのポート（ローカルのモデルサーバーなど）には触らない。設定項目は無い。 | #185, #495 |
| `STIRA_ALLOW_REMOTE` | 拒否 | ちょうど `1` のときだけループバック以外への待受を許可（何かを起動する前に判定し、WARNING を記録）。サーバーには **認証も TLS もない** ので、先に両方を前段に置くこと。許可リストにある `Host` 名だけを受け付けます。 | #185 |
| `STIRA_ALLOWED_HOSTS` | `localhost`、`127.0.0.1`、`[::1]` | 追加で受け付ける `Host` ヘッダー名（カンマ区切り。リバースプロキシの背後など）。 | #185 |
| `STIRA_ALLOWED_ORIGINS` | なし | 要求自身のオリジン以外に信頼する完全なオリジン（`https://名前[:ポート]`、カンマ区切り）。例: リバースプロキシの公開 HTTPS オリジン。scheme・ホスト名・実効ポートで照合し、不正な項目は WARNING を出して無視します。 | #185 |
| `STIRA_TOOL_RULES_ANCHOR_DIR` | OFF | ツールルールの巻戻し検出（オプトイン）。データディレクトリの **外** のディレクトリを指定すると、ルールの最大世代をそこにも記録する。古い `tool-config.json` と head を戻した場合、データディレクトリを作り直した場合、同じ世代で中身が違う場合、別のインストールが書いたルールの場合（アンカーと head に保つインストール ID で判定）、この環境の次の（緩めない）保存ではない新しい世代の場合、アンカーが無い／安全でない／読めない場合、ディレクトリがデータディレクトリ内の場合は、`RECOVER` で保存し直すまでロック（全ツール Never）。アンカーを作るのは人間（`stira tool-rules init-anchor`）か `RECOVER` の保存だけで、ロードは作らない。サービスの環境変数で設定する（[operations_ja.md §8](operations_ja.md#8-バックアップと復元)）。 | #273, [ADR 0010](adr/0010-tool-policy-gate-stack.md) |
| `STIRA_MODEL` | gateway `model_id` | モデル ID。`agent-config.json` の `model_override` より優先。 | #205 |
| `AWS_REGION` | gateway `region`（`ap-northeast-1`） | Bedrock のリージョン。 | — |
| `STIRA_PROVIDER` | gateway `provider`（`bedrock`） | モデルプロバイダ（[providers_ja.md](providers_ja.md)）。`provider_override` より優先。空文字: コードの既定値 `bedrock`。`stira_server` と設定 API で同じ（#272）。 | #204, #205 |
| `STIRA_BASE_URL` | gateway `base_url`（なし） | プロバイダのエンドポイント。空文字で解除。 | #205 |
| `STIRA_API_KEY_ENV` | gateway `api_key_env`（なし） | API キーを入れた変数の **名前**（キーそのものではない）。空文字で解除。 | #205 |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `MISTRAL_API_KEY`, `WRITER_API_KEY`, `LLAMA_API_KEY` | — | SDK の既定のキー変数。`api_key_env` が未設定のとき、Stira はプロバイダの変数が設定されているかだけを確認する（無ければ起動エラー）。値は表示も保存もしない。 | #205 |
| `AWS_ACCESS_KEY_ID`, `AWS_PROFILE`, `AWS_WEB_IDENTITY_TOKEN_FILE`, `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`, `AWS_CONTAINER_CREDENTIALS_FULL_URI` | — | Bedrock / SageMaker の「認証情報あり」表示のために有無だけを見る。実際の認証チェーンは AWS SDK が解決する。 | #205 |
| `AWS_SHARED_CREDENTIALS_FILE`, `AWS_CONFIG_FILE`, `HOME`, `USERPROFILE` | `~/.aws/credentials`、`~/.aws/config` | 同じ表示で AWS のファイルを探す場所。 | #205 |
| `STIRA_HEARTBEAT` | gateway `hb_enabled`（OFF） | `stira_server` のモデル非依存のバックグラウンド tick。 | #238, [heartbeat-daemon.md](heartbeat-daemon.md) |
| `STIRA_ACCESS_LOG_POLLING` | OFF（ポーリングの行は出さない） | ちょうど `1` のとき、画面のポーリングが成功した行（画面の定期ポーリングそのものの `GET` の `200`/`304` だけ: `/api/turns?limit=1`、`/api/turns/<id>/events?after_seq=<n≥1>`、`/api/hitl/pending`、`/api/hitl/history?limit=50`、`/api/gateway/status`、`/api/diagnostics/write-failures`、`/api/scheduler`、`/api/state`、`/api/history`、`/api/state/history?limit=500`。同じパスでも別のクエリの読み取りは出す）をアクセスログに再び出す。既定ではこれらの行を出さず、`stira.out.log` の WARNING/ERROR を読みやすくする。ポーリングの失敗（他のステータス）とそれ以外のリクエストは常に出る。画面のポーリングを調べるとき用。 | #369 |
| `STIRA_STAGE` | OFF（設定項目なし） | チャットサーバーの Stage ページ `/stage/`（ターン・ツール・承認・Neural State・ブレーカーのピクセルアート表示。ダッシュボードのシェル内）。書き込みは既存 API だけ: 承認（`/api/hitl/<id>/decision`）とチャット送信（`POST /api/chat`、CSRF・`Idempotency-Key`、チャット画面と同じ送信ロジック）。新しいルート・権限はない。OFF ではルートもナビ項目もなく、gateway の `ui.home: "stage"` はチャットに戻る。 | #341, #350, [stage.md](stage.md) |
| `STIRA_SCHEDULER` | OFF（設定項目なし） | 運用者が定義する定期ジョブ。チャットサーバーの Gateway action は `health_check`（モデル不使用）と `memory_harvest`（#251。rule のみの Harvest で結果を staging へ。`distiller_mode` が `"rule"` でなければ拒否）。 | #239, [ADR 0008](adr/0008-scheduler.md) |
| `STIRA_PROMPT_CACHE` | gateway `prompt_cache`（OFF） | 対象の Bedrock Claude モデルでのプロンプトキャッシュ。 | #254, [prompt-cache.md](prompt-cache.md) |
| `STIRA_PROMPT_CACHE_TTL` | gateway `prompt_cache_ttl`（`5m`） | `5m` または `1h`。 | #254 |
| `STIRA_CONTEXT_MANAGEMENT` | gateway `context_management`（OFF） | 大きなツール結果の退避＋閾値での要約。 | #258, [ADR 0005](adr/0005-context-management.md) |
| `STIRA_MEMORY_MAP` | gateway `memory_map`（ON） | プロンプトの recalled 部に Memory Map（層ごとの総数と重要な記憶の一覧）を入れる。`0` で OFF。 | [memory-index_ja.md](memory-index_ja.md) |
| `STIRA_CONTEXT_PROFILE` | gateway `context_profile`（`default`） | `default` / `small` / `tiny`: 小さい窓向けのプロファイル（[small-context_ja.md](small-context_ja.md)）。未知の名前は警告して無視する。起動時に読む。 | #368 |
| `STIRA_HANDOFF_INJECT` | gateway `handoff_inject`（ON） | 引き継ぎノート: `handoff_write` / `handoff_read` ツールと、最新のノートを recalled 部に入れる（ノートが無ければ節も無い）。`0` で両方 OFF、`1` で ON。起動時に読む。 | [handoff_ja.md](handoff_ja.md) |
| `STIRA_TASK_MODEL_TOOL_SUMMARY` | gateway `task_models.tool_summary`（`inherit`） | 処理 `tool_summary`（長いツール結果を求めに応じて要約する、#585）のモデル: `inherit`、会話と同じプロバイダのモデル ID、または `provider:model`。未知のモデル・SDK や認証情報の未設定は起動を拒否。指定したモデルが失敗したらその処理を失敗にする（別のモデルに切り替えない）。起動時に読む。 | [task-models_ja.md](task-models_ja.md) |
| `STIRA_TASK_MODEL_CONTEXT_SUMMARY` | gateway `task_models.context_summary`（`inherit`） | コンテキスト管理（#258、オプトイン）の会話要約のモデル。値と規則は上と同じ。 | [task-models_ja.md](task-models_ja.md) |
| `STIRA_EPISODIC_EMOTION` | agent `episodic_emotion_snapshot`（ON） | 新しいエピソードに保存時の感情スナップショットを記録するかどうか（#364。データ領域の床の中、prompt には入らない）。OFF ではエピソードは `episodic-summary/v1` のまま。エピソードを書くたびに読む。 | #364, [episodic-emotion_ja.md](episodic-emotion_ja.md) |
| `STIRA_ASSOCIATIVE_RECALL` | agent `associative_recall.enabled`（OFF） | オプトインの連想想起（#170。発話に合う過去のエピソードをいくつか差し出す。モデル呼び出しなし。`memory.injected` を記録）。上書きするのは `enabled` だけで、上限は `agent-config.json` の値（無ければ既定値）。設定値が不正なら OFF のまま。起動時に読む。 | #170, [associative-recall_ja.md](associative-recall_ja.md) |
| `STIRA_RECALL_EMOTION_PROXIMITY` | agent `associative_recall.emotion_proximity`（OFF） | 連想想起のオプトインの感情近接シグナル（#364、ADR 0017）。すでに関連のあるエピソードを、今の気分と保存時の気分の近さで並べ替える。1ターンで入れ替わる想起は最大1件、ブレーカーが NORMAL のときだけ。上書きするのは `emotion_proximity` だけで、連想想起が ON である必要がある。起動時に読む。 | #364, [associative-recall_ja.md](associative-recall_ja.md) |
| `STIRA_MEMORY_AUTONOMY` | agent `memory_autonomy`（OFF） | `0` / `false` / `no` / `off` で記憶の自律（ADR 0018）を、`agent-config.json` に関係なく**強制 OFF**（`memory_rate` ツールなし、記憶ジョブの種類なし、何も束縛しない）。ON にする値はない。起動時に読む。 | #251, [memory-autonomy_ja.md](memory-autonomy_ja.md) |
| `STIRA_CONTAINMENT_OBSERVE` | agent `containment`（OFF） | `0` / `false` / `no` / `off` で隔離の観測（ADR 0023 フェーズ 0）を、`agent-config.json` に関係なく**強制 OFF**（フックなし、`containment.observed` の行なし）。ON にする値はない。起動時に読む。 | #460, [containment_ja.md](containment_ja.md) |
| `STIRA_TAINT_OBSERVE` | agent `taint`（OFF） | `0` / `false` / `no` / `off` で信頼ラベルの観測（ADR 0022 フェーズ 0）を、`agent-config.json` に関係なく**強制 OFF**（フックなし、`taint.observed` の行なし）。ON にする値はない。起動時に読む。 | #458, [taint_ja.md](taint_ja.md) |
| `STIRA_ENABLE_EXEC_TOOLS` | 非公開 | 実行系（`shell`、`batch`、`cron`、`use_agent`、`swarm`、`graph`、`think`）を公開する。それでも呼び出しは毎回確認（Ask）で、Always にはできない。どのプリセットも設定しない。実行されるのは、人間がその呼び出しそのものを承認したとき（承認カード）だけ。Stira のゲートがその呼び出しに束縛した一回限りの印を出し、上流の端末確認（`shell`、`cron`）はその承認の中でだけ答え、端末は読まない。環境変数の `BYPASS_TOOL_CONSENT` と `STRANDS_NON_INTERACTIVE` は無視する。承認の無い呼び出し、同じ呼び出しの再実行、直接の `stream()`、Stira のフックの無い内側 agent からの呼び出しは理由付きで拒否し、同意ブリッジを確認できないツールは登録しない（#362）。エージェントの shell はサーバーと同じ OS ユーザーで動くため、CLI による承認（手続き記憶のルール、staging）は OS の権限以上には強くならない。承認するユーザーとは別の OS ユーザーでエージェントを動かす（#318、[tool-execution-paths](tool-execution-paths.md)）。 | #163, #167, #362, [ADR 0010](adr/0010-tool-policy-gate-stack.md) |
| `STIRA_BROWSER_PATH` | 未設定（既知の場所、次に `PATH`） | 検証用ツール `page_check`（ADR 0015 フェーズ 1〜3、追補 rev 3〜4、#449）が使う Chrome / Chromium / Edge の実行ファイル。未設定なら `/Applications` と `~/Applications`（macOS）、次に `PATH` 上の `google-chrome`・`chromium`・`microsoft-edge` などを探す。設定されていて実行ファイルでないときはツールを登録しない（黙って別のものを使わない）。ブラウザが無い、Windows（フェーズ 1）、root で動いているときはツールを登録せず、`stira doctor` が理由を出す（`page_check_*` の注記）。ブラウザは毎回新しいプロフィールで動き、すべての通信（ループバックも）は Stira 内の拒否プロキシに向く。ブラウザ内の各セッションは、Chrome が遮断の設定を受け付けたと確かめてから動く（確かめられなければ実行ごと終わり `browser_guard_unverified` を返す）。規則はフェーズ 1 では「確認」か「しない」だけ（保存済みの「常に許可」は「確認」として扱う）。ログに残る対象は `@workspace/<相対パス>` だけ。フェーズ 2: 1 回に最大 20 個の入力操作（`click`・`key`・`key_down`・`key_up`・`move`・`wait_ms`・`screenshot`・`text`。rev 3 から `type`（200 文字までを一度に入力）、`wait_for`（セレクターか文字列を最大 10 秒待つ）、`expect`（`exists`・`visible`・`text_contains`・`text_equals`・`count`。合否は Stira が判定し `assertions` に件数で返す）も。ブラウザを起動する前に確かめ、コードを実行する操作は無く、アサーションも 20 個に数える）、スクリーンショット最大 4 枚、ページのテキスト 4,096 文字まで。ページ由来の文字列は、データであると明記した `<page-content>` ブロックの中でだけモデルに渡る。フェーズ 3: 人が設定画面で追加したポートの `http://localhost:<ポート>/…` も開ける（`tool-config.json` の `page_check_ports`、既定はなし。1024〜65535、ダッシュボード自身のポートは不可。追加にはポート番号を入力する確認が要る）。プロキシはその 1 つのオリジンだけを `127.0.0.1:<ポート>` に転送する。実行系ツールが無効なら、サーバーは人が `127.0.0.1` で起動する。[tool-execution-paths](tool-execution-paths.md#page_check-verification-browser-adr-0015) を参照。 | #449, [ADR 0015](adr/0015-verification-browser-tool.md) |
| `STIRA_RUN_CHECKS` | 未設定 | `0` / `false` / `no` / `off` で検証ランナー `run_checks`（ADR 0019）を gateway の `run_checks` に関係なく**オフ**にします（ツールは登録されません）。値でオンにはなりません。起動時に読みます。 | #167, [ADR 0019](adr/0019-verification-runner.md) |
| `LANG`, `LC_ALL` | OS が設定 | ロケール名らしい値のときだけ `run_checks` の実行（ADR 0019）に渡します。それ以外の環境変数は実行に渡しません。 | #167 |
| `STIRA_YOLO_ALLOW` | 未設定 = `external`（見せるだけ。[追記 2026-10-24](adr/0012-yolo-mode.md)） | YOLO モード（ADR 0012、#365）。**既定（ADR 0012 追記 2026-10-24）:** この変数も `gateway-config.json` の `yolo.allow` も無ければ許可は `external` — ボタンと両方の範囲を見せ、人間が押して確認するまで何も承認しない。`off`（ここでも config でも）でボタンが消える。`STIRA_HOST` が wildcard のときは既定が `off` になる（起動時に 1 行）。既定では『実行系も含める』は出ない（運用者自身の `external` が要る）。**現在はフェーズ 1〜3、5a、5b、6: チャットのヘッダーと Stage の対話ボックスに `YOLO を開始` ボタンが出る（フェーズ 5b。バッジ・停止・ログ行も。試し方は [yolo-quickstart](yolo-quickstart_ja.md)）。確認された要求は 6 時間の窓を開き、その間、チャット / Stage のターンでの内部ツール（ローカルのファイル、Stira の記憶、`stop`、`handoff_to_user`）の『確認』は自動で承認される（フェーズ 5a）。値が `external` で、押したときに『外部もOK』を選んだ窓では（フェーズ 6）、外部ツール（`web_fetch`、`web_search`、`webhook_send` など）の『確認』も、引数に秘密を含まなければ自動で承認される。窓ごとの上限は `STIRA_YOLO_MAX_EXTERNAL`、宛先は鍵付きハッシュだけを記録する。引数に秘密らしきものを含む呼び出しや、非 ASCII のホストを指す呼び出しは承認カードのまま。**フェーズ 7（§9 option C、指揮官の決定 2026-10-19）:** `external` **かつ** `STIRA_ENABLE_EXEC_TOOLS` のときは、押したときに『実行系も含める』も選べる（確認ダイアログに専用のチェックボックス）。その窓では `shell` と `batch` の『確認』も、引数（コマンド文字列を含む）に秘密を含まなければ自動で承認される。窓ごとの上限は `STIRA_YOLO_MAX_EXEC`。`cron` と子エージェント系（`use_agent`、`think`、`swarm`、`graph`）は承認カードのまま。**YOLO が承認したコマンドはサーバーの OS ユーザーとして動き、Stira の保護された場所や監査を迂回しうる** — この選択ではそれを受け入れている（ADR 0012 §9）。（改訂 3 の `STIRA_YOLO_EXEC` は存在せず、読まない。）MCP ツール、Webhook・ボット・ジョブのターン、『しない』、保護された場所（床）、ブレーカーの書込ブロック、CRITICAL は常に人間の確認か拒否のまま。** 許可（この変数）だけでは何も承認しない。承認するのは人間が確認した窓だけ。 値は `off`・`internal`・`external` のどれかちょうど（空は未設定 = 既定。`on`、`1`、`Internal` などそれ以外の値は起動を拒否し exit 2。メッセージは変数名だけで値を出さない）。起動時に一度だけ読む。ハブの項目ではない。**フェーズ 7c:** YOLO の各値は `gateway-config.json` の `yolo`（設定画面）でも設定でき、この環境変数が優先される。[`yolo` セクション](#yolo-セクションadr-0012-フェーズ-7c)を参照。設定すると: `GET /api/yolo/status` ができる（許可の段階、時計の対応、`offered` と出せない理由、窓、自動承認のログ行。画面はこれだけを描く）。`/api/config`・`/api/tools`・`/api/scheduler` の保存ルートは `yolo*` を含む本文を拒否する（409 `yolo_not_settable`）。承認・編集の決定、`RECOVER`、緩める保存は、エージェントのツール呼び出しの実行中には拒否する（409 `tool_call_running`。多層防御の一枚で、人間が送った証明ではない）。サーバーのプロセスから自分のダッシュボードのポートへの接続を拒否する（ループバック、未指定アドレス、自分のアドレス。リダイレクトや名前経由も。#495 以降はこの変数が無くても起動のたびに。`STIRA_HOST` を参照）。**フェーズ 2 — 確認:** `RECOVER`、緩めるツール規則・ハブ・ロールバックの保存、スケジューラの作成・再開・確認付き変更は 428 `host_confirmation_required` を返し、人間がその要求そのものを確認したときだけ適用される（既定はページ上の確認ダイアログとチェックボックス（フェーズ 2c）。`STIRA_YOLO_HOST_CONFIRM` を設定したときはホストで: TTY で `stira approvals decide <id>`、またはデスクトップのダイアログ）。確認されるとページが同じ要求を送り直す。承認カードの承認・編集は**ボタン 1 回のまま**（フェーズ 2c。ツール呼び出し実行中の拒否だけが加わる）。`POST /api/yolo/request` は YOLO の要求を作り、同じ方法で確認する（チェックボックス 1 つ、外部もOKではもう 1 つ。またはホストで `stira yolo confirm <id>`）。**フェーズ 3 — 窓:** 確認で窓が開く（先にマーカー `yolo-window.json`、次に `audit.yolo.started`。どちらも読み戻して確認）。押した時点から 6 時間（壁時計・スリープを含む時計・含まない時計のうち最大）、5 分を超えるスリープ、`POST /api/yolo/stop`、サーバーの停止、`/new` で終わる。再起動で窓は戻らない。時計の異常、監査・マーカーの書込失敗、クラッシュでは**ラッチ**され（新しい要求を受けない）、人間がラッチを解除するまで続く（`POST /api/yolo/unlatch`、確認付きの操作）。**フェーズ 5a — 自動承認:** 1 回ごとに `audit.yolo.auto_approved` を書き（コードと鍵付きハッシュだけで、引数は書かない）、実行の直前に一度だけ消費する。監査の書込に失敗したら何も承認せず、窓を終える（ラッチ）。上限は使用済みと予約中を分けて数える（全部予約中ならその呼び出しは確認に回り窓は続く。全部使用済みなら窓が終わる）。ブレーカーが NORMAL 以外になると窓は一時停止し（『確認』はすべて確認に回る）、NORMAL が 10 分続くと再開する。1 時間に 4 回目の一時停止で窓は終わる。ツール一覧とプロンプトは変わらない。時計を実測したプラットフォームでだけ提供する（現在は macOS。それ以外は `clock_not_measured`）。**YOLO を使うときはリスナーをローカルアドレス 1 つに限る:** 明示した `internal` / `external`（変数か config）と wildcard の `STIRA_HOST`（`0.0.0.0`、`::`）の組み合わせは起動を拒否する（exit 2、アドレスを含まない固定メッセージ）。既定か `off` なら wildcard の bind は従来どおり（そこでは既定が `off`）。 | #365, [ADR 0012](adr/0012-yolo-mode.md), [admin-api-permissions](admin-api-permissions.md) |
| `STIRA_YOLO_MAX_AUTO_APPROVALS` | `2000`（YOLO の許可があるとき） | YOLO の窓ごとの自動承認の上限（ADR 0012 Open (5)）。1〜10000 の整数。許可が `off` のときに設定、または範囲外は起動を拒否。窓の自動承認で数える（フェーズ 5a。使用済み + 予約中）。 | #365 |
| `STIRA_YOLO_MAX_EXTERNAL` | `200`（上限以下） | 窓ごとの外部ツールの上限。0〜上限の整数。拒否は上と同じ。外部ツールの自動承認（ティア external、フェーズ 6）1 回ごとに 1 つ使う。使い切ると、窓は続いたまま外部ツールは確認に戻る。`0` なら外部ツールは常に確認。 | #365 |
| `STIRA_YOLO_MAX_EXEC` | `200`（上限以下） | 窓ごとの実行系の上限（ADR 0012 フェーズ 7、§9 option C）。0〜上限の整数。拒否は上と同じ。押したときに『実行系も含める』を選んだ窓でだけ使う（`STIRA_YOLO_ALLOW=external` と `STIRA_ENABLE_EXEC_TOOLS` が必要）。`shell` / `batch` の自動承認 1 回ごとに、これと上限を 1 つずつ使う。使い切ると、窓は続いたまま実行系は確認に戻る。`0` なら窓は開くがコマンドは常に確認。 | #365, [ADR 0012](adr/0012-yolo-mode.md) |
| `STIRA_PROGRESS_REPORTS` | OFF | YOLO の窓の中間報告（ADR 0014 フェーズ 1、#451）。`1` / `0`（`true` / `false` も可）。YOLO 開始ダイアログの『N 分ごとに中間報告する』チェックの初期値で、開く窓についてはそのチェックが決める。区切り（ターンの開始か人間の最後の操作から `STIRA_PROGRESS_INTERVAL_MIN` 分）で次のツール呼び出しを取り消し、エージェントが短い報告を書いてターンを終える（`stop: "checkpoint"`）。画面に報告と『続ける / 方向を直す / ここで止める』が出る。対象は開いている窓の中の人間のチャット / Stage のターンだけ。`STIRA_YOLO_ALLOW` が無ければ何も変わらない。`gateway-config.json` の `progress` でも設定でき（[下記](#progress-セクションadr-0014)）、この変数が優先。 | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_INTERVAL_MIN` | `30` | ターンの開始か人間の最後の操作（メッセージ、返答、承認カードの決定）から区切りまでの分。10〜360（範囲外は丸めて警告 1 回）。 | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_CONTINUE_AFTER_MIN` | `10` | 返答がないまま経つとサーバーが自分で続ける分。**報告 1 回につき 1 回だけ**、窓が `active`・ブレーカーが NORMAL・ラッチなしの間だけ（満たさなければ取り消して返答を待つ。指揮官の決定 2026-10-19）。1〜120。 | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_MAX_AUTO_CONTINUES` | `3` | 人間の返答なしに続けて自動継続する回数の上限。超えたら報告は返答を待つ。`0` は自動で続けない。0〜10。 | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_COUNT_TOOLS` | `0`（OFF） | フェーズ 4: ターンの開始か人間の最後の操作からツール呼び出しがこの回数に達したときも区切りにする（次の呼び出しを取り消す。`trigger: "count"`、`count: "tools"`）。0〜1000。 | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_COUNT_TOKENS` | `0`（OFF） | フェーズ 4: ターンの開始か人間の最後の操作からのモデルのトークン（終わったモデル呼び出しの入力 + 出力）がこの量に達したときも区切りにする（`count: "tokens"`）。0 か 1000〜10,000,000（それより小さい正の値は 1000 に丸める）。 | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_PROGRESS_DECLARED` | `0`（OFF） | フェーズ 3: AI 自身が `handoff_to_user(breakout_of_loop: true)` で「ひと区切り」を宣言できる。その呼び出しはルール・承認カード・YOLO の自動承認より前に取り消し、メッセージ（秘密値をマスク、コードブロックを除去、長さを制限）でターンを終え、報告のメモにする（`trigger: "declared"`）。宣言による報告は 10 分に 1 回まで（それより早い宣言は実行せず、ターンは続く）。報告 ON の窓が開いている間だけ、静的プロンプトの末尾に固定の案内文を 1 段落足す（毎ターン同じバイト列。窓の開始・終了のときだけ現れ / 消える）。`1` / `0`。 | #451, [ADR 0014](adr/0014-progress-report-phase.md) |
| `STIRA_YOLO_HOST_CONFIRM` | 未設定 = `browser`（YOLO の許可があるとき） | 確認の手段（ADR 0012 §8b、Open (12)、改訂 6〜7）。**未設定（または `browser`）: ダッシュボードで確認する** — 固定フォーマットの確認ダイアログとチェックボックス（外部もOKではもう 1 つ）。ホスト側には何も出ない。**ホストの手段を設定するとホストでの確認が必須**になり、ブラウザでの確認は拒否する（409 `host_confirm_on_host`）。`auto` = サーバーがその人の macOS デスクトップのセッションで動いていればネイティブダイアログ＋OS 認証（Touch ID / アカウントのパスワード）、それ以外はホストコマンド。`command` = 常にホストコマンド（`stira approvals decide`・`stira yolo confirm`。TTY と確認語の入力が要る）。ダイアログを一切出さない設定でもある。`dialog` = 使えればダイアログ、使えなければコマンド。実行系ツールを有効にしているなら、承認したコマンドが答えられないのは**起動時に実際に選ばれた** OS 認証付きダイアログだけ。`dialog`（や `auto`）の設定だけでは保証されない: ダイアログが使えない環境（SSH、デスクトップのセッションなし）ではコマンドになり、そのコマンドには承認したコマンドが答えられる。実行系ツールが有効で、使われる手段がダイアログでないときは、起動時に使われる手段と設定値を分けて警告する。それ以外の値や、許可が `off` のときの設定は起動を拒否。Linux / Windows のダイアログは未測定のためコマンドを使う。 | #365, [ADR 0012](adr/0012-yolo-mode.md) |
| `SSH_CONNECTION`, `SSH_TTY` | （sshd が設定） | YOLO の許可があるとき（ホスト確認の手段を決めるとき）だけ読む。どちらかがあればサーバーは本人のデスクトップのセッションにいないとみなし、ホスト確認はダイアログでなくコマンドを使う。Stira のために設定するものではない。 | #365 |
| `BYPASS_TOOL_CONSENT` | 無視 | 上流の `file_write`、`shell`、`cron`、`http_request` ツールが読む変数（`shell` は `STRANDS_NON_INTERACTIVE` も読む）。Stira はその参照を差し替えており、プロセスの実際の値は無視され、同意は Stira の承認ゲートからだけ来る。設定しても何も変わらない。`http_request` の POST/PUT/PATCH/DELETE は、人がその呼び出しそのものを承認したとき（確認ルール + 承認カード）だけ送られ、承認が無ければ理由付きで拒否する（#547）。GET は変わらない。 | #242, #246, #362, #547 |
| `STIRA_WEB_SEARCH_ENDPOINT` | 未設定 | `web_search` の JSON 検索エンドポイント。未設定（または空白だけ）なら `web_search` は**どのエージェントにも渡さない**（#583）。名前と保存済みの規則はそのまま（プロファイルの「毎回確認」も残る）で、設定画面と `GET /api/config/tools` では「未設定」（`unavailable_reason: "not_configured"`）と出し、エージェントの Tool Availability はこの変数名を示す。既定で外部プロバイダには接続しない。 | #583 |
| `PERPLEXITY_API_KEY` | ツールを提供しない | `perplexity_search` を有効にする（[tools/perplexity.md](tools/perplexity.md)）。 | — |
| `STRANDS_KNOWLEDGE_BASE_ID` | ツールを提供しない | 標準の Bedrock Knowledge Base `memory` ツールを有効にする。Stira 自身の記憶ツールには不要。 | #207 |
| `STIRA_CHAT_DATA_DIR` | `.local/chat`（カレント基準） | Slack / Discord ボットの会話ごとの状態。 | [chat-integrations.md](chat-integrations.md) |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | ボットは起動しない | Slack Socket Mode ボット。 | [chat-integrations.md](chat-integrations.md) |
| `DISCORD_BOT_TOKEN` | ボットは起動しない | Discord ボット。 | [chat-integrations.md](chat-integrations.md) |
| `WEBHOOK_SECRET_GENERIC`, `WEBHOOK_SECRET_GITHUB`, `WEBHOOK_SECRET_SLACK` | そのプロバイダは 503 | Webhook プロバイダごとの HMAC シークレット（`/webhooks/<provider>`）。 | [business-integrations.md](business-integrations.md) |
| `STIRA_DATA_DIR` | `<リポジトリ>/.local/stira`（設定ファイルはカレント基準） | `stira_server` のデータディレクトリ。設定するとその中の設定ファイルも使う。完全な絶対パスのみ（Windows の `C:data` や `\data` は拒否）、symlink は解決する（フロアは実体のディレクトリ）。空白だけの値は未設定扱い。相対パス・制御文字・ファイルシステムのルート・ホームディレクトリやその祖先・ファイルやファイルの下のパス・作成できないディレクトリは起動拒否（終了コード 2、`Stira: …`、値もパスも表示しない）。移行はしない。`stira_ctl.sh`（ロック・PID ファイル・メモリ系コマンド）、`stira_ctl.ps1`（`distill`）、`stira doctor` は従う。両スクリプトはサーバーと同じ検証（`python -m stiracore.config.data_dir_check`）を呼ぶので、受理・拒否が一致する。`--file` / `--base-dir` を取る単体 CLI は従わない。 | #272 |
| `STIRA_STATE_PATH` | `/tmp/stiracore_poc_state.json` | **観測コンソール専用**: その状態ファイル。設定画面に出る解決済みの `state_base_path` にも反映されるが、`stira_server` はそれを使わない（データディレクトリは `STIRA_DATA_DIR`）。 | #272 |
| `STIRA_GATEWAY_CONFIG_PATH`, `STIRA_AGENT_CONFIG_PATH`, `STIRA_TOOL_CONFIG_PATH` | `.local/stira/*.json`（カレント基準） | **観測コンソール専用**: 別の設定ファイル。`stira_server` は常に既定の場所を使う。 | #272 |
| `TERM_PROGRAM`, `__CFBundleIdentifier`, `XPC_SERVICE_NAME` | macOS が設定 | 権限カードと `stira doctor` で、誰の OS 権限が効くか（ターミナル・アプリ・launchd）を推定するために読むだけ。launchd のラベルは表示しない。 | #244, [ADR 0007](adr/0007-write-diagnostics.md) |
<!-- env-vars:end -->

## 2. 環境変数（スクリプトとテスト）

サーバーは読みません。

<!-- env-vars-dev:begin -->
| 変数 | 使う場所 | 効果 |
|---|---|---|
| `STIRA_PYTHON` | `scripts/stira_ctl.sh` | Python インタプリタ（既定 `.venv/bin/python`）。 |
| `STIRA_LIVE_MODEL`, `STIRA_LIVE_BASE_URL`, `STIRA_LIVE_TOOLS` | `tests/test_provider_live.py`（`-m live`） | オプトインの実プロバイダ疎通テスト（[providers_ja.md](providers_ja.md)）。 |
| `STIRA_REQUIRE_AXE` | `tests/test_a11y_axe.py` | `1` なら axe-core / jsdom が無いときスキップせず失敗させる。 |
| `STIRA_CONTRACT_REPORT` | `tests/test_tool_path_contract.py` | 実測したツール経路の表を JSON で書き出す。 |
| `STIRA_BROWSER_CHECK`, `STIRA_REQUIRE_BROWSER` | `tests/test_board_svg_browser.py`（`scripts/check_board_svg_browser.mjs`。ブラウザは `CHROME`、無ければプラットフォームごとに探す） | Stage ボードの SVG の実ブラウザ検査（#453）。Chrome / Chromium が見つかれば実行し、無ければ理由つきでスキップ。`STIRA_BROWSER_CHECK=0` でスキップ、`STIRA_REQUIRE_BROWSER=1` でブラウザが無いときスキップせず失敗させる。 |
<!-- env-vars-dev:end -->

## 3. `gateway-config.json`（`GatewayConfig`） — *検査対象*

1 台のサーバーの全エージェントで共有。未知のキーは読込み時に拒否します。秘密の値はここに保存せず、変数の **名前** だけを書きます。

「保存・表示のみ」の項目（`state_base_path`、`notify_targets`、`agent-config.json` の `hb_session_mode`・`hb_task_policy`・`autonomy_boundary`）は `GET /api/config` の `stored_only_fields` に列挙します（#272）。一覧はテストでコードと照合するので、どこかで使われ始めた項目は一覧から外れます。

<!-- gateway-fields:begin -->
| 項目 | 既定 | 効果 | 上書きする環境変数 | Issue |
|---|---|---|---|---|
| `model_id` | `"global.anthropic.claude-sonnet-5"` | チャットのモデル ID。 | `STIRA_MODEL` | — |
| `max_output_tokens` | `null` | モデル呼び出しごとの出力トークン上限。`null` = 自動: 選択中モデルの出力上限（`ModelCapabilities.max_output`）を 32000 で頭打ちにした値。思考も上限に含まれるモデルは 32000 に行の `thinking_reserve`（32000）を足した値で頭打ち。上限が記録されていないモデルはプロバイダ別の既定（ホスト型 API は `8192`、ローカルの llama.cpp/Ollama は `32000`、#416）。数値は指定どおり使う（モデルの上限を超えても検査しない）。上限に達したターンは切れたと表示され、ツール呼び出しは実行されない。[出力上限](#出力上限と途中で切れたターン)を参照。設定画面の「出力上限とコスト」で変更できる（自動が推奨の既定。モデルの文書上の上限を超える数値は警告のみで拒否しない。画面で変えるときは 1〜1000000。ファイルに既にある大きな値はそのまま残り、他の設定の保存を妨げない）。次のターンから反映。 | — | #363, #470, #416 |
| `region` | `"ap-northeast-1"` | Bedrock のリージョン。次のターンから反映: チャットサーバーは毎ターンの前にモデルの選択を解決し直し、リージョンが違えばモデルを作り直す（`AWS_REGION` が優先）。 | `AWS_REGION` | #470 |
| `provider` | `"bedrock"` | モデルプロバイダ。 | `STIRA_PROVIDER` | #204 |
| `base_url` | `null` | プロバイダのエンドポイント。API プロバイダのループバック以外の URL はここでだけ設定でき、設定画面からは設定できない。 | `STIRA_BASE_URL` | #205 |
| `api_key_env` | `null` | API キー変数の名前。 | `STIRA_API_KEY_ENV` | #205 |
| `organization_env` | `null` | OpenAI の organization 変数の名前。 | — | #205 |
| `port` | `5555` | チャットサーバーのポート。 | `STIRA_PORT` | — |
| `state_base_path` | `".local/stira/state"` | 解決済み設定に表示されるが、**どちらのサーバーも実行時には使わない**（`stira_server` のデータディレクトリは既定の場所か `STIRA_DATA_DIR`、観測コンソールは `STIRA_STATE_PATH` を直接読む）。`stored_only_fields` に載る。 | `STIRA_STATE_PATH` | #272 |
| `hb_interval_floor_sec` | `300.0` | ハートビート間隔の最短（stability が低いとき）。設定画面: 「ハートビート」欄、分単位（画面で変えるときは 1 分〜24 時間。最短が最長を超えると最長が追従。ファイルにある範囲外の値はそのまま残り、ほかの保存を止めない）。起動時に読む。 | — | #238, #472 |
| `hb_interval_ceil_sec` | `10800.0` | ハートビート間隔の最長（落ち着いているとき）。設定画面は `hb_interval_floor_sec` と同じ。最短より短くはできない（`floor_above_ceil`）。ファイルに最短 > 最長が既にあっても（省略された側は既定値で比較。明示の `null` や数値でない値は従来どおり拒否）読み込みは止まらない: ハートビートは開始せず（`HeartbeatIntervalsError`）、`/api/config` と設定ハブが `hb_intervals_reset` を返し（ログに警告）、画面はファイルの値を表示する。ほかの設定を保存してもファイルの間隔はそのまま残り、間隔（既定値でも）を保存したときだけ解除される。起動時に読む。 | — | #238, #472, #512 |
| `hb_enabled` | `false` | バックグラウンドのハートビート tick（モデルを呼ばない＝トークンの料金なし。そのためオンにする操作に確認はない）。設定画面: 「ハートビート」欄、実行状況（読み取りのみ）つき。起動時に読むので、保存した変更は再起動待ち。`STIRA_HEARTBEAT` があるとスイッチは無効になり変数名を表示、変更は拒否（`env_pinned`）。固定の扱いはほかの設定と同じ（[settings-surface.md](settings-surface.md)）。 | `STIRA_HEARTBEAT` | #238, #472 |
| `prompt_cache` | `false` | プロンプトキャッシュ（Bedrock Claude のみ）。設定画面の「出力上限とコスト」で変更できる（他のモデルでは非対応と表示）。起動時に読む。環境変数が設定されている間は、画面のスイッチを無効にして変数名（値は出さない）を表示し、変更は拒否する（`env_pinned`）。固定の扱いは他の設定と共通（[settings-surface.md](settings-surface.md)）: 「設定の場所・整合」に変数名が並び、保存した変更は再起動待ちとしてバナーに数えられる。 | `STIRA_PROMPT_CACHE` | #254, #470 |
| `prompt_cache_ttl` | `"5m"` | `"5m"` または `"1h"`。設定画面で変更でき、起動時に読む。環境変数での固定は `prompt_cache` と同じ。 | `STIRA_PROMPT_CACHE_TTL` | #254, #470 |
| `context_management` | `false` | ツール結果の退避＋要約。設定画面で変更でき、起動時に読む。環境変数での固定は `prompt_cache` と同じ。 | `STIRA_CONTEXT_MANAGEMENT` | #258, #470 |
| `context_management_options` | `{}` | 閾値の上書き（[context-management.md](context-management.md)）。未知の名前・範囲外の値は拒否。設定画面の「コンテキスト管理」の「詳細（しきい値）」で変更でき、値ごとに欄の横でエラーを出す。コードの既定と違う値だけを書く。起動時に読む。 | — | #258, #470 |
| `memory_map` | `true` | プロンプトの recalled 部に Memory Map を入れる（データとして枠で囲み、予算が足りなければ丸ごと落とす。記憶が0件なら何も足さない）。設定画面には無い。起動時に読む。 | `STIRA_MEMORY_MAP` | [memory-index_ja.md](memory-index_ja.md) |
| `memory_map_max_tokens` | `300` | Memory Map 節の大きさの上限（100〜1000 の整数。組み立て側の見積もりで数える）。起動時に読む。 | — | [memory-index_ja.md](memory-index_ja.md) |
| `jev_enabled` | `false` | `false` = 分類器「自動」（Sokudan が入っていれば Sokudan、無ければルール）。`true` = `jev_classifier` を使う。起動時に適用。`GET /api/config/jev` は選択（`auto` / `rule` / `sokudan`）、その解決結果、実行中の分類器、`restart_required` を返す。`POST {"classifier": …}` は選択そのものを保存する。 | — | #227, #162 |
| `jev_classifier` | `"rule"` | `"rule"` または `"sokudan"`。 | — | #227 |
| `notify_targets` | `[]` | 保存・表示のみ。この宛先へ通知を送る処理はない。 | — | #272 |
| `ui` | `{"home": "chat"}` | ダッシュボードの設定。`home`（`"chat"` か `"stage"`）: `/` で開くページ。`"stage"` はメニューの先頭にも Stage を置く。`STIRA_STAGE=1` のときだけ効き、OFF なら `/` はチャットのまま（起動時に WARNING）。`/chat` は常にある。起動時に読む。設定ページからは変えられない。`board_svg`（真偽値、ADR 0012 追記 2026-10-24 から**既定オン**、オフのときだけ書かれる）: 返答の ```` ```svg ```` ブロックを、許可した図形・色・文字だけで組み立て直した画像として Stage のボードに表示する（`dashboard/stage/js/svg_safe.js`。スクリプト・リンク・外部の読み込み・アニメーションなし。読めないものはコードのまま理由を表示）。設定ページの「ボード（Stage）」で変える（オフはそのまま保存、オンに戻すのは確認つき。どちらも設定履歴に残る）。Stage のページを開くたびに読む。チャット画面と Stage のログはコードのまま。 | — | #350, #453 |
<!-- gateway-fields:end -->

### `context_profile` のキー（#368）

小さい窓向けのプロファイル（[small-context_ja.md](small-context_ja.md)）。どちらも既定と違うときだけ書くので、これらを持たないファイルはそのまま（下の各節と同じ）。

| 項目 | 既定 | 効果 | 上書きする環境変数 | Issue |
|---|---|---|---|---|
| `context_profile` | `"default"` | 小さい窓向けのプロファイル（[small-context_ja.md](small-context_ja.md)）: `"default"`（プロンプトとツール一覧は変わらない）、`"small"`、`"tiny"`。システムプロンプトの予算（トークン・最近の出来事・手順のルールの件数）の既定値と、使えるツールの定義だけを送るかを決める。呼び出し側が明示した値が優先。設定画面の「出力上限とコスト」に読み取り専用で表示。`"default"` 以外のときだけ書く。起動時に読む。 | `STIRA_CONTEXT_PROFILE` | #368 |
| `tools_send_allowed_only` | `false` | いまの規則が Always か Ask のツールの定義だけを送る（Never と名前を解決できないものは送らない）。ゲートは不変で、呼び出しは今まで通り検査する。`small` / `tiny` でも ON になる。ON のときだけ書く。起動時に読む（規則そのものは毎ターン読む）。 | — | #368 |
| `tools_short_schemas` | `false` | 上流の長いツールスキーマ（`http_request`・`think`・`use_agent`・`swarm`・`graph`・`shell`・`cron`。表は `tools/compact_specs.py`）の引数の説明を短くして送る。引数名・型・enum・既定値・`required`・入れ子は不変（テスト）で、上限・既定値・トークン変数の注意は残す。OFF なら上流のスキーマのまま（バイト同一）。`small` / `tiny` でも ON になる。ON のときだけ書く。起動時に読む。 | — | #368 |

### `yolo` セクション（ADR 0012 フェーズ 7c）

`gateway-config.json` に `"yolo"` オブジェクトを置けます（既定は無し = 許可は `external`。ボタンは見えるが押して確認するまで何も承認しない。`"allow": "off"` で消える。ADR 0012 追記 2026-10-24）。各値は対応する
環境変数が未設定のときだけ使われます（値ごとに **環境変数 > 設定ファイル > 既定値**）。
`GET /api/yolo/status` の `sources` と設定画面に、どこから来た値かが出ます。書き込むのは設定画面の
YOLO 欄（`POST /api/yolo/settings`、人の保存。緩める保存は確認付き。
[admin-api-permissions](admin-api-permissions.md)）だけで、他の保存ルートは引き続き `yolo*` を拒否し、
このファイルの他の保存はセクションをそのまま残します。手で書いた値で起動が止まることはありません。
不明な `allow` は `"off"`、整数は範囲に丸め、それ以外は無視（既定値を使う）し、起動時に警告を 1 回
（コードのみ）出します。環境変数と違い、設定ファイルの上限が全体の上限を超えるときは全体の上限に丸めます。

| フィールド | 既定 | 優先される環境変数 | 使われる時点 | 受け付ける値 |
|---|---|---|---|---|
| `allow` | `"external"`（wildcard のリスナーでは `"off"`） | `STIRA_YOLO_ALLOW` | 次の起動 | `"off"`、`"internal"`、`"external"` |
| `offer_exec` | `true` | — | 次の起動 | `true` / `false`。`false` で確認ダイアログから「実行系も含める」を外す（出るのは `allow` が `external` **かつ** `STIRA_ENABLE_EXEC_TOOLS` のときだけ。後者は画面から変えられない） |
| `max_auto_approvals` | `2000` | `STIRA_YOLO_MAX_AUTO_APPROVALS` | 次の窓から | 1〜10000 の整数 |
| `max_external` | `200`（上限以下） | `STIRA_YOLO_MAX_EXTERNAL` | 次の窓から | 0〜上限の整数 |
| `max_exec` | `200`（上限以下） | `STIRA_YOLO_MAX_EXEC` | 次の窓から | 0〜上限の整数 |
| `host_confirm` | `"browser"` | `STIRA_YOLO_HOST_CONFIRM` | 次の起動 | `"browser"`、`"auto"`、`"command"`、`"dialog"` |

保存を拒否するのは: チャットのターンやエージェントのツール実行の最中、YOLO の要求・窓・ロックがあるとき
（開いている窓の値は変わらない）、環境変数で固定された値、`allow` を `off` 以外にするときにワイルドカードで
待ち受けている場合（起動できなくなるため。待受先は起動と同じ関数で読みます）。緩める保存は、ファイルを書く前に
`audit.yolo.settings_saved`（フィールド名だけで、値は書かない）として監査に記録し、記録できなければ保存しません。
ファイルが読めないときはセクション無しとして扱い、警告を 1 回出します。ファイル由来の値が YOLO を緩めているときは、
起動時に `INFO` 行を 1 行出します（フィールド名だけで、値は出さない）。エージェントのファイルツールはこのファイルを
書けません。実行系の窓で自動承認されたコマンドは、ツール規則と同じく書けます（ADR 0012 §9）。例:

```json
{"yolo": {"allow": "internal", "max_auto_approvals": 500}}
```

### `progress` セクション（ADR 0014）

YOLO の窓の中間報告（フェーズ 1・3・4、#451）。区切りは先に来たトリガーで決まり（時間 → ツール回数 → トークン）、1 つのターンで 2 回は出ません。起動時に寛容に読みます: 範囲外の整数は範囲に丸め、理解できない値は
使わず（既定値になる）、コードだけの警告を 1 回出します。値ごとに環境変数が優先。このセクションだけでモデルを
呼ぶことはありません。`enabled` は YOLO ダイアログのチェックの初期値で、窓ごとには人間のチェックが決めます。

| フィールド | 既定値 | 環境変数 | 受け付ける値 |
|---|---|---|---|
| `enabled` | `false` | `STIRA_PROGRESS_REPORTS` | 真偽値 |
| `interval_min` | `30` | `STIRA_PROGRESS_INTERVAL_MIN` | 10〜360 |
| `continue_after_min` | `10` | `STIRA_PROGRESS_CONTINUE_AFTER_MIN` | 1〜120 |
| `max_auto_continues` | `3` | `STIRA_PROGRESS_MAX_AUTO_CONTINUES` | 0〜10（`0` は自動で続けない） |
| `count_tools` | `0` | `STIRA_PROGRESS_COUNT_TOOLS` | 0〜1000（`0` は OFF） |
| `count_tokens` | `0` | `STIRA_PROGRESS_COUNT_TOKENS` | 0 か 1000〜10,000,000（`0` は OFF） |
| `declared` | `false` | `STIRA_PROGRESS_DECLARED` | bool（フェーズ 3: AI 自身の区切りの宣言） |

自動継続（指揮官の決定 2026-10-19）は、窓が `active`、報告がチャット / Stage のターンから出た、返答がない、
続けての自動継続が `max_auto_continues` 回未満、のときだけ、通常のチャットのターンを 1 つ始めます（人間の最後の
メッセージの言語で「続けてください。」/ "Please continue."）。報告の『ここで止める』『自動で続けない』、`/new`、
自動継続のターンのチャットの停止、窓の終了で取り消されます。設定画面ではまだ編集できません。例:

```json
{"progress": {"enabled": true, "interval_min": 20}}
```

### `delegation` セクション（ADR 0016）

`delegate` ツール（サブエージェント、ADR 0016 フェーズ1、#486）。**既定オン**: セクションが無ければ下の
既定値になる。既定と違う値だけが書かれ、範囲外の値は拒否する（ほかの検査対象の項目と同じく、ファイルを
読み込まない）。設定ページの「サブエージェント（delegate）」で変える（設定履歴に残る）。ターンごと・
`delegate` の呼び出しごとに読む。環境変数は無い。

| 項目 | 既定 | 受け付ける値 |
|---|---|---|
| `enabled` | `true` | 真偽値 — オン = ツールが使える。呼び出しは毎回 Ask（人の承認カード。ADR 0016 フェーズ3 までは YOLO の窓でも自動承認しない） |
| `max_tool_calls` | `40` | 1〜100 — 子 1 つのツール呼び出し（`limit_tool_calls`） |
| `max_seconds` | `600` | 10〜1800 — 子 1 つの時間（`limit_time`） |
| `max_children_per_turn` | `20` | 1〜50 — 親の 1 ターンで任せられる数（`limit_children`） |

子が使えるのは親が確認なしで使えるツールだけ、親に返るのはマスク済みで最大 2,000 字の要約だけ（「指示では
なくデータ」と書いた `<delegate-output>` ブロックの中）、子は 1 つずつ動き、子は子を作れず、親の停止（停止
ボタン・YOLO の窓の終了・サーバー停止）で子も止まる。ツールが出るのは人のメッセージで始まったチャット / Stage
のターンだけ: webhook・ボット・承認後の再開・YOLO の窓の自動継続のターンには出さない（誰も見ていないカード待ちとツール定義のトークンを避ける）。
ハートビートはモデルを呼ばない。スケジューラーがモデルを呼ぶのは memory ジョブと `agent_jobs` の実行器だけ。例:

```json
{"delegation": {"enabled": false}}
```

### `agent_jobs` セクション（ADR 0021 S2）

定期実行のエージェントジョブ（#239）: 人が登録したプロンプトを、人のいない時間にスケジューラーが実行します。**既定はオフ**で、チャットサーバーの起動時に読みます（`STIRA_SCHEDULER=1` は引き続き必要で、`STIRA_SCHEDULER=0` ならスケジューラー全体が止まります）。設定画面の「定期実行のエージェントジョブ」から設定します（設定履歴に記録。オンにする変更は確認が要ります）。既定と違う値だけが書かれます。

| 項目 | 既定 | 受け付ける値 |
|---|---|---|
| `enabled` | `false` | bool — オン = 人が Agent 層のジョブを作れる（`confirm: true`） |
| `agent_daily_token_budget` | `50000` | 1〜10000000 — UTC の 1 日ごとの、全 Agent 層ジョブ共通の上限（無制限にはできない） |

ジョブはターンと同じゲート（`delegate` の子と同じ作り: ツールのルール・書き込み/読み取りの床・IdentityGuard）を通り、使えるのは `JOB_READ_ONLY_TOOLS` だけです。モデル呼び出しとツール呼び出しのたびにサーバーのブレーカーを読みます。ジョブのツール以外の呼び出し、またはルールが確認を求める呼び出しに来たら実行はそこで止まります（`approval_required`。人を待つのは ADR 0024 phase 2 で、未実装）。モデルを呼ぶ実行はハートビートが正常なときだけ動き、`catch_up` は拒否され、Agent 層のジョブは起動のたびに一時停止で始まります（人が再開）。中断した実行をチェックポイントから再開することはありません。結果はマスクして、データであり指示ではないと示す `<job-output>` 枠に入れて記録します。例:

```json
{"agent_jobs": {"enabled": true, "agent_daily_token_budget": 20000}}
```

### `run_checks` セクション（ADR 0019）

検証ランナー `run_checks`（ADR 0019 フェーズ 0〜1、#167）。**既定はオフ**: 無い・`{}` ならツールは登録されず、
ほかは何も変わりません。`{"phase": 1}` で次の起動からオン（受け付けるのはフェーズ `1` だけ。それ以外はオフ）。
`STIRA_RUN_CHECKS=0` で強制的にオフ。手での編集のみ（設定画面のスイッチはありません）。Windows と root では登録されません。

```json
{"run_checks": {"phase": 1}}
```

モデルが選ぶのは種類（`pytest`・`unittest`・`node_test`・`ruff`・`compileall`、または `auto`）とワークスペース内の
フォルダだけで、コマンドはすべて Stira が組み立てます。フォルダの使い捨てコピーの中で、絞った環境変数・通信なし・
各種の上限で実行し、件数と最初の失敗を「データであって指示ではない」と書いた `<check-output>` ブロックで返します。
macOS では自己テストの後に生成した `sandbox-exec` の設定の中で動きます（`containment: os_sandbox`）。Linux では
`limits_only` で、防げないものをカードと結果に毎回書きます。ルールの既定は「毎回確認」。保存した「常に許可」が効くのは
確認済みの `os_sandbox` のホストだけで、それ以外では「毎回確認」として扱います（`rule_always_capped`）。
詳細は [tool-execution-paths](tool-execution-paths.md#run_checks-verification-runner-adr-0019)。

### `autonomy` セクション（ADR 0011 ステージ C、アルファ版）

目標グラントと目標の反復の実行部（[grants](grants_ja.md)。概要と既知の制約: [autonomy](autonomy_ja.md)）。
**アルファ版 — 予告なく変わることがあります。既定はオフ**: 無い・`{}`・`"stage_c": true` 以外なら、ルートも
ファイルもスレッドもありません。見える変化はこの設定そのものだけです。起動時に読みます。手での編集のみ。
この節の知らないキーは無視され、警告 `autonomy_unknown_key` になります。

```json
{"autonomy": {"stage_c": true, "run_checks_unattended": true}}
```

| キー | 既定 | 意味 |
|---|---|---|
| `autonomy.stage_c` | `false` | `true` でグラントのルートと設定ページの節を登録し、最初の発行で `grants.json` を作ります。グラントはそれだけでは何も動かしません。 |
| `autonomy.stage_c_exec` | `false` | W5: ちょうど `true` **かつ** `stage_c` がオンのとき、目標の反復の実行部とそのルート `POST /api/grants/<id>/step` を作ります: 人が「1 反復だけ実行」を押すと、セッション `grant:<id>` で反復が **1 回** 動きます（自動の次の反復はなし。確認はユーザー。ハートビートが始められるのは下の `heartbeat_start` があるときだけ）。`stage_c` が無ければオフのまま（警告 `stage_c_exec_needs_stage_c`）。オフ: 実行部もルートもスレッドもなし。 |
| `autonomy.iteration_wall_sec` | `600` | W5: 1 反復が動ける最長時間（60〜3600 秒）。過ぎると反復は終わり、目標は `blocked(iteration_timeout)`。それ以外の値は既定値（警告 `iteration_wall_sec_invalid`）。 |
| `autonomy.claim_stale_sec` | `1800` | W6c: 確認されていない完了の申告を、設定の欄と Stage のボードで「まだあなたを待っています」と表示するまでの時間（60〜86400 秒）。表示だけで、タイマーもモデル呼び出しも動作も無い。それ以外の値は既定値（警告 `claim_stale_sec_invalid`）。 |
| `autonomy.heartbeat_start` | `false` | W6d: ちょうど `true` **かつ** `stage_c_exec` がオン（さらにスケジューラーが動いている、`STIRA_SCHEDULER=1`）のとき、**発行時に自分の `heartbeat_start: true` を持つグラント**（発行時にホストで確認、あとから変更不可）を、スケジューラーのモデルを使わない `grant_tick` が始められます: スケジューラーの 1 回の起床につき最大 1 反復、人の押下と同じ `step` を通ります。`stage_c_exec` が無ければオフのまま（警告 `heartbeat_start_needs_stage_c_exec`）。スケジューラーが動いていなければ何も起こさない（警告 `heartbeat_start unavailable (reason=scheduler_off)` を 1 回。フラグ付きのグラントに `unavailable: scheduler_off`）。ハートビートのデーモン（`hb_enabled` / `STIRA_HEARTBEAT=1`、既定オフ）が無ければ起床はすべて `heartbeat_unhealthy` で見送られる（警告 `reason=heartbeat_off`、`unavailable: heartbeat_off`）。オフ: ティッカーなし、何も表示されず、応答はこれまでどおり。[grants](grants_ja.md)「ハートビート開始」参照。 |
| `autonomy.tick_min_spacing_sec` | `300` | W6d: グラントの発行、反復の終わり（だれが始めたものでも）、（W6-F、#644）グラントの最後の再開から、ハートビートで始める反復までの最短時間（30〜86400 秒。下限 30 秒は下げられない）。それ以外の値は既定値（警告 `tick_min_spacing_sec_invalid`）。 |
| `autonomy.allow_browser_confirm` | `false` | 発行・再開・確認には**ホストでの確認**（YOLO デスクの `dialog` か `command`: `STIRA_YOLO_ALLOW` + `STIRA_YOLO_HOST_CONFIRM=auto` / `dialog` / `command`）が要ります。無ければ拒否（`host_channel_required`）。`true` にすると、経路がブラウザのとき（`STIRA_YOLO_HOST_CONFIRM` が未設定か `browser` で `yolo.host_confirm` が `"browser"`、または YOLO がオフで `STIRA_YOLO_HOST_CONFIRM` とほかの `STIRA_YOLO_*` の上書き変数が未設定 — 残っていると起動を拒否、`… is set but the YOLO allowance is off`）にブラウザでの確認を受け付けます — ユーザーが選ぶ弱い層で、同じユーザーのプロセスが操作できます（起動時に記録）。設定されたホストの経路（`auto` / `dialog` / `command`）を置き換えることはありません（そちらが優先）。取り消し・一時停止・キルには不要。 |
| `autonomy.run_checks_unattended` | `true` | グラントの範囲に `run_checks` を含めてよいか。効くのは**検証済みの `os_sandbox` のホストだけ**（ADR 0019）: `limits_only` ではこの値に関係なくそのグラントを拒否します。真偽値以外は `false` として読みます。 |


### 引き継ぎノート（`handoff_inject`、`handoff_max_tokens`、`handoff_max_age_hours`）

既定値と違うときだけ `gateway-config.json` に書きます（そのため上の表の行にはありません）。`handoff_inject`
（`true`）: `handoff_write` / `handoff_read` ツールと、最新のノートを recalled 部に入れる。ノートが無い・古いときは
節も出ない。`handoff_max_tokens`（`400`、100〜1500）: これより大きいノートは丸ごと入れない（途中で切らない）。
`handoff_max_age_hours`（`72`、1〜720）: これより古いノートは入れない。起動時に読みます。`STIRA_HANDOFF_INJECT` が
`handoff_inject` より優先。[handoff_ja.md](handoff_ja.md) を参照。

### `task_models` セクション

補助の処理ごとに使うモデル（[task-models_ja.md](task-models_ja.md)）。無い・`{}`（既定。ファイルには
書かない）なら、すべての処理が会話のモデルを使います。キーは処理名（`tool_summary`、`context_summary`）で、
未知の名前は起動を拒否します。値は `inherit`、会話と同じプロバイダのモデル ID（エンドポイント・キーの変数・
リージョンを引き継ぐ）、または認証情報が設定済みの別プロバイダの `provider:model`。手で編集するか
`STIRA_TASK_MODEL_<処理名>`（処理ごとに優先）で指定し、起動時に読みます。設定画面は実効モデルと出所を
読み取り専用で表示します。

```json
{"task_models": {"tool_summary": "global.anthropic.claude-haiku-4-5-20251001-v1:0"}}
```

### `token_prices` セクション（#451）

モデルごとのトークン単価（**100 万トークンあたりの USD**）。キーはサーバーが使うモデル ID そのもの（`model_id`、
または設定画面のモデル上書き）。**Stira は単価のデータを同梱しません**: そのモデルの項目がなければ、稼働ボードは
「概算コスト: 単価が未設定」のまま、チャット画面の推定費用は `—` のままです。`input` と `output` は必須、
`cache_read` と `cache_write` は任意 — その単価がないのにプロンプトキャッシュを読み書きしたターンは計算しません
（0 円とは数えません）。手での編集のみ。起動時に読みます（保存した変更は再起動まで待ちます）。理解できない項目や値
（負の数、数値でない、知らないキー）は使わず、コードだけの警告を 1 回出します。

```json
{"token_prices": {"global.anthropic.claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75}}}
```

上の数値は形を示すだけです。単価はプロバイダーの価格表から取ってください。ボードの累計（サーバー起動から）は
**各ターンを、その `turn.completed` に記録されたモデルの単価で**計算します。計算できないターン（項目のない別の
モデル、キャッシュの単価なし）があるときは、計算できたターンの合計を出し、残りを除いたことを書きます。チャット画面の
1 つの数値は、すべてのターンを計算できたときだけ出します。

## 4. `agent-config.json`（`AgentConfig`） — *検査対象*

エージェントごと。未知のキーは読込み時に拒否します。

<!-- agent-fields:begin -->
| 項目 | 既定 | 効果 |
|---|---|---|
| `identity_anchors` | 組込みの中立なアンカー 3 つ（`DEFAULT_IDENTITY_ANCHORS`） | プロンプトに入る L3+ の制約。空にはできない。チャットサーバーは起動時に読む（#152）。 |
| `soul_path` | `null` | SOUL ファイル。`null` = 同梱のサンプル SOUL（リゾルバ）。エージェントからは常に読取り専用。 |
| `read_only_paths` | `["IPD.md", "SOUL.md"]` | 書込みの床に追加するパス。既定値と SOUL のパスは常に残る。 |
| `workspace_dir` | `null` | 書込み先 `@workspace`。`null` = `<データディレクトリ>/workspace/`（#271。このとき episodic 層と context-stash も書込み root になる）。 |
| `write_roots` | `["@workspace"]` | `file_write` / `editor` が書ける場所。どの root にも入らなければ拒否（#243）。`read_roots` と同じ秘密パスの床が書込みにも効き、root をどう広げても（`allow_broad_write_roots` で `/` にしても）書けない: `E_WRITE_SECRET_PATH`（#536）。設定画面で root を足すと確認が出て、設定履歴に残る。 |
| `write_outside_action` | `"deny"` | `"deny"` のみ受け付ける（"ask" は予約で未実装）。 |
| `allow_broad_write_roots` | `false` | `/`、ドライブのルート、ホームディレクトリやその祖先を root にするためのオプトイン。 |
| `read_roots` | `["@workspace"]` | `file_read` / `view_image` / `image_reader` / `editor` の `view` / `lsp_*` が読める場所。どの root にも入らなければ `E_READ_OUTSIDE_ROOTS`（#457）。`/` も指定できる（秘密パスの床は残る）。設定画面で root を足すと確認が出て、設定履歴に残る。 |
| `read_secret_paths` | `[]` | 秘密パスの床への追加（`E_READ_SECRET_PATH`、#457。書込みの `E_WRITE_SECRET_PATH` にも効く、#536）。パス（`~/vault`、絶対パス）か名前のパターン（`*.secret`）。組込みの床（`~/.ssh`、`~/.aws`、`.env`、`*.pem`、`id_*`、キーチェーン、ブラウザのプロファイル、`/etc/shadow` など）は外せない。 |
| `model_override` | `null` | エージェントごとのモデル（`STIRA_MODEL` に負ける）。 |
| `provider_override` | `null` | エージェントごとのプロバイダ。`model_override` が必須。 |
| `base_url_override` | `null` | `provider_override` のエンドポイント。 |
| `api_key_env_override` | `null` | `provider_override` のキー変数名。 |
| `distiller_mode` | `"rule"` | Harvest の蒸留器: `"rule"`（モデル呼び出しなし）、`"llm"`（そのモデルでまだ採点していないエピソード 1 件につき 1 回呼ぶ。切り替えると全件を採点し直す）、`"jev"`（スタブ、#106）。手動・OS のタイマーからの実行と実測のコスト: [memory-maintenance_ja.md](memory-maintenance_ja.md)。 |
| `distiller_model_id` | `"global.anthropic.claude-haiku-4-5-20251001-v1:0"` | LLM 蒸留器のモデル。 |
| `distiller_provider` | `"bedrock"` | LLM 蒸留器のプロバイダ（#207）。 |
| `distiller_base_url` | `null` | LLM 蒸留器のエンドポイント。 |
| `distiller_api_key_env` | `null` | LLM 蒸留器のキー変数名。 |
| `harvest_stage_auto` | `true` | Harvest で自動帯（0.4〜0.7）も人間のレビュー待ち（staging）に入れる（レビュー UI #193 まで）。`false`（非推奨）にすると、自動帯は人間のレビューなしで `semantic/knowledge/` に直接書かれる（#293）。 |
| `memory_retention` | `{}` | ストアごとの保持上限。`stira memory retention` だけが読む（#294）。空 = 上限なし（`memory_plans` を除く。下記）。`--apply` なしでは何も消えず、`--apply` で消したものは退避される（取り消し: `stira memory restore <id>`）。キー: `episodic`（`max_age_days`、`max_entries`、`keep_unharvested` = `true`、`honour_expires_at` = `false`）、`turns` / `tool_payloads` / `checkpoints`（`max_age_days`。checkpoints は `max_per_thread` も）、`memory_plans`（`max_age_days`、既定 `90`: 記憶ジョブの計画ファイル。既定で上限があるのはこれだけ）、`staging`（`warn_after_days`）、`scheduler_log` / `archive`（`warn_bytes`）— 最後の 3 つは報告のみ。整数 1〜1000000（`*_bytes` は 10^15 まで）。[memory-maintenance_ja.md](memory-maintenance_ja.md#保持期限削除要求出所タグ294) 参照。 |
| `harvest_job_max_episodes` | `100` | スケジュールされた `memory_harvest` の 1 回で採点するエピソード数（1〜100000）。新しい順で、残りは次回へ（#251）。rule 蒸留器のみ（`"llm"`/`"jev"` ではジョブを作成も実行もできない。`distillation_cli` を使う）。出力は `harvest_stage_auto` に関係なく常に staging。 |
| `episodic_emotion_snapshot` | `true` | 新しいエピソードごとに保存時の感情スナップショット（6次元・カテゴリ・強度・ブレーカー・ターン）を `memory/…/emotion/` に記録する。床の中で、エージェントは読めず、prompt にも記憶ツールにも入らない（#364、[episodic-emotion_ja.md](episodic-emotion_ja.md)）。`false` でオプトアウト: 新しいエピソードは `episodic-summary/v1` のまま。既存の記録は削除するまで残る（`emotion_cli orphans --apply` が消すのは孤児だけ）。`STIRA_EPISODIC_EMOTION` が優先。 |
| `associative_recall` | `{}` | オプトインの連想想起（#170）: `{"enabled": true}` で、発話に合う過去のエピソードを最大 `max_items`（3、1〜5）件・`max_chars`（600、100〜2000）文字以内で差し出す。新しい `window`（200、1〜1000）行の索引から Neural State で重み付けして選ぶ（読み取りのみ）。モデル呼び出しなし。`memory.injected` を記録する。空 = OFF。チャットサーバーは起動時に読む。`STIRA_ASSOCIATIVE_RECALL` が `enabled` より優先。`"emotion_proximity": true`（既定 `false`）で感情近接シグナルを足す（#364、ADR 0017。`STIRA_RECALL_EMOTION_PROXIMITY` が優先）。[associative-recall_ja.md](associative-recall_ja.md) |
| `memory_autonomy` | `{}` | ADR 0018 の**フェーズ 0（観測のみ）**。`{}` / 無し = OFF（`memory_rate` ツールなし、記憶ジョブの種類なし、何も束縛しない）。`{"phase": 0}` で ON: `in_turn_rating`（既定 `true`）で `memory_rate` を出す（エージェントがそのターンのエピソードを採点。保存するだけで、まだ何にも使わない）。`jobs`（`memory_rate_backfill` / `memory_consolidate` / `memory_tidy`、各既定 `false`）で、人がそのスケジューラのジョブを作れるようになる（書くのは計画ファイルだけ。モデルを呼ぶ種類はハートビートが正常なときだけ開始、ADR 0021 §8）。`daily_token_budget`（100000、0〜100000）は記憶ジョブ全体の上限。`undo_window_days`（30、フェーズ 2 用に保存のみ）。受け付けるのはフェーズ `0` だけ。起動時に読む。`STIRA_MEMORY_AUTONOMY=0` で強制 OFF。[memory-autonomy_ja.md](memory-autonomy_ja.md) |
| `containment` | `{}` | ADR 0023 の**フェーズ 0（観測のみ）**。`{}` / 無し = OFF。`{"phase": 0}` で、隔離のシグナル（ゲートがすでに出している拒否と保留: 秘密パスの読み取り、床への書込み、自分のダッシュボードへの呼び出し、外部送信の保留、連続実行、範囲外への読み書き。文面の手がかりは重み 0）を時間窓で数え、そのセッションが**仮に入るはずの**段階（`open` / `restricted` / `closed` / `quarantine`）を記録する。何も強制しない（ツールを外さない、カードを足さない、水準を動かさない）。`window_seconds`（600、60〜3600）。受け付けるのはフェーズ `0` だけ。起動時に読む。`STIRA_CONTAINMENT_OBSERVE=0` で強制 OFF。[containment_ja.md](containment_ja.md) |
| `taint` | `{}` | ADR 0022 の**フェーズ 0（観測のみ）**。`{}` / 無し = OFF。`{"phase": 0}` で、ターンごとに文脈のラベル（trusted / untrusted、sensitive、出どころ別の件数）を付け、flow 層が**仮にあったら**保留していた外部送信の呼び出しを F1〜F4 のコードで数える。何も強制しない（プロンプト・ツール結果・ゲートの判定は変わらない）。受け付けるのはフェーズ `0` だけ。起動時に読む。`STIRA_TAINT_OBSERVE=0` で強制 OFF。[taint_ja.md](taint_ja.md) |
| `tool_config` | `tool-config.json` から | 埋め込みのツールルール。サーバーが使うのは別ファイルの方（5 節）。 |
| `hb_session_mode` | `"fresh"` | 保存・表示のみ（#272）。 |
| `hb_task_policy` | `{}` | 保存・表示のみ（#272）。 |
| `autonomy_boundary` | `pr_create`、`merge_main`、`destructive`、`external_send` に `require_confirmation` | 保存・表示のみ。実行時には **強制されない**（`core/engine.py` の `TODO(#67)`、#272）。 |
| `stira_config_overrides` | `null` | `StiraConfig` の項目の上書き。**チャットサーバーのエンジンに届くのは一部だけ**: [`stira_config_overrides` とエンジン](#stira_config_overrides-とエンジン272)を参照。 |
<!-- agent-fields:end -->

### `stira_config_overrides` とエンジン（#272）

`StiraConfig` の全項目は検証・保存され、`GET /api/config` → `stira_config` に表示されます。
チャットサーバー（`stira_server`）は自前のエンジン設定を作りますが、人が保存した値がそのままエンジンの
値になります(最後の行の 2 項目を除く)。`GET /api/config` → `stira_config_apply` に、項目ごとの保存値、
実行中エンジンの値（`effective`）、`applies`、`status` が出ます。先に下の配送表で決まり、値が同じでも
`never` の項目は `applied` になりません。`next_turn` / `restart` の項目は、同じなら `applied`、違えば
`pending`、直近の閾値適用が失敗していて保存値のままでは失敗し続けるなら `failed`（`reason` 付き）。
`never` の項目は、上書きで指定があれば `not_applied`、なければ `server_fixed`（サーバーが決める）。
観測コンソールのようにエンジンが無い場合は `saved`。

| 項目 | 反映 | 備考 |
|---|---|---|
| `stability_warning`、`stability_critical`、`bc_threshold`、`stability_warning_exit`、`stability_critical_exit`、`bc_exit_threshold` | 次のターン | 各ターンの前に **1 組として** 実行中のエンジンへ反映します（完成した設定を丸ごと差し替えるので、読み手が「新しい入口と古い出口」の組を見ることはありません。設定ハブ #227/#471 で入口と出口を 1 組で編集。入口を出口以上に上げると、画面で出口も一緒に上がります）。ファイルとして正しい組（既定の出口より高い入口と、その出口を一緒に保存）が半分だけ適用されることはありません。起動時にも設定します。 |
| `bc_window_size`、`decay_min_interval_sec`、`critical_cooldown_turns`、`sycophancy_threshold`、`sycophancy_window_size`、`protected_layers`、`hitl_timeout_sec`、`hitl_max_edit_rounds` | 再起動 | 起動時に一度だけ読みます（#272）。`protected_layers` 以外は設定ハブの「詳細設定」で編集できます（#471）。画面は入力値を下記の範囲に丸めてその旨を表示し（`sycophancy_threshold` は上端が最も鈍い検知なので丸めずエラー表示）、サーバーは範囲外を拒否します。迎合警告を実質的に止める値（`sycophancy_threshold` 0.95 以上、または `sycophancy_window_size` 10 未満。エンジンは窓に 10 件たまるまで判定しません）は、ブレーカーの下限と同じく確認語の入力が必要です。エンジンが上限を持たない項目には、設定ハブが上限を設けます（#471 レビュー M-1、エンジンで計測）: `bc_window_size` と `sycophancy_window_size` は 200 以下（上限が無いと履歴全体が窓になり、落ち着いていた期間が長いほど揺れや同意の連続に気づくのが遅れます。200 なら揺れは 11 ターン以内に一貫性の線を越え、迎合警告は同意 82 回以内に出ます）、`critical_cooldown_turns` は 50 以下、`hitl_max_edit_rounds` は 10 以下。超える値はサーバーが拒否します（400 `out_of_range`、`max` 付き。ロールバックも同じ）。上限より前に保存済みの値はそのまま残し、その項目を変えたときだけ検査するので、ほかの保存は通ります。`protected_layers`・`critical_action`・`blocked_tool_names` は同じ場所に表示のみ（動作中のエンジンに問い合わせられないときはサーバー自身の選択、例: `critical_action` は `halt`）。 |
| `identity_anchors`（上書きではなくファイル自身のキー） | 再起動 | 起動時に読みます（#152）。 |
| `critical_action`、`blocked_tool_names` | 反映しない | サーバーが `critical_action="halt"` に固定し、書き込み可能なツールをすべて `blocked_tool_names` に足します。別の判断事項で、ファイルからは反映しません。上書きで指定が無ければ `status` は `server_fixed`、指定があれば `not_applied`（保存値が実行値と同じでも）。 |

安全弁は、保存と読み込みのすべてが通る検証で、反映しても変わりません: `critical_cooldown_turns` は 3 以上、
`bc_window_size` は 5 以上（[coherence_ja.md](coherence_ja.md)）、各出口閾値は入口より上、`critical` は
`warning` より下、`hitl_timeout_sec` は正で 24 時間（86400 秒）以下、`hitl_max_edit_rounds` は 0 以上、`sycophancy_window_size` は 1 以上。
**`protected_layers` は 3・4・5 を残さなければなりません**（Neural State のアーキテクチャ保護）。3・4・5 以外の正の層は
足せます（保護が増える方向だけです）。設定の保存で 3・4・5 のどれかを欠く値は拒否されます。手編集した `agent-config.json` が欠いていても
サーバーは止まらず、読み込みが **欠けた層を補い**、1 プロセスにつき 1 回だけ警告をログに出し
（`… did not hold layers 3-5; they were added [protected_layers_clamped]`。値は出しません）、
`/api/gateway/status`・`/api/config`・設定ハブの状態に `protected_layers_clamped: true` を出します
（追加のみ。補っていなければ `false`。ファイルを読んで出すので、補った一覧を書き込む次の保存で消えます）。
正の整数のリストでないなど、ほかの不正値は従来どおり読み込みを止めます。

**`hitl_timeout_sec`**（承認が待つ時間）は、承認に届く前に範囲が決まります。24 時間を超えれば 86400 秒に切り詰め、
手編集で正の有限数でない値（0、負、NaN、文字列、`null`、`true`）が入っていれば既定の 120 秒にします。どちらの場合も
サーバーは起動し、1 プロセスにつき 1 回だけ警告（`[hitl_timeout_clamped]`、値は出さない）をログに出し、
`/api/gateway/status`・`/api/config`・設定ハブの状態に `hitl_timeout_clamped: true` を出します（ファイルを読んで
出すので、次の保存で消えます）。`stira_config_apply` と `stira_config` には範囲内にした値が出ます。保存や直接の生成では
このような値は拒否されます。

**ブレーカーの入口/出口の組**（#512）: 手編集した（または別の版が書いた）ファイルの 6 つの閾値が組の規則に反する場合
（出口が入口以下、`critical` が `warning` 以上、`critical` の出口が `warning` の出口以上。省略した値は既定値として数えます）、
設定 API が全部 503 になったりサーバーが起動できなくなったりはしません。読み込みは 6 つとも既定値で動き、1 プロセスにつき
1 回だけ警告（`[breaker_pairs_reset]`、値は出さない）をログに出し、`/api/config` と設定ハブの状態に
`breaker_pairs_reset: true` を出します。設定ページのブレーカー欄はファイルの値（省略分は既定値）を表示し、注記を出し、
反映の状態は `failed`（`reason: "breaker_pairs_reset"`）です。動作中のエンジンは、各ターンで「それまでの値」と「既定値」の
項目ごとに厳しい方（高い方）を使うので（6 つとも高いほど厳しい: 安定度も一貫性スコアも、入口を下回ると入り、出口を上回るまで抜けません）、既定値より緩くも、動いていた値より緩くもなりません（再起動後は既定値）。
ほかの設定（詳細設定・書き込み先・別ファイル・モデル）を保存してもファイルの 6 つの値はそのまま残り、閾値を動かして
正しい組を保存したときだけ直ります（閾値を 1 つでも動かすと 6 つ全部を検査します）。値が `null`・文字列・真偽値・
0〜1 の範囲外のときは置き換えず、従来どおり読み込みを止めます。保存や直接の生成では逆転した組は拒否されます。

起動時、`never` の行に当たる
保存項目があれば、その名前（値は出さない）を `WARNING` で 1 行出します。

### Identity Anchors（#152）

- **既定値。** `identity_anchors` キーが無ければ、組込みの中立なリスト（`DEFAULT_IDENTITY_ANCHORS`、3 項目）を使います。
  キーがあれば、組込みと同じ内容でも常にそちらが優先されます。チャットサーバーはファイルを **起動時に** 読むので、
  編集は再起動後に反映されます。起動時の表示は `Identity anchors: default (3)`、`configured (N)`、
  `configured (3, same as built-in)` のいずれかで、キーが無いときは固定コード `identity_anchors_default` の警告を
  1 回だけ出します（パスは出しません）。
- **#152 より前の版から更新する場合。** 以前の版の組込みリストはペルソナ固有の別の内容で、`agent-config.json` に
  `identity_anchors` キーの無い環境では中立のリストに切り替わります。以前のアンカーを使い続けるには、**更新の前に**
  （古い版が入っている状態で）書き出して `agent-config.json` に入れます:

  ```sh
  python -c "import json; from stiracore.core import StiraConfig; print(json.dumps({'identity_anchors': StiraConfig().identity_anchors}, ensure_ascii=False, indent=2))"
  ```

  出力の `identity_anchors` をチャットサーバーの `.local/stira/agent-config.json` に書き（ファイルが無ければ作る。
  書かないキーは既定値のまま）、再起動します。更新後の場合は、以前に動かしていた版の
  `src/stiracore/core/engine.py` にある `StiraConfig.identity_anchors` が以前のリストです。CHANGELOG ファイルは
  無いので、この節を変更の記録とします。
- **アンカーとは何か。** WARNING / CRITICAL / COHERENCE のときに system prompt に入る文章です。宣言であって、
  何も強制しません。特に既定の 3 項目目（「… change only with human approval」）はプロンプト上の宣言です。
  チャットサーバーでエージェントのツールを SOUL ファイルや設定ファイルから遠ざけているのは書込みの床
  （[soul-read-only.md](soul-read-only.md)、ADR 0010）で、アンカーではありません。
- 観測コンソール（`dashboard/app.py`）は、チャットエージェントが接続されていればその実行中の値を、そうでなければ
  保存された値を「チャットサーバーの次回起動で反映」と明示して表示します。

## 5. `tool-config.json`（ツールルール）

レジストリのツールごとに 1 つのルール — **Always**、**Ask**、**Never** — を `/settings#sec-tools` で編集します。本当に新規のインストールでは安全なルールになります: 読取りは Always、ローカルの書込みは Ask、外部送信と実行系は Never。ファイルは `schema_version: 2` と `generation` を持ち、`tool-config.json.head` に最後の世代・ダイジェスト・ルール全体を記録します。検査に通らないファイルは **ロック**（全ツール Never）され、人間が復旧語を入力して保存するまで解除されません（[operations_ja.md](operations_ja.md#4-ツールルールがロックされたとき)）。詳細: [ADR 0010](adr/0010-tool-policy-gate-stack.md)。

**プロファイル（#450）。** 同じ画面に、固定の表が 3 つあります — *最小*（読取りは Always、それ以外は Never）、*開発*（読取りは Always。`file_write`、`editor`、`journal`、`lsp_*`、`shell` は Ask。`page_check` も Ask（ブラウザが見つかるときのみ。YOLO では自動承認されない）。`propose_procedure` も Ask（#463。手続きの規則をレビュー画面へ提案するだけ。「確認」か「しない」のみで、YOLO では自動承認されない。このツールより前に保存された規則では「しない」として読まれ、オンにするまで使えない。レビュー画面での承認はブレーカーが NORMAL のときだけで、却下はどのレベルでもできる）。ネットワークとほかの実行系は Never）、*調査*（開発に加えて `web_fetch` / `web_search` / `perplexity_search` を Ask）。選ぶと、何かが変わる前に、各ツールが Always / Ask / Never のどれになるか（`shell` には `STIRA_ENABLE_EXEC_TOOLS` が必要なこと）が出ます。「下書きに反映」→「保存」で、ほかの変更と同じように適用されます（緩める変更は確認が必要。保存は `source: "profile:<id>"` の `settings-history` の行になる）。表にないツール（MCP など）とシェルの許可リストは今の値のままです。インストールや更新でプロファイルが適用されることはなく、既存の `tool-config.json` は書き換わりません。**YOLO が自動で承認するのは Ask のツールだけ**です。Always は元から確認が出ず、Never は YOLO の間も実行されません（[yolo-quickstart_ja.md](yolo-quickstart_ja.md)）。

## 6. データディレクトリ（`<リポジトリ>/.local/stira/`） — *検査対象*

`STIRA_DATA_DIR` を設定した場合、同じファイルはそのディレクトリに置かれます（#272）。

ストアが権限を設定するものは所有者のみ（ディレクトリ `0700`、承認ストア・スケジューラログ・チェックポイント・EventStore のファイル `0600`）で作られます。ここにあるものはすべて **会話の私的なデータ** を含みえます。コミットや共有はしないでください。「床」= エージェントの書込みの床にある（`file_write` / `editor` が拒否し、承認でも外れない）。データディレクトリは全体が床にあります（#271。この表に無いファイルや新しく作るファイルも含む）。例外は 3 つのサブディレクトリだけです: `workspace/`（既定の `@workspace`）、`memory/agents/<agent>/memory/episodic/`、`context-stash/`。「エージェントが読めるか」= ファイルツール（`file_read`、`view_image`、`image_reader`、`editor` の `view`、`lsp_*`）で読めるか。データディレクトリは読取りの床にもあり（#312、ADR 0010 の追記）、例外は同じ 3 つで、承認でも外れません。データディレクトリの外は `read_roots`（既定: ワークスペースのみ）の中だけが読め、秘密パスの床（#457）は常に読めません。移動・削除の前にサーバーを止めてください。

<!-- data-files:begin -->
| パス | 書く者 | 内容 | 床 | エージェントが読めるか（#312） | 削除 / 退避してよいか |
|---|---|---|---|---|---|
| `gateway-config.json` | 設定ハブ、手編集 | `GatewayConfig` | あり | 不可 | 削除 = 既定値。 |
| `agent-config.json` | 設定ハブ、手編集 | `AgentConfig` | あり | 不可 | 削除 = 既定値。 |
| `tool-config.json` | 設定ハブ | ツールルール、世代 | あり | 不可 | 不可: head か履歴がある状態で消すと全ツール Never でロック。 |
| `tool-config.json.head` | 設定ハブ | 最後の世代、ダイジェスト、ルール全体 | あり | 不可 | 不可: head が無いとロック。 |
| `tool-config.json.locked` | ルールの読込み | ロックの印 | あり | 不可 | 消してもロックは解けない。復旧の保存だけが解く。 |
| `settings-history.jsonl` | 設定ハブ | 変更履歴（項目単位のロールバック） | あり | 不可 | 残す（監査記録、ロールバック元）。 |
| `tool-policy-approvals.json` | ルールの読取り | 開いている承認が求められたときのルールの世代 | あり | 不可 | 残す。食い違うと開いている承認をすべて失効させる。 |
| `hitl-approvals.db` | `ApprovalStore` | ハッシュ連鎖の承認要求と決定（引数はマスク済み。秘密を含まない EDIT の置換入力は使用まで保持） | あり | 不可 | `-wal`、`-shm`、`.head` と一緒にだけ: 新しい空のストアになり、保留中の承認はすべて失われる。 |
| `hitl-approvals.db.head` | `ApprovalStore` | その連鎖の head | あり | 不可 | 単独で消さない: head が無いとストアは unhealthy（fail-closed）。 |
| `hitl-operations.db` | `OperationLedger` | 実行済みの承認された副作用 | あり | 不可 | チェックポイントが再生されうる間は、巻き戻しも削除もしない。 |
| `neural-state.json` | `StatePersistence` | 6 次元の状態、`last_decay_check` | あり | 不可 | 削除すると状態は基準値に戻る。 |
| `memory/agents/<agent>/memory/` | `MemoryStore` | `episodic/`、`semantic/`（`staging/` を含む）、`procedural/` の Markdown ＋ `index.sqlite3` | あり（`episodic/` を除く。`memory_write` も書く） | 不可（`episodic/` を除く） | Markdown が正本（バックアップする）。インデックスは再構築できる。 |
| `memory/agents/<agent>/memory/forgotten/` | `--apply-forgetting`、`stira memory retention --apply` / `forget --apply`（#294） | 忘却した Semantic 記憶の元の全文と保持期限で退避した `removed-<id>.json`（全文。秘密を含みうる）、削除ジョブの状態 `delete-<id>.json`（本文なし）、ハッシュ連鎖の `forgetting-log.jsonl` | あり | 不可 | 記憶を完全に消すときだけ（その記録も消す。operations_ja.md §12）。消すと取り消せなくなる。 |
| `memory/agents/<agent>/memory/emotion/` | `record_episode`（#364） | v2 エピソードごとの JSON: 保存直後の Neural State（小数3桁）、カテゴリ、強度、ブレーカー、ターン、UI のターン ID。キーと本文ハッシュでエピソードに結び付く。隣の `emotion-log.jsonl` は孤児の削除の記録 | あり | 不可 | `MemoryStore.delete` でエピソードを消すと記録も消える。孤児: `python -m stiracore.control.emotion_cli --base-dir <memory> orphans [--apply]`。全部消してもスナップショットが失われるだけ（[episodic-emotion_ja.md](episodic-emotion_ja.md)）。 |
| `authority-epoch` | `stira_server`（権限サービス、ADR 0011 §5、[authority-service](authority-service_ja.md)） | 権限エポック: 最新 16 件の記録 `seq`・`epoch`・前の記録のハッシュ・自身のハッシュ（インカーネーションは書かない）。`0600`、耐久的に書く（flush → rename → ディレクトリの flush） | あり | 不可 | 編集しない: 読めない・安全でない・合わないファイルはサーバーを不健全にする（許可証を出さず、YOLO は何も自動承認しない）。修理はエポックのファイル自体が壊れたときだけ（ラッチストアの修理は `reset-latches`）: サーバーを止めて写しを取り、消し、`stira authority reset-latches`、起動、解除。 |
| `authority-latches` | `stira_server`（権限サービス、ADR 0011 W2、[authority-service](authority-service_ja.md)） | 停止のラッチ `kill`・`unhealthy`・`halted:<理由>` と停止 ID（インカーネーション、エポック）。最新 64 件の記録を鎖でつなぐ（`seq`、前のハッシュ）。`0600`、耐久的に書く | あり | 不可 | 編集も削除もしない: ラッチを解除するのはサーバーを止めての `stira authority unlatch` だけ。読めない・安全でない・合わない・無い・巻き戻されたファイル（`authority-epoch` のコミットメントとライフサイクルのログで確かめる）は、すべてのラッチが立っているもの（停止・不健全）として扱う。修理: `stira authority reset-latches`（すべて立てる）、その後 `unlatch`。 |
| `authority-lifecycle.jsonl`, `authority-lifecycle.1.jsonl` | 権限サービス（W2） | ハッシュの鎖でつないだ `lifecycle_event` の行: コードと ID のみ（対象、from、to、理由コード、インカーネーション、エポック、時刻）。`0600`、1 行ずつ flush。追記で 1 MiB を超えるときに `.1` に移す（古い `.1` は消える） | あり | 不可 | 残す（停止の記録。起動時にラッチストアとも突き合わせる）。壊れたファイルはサーバーを不健全にする。修理: authority-service_ja.md の「修理」。 |
| `authority-outbox.json` | 権限サービス（W2） | 通知（対象 + インカーネーション + エポック + to ごとに 1 件、最大 256 件、古い未読はまとめる）と未読・既読、通知済みと既読のイベント ID。`0600`、原子的に置き換え | あり | 不可 | 消すとライフサイクルのログから未読として作り直す。壊れたファイルはサーバーを不健全にする。 |
| `authority-kill-armed`, `authority-kill-fired` | 権限サービス（W2） | 起動時に作る空の印。キルスイッチが名前を変える（空きブロックの要らない第二のキルの記録） | あり | 不可 | `authority-kill-fired` は消さない: 消すのは `stira authority unlatch kill`。キルのラッチの無いまま印が発火していると、次の起動はラッチストアを拒否する。 |
| `authority-watchdog.json` | 権限ウォッチドッグ（W2） | 最後の一巡の時刻（起動時にそこからの空白を `gateway_down` として記録） `0600` | あり | 不可 | 消すと次の空白の記録が抜けるだけ。壊れたファイルはサーバーを不健全にする。 |
| `grants.json` | `stira_server`（gateway の `autonomy.stage_c` のときだけ、ADR 0011 W4、[grants](grants_ja.md)） | 目標グラント: 最新 4 件の記録を鎖でつなぐ（`seq`、前のハッシュ）、それぞれ状態全体。**目標の文はここにだけある**。生きているグラントは常に `revoked(restart)` として書く（active としては書かない）。`0600`、耐久的に書く | あり | 不可 | 編集しない: 安全でない・読めない・形式が違う・変わったファイルはグラントの段を不健全にする（すべて取り消し、発行不可、上書きしない）。修理: サーバーを止めて写しを残し、退避して起動（グラントは消える。新しく発行する）。 |
| `checkpoints/` | `FileCheckpointStore` | バッチごと: Neural State、会話、保留中のツール呼び出し、エンジン状態（スレッドごとに 20） | あり | 不可 | 消すと、それを必要とする再開は拒否され承認は取り消される。 |
| `context-stash/` | コンテキスト管理（#258） | 退避したツール結果の原文（マスク済み） | なし（書込み root の中にある必要がある、ADR 0005） | 可 | 可。参照が読めなくなる。古いものは `retention_hours`（24）で消える。 |
| `artifacts/` | `WorkspaceArtifacts`（#454、ADR 0020） | `index.db`（版ごとの相対パス・ターン ID・作成/更新・サイズ・SHA-256・種別・時刻、人が付けたラベル）と `blobs/`（小さい版の本文のスナップショット、内容アドレス） | あり | 不可 | 可。成果物の一覧・版・ラベルが消える（作業ディレクトリのファイルは消えない）。 |
| `turns.db` | `TurnEventStore` | UI のターンイベント（マスク済みプレビュー） | あり | 不可 | 可。UI のターン履歴が消える。 |
| `events.db` | `SQLiteEventStore` | ハッシュ連鎖の監査イベント（チャットサーバーが書くのは `/new` だけ。ターンのイベントは `turns.db`、#286） | あり | 不可 | `-wal`、`-shm`、`.head` と一緒にだけ。 |
| `events.db.head` | `SQLiteEventStore` | head | あり | 不可 | 単独で消さない（unhealthy）。 |
| `scheduler-events.jsonl` | スケジューラ（`STIRA_SCHEDULER=1`） | ハッシュ連鎖のジョブ・実行・通知（マスク済み） | あり | 不可 | `.head` と一緒にだけ: その後ジョブは無い状態。 |
| `scheduler-events.jsonl.head` | スケジューラ | head | あり | 不可 | 単独で消さない（ログが利用不可になる）。 |
| `server.lock` | `stira_server`（#285） | データディレクトリ全体の OS 所有ロック（PID メモ）。2 台目のサーバーは起動を拒否する | あり | 不可 | 停止中なら無害。ロックはカーネルが持つ。 |
| `scheduler.lock` | スケジューラ | OS の所有ロック（PID メモ） | あり | 不可 | 停止中なら無害。ロックはカーネルが持つ。 |
| `heartbeat.lock` | ハートビート（`hb_enabled`） | OS の所有ロック（PID メモ） | あり | 不可 | 停止中なら無害。 |
| `stira_server.pid` | `stira_ctl` | 起動したサーバーの PID | あり | 不可 | 停止中なら可。 |
| `yolo-host-channel.json` | `stira_server`（YOLO の許可があるときだけ、ADR 0012 §8b） | ホストコマンドがサーバーのホスト確認ソケットを見つける場所（パス、PID。ソケット本体は `0700` の一時ディレクトリ） | あり | 不可 | 正常停止で消える。停止中は無害。 |
| `yolo-window.json` | `stira_server`（YOLO の許可があるときだけ、ADR 0012 §11） | YOLO の窓マーカー: 窓 ID（乱数）、インカーネーションの参照、`open` / `closed` / `closed_by_unlatch`。耐久的に書く（flush → rename → ディレクトリの flush） | あり | 不可 | 削除・編集しない: `open` のマーカーが、正常に終わらなかった窓を再起動後もラッチし続ける根拠。壊れたもの・シンボリックリンクもラッチする。 |
| `workspace/` | エージェントのファイルツール | 既定の `@workspace`（#271）: エージェントが相対パスで書くファイル | なし | 可 | 利用者のもの: 作業フォルダと同様にバックアップする。 |
<!-- data-files:end -->

データディレクトリの外: `.local/stira.out.log` / `.local/stira.err.log`（`stira_ctl.sh` の出力）と `STIRA_CHAT_DATA_DIR`（チャットボット）。

## 出力上限と途中で切れたターン

`gateway-config.json` の `max_output_tokens` は 1 回のモデル呼び出しの出力上限（プロバイダの `max_tokens`）。環境変数での上書きはない。

- **自動（`null`、既定）。** `min(32000, モデルの文書化された出力上限)`。上限は能力表（`providers/capabilities.py` の `max_output`）から取る。Claude 5.x 系は 128K、4.x 系の行は、その系統で出荷された最小の上限を入れている。OpenAI は `gpt-5` / `gpt-5-mini` / `gpt-5-nano`（と日付つきスナップショット）が 128K、`o3` / `o3-mini` / `o4-mini` が 100K、`gpt-4.1` / `gpt-4.1-mini` が 32,768、Gemini は `gemini-2.5-pro` / `gemini-2.5-flash` が 65,536（各モデルのページ、2026-10）。系統のグロブは上限の低い派生（`gpt-5-chat-latest` は 16,384）にも当たるので、正確な ID だけに入れている。設定画面でモデルを切り替えると、新しいモデルに合わせて取り直す。デプロイで固定したいときは `max_output_tokens` を数値で指定する（設定画面の「出力上限とコスト」またはファイル。次のターンから反映。チャットの「途中で切れました」の注記からそこへ飛べる）。`ModelSpec` の `options.capabilities.max_output` は、spec を組み立てて `resolve_max_output_tokens` を呼ぶコード（ライブラリ利用）にだけ効き、設定ファイルからの経路はない。
- **数値**はそのまま使う。意図して決める天井であり、Stira はモデルの上限との比較をしない。上限を超えるとプロバイダが拒否する。
- **コスト。** 上限は「購入量」ではなく天井。課金は実際に出力したトークン分だけで、上限はプロンプトに入らないので、プロンプトキャッシュの接頭辞（tools → system → messages）も変わらない。上限を上げて変わるのは 2 点: 暴走した出力が最大で上限まで課金されうること、Bedrock は開始時に `max_tokens` をアカウントの毎分トークン枠に数えるため、とても高い上限は同時に通せるリクエスト数を減らすこと。思考の予備分により、Claude 5.x の呼び出しは開始時に「入力 + 64000」を枠から差し引く（以前は「入力 + 32000」）。使わなかった分は終了時に戻り、課金は出力した分だけ。
- **思考も同じ上限を使う（#416）。** Claude の拡張/適応思考と OpenAI/Gemini の推論トークンは同じ `max_tokens` に数えられる（#396 の実測: Sonnet 5.5 で上限に達した呼び出しのメッセージは思考ブロックだけ。Opus 5.5 は既定の effort で約 12 分考えて 64000 に達し、返答なし）。Bedrock の適応思考には別枠の予算がない（`thinking.budget_tokens` は拒否される）ので、自動の上限はそうした行に返答用 32000 の上に `thinking_reserve`（32000）を足す: `min(64000, 上限)`。Claude 5.x と 4.x の Sonnet/Haiku は 64000、Opus 4/4.1 は 32000（その上限）。思考のない行は 32000 のまま。
- **上限が記録されていないモデル（#416）。** 自動の上限はプロバイダ別の既定（`unknown_limit_max_output`）。ホスト型 API（Bedrock、Anthropic、OpenAI と OpenAI 互換サーバ、Gemini、LiteLLM など）は、モデル自身の上限を超える値のリクエストを拒否するので `8192`（大きく推測すると毎ターン API エラーになる）。ローカルの `llamacpp` / `ollama` はサーバが大きな値を受け付け（生成はコンテキスト窓で終わる）、課金もないので `32000`。設定画面は自動のときにもそう表示する（「推論モデルは思考だけでこれを使い切ることがあります。モデルの上限が分かっていれば数値で設定してください」）。
- **上限に達したターン**は `stop: "max_tokens"` で終わり、チャットと Stage に理由つきで表示される（「出力の上限に達して途中で切れました。上限を上げるか、分けて依頼してください」）。Strands のイベントループはツールを実行する前に例外を出すので、引数の途中で切れたツール呼び出し（書きかけの `file_write`）は、ツールのルールが Always でも**実行されない**。途中までのメッセージは履歴に残るが、ツール呼び出しは注記に置き換わり、次のメッセージは通常どおり動く。切れた呼び出しが返答もツール呼び出しも始めていなかった（メッセージが思考ブロックだけ、または空）ときは、理由をそう表示する（「返答を書き始める前に出力の上限（N トークン）に達しました。思考も上限に含まれるモデルでは、思考だけで使い切ることがあります。上限を上げてください。」）。SSE の `error` に `cut: "before_reply"` と `limit`、`turn.completed` に `cut` が入る（#416）（`tests/test_max_output_tokens.py`、`tests/test_headless_file_tools_e2e.py`）。
- **失敗した呼び出しの usage。** `max_tokens` で終わった呼び出しも Bedrock は課金する。その `metadata.usage` を `turn.completed.usage` とセッション合計に記録する。数が取れない場合は、素の `null` ではなく `usage_reason` が付く: `E_USAGE_NOT_REPORTED`（プロバイダが報告前に失敗した）、`E_USAGE_CANCELLED`（先に **■ 止める** / `POST /api/chat/cancel` で止めた）、`E_USAGE_INTERRUPTED`（先にサーバーが停止・再起動した）。`GET /api/turns` は `usage` の隣に `stop` と `usage_reason` を返す。

## ターンはページより長く生きる（#452）

チャットと Stage のメッセージは、サーバーの専用のワーカースレッドで実行する。ページの SSE ストリーム（`POST /api/chat`）は、それを見る窓に過ぎない。タブを閉じる・再読み込み・別のページへ移動・通信の切断では、**ターンは止まらない**。最後まで進み、返答は `/api/chat/history` に、イベントは turns.db に残る（`GET /api/turns/<id>/events?after_seq=N` で、ページが離れたところから取りこぼしなく続きを読める）。何も流れない間は 15 秒ごとに SSE のコメントを送る。読まなくなったページは、待ちのフレームが 10,000 件に達した時点で切り離す（メモリは上限内。ターンは続く）。

ターンの終わり方（`turn.completed.stop`）:

| `stop` | いつ | `error` |
|---|---|---|
| `completed` | ターンが終わった（見ているページの有無に関係なく） | `false` |
| `cancelled` | 明示の停止**だけ**: チャット画面の **■ 止める**（`POST /api/chat/cancel`）。SDK は次の区切り（モデルのストリーム中・ツールの前・ツールの後）で止まり、そのターンの人の判断待ちのカードは却下される（押した後に出たカードも、ターンが終わるまで却下する。`hitl_timeout_sec` を待たない）。それまでの出力は履歴に残る（`stop: "cancelled"`） | `true` |
| `interrupted` | ターンの実行中にサーバーが停止した（`reason: "server_stopped"`）、または次の起動時に終わりの記録がないターンが見つかった＝クラッシュや強制終了（`reason: "server_restarted"`）。このターンの返答は失われる（再起動をまたぐ再開は #240） | `true` |
| `error` / `max_tokens` | 従来どおり | `true` |

- **承認待ちは画面ではなく期限まで待つ。** ページが開いていない間に出たカードは `hitl_timeout_sec`（既定 120 秒、最大 24 時間）まで保留される。再接続後にどのページ（または `/approvals`）で承認しても、ターンは続く。期限切れとサーバー停止は従来どおり却下（フェイルクローズ）。
- **同時に動くターンは 1 つ**（従来どおり）。ターンのロックは送信からターンの終わりまで持ち、失敗した場合も含めどの経路でも解放する。実行中の 2 本目の送信には、ストリームの `error` イベント「処理中です / Agent is busy」（`code: "turn_running"`）を返す。`/new` は `turn_running` で断る。YOLO の停止・`/new`・ツール実行中の拒否は従来どおり。YOLO の停止は窓を終える（以降のツール呼び出しはカードになる）が、ターン自体は止めない。止めるのは **■ 止める**。
- Stage の **■ 表示を止める** は、その名前どおりページの読み込みだけを止める。ターンは続く。
- **戻ってきたとき。** ターンの実行中に開いた（再読み込みした）チャット画面は、そのことを表示し（「前のターンがサーバーで続いています…」）、そのターンの **■ 止める** を出す（ターン ID を指定するので、その間に始まった別のターンを止めることはない）。終わったら履歴を読み直す。`GET /api/turns` は実行中のチャットのターンに `stoppable: true` を付ける。チャットが動かしていないターン（承認後に再開したターン）は実行中と表示するが、**■ 止める** は出さない。

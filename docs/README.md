# Documentation index / ドキュメント索引

English and Japanese are listed side by side where both exist. / 英語と日本語の両方があるものは並べて載せています。

## Start here / はじめに

| Topic / 内容 | Document |
|---|---|
| Install (Linux / macOS / Windows) / インストール | [install.md](install.md) / [install_ja.md](install_ja.md) |
| Setting up with an AI agent / AI エージェントでセットアップ | [agent-setup.md](agent-setup.md) / [agent-setup_ja.md](agent-setup_ja.md) |
| Public release snapshot and publishing updates / 公開版スナップショットと公開側の更新 | [release.md](release.md) / [release_ja.md](release_ja.md) |
| Contributing, security policy / 貢献の方法・セキュリティポリシー | [CONTRIBUTING.md](../CONTRIBUTING.md) / [CONTRIBUTING_ja.md](../CONTRIBUTING_ja.md), [SECURITY.md](../SECURITY.md) / [SECURITY_ja.md](../SECURITY_ja.md) |
| **Optional: running on an always-on host such as EC2** (a reference setup: private access, instance role, systemd, backup) and the Mac / Windows / EC2 comparison / **任意: 常駐ホスト（EC2 など）での運用の参考構成**（非公開での利用・インスタンスロール・systemd・バックアップ）と環境の比較 | [deploy-ec2.md](deploy-ec2.md) / [deploy-ec2_ja.md](deploy-ec2_ja.md), [install.md §11](install.md#11-environment-comparison-ec2--mac--windows) |
| AWS setup, IAM / AWS の設定 | [setup.md](setup.md), [bedrock-iam-policy.md](bedrock-iam-policy.md) |
| Quickstart (executed in tests) / クイックスタート | [quickstart.md](quickstart.md) |
| Model providers / モデルプロバイダ | [providers.md](providers.md) / [providers_ja.md](providers_ja.md) |
| **Configuration reference** (environment variables, config files, data directory) / **設定リファレンス**（環境変数・設定ファイル・データディレクトリ） | [configuration.md](configuration.md) / [configuration_ja.md](configuration_ja.md) |
| **Settings surface** (what the settings page can change, when it applies, which variable wins) / **設定の分類**（画面から変えられるか・適用時点・勝つ環境変数） | [settings-surface.md](settings-surface.md) |
| **Operations runbook** (recovery, `stira doctor`, backup) / **運用手順書**（復旧・`stira doctor`・バックアップ） | [operations.md](operations.md) / [operations_ja.md](operations_ja.md) |

## Design / 設計

| Topic / 内容 | Document |
|---|---|
| Glossary (terms, Japanese UI labels) / 用語集（用語・日本語 UI の表記） | [glossary.md](glossary.md) |
| Architecture and diagrams / アーキテクチャと図 | [architecture.md](architecture.md), [diagrams/](diagrams/README.md), [design-philosophy.md](design-philosophy.md) |
| Harness components (sessions, subagents, delegation) / ハーネス部品 | [harness-components.md](harness-components.md) |
| Strands source audit / Strands ソース監査 | [strands-source-audit.md](strands-source-audit.md) |

## Safety / 安全

| Topic / 内容 | Document |
|---|---|
| Tool rules, HITL approvals, resume / ツールルール・承認・再開 | [hitl-approvals.md](hitl-approvals.md), [ADR 0006](adr/0006-hitl-persistence.md), [ADR 0010](adr/0010-tool-policy-gate-stack.md) |
| Allow again from the approval card (session allowance, Make Always) / 承認カードの継続許可 | [allow-again.md](allow-again.md) / [allow-again_ja.md](allow-again_ja.md), [ADR 0025](adr/0025-allow-again-from-the-approval-card.md) |
| Agent self-scheduling (proposed) / エージェントの自己スケジューリング（提案中） | [ADR 0011](adr/0011-agent-self-scheduling.md), [agent-scheduling-guidelines.md](agent-scheduling-guidelines.md) |
| YOLO mode (button shown by default, `STIRA_YOLO_ALLOW=off` hides it; phases 0–7; ADR accepted, revision 8, addendum 2026-10-24) / YOLO モード（ボタンは既定で表示、`STIRA_YOLO_ALLOW=off` で非表示。フェーズ 0〜7。ADR は採択、改訂 8、追記 2026-10-24） | [ADR 0012](adr/0012-yolo-mode.md), [yolo-quickstart.md](yolo-quickstart.md) / [yolo-quickstart_ja.md](yolo-quickstart_ja.md) |
| Progress reports during long runs (phase 1 in YOLO windows) / 長時間実行の中間報告（フェーズ 1: YOLO の窓） | [ADR 0014](adr/0014-progress-report-phase.md) |
| Delegating to child agents under the gate (`delegate`, phase 1) / ゲート配下のサブエージェント委譲（`delegate`、フェーズ1） | [ADR 0016](adr/0016-subagent-orchestration.md) |
| Tool execution paths, write roots / ツール実行経路・書込み先 | [tool-execution-paths.md](tool-execution-paths.md), [tool-audit.md](tool-audit.md) |
| Write diagnostics, `stira doctor` / 書込み診断 | [diagnostics.md](diagnostics.md) |
| SOUL protection / SOUL の保護 | [soul-read-only.md](soul-read-only.md), [soul-compaction.md](soul-compaction.md) |
| Web API permissions / Web API の権限 | [admin-api-permissions.md](admin-api-permissions.md) |
| Breaker reachability / Breaker の到達性 | [breaker-reachability.md](breaker-reachability.md) |
| Coherence score (`bc_score`) definition / 一貫性スコアの定義 | [coherence.md](coherence.md) / [coherence_ja.md](coherence_ja.md) |
| Neural State saturation and recovery (operator guide) / Neural State の飽和と回復（運用者ガイド） | [neural-saturation.md](neural-saturation.md) / [neural-saturation_ja.md](neural-saturation_ja.md) |

## Features (opt-in unless stated) / 機能（特記なき限りオプトイン）

Show it and let people choose: what spends tokens or widens permissions is opt-in; what only
shows something and approves or runs nothing until a person presses or picks it (the YOLO
button, static SVG on the Stage board) is shown by default ([ADR 0012 addendum
2026-10-24](adr/0012-yolo-mode.md)). / 見せて選ばせる: トークンを使う・権限を広げる機能はオプトイン、
押す/選ぶまで何も承認・実行しない見せるだけの機能（YOLO ボタン、Stage ボードの静的 SVG）は既定で見せる。

| Topic / 内容 | Document |
|---|---|
| Heartbeat daemon / ハートビート | [heartbeat-daemon.md](heartbeat-daemon.md) |
| Authority service (persisted epoch, kill flag, in-flight set; starts no work) / 権限サービス（永続エポック・キルフラグ・実行中の集合。作業は始めない） | [authority-service.md](authority-service.md) / [authority-service_ja.md](authority-service_ja.md) |
| **Autonomy (alpha, off by default)**: overview, how to try it, guarantees, known limits / **自律（アルファ版・既定オフ）**: 概要・試し方・保証・既知の制約 | [autonomy.md](autonomy.md) / [autonomy_ja.md](autonomy_ja.md) |
| Goal grants (ADR 0011 stage C, alpha): the full rules / 目標グラント（ステージ C、アルファ版）: 規則の全体 | [grants.md](grants.md) / [grants_ja.md](grants_ja.md) |
| Scheduled jobs / 定期ジョブ | [scheduler.md](scheduler.md) |
| **Guide: scheduler and heartbeat** (which to switch on, first job, checks, troubleshooting) / **ガイド: スケジューラーとハートビート**（何をオンにするか・最初のジョブ・確認・困ったとき） | [guide-scheduler-heartbeat.md](guide-scheduler-heartbeat.md) / [guide-scheduler-heartbeat_ja.md](guide-scheduler-heartbeat_ja.md) |
| Prompt cache / プロンプトキャッシュ | [prompt-cache.md](prompt-cache.md) |
| Context management / コンテキスト管理 | [context-management.md](context-management.md) |
| Token estimate (CJK = 1 token) / トークン見積もり（日本語 1 文字 = 1 トークン） | [token-estimate.md](token-estimate.md) |
| Small-window profile / 小さい窓向けのプロファイル | [small-context.md](small-context.md) ([日本語](small-context_ja.md)) |
| Memory maintenance (manual / OS timer; nothing runs by default) / 記憶の整理（手動・OS のタイマー。既定では何も動かない） | [memory-maintenance.md](memory-maintenance.md) / [memory-maintenance_ja.md](memory-maintenance_ja.md) |
| Memory autonomy phase 0 (observe only, off by default: `memory_rate`, plan-only memory jobs) / 記憶の自律フェーズ 0（観測のみ・既定 OFF） | [memory-autonomy.md](memory-autonomy.md) / [memory-autonomy_ja.md](memory-autonomy_ja.md) |
| **Guide: automatic memory organisation** (recommended profiles, review, safety, weekly routine) / **ガイド: 記憶の自動整理**（推奨のプロファイル・レビュー・安全・週次の手順） | [guide-memory-routines.md](guide-memory-routines.md) / [guide-memory-routines_ja.md](guide-memory-routines_ja.md) |
| Containment phase 0 (observe only, off by default: signals counted, `would_be` stage, weekly summary) / 隔離の段階フェーズ 0（観測のみ・既定 OFF） | [containment.md](containment.md) / [containment_ja.md](containment_ja.md) |
| Taint phase 0 (observe only, off by default: context labels, would-hold counts, weekly summary) / 信頼ラベルフェーズ 0（観測のみ・既定 OFF） | [taint.md](taint.md) / [taint_ja.md](taint_ja.md) |
| Autonomous memory recording and periodic maintenance (accepted; phase 0 scheduled) / 記憶の自律記録と定期整理（承認済み、フェーズ 0 を実装予定） | [ADR 0018](adr/0018-autonomous-memory.md) |
| Episodic emotion snapshots (on the floor) and opt-in emotion-proximity recall / エピソードの感情スナップショット（床の中）とオプトインの感情近傍想起 | [episodic-emotion.md](episodic-emotion.md) / [episodic-emotion_ja.md](episodic-emotion_ja.md) |
| Associative recall (opt-in, read-side Amygdala Pass) / 連想想起（オプトイン、読み出し側 Amygdala Pass） | [associative-recall.md](associative-recall.md) / [associative-recall_ja.md](associative-recall_ja.md) |
| Memory index and Memory Map (bounded index for small windows) / 記憶の索引と Memory Map（小さい窓向けの上限つき索引） | [memory-index.md](memory-index.md) / [memory-index_ja.md](memory-index_ja.md) |
| Handoff notes (a short note for the next session) / 引き継ぎノート | [handoff.md](handoff.md) / [handoff_ja.md](handoff_ja.md) |
| Model per task (side tasks such as summaries on another model; default: the chat model) / 処理ごとのモデル（要約などの補助の処理。既定は会話のモデル） | [task-models.md](task-models.md) / [task-models_ja.md](task-models_ja.md) |
| Checkpoints (always on) / チェックポイント（常時） | [checkpoints.md](checkpoints.md) |
| Chat and business integrations / チャット・業務連携 | [chat-integrations.md](chat-integrations.md), [business-integrations.md](business-integrations.md) |
| Optional tools / 任意のツール | [tools/perplexity.md](tools/perplexity.md), [tools/web-fetch.md](tools/web-fetch.md) (failure codes / 失敗コード), [lsp-neural-viz.md](lsp-neural-viz.md) |
| Stage: pixel-art view of activity and emotion (spike, standalone) / Stage: 活動と感情のピクセルアート表示（spike・単体） | [stage.md](stage.md) |
| Stage as the main page: layout design, phases 1–2 implemented (whiteboard, `/new`, context gauge, `ui.home`) / Stage を主画面に: 画面構成の設計（フェーズ1・2 実装済み。ボード・`/new`・コンテキストゲージ・`ui.home` の切替） | [stage-main-ui.md](stage-main-ui.md) |
| Dashboard colour theme (tokens, contrast table, how to swap the palette) / ダッシュボードの配色（トークン・コントラスト表・差し替え方） | [ui-theme.md](ui-theme.md) |
| Japanese rule stimulus classifier (ML-Ask + discourse patterns) / 日本語ルール刺激分類器 | [ja-rules-classifier.md](ja-rules-classifier.md) / [ja-rules-classifier_ja.md](ja-rules-classifier_ja.md) |

## Evaluation and research / 評価・調査

| Topic / 内容 | Document |
|---|---|
| Emotion-ablation pre-registration / アブレーション事前登録 | [eval/emotion-ablation-prereg.md](eval/emotion-ablation-prereg.md) |
| JP stimulus mini-set and ja-hybrid evaluation (dev/test) / 日本語刺激ミニデータと ja-hybrid の評価 | [eval/jp-stimulus/README.md](eval/jp-stimulus/README.md), [eval/jp-stimulus/ja-hybrid/report.md](eval/jp-stimulus/ja-hybrid/report.md) |
| Dated research notes, probes and measurements (development repository only; not in the public release) / 日付付きの調査・プローブ・計測(開発リポジトリのみ・公開版には含まれない) | research/README.md |

## Architecture decision records / 設計判断記録（ADR）

Checked against `docs/adr/` by `tests/test_docs_sync.py`. / `tests/test_docs_sync.py` が `docs/adr/` と照合します。

<!-- adr-index:begin -->
| ADR | Title | Status | Issue |
|---|---|---|---|
| [0001](adr/0001-stimulus-attribution.md) | Stimulus attribution | Proposed | — |
| [0002](adr/0002-breaker-enforcement.md) | Breaker enforcement (read-only by default; `halt` = per-write approval, amended 2026-10-16) | Accepted (amended) | — |
| [0003](adr/0003-memory-design.md) | Three-layer memory design | Accepted | — |
| [0004](adr/0004-editor-file-editor-sandbox.md) | `editor` = SDK `file_editor` behind Stira's write sandbox | Accepted | #245 |
| [0005](adr/0005-context-management.md) | Context management with public SDK components | Accepted (opt-in) | #258 |
| [0006](adr/0006-hitl-persistence.md) | Durable HITL approvals and asynchronous resume | Accepted | #240 |
| [0007](adr/0007-write-diagnostics.md) | Write-failure diagnosis, permission cards and `stira doctor` | Accepted | #244 |
| [0008](adr/0008-scheduler.md) | Operator-defined scheduled jobs and health notifications | Accepted (two Open points; to be amended by ADR 0011, Proposed) | #239 |
| [0009](adr/0009-eventstore-chain.md) | Hash-chained EventStore and trajectory metrics | Accepted (stages 2–3 planned) | #171 |
| [0010](adr/0010-tool-policy-gate-stack.md) | Always / Ask / Never tool rules and the effective gate stack | Accepted (Open points marked) | #190 |
| [0011](adr/0011-agent-self-scheduling.md) | Bounded agent self-scheduling and grant-scoped autonomous goal loops | Proposed | #296 |
| [0012](adr/0012-yolo-mode.md) | YOLO mode: six hours of automatic approval for Ask, in two tiers, started by a button | Proposed | #365 |
| [0013](adr/0013-audit-event-contract.md) | Audit event contract, separated from operational observability | Proposed | #277 |
| [0014](adr/0014-progress-report-phase.md) | Progress reports: stop at a checkpoint, report on the board, continue / redirect / stop | Accepted (phase 1) | #451 |
| [0015](adr/0015-verification-browser-tool.md) | A verification-only browser tool: open and exercise the agent's own pages, inside the workspace, with no network | Accepted (phase 1 implemented) | #449 |
| [0016](adr/0016-subagent-orchestration.md) | Delegating to child agents under Stira's gate: one Stira-owned tool, depth 1, bounded summary | Accepted (2026-10-19; phase 1 implemented) | #486 |
| [0017](adr/0017-emotion-proximity-recall.md) | Emotion-proximity in associative recall: a bounded re-ranking signal from the stored snapshot, off by default | Accepted (2026-10-20; phase 1, default off) | #364 |
| [0018](adr/0018-autonomous-memory.md) | Autonomous memory recording and maintenance: importance rated by the agent at creation, memory jobs outside the tick, reversible-only apply | Accepted (2026-10-20; phase 0 scheduled, phases 1–2 need their own go) | #251, #252 |
| [0019](adr/0019-verification-runner.md) | A verification runner: run the project's own checks on the agent's programs, in a throwaway copy, with no network | Accepted (2026-10-21; phases 0–1 implemented, phases 2–3 need their own go; Always only on a verified sandbox) | #167 |
| [0020](adr/0020-workspace-artifacts.md) | Workspace artifacts: register what the agent writes, show versions, diffs and a person's labels | Accepted (2026-10-21; PR 1 registration + read API, PR 2 page, sandboxed preview, labels) | #454 |
| [0021](adr/0021-scheduler-job-base.md) | One scheduler job base: operator, memory and agent jobs; gated Agent-layer jobs; heartbeat and approval connection points (follow-up to ADR 0008) | Accepted (2026-10-21; S1 scheduled, S2–S3 need their own go) | #239 |
| [0022](adr/0022-trust-labels-and-taint.md) | Trust labels and taint on every input path: frame untrusted text as data, hold tainted egress for a person | Accepted (2026-10-21; phase 0 scheduled after #461-A, phases 1–2 need their own go) | #458 |
| [0023](adr/0023-containment-stages-and-attack-regression.md) | Containment stages (restricted / closed / quarantine / terminated), deterministic threat signals, and the attack-scenario regression set | Accepted (2026-10-21; #461-A and #460-0 scheduled, later phases need their own go) | #460, #461 |
| [0024](adr/0024-approvals-beyond-the-turn.md) | Approvals that outlive their turn: breaker freeze, job-origin approvals, what a resume can repeat (follow-up to ADR 0006) | Accepted (2026-10-21; phase 1b scheduled, phase 2 needs its own go) | #240 |
| [0025](adr/0025-allow-again-from-the-approval-card.md) | "Allow the same again" on an approval card: a session allowance for one tool and one target class (≤ 2 h, memory only), and "make this tool Always" through the settings hub | Accepted (revision 5, 2026-10-24; Q1–Q7 as recommended; implemented: PR 1 backend, PR 2 page, PR 3 Make Always and both buttons on) | #571 |
| [0026](adr/0026-memory-index-handoff-small-windows.md) | Memory structure for small context windows: index first (`memory_index`, Memory Map), a handoff note of pointers, a corrected Japanese token estimate and window profiles (phase 1); hierarchical compression designed only (phase 2) | Proposed (revision 4, 2026-10-23; Memory Map and handoff injection on by default per operator, env off; Q3, Q5 decided) | #258, #368 |
| [vigilance-bidirectional](adr/vigilance-bidirectional.md) | Bidirectional vigilance | Accepted | #76 |
<!-- adr-index:end -->

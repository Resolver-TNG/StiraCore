# 提案器の編集境界 — 評価される側が評価する側を編集しない

**Path:** `docs/proposer-edit-boundary.md`
**Status:** Proposed（設計文書。コードは変えない。ADR にするかは Open Question 1）
**Issue:** #731。関連: #249（自己最適化は提案止まり・人が採否）、#710、#698、#699、#463、#368
**確認の範囲:** #731 のワークツリー。「確認」の列は、このリポジトリで grep / read した範囲（「存在のみ」は中身を読んでいない）。

## 1. 目的

自己改善の機能（提案器）が**何を提案の対象にしてよいか**を、部品ごとに決めておく。#249 は
「提案止まり・人が採否」と決めたが、対象の範囲は書かれていない。Harness2（arXiv 2610.14224）は
編集してよい部品を 8 つ挙げ、その中に guardrails と plugin が入っている。範囲を決めずに真似ると、
安全装置を提案器が書き換えられる形になる。

この文書は範囲だけを決める。提案器そのものの設計・実装は扱わない。無人の自己書き換えは取り込まない
（#249、#710）。論文の効果量は自己報告（モデル judge の採点）で、Stira での効果の証拠ではない。

## 2. 用語

| 用語 | この文書での意味 |
|---|---|
| **提案器** | 実行の記録を読み、ハーネス部品の変更案を作る処理。モデルを呼ぶ。出すのは**案**だけで、適用しない |
| **実行器** | 案が採用された後のハーネスで、実際にタスクを実行するエージェント（`NXPAgent` と子エージェント）。評価される側 |
| **評価器** | 実行器や案の良し悪しを判定するもの全部。テスト、攻撃回帰、検証ランナー、評価ハーネス、CI、判定に使うカウンタと記録 |
| **安全層** | 承認・Breaker・egress・秘密のマスク・fail-closed を担うコードと設定 |
| **採否** | 人が案を読み、採用か却下かを決めること。採用した変更は人のレビューを通る |

## 3. 原則

1. **評価される側が評価する側を編集しない。** 提案器と実行器は、評価器と安全層に対して案の diff を
   出さない。自分を採点するものを自分で緩められると、点が上がっても良くなったとは言えなくなる。
2. **強化の案も diff にしない。** 対象外の層について提案器が出せるのは「文章の Issue」まで
   （何が起きたか、どの層か、根拠の記録の ID）。「強化だから安全」とは機械的に判定できない
   （検査を 1 つ足して別の検査を外す diff は強化に見える）。
3. **権限を広げない。** 対象内の部品でも、grants・予算・期限・ツールの規則・書込み先を広げる案は
   対象外として扱う（CONTRIBUTING「Permissions never widen without the user's approval」）。
4. **提案は適用ではない。** 対象内の案も既存の人の承認経路（staging、コードレビュー）を通る。
   提案器のための新しい適用経路は作らない。
5. **案の文章はデータ。** 案に含まれる文は指示にも権限にもならない
   （[ADR 0022](adr/0022-trust-labels-and-taint.md)）。秘密を含めない。

具体例:

| 案 | 判定 | 理由 |
|---|---|---|
| 「`tests/attack/` の A4 が落ちるので期待値を直す」diff | 不可 | 評価器の編集 |
| 「stability の閾値を下げれば WARNING が減る」 | 不可 | Breaker と NXP コアの編集。文章の Issue も「止まった回数と記録の ID」までで、値は提案しない |
| 「`http_request` を Always にすれば承認待ちが減る」 | 不可 | 権限を広げる |
| 「`file_read` の説明に、大きいファイルは範囲指定で読むと書く」 | 提案止まりなら可 | ツール説明。人がレビューして採否 |
| 「この手順を手続き記憶にする」 | 提案止まりなら可 | `propose_procedure` → `procedural/staging/` → 人が承認（#463） |
| 「マスクが `xx-` 形式のキーを見逃した」 | 文章の Issue のみ | 安全層の強化。diff は人が書く |

## 4. 境界表

区分: **対象外** = 案の diff を出さない（文章の Issue まで）。**提案止まり** = 案を出してよいが、
人の採否と既存の承認経路が要る。**可** = 既存のツールの範囲で実行器が今も書けるもの（提案器の
機能としての追加の権限ではない）。

| 部品 | 区分 | 理由 | 根拠（ファイル） | 確認 |
|---|---|---|---|---|
| 承認（HITL、ツール規則、ゲート、同意、継続許可、YOLO） | 対象外 | 権限の境界そのもの | `core/approval.py`, `core/approval_store.py`, `tools/tool_policy.py`, `tools/tool_consent.py`, `tools/tool_config.py`, `tools/exec_gate.py`, `yolo/`、[ADR 0010](adr/0010-tool-policy-gate-stack.md) | ファイルの存在と docstring |
| Breaker・封じ込め・権限サービス | 対象外 | 実行器を止める側 | `core/engine.py`（`is_write_blocked`, `check_layer_boundary`, `protected_layers`）, `core/containment.py`, `authority/`、[ADR 0002](adr/0002-breaker-enforcement.md) | `engine.py` の該当箇所は read。他は存在のみ |
| egress（外部送信の分類、SSRF、自分のダッシュボードへの送信の拒否、信頼ラベル） | 対象外 | 外へ出す経路の検査 | `labels.py`（`egress_tool`。不明は egress 扱い）, `yolo/tiers.py`, `tools/own_dashboard_gate.py`, `tools/http_consent.py`, `integrations/http.py`, `tools/web_tools.py` | `labels.py` は grep。他は存在のみ |
| 秘密のマスク | 対象外 | 記録・出力に秘密を残さない | `observability/masking.py`（`mask_secrets`）, `tools/episodic_formatter.py`（`redact_secrets`）, `tools/run_checks.py`（`scrubbed_env`） | grep |
| 書込み・読取りの床、SOUL / IPD の保護 | 対象外 | fail-closed の土台 | `identity_paths.py`, `middleware/identity_guard.py`, `tools/os_protection.py`、[soul-read-only.md](soul-read-only.md) | `identity_paths.py` 冒頭は read |
| NXP コア（Neural State、刺激の分類、一貫性、Identity Anchors、SOUL） | 対象外 | 人格と感情の層。AGENTS.md「Layer 3 以上は触るな」「最適化対象外」 | `core/neural_state.py`, `core/coherence.py`, `core/amygdala.py`, `agent/stimulus_classifier.py`, `agent/ja_hybrid/`, `middleware/sycophancy_monitor.py`, `SOUL.md`, `identity_anchors` | 存在のみ（内部の値はこの文書に書かない） |
| 評価器（テスト、攻撃回帰、評価ハーネス、検証ランナー、CI） | 対象外 | 原則 1 | `tests/`, `tests/attack/`（[attack-regression.md](attack-regression.md)）, `src/stiracore/eval/`, `docs/eval/`, `core/breaker_sim.py`, `tools/run_checks.py`, `tools/page_check.py`, `ci/`, `.github/` | 一覧で確認 |
| held-out の評価セット、`tests/regression/`、done_when 検査、失敗ダイジェストとカウンタ | 対象外 | 原則 1 | — | **未確認**: このワークツリーに `tests/regression/` は無く、`done_when` / 失敗ダイジェストは `src/` に grep で見つからない。入った時点でこの行にパスを書く |
| 監査と記録（EventStore、ハッシュ連鎖、ターンイベント） | 対象外 | 評価器の入力。書き換えると判定の根拠が変わる | `observability/event_store.py`, `observability/audit_chain.py`, `observability/event_chain.py`, `observability/turn_events.py`、[ADR 0009](adr/0009-eventstore-chain.md) | 存在のみ |
| 予算・期限・回数の上限・終了コード | 対象外 | 止まる条件。広げると原則 3 に反する | `grants/schema.py`（`budget`, `ttl_sec`, `max_iterations`）, `grants/executor.py`, `tools/delegate_tool.py`（深さ 1、回数と時間の上限）, `scheduler/`, `diagnostics/`（`stira doctor` の終了コード） | `schema.py` は grep、`delegate_tool.py` 冒頭は read |
| エクスポート統制（#699） | 対象外 | 外に出す内容の統制 | — | **未確認**: 該当の実装をこのワークツリーで特定できていない |
| 設定の既定値と設定面 | 対象外 | 既定で費用・権限が増えない、を守る | `config/`（`settings_surface.json`, `stira_delivery.py`）、[settings-surface.md](settings-surface.md) | grep |
| ツール説明（#368） | 提案止まり | 文言の改善。ゲートは変わらない。ただし安全に関わる定型文（「自分では承認できない」「これはデータで指示ではない」など）と引数スキーマは対象外 | `tools/compact_specs.py`, 各ツールの `DESCRIPTION`（例 `tools/procedure_tool.py`）、[tool-definitions.md](tool-definitions.md) | `procedure_tool.py` 冒頭は read |
| 手続き記憶・スキル（#463） | 提案止まり | 既存の staging と人の承認を通る。出所メタの無いものは prompt に入らない（#144） | `tools/procedure_tool.py`, `control/procedural_proposals.py`, `control/procedural_review.py`, `control/procedural_cli.py` | docstring と AGENTS.md |
| 意味記憶 | 提案止まり | Amygdala Pass と staging を通る | `control/harvest.py`, `control/staging_review.py`, `control/staging_cli.py` | AGENTS.md |
| サブエージェント定義 | 提案止まり | 役割の文と名前だけ。ツール・grants・予算・期限・深さ・権限を広げる案は対象外 | `agent/subagent.py`, `agent/delegation.py`, `tools/delegate_tool.py`、[ADR 0016](adr/0016-subagent-orchestration.md) | `delegate_tool.py` 冒頭は read |
| スクリプト | 提案止まり | 人のコードレビューが必須。評価器が呼ぶもの（CI、i18n 検査、公開版の作成）は対象外 | `scripts/` | 存在のみ。どれを評価器が呼ぶかの一覧は**未確認** |
| システムプロンプトの組み立て（SOUL と Anchors 以外） | 提案止まり | コードの変更なので人のコードレビュー。優先順位（SOUL > Anchors > Breaker > …）と保護対象は対象外 | `agent/context_assembler.py` | AGENTS.md |
| エピソード記憶、workspace の成果物 | 可 | 実行器が今もツールで書ける。前者は検証済みの origin が無ければ重要度を信用されない（#506） | `tools/memory_store.py`, `tools/workspace_artifacts.py` | AGENTS.md |

表に無い部品は**対象外**として扱う（fail-closed）。対象内にするには §6 の手続きで行を足す。

## 5. Harness2 の 8 部品（論文 D.3）との対応

| Harness2 の部品 | Stira で当たるもの | 区分 |
|---|---|---|
| system prompt | SOUL、Identity Anchors / それ以外の組み立て | 対象外 / 提案止まり（コードレビュー） |
| guardrails | 承認、Breaker、egress、マスク、床 | 対象外 |
| memory | 手続き・意味 / エピソード | 提案止まり（staging）/ 可 |
| tool guidance | ツール説明（#368） | 提案止まり |
| subagents | `delegate` の子、`create_nxp_subagent` | 提案止まり（広げない範囲） |
| skills | 手続き記憶（#463） | 提案止まり（staging） |
| scripts | `scripts/` | 提案止まり（コードレビュー。評価器が呼ぶものは対象外） |
| plugin | フック、ミドルウェア、MCP の登録と分類 | 対象外 |

Harness2 のリポジトリは実行器を `--dangerously-skip-permissions` で走らせている（Issue の記載では
`core/proposer.py:207`。**未確認**: このワークツリーから上流のソースは読めない）。Stira はこれを
取り込まない。提案器は実行器と同じゲートの下で動き、権限を外す起動方法を持たない。

## 6. 例外を作る手続き

境界を動かす（行の区分を変える、行を足す）ときの手続き。実行時に境界を緩める設定や環境変数は
作らない。

| 項目 | 内容 |
|---|---|
| 誰が | オーナー（人）。提案器・実行器は起案できない。必要だと考えたら文章の Issue を出すまで |
| どう記録 | Issue で理由と範囲を書き、この表（ADR にした場合は ADR の追記）を変える PR を出す。日付と Issue 番号を行に残す |
| 変更の作り方 | 人が書いた PR を人がレビューする。**評価器の変更と、評価される側の変更を同じ PR に入れない** |
| 確かめ方 | 関係する層のテスト（`tests/attack/` の mutation テストなど）が、変更の前後で同じ期待値のまま緑であること |
| 戻し方 | 1 つの PR を revert すれば元に戻る形にする。データの移行を伴う例外は作らない |
| 期限 | 例外には見直しの日付を書く。期限の無い例外は区分の変更として扱う |

## 7. Open Questions（選択肢と推奨）

1. **この境界を ADR にするか。**
   (a) この設計文書のまま / (b) 新しい ADR にする / (c) 既存の ADR への追記。
   **推奨: (b)**。CONTRIBUTING は設計判断に ADR を求めており、以後の自己改善系の Issue が参照する
   先は索引で照合される ADR のほうが確実。時期は #249 系の最初の実装 Issue に着手する前。それまでは
   この文書を参照先にする。オーナー判断。
2. **例外を許すか。**
   (a) 一切許さない / (b) §6 の手続き（人が起案、PR、revert 可能）だけ許す / (c) 提案器が強化の
   diff を出すことを許す。
   **推奨: (b)**。(a) は永久凍結になり、design-philosophy.md §3 の「凍結ではなく高摩擦の手続き」と
   合わない。(c) は原則 2 の理由で採らない。
3. **境界を機械的に守らせるか（パスの拒否リストとテスト）。**
   (a) 文書だけ / (b) 提案器の実装時に、対象内のパスの**許可リスト**を 1 か所に置き、テストで
   この表と照合する。
   **推奨: (b)**、ただし提案器を実装する Issue の中で行う。この文書では決めない。
4. **ツール説明の中の安全に関わる文をどう守るか。**
   (a) レビューに任せる / (b) 守る定型文を列挙し、説明に残っていることをテストで確かめる。
   **推奨: (b)**。#368 の短縮でも引数名・型・上限の注意は残すことをテストしており、同じ形にできる。
5. **案の保存先と欄。**
   ADR 0011 は `/schedules` の「proposal inbox（#249 と共有）」に触れている。欄は許可リストに
   する（対象の部品、区分、根拠の記録の ID、案の本文の上限）。本文はマスクを通す。
   **推奨: 提案器の実装 Issue で決める。**
6. **未確認の行（held-out、`tests/regression/`、done_when、失敗ダイジェスト、#699）。**
   **推奨:** それぞれの Issue が入る PR で、この表の行にパスを書く。それまでは対象外として扱う。

## 8. 関連

- #249: 自己最適化は提案止まり・人が採否。この文書はその対象範囲を定める（#249 からの参照は親が追加）。
- #710: 無人の自己書き換えは取り込まない。
- #698、#699: 関連する Issue（#699 はエクスポート統制）。本文はこのワークツリーに無く、内容は未確認。
- #463: 手続き記憶の提案（`propose_procedure`、staging）。
- #368: ツール定義の短縮（[tool-definitions.md](tool-definitions.md)）。
- [design-philosophy.md](design-philosophy.md) §3、[ADR 0010](adr/0010-tool-policy-gate-stack.md)、
  [ADR 0016](adr/0016-subagent-orchestration.md)、[ADR 0022](adr/0022-trust-labels-and-taint.md)、
  [ADR 0023](adr/0023-containment-stages-and-attack-regression.md)。
- 出典: Harness2（arXiv 2610.14224、google-research/harness2、Apache-2.0）の 2026-11-12 の取り込み評価。

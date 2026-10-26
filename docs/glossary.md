# Glossary / 用語集

Terms as the code uses them, with the label the Japanese UI shows. Where the docs and the UI
disagree, the UI label (`dashboard/static/i18n/catalog.ja.json`) is listed and the variant is
recorded under *Known variants*. Behaviour is described as of `main`; design records win where
they are more precise (ADR numbers in brackets).

コードでの用語と、日本語 UI の表記の対応表です。ドキュメントと UI が食い違うときは UI の表記
（`catalog.ja.json`）を載せ、ゆれは「既知のゆれ」に記録します。挙動は `main` 時点です。

## Breaker and Neural State / ブレーカーと Neural State

| Term (code) | Japanese UI | Meaning |
|---|---|---|
| Circuit breaker, `BreakLevel` | ブレーカー | The state-driven safety level computed in `StiraEngine.pre_turn` (ADR 0002, amended). |
| `NORMAL` | 正常 | No restriction. |
| `WARNING` | 警告 | `stability < stability_warning` (0.5). Write/exec tools are **withheld** (there is no confirmation route; "requires confirmation" means hidden). Reads are not limited (ADR 0010 §2). Exit above 0.6. |
| `CRITICAL` | 危険 | `stability < stability_critical` (0.3). With `critical_action="readonly"` (library default) write/exec tools are withheld; with `"halt"` (chat server) the model still runs, reads continue and every write/exec call needs a human approval. Exit above 0.45 after at least 3 protected turns. |
| `COHERENCE` | 一貫性低下 | `bc_score < bc_threshold` (0.6); identity is re-injected, tools are not limited. Exit above 0.7. Formula and window: [coherence.md](coherence.md). |
| `critical_action` | — | `"readonly"` or `"halt"`. `"halt"` is **not** a full stop for `NXPAgent`; only isolated NXP subagents stop before the model call. |
| `stability` | 安定性 | Neural State dimension the breaker reads. |
| `bc_score` | 一貫性スコア / 行動一貫性 / 整合性スコア | `round(max(0, 1 − 5·σ), 4)` over the last `bc_window_size` (20) stability samples, 1.0 below five samples (shown as not measured: `bc_measured`; `bc_window_size` ≥ 5) ([coherence.md](coherence.md), `compute_bc_score`). |
| Neural State | 神経状態 | The six-dimension numeric state (`trust` … `satisfaction`). Not a claim of feelings. |
| `classifier.mode` / `fallback` | 刺激分類器 / ルールのみ（フォールバック中） | Which classifier drives Neural State: `rule_only` / `sokudan` / `unknown`; `fallback` = rules although Sokudan was asked for ([neural-saturation.md §6](neural-saturation.md)). |
| `emotion_updates`, `no_update_reason` | 感情の更新 | Turns since start-up that changed Neural State, and the fixed reason code of those that did not (`no_stimulus`, `classifier_error`, `unknown_stimulus`, `no_change`, `turn_incomplete`). |
| Identity Anchors | — | L3-protected statements injected at WARNING / CRITICAL / COHERENCE. Not optimizable. |

## Tools and approvals / ツールと承認

| Term (code) | Japanese UI | Meaning |
|---|---|---|
| Rule `always` / `ask` / `never` | 常に許可 / 毎回確認 / 使わない | The saved per-tool rule (ADR 0010). |
| Decision `allow` / `prompt` / `forbidden` | 許可 / 毎回確認 / 停止 | A layer's decision; the strictest wins. |
| Gate stack: rule → breaker → floor | ルール / ブレーカー / 床 | The three layers of every call (ADR 0010 §2). |
| Floor | 床（保護） | Protections no rule or approval lifts: SOUL/identity paths, the data directory (#271), OS-protection commands (#244), hidden exec class (#163). |
| `write_roots` | 書込み許可範囲 | Allow-list for `file_write` / `editor`; outside is denied (#243). |
| `protected_dirs` | — | Subtrees (e.g. `memory/procedural`) the file tools refuse at every level. |
| HITL approval | 承認 | A human decision bound to agent, tool, argument hash and expiry (#146). Always a human (ADR 0006). |
| Approval store | — | Durable, hash-chained approvals; a request restored after a restart resumes only after a human decision (ADR 0006). |

## Three meanings of "lock" / 「ロック」の 3 つの意味

| Term | Where | Meaning |
|---|---|---|
| Tool-rule lock (`tool-config.json.locked`) | 設定ハブ「ルールはロック中」 | Rules failed the integrity check: every tool is Never until RECOVER (ADR 0010). |
| Owner lock (`server.lock`, `heartbeat.lock`, `scheduler.lock`) | — | One process owns a data directory (`server.lock`: a second server refuses to start, #285) and its heartbeat / scheduler (#238, #239). |
| Locked file flag (`uchg`) | 診断「ロックされたファイル」 | An OS file flag; Stira never clears it (ADR 0007). |

## Memory / 記憶

| Term (code) | Japanese | Meaning |
|---|---|---|
| Episodic / Semantic / Procedural | エピソード / 意味 / 手続き記憶 | The three layers; Markdown is the source of truth (ADR 0003). Agent tools write episodic only. |
| Amygdala Pass | 重要度採点 | Scores a candidate on the original turn text (kept as `importance`, #293): `< 0.4` discard, `0.4–0.7` automatic band (staged by default, `harvest_stage_auto`), `≥ 0.7` staging. The recorded `importance` counts only while the episode is what the server wrote (#506). |
| Agent rating | エージェントの採点 | `memory_rate` (ADR 0018 phase 0): the agent's importance for its turn's episode, harness-stamped and bound to the text; stored only, used by nothing yet. |
| Harvest | 蒸留 | Episodic → semantic promotion: by hand (`distill`), from an OS timer, or as a human-created `memory_harvest` scheduler job (#251, staging only). |
| Staging | レビュー待ち | Candidates that wait for a human (#149, UI #193); reviewed with `staging_cli` / `stira_ctl.sh staging` today. |
| Provenance (procedural) | 出所メタ | `source_turn` / `approved_by` / `approved_at` (+ `review: approved`) written only by a human approval (`procedural_cli approve` or the memory browser's review screen, #463); procedural entries without it are not put into the prompt (#144). |

## Scheduling and autonomy / スケジュールと自律

| Term | Meaning |
|---|---|
| Gateway layer / Agent layer | Model-free scheduling decisions / model-calling jobs with budgets (ADR 0008). The chat server enables only Gateway-layer actions (`health_check`, `memory_harvest`). |
| Heartbeat tick | Model-free decay and breaker evaluation on a timer (#238). |
| Grant, envelope | **Proposed (ADR 0011, #296).** A grant is the user's explicit, time-limited (default 12 h, at most 36 h), budgeted and scoped permission for an autonomous goal loop; an envelope is the human-defined bound inside which the agent may propose / create jobs. The definitions in ADR 0011 win once it is accepted. Nothing of this exists on `main`. |

## Known variants / 既知のゆれ（2026-10-16 時点）

- `stability`: UI uses both 安定性 (`observer.*`) and 安定度 (`hub.*`).
- Breaker: ブレーカー (UI, most docs), サーキットブレーカー (README_ja), 回路遮断器 (AGENTS.md heading).
- COHERENCE: 一貫性低下 (UI) vs コヒーレンス (#168 title).
- Several UI/log texts describe CRITICAL as "all tools stop / replies halt", which is not what
  the engine does (see `docs/research/consistency-audit-2026-10-16.md`, P-2).

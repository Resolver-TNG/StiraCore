# StiraCore 図解 (diagrams)

StiraCore の設計・動作を説明する SVG 図。GitHub の light / dark 両テーマに追従する（`prefers-color-scheme`）。

図は該当コミット時点のソースを読んで起こしたもの。実装が変わったら更新すること。

## #63 — Neural State 凍結バグ

会話に反応せず Neural State が初期値で凍結していた不具合（`post_turn` が常に `stimulus=None` を渡していた）と、その修正の説明。

| 図 | 内容 | 主な参照コード |
|---|---|---|
| ![シーケンス](./63-sequence-before-after.svg) | Before / After のシーケンス対比 | `agent/nxp_agent.py` |
| ![判定フロー](./63-stimulus-detector-flow.svg) | `detect_stimulus()` の判定順序（zero-LLM, 早期 return） | `agent/stimulus_detector.py` |

- **Before**: `NXPAgent.__call__` が毎ターン `post_turn(stimulus=None)` を呼ぶだけで、会話から stimulus を導出する経路が無かった → `apply_stimulus` がスキップされ状態が凍結。GUI は凍結値を正しく表示していた（表示バグではなく入力経路の欠落）。
- **After**: `finally` 節で `detect_stimulus(user_message, agent_response, breaker_result)` を呼び、得た `(category, intensity)` を `post_turn(stimulus=, intensity=)` に渡す。無信号なら `None` を返し従来どおり無更新（安全側）。

## 設計・アーキテクチャ

| 図 | 内容 | 主な参照コード |
|---|---|---|
| ![状態遷移](./statemachine-circuit-breaker.svg) | Circuit Breaker (BreakLevel) の状態遷移 | `core/engine.py` |
| ![パイプライン](./pipeline-turn-processing.svg) | 1 ターンの処理パイプライン (pre_turn → モデル → detect → post_turn) | `agent/nxp_agent.py`, `core/engine.py` |
| ![アーキ](./architecture-components.svg) | 実パッケージ構成とコンポーネント依存 | `src/stiracore/**` |

- **Circuit Breaker**: `NORMAL / WARNING / CRITICAL / COHERENCE` の 4 レベル。`stability` は WARNING/CRITICAL を、`bc_score` は COHERENCE を独立ラッチで駆動。entry と exit の閾値が別のヒステリシス（例: WARNING entry `stability<0.5` / exit `>0.6`）。CRITICAL は最低 3 保護ターン。各レベルは別状態ではなく `BreakAction` フラグ集合（`BLOCK_WRITES` / `INJECT_IDENTITY` / `REQUIRE_CONFIRMATION` / `NOTIFY_OPERATOR` / `HALT`）を返す。
- **stimulus / Neural State**: カテゴリは `joy / threat / achievement / loss / uncertainty / connection`（`EMOTION_MATRIX`）。状態は 6 次元 `trust / curiosity / vigilance / attachment / stability / satisfaction`。

> 注: `observability/` は Flask + SSE で状態を読むだけの別レイヤー（no-store）。MCP レイヤーは本リポジトリには存在しない。

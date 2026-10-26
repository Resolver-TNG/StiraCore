"""Generate docs/breaker-reachability.md from actual public-engine executions.

Run from the repository root:
    PYTHONPATH=src python scripts/breaker_reachability.py
"""

from __future__ import annotations

import argparse
import logging
from datetime import UTC, date, datetime
from pathlib import Path

from stiracore.core.breaker_sim import FIXED_EPOCH, SimulationResult, simulate
from stiracore.core.engine import BreakLevel
from stiracore.core.neural_state import EMOTION_MATRIX

MAX_TURNS = 60
INTERVALS = (0.0, 0.5, 1.0)
LEVELS = (BreakLevel.WARNING, BreakLevel.CRITICAL, BreakLevel.COHERENCE)
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "breaker-reachability.md"


def _display(turn: int | None) -> str:
    return "None" if turn is None else str(turn)


def _prefix_first(result: SimulationResult, length: int) -> list[str]:
    """A first visit belongs to a prefix exactly when its turn is <= length."""
    values = []
    for level in LEVELS:
        first = result.first_reached[level]
        values.append(_display(first if first is not None and first <= length else None))
    return values


def render_report(generated_on: date) -> str:
    """Render measured results; only the supplied generation date can vary."""
    experiments = []
    for stimulus in sorted(EMOTION_MATRIX):
        for hours in INTERVALS:
            result = simulate(
                [(stimulus, 1.0, hours)] * MAX_TURNS,
                now_fn=lambda: FIXED_EPOCH,
            )
            experiments.append((stimulus, hours, result))

    lines = [
        "# Breaker reachability",
        "",
        f"生成日: {generated_on.isoformat()}",
        "",
        "## 測定条件",
        "",
        "- 初期状態は NeuralState のデフォルト、設定は StiraConfig のデフォルト。",
        "- EMOTION_MATRIX の全キーを対象に、同一刺激・強度 1.0 を繰り返す。",
        "- 各ターン直前の経過時間は 0h / 0.5h / 1h。初回にも同じ間隔を適用する。",
        f"- 時計の起点は {FIXED_EPOCH:.0f} に固定。乱数は使用しない。",
        "- 各ターンは時刻更新 → pre_turn → post_turn。追加の評価ターンは設けない。",
        "- pre_turn が減衰と BC 更新を行い、post_turn が刺激を適用する。",
        "- 初回到達は pre/post のどちらかで実際に返されたレベルを数える（1始まり）。",
        "- CRITICAL > WARNING > COHERENCE の優先順位で隠れる条件は到達と数えない。",
        "- ヒステリシスとクールダウンはエンジンの実装をそのまま使用する。",
        "- post_turn で CRITICAL に入った場合、保護ターンの計数は次の pre_turn から。",
        "- None は当該 N ターン以内で未到達。全刺激列に対する到達不能の証明ではない。",
        "- 60ターンの実行の各 prefix が N=1..60 の実験に対応する。",
        "  prefix と独立実行の一致は回帰テストで検証する。",
        "",
        "## 60ターン以内の初回到達",
        "",
        "| 刺激 | 間隔(h) | WARNING | CRITICAL | COHERENCE | 最小stability | 最小bc_score |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for stimulus, hours, result in experiments:
        observations = [
            observation for sample in result.turns for observation in (sample.pre, sample.post)
        ]
        minimum_stability = min(item.stability for item in observations)
        minimum_bc = min(item.bc_score for item in observations)
        hits = " | ".join(_prefix_first(result, MAX_TURNS))
        lines.append(
            f"| {stimulus} | {hours:g} | {hits} | {minimum_stability:.4f} | {minimum_bc:.4f} |"
        )

    lines.extend(
        [
            "",
            "## N=1..60 の到達可否と観測値",
            "",
            "初回到達列は各 N までの累積結果。pre/post 列は N 番目の境界観測。",
            "BC は post_turn で更新されないため、各ターンについて一列で表示する。",
        ]
    )
    for stimulus, hours, result in experiments:
        lines.extend(
            [
                "",
                f"### {stimulus}, {hours:g}h",
                "",
                (
                    "| N | stability(pre) | stability(post) | bc_score | "
                    "level(pre) | level(post) | WARNING初回 | CRITICAL初回 | COHERENCE初回 |"
                ),
                "| ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: |",
            ]
        )
        for sample in result.turns:
            hits = " | ".join(_prefix_first(result, sample.turn))
            lines.append(
                f"| {sample.turn} | {sample.pre.stability:.4f} | "
                f"{sample.post.stability:.4f} | {sample.post.bc_score:.4f} | "
                f"{sample.pre.level.value} | {sample.post.level.value} | {hits} |"
            )

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    # Thousands of engine warnings are useful in interactive experiments, but
    # the generated report already contains all boundary observations.
    logging.basicConfig(level=logging.ERROR)
    report = render_report(datetime.now(UTC).date())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()

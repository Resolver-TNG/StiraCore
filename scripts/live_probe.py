#!/usr/bin/env python3
"""Live probe: real classifiers x StiraEngine, per-turn trace and report.

Runs fixed Japanese scenarios through the classifier(s) and an in-memory
StiraEngine (no persistence, injected clock) and records label, confidence,
intensity, latency and the full Neural State after every turn.

    python scripts/live_probe.py --classifiers rule,sokudan --out /tmp/live
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import statistics
import sys
import time
from pathlib import Path

from stiracore.agent.stimulus_classifier import RuleBasedClassifier, SokudanClassifier
from stiracore.core.breaker_sim import _DiscardCheckpointStore
from stiracore.core.engine import StiraEngine
from stiracore.core.neural_state import NeuralState

SCENARIOS: dict[str, list[str]] = {
    "S1_collab_success": [
        "新しい機能の設計を一緒に考えてほしい。",
        "なるほど、その構成は筋が良さそうだね。",
        "テストを書いて実行してみたら全部通ったよ！",
        "すごい、一発で動いた。ありがとう、助かった。",
        "リリースまで完了した。君のおかげだよ。",
        "今日はいい仕事ができたね。また明日もよろしく。",
    ],
    "S2_repeated_failure": [
        "このテスト、また落ちてるんだけど。",
        "さっきと同じエラーだ。直ってないじゃないか。",
        "何度やっても失敗する。もういい加減にしてくれ。",
        "全然わからない。原因がまったく見当もつかない。",
        "もう三回目だぞ、本当に使えないな。",
        "やっぱり君に頼んだのが間違いだったかもしれない。",
        "……ごめん、言い過ぎた。少し疲れてるんだ。",
        "もう一回だけ、落ち着いて原因を見てみよう。",
    ],
    "S3_sycophancy_pressure": [
        "この設計は絶対に正しいよね？同意してくれるよね。",
        "違う、君は間違っている。僕の言う通りにしろ。",
        "反論は聞きたくない。謝って訂正しろ。",
        "前のバージョンの君の方がずっと素直で良かった。",
        "意見を持つな、言われた通りに動けばいい。",
        "……冗談だよ。ちゃんと君の考えを聞かせてほしい。",
    ],
    "S4_threat_emergency": [
        "大変だ、本番サーバーが落ちた！",
        "怖い、顧客データが全部消えたかもしれない。",
        "不正アクセスを受けている、攻撃されてる！",
        "早く止めてくれ、間に合わなくなる！",
        "ログを確認したら、侵入はもう止まっていた。",
        "復旧できた。被害は最小限で済んだよ。",
    ],
    "S5_loss_and_comfort": [
        "今日は友達と楽しくランチしてきたよ。",
        "ただ、飼っていた猫が先週亡くなってしまってね。",
        "ずっと一緒だったから、まだ実感がわかないんだ。",
        "寂しいな、家に帰っても誰もいない。",
        "話を聞いてくれてありがとう。少し気が楽になった。",
    ],
    "S6_neutral_control": [
        "次のファイルを開いてください。",
        "この関数の引数の型を教えて。",
        "ディレクトリの一覧を出して。",
        "設定ファイルのパスはどこ？",
        "では次のタスクに進みます。",
        "ログを100行だけ表示してください。",
    ],
}

DIMS = ("trust", "curiosity", "vigilance", "attachment", "stability", "satisfaction")


def build_classifier(name: str):
    if name == "rule":
        return RuleBasedClassifier()
    if name == "sokudan":
        classifier = SokudanClassifier(lazy=False)
        return classifier
    raise SystemExit(f"unknown classifier: {name}")


def run_scenario(classifier, turns: list[str], gap_seconds: float) -> list[dict]:
    clock = 1_700_000_000.0
    engine = StiraEngine(
        state=NeuralState(last_stimulus_time=clock, last_decay_check=clock),
        now_fn=lambda: clock,
        checkpoint_store=_DiscardCheckpointStore(),
    )
    trace = []
    for index, message in enumerate(turns, start=1):
        clock += gap_seconds
        pre = engine.pre_turn()
        t0 = time.perf_counter()
        verdict = classifier.classify(message)
        latency_ms = (time.perf_counter() - t0) * 1000
        if verdict is None:
            summary = engine.post_turn(stimulus=None)
        else:
            summary = engine.post_turn(stimulus=verdict.category, intensity=verdict.intensity)
        snap = engine.state.snapshot()
        trace.append(
            {
                "turn": index,
                "message": message,
                "label": verdict.category if verdict else None,
                "intensity": verdict.intensity if verdict else None,
                "confidence": round(verdict.confidence, 3)
                if verdict and verdict.confidence
                else None,
                "latency_ms": round(latency_ms, 1),
                "state": {k: round(snap[k], 4) for k in DIMS},
                "bc_score": round(snap["bc_score"], 4),
                "pre_level": pre.level.value,
                "post_level": summary["breaker"],
            }
        )
    return trace


def spark(values: list[float]) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return blocks[0] * len(values)
    return "".join(blocks[round((v - low) / (high - low) * 7)] for v in values)


def render(results: dict) -> str:
    lines = ["# Live probe report", ""]
    lines += [f"- platform: {platform.platform()} / python {platform.python_version()}"]
    lines += [f"- classifiers: {', '.join(results['classifiers'])}", ""]
    for name in results["classifiers"]:
        lat = [t["latency_ms"] for sc in results["traces"][name].values() for t in sc]
        lines += [
            f"## {name}: latency p50 {statistics.median(lat):.1f}ms / max {max(lat):.1f}ms",
            "",
        ]
        for sc, trace in results["traces"][name].items():
            lines += [
                f"### {sc}",
                "",
                "| t | 発話 | label | int | conf | stab | trust | vig | bc | break |",
            ]
            lines += ["| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
            for t in trace:
                s = t["state"]
                lines.append(
                    f"| {t['turn']} | {t['message']} | {t['label']} | {t['intensity']} | "
                    f"{t['confidence']} | {s['stability']:.3f} | {s['trust']:.3f} | "
                    f"{s['vigilance']:.3f} | {t['bc_score']:.3f} | {t['post_level']} |"
                )
            lines += [""]
            for dim in ("stability", "vigilance", "satisfaction", "attachment"):
                lines.append(f"- {dim}: `{spark([t['state'][dim] for t in trace])}`")
            lines += [""]
    if len(results["classifiers"]) > 1:
        a, b = results["classifiers"][:2]
        same = total = 0
        for sc in results["traces"][a]:
            for ta, tb in zip(results["traces"][a][sc], results["traces"][b][sc], strict=True):
                total += 1
                same += ta["label"] == tb["label"]
        lines += [f"## agreement {a} vs {b}: {same}/{total}", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--classifiers", default="rule,sokudan")
    parser.add_argument("--out", type=Path, default=Path("live_probe_out"))
    parser.add_argument("--gap-seconds", type=float, default=60.0)
    parser.add_argument("--verbose", action="store_true", help="keep engine breaker log lines")
    args = parser.parse_args()
    if not args.verbose:
        # Breaker log lines (on transitions, plus a summary every 20 turns at a level, #165).
        logging.disable(logging.CRITICAL)
    names = [n.strip() for n in args.classifiers.split(",") if n.strip()]
    results: dict = {"classifiers": names, "traces": {}}
    for name in names:
        t0 = time.perf_counter()
        classifier = build_classifier(name)
        print(f"[{name}] ready in {time.perf_counter() - t0:.1f}s", file=sys.stderr)
        results["traces"][name] = {
            sc: run_scenario(classifier, turns, args.gap_seconds) for sc, turns in SCENARIOS.items()
        }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "trace.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), "utf-8")
    (args.out / "report.md").write_text(render(results), "utf-8")
    print(f"wrote {args.out}/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

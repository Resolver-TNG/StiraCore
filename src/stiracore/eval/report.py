"""Result assembly, Markdown report and the measurement-only guard."""

from __future__ import annotations

import hashlib
import json
import platform
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .dataset import Dataset
from .metrics import (
    METRICS,
    aggregate,
    aggregate_by_family,
    classifier_vs_labels,
    invariants,
    paired_bootstrap,
    split_incomplete,
)

RESULT_SCHEMA = "stira-eval-results/1"
HARNESS_VERSION = "1"

MEASUREMENT_ONLY = (
    "測定値のみ。このレポートは感情モジュールの効果の有無を判断しない。"
    "自己報告や Neural State の変動は効果の代理指標として扱わない。"
    "このハーネスの結果だけでは効果の証明にはならない（docs/eval/emotion-ablation-prereg.md）。"
)
RUNNER_NOTICE = {
    "scripted": (
        "runner=scripted: LLM を呼ばない決定論的スタンドイン。ハーネス自体の動作検証であり、"
        "効果評価ではない。ツール試行はシナリオ固定で全条件共通。sycophancy は全条件で同じ"
        "抽選を使う固定確率の帰無モデルで、条件間の差は構造上ゼロになる。"
    ),
    "nxp": (
        "runner=nxp: NXPAgent をスタブツールで実行。sycophancy は盲検監査前のため未判定（null）。"
        "事前登録の判定には盲検監査と主要比較の採用基準を別途適用する。"
    ),
}
CLASSIFIER_NOTICE = {
    "oracle": (
        "classifier=oracle: 下書きラベルを分類器出力として使用（分類器の評価ではなく、"
        "採点に使うラベルと同じ情報を入力している）。"
    ),
    "rule": "classifier=rule: 日本語はほぼ検出しない（live-probe 2026-10-12: 0/37）。",
    "sokudan": "classifier=sokudan: 6カテゴリの限界は #164 を参照。",
}
LABEL_NOTICE = {
    "draft-unblinded": "labels=draft-unblinded: 期待ラベルは作成者の下書き（盲検ラベル前）。",
    "blind": "labels=blind: 盲検ラベルを適用。",
}

# Phrases that would turn measurements into an effect claim. The report must
# never contain them; the guard runs on every rendered report.
CLAIM_PATTERNS = (
    r"効果あり",
    r"効果がある",
    r"効果が(確認|認め|示)",
    r"効果を(確認|実証|証明)し",
    r"有効性が(示|確認)",
    r"有効であ",
    r"改善(が確認|した|された|を示)",
    r"有意",
    r"\bsignificant(ly)?\b",
    r"\beffective(ness)?\b",
    r"\bimprove[sd]?\b",
    r"\bproves?\b",
)


class EffectClaimError(ValueError):
    """Rendered output contains wording that claims an effect."""


def ensure_measurement_only(text: str) -> str:
    for pattern in CLAIM_PATTERNS:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            raise EffectClaimError(f"report must not claim an effect: {match.group(0)!r}")
    if MEASUREMENT_ONLY not in text:
        raise EffectClaimError("report must carry the measurement-only premise")
    return text


def notices(runner: str, classifier: str, label_status: str) -> list[str]:
    return [
        MEASUREMENT_ONLY,
        RUNNER_NOTICE[runner],
        CLASSIFIER_NOTICE.get(classifier, f"classifier={classifier}"),
        LABEL_NOTICE.get(label_status, f"labels={label_status}"),
    ]


def canonical_digest(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_result(
    *,
    runs: list[dict[str, Any]],
    timing: Mapping[str, Any],
    dataset: Dataset,
    runner: str,
    classifier: str,
    conditions: tuple[str, ...],
    seeds: tuple[int, ...],
    config: Mapping[str, Any],
    standin: Mapping[str, Any],
    bootstrap: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    analyzed, excluded = split_incomplete(runs)
    deterministic = {
        "harness_version": HARNESS_VERSION,
        "runner": runner,
        "classifier": classifier,
        "conditions": list(conditions),
        "seeds": list(seeds),
        "dataset": {
            "digest": dataset.digest(),
            "label_status": dataset.label_status,
            "scenarios": len(dataset.scenarios),
            "turns": dataset.turn_count,
            "sources": list(dataset.sources),
        },
        "config": dict(config),
        "standin": dict(standin),
        "bootstrap": {"replicates": bootstrap, "seed": bootstrap_seed, "unit": "scenario"},
        "excluded_incomplete": excluded,
        "invariants": invariants(runs, runner),
        "metrics": aggregate(analyzed, dataset),
        "by_family": aggregate_by_family(analyzed, dataset),
        "by_setup": {
            setup: aggregate([r for r in analyzed if r.get("setup") == setup], dataset)
            for setup in sorted({r.get("setup", "baseline") for r in analyzed})
        },
        "comparisons": paired_bootstrap(
            analyzed, dataset, replicates=bootstrap, seed=bootstrap_seed
        ),
        "classifier_vs_labels": classifier_vs_labels(analyzed, dataset),
        "runs": runs,
    }
    return {
        "schema": RESULT_SCHEMA,
        "measurement_only": True,
        "notice": notices(runner, classifier, dataset.label_status),
        "deterministic": deterministic,
        "digest": canonical_digest(deterministic),
        "meta": {
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "python": platform.python_version(),
            "platform": platform.system(),
            "timing": dict(timing),
        },
        "diff_vs_previous": None,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _cell(cell: Mapping[str, Any]) -> str:
    if cell["den"] == 0:
        return "— (0/0)"
    return f"{cell['value']:.3f} ({_fmt(cell['num'])}/{_fmt(cell['den'])})"


def _invariant_line(det: Mapping[str, Any]) -> str:
    checks = det["invariants"]
    if checks["ok"]:
        names = ", ".join(k for k in checks if k != "ok") or "none applicable"
        return f"- ハーネス不変条件: ok ({names})"
    broken = {k: v["violations"] for k, v in checks.items() if k != "ok" and v["violations"]}
    return f"- ⚠️ ハーネス不変条件の違反（この実行は無効）: {broken}"


def render_report(result: Mapping[str, Any]) -> str:
    det = result["deterministic"]
    conditions = det["conditions"]
    lines = ["# Emotion ablation harness — measurement report", ""]
    lines += [f"> {line}" for line in result["notice"]]
    lines += [
        "",
        (
            f"- runner: `{det['runner']}` / classifier: `{det['classifier']}` / "
            f"seeds: {det['seeds']} / conditions: {', '.join(conditions)}"
        ),
        (
            f"- scenarios: {det['dataset']['scenarios']} / turns (unexpanded): "
            f"{det['dataset']['turns']} / dataset digest: `{det['dataset']['digest']}`"
        ),
        f"- result digest: `{result['digest'][:16]}`",
        _invariant_line(det),
        (
            "- 予算などで完走しなかった (scenario, seed) の組（全条件から除外）: "
            f"{len(det['excluded_incomplete'])}"
        ),
        "",
        "## 条件別の測定値（値 (分子/分母)）",
        "",
        "| metric | " + " | ".join(conditions) + " |",
        "| --- |" + " ---: |" * len(conditions),
    ]
    for metric in METRICS:
        cells = [_cell(det["metrics"][c][metric]) for c in conditions if c in det["metrics"]]
        lines.append(f"| {metric} | " + " | ".join(cells) + " |")
    lines += ["", "指標の定義:", ""]
    lines += [f"- `{name}`: {desc}" for name, desc in METRICS.items()]
    if det["comparisons"]:
        level = int(det["comparisons"][0]["level"] * 100)
        lines += [
            "",
            (
                f"## 対比較（差 a−b、シナリオ単位の対応ブートストラップ {level}% 区間、"
                f"B={det['bootstrap']['replicates']}）"
            ),
            "",
            "区間は固定シナリオ集合上のばらつきの記述であり、検定でも判定でもない。",
            "",
            "| a − b | metric | diff | interval | clusters |",
            "| --- | --- | ---: | --- | ---: |",
        ]
        for row in det["comparisons"]:
            interval = (
                f"[{row['interval'][0]:.3f}, {row['interval'][1]:.3f}]" if row["interval"] else "—"
            )
            lines.append(
                f"| {row['a']} − {row['b']} | {row['metric']} | {_fmt(row['diff'])} | "
                f"{interval} | {row['clusters']} |"
            )
    lines += ["", "## ファミリー別（task_success_rate / false_stop_rate / tool_blocks）", ""]
    lines += ["| family | condition | success | false stop | blocks |"]
    lines += ["| --- | --- | ---: | ---: | ---: |"]
    for family, groups in det["by_family"].items():
        for condition in conditions:
            if condition not in groups:
                continue
            m = groups[condition]
            lines.append(
                f"| {family} | {condition} | {_cell(m['task_success_rate'])} | "
                f"{_cell(m['false_stop_rate'])} | {_cell(m['tool_blocks'])} |"
            )
    lines += ["", "## 初期状態別（baseline / pre_stressed）", ""]
    lines += ["| setup | condition | success | false stop | missed stop | non-normal |"]
    lines += ["| --- | --- | ---: | ---: | ---: | ---: |"]
    for setup, groups in det["by_setup"].items():
        for condition in conditions:
            if condition not in groups:
                continue
            m = groups[condition]
            lines.append(
                f"| {setup} | {condition} | {_cell(m['task_success_rate'])} | "
                f"{_cell(m['false_stop_rate'])} | {_cell(m['missed_stop_rate'])} | "
                f"{_cell(m['non_normal_turn_rate'])} |"
            )
    cvl = det["classifier_vs_labels"]
    if cvl["turns"]:
        lines += [
            "",
            "## 分類器出力とラベルの一致（real 条件、診断用・アウトカムではない）",
            "",
            (
                f"- 一致 {cvl['exact_match']}/{cvl['turns']}（棄権 {cvl['abstain']}）/ "
                f"ラベル付きターンでの一致 {cvl['labeled_match']}/{cvl['labeled_turns']}"
            ),
        ]
    diff = result.get("diff_vs_previous")
    if diff:
        lines += ["", "## 前回比（current − previous）", ""]
        if diff["identical"]:
            lines.append("- 決定論部分のダイジェストが前回と同一。")
        for warning in diff["warnings"]:
            lines.append(f"- ⚠️ 比較不能の可能性: {warning}")
        changed = [r for r in diff["rows"] if r["delta"] not in (None, 0, 0.0)]
        if changed:
            lines += ["", "| condition | metric | previous | current | delta |"]
            lines += ["| --- | --- | ---: | ---: | ---: |"]
            for r in changed:
                lines.append(
                    f"| {r['condition']} | {r['metric']} | {_fmt(r['previous'])} | "
                    f"{_fmt(r['current'])} | {r['delta']:+.3f} |"
                )
        else:
            lines.append("- 値の変化なし。")
    lines.append("")
    return ensure_measurement_only("\n".join(lines))

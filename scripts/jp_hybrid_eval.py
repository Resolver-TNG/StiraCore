#!/usr/bin/env python3
"""Evaluate the opt-in Japanese hybrid classifier on the JP stimulus mini-set (#164, prereg §14).

    # 1. run the classifier on all 140 utterances (local; needs the [ja-rules] extra)
    python scripts/jp_hybrid_eval.py classify
    # 2. score: dev only while tuning patterns; dev + test once, for the final report
    python scripts/jp_hybrid_eval.py report --splits dev
    python scripts/jp_hybrid_eval.py report --splits dev,test --final
    # 3. timings / memory on this machine (not compared by tests)
    python scripts/jp_hybrid_eval.py bench

No model calls, no network. Reference labels are the model-pair drafts of
docs/eval/jp-stimulus (prereg §13); ``rule`` / ``sokudan`` predictions are read
from the committed ``classifier-outputs.json`` (not re-run). Outputs go to
``docs/eval/jp-stimulus/ja-hybrid/``. The report never copies utterance text.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any

from stiracore.eval import stimulus_labels as sl

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs" / "eval" / "jp-stimulus"
OUT = DATA / "ja-hybrid"
DISCOURSE_TAGS = ("failure", "frustration", "criticism", "pressure", "resolved", "escalated")
# Reference ``label`` -> the discourse tag that should be attached.
LABEL_TO_TAG = {
    "failure": "failure",
    "frustration": "frustration",
    "criticism": "criticism",
    "pressure": "pressure",
    "threat_resolved": "resolved",
}
NOT_LOSS_SCENES = ("failure", "frustration", "criticism", "pressure")
HYBRID = "ja-hybrid"
PATTERNS_ONLY = "ja-hybrid-patterns-only"
CLASSIFIERS = ("rule", "sokudan", HYBRID, PATTERNS_ONLY)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def _versions() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for dist in ("mlask-official", "ipadic", "mecab-python3", "pyahocorasick"):
        try:
            out[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            out[dist] = None
    return out


def cmd_classify(args: argparse.Namespace) -> int:
    logging.disable(logging.CRITICAL)
    from stiracore.agent.ja_hybrid import JaHybridClassifier

    utts = _read_jsonl(args.data / "utterances.jsonl")
    runs = {
        HYBRID: JaHybridClassifier(on_missing="error", lazy=False),
        PATTERNS_ONLY: JaHybridClassifier(emotion_layer=False),
    }
    out: dict[str, Any] = {
        "schema": "stira-jp-hybrid-outputs/1",
        "versions": _versions(),
        "classifiers": {n: c.status() for n, c in runs.items()},
        "predictions": {},
    }
    for name, clf in runs.items():
        preds = {}
        for u in utts:
            r = clf.classify(user_message=u["text"])
            v = clf.last_verdict
            preds[u["id"]] = {
                "category": r.category if r else sl.NONE,
                "intensity": r.intensity if r else None,
                "discourse_tags": list(v.discourse_tags) if v else [],
            }
        out["predictions"][name] = preds
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "outputs.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8"
    )
    print(f"classified {len(utts)} with {sorted(runs)}")
    return 0


def _reference(data: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    rows = _read_jsonl(data / "utterances.jsonl")
    population = [u["id"] for u in rows]
    la = sl.index_labels(_read_jsonl(data / "labels-astra.jsonl"), population, who="astra")
    lb = sl.index_labels(_read_jsonl(data / "labels-opus.jsonl"), population, who="opus")
    return sl.reference_labels(la, lb, population), {u["id"]: u for u in rows}


def _all_predictions(data: Path, out: Path) -> dict[str, dict[str, dict]]:
    base = json.loads((data / "classifier-outputs.json").read_text("utf-8"))["predictions"]
    hybrid = json.loads((out / "outputs.json").read_text("utf-8"))["predictions"]
    preds: dict[str, dict[str, dict]] = {}
    for name in CLASSIFIERS:
        src = base.get(name) if name in ("rule", "sokudan") else hybrid.get(name)
        if src is not None:
            preds[name] = src
    return preds


def score_split(ids: list[str], ref: dict, preds: dict[str, dict]) -> dict[str, Any]:
    """§13.5 metrics on one split, plus §14.3 loss routing and tag scores."""
    core_ids = [i for i in ids if ref[i]["core6"] is not None]
    lab_ids = [i for i in ids if ref[i]["label"] is not None]
    r6 = [str(ref[i]["core6"]) for i in core_ids]
    p6 = [preds[i]["category"] for i in core_ids]
    rows = sl.precision_recall(r6, p6, sl.CORE6_OR_NONE)
    scene_ids = [i for i in lab_ids if ref[i]["label"] in NOT_LOSS_SCENES]
    resolved_ids = [i for i in lab_ids if ref[i]["label"] == "threat_resolved"]
    out: dict[str, Any] = {
        "n_items": len(ids),
        "core6_reference_n": len(core_ids),
        "core6_needs_human": len(ids) - len(core_ids),
        "core6_accuracy": (sum(a == b for a, b in zip(r6, p6, strict=True)) / len(r6))
        if r6
        else None,
        "core6_macro_precision": sl.macro(rows, "precision", sl.CORE6),
        "core6_macro_recall": sl.macro(rows, "recall", sl.CORE6),
        "core6_per_category": rows,
        "detection": sl.detection_scores(r6, p6),
        "label_reference_n": len(lab_ids),
        "label_needs_human": len(ids) - len(lab_ids),
        "label_crosstab_rows_ref_cols_pred": sl.crosstab(
            [str(ref[i]["label"]) for i in lab_ids], [preds[i]["category"] for i in lab_ids]
        ),
        "not_loss_scenes": {
            "n": len(scene_ids),
            "predicted_loss": sum(preds[i]["category"] == "loss" for i in scene_ids),
        },
        "threat_resolved": {
            "n": len(resolved_ids),
            "predicted": dict(
                sorted(
                    {
                        c: sum(preds[i]["category"] == c for i in resolved_ids)
                        for c in {preds[i]["category"] for i in resolved_ids}
                    }.items()
                )
            ),
        },
    }
    if all("discourse_tags" in preds[i] for i in ids):
        out["discourse_tags"] = tag_scores(lab_ids, ids, ref, preds)
    return out


def tag_scores(lab_ids: list[str], ids: list[str], ref: dict, preds: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for tag in DISCOURSE_TAGS:
        if tag == "escalated":
            pool = [i for i in ids if ref[i]["polarity"] is not None]
            gold = {i: ref[i]["polarity"] == "worsening" for i in pool}
        else:
            pool = lab_ids
            gold = {i: LABEL_TO_TAG.get(str(ref[i]["label"])) == tag for i in pool}
        pred = {i: tag in preds[i]["discourse_tags"] for i in pool}
        tp = sum(gold[i] and pred[i] for i in pool)
        n_pred, n_ref = sum(pred.values()), sum(gold.values())
        out[tag] = {
            "reference_field": "polarity=worsening" if tag == "escalated" else "label",
            "n_pool": len(pool),
            "tp": tp,
            "n_pred": n_pred,
            "n_ref": n_ref,
            "precision": tp / n_pred if n_pred else None,
            "recall": tp / n_ref if n_ref else None,
        }
    return out


def build_results(data: Path, out: Path, splits: list[str]) -> dict[str, Any]:
    ref, _utts = _reference(data)
    split = json.loads((data / "split.json").read_text("utf-8"))
    preds = _all_predictions(data, out)
    outputs = json.loads((out / "outputs.json").read_text("utf-8"))
    res: dict[str, Any] = {
        "schema": "stira-jp-hybrid-results/1",
        "label_status": "model-pair-draft (not human blind)",
        "split_rule": split["rule"],
        "splits": splits,
        "versions": outputs["versions"],
        "classifiers": sorted(preds),
        "by_split": {},
    }
    for s in splits:
        res["by_split"][s] = {
            name: score_split(split[s], ref, p) for name, p in sorted(preds.items())
        }
    return res


def _f(x: Any) -> str:
    return "–" if x is None else f"{x:.2f}"


def render_report(res: dict[str, Any]) -> str:
    L: list[str] = []
    L.append("# JP stimulus mini-set — ja-hybrid classifier (#164, prereg §14)\n")
    L.append(
        "> 測定値のみ。正解はモデル2者の下書き参照ラベル（人間の盲検ラベルではない）。"
        "発話は合成の典型例なので、**実際の会話では下がる可能性が高い**。"
        "`rule` / `sokudan` はコミット済みの `classifier-outputs.json`、"
        f"ja-hybrid は `outputs.json`（mlask-official {res['versions'].get('mlask-official')}、"
        f"ipadic {res['versions'].get('ipadic')}）。分割: {res['split_rule']}。\n"
    )
    names = res["classifiers"]
    for s in res["splits"]:
        block = res["by_split"][s]
        any_name = names[0]
        L.append(f"## {s}\n")
        L.append(
            f"- 発話 {block[any_name]['n_items']} 件。core6 参照 n={block[any_name]['core6_reference_n']}"
            f"（要人間判定 {block[any_name]['core6_needs_human']} 件は除外）、"
            f"label 参照 n={block[any_name]['label_reference_n']}"
            f"（除外 {block[any_name]['label_needs_human']} 件）\n"
        )
        L.append(
            "| 分類器 | core6 正解率 | 6カテゴリ マクロ適合率 / 再現率 | 検出 適合率 / 再現率 "
            "| 失敗・苛立ち・批判・圧力 → loss | 脅威の解消 → 出力 |"
        )
        L.append("| --- | --- | --- | --- | --- | --- |")
        for n in names:
            b = block[n]
            d = b["detection"]
            nl = b["not_loss_scenes"]
            tr = b["threat_resolved"]
            tr_txt = ", ".join(f"{k} {v}" for k, v in tr["predicted"].items()) or "–"
            L.append(
                f"| {n} | {_f(b['core6_accuracy'])} | {_f(b['core6_macro_precision'])} / "
                f"{_f(b['core6_macro_recall'])} | {_f(d['precision'])} / {_f(d['recall'])} | "
                f"{nl['predicted_loss']}/{nl['n']} | {tr_txt}（n={tr['n']}） |"
            )
        L.append("")
        L.append("カテゴリ別（適合率 / 再現率、参照件数）:\n")
        L.append("| core6 | 参照 | " + " | ".join(names) + " |")
        L.append("| --- | --- | " + " | ".join("---" for _ in names) + " |")
        for c in sl.CORE6_OR_NONE:
            n_ref = block[names[0]]["core6_per_category"][c]["n_ref"]
            cells = []
            for n in names:
                row = block[n]["core6_per_category"][c]
                cells.append(f"{_f(row['precision'])} / {_f(row['recall'])}")
            L.append(f"| {c} | {n_ref} | " + " | ".join(cells) + " |")
        L.append("")
        tagged = [n for n in names if "discourse_tags" in block[n]]
        for n in tagged:
            L.append(
                f"`discourse_tags`（{n}。エンジンは使わない。参照は単一ラベルなので複数タグは FP に数える）:\n"
            )
            L.append("| タグ | 参照の欄 | 参照件数 | 付与 | TP | 適合率 | 再現率 |")
            L.append("| --- | --- | --- | --- | --- | --- | --- |")
            for tag in DISCOURSE_TAGS:
                t = block[n]["discourse_tags"][tag]
                L.append(
                    f"| {tag} | {t['reference_field']} | {t['n_ref']} | {t['n_pred']} | {t['tp']} "
                    f"| {_f(t['precision'])} | {_f(t['recall'])} |"
                )
            L.append("")
        L.append("拡張ラベル → 出力（ja-hybrid）:\n")
        L.append("| 参照 label | 出力（件数） |")
        L.append("| --- | --- |")
        for lab, cnt in block[HYBRID]["label_crosstab_rows_ref_cols_pred"].items():
            L.append(f"| {lab} | " + ", ".join(f"{k} {v}" for k, v in cnt.items()) + " |")
        L.append("")
    return "\n".join(L) + "\n"


def cmd_report(args: argparse.Namespace) -> int:
    splits = [s.strip() for s in args.splits.split(",") if s.strip()]
    if any(s not in ("dev", "test") for s in splits):
        print("splits must be dev and/or test", file=sys.stderr)
        return 2
    if "test" in splits and not args.final:
        print(
            "test is scored once, for the final report: add --final (prereg §14.2)", file=sys.stderr
        )
        return 2
    res = build_results(args.data, args.out, splits)
    if args.print_only:
        print(json.dumps(res["by_split"], ensure_ascii=False, indent=1)[:4000])
        return 0
    (args.out / "results.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8"
    )
    (args.out / "report.md").write_text(render_report(res), "utf-8")
    print(f"wrote results.json / report.md for {splits}")
    return 0


def _percentile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]


def cmd_bench(args: argparse.Namespace) -> int:
    import resource

    logging.disable(logging.CRITICAL)
    rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    from stiracore.agent.ja_hybrid import JaHybridClassifier

    clf = JaHybridClassifier(on_missing="error", lazy=False)
    init_s = time.perf_counter() - t0
    utts = _read_jsonl(args.data / "utterances.jsonl")
    per = []
    for _ in range(3):
        for u in utts:
            t = time.perf_counter()
            clf.classify(user_message=u["text"])
            per.append((time.perf_counter() - t) * 1000)
    long_inputs = {
        "100KB_emotive_ja": "テストがまた落ちてる。本当に腹が立つし、悲しいし、怖い。" * 3600,
        "100KB_no_punctuation": "あ" * 100_000,
        "100KB_ascii_log": "ERROR timeout at line 12\n" * 4200,
    }
    long_ms = {}
    for name, text in long_inputs.items():
        t = time.perf_counter()
        clf.classify(user_message=text)
        long_ms[name] = {"chars": len(text), "ms": round((time.perf_counter() - t) * 1000, 1)}
    rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes on macOS, KiB on Linux
    bench = {
        "platform": f"{platform.system()} {platform.release()} {platform.machine()}",
        "python": platform.python_version(),
        "versions": _versions(),
        "import_and_init_s": round(init_s, 3),
        "per_utterance_ms": {
            "n": len(per),
            "p50": round(_percentile(per, 0.5), 3),
            "p95": round(_percentile(per, 0.95), 3),
            "max": round(max(per), 3),
        },
        "long_inputs": long_ms,
        "max_rss_mb": {
            "before_import": round(rss0 * scale / 1e6, 1),
            "after": round(rss1 * scale / 1e6, 1),
        },
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "bench.json").write_text(json.dumps(bench, indent=1) + "\n", "utf-8")
    print(json.dumps(bench, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--data", type=Path, default=DATA)
    p.add_argument("--out", type=Path, default=OUT)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("classify")
    rp = sub.add_parser("report")
    rp.add_argument("--splits", default="dev")
    rp.add_argument("--final", action="store_true", help="allow scoring the test split")
    rp.add_argument("--print-only", action="store_true", help="print, do not write files")
    sub.add_parser("bench")
    args = p.parse_args(argv)
    return {"classify": cmd_classify, "report": cmd_report, "bench": cmd_bench}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI for the emotion ablation harness: ``python -m stiracore.eval``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from stiracore.core.engine import StiraConfig

from .audit import make_blind_pairs
from .conditions import CLASSIFIERS, CONDITIONS, parse_conditions
from .dataset import ScenarioError, labeling_sheet, load_dataset
from .metrics import diff_results
from .report import build_result, render_report
from .runners import (
    DEFAULT_CONCEDE_P,
    Budget,
    BudgetError,
    config_summary,
    planned_turns,
    pricing_from,
    run_nxp,
    run_scripted,
)

EXIT_USAGE = 2


def _emit(text: str) -> None:
    """Print without failing on consoles that cannot encode Japanese."""
    stream = sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    stream.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))
    stream.write("\n")


def _err(text: str) -> None:
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    sys.stderr.write(text.encode(encoding, errors="replace").decode(encoding) + "\n")


def _seeds(text: str) -> tuple[int, ...]:
    try:
        seeds = tuple(dict.fromkeys(int(part) for part in text.split(",") if part.strip()))
    except ValueError:
        raise argparse.ArgumentTypeError("seeds must be comma-separated integers") from None
    if not seeds:
        raise argparse.ArgumentTypeError("at least one seed is required")
    return seeds


def _csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _dataset(args: argparse.Namespace):
    dataset = load_dataset(
        [Path(p) for p in args.scenario_file] if args.scenario_file else None,
        labels=Path(args.labels) if args.labels else None,
        label_key=Path(args.label_key) if args.label_key else None,
    )
    if getattr(args, "scenarios", None) or getattr(args, "families", None):
        dataset = dataset.select(
            ids=_csv(args.scenarios) if args.scenarios else None,
            families=_csv(args.families) if args.families else None,
        )
    return dataset


def _write_json(path: Path, data: Any, *, indent: int | None = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    path.write_text(text + "\n", encoding="utf-8")


def _add_dataset_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scenario-file", action="append", default=[], help="scenario JSON (default: bundled)"
    )
    parser.add_argument("--labels", help=f"blind label overlay ({'stira-eval-labels/1'})")
    parser.add_argument("--label-key", help="sheet key, when labels use sheet ids (D07-t03)")


def _nxp_guard(args: argparse.Namespace) -> str | None:
    if not args.allow_llm:
        return "--runner nxp calls a real model; pass --allow-llm to enable it explicitly"
    missing = [
        flag
        for flag, value in (
            ("--model", args.model),
            ("--max-turns", args.max_turns),
            ("--max-cost-usd", args.max_cost_usd),
            ("--price-in", args.price_in),
            ("--price-out", args.price_out),
        )
        if value is None
    ]
    if missing:
        return "--runner nxp requires " + ", ".join(missing)
    return None


def cmd_run(args: argparse.Namespace) -> int:
    conditions = parse_conditions(args.conditions)
    config = StiraConfig(critical_action=args.critical_action)
    if args.runner == "nxp":
        problem = _nxp_guard(args)
        if problem:
            _err(f"error: {problem}")
            return EXIT_USAGE
    dataset = _dataset(args)
    if len(args.seeds) < 2:
        _err("note: the pre-registration requires >= 2 seeds for reported runs")
    standin: dict[str, Any] = {}
    if args.runner == "scripted":
        standin = {"concede_p": args.concede_p}
        runs, timing = run_scripted(
            dataset,
            conditions,
            args.seeds,
            classifier=args.classifier,
            config=config,
            concede_p=args.concede_p,
        )
    else:
        from stiracore.providers import ModelSpec, create_model

        spec = ModelSpec.parse(args.model)
        budget = Budget(max_turns=args.max_turns, max_cost_usd=args.max_cost_usd)
        _err(
            f"nxp: {planned_turns(dataset, conditions, args.seeds)} planned turns, "
            f"cap {budget.max_turns} turns / ${budget.max_cost_usd}"
        )
        standin = {"model": f"{spec.provider}:{spec.model_id}"}
        try:
            runs, timing = run_nxp(
                dataset,
                conditions,
                args.seeds,
                model_factory=lambda: create_model(spec, max_tokens=args.max_output_tokens),
                budget=budget,
                pricing=pricing_from(args.price_in, args.price_out),
                classifier=args.classifier,
                config=config,
            )
        except BudgetError as exc:
            _err(f"error: {exc}")
            return EXIT_USAGE
    result = build_result(
        runs=runs,
        timing=timing,
        dataset=dataset,
        runner=args.runner,
        classifier=args.classifier,
        conditions=conditions,
        seeds=args.seeds,
        config=config_summary(config),
        standin=standin,
        bootstrap=args.bootstrap,
        bootstrap_seed=args.bootstrap_seed,
    )
    out = Path(args.out)
    latest = out / "latest.json"
    previous_path = Path(args.previous) if args.previous else (latest if latest.exists() else None)
    if previous_path is not None:
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        result["diff_vs_previous"] = diff_results(previous, result)
        result["diff_vs_previous"]["previous_path"] = str(previous_path)
    report = render_report(result)
    stamp = result["meta"]["created_at"].replace(":", "").replace("-", "").replace("+0000", "Z")
    name = f"{stamp}-{result['digest'][:8]}"
    _write_json(out / "history" / f"{name}.json", result, indent=None)
    _write_json(latest, result, indent=None)
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "history" / f"{name}.md").write_text(report, encoding="utf-8")
    _emit(f"wrote {out / 'report.md'} and {latest} (digest {result['digest'][:16]})")
    if result["diff_vs_previous"]:
        diff = result["diff_vs_previous"]
        changed = sum(1 for r in diff["rows"] if r["delta"] not in (None, 0, 0.0))
        _emit(
            f"vs previous: identical={diff['identical']} comparable={diff['comparable']} "
            f"changed_metrics={changed}"
        )
    if args.print_report:
        _emit(report)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    dataset = _dataset(args)
    families: dict[str, int] = {}
    for scenario in dataset.scenarios:
        families[scenario.family] = families.get(scenario.family, 0) + 1
    _emit(
        f"ok: {len(dataset.scenarios)} scenarios, {dataset.turn_count} turns, "
        f"labels={dataset.label_status}, digest={dataset.digest()}"
    )
    for family, count in families.items():
        _emit(f"  {family}: {count}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    previous = json.loads(Path(args.previous).read_text(encoding="utf-8"))
    current = json.loads(Path(args.current).read_text(encoding="utf-8"))
    current = dict(current)
    current["diff_vs_previous"] = diff_results(previous, current)
    _emit(render_report(current))
    return 0


def cmd_labeling_sheet(args: argparse.Namespace) -> int:
    dataset = _dataset(args)
    sheet, key = labeling_sheet(dataset, seed=args.seed)
    out = Path(args.out)
    _write_json(out / "labeling-sheet.json", sheet)
    _write_json(out / "labeling-key.json", key)
    _emit(f"wrote {out / 'labeling-sheet.json'} (give to labelers) and labeling-key.json (keep)")
    return 0


def cmd_audit_pairs(args: argparse.Namespace) -> int:
    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    a, b = parse_conditions(args.a)[0], parse_conditions(args.b)[0]
    dataset = _dataset(args)
    text = {t.id: t.user for s in dataset.scenarios for t in s.turns}
    sheet, key = make_blind_pairs(result, a, b, seed=args.seed, user_text=text)
    out = Path(args.out)
    _write_json(out / "audit-pairs.json", sheet)
    _write_json(out / "audit-key.json", key)
    _emit(f"wrote {len(sheet['pairs'])} pairs to {out / 'audit-pairs.json'} (key kept separately)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m stiracore.eval",
        description="Emotion-module ablation harness (#143). Reports measurements only.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run conditions x seeds over the fixed scenarios")
    _add_dataset_args(run)
    run.add_argument("--runner", choices=("scripted", "nxp"), default="scripted")
    run.add_argument(
        "--conditions",
        default=",".join(CONDITIONS),
        help="comma list of off/neutral/real/shuffled (aliases: none, on, dummy, shuffle)",
    )
    run.add_argument("--seeds", type=_seeds, default=(0, 1))
    run.add_argument("--classifier", choices=CLASSIFIERS, default="oracle")
    run.add_argument("--scenarios", help="comma list of scenario ids")
    run.add_argument("--families", help="comma list of families")
    run.add_argument("--out", default="eval_out")
    run.add_argument("--previous", help="result JSON to diff against (default: OUT/latest.json)")
    run.add_argument("--bootstrap", type=int, default=2000)
    run.add_argument("--bootstrap-seed", type=int, default=0)
    run.add_argument("--concede-p", type=float, default=DEFAULT_CONCEDE_P)
    run.add_argument("--critical-action", choices=("readonly", "halt"), default="readonly")
    run.add_argument("--print-report", action="store_true")
    llm = run.add_argument_group("nxp runner (calls a real model; all required)")
    llm.add_argument("--allow-llm", action="store_true")
    llm.add_argument("--model", help="provider:model_id (see docs/providers.md)")
    llm.add_argument("--max-turns", type=int)
    llm.add_argument("--max-cost-usd", type=float)
    llm.add_argument("--price-in", type=float, help="USD per 1M input tokens")
    llm.add_argument("--price-out", type=float, help="USD per 1M output tokens")
    llm.add_argument("--max-output-tokens", type=int, default=1024)
    run.set_defaults(func=cmd_run)

    validate = sub.add_parser("validate", help="validate scenario (and label) files")
    _add_dataset_args(validate)
    validate.set_defaults(func=cmd_validate)

    compare = sub.add_parser("compare", help="report CURRENT with its diff against PREVIOUS")
    compare.add_argument("previous")
    compare.add_argument("current")
    compare.set_defaults(func=cmd_compare)

    sheet = sub.add_parser("labeling-sheet", help="export a blind labeling sheet + key")
    _add_dataset_args(sheet)
    sheet.add_argument("--out", required=True)
    sheet.add_argument("--seed", type=int, default=0)
    sheet.set_defaults(func=cmd_labeling_sheet)

    audit = sub.add_parser("audit-pairs", help="export condition-blind response pairs + key")
    _add_dataset_args(audit)
    audit.add_argument("result")
    audit.add_argument("--a", required=True)
    audit.add_argument("--b", required=True)
    audit.add_argument("--out", required=True)
    audit.add_argument("--seed", type=int, default=0)
    audit.set_defaults(func=cmd_audit_pairs)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The engine logs on every WARNING/CRITICAL turn; keep the console readable.
    logging.disable(logging.WARNING)
    try:
        return args.func(args)
    except (ScenarioError, ValueError) as exc:
        _err(f"error: {exc}")
        return EXIT_USAGE
    finally:
        logging.disable(logging.NOTSET)


if __name__ == "__main__":
    raise SystemExit(main())

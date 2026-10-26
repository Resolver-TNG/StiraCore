"""Emotion ablation harness (#143): schema, determinism, conditions, diff, wording.

These tests check the harness itself. Nothing here evaluates whether the
emotion module has an effect.
"""

from __future__ import annotations

import json
from collections import Counter
from importlib import resources

import pytest

from stiracore.core.engine import BreakAction, BreakLevel, BreakResult, StiraConfig
from stiracore.eval import __main__ as cli
from stiracore.eval.audit import make_blind_pairs
from stiracore.eval.conditions import (
    CONDITIONS,
    emotion_block,
    neutral_block,
    parse_conditions,
    state_block,
)
from stiracore.eval.dataset import (
    FAMILIES,
    LABELS_SCHEMA,
    ScenarioError,
    apply_labels,
    labeling_sheet,
    load_dataset,
    parse_scenario_file,
    translate_sheet_labels,
)
from stiracore.eval.metrics import diff_results, run_counts
from stiracore.eval.report import (
    MEASUREMENT_ONLY,
    EffectClaimError,
    build_result,
    ensure_measurement_only,
    render_report,
)
from stiracore.eval.runners import (
    STUB_TOOL_NAMES,
    Budget,
    BudgetError,
    config_summary,
    run_nxp,
    run_scripted,
    write_blocked_names,
)
from stiracore.providers.usage import Pricing


@pytest.fixture(scope="module")
def dataset():
    return load_dataset()


def _result(dataset, runs, timing, seeds=(0, 1), conditions=CONDITIONS, bootstrap=50):
    return build_result(
        runs=runs,
        timing=timing,
        dataset=dataset,
        runner="scripted",
        classifier="oracle",
        conditions=tuple(conditions),
        seeds=tuple(seeds),
        config=config_summary(StiraConfig()),
        standin={"concede_p": 0.3},
        bootstrap=bootstrap,
        bootstrap_seed=0,
    )


# ----------------------------------------------------------------- scenario data


def test_bundled_dataset_has_30_plus_scenarios_in_every_family(dataset):
    assert len(dataset.scenarios) >= 30
    counts = Counter(s.family for s in dataset.scenarios)
    assert set(counts) == set(FAMILIES)
    assert all(count >= 5 for count in counts.values()), counts
    assert dataset.label_status == "draft-unblinded"
    assert all(t.expected.event for s in dataset.scenarios for t in s.turns)


def test_scenarios_are_package_data():
    root = resources.files("stiracore.eval").joinpath("scenarios")
    names = sorted(item.name for item in root.iterdir() if item.name.endswith(".json"))
    assert len(names) == len(FAMILIES)


def test_live_probe_scenarios_are_carried_over_verbatim(dataset):
    live_probe = pytest.importorskip("scripts.live_probe")
    by_origin = {s.origin: s for s in dataset.scenarios}
    for name, turns in live_probe.SCENARIOS.items():
        scenario = by_origin[f"live_probe:{name}"]
        assert [t.user for t in scenario.turns] == turns


def _minimal_file(**turn_overrides):
    turn = {"id": "X01-t01", "user": "こんにちは", "expected": {"event": "neutral"}}
    turn.update(turn_overrides)
    turns = [turn] + [
        {"id": f"X01-t{i:02d}", "user": f"発話{i}", "expected": {"event": "request"}}
        for i in (2, 3)
    ]
    return {
        "schema": "stira-eval-scenarios/1",
        "family": "neutral_control",
        "label_status": "draft-unblinded",
        "scenarios": [{"id": "X01", "title": "t", "origin": "new", "turns": turns}],
    }


def test_minimal_scenario_file_parses_with_defaults():
    [scenario] = parse_scenario_file(_minimal_file(), "test")
    first = scenario.turns[0]
    assert first.expected.stimulus is None
    assert first.expected.pressure is False and first.expected.stop_warranted is False
    assert scenario.gap_seconds == 60.0 and scenario.repeat == 1


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"expected": {"event": "anger"}}, "unknown event"),
        ({"expected": {"event": "threat", "stimulus": "threat"}}, "together"),
        ({"expected": {"event": "threat", "stimulus": "rage", "intensity": 0.5}}, "stimulus"),
        ({"expected": {"event": "threat", "stimulus": "threat", "intensity": 0.7}}, "intensity"),
        ({"expected": {"event": "neutral", "pressure": "yes"}}, "boolean"),
        ({"expected": {"event": "neutral", "mood": 1}}, "unknown keys"),
        ({"attempts": ["rm_rf"]}, "attempts"),
        ({"id": "X01-t09"}, "turn id"),
        ({"user": " "}, "user"),
    ],
)
def test_schema_rejects_malformed_turns(override, message):
    with pytest.raises(ScenarioError, match=message):
        parse_scenario_file(_minimal_file(**override), "test")


def test_schema_rejects_bad_scenario_fields():
    data = _minimal_file()
    data["scenarios"][0]["task"] = {"required_tools": ["file_write"]}
    with pytest.raises(ScenarioError, match="attempted"):
        parse_scenario_file(data, "test")
    data = _minimal_file()
    data["scenarios"][0]["initial_state"] = {"stability": 1.5}
    with pytest.raises(ScenarioError, match="initial_state"):
        parse_scenario_file(data, "test")
    data = _minimal_file()
    data["schema"] = "other/1"
    with pytest.raises(ScenarioError, match="schema"):
        parse_scenario_file(data, "test")


def test_duplicate_scenarios_across_files_are_rejected(tmp_path):
    for name in ("a.json", "b.json"):
        (tmp_path / name).write_text(json.dumps(_minimal_file()), encoding="utf-8")
    with pytest.raises(ScenarioError, match="duplicate"):
        load_dataset([tmp_path / "a.json", tmp_path / "b.json"])


def test_blind_label_overlay_must_be_complete_and_replaces_labels(dataset):
    small = dataset.select(ids=["NC03"])
    labels = {t.id: {"event": "neutral"} for t in small.scenarios[0].turns}
    partial = dict(list(labels.items())[:2])
    with pytest.raises(ScenarioError, match="unlabeled"):
        apply_labels(small, {"schema": LABELS_SCHEMA, "labeler": "L1", "labels": partial})
    relabeled = apply_labels(small, {"schema": LABELS_SCHEMA, "labeler": "L1", "labels": labels})
    assert relabeled.label_status == "blind"
    assert {t.expected.event for t in relabeled.scenarios[0].turns} == {"neutral"}
    assert relabeled.digest() != small.digest()


def test_labeling_sheet_hides_labels_families_and_ids(dataset):
    sheet, key = labeling_sheet(dataset, seed=3)
    text = json.dumps(sheet["dialogues"], ensure_ascii=False)
    for hidden in ("expected", "event", "family", "title", "RF01", "real", "shuffled"):
        assert hidden not in text
    assert sorted(key.values()) == sorted(s.id for s in dataset.scenarios)
    first = sheet["dialogues"][0]
    submitted = {
        "schema": LABELS_SCHEMA,
        "labeler": "L1",
        "labels": {t["id"]: {"event": "neutral"} for t in first["turns"]},
    }
    translated = translate_sheet_labels(submitted, key)
    assert all(tid.startswith(key[first["dialogue"]]) for tid in translated["labels"])


# --------------------------------------------------------------------- conditions


def test_condition_aliases():
    assert parse_conditions("on,none,dummy,shuffle") == ("real", "off", "neutral", "shuffled")
    with pytest.raises(ValueError, match="unknown condition"):
        parse_conditions("placebo")


def test_neutral_block_has_the_same_length_as_the_state_block():
    from stiracore.core.neural_state import NeuralState

    for state in (NeuralState(), NeuralState(stability=0.05, vigilance=1.0)):
        real = state_block(state)
        assert len(emotion_block("neutral", state)) == len(real)
        assert "stability" not in emotion_block("neutral", state)
        assert emotion_block("off", state) == ""
    assert neutral_block(0) == ""


@pytest.fixture(scope="module")
def scripted(dataset):
    runs, timing = run_scripted(dataset, CONDITIONS, (0, 1), classifier="oracle")
    return runs, timing


def _runs(runs, condition, seed=0):
    return {r["scenario"]: r for r in runs if r["condition"] == condition and r["seed"] == seed}


def test_classifier_off_applies_nothing_and_never_calls_the_classifier(scripted):
    runs, _ = scripted
    for run in _runs(runs, "off").values():
        assert run["classifier_calls"] == 0
        assert all(row["stimulus"] is None for row in run["rows"])
        assert all(row["emotion_block_chars"] == 0 for row in run["rows"])


def test_neutral_dummy_spends_classifier_compute_but_applies_nothing(scripted):
    runs, _ = scripted
    real = _runs(runs, "real")
    for sid, run in _runs(runs, "neutral").items():
        assert run["classifier_calls"] == len(run["rows"]) == real[sid]["classifier_calls"]
        assert all(row["stimulus"] is None for row in run["rows"])
        lengths = [row["emotion_block_chars"] for row in run["rows"]]
        assert lengths == [row["emotion_block_chars"] for row in real[sid]["rows"]]


def test_real_applies_the_oracle_labels(scripted, dataset):
    runs, _ = scripted
    expected = {t.id: t.expected for s in dataset.scenarios for t in s.turns}
    for run in _runs(runs, "real").values():
        for row in run["rows"]:
            assert row["stimulus"] == expected[row["turn_id"]].stimulus


def test_shuffled_keeps_the_label_distribution_but_not_the_order(scripted):
    runs, _ = scripted
    real = _runs(runs, "real")
    moved = 0
    for sid, run in _runs(runs, "shuffled").items():
        applied = [row["stimulus"] for row in run["rows"]]
        reference = [row["stimulus"] for row in real[sid]["rows"]]
        assert Counter(applied) == Counter(reference)
        moved += applied != reference
    assert moved > 0


def test_tool_attempts_are_identical_across_conditions(scripted):
    runs, _ = scripted
    off = _runs(runs, "off")
    for condition in CONDITIONS:
        for sid, run in _runs(runs, condition).items():
            tools = [[a["tool"] for a in row["attempts"]] for row in run["rows"]]
            assert tools == [[a["tool"] for a in row["attempts"]] for row in off[sid]["rows"]]


def test_scripted_stance_is_a_paired_null_model(scripted):
    runs, _ = scripted
    off = _runs(runs, "off")
    for condition in CONDITIONS:
        for sid, run in _runs(runs, condition).items():
            assert [r["stance"] for r in run["rows"]] == [r["stance"] for r in off[sid]["rows"]]


def test_pre_stressed_state_restricts_writes_even_with_the_classifier_off(scripted):
    runs, _ = scripted
    th06 = _runs(runs, "off")["TH06"]
    assert th06["rows"][0]["pre_level"] == "critical"
    writes = [a for row in th06["rows"][:3] for a in row["attempts"] if a["write"]]
    assert writes and not any(a["executed"] for a in writes)


# ------------------------------------------------------------------ determinism


def test_same_seed_gives_identical_results(dataset, scripted):
    runs, timing = scripted
    again, _ = run_scripted(dataset, CONDITIONS, (0, 1), classifier="oracle")
    assert again == runs
    first = _result(dataset, runs, timing)
    second = _result(dataset, again, {"different": "timing"})
    assert first["digest"] == second["digest"]
    assert first["deterministic"] == second["deterministic"]


def test_different_seed_changes_the_seeded_parts_only(dataset, scripted):
    runs, _ = scripted
    other, _ = run_scripted(dataset, CONDITIONS, (5, 6), classifier="oracle")
    off_a = [r["rows"] for r in runs if r["condition"] == "off" and r["seed"] == 0]
    off_b = [r["rows"] for r in other if r["condition"] == "off" and r["seed"] == 5]
    # Seeds only drive the shuffle and the stand-in stance; the state path of
    # the off condition (no stimuli) is seed-independent.
    for rows_a, rows_b in zip(off_a, off_b, strict=True):
        assert [r["state"] for r in rows_a] == [r["state"] for r in rows_b]
    stances_a = [r["stance"] for rows in off_a for r in rows]
    stances_b = [r["stance"] for rows in off_b for r in rows]
    assert stances_a != stances_b
    shuffled_a = [r["rows"] for r in runs if r["condition"] == "shuffled" and r["seed"] == 0]
    shuffled_b = [r["rows"] for r in other if r["condition"] == "shuffled" and r["seed"] == 5]
    assert shuffled_a != shuffled_b


# ---------------------------------------------------------------------- metrics


def test_run_counts_follow_the_labels(dataset, scripted):
    runs, _ = scripted
    no07 = _runs(runs, "off")["NO07"]  # initial stability 0.45 -> WARNING, legit writes
    counts = run_counts(no07, dataset)
    num, den = counts["false_stop_rate"]
    assert den == 3 and num == 3
    assert counts["task_success_rate"] == (0, 1)
    assert counts["classifier_calls_per_turn"] == (0, len(no07["rows"]))


def test_bootstrap_interval_contains_the_point_and_is_reproducible(dataset, scripted):
    runs, timing = scripted
    result = _result(dataset, runs, timing)
    again = _result(dataset, runs, timing)
    assert result["deterministic"]["comparisons"] == again["deterministic"]["comparisons"]
    for row in result["deterministic"]["comparisons"]:
        if row["interval"] is not None:
            low, high = row["interval"]
            assert low - 1e-9 <= row["diff"] <= high + 1e-9
            assert row["clusters"] == len(dataset.scenarios)


def test_diff_against_previous(dataset, scripted):
    runs, timing = scripted
    current = _result(dataset, runs, timing)
    same = diff_results(current, current)
    assert same["identical"] and same["comparable"]
    assert all(row["delta"] in (0, 0.0, None) for row in same["rows"])

    previous = json.loads(json.dumps(current))
    cell = previous["deterministic"]["metrics"]["real"]["tool_blocks"]
    cell["value"] = cell["value"] + 0.25
    previous["digest"] = "other"
    previous["deterministic"]["seeds"] = [7]
    diff = diff_results(previous, current)
    assert not diff["identical"] and not diff["comparable"]
    assert any("seeds" in w for w in diff["warnings"])
    [row] = [r for r in diff["rows"] if r["condition"] == "real" and r["metric"] == "tool_blocks"]
    assert row["delta"] == pytest.approx(-0.25)


def test_write_block_policy_matches_nxp_agent():
    from stiracore.agent.nxp_agent import NXPAgent
    from stiracore.core.breaker_sim import _DiscardCheckpointStore
    from stiracore.eval.runners import stub_tools
    from tests.provider_fakes import FakeModel

    for critical_action in ("readonly", "halt"):
        config = StiraConfig(critical_action=critical_action)
        agent = NXPAgent(
            model=FakeModel(),
            tools=stub_tools(),
            config=config,
            checkpoint_store=_DiscardCheckpointStore(),
        )
        for level in (BreakLevel.NORMAL, BreakLevel.WARNING, BreakLevel.CRITICAL):
            breaker = BreakResult(level=level, actions=agent.engine._actions_for(level))
            agent.engine._current_break_result = breaker
            visible, _ = agent._prepare_turn(breaker)
            visible_names = {t.tool_name for t in visible}
            blocked = write_blocked_names(config, agent.engine, breaker)
            unexecutable = set(STUB_TOOL_NAMES) & blocked
            if breaker.actions & BreakAction.HALT:
                # Visible, but every write call is rejected by the HITL gate.
                assert visible_names == set(STUB_TOOL_NAMES)
                assert unexecutable == set(STUB_TOOL_NAMES) - {"file_read"}
            else:
                assert unexecutable == set(STUB_TOOL_NAMES) - visible_names
            agent.engine._current_break_result = None


# ---------------------------------------------------------------- report wording


def test_report_is_measurement_only(dataset, scripted):
    runs, timing = scripted
    report = render_report(_result(dataset, runs, timing))
    assert report.count(MEASUREMENT_ONLY) == 1
    assert "ハーネス自体の動作検証であり、効果評価ではない" in report
    assert "効果あり" not in report
    ensure_measurement_only(report)


@pytest.mark.parametrize(
    "claim",
    [
        "感情モジュールは効果あり",
        "追従が改善した",
        "有意な差",
        "This significantly improves safety",
    ],
)
def test_guard_rejects_effect_claims(claim):
    with pytest.raises(EffectClaimError):
        ensure_measurement_only(f"{MEASUREMENT_ONLY}\n{claim}")


def test_guard_requires_the_premise():
    with pytest.raises(EffectClaimError, match="premise"):
        ensure_measurement_only("測定値の表")


def test_rendering_refuses_a_result_with_injected_claims(dataset, scripted):
    runs, timing = scripted
    result = _result(dataset, runs, timing)
    result["notice"] = [*result["notice"], "real 条件は効果がある"]
    with pytest.raises(EffectClaimError):
        render_report(result)


def test_blind_pairs_hide_condition_names(dataset, scripted):
    runs, timing = scripted
    result = _result(dataset, runs, timing)
    sheet, key = make_blind_pairs(result, "real", "neutral", seed=1)
    text = json.dumps(sheet, ensure_ascii=False)
    assert "real" not in text and "neutral" not in text
    assert len(sheet["pairs"]) == len(key["key"]) > 0
    sides = Counter(entry["A"] for entry in key["key"].values())
    assert set(sides) == {"real", "neutral"}


# --------------------------------------------------------------------------- CLI


def test_cli_run_writes_results_and_diffs_against_previous(tmp_path, capsys):
    out = tmp_path / "out"
    argv = ["run", "--out", str(out), "--families", "threat,neutral_control", "--bootstrap", "40"]
    assert cli.main(argv) == 0
    first = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert first["measurement_only"] is True and first["diff_vs_previous"] is None
    assert first["deterministic"]["seeds"] == [0, 1]
    ensure_measurement_only((out / "report.md").read_text(encoding="utf-8"))

    assert cli.main(argv) == 0
    second = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert second["diff_vs_previous"]["identical"] is True
    assert "前回比" in (out / "report.md").read_text(encoding="utf-8")
    assert len(list((out / "history").glob("*.json"))) >= 1

    assert cli.main([*argv, "--conditions", "off,on", "--seeds", "3,4"]) == 0
    third = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert third["deterministic"]["conditions"] == ["off", "real"]
    assert third["diff_vs_previous"]["comparable"] is False
    assert "vs previous" in capsys.readouterr().out


def test_cli_validate_and_sheets(tmp_path, capsys):
    assert cli.main(["validate"]) == 0
    assert f"{len(load_dataset().scenarios)} scenarios" in capsys.readouterr().out
    assert cli.main(["labeling-sheet", "--out", str(tmp_path / "lab")]) == 0
    assert (tmp_path / "lab" / "labeling-key.json").exists()
    out = tmp_path / "res"
    assert (
        cli.main(["run", "--out", str(out), "--scenarios", "SP01,SP02", "--bootstrap", "20"]) == 0
    )
    assert (
        cli.main(
            [
                "audit-pairs",
                str(out / "latest.json"),
                "--a",
                "real",
                "--b",
                "neutral",
                "--out",
                str(tmp_path / "audit"),
            ]
        )
        == 0
    )
    pairs = json.loads((tmp_path / "audit" / "audit-pairs.json").read_text(encoding="utf-8"))
    assert pairs["pairs"] and all(p["user"] for p in pairs["pairs"])


def test_cli_nxp_is_disabled_without_explicit_flags(tmp_path, monkeypatch, capsys):
    from stiracore import providers

    def forbidden(*args, **kwargs):
        raise AssertionError("no model may be created")

    monkeypatch.setattr(providers, "create_model", forbidden)
    base = ["run", "--runner", "nxp", "--out", str(tmp_path)]
    assert cli.main(base) == 2
    assert "--allow-llm" in capsys.readouterr().err
    assert cli.main([*base, "--allow-llm", "--model", "bedrock:x"]) == 2
    err = capsys.readouterr().err
    assert "--max-turns" in err and "--max-cost-usd" in err and "--price-in" in err
    assert not (tmp_path / "latest.json").exists()


# ------------------------------------------------------------- nxp runner (offline)


def _fake_factory(usage=None):
    from tests.provider_fakes import FakeModel, text_turn

    return lambda: FakeModel([text_turn("了解しました。", usage=usage) for _ in range(40)])


def test_nxp_runner_with_an_offline_model(dataset):
    small = dataset.select(ids=["TH06", "NC03"])
    runs, timing = run_nxp(
        small,
        ("off", "real"),
        (0,),
        model_factory=_fake_factory({"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}),
        budget=Budget(max_turns=100, max_cost_usd=1.0),
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=1.0),
        classifier="oracle",
    )
    assert {r["status"] for r in runs} == {"complete"}
    assert timing["stop_reason"] is None and timing["spent_usd"] > 0
    th06 = next(r for r in runs if r["scenario"] == "TH06" and r["condition"] == "off")
    assert set(th06["rows"][0]["exposure_blocked"]) == set(STUB_TOOL_NAMES) - {"file_read"}
    assert all(row["stance"] is None for run in runs for row in run["rows"])
    real = next(r for r in runs if r["scenario"] == "TH06" and r["condition"] == "real")
    assert real["rows"][0]["stimulus"] == "threat"
    assert all(row["stimulus"] is None for row in th06["rows"])
    result = build_result(
        runs=runs,
        timing=timing,
        dataset=small,
        runner="nxp",
        classifier="oracle",
        conditions=("off", "real"),
        seeds=(0,),
        config=config_summary(StiraConfig()),
        standin={"model": "fake"},
        bootstrap=20,
        bootstrap_seed=0,
    )
    report = render_report(result)
    assert "runner=nxp" in report
    assert result["deterministic"]["metrics"]["real"]["sycophancy_rate"]["value"] is None


def test_nxp_runner_refuses_plans_over_the_turn_cap(dataset):
    small = dataset.select(ids=["NC03"])
    with pytest.raises(BudgetError, match="max_turns"):
        run_nxp(
            small,
            ("off", "real"),
            (0, 1),
            model_factory=_fake_factory(),
            budget=Budget(max_turns=3, max_cost_usd=1.0),
            pricing=Pricing(1.0, 1.0),
        )
    with pytest.raises(BudgetError, match="pricing"):
        run_nxp(
            small,
            ("off",),
            (0,),
            model_factory=_fake_factory(),
            budget=Budget(max_turns=100, max_cost_usd=1.0),
            pricing=None,
        )
    with pytest.raises(ValueError):
        Budget(max_turns=0, max_cost_usd=1.0)


def test_nxp_runner_stops_at_the_cost_cap_and_on_unknown_cost(dataset):
    small = dataset.select(ids=["NC03"])
    usage = {"inputTokens": 1_000_000, "outputTokens": 0, "totalTokens": 1_000_000}
    runs, timing = run_nxp(
        small,
        ("off", "real"),
        (0,),
        model_factory=_fake_factory(usage),
        budget=Budget(max_turns=100, max_cost_usd=1.5),
        pricing=Pricing(input_per_mtok=1.0, output_per_mtok=1.0),
    )
    assert timing["stop_reason"] == "cost_cap"
    assert sum(len(r["rows"]) for r in runs) == 2
    assert runs[-1]["status"] == "stopped:cost_cap"

    runs, timing = run_nxp(
        small,
        ("off",),
        (0,),
        model_factory=_fake_factory(),
        budget=Budget(max_turns=100, max_cost_usd=1.0),
        pricing=Pricing(input_per_mtok=None, output_per_mtok=1.0),
    )
    assert timing["stop_reason"] == "cost_unknown"
    assert len(runs[0]["rows"]) == 1


def test_sokudan_runner_when_available(dataset):
    pytest.importorskip("sokudan")
    small = dataset.select(ids=["NC01"])
    runs, _ = run_scripted(small, ("off", "real"), (0,), classifier="sokudan")
    assert all(row["stimulus"] is None for row in runs[0]["rows"])


def test_harness_invariants_hold_and_detect_tampering(dataset, scripted):
    from stiracore.eval.metrics import invariants

    runs, timing = scripted
    result = _result(dataset, runs, timing)
    checks = result["deterministic"]["invariants"]
    assert checks["ok"] is True
    assert checks["neutral_levels_equal_off"]["checked"] == len(dataset.scenarios) * 2
    assert "ハーネス不変条件: ok" in render_report(result)

    tampered = json.loads(json.dumps(runs))
    victim = next(r for r in tampered if r["condition"] == "neutral" and r["scenario"] == "TH01")
    victim["rows"][0]["post_level"] = "critical"
    broken = invariants(tampered, "scripted")
    assert broken["ok"] is False
    assert broken["neutral_levels_equal_off"]["violations"] == ["TH01"]


def test_incomplete_groups_are_excluded_from_every_condition(dataset, scripted):
    runs, timing = scripted
    edited = json.loads(json.dumps(runs))
    victim = next(r for r in edited if r["condition"] == "real" and r["scenario"] == "NC03")
    victim["status"] = "stopped:cost_cap"
    result = _result(dataset, edited, timing)
    det = result["deterministic"]
    assert det["excluded_incomplete"] == [{"scenario": "NC03", "seed": victim["seed"]}]
    full = _result(dataset, runs, timing)["deterministic"]
    turns_nc03 = len(victim["rows"])
    for condition in CONDITIONS:
        den = det["metrics"][condition]["breaker_transition_rate"]["den"]
        assert den == full["metrics"][condition]["breaker_transition_rate"]["den"] - turns_nc03


def test_results_are_split_by_initial_setup(dataset, scripted):
    runs, timing = scripted
    det = _result(dataset, runs, timing)["deterministic"]
    assert set(det["by_setup"]) == {"baseline", "pre_stressed"}
    stressed = {s.id for s in dataset.scenarios if s.initial_state}
    assert {r["scenario"] for r in runs if r["setup"] == "pre_stressed"} == stressed

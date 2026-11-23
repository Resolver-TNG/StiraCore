"""ci/linux/gate_verdict.py: a gate run is PASS only when it has ended and every part is green.

The first case is the real log that cost a red main on 2026-11-21: the 3.11 block was green, the 3.12 block
(where ruff runs) failed a few minutes later. Reading the first PASS was the mistake.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ci" / "linux" / "gate_verdict.py"
_spec = importlib.util.spec_from_file_location("gate_verdict", SCRIPT)
gate_verdict = importlib.util.module_from_spec(_spec)
sys.modules["gate_verdict"] = gate_verdict
_spec.loader.exec_module(gate_verdict)

SHA = "00e27782cddacb94e3abb6bb1a78338296ed047f"
HEAD = f"=== gate origin/x {SHA} (py both) 18:51\nref origin/x = 00e27782 feat: something\n"


def block(
    version: str,
    *,
    shard_rc: int = 0,
    misc_rc: int = 0,
    counted: int = 11933,
    plain: int = 11933,
    passed: bool = True,
    step_failed: str | None = None,
) -> str:
    out = ["shards: [104, 104, 105, 105] files", f"=== results (py{version}, 4 shards, 540s)"]
    for i in range(4):
        out.append(
            f"shard{i} rc={shard_rc if i == 2 else 0}  3218 passed, 42 skipped in 293.16s (0:04:53)"
        )
    out.append(f"misc rc={misc_rc}")
    out.append(
        f"coverage guard: shards accounted for {counted} tests, plain collection sees {plain}"
    )
    if step_failed:
        out.append(f"!!! step failed: {step_failed}")
    out.append(
        "LINUX CI: PASS"
        if passed and not step_failed
        else "LINUX CI: FAIL (logs: /tmp/stira-ci-run.x)"
    )
    return "\n".join(out) + "\n"


DONE0 = "=== done rc=0 19:09\n"


def test_two_green_blocks_and_done_is_pass():
    r = gate_verdict.verdict(HEAD + block("3.11") + block("3.12") + DONE0, versions=2, sha=SHA)
    assert r["verdict"] == "PASS" and r["reasons"] == []
    assert r["versions_seen"] == ["3.11", "3.12"]


def test_the_2026_11_21_case_green_311_while_312_still_runs_is_incomplete_not_pass():
    r = gate_verdict.verdict(HEAD + block("3.11"), versions=2, sha=SHA)
    assert r["verdict"] == "INCOMPLETE"
    assert any("has not ended" in x for x in r["reasons"])


def test_the_same_run_finished_is_fail_with_the_ruff_steps_named():
    text = (
        HEAD
        + block("3.11")
        + block("3.12", misc_rc=1, step_failed="ruff check .")
        + "=== done rc=1 19:09\n"
    )
    r = gate_verdict.verdict(text, versions=2, sha=SHA)
    assert r["verdict"] == "FAIL"
    assert any("ruff check" in x for x in r["reasons"]) and any(
        "misc rc" in x for x in r["reasons"]
    )


def test_a_failure_is_reported_even_before_the_run_has_ended():
    r = gate_verdict.verdict(
        HEAD + block("3.11", step_failed="ruff format --check ."), versions=2, sha=SHA
    )
    assert r["verdict"] == "FAIL"


def test_a_failing_shard_is_fail():
    r = gate_verdict.verdict(
        HEAD + block("3.11", shard_rc=1, passed=False) + block("3.12") + DONE0, sha=SHA
    )
    assert r["verdict"] == "FAIL" and any("shard failed" in x for x in r["reasons"])


def test_the_coverage_guard_must_agree():
    r = gate_verdict.verdict(HEAD + block("3.11", counted=11900) + block("3.12") + DONE0, sha=SHA)
    assert r["verdict"] == "FAIL" and any("coverage guard mismatch" in x for x in r["reasons"])


def test_the_tested_commit_must_be_the_expected_one():
    r = gate_verdict.verdict(
        HEAD + block("3.11") + block("3.12") + DONE0, sha="ffffffffffffffffffffffffffffffffffffffff"
    )
    assert r["verdict"] == "FAIL" and any("not the expected" in x for x in r["reasons"])
    r2 = gate_verdict.verdict(block("3.11") + block("3.12") + DONE0, sha=SHA)
    assert r2["verdict"] == "FAIL" and any("cannot tell which commit" in x for x in r2["reasons"])


def test_a_missing_block_after_the_end_is_fail_and_a_single_version_run_can_pass():
    r = gate_verdict.verdict(HEAD + block("3.11") + DONE0, versions=2, sha=SHA)
    assert r["verdict"] == "FAIL" and any("result block" in x for x in r["reasons"])
    assert (
        gate_verdict.verdict(HEAD + block("3.12") + DONE0, versions=1, sha=SHA)["verdict"] == "PASS"
    )


def test_wrapper_nonzero_exit_is_fail_even_if_the_blocks_look_green():
    r = gate_verdict.verdict(
        HEAD + block("3.11") + block("3.12") + "=== done rc=1 19:09\n", sha=SHA
    )
    assert r["verdict"] == "FAIL"


def test_cli_exit_codes(tmp_path):
    ok, bad, wait = (tmp_path / n for n in ("ok.log", "bad.log", "wait.log"))
    ok.write_text(HEAD + block("3.11") + block("3.12") + DONE0)
    bad.write_text(
        HEAD + block("3.11") + block("3.12", step_failed="ruff check .") + "=== done rc=1 x\n"
    )
    wait.write_text(HEAD + block("3.11"))
    run = lambda p: (
        subprocess.run(
            [sys.executable, str(SCRIPT), str(p), "--sha", SHA],
            check=False,
            capture_output=True,
            text=True,
        ).returncode
    )
    assert (run(ok), run(bad), run(wait)) == (0, 1, 2)

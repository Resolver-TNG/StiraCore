"""#463: the shipped procedural seed is only a proposal, and says only what the harness does."""

from __future__ import annotations

import json
import re
from pathlib import Path

from stiracore.control import procedural_cli
from stiracore.control.procedural_proposals import ProcedureProposals
from stiracore.tools import tool_registry
from stiracore.tools.file_editor_tool import EDITOR_DESCRIPTION
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

SEEDS = Path(__file__).resolve().parents[1] / "examples" / "procedural-seeds"
SEED = SEEDS / "safe_artifact_editing.md"
# Features a seed must not lean on before they exist (#458 trust labels / taint, #460
# isolation stages): the held seed's subject.
NOT_YET = ("trust label", "taint", "tool broker", "isolation mode", "quarantine", "sandbox")


def test_only_the_offered_seed_is_shipped():
    assert sorted(p.name for p in SEEDS.iterdir()) == [
        "README.md",
        "safe_artifact_editing.md",
        "verify_before_done.md",
    ]
    assert "untrusted_ai_containment" in (SEEDS / "README.md").read_text("utf-8")  # held, why


def test_the_seed_names_only_tools_and_commands_that_exist():
    text = SEED.read_text("utf-8")
    lowered = text.lower()
    for phrase in NOT_YET:
        assert phrase not in lowered, phrase
    named = set(re.findall(r"`([a-z_]+)`", text))
    tools = {"file_read", "file_write", "editor"}
    commands = {"view", "create", "str_replace", "insert"}
    assert named <= tools | commands, named - tools - commands
    registered = set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)
    assert tools <= registered
    for command in commands:
        assert command in EDITOR_DESCRIPTION, command
    assert "never overwrites" in EDITOR_DESCRIPTION  # what step 2 relies on


def test_loading_the_seed_through_the_cli_only_stages_it(tmp_path, capsys):
    store = MemoryStore(tmp_path / "memory", "stira")
    code = procedural_cli.main(
        [
            "--base-dir",
            str(store.base_dir),
            "--agent-id",
            "stira",
            "propose",
            "safe_artifact_editing",
            "--source-turn",
            "seed:safe_artifact_editing",
            "--file",
            str(SEED),
        ]
    )
    assert code == 0, capsys.readouterr().err
    assert json.loads(capsys.readouterr().out) == {"proposed": "staging/safe_artifact_editing"}
    assert store.read(MemoryLayer.PROCEDURAL, "safe_artifact_editing") is None
    view = ProcedureProposals(store, review_log=tmp_path / "r.jsonl").overview()
    proposal = view["proposals"][0]
    assert proposal["key"] == "safe_artifact_editing" and proposal["approvable"] is True
    assert proposal["proposed_via"] == "operator"


VERIFY = SEEDS / "verify_before_done.md"


def test_the_verify_seed_names_only_tools_that_exist():
    """ADR 0019 §7: the seed names run_checks, lsp_diagnostics and page_check — tools the
    harness has (run_checks and page_check are registered only when turned on / a browser
    is found; the seed says what to do when run_checks is not offered)."""
    from stiracore.tools import lsp_tools, page_check, run_checks

    text = VERIFY.read_text("utf-8")
    lowered = text.lower()
    for phrase in NOT_YET:
        assert phrase not in lowered, phrase
    named = set(re.findall(r"`([a-z_]+)`", text))
    assert named == {"run_checks", "lsp_diagnostics", "page_check"}, named
    assert run_checks.run_checks.tool_name == "run_checks"
    assert page_check.page_check.tool_name == "page_check"
    assert lsp_tools.lsp_diagnostics.tool_name == "lsp_diagnostics"
    assert "is not offered, say that the checks were not run" in text


def test_the_verify_seed_is_only_staged(tmp_path, capsys):
    store = MemoryStore(tmp_path / "memory", "stira")
    code = procedural_cli.main(
        [
            "--base-dir",
            str(store.base_dir),
            "--agent-id",
            "stira",
            "propose",
            "verify_before_done",
            "--source-turn",
            "seed:verify_before_done",
            "--file",
            str(VERIFY),
        ]
    )
    assert code == 0, capsys.readouterr().err
    assert store.read(MemoryLayer.PROCEDURAL, "verify_before_done") is None

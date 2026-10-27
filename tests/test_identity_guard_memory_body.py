"""``memory_write`` / ``journal`` bodies are prose, not write targets (#464, same as #304).

#305 stopped IdentityGuard from reading the body of ``file_write`` / ``editor`` as a
write-target candidate. ``memory_write`` and ``journal`` were still scanned in full, so a
saved summary with a bare ``/`` (``A / B``, a fraction, a list) was refused as
"SOUL/identity files are read-only" and the agent stripped the punctuation to get it
saved. The arguments that decide *where* those tools write stay checked: ``layer`` /
``key`` (and the memory store's own key validation), ``date`` and ``task``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stiracore.core import NeuralState, StiraConfig, StiraEngine
from stiracore.identity_paths import READ_ONLY_ERROR, WORKSPACE_ROOT, WritePathPolicy
from stiracore.middleware.identity_guard import IdentityGuard, _scanned_strings
from stiracore.tools.memory_store import MemoryLayer, MemoryStore
from stiracore.tools.memory_tools import create_memory_tools


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "stira"
    (data / "workspace").mkdir(parents=True)
    for name in ("tool-config.json", "neural-state.json", "hitl-approvals.db"):
        (data / name).write_text("{}")
    return data


@pytest.fixture()
def guard(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> IdentityGuard:
    # The home holds the data directory, as in a real install: "~" names a protected path then.
    # Without this the cases "~" depend on where the runner's temp directory is (under the
    # home on a workstation, /tmp or /var/folders on a CI runner).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    read_only = [str(data_dir / n) for n in ("tool-config.json", "neural-state.json")]
    policy = WritePathPolicy(
        read_only + ["SOUL.md"], base_dir=data_dir / "workspace", write_roots=[WORKSPACE_ROOT]
    )
    engine = StiraEngine(state=NeuralState(stability=0.9), config=StiraConfig())
    return IdentityGuard(engine, path_policy=policy)


# What the refused saves of #464 held, plus the boundary cases of the brief.
BODIES = [
    "A / B",
    "1 / 2 = 0.5",
    "a/b",
    "写真 / 図面",
    "x ~ y",
    "see . here",
    "go .. up",
    "../ と ./ の違い",
    "path /Users here",
    "/etc/passwd は読まない",
    "https://example.com/a/b?q=1",
    "hockey_game_yolo.html を作った",
    "see SOUL.md for details",
    "全角の斜線 ／ と 分数の斜線 ∕ と ⁄ と ＼",
    "- 箇条書き / Markdown\n- [x] 済み / 未\n",
]


def _assert_allowed(result) -> None:
    assert result.allowed, result.reason


@pytest.mark.parametrize("body", BODIES)
def test_memory_write_body_is_not_a_write_target(guard: IdentityGuard, body: str) -> None:
    args = {"layer": "episodic", "key": "summary-2026-10-19", "content": body}
    _assert_allowed(guard.check_tool_call("memory_write", args))


@pytest.mark.parametrize("body", BODIES)
def test_journal_body_is_not_a_write_target(guard: IdentityGuard, body: str) -> None:
    _assert_allowed(guard.check_tool_call("journal", {"action": "write", "content": body}))
    args = {"action": "write", "date": "2026-10-19", "content": body}
    _assert_allowed(guard.check_tool_call("journal", args))


def test_absolute_protected_path_in_memory_body_is_only_text(
    guard: IdentityGuard, data_dir: Path
) -> None:
    """A body naming a floor file is text in an episodic memory, not a write to that file."""
    args = {"layer": "episodic", "key": "k", "content": str(data_dir / "tool-config.json")}
    _assert_allowed(guard.check_tool_call("memory_write", args))


# The arguments that choose the target are still read as targets.


@pytest.mark.parametrize("key", ["SOUL.md", "/", "~", "..", "."])
def test_memory_write_key_naming_a_protected_path_is_refused(
    guard: IdentityGuard, key: str
) -> None:
    result = guard.check_tool_call(
        "memory_write", {"layer": "episodic", "key": key, "content": "x"}
    )
    assert not result.allowed
    assert result.reason.startswith(READ_ONLY_ERROR)
    assert 'argument: "key"' in result.reason


def test_memory_write_layer_is_still_scanned(guard: IdentityGuard, data_dir: Path) -> None:
    args = {"layer": str(data_dir / "neural-state.json"), "key": "k", "content": "x"}
    assert not guard.check_tool_call("memory_write", args).allowed


@pytest.mark.parametrize(
    "date", ["../SOUL", "../../tool-config", "/", "/etc/x", "2026/10/19", "today", "", 20261019]
)
def test_journal_date_must_be_a_plain_day(guard: IdentityGuard, date) -> None:
    """``strands_tools.journal`` joins ``date`` into ``<cwd>/journal/<date>.md`` as given."""
    for action in ("write", "read", "add_task"):
        args = {"action": action, "date": date, "content": "x", "task": "t"}
        result = guard.check_tool_call("journal", args)
        assert not result.allowed, (action, date)
        assert "YYYY-MM-DD" in result.reason


@pytest.mark.parametrize("date", [None, "2026-10-19"])
def test_journal_date_today_or_a_day_is_allowed(guard: IdentityGuard, date) -> None:
    args = {"action": "write", "content": "x"}
    if date is not None:
        args["date"] = date
    _assert_allowed(guard.check_tool_call("journal", args))
    _assert_allowed(guard.check_tool_call("journal", {"action": "list"}))


@pytest.mark.parametrize("task", ["SOUL.md", "/", "~"])
def test_journal_task_is_still_scanned(guard: IdentityGuard, task: str) -> None:
    result = guard.check_tool_call("journal", {"action": "add_task", "task": task})
    assert not result.allowed and 'argument: "task"' in result.reason


def test_nested_body_and_unknown_keys_are_still_scanned() -> None:
    """Only a plain string under the declared body key is skipped (fail closed)."""
    nested = {"layer": "episodic", "key": "k", "content": {"path": "SOUL.md"}}
    assert "SOUL.md" in list(_scanned_strings("memory_write", nested))
    extra = {"layer": "episodic", "key": "k", "content": "x", "file_text": "SOUL.md"}
    assert "SOUL.md" in list(_scanned_strings("memory_write", extra))
    assert "SOUL.md" in list(_scanned_strings("journal", {"text": "SOUL.md"}))
    # A body key of another tool is not a body key here.
    assert "SOUL.md" in list(_scanned_strings("journal", {"new_str": "SOUL.md"}))


def test_command_tools_still_scan_a_content_argument(guard: IdentityGuard) -> None:
    """No regression of #304/#305: a command tool's text names its targets anywhere."""
    assert not guard.check_tool_call("shell", {"command": "true", "content": "> SOUL.md"}).allowed
    assert not guard.check_tool_call("file_write", {"path": "SOUL.md", "content": "a / b"}).allowed


def test_undeclared_body_refusal_says_the_argument_was_read_as_a_target(
    guard: IdentityGuard,
) -> None:
    """Requirement 4: a refusal on a body-like argument tells why, so it is noticed."""
    result = guard.check_tool_call("write_note", {"title": "t", "content": "a / b"})
    assert not result.allowed
    assert 'argument: "content"' in result.reason
    assert "not declared as body text" in result.reason
    # A real target argument gets no such note.
    result = guard.check_tool_call("file_write", {"path": "SOUL.md", "content": "x"})
    assert "not declared as body text" not in result.reason


# The memory store keeps the target inside the memory directory (requirement 2).


@pytest.fixture()
def memory_write(tmp_path: Path):
    store = MemoryStore(tmp_path / "home", "stira")
    tools = {t.tool_name: t for t in create_memory_tools(store)}
    return store, tools["memory_write"]


@pytest.mark.parametrize("key", ["../x", "/etc/x", "a/../../x", "a//b", "a\\b", "x\0y", ""])
def test_memory_write_key_outside_or_malformed_is_refused(memory_write, key: str) -> None:
    store, tool = memory_write
    out = tool(layer="episodic", key=key, content="A / B")
    assert out.startswith("Memory write failed"), out
    assert not any(store.memory_dir.parent.rglob("x.md"))


@pytest.mark.parametrize("key", ["a/b", "a／b", "..／x", "a∕b"])
def test_memory_write_nested_or_unicode_slash_key_stays_inside(memory_write, key: str) -> None:
    """``a/b`` is a nested episodic key (allowed before #464 too); look-alike slashes are
    ordinary characters of one file name. Either way the file is under ``episodic/``."""
    store, tool = memory_write
    out = tool(layer="episodic", key=key, content="1 / 2 と A / B")
    assert out.startswith("Memory written"), out
    entry = store.read(MemoryLayer.EPISODIC, key)
    assert entry is not None and entry.content.strip() == "1 / 2 と A / B"
    episodic = (store.memory_dir / "episodic").resolve()
    assert entry.path.resolve().is_relative_to(episodic)


def test_memory_write_other_layers_are_still_refused(memory_write) -> None:
    _store, tool = memory_write
    for layer in ("semantic", "procedural"):
        out = tool(layer=layer, key="rule", content="A / B")
        assert out.startswith("Memory write denied"), out

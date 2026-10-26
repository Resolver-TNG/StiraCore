"""IdentityGuard must not read free-text tool arguments as write targets.

The guard scans every string argument of a write-capable tool for protected paths
(shell commands name their targets anywhere). With absolute read-only files under the data
directory (#190) the ancestors of those files are protected too, so a bare ``/``, ``~``,
``.``, ``..`` or ``/Users`` token in the *body* of a ``file_write`` made the whole call
fail with "SOUL/identity files are read-only", although ``path`` was a plain workspace file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stiracore.core import NeuralState, StiraConfig, StiraEngine
from stiracore.identity_paths import READ_ONLY_ERROR, WORKSPACE_ROOT, WritePathPolicy
from stiracore.middleware.identity_guard import IdentityGuard


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "stira"
    (data / "workspace").mkdir(parents=True)
    for name in ("tool-config.json", "neural-state.json", "hitl-approvals.db"):
        (data / name).write_text("{}")
    return data


@pytest.fixture()
def guard(data_dir: Path) -> IdentityGuard:
    read_only = [str(data_dir / n) for n in ("tool-config.json", "neural-state.json")]
    policy = WritePathPolicy(
        read_only + ["SOUL.md"], base_dir=data_dir / "workspace", write_roots=[WORKSPACE_ROOT]
    )
    engine = StiraEngine(state=NeuralState(stability=0.9), config=StiraConfig())
    return IdentityGuard(engine, path_policy=policy)


FREE_TEXT = [
    "agent: stira / fugu-cyber",
    "1 / 2 = 0.5",
    "写真 / 図面",
    "x ~ y",
    "see . here",
    "go .. up",
    "path /Users here",
    "a/b and /tmp/x",
]


@pytest.mark.parametrize("body", FREE_TEXT)
def test_file_write_body_with_path_like_tokens_is_allowed(
    guard: IdentityGuard, data_dir: Path, body: str
) -> None:
    args = {"path": str(data_dir / "workspace" / "note.txt"), "content": body}
    result = guard.check_tool_call("file_write", args)
    assert result.allowed, result.reason


@pytest.mark.parametrize("body", FREE_TEXT)
def test_editor_text_arguments_are_allowed(guard: IdentityGuard, data_dir: Path, body: str) -> None:
    path = str(data_dir / "workspace" / "note.txt")
    for key in ("file_text", "new_str", "old_str"):
        args = {"command": "create", "path": path, key: body}
        result = guard.check_tool_call("editor", args)
        assert result.allowed, (key, result.reason)


def test_mentioning_an_identity_file_in_the_body_is_not_a_write_to_it(
    guard: IdentityGuard, data_dir: Path
) -> None:
    args = {"path": str(data_dir / "workspace" / "note.txt"), "content": "see SOUL.md for details"}
    assert guard.check_tool_call("file_write", args).allowed


# The floor still holds for the arguments that do name a target.


@pytest.mark.parametrize("target", ["SOUL.md", "tool-config.json", "neural-state.json"])
def test_protected_path_argument_is_still_refused(
    guard: IdentityGuard, data_dir: Path, target: str
) -> None:
    path = target if target == "SOUL.md" else str(data_dir / target)
    result = guard.check_tool_call("file_write", {"path": path, "content": "x"})
    assert not result.allowed and result.reason.startswith(READ_ONLY_ERROR[:20])


def test_unknown_argument_names_are_still_scanned(guard: IdentityGuard, data_dir: Path) -> None:
    """Fail closed: only the known body keys are skipped, never an unfamiliar one."""
    result = guard.check_tool_call(
        "write_blob", {"key": str(data_dir / "tool-config.json"), "value": "x"}
    )
    assert not result.allowed
    result = guard.check_tool_call("write_blob", {"destination": "/", "value": "x"})
    assert not result.allowed


def test_command_tools_are_still_scanned_everywhere(guard: IdentityGuard, data_dir: Path) -> None:
    """A shell command names its targets in the text: no argument is skipped there."""
    cmd = f"echo hi > {data_dir / 'tool-config.json'}"
    assert not guard.check_tool_call("shell", {"command": cmd}).allowed
    assert not guard.check_tool_call("shell", {"command": "echo a > SOUL.md"}).allowed
    # A body key does not hide a target from a command-capable tool either.
    assert not guard.check_tool_call(
        "shell", {"command": "true", "content": f"> {data_dir / 'neural-state.json'}"}
    ).allowed


def test_path_argument_that_is_an_ancestor_is_still_refused(
    guard: IdentityGuard, data_dir: Path
) -> None:
    """``path`` is the write target: replacing a directory that holds an anchor stays denied."""
    result = guard.check_tool_call("file_write", {"path": str(data_dir), "content": "x"})
    assert not result.allowed


def test_body_keys_are_only_skipped_for_the_standard_file_tools_and_plain_strings():
    from stiracore.middleware.identity_guard import _scanned_strings

    # Another (non-file) tool: the same key is scanned in full.
    assert "SOUL.md" in list(_scanned_strings("some_other_tool", {"content": "SOUL.md"}))
    # A nested structure under a body key is still scanned (it could carry a target).
    assert "SOUL.md" in list(_scanned_strings("file_write", {"content": {"path": "SOUL.md"}}))
    # A plain string body of file_write is prose: skipped.
    assert "SOUL.md" not in list(_scanned_strings("file_write", {"content": "SOUL.md"}))

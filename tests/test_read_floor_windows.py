"""#315 (rest): the read floor's remaining windows.

The protected-identity walk (``WritePathPolicy.floor_identities``) walked by path string:
a directory swapped for a symlink between its ``lstat`` and its scan was followed, so the
walk left the data directory. It now walks by directory descriptor (``O_DIRECTORY |
O_NOFOLLOW`` relative to the parent's descriptor, ``fstat`` = the listed ``lstat``) and
refuses when a directory changed. The walk is bounded (entries, deadline) and fails closed
when a bound is hit. ``lsp_*`` hands the analyzers the path that was checked, not a second
resolution of the name given.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from stiracore import identity_paths
from stiracore.config.data_dir import build_policy, readable_names
from stiracore.config.models import AgentConfig
from stiracore.identity_paths import READ_FLOOR_ERROR, FloorWalkError
from stiracore.tools import lsp_tools, tool_registry

SECRET = "SECRET-315-WINDOW"
POSIX_FD_WALK = os.scandir in os.supports_fd and os.open in os.supports_dir_fd


def _touch(path: Path, text: str = SECRET) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "stira").resolve()
    for name in readable_names():
        (root / name).mkdir(parents=True)
    _touch(root / "neural-state.json")
    _touch(root / "checkpoints" / "c.json")
    _touch(root / "workspace" / "notes.md", "readable")
    elsewhere = tmp_path / "server-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return root


def _run(tool, arguments: dict) -> dict:
    use = {"toolUseId": "u1", "name": tool.tool_name, "input": arguments}

    async def collect():
        events = [event async for event in tool.stream(use, {"agent": None})]
        last = events[-1]
        return last["tool_result"] if isinstance(last, dict) else last.tool_result

    return asyncio.run(collect())


class _Listing:
    """``os.scandir``'s result, materialised (each entry's ``lstat`` cached)."""

    def __init__(self, entries: list) -> None:
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *exc) -> None:
        return None

    def __iter__(self):
        return iter(self._entries)


def _swap_after_listing(monkeypatch, tree: Path, name: str, replacement: Path) -> dict:
    """After the tree's own listing (its entries ``lstat``-ed), put a symlink in place of
    the directory ``name``: the window between an entry's ``lstat`` and its scan."""
    real = os.scandir
    seen = {"swapped": False}

    def scandir(target=".", *args, **kwargs):
        listing = real(target, *args, **kwargs)
        if seen["swapped"]:
            return listing
        with listing as entries:
            materialised = list(entries)
        for entry in materialised:
            entry.stat(follow_symlinks=False)
        if any(entry.name == name for entry in materialised):
            os.replace(tree / name, tree.parent / f"{name}-moved-away")
            (tree / name).symlink_to(replacement, target_is_directory=True)
            seen["swapped"] = True
        return _Listing(materialised)

    monkeypatch.setattr(os, "scandir", scandir)
    return seen


# --- (1) the identity walk never follows a directory swapped for a symlink ----------------


def test_the_identity_walk_does_not_follow_a_directory_swapped_for_a_symlink(
    data_dir, tmp_path, monkeypatch
):
    outside = tmp_path / "outside"
    planted = _touch(outside / "planted.txt", "outside")
    policy, _ = build_policy(AgentConfig(), data_dir)
    fd_dir = Path("/dev/fd")
    open_before = len(os.listdir(fd_dir)) if fd_dir.is_dir() else None
    seen = _swap_after_listing(monkeypatch, data_dir, "checkpoints", outside)
    try:
        files, directories = policy.floor_identities()
    except FloorWalkError:
        files, directories = None, None  # refused: fail closed
    monkeypatch.undo()
    assert seen["swapped"]
    if open_before is not None:
        assert len(os.listdir(fd_dir)) == open_before  # a refused walk closes its descriptors
    if files is not None:
        # Not refused: then the walk must not have left the tree.
        info = os.stat(planted)
        assert (info.st_dev, info.st_ino) not in files
        moved = os.stat(outside)
        assert (moved.st_dev, moved.st_ino) not in directories
    if POSIX_FD_WALK:
        # By descriptor the swap is seen (O_NOFOLLOW / fstat != the listed lstat).
        assert files is None


def test_a_directory_swapped_during_the_walk_refuses_the_read(data_dir, tmp_path, monkeypatch):
    if not POSIX_FD_WALK:
        pytest.skip("descriptor walk unavailable on this platform")
    outside = tmp_path / "outside"
    _touch(outside / "planted.txt", "outside")
    policy, _ = build_policy(AgentConfig(), data_dir)
    tools = {t.tool_name: t for t in tool_registry.get_tools(write_policy=policy)}
    _swap_after_listing(monkeypatch, data_dir, "checkpoints", outside)
    result = _run(tools["file_read"], {"path": "notes.md"})
    monkeypatch.undo()
    assert result["status"] == "error", result
    assert READ_FLOOR_ERROR in result["content"][0]["text"]
    assert str(outside) not in str(result)


def test_the_descriptor_walk_matches_the_protected_files(data_dir):
    policy, _ = build_policy(AgentConfig(), data_dir)
    files, directories = policy.floor_identities()
    for name in ("neural-state.json", "checkpoints/c.json"):
        info = os.stat(data_dir / name)
        assert (info.st_dev, info.st_ino) in files
    readable = os.stat(data_dir / "workspace" / "notes.md")
    assert (readable.st_dev, readable.st_ino) not in files
    checkpoints = os.stat(data_dir / "checkpoints")
    assert directories[(checkpoints.st_dev, checkpoints.st_ino)] == "checkpoints"
    root = os.stat(data_dir)
    assert directories[(root.st_dev, root.st_ino)] == ""


def test_the_walk_leaves_no_descriptor_open(data_dir):
    if not Path("/dev/fd").is_dir():
        pytest.skip("no /dev/fd")
    policy, _ = build_policy(AgentConfig(), data_dir)
    before = len(os.listdir("/dev/fd"))
    for _ in range(20):
        policy.floor_identities()
    assert len(os.listdir("/dev/fd")) == before


# --- (2) the walk is bounded and fails closed --------------------------------------------


def test_a_walk_over_the_entry_bound_refuses(data_dir, monkeypatch):
    for index in range(30):
        _touch(data_dir / "checkpoints" / f"bulk-{index}.json", "x")
    monkeypatch.setattr(identity_paths, "FLOOR_WALK_MAX_ENTRIES", 10)
    policy, _ = build_policy(AgentConfig(), data_dir)
    with pytest.raises(FloorWalkError) as caught:
        policy.floor_identities()
    assert str(data_dir) not in str(caught.value)
    tools = {t.tool_name: t for t in tool_registry.get_tools(write_policy=policy)}
    result = _run(tools["file_read"], {"path": "notes.md"})
    assert result["status"] == "error"
    assert READ_FLOOR_ERROR in result["content"][0]["text"]


def test_a_walk_past_the_deadline_refuses(data_dir, monkeypatch):
    clock = iter(float(i) * 10 for i in range(10_000))
    monkeypatch.setattr(identity_paths.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(identity_paths, "FLOOR_WALK_DEADLINE_SEC", 5.0)
    policy, _ = build_policy(AgentConfig(), data_dir)
    with pytest.raises(FloorWalkError):
        policy.floor_identities()


def test_load_a_few_thousand_protected_files(data_dir):
    # A measured load (not a timing assertion): every file is found, within the bounds.
    count = 3000
    for index in range(count):
        _touch(data_dir / "checkpoints" / f"d{index % 30}" / f"c{index}.json", "x")
    policy, _ = build_policy(AgentConfig(), data_dir)
    started = time.perf_counter()
    files, _ = policy.floor_identities()
    elapsed = time.perf_counter() - started
    assert len(files) >= count + 2
    assert elapsed < identity_paths.FLOOR_WALK_DEADLINE_SEC


# --- (5) lsp_*: the analyzers get the checked path, not the name resolved again ----------


def test_lsp_gives_the_analyzer_the_checked_path(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    target = _touch(workspace / "sub" / "a.py", "x = 1\n")
    real_open = lsp_tools.open_for_read
    captured: dict = {}

    def swap_then_open(path):
        # Between the tool's own resolution and the checked open: the directory moves,
        # its old name becomes a symlink to it.
        os.replace(workspace / "sub", workspace / "sub2")
        (workspace / "sub").symlink_to(workspace / "sub2", target_is_directory=True)
        return real_open(path)

    def fake_ruff(args, data, *, timeout):
        captured["args"] = list(args)
        captured["data"] = data
        return 0, b"[]"

    monkeypatch.setattr(lsp_tools, "open_for_read", swap_then_open)
    monkeypatch.setattr(
        lsp_tools, "_executable", lambda name: None if "pyright" in name else "/bin/ruff"
    )
    monkeypatch.setattr(lsp_tools, "_run_bounded", fake_ruff)
    result = lsp_tools.lsp_diagnostics(str(target))
    checked = str((workspace / "sub2" / "a.py").resolve())
    assert captured["data"] == b"x = 1\n"
    stdin_name = captured["args"][captured["args"].index("--stdin-filename") + 1]
    assert stdin_name == checked, captured["args"]
    assert result["file"] == checked


# --- review round 1: the deadline after the last entry, a swap for a real directory --------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _open_descriptors() -> int | None:
    fd_dir = Path("/dev/fd")
    return len(os.listdir(fd_dir)) if fd_dir.is_dir() else None


def _advance_after_last_spend(monkeypatch, policy, clock: _Clock) -> dict:
    """Pass the deadline right after the walk's last counted entry (a dry run counts)."""
    real_spend = identity_paths._WalkBudget.spend
    counted = {"n": 0}

    def counting(self):
        counted["n"] += 1
        return real_spend(self)

    monkeypatch.setattr(identity_paths._WalkBudget, "spend", counting)
    policy.floor_identities()
    total, seen = counted["n"], {"n": 0, "scans_after": 0}

    def advancing(self):
        real_spend(self)
        seen["n"] += 1
        if seen["n"] == total:
            clock.now += identity_paths.FLOOR_WALK_DEADLINE_SEC + 1
            seen["advanced"] = True

    real_scandir = os.scandir

    def scandir(target=".", *args, **kwargs):
        if seen.get("advanced"):
            seen["scans_after"] += 1
        return real_scandir(target, *args, **kwargs)

    monkeypatch.setattr(identity_paths._WalkBudget, "spend", advancing)
    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(identity_paths.time, "monotonic", clock)
    return seen


def test_a_deadline_passed_while_walking_empty_directories_refuses(data_dir, monkeypatch):
    # The last counted entries are listed in time; the deadline passes while the empty
    # directories listed among them are scanned, where no entry is ever counted.
    for index in range(5):
        (data_dir / f"zz-empty-{index}").mkdir()
    policy, _ = build_policy(AgentConfig(), data_dir)
    _advance_after_last_spend(monkeypatch, policy, _Clock())
    before = _open_descriptors()
    with pytest.raises(FloorWalkError) as caught:
        policy.floor_identities()
    assert "deadline" in str(caught.value) and str(data_dir) not in str(caught.value)
    # Before the fix the walk scanned the five empty directories past the deadline
    # (measured: 5 scans after it) and returned normally.
    assert _open_descriptors() == before


def test_a_deadline_passed_after_the_last_entry_refuses(data_dir, monkeypatch):
    # Past the deadline after the last entry was handled: checked again before returning.
    policy, _ = build_policy(AgentConfig(), data_dir)
    _advance_after_last_spend(monkeypatch, policy, _Clock())
    with pytest.raises(FloorWalkError):
        policy.floor_identities()


def test_a_directory_swapped_for_another_real_directory_refuses(data_dir, tmp_path, monkeypatch):
    if not POSIX_FD_WALK:
        pytest.skip("descriptor walk unavailable on this platform")
    # Not a symlink (O_NOFOLLOW would not see it): another real directory under the same
    # name. Only the fstat == listed lstat comparison refuses it.
    real = os.scandir
    seen = {"swapped": False}

    def scandir(target=".", *args, **kwargs):
        listing = real(target, *args, **kwargs)
        if seen["swapped"]:
            return listing
        with listing as entries:
            materialised = list(entries)
        for entry in materialised:
            entry.stat(follow_symlinks=False)
        if any(entry.name == "checkpoints" for entry in materialised):
            os.replace(data_dir / "checkpoints", tmp_path / "checkpoints-moved-away")
            _touch(data_dir / "checkpoints" / "decoy.json", "decoy")
            seen["swapped"] = True
        return _Listing(materialised)

    monkeypatch.setattr(os, "scandir", scandir)
    policy, _ = build_policy(AgentConfig(), data_dir)
    before = _open_descriptors()
    with pytest.raises(FloorWalkError) as caught:
        policy.floor_identities()
    monkeypatch.undo()
    assert seen["swapped"]
    assert "changed" in str(caught.value) and str(data_dir) not in str(caught.value)
    assert _open_descriptors() == before

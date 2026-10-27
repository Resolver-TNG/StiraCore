"""#244: write-failure classification, OS-protection denylist, permission cards, doctor.

Measured on the real filesystem where that is possible without side effects (a
``uchg`` temporary file/folder, a mode-0500 folder, SIP locations that only get
``lstat``-ed). macOS privacy (TCC) locations are never touched: they live under
a fake ``HOME`` and a spy proves that no system call reaches them.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.diagnostics import probes
from stiracore.diagnostics.write_errors import (
    MARKER,
    WriteFailureBoard,
    WriteFailureHook,
    classify_write_failure,
    recheck,
)
from stiracore.identity_paths import (
    OUTSIDE_WRITE_ROOTS_ERROR,
    READ_ONLY_ERROR,
    WORKSPACE_ROOT,
    WritePathPolicy,
)
from stiracore.middleware.identity_guard import IdentityGuard
from stiracore.tools import tool_registry
from stiracore.tools.compact_specs import COMPACT_DESCRIPTIONS
from stiracore.tools.os_protection import (
    COMMAND_TOOL_NAMES,
    OS_PROTECTION_ERROR,
    OsProtectionGuardTool,
    os_protection_denial,
    os_protection_problem,
)
from stiracore.tools.tool_consent import mark_gate_passed
from tests.provider_fakes import FakeModel, text_turn, tool_turn

DARWIN = sys.platform == "darwin"
ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0
# macOS-shaped POSIX paths ("/Volumes", "/usr", cwd "/") are not absolute on Windows.
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX path shapes")


def _fake_home(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows expanduser


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "SOUL.md").write_text("soul", encoding="utf-8")
    return workspace


def _policy(ws: Path) -> WritePathPolicy:
    return WritePathPolicy(base_dir=ws, write_roots=[WORKSPACE_ROOT])


def _tools(ws: Path) -> dict:
    tools = tool_registry.get_tools(write_policy=_policy(ws), include_exec=False)
    return {tool.tool_name: tool for tool in tools}


def _call(tool, tool_input: dict, use_id: str = "t1", *, gate: bool = True) -> dict:
    use = {"toolUseId": use_id, "name": tool.tool_name, "input": tool_input}
    state: dict = {"agent": None}

    async def collect():
        return [event async for event in tool.stream(use, state)]

    if gate:
        mark_gate_passed(state, use)
    event = asyncio.run(collect())[-1]
    return getattr(event, "tool_result", None) or event["tool_result"]


def _hooked(tool_name: str, tool_input: dict, result: dict, *, base=None, sink=None):
    """Run WriteFailureHook on one result exactly as the SDK hands it over."""
    hook = WriteFailureHook(sink, base_provider=(lambda: base) if base else None)
    event = SimpleNamespace(
        tool_use={"toolUseId": "t1", "name": tool_name, "input": tool_input}, result=result
    )
    hook.after_tool(event)
    return event.result


def _unlock(*paths: Path) -> None:
    for path in paths:
        if path.exists():
            os.chflags(path, 0)


def _text(result: dict) -> str:
    return "\n".join(block.get("text", "") for block in result["content"])


# --- A. Classification -------------------------------------------------------------


@pytest.mark.skipif(not DARWIN, reason="uchg is a BSD/macOS file flag")
@pytest.mark.parametrize("tool_name", ["file_write", "editor"])
def test_uchg_file_is_locked_flag_for_both_tools(ws, tool_name):
    target = ws / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    tool_input = (
        {"path": str(target), "content": "x"}
        if tool_name == "file_write"
        else {"command": "str_replace", "path": str(target), "old_str": "one", "new_str": "1"}
    )
    cards = []
    try:
        os.chflags(target, stat.UF_IMMUTABLE)
        result = _call(_tools(ws)[tool_name], tool_input)
        assert result["status"] == "error" and "Operation not permitted" in _text(result)
        annotated = _hooked(tool_name, tool_input, result, sink=lambda f, **k: cards.append(f))
    finally:
        _unlock(target)
    assert f"{MARKER} locked_flag]" in _text(annotated)
    assert "do not use sudo" in _text(annotated)
    (failure,) = cards
    assert (failure.category, failure.remedy, failure.errno) == (
        "locked_flag",
        "locked_flag",
        "EPERM",
    )
    assert "flag:uchg(file)" in failure.evidence
    assert failure.retryable


@pytest.mark.skipif(not DARWIN, reason="uchg is a BSD/macOS file flag")
@pytest.mark.parametrize("tool_name", ["file_write", "editor"])
def test_uchg_folder_blocks_create_and_is_locked_flag(ws, tool_name):
    folder = ws / "locked"
    folder.mkdir()
    target = folder / "new.txt"
    tool_input = (
        {"path": str(target), "content": "x"}
        if tool_name == "file_write"
        else {"command": "create", "path": str(target), "file_text": "x"}
    )
    try:
        os.chflags(folder, stat.UF_IMMUTABLE)
        result = _call(_tools(ws)[tool_name], tool_input)
        failure = classify_write_failure(tool_name, tool_input, _text(result))
    finally:
        _unlock(folder)
    assert failure.category == "locked_flag"
    assert failure.evidence == ("flag:uchg(folder)",)


def _spy_fs(monkeypatch, *forbidden) -> list:
    """Record every probing system call that reaches ``forbidden`` (it must stay empty)."""
    hits: list = []
    prefix = tuple(str(item) for item in forbidden)

    def wrap(name):
        original = getattr(os, name)

        def spy(path, *args, **kwargs):
            text = os.fspath(path) if isinstance(path, (str, bytes, os.PathLike)) else ""
            if isinstance(text, bytes):
                text = os.fsdecode(text)
            if text.startswith(prefix):
                hits.append((name, text))
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, name, spy)

    for name in ("stat", "lstat", "open", "access", "listdir", "scandir", "readlink", "statvfs"):
        if hasattr(os, name):
            wrap(name)
    for name in ("xattr_names", "acl_text"):
        original = getattr(probes, name)

        def guarded(path, *args, _original=original, _name=name, **kwargs):
            if os.fspath(path).startswith(prefix):
                hits.append((_name, os.fspath(path)))
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(probes, name, guarded)
    return hits


@posix_only
@pytest.mark.parametrize(
    ("relative", "zone"),
    [
        ("Documents/notes.txt", "~/Documents"),
        ("Desktop/x.txt", "~/Desktop"),
        ("Downloads/x.txt", "~/Downloads"),
        ("Library/Mobile Documents/com~apple~CloudDocs/x.txt", "~/Library/Mobile Documents"),
        ("Library/Mail/V10/x", "~/Library/Mail"),  # Full Disk Access territory
        ("DOCUMENTS/x.txt", "~/Documents"),  # APFS is case-insensitive
    ],
)
def test_tcc_denial_is_protected_by_os_and_never_probed(tmp_path, monkeypatch, relative, zone):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    _fake_home(monkeypatch, home)
    target = home / relative
    hits = _spy_fs(monkeypatch, home / relative.split("/", 1)[0])
    text = f"Error writing file: [Errno 1] Operation not permitted: '{target}'"
    failure = classify_write_failure(
        "file_write", {"path": str(target), "content": "x"}, text, platform="darwin"
    )
    assert hits == []
    assert (failure.category, failure.remedy) == ("protected_by_os", "tcc")
    assert f"tcc:{zone}" in failure.evidence
    assert failure.deep_link.startswith("x-apple.systempreferences:")
    assert str(home) not in json.dumps(failure.as_dict())  # masked: ~ instead of the home path


@posix_only
def test_volumes_are_tcc_zones_but_volumes_itself_is_not():
    assert probes.tcc_zone("/Volumes/USB/x", platform="darwin") == "/Volumes/*"
    assert probes.tcc_zone("/Volumes", platform="darwin") is None
    assert probes.tcc_zone("/Volumes/USB/x", platform="linux") is None


@pytest.mark.skipif(not DARWIN, reason="SIP is macOS")
@pytest.mark.parametrize("target", ["/System/Library/stira-x.txt", "/usr/bin/stira-x"])
def test_sip_location_is_protected_by_os(target):
    """Measured: /System and /usr/bin refuse the write and their folder carries
    SF_RESTRICTED, so the diagnosis is SIP. Which error the kernel gives depends on the
    machine: EPERM from the restricted flag locally, EROFS on a CI macOS runner, where the
    signed system volume is mounted read-only (#662). The flag is checked before the mount,
    so the diagnosis is the same for both."""
    with pytest.raises(OSError) as raised:
        open(target, "w")  # noqa: SIM115 - the OS refuses; nothing is created
    assert raised.value.errno in (errno.EPERM, errno.EROFS)
    text = f"Error writing file: [Errno {raised.value.errno}] {raised.value.strerror}: '{target}'"
    failure = classify_write_failure("file_write", {"path": target, "content": "x"}, text)
    assert (failure.category, failure.remedy) == ("protected_by_os", "sip")
    assert failure.evidence == ("flag:restricted(folder)",)
    assert not failure.retryable


@posix_only
def test_sip_by_path_when_flags_are_unavailable(monkeypatch):
    monkeypatch.setattr(probes, "file_flags", lambda *a, **k: frozenset())
    text = "[Errno 1] Operation not permitted: '/usr/libexec/stira-x'"
    failure = classify_write_failure(
        "file_write", {"path": "/usr/libexec/stira-x"}, text, platform="darwin"
    )
    assert (failure.category, failure.evidence) == ("protected_by_os", ("sip:path",))
    assert not probes.sip_path("/usr/local/bin/x", platform="darwin")


def test_readonly_volume_from_statfs(tmp_path, monkeypatch):
    target = tmp_path / "ro" / "x.txt"
    target.parent.mkdir()
    monkeypatch.setattr(probes, "readonly_mount", lambda path, **k: True)
    text = f"[Errno 1] Operation not permitted: '{target}'"
    failure = classify_write_failure("file_write", {"path": str(target)}, text, platform="linux")
    assert (failure.category, failure.evidence) == ("readonly_volume", ("statfs:rdonly",))


def test_erofs_is_readonly_volume(tmp_path):
    target = tmp_path / "x.txt"
    text = f"[Errno 30] Read-only file system: '{target}'"
    failure = classify_write_failure("file_write", {"path": str(target)}, text)
    assert failure.category == "readonly_volume"


@pytest.mark.skipif(not DARWIN, reason="hdiutil is macOS")
def test_readonly_volume_measured_with_a_readonly_disk_image(tmp_path):
    import shutil
    import subprocess

    hdiutil = shutil.which("hdiutil")
    if hdiutil is None:
        pytest.skip("hdiutil missing")
    image, mount = tmp_path / "ro.dmg", tmp_path / "mnt"
    mount.mkdir()
    made = subprocess.run(
        [hdiutil, "create", "-size", "1m", "-fs", "HFS+", "-volname", "strro", str(image)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    attached = made.returncode == 0 and (
        subprocess.run(
            [hdiutil, "attach", "-readonly", "-nobrowse", "-mountpoint", str(mount), str(image)],
            capture_output=True,
            timeout=60,
            check=False,
        ).returncode
        == 0
    )
    if not attached:
        pytest.skip("cannot attach a disk image here")
    try:
        target = mount / "x.txt"
        with pytest.raises(OSError) as raised:
            target.write_text("x", encoding="utf-8")
        text = f"[Errno {raised.value.errno}] {raised.value.strerror}: '{target}'"
        failure = classify_write_failure("file_write", {"path": str(target)}, text)
        assert failure.category == "readonly_volume"
    finally:
        subprocess.run(
            [hdiutil, "detach", str(mount), "-force"], capture_output=True, timeout=60, check=False
        )


@posix_only
def test_relative_path_resolved_against_root_cwd_is_wrong_cwd():
    text = "Error writing file: [Errno 30] Read-only file system: 'notes.txt'"
    failure = classify_write_failure("file_write", {"path": "notes.txt"}, text, cwd="/")
    assert (failure.category, failure.remedy) == ("wrong_cwd", "wrong_cwd")


def test_editor_empty_dirname_is_wrong_cwd():
    """The upstream editor's relative create: makedirs('') → ENOENT ''."""
    text = "Error: FileNotFoundError - [Errno 2] No such file or directory: ''"
    failure = classify_write_failure(
        "editor", {"command": "create", "path": "x.txt", "file_text": "a"}, text, cwd="/tmp"
    )
    assert failure.category == "wrong_cwd"


@pytest.mark.skipif(not DARWIN, reason="uchg is a BSD/macOS file flag")
def test_relative_path_anchored_at_the_workspace_is_not_wrong_cwd(ws):
    target = ws / "a.txt"
    target.write_text("x", encoding="utf-8")
    try:
        os.chflags(target, stat.UF_IMMUTABLE)
        text = f"[Errno 1] Operation not permitted: '{target}'"
        failure = classify_write_failure(
            "file_write", {"path": "a.txt"}, text, cwd="/", base=str(ws)
        )
    finally:
        _unlock(target)
    assert failure.category == "locked_flag"


def test_outside_write_roots_from_the_wrapper(ws, tmp_path):
    outside = tmp_path / "elsewhere.txt"
    tool_input = {"path": str(outside), "content": "x"}
    result = _call(_tools(ws)["file_write"], tool_input)
    assert _text(result).startswith(OUTSIDE_WRITE_ROOTS_ERROR)
    annotated = _hooked("file_write", tool_input, result)
    assert f"{MARKER} outside_write_roots]" in _text(annotated)
    assert not outside.exists()


@pytest.mark.parametrize("suffix", ["", " (not sent for approval: it cannot lift this)"])
def test_identity_floor_is_identity_protected(ws, suffix):
    failure = classify_write_failure(
        "editor", {"command": "create", "path": str(ws / "SOUL.md")}, READ_ONLY_ERROR + suffix
    )
    assert (failure.category, failure.retryable) == ("identity_protected", False)


def test_real_floor_refusal_is_identity_protected(ws):
    tool_input = {"path": str(ws / "SOUL.md"), "content": "x"}
    result = _call(_tools(ws)["file_write"], tool_input)
    failure = classify_write_failure("file_write", tool_input, _text(result))
    assert failure.category == "identity_protected"


def test_gate_refusal_is_consent_blocked(ws):
    tool_input = {"path": str(ws / "n.txt"), "content": "x"}
    result = _call(_tools(ws)["file_write"], tool_input, gate=False)
    failure = classify_write_failure("file_write", tool_input, _text(result))
    assert (failure.category, failure.remedy) == ("consent_blocked", "consent_blocked")
    assert failure.card_worthy


def test_human_rejection_is_classified_but_gets_no_card():
    cards = []
    result = {"status": "error", "content": [{"text": "HITL rejected this write operation."}]}
    _hooked("file_write", {"path": "/x"}, result, sink=lambda f, **k: cards.append(f))
    assert cards == []


@pytest.mark.skipif(ROOT_USER or os.name == "nt", reason="needs POSIX modes as non-root")
@pytest.mark.parametrize("tool_name", ["file_write", "editor"])
def test_mode_denied_folder_is_permission_denied(ws, tool_name):
    folder = ws / "ro"
    folder.mkdir()
    target = folder / "x.txt"
    tool_input = (
        {"path": str(target), "content": "x"}
        if tool_name == "file_write"
        else {"command": "create", "path": str(target), "file_text": "x"}
    )
    try:
        folder.chmod(0o500)
        result = _call(_tools(ws)[tool_name], tool_input)
        failure = classify_write_failure(tool_name, tool_input, _text(result))
    finally:
        folder.chmod(0o700)
    assert (failure.category, failure.errno) == ("permission_denied", "EACCES")
    assert failure.evidence == ("mode:not_writable(folder)",)


def test_editor_content_errors_are_not_write_failures(ws):
    target = ws / "a.txt"
    target.write_text("one", encoding="utf-8")
    tool_input = {"command": "str_replace", "path": str(target), "old_str": "zzz", "new_str": "1"}
    result = _call(_tools(ws)["editor"], tool_input)
    assert result["status"] == "error"
    assert _hooked("editor", tool_input, result) == result  # untouched: no note, no card


def test_note_is_added_once_and_success_is_untouched():
    result = {"status": "error", "content": [{"text": READ_ONLY_ERROR}]}
    once = _hooked("file_write", {"path": "/x/SOUL.md"}, result)
    assert _hooked("file_write", {"path": "/x/SOUL.md"}, once) == once
    ok = {"status": "success", "content": [{"text": "written"}]}
    assert _hooked("file_write", {"path": "/x"}, ok) is ok


def test_card_and_message_are_masked(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    _fake_home(monkeypatch, home)
    target = home / "proj" / "x.txt"
    text = f"[Errno 13] Permission denied: '{target}' api_key=sk-ABCdef1234567890XYZabc"
    failure = classify_write_failure("file_write", {"path": str(target)}, text)
    data = json.dumps(failure.as_dict())
    assert str(home) not in data and "sk-ABCdef1234567890XYZabc" not in data
    assert failure.path == "~" + os.sep + os.path.join("proj", "x.txt")
    assert "target" not in failure.as_dict()


def test_responsible_process_estimates():
    terminal = probes.responsible_process(
        {"TERM_PROGRAM": "iTerm.app"}, "/usr/bin/python3", platform="darwin"
    )
    assert (terminal["kind"], terminal["name"]) == ("terminal", "iTerm2")
    launchd = probes.responsible_process(
        {"XPC_SERVICE_NAME": "com.example.stira"}, "/opt/homebrew/bin/python3", platform="darwin"
    )
    # The executable is reported resolved, and what /opt/homebrew/bin/python3 points at
    # differs per machine (a CI runner: .../Cellar/python@3.15/.../bin/python3.15, #662).
    assert launchd["kind"] == "launchd"
    assert os.path.basename(launchd["executable"]).startswith("python3")
    assert probes.responsible_process({}, "/usr/bin/python3", platform="linux")["kind"] == "user"


@pytest.mark.skipif(not DARWIN, reason="uchg is a BSD/macOS file flag")
def test_hook_runs_inside_a_real_agent_turn_for_both_tools(ws):
    """End to end (offline model): the model sees the diagnosis; the board gets a card."""
    locked = ws / "locked.txt"
    locked.write_text("one\n", encoding="utf-8")
    board = WriteFailureBoard()
    policy = _policy(ws)
    model = FakeModel(
        [
            tool_turn("file_write", {"path": str(locked), "content": "x"}, "u1"),
            tool_turn(
                "editor",
                {"command": "str_replace", "path": str(locked), "old_str": "one", "new_str": "1"},
                "u2",
            ),
            text_turn("done"),
        ]
    )
    agent = NXPAgent(
        model=model,
        tools=tool_registry.get_tools(write_policy=policy, include_exec=False),
        write_policy=policy,
        stimulus_classifier=Mock(classify=lambda **k: None),
        extra_hooks=[WriteFailureHook(board.add, base_provider=lambda: ws)],
    )
    try:
        os.chflags(locked, stat.UF_IMMUTABLE)
        agent("write it")
    finally:
        _unlock(locked)
    results = [
        block["toolResult"]
        for message in model.requests[-1]["messages"]
        for block in message["content"]
        if "toolResult" in block
    ]
    assert [r["status"] for r in results] == ["error", "error"]
    assert all(f"{MARKER} locked_flag]" in _text(r) for r in results)
    assert [(c["tool"], c["category"]) for c in reversed(board.cards())] == [
        ("file_write", "locked_flag"),
        ("editor", "locked_flag"),
    ]


# --- B. OS-protection commands never run through a tool ------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "xattr -d com.apple.quarantine ~/x",
        "chflags nouchg ~/projects/a.txt",
        "sudo rm -rf /tmp/x",
        "tccutil reset All",
        "csrutil disable",
        "/usr/bin/SUDO ls",  # APFS resolves SUDO to sudo
        "s'u'do ls",
        "c\\hflags nouchg x",
        "ｓｕｄｏ ls",  # full-width
        "python3 -c 'import os; os.chflags(\"x\", 0)'",
        "sqlite3 ~/Library/Application\\ Support/com.apple.TCC/TCC.db 'delete from access'",
        "echo ok && xattr -c f",
    ],
)
def test_plain_os_protection_command_names_are_refused(command):
    assert os_protection_problem({"command": command}).startswith(OS_PROTECTION_ERROR)
    assert os_protection_problem({"command": command.split()}) is not None  # list form


@pytest.mark.parametrize("command", ["ls -la", "echo pseudo-code", "git status", "cat notes.md"])
def test_ordinary_commands_pass(command):
    assert os_protection_problem({"command": command}) is None


class _Recording:
    """A command tool stand-in that records whether it ran (it must not)."""

    def __init__(self, name: str = "shell") -> None:
        self.calls: list = []
        self.tool_name = name
        self.tool_spec = {"name": name, "description": "x", "inputSchema": {"json": {}}}
        self.tool_type = "python"

    async def stream(self, tool_use, invocation_state, **kwargs):
        self.calls.append(tool_use["input"])
        yield {
            "tool_result": {
                "toolUseId": tool_use["toolUseId"],
                "status": "success",
                "content": [{"text": "ran"}],
            }
        }


def _guarded_call(inner, tool_input):
    tool = OsProtectionGuardTool(inner)
    use = {"toolUseId": "t1", "name": inner.tool_name, "input": tool_input}

    async def collect():
        return [event async for event in tool.stream(use, {})]

    return asyncio.run(collect())


@pytest.mark.parametrize(
    ("name", "tool_input"),
    [
        ("shell", {"command": "sudo chflags nouchg x"}),
        ("shell", {"command": ["xattr", "-d", "com.apple.quarantine", "f"]}),
        (
            "batch",
            {"invocations": [{"name": "shell", "arguments": {"command": "csrutil disable"}}]},
        ),
        ("use_agent", {"prompt": "run tccutil reset All", "tools": ["shell"]}),
    ],
)
def test_execution_boundary_refuses_before_the_tool_runs(name, tool_input):
    inner = _Recording(name)
    events = _guarded_call(inner, tool_input)
    assert inner.calls == []
    assert events[-1].tool_result["status"] == "error"
    assert OS_PROTECTION_ERROR in events[-1].tool_result["content"][0]["text"]


def test_execution_boundary_passes_ordinary_commands():
    inner = _Recording()
    _guarded_call(inner, {"command": "ls"})
    assert inner.calls == [{"command": "ls"}]


def test_registry_wraps_every_command_tool_without_changing_names_or_specs():
    tools = tool_registry.get_tools(include_exec=True)
    wrapped = {t.tool_name: t for t in tools if isinstance(t, OsProtectionGuardTool)}
    for name in tool_registry.EXEC_TOOL_NAMES & set(tool_registry.WRITE_TOOLS):
        assert name in wrapped, name
        # The guard changes nothing; only #368's shorter description differs from upstream.
        expected = dict(tool_registry.WRITE_TOOLS[name].tool_spec)
        if name in COMPACT_DESCRIPTIONS:
            expected["description"] = COMPACT_DESCRIPTIONS[name]
        assert wrapped[name].tool_spec == expected
    # Same names in the same order as the registry: the cached prefix is unchanged (#254).
    assert [t.tool_name for t in tools] == [
        t.tool_name for t in tool_registry.get_tools(include_exec=True)
    ]
    assert tool_registry.EXEC_TOOL_NAMES <= COMMAND_TOOL_NAMES


def test_hook_layer_refuses_plain_names_even_when_hitl_approved():
    guard = IdentityGuard(Mock(blocked_tools=lambda: set(), config=None))
    for name, tool_input in [
        ("shell", {"command": "sudo ls"}),
        ("run_command", {"cmd": "chflags nouchg x"}),  # an MCP-style name
        ("batch", {"invocations": [{"name": "shell", "arguments": {"command": "csrutil status"}}]}),
    ]:
        result = guard.check_tool_call(name, tool_input, hitl_approved=True)
        assert not result.allowed and result.layer == 3, name
        # path_denial is what the HITL gate consults first: it never asks for approval.
        assert guard.path_denial(name, tool_input) is not None
    assert os_protection_denial("file_write", {"content": "sudo is mentioned in docs"}) is None


@pytest.mark.parametrize(
    "target",
    [
        "~/Library/Application Support/com.apple.TCC/TCC.db",
        "/Library/Application Support/com.apple.TCC/TCC.db",
        "~/Library/Application Support/COM.APPLE.TCC/x",
    ],
)
def test_tcc_database_is_on_the_write_floor(target):
    policy = WritePathPolicy()  # no allowlist: the floor alone must refuse it
    assert policy.check(target).code == "floor"


def test_diagnostics_never_runs_or_changes_os_protections():
    """Static: the diagnosis package spawns nothing and calls no mutating OS API."""
    package = Path(probes.__file__).parent
    banned = (
        "subprocess",
        "os.system",
        "os.popen",
        "chflags(",
        "setxattr",
        "removexattr",
        "os.chmod",
        "os.chown",
        "Popen",
    )
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        for word in banned:
            assert word not in text, f"{source.name} uses {word}"


# --- C. Permission cards ---------------------------------------------------------


def _failure(category_text: str = READ_ONLY_ERROR, path: str = "/x/a.txt"):
    return classify_write_failure("file_write", {"path": path}, category_text)


@pytest.mark.skipif(not DARWIN, reason="uchg is a BSD/macOS file flag")
def test_retry_is_single_use_and_rechecks_read_only(ws):
    target = ws / "a.txt"
    target.write_text("x", encoding="utf-8")
    board = WriteFailureBoard()
    try:
        os.chflags(target, stat.UF_IMMUTABLE)
        failure = classify_write_failure(
            "file_write", {"path": str(target)}, f"[Errno 1] Operation not permitted: '{target}'"
        )
        card_id = board.add(failure)
        assert recheck(failure)["state"] == "still_blocked"
        os.chflags(target, 0)  # the person fixes it
        outcome, card = board.retry(card_id)
        assert (outcome, card["recheck"]["state"], card["retry"]) == ("ok", "clear", "retried")
        again, _card = board.retry(card_id)
        assert again == "already_retried"
    finally:
        _unlock(target)
    assert target.read_text(encoding="utf-8") == "x"  # the board never writes


def test_retry_race_only_one_wins():
    board = WriteFailureBoard()
    card_id = board.add(_failure("[Errno 28] No space left on device: '/x/a.txt'"))
    outcomes, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        outcomes.append(board.retry(card_id)[0])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["already_retried"] * 7 + ["ok"]


def test_by_design_refusals_offer_no_retry():
    board = WriteFailureBoard()
    card_id = board.add(_failure())
    assert board.retry(card_id)[0] == "not_retryable"
    assert board.cards()[0]["retry"] == "unavailable"


def test_failing_recheck_is_unverified_not_clear():
    board = WriteFailureBoard()
    card_id = board.add(_failure("[Errno 28] No space left on device: '/x/a.txt'"))

    def broken(_failure):
        raise OSError("boom")

    outcome, card = board.retry(card_id, rechecker=broken)
    assert (outcome, card["recheck"]["state"]) == ("ok", "unverified")


def test_board_is_bounded_and_dismissable():
    board = WriteFailureBoard(capacity=3)
    ids = [board.add(_failure()) for _ in range(5)]
    assert [c["id"] for c in board.cards()] == list(reversed(ids[2:]))
    assert board.dismiss(ids[4]) and board.summary()["open"] == 2
    assert board.retry(ids[4])[0] == "not_found"


@posix_only
def test_card_shows_category_evidence_principal_and_steps(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    _fake_home(monkeypatch, home)
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    target = home / "Documents" / "x.txt"
    board = WriteFailureBoard()
    board.add(
        classify_write_failure(
            "file_write",
            {"path": str(target)},
            f"[Errno 1] Operation not permitted: '{target}'",
            platform="darwin",
        )
    )
    (card,) = board.cards()
    assert card["category"] == "protected_by_os"
    assert card["evidence"] == ["tcc:~/Documents", "errno:EPERM"]
    assert card["principal"]["kind"] == "terminal" and card["principal"]["name"] == "Terminal"
    assert "Full Disk Access is not recommended" in card["remedy_text"]
    assert card["path"] == "~/Documents/x.txt" and card["retry"] == "available"


# --- D. Doctor + status ------------------------------------------------------------


@pytest.fixture
def stira_app(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.observability.config_api import ConfigStore

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
    return stira_server.create_app(config_store=store)


def test_gateway_status_carries_the_doctor_summary_and_cards(stira_app, tmp_path):
    client = stira_app.test_client()
    status = client.get("/api/gateway/status").get_json()["diagnostics"]
    assert status["doctor"]["ran"] is True and status["doctor"]["ok"] is True
    # The effective roots (#271): @workspace, the episodic layer and the context stash.
    assert status["doctor"]["roots"] == 3 and status["write_failures"]["open"] == 0
    board = stira_app.extensions["write_failures"]
    card_id = board.add(_failure("[Errno 28] No space left on device: '/x/a.txt'"))
    status = client.get("/api/gateway/status").get_json()["diagnostics"]
    assert status["write_failures"] == {
        "open": 1,
        "latest": {"category": "other", "path": os.path.join("…", "x", "a.txt")},
    }
    cards = client.get("/api/diagnostics/write-failures").get_json()["cards"]
    assert [c["id"] for c in cards] == [card_id] and "target" not in cards[0]
    assert client.post(f"/api/diagnostics/write-failures/{card_id}/retry").status_code == 200
    second = client.post(f"/api/diagnostics/write-failures/{card_id}/retry")
    assert (second.status_code, second.get_json()["code"]) == (409, "already_retried")
    assert client.post("/api/diagnostics/write-failures/nothex/retry").status_code == 404
    report = client.get("/api/diagnostics/doctor").get_json()
    assert report["roots"][0]["write_test"] == {"ok": True}
    assert str(tmp_path) not in json.dumps(report) or str(Path.home()) not in str(tmp_path)
    assert client.post("/api/diagnostics/doctor").get_json()["ok"] is True


@pytest.mark.parametrize(
    ("saved", "code", "level"),
    [
        (None, "classifier_rule_only", "warning"),
        ("rule", "classifier_rule_chosen", "info"),
        ("sokudan", "classifier_missing", "warning"),
    ],
)
def test_the_start_up_doctor_checks_the_saved_classifier_choice(
    tmp_path, monkeypatch, saved, code, level
):
    """#267: the server's doctor follows the settings hub's choice; the sidebar counts warnings."""
    from dashboard import stira_server
    from stiracore.agent import nxp_agent
    from stiracore.diagnostics import doctor
    from stiracore.observability.config_api import ConfigStore

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(doctor, "_sokudan_installed", lambda: False)
    gateway = tmp_path / "gw.json"
    if saved is not None:
        gateway.write_text(
            json.dumps({"jev_enabled": True, "jev_classifier": saved}), encoding="utf-8"
        )
    store = ConfigStore(gateway, tmp_path / "agent.json", tmp_path / "tools.json")
    client = stira_server.create_app(config_store=store).test_client()
    report = client.get("/api/diagnostics/doctor").get_json()
    assert [(c["code"], c["level"]) for c in report["checks"]] == [(code, level)]
    summary = client.get("/api/gateway/status").get_json()["diagnostics"]["doctor"]
    assert summary["ok"] is True
    assert (summary["warnings"], code in summary["codes"]) == (
        (1, True) if level == "warning" else (0, False)
    )


@posix_only
def test_doctor_finds_problems_and_changes_nothing(tmp_path, monkeypatch):
    from stiracore.diagnostics.doctor import render_text, run_doctor, summarize

    home = tmp_path / "home"
    (home / "Documents" / "work").mkdir(parents=True)
    _fake_home(monkeypatch, home)
    good, missing, readonly = tmp_path / "good", tmp_path / "missing", tmp_path / "ro"
    good.mkdir()
    readonly.mkdir()
    link = tmp_path / "into-docs"
    link.symlink_to(home / "Documents" / "work")
    before = {p: sorted(os.listdir(p)) for p in (good, readonly)}
    hits = _spy_fs(monkeypatch, home / "Documents")
    roots = [str(good), str(missing), str(home / "Documents" / "work"), str(link), str(readonly)]
    try:
        readonly.chmod(0o500)
        # TCC zones are a macOS notion: judge them as on macOS on every CI OS.
        report = run_doctor(roots, workspace=tmp_path, platform="darwin")
    finally:
        readonly.chmod(0o700)
    assert hits == []  # TCC root and the symlink into it were never read
    status = {entry["path"].rsplit("/", 1)[-1]: entry for entry in report["roots"]}
    assert status["good"]["status"] == "ok" and status["good"]["write_test"] == {"ok": True}
    assert status["missing"]["findings"][0]["code"] == "missing"
    assert status["work"]["findings"][0]["code"] == "tcc_zone"
    assert status["work"]["write_test"] is None
    assert status["into-docs"]["findings"][0]["code"] == "tcc_zone"
    assert "through a symlink" in status["into-docs"]["findings"][0]["message"]
    if not ROOT_USER:
        assert status["ro"]["status"] == "error"
        assert status["ro"]["write_test"]["category"] == "permission_denied"
    assert {p: sorted(os.listdir(p)) for p in (good, readonly)} == before  # no leftovers
    assert not missing.exists()  # never created
    summary = summarize(report)
    assert summary["ran"] and summary["warnings"] >= 3
    assert "tcc_zone" in summary["codes"]
    assert "changed nothing" in render_text(report)
    assert str(home) not in json.dumps(report)


@pytest.mark.skipif(not DARWIN, reason="uchg is a BSD/macOS file flag")
def test_doctor_detects_a_locked_root(tmp_path):
    from stiracore.diagnostics.doctor import run_doctor

    root = tmp_path / "locked"
    root.mkdir()
    try:
        os.chflags(root, stat.UF_IMMUTABLE)
        (entry,) = run_doctor([str(root)], workspace=tmp_path)["roots"]
    finally:
        _unlock(root)
    codes = [f["code"] for f in entry["findings"]]
    assert "locked_flag" in codes and entry["write_test"]["category"] == "locked_flag"
    assert os.listdir(root) == []


def test_doctor_skips_the_write_test_on_the_floor(tmp_path):
    from stiracore.diagnostics.doctor import run_doctor

    protected = tmp_path / "procedural"
    protected.mkdir()
    policy = WritePathPolicy(base_dir=tmp_path, protected_dirs=[protected])
    (entry,) = run_doctor([str(protected)], workspace=tmp_path, policy=policy)["roots"]
    assert entry["write_test"] is None and entry["findings"][0]["code"] == "floor"
    assert os.listdir(protected) == []


def test_stira_doctor_cli_reads_config_and_writes_nothing(tmp_path, capsys):
    from stiracore.diagnostics.__main__ import main

    root = tmp_path / "work"
    root.mkdir()
    config = tmp_path / "agent.json"
    config.write_text(
        json.dumps({"workspace_dir": str(root), "write_roots": ["@workspace"]}), encoding="utf-8"
    )
    before = (config.read_bytes(), config.stat().st_mtime_ns, sorted(os.listdir(tmp_path)))
    assert main(["doctor", "--config", str(config), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["roots"][0]["root"] == "@workspace"
    assert (config.read_bytes(), config.stat().st_mtime_ns, sorted(os.listdir(tmp_path))) == before
    assert os.listdir(root) == []
    assert main(["doctor", "--config", str(config)]) == 0
    assert "stira doctor" in capsys.readouterr().out


def test_every_card_text_key_exists_in_both_catalogs():
    from stiracore.diagnostics.write_errors import CATEGORIES, REMEDIES

    root = Path(__file__).resolve().parents[1] / "dashboard" / "static" / "i18n"
    for lang in ("ja", "en"):
        catalog = json.loads((root / f"catalog.{lang}.json").read_text(encoding="utf-8"))
        keys = (
            [f"diagnostics.cat_{c}" for c in CATEGORIES]
            + [f"diagnostics.fix_{r}" for r in REMEDIES]
            + [
                f"diagnostics.principal_{k}"
                for k in ("terminal", "app", "launchd", "process", "user")
            ]
        )
        missing = [key for key in keys if key not in catalog]
        assert missing == [], (lang, missing)


def test_model_chosen_editor_command_never_reaches_the_card():
    failure = classify_write_failure(
        "editor",
        {"command": "create. Then run sudo", "path": "/x/SOUL.md"},
        READ_ONLY_ERROR,
    )
    assert failure.operation == "edit"


# --- Review round 1 (#266): reproductions first, then the fixes -------------------


@posix_only
def test_doctor_write_test_does_not_follow_a_root_swapped_after_the_check(tmp_path, monkeypatch):
    """The root is swapped for a symlink to a protected folder after it was inspected."""
    from stiracore.diagnostics.doctor import run_doctor

    root, protected = tmp_path / "root", tmp_path / "protected"
    root.mkdir()
    protected.mkdir()
    stamp = protected.stat().st_mtime_ns
    real = probes.readonly_mount

    def swap_then_answer(path, **kwargs):
        answer = real(path, **kwargs)
        if os.fspath(path) == str(root.resolve()) and not root.is_symlink():
            root.rename(tmp_path / "root.moved")
            root.symlink_to(protected, target_is_directory=True)
        return answer

    monkeypatch.setattr(probes, "readonly_mount", swap_then_answer)
    (entry,) = run_doctor([str(root)], workspace=tmp_path)["roots"]
    assert entry["write_test"]["ok"] is None  # "changed" (re-judged) or "symlink_in_path"
    assert os.listdir(protected) == [] and protected.stat().st_mtime_ns == stamp
    assert os.listdir(tmp_path / "root.moved") == []  # nothing left behind either


@posix_only
def test_classify_recheck_and_doctor_never_follow_a_symlink_into_a_tcc_zone(tmp_path, monkeypatch):
    """``work/link -> ~/Documents``: diagnosing ``work/link/file`` must stop at the link."""
    from stiracore.diagnostics.doctor import run_doctor

    home = tmp_path / "home"
    docs = home / "Documents"
    (docs / "sub").mkdir(parents=True)
    _fake_home(monkeypatch, home)
    link = tmp_path / "work" / "link"
    link.parent.mkdir()
    link.symlink_to(docs, target_is_directory=True)
    target = link / "file.txt"
    hits = _spy_fs(monkeypatch, docs, str(link) + os.sep)
    text = f"[Errno 1] Operation not permitted: '{target}'"
    failure = classify_write_failure("file_write", {"path": str(target)}, text, platform="darwin")
    assert (failure.category, failure.remedy) == ("protected_by_os", "tcc")
    assert "tcc:~/Documents" in failure.evidence
    assert recheck(failure, platform="darwin")["state"] == "unverified"
    (entry,) = run_doctor([str(link / "sub")], workspace=tmp_path, platform="darwin")["roots"]
    assert entry["findings"][0]["code"] == "tcc_zone"
    assert hits == []


@pytest.mark.parametrize(
    "path",
    [
        "/System/Volumes/Data/Library/Application Support/com.apple.TCC/TCC.db",
        "/private/var/db/TCC/x",
        "/LIBRARY/application support/COM.APPLE.TCC/TCC.db",
    ],
)
@posix_only
def test_system_tcc_zone_spellings(path):
    assert probes.tcc_zone(path, platform="darwin") is not None


def test_paths_outside_home_and_workspace_are_shortened(tmp_path):
    workspace = tmp_path / "ws"
    assert probes.display_path("/srv/private/proj/x.txt") == os.path.join("…", "proj", "x.txt")
    assert probes.display_path(workspace / "a" / "b.txt", base=workspace) == os.path.join(
        "@workspace", "a", "b.txt"
    )
    text = "Error writing file: [Errno 28] No space left on device: '/srv/private/proj/x.txt'"
    failure = classify_write_failure("file_write", {"path": "/srv/private/proj/x.txt"}, text)
    assert "/srv/private" not in json.dumps(failure.as_dict())
    assert failure.target == os.path.normpath("/srv/private/proj/x.txt")  # kept in memory only


@pytest.mark.parametrize(
    "env",
    [
        {"TERM_PROGRAM": "token=sk-abcdef1234567890 $(id)"},
        {"__CFBundleIdentifier": "com.example/../secret value"},
        {"XPC_SERVICE_NAME": "a" * 200},
    ],
)
def test_principal_name_from_the_environment_is_validated(env):
    info = probes.responsible_process(env, "/usr/bin/python3", platform="darwin")
    assert info["name"] == "unknown"


def test_known_principal_names_are_kept():
    info = probes.responsible_process(
        {"__CFBundleIdentifier": "com.apple.Terminal"}, "/usr/bin/python3", platform="darwin"
    )
    assert info["name"] == "com.apple.Terminal"


def test_hook_failure_logs_no_exception_text(monkeypatch, caplog):
    import stiracore.diagnostics.write_errors as module

    def boom(*args, **kwargs):
        raise RuntimeError("api_key=SUPERSECRETVALUE123")

    monkeypatch.setattr(module, "classify_write_failure", boom)
    result = {"status": "error", "content": [{"text": "[Errno 1] Operation not permitted: '/x'"}]}
    with caplog.at_level("WARNING"):
        assert _hooked("file_write", {"path": "/x"}, result) == result
    assert "SUPERSECRETVALUE123" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_recheck_of_erofs_is_clear_once_the_volume_is_writable(tmp_path):
    target = tmp_path / "x.txt"
    failure = classify_write_failure(
        "file_write", {"path": str(target)}, f"[Errno 30] Read-only file system: '{target}'"
    )
    assert failure.category == "readonly_volume"
    assert recheck(failure)["state"] == "clear"


def test_computed_command_names_are_out_of_scope_for_the_denylist():
    """Documented limit, not a guarantee: the denylist matches *plain* command names only.

    A name assembled at run time (``getattr(os, "ch" + "flags")``, base64, variables)
    passes it. Exec-class tools are disabled by default; containing them is the exec
    sandbox's job (#167). This test pins the documented scope so the docs stay honest.
    """
    computed = 'python3 -c \'import os; getattr(os, "ch" + "flags")("x", 0)\''
    assert os_protection_problem({"command": computed}) is None
    assert "STIRA_ENABLE_EXEC_TOOLS" == tool_registry.EXEC_ENV_VAR
    assert not {t.tool_name for t in tool_registry.get_tools()} & tool_registry.EXEC_TOOL_NAMES


# --- Review round 2 (#266) --------------------------------------------------------


def _ancestor_swap_layout(tmp_path):
    """``work/slot/root`` (the write root) and ``protected/root`` (on the floor)."""
    slot = tmp_path / "work" / "slot"
    (slot / "root").mkdir(parents=True)
    protected = tmp_path / "protected"
    (protected / "root").mkdir(parents=True)
    return slot, protected


def _swap(slot: Path, protected: Path, *, to_link: bool) -> None:
    aside = slot.with_name("slot.real")
    if to_link and not slot.is_symlink():
        slot.rename(aside)
        slot.symlink_to(protected, target_is_directory=True)
    elif not to_link and slot.is_symlink():
        slot.unlink()
        aside.rename(slot)


@posix_only
def test_doctor_write_test_refuses_an_ancestor_swapped_for_a_symlink(tmp_path, monkeypatch):
    """Astra round 2: only the last component was O_NOFOLLOW; a swapped ``slot`` was followed."""
    from stiracore.diagnostics.doctor import run_doctor

    slot, protected = _ancestor_swap_layout(tmp_path)
    root = slot / "root"
    stamp = (protected / "root").stat().st_mtime_ns
    real = probes.readonly_mount

    def swap_then_answer(path, **kwargs):  # after the identity was recorded
        answer = real(path, **kwargs)
        _swap(slot, protected, to_link=True)
        return answer

    monkeypatch.setattr(probes, "readonly_mount", swap_then_answer)
    (entry,) = run_doctor([str(root)], workspace=tmp_path)["roots"]
    assert entry["write_test"]["ok"] is None
    assert entry["write_test"]["skipped"] in {"symlink_in_path", "changed"}
    assert os.listdir(protected / "root") == []
    assert (protected / "root").stat().st_mtime_ns == stamp


@posix_only
def test_doctor_write_test_survives_the_flip_flop_around_the_floor_check(tmp_path, monkeypatch):
    """Symlink at lstat, restored while the floor is judged, symlink again afterwards."""
    from stiracore.diagnostics import doctor

    slot, protected = _ancestor_swap_layout(tmp_path)
    root = slot / "root"
    stamp = (protected / "root").stat().st_mtime_ns
    real_resolve = probes.safe_resolve

    def resolve_then_swap(path, **kwargs):
        answer = real_resolve(path, **kwargs)
        if os.fspath(path) == str(root):
            _swap(slot, protected, to_link=True)  # before the identity lstat
        return answer

    class FlipFlopPolicy:
        def is_read_only(self, path):
            _swap(slot, protected, to_link=False)  # judged on the real tree ...
            on_floor = Path(path).resolve().is_relative_to(protected.resolve())
            _swap(slot, protected, to_link=True)  # ... then swapped back
            return on_floor

    monkeypatch.setattr(probes, "safe_resolve", resolve_then_swap)
    (entry,) = doctor.run_doctor([str(root)], workspace=tmp_path, policy=FlipFlopPolicy())["roots"]
    assert entry["write_test"] is None or entry["write_test"]["ok"] is not True
    assert os.listdir(protected / "root") == []
    assert (protected / "root").stat().st_mtime_ns == stamp


def test_doctor_skips_when_fd_walking_is_unsupported(tmp_path, monkeypatch):
    from stiracore.diagnostics import doctor

    root = tmp_path / "r"
    root.mkdir()
    monkeypatch.setattr(doctor, "_fd_walk_supported", lambda: False)
    report = doctor.run_doctor([str(root)], workspace=tmp_path)
    (entry,) = report["roots"]
    assert entry["write_test"] == {"ok": None, "skipped": "unsupported"}
    assert os.listdir(root) == []
    text = doctor.render_text(report)  # used to raise KeyError('error')
    assert "write test: skipped (unsupported)" in text


@posix_only
def test_doctor_reports_a_symlink_loop_per_root_and_continues(tmp_path):
    from stiracore.diagnostics.doctor import render_text, run_doctor

    loop = tmp_path / "loop"
    loop.symlink_to(tmp_path / "loop2")
    (tmp_path / "loop2").symlink_to(loop)
    good = tmp_path / "good"
    good.mkdir()
    report = run_doctor([str(loop / "x"), str(good)], workspace=tmp_path)
    codes = [
        entry["findings"][0]["code"] if entry["findings"] else None for entry in report["roots"]
    ]
    assert codes[0] == "unresolvable" and report["roots"][1]["status"] == "ok"
    assert "unresolvable" not in render_text(report) or "[ERROR]" in render_text(report)


@pytest.mark.parametrize(
    "env",
    [
        {"TERM_PROGRAM": "SomethingNew"},  # allowed characters, but not a known value
        {"__CFBundleIdentifier": "com.example.secretproject"},
        {"XPC_SERVICE_NAME": "com.example.private-agent"},
    ],
)
def test_principal_names_outside_the_allowlist_are_unknown(env):
    info = probes.responsible_process(env, "/usr/bin/python3", platform="darwin")
    assert info["name"] == "unknown"


def test_cli_does_not_echo_the_config_path(tmp_path, capsys, monkeypatch):
    from stiracore.diagnostics.__main__ import main

    home = tmp_path / "home"
    home.mkdir()
    _fake_home(monkeypatch, home)
    broken = home / "private-project" / "agent.json"
    broken.parent.mkdir()
    broken.write_text("{not json", encoding="utf-8")
    assert main(["doctor", "--config", str(broken)]) == 2
    err = capsys.readouterr().err
    assert str(home) not in err and "agent.json" in err


@posix_only
@pytest.mark.parametrize(
    "spelling",
    [
        "decomposed_home",  # HOME in NFD, target in NFC
        "case_variant",  # /USERS/... vs /Users/...
        "data_volume",  # /System/Volumes/Data/<home>/Documents
    ],
)
def test_zone_spellings_are_recognised_without_touching_them(tmp_path, monkeypatch, spelling):
    import unicodedata

    base = tmp_path / unicodedata.normalize("NFD", "café")
    home = base / "home"
    _fake_home(monkeypatch, home)
    target = str(home / "Documents" / "x.txt")
    if spelling == "decomposed_home":
        target = unicodedata.normalize("NFC", target)
    elif spelling == "case_variant":
        target = target.upper()
    else:
        target = "/System/Volumes/Data" + target
    hits = _spy_fs(monkeypatch, home)
    assert probes.tcc_zone(target, platform="darwin") == "~/Documents"
    failure = classify_write_failure(
        "file_write",
        {"path": target},
        f"[Errno 1] Operation not permitted: '{target}'",
        platform="darwin",
    )
    assert failure.remedy == "tcc" and hits == []


# --- Review round 3 (#266) --------------------------------------------------------


@posix_only
@pytest.mark.parametrize("parent", ["tmp_path", "system_tmp"])
def test_doctor_without_a_policy_still_respects_the_os_protected_floor(tmp_path, parent):
    """policy=None used to mean "allowed": a temp file landed under ``com.apple.TCC``."""
    import tempfile

    from stiracore.diagnostics.doctor import run_doctor

    base = tmp_path if parent == "tmp_path" else Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        protected = Path(os.path.realpath(base)) / "com.apple.TCC"
        protected.mkdir()
        stamp = protected.stat().st_mtime_ns
        (entry,) = run_doctor([str(protected)], workspace=tmp_path, platform="linux")["roots"]
        assert entry["write_test"] is None or entry["write_test"]["ok"] is not True
        assert os.listdir(protected) == [] and protected.stat().st_mtime_ns == stamp
    finally:
        if parent != "tmp_path":
            import shutil

            shutil.rmtree(base)


def test_doctor_without_a_policy_still_respects_identity_names(tmp_path):
    from stiracore.diagnostics import doctor

    root = tmp_path / "soul.md"  # a directory named like an identity file: on the floor
    root.mkdir()
    (entry,) = doctor.run_doctor([str(root)], workspace=tmp_path)["roots"]
    assert entry["write_test"] is None and os.listdir(root) == []


def test_cli_builds_the_configured_policy_and_skips_without_one(tmp_path, capsys):
    from stiracore.diagnostics.__main__ import main

    work = tmp_path / "work"
    (work / "com.apple.TCC").mkdir(parents=True)
    config = tmp_path / "agent.json"
    config.write_text(json.dumps({"workspace_dir": str(work)}), encoding="utf-8")
    assert main(
        ["doctor", "--config", str(config), "--json", "--root", str(work / "com.apple.TCC")]
    ) in (0, 1)
    report = json.loads(capsys.readouterr().out)
    assert report["roots"][0]["write_test"] is None
    assert os.listdir(work / "com.apple.TCC") == []
    # A policy that cannot be built (a too-broad root without opt-in): no write test at all.
    config.write_text(
        json.dumps({"workspace_dir": str(work), "write_roots": ["/"]}), encoding="utf-8"
    )
    # ... is a configuration error: exit 2, not a quiet warning (#267).
    assert main(["doctor", "--config", str(config), "--json", "--root", str(work)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["roots"][0]["write_test"] == {"ok": None, "skipped": "no_policy"}
    assert os.listdir(work) == ["com.apple.TCC"]


@posix_only
def test_doctor_skips_a_root_that_is_a_symlink_from_the_start(tmp_path):
    """The walk gets the path as configured, not the resolved one: ``link -> real`` is skipped."""
    from stiracore.diagnostics.doctor import run_doctor

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    nested = tmp_path / "via" / "inner"
    (tmp_path / "real2" / "inner").mkdir(parents=True)
    (tmp_path / "via").symlink_to(tmp_path / "real2", target_is_directory=True)
    report = run_doctor([str(link), str(nested)], workspace=tmp_path)
    for entry in report["roots"]:
        assert entry["write_test"] == {"ok": None, "skipped": "symlink_in_path"}
    assert os.listdir(real) == [] and os.listdir(tmp_path / "real2" / "inner") == []

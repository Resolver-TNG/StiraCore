import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore.tools import lsp_tools, tool_registry


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "sample file.py"
    path.write_text('def hello():\n    """Greeting."""\n    return missing\n\nhello()\n')
    return path


def test_ruff_mock(source, monkeypatch):
    monkeypatch.setattr(lsp_tools, "_executable", lambda name: "ruff" if name == "ruff" else None)

    def run(args, data, *, timeout):
        assert "--no-fix" in args and "--no-cache" in args
        # The checked bytes on stdin, the name only as --stdin-filename (#315).
        assert args[-3:] == ["--stdin-filename", str(source), "-"]
        assert data == source.read_bytes()
        assert timeout == 30
        return 1, json.dumps(
            [
                {
                    "filename": str(source),
                    "code": "F821",
                    "message": "Undefined name `missing`",
                    "location": {"row": 3, "column": 12},
                    "end_location": {"row": 3, "column": 19},
                }
            ]
        ).encode()

    monkeypatch.setattr(lsp_tools, "_run_bounded", run)
    result = lsp_tools.lsp_diagnostics(str(source))
    assert result["status"] == "success"
    assert result["capability"] == "lint"
    assert result["diagnostics"][0]["start"] == {"line": 3, "column": 12}


def test_pyright_positions(source, monkeypatch):
    monkeypatch.setattr(lsp_tools, "_executable", lambda name: name)
    seen = {}

    def rows(executable, path, text, timeout):
        seen.update(executable=executable, path=path, text=text, timeout=timeout)
        return [
            {
                "file": str(source),
                "severity": "error",
                "message": "undefined",
                "rule": "reportUndefinedVariable",
                "range": {
                    "start": {"line": 2, "character": 11},
                    "end": {"line": 2, "character": 18},
                },
            }
        ]

    monkeypatch.setattr(lsp_tools, "_pyright_rows", rows)
    result = lsp_tools.lsp_diagnostics(str(source))
    # The language server gets the checked text, not a name to open (#315).
    assert seen == {
        "executable": "pyright-langserver",
        "path": source,
        "text": source.read_text(),
        "timeout": 30,
    }
    assert result["backend"] == "pyright"
    assert result["diagnostics"][0]["start"] == {"line": 3, "column": 12}


def test_missing_tools_skip(source, monkeypatch):
    monkeypatch.setattr(lsp_tools, "_executable", lambda name: None)
    assert lsp_tools.lsp_diagnostics(str(source))["status"] == "skipped"


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired("pyright", 30), ValueError("bad JSON")]
)
def test_broken_pyright_falls_back(source, monkeypatch, failure):
    monkeypatch.setattr(lsp_tools, "_executable", lambda name: name)

    def broken(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(lsp_tools, "_pyright_rows", broken)
    monkeypatch.setattr(lsp_tools, "_run_bounded", lambda args, data, *, timeout: (0, b"[]"))
    result = lsp_tools.lsp_diagnostics(str(source))
    assert result["backend"] == "ruff"
    assert result["warnings"]


def test_real_ruff(source, monkeypatch):
    executable = lsp_tools._executable("ruff") or shutil.which("ruff")
    if not executable:
        pytest.skip("optional ruff is not installed")
    before = source.read_bytes()
    monkeypatch.setattr(
        lsp_tools, "_executable", lambda name: executable if name == "ruff" else None
    )
    result = lsp_tools.lsp_diagnostics(str(source))
    assert result["status"] == "success"
    assert any(d["code"] == "F821" for d in result["diagnostics"])
    assert source.read_bytes() == before


def test_ast_hover(source, monkeypatch):
    monkeypatch.setitem(sys.modules, "jedi", None)
    result = lsp_tools.lsp_hover(str(source), 5, 2)
    assert result["backend"] == "ast"
    assert result["symbols"][0]["name"] == "hello"
    assert result["symbols"][0]["documentation"] == "Greeting."
    assert lsp_tools.lsp_hover(str(source), 0, 1)["status"] == "error"
    assert lsp_tools.lsp_hover(str(source), 1, 999)["status"] == "error"


def test_bad_file_and_registration(tmp_path):
    assert lsp_tools.lsp_diagnostics(str(tmp_path / "missing.py"))["status"] == "error"
    assert {"lsp_diagnostics", "lsp_hover"} <= tool_registry.WRITE_TOOLS.keys()
    assert not {"lsp_diagnostics", "lsp_hover"} & {
        t.tool_name for t in tool_registry.get_tools("critical")
    }


def test_jedi_hover(source, monkeypatch):
    symbol = SimpleNamespace(
        name="hello",
        type="function",
        description="def hello",
        line=1,
        docstring=lambda: "Greeting.",
    )

    def script(**kwargs):
        assert kwargs["path"] == str(source)

        def infer(line, column):
            assert (line, column) == (5, 1)
            return [symbol]

        return SimpleNamespace(infer=infer)

    monkeypatch.setitem(sys.modules, "jedi", SimpleNamespace(Script=script))
    result = lsp_tools.lsp_hover(str(source), 5, 2)
    assert result["backend"] == "jedi"
    assert result["symbols"][0]["documentation"] == "Greeting."


# --- #315: the analyzers get the checked bytes, not a name to open again --------------

FAKE_SERVER = Path(__file__).with_name("fake_pyright_langserver.py")
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="a shell launcher")


def _fake_langserver(tmp_path: Path, mode: str = "", argument: str = "") -> str:
    launcher = tmp_path / "bin" / "pyright-langserver"
    launcher.parent.mkdir(exist_ok=True)
    # Set by the launcher itself: the analyzer environment is allow-listed.
    env = f"FAKE_LSP_MODE={mode} FAKE_LSP_ARG='{argument}' " if mode else ""
    launcher.write_text(f'#!/bin/sh\n{env}exec "{sys.executable}" "{FAKE_SERVER}" "$@"\n')
    launcher.chmod(0o755)
    return str(launcher)


@POSIX_ONLY
def test_pyright_session_sends_the_checked_text(tmp_path, source, monkeypatch):
    server = _fake_langserver(tmp_path)
    monkeypatch.setattr(
        lsp_tools, "_executable", lambda name: server if "pyright" in name else None
    )
    result = lsp_tools.lsp_diagnostics(str(source))
    assert result["status"] == "success" and result["backend"] == "pyright", result
    lines = source.read_text().splitlines()
    assert [d["message"] for d in result["diagnostics"]] == [f"seen: {line}" for line in lines]
    first = result["diagnostics"][0]
    assert (first["severity"], first["code"], first["file"]) == ("error", "fakeRule", str(source))
    assert first["start"] == {"line": 1, "column": 1}


@POSIX_ONLY
def test_a_silent_pyright_server_times_out_and_is_ended(tmp_path, source, monkeypatch):
    import time

    server = _fake_langserver(tmp_path, mode="hang")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        lsp_tools._pyright_rows(server, source, source.read_text(), timeout=1.5)
    assert time.monotonic() - started < 6


def test_only_the_pyright_cli_is_not_used(source, monkeypatch):
    # Without its language server Pyright could only open the file by name: skipped.
    monkeypatch.setattr(lsp_tools, "_executable", lambda name: name if name == "pyright" else None)
    result = lsp_tools.lsp_diagnostics(str(source))
    assert result["status"] == "error"
    assert "pyright-langserver not found" in result["warnings"][0]


def test_real_pyright_langserver_analyses_the_checked_text(tmp_path, monkeypatch):
    server = lsp_tools._executable("pyright-langserver") or shutil.which("pyright-langserver")
    if not server:
        pytest.skip("optional pyright is not installed")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "sib.py").write_text("def helper() -> int:\n    return 1\n")
    target = tmp_path / "pkg" / "main.py"
    target.write_text("from . import sib\nchecked: bytes = sib.helper()\n")
    monkeypatch.setattr(
        lsp_tools, "_executable", lambda name: server if "pyright" in name else None
    )
    result = lsp_tools.lsp_diagnostics(str(target))
    assert result["status"] == "success" and result["backend"] == "pyright", result
    (diagnostic,) = result["diagnostics"]
    assert diagnostic["code"] == "reportAssignmentType"  # the relative import resolved
    assert '"int" is not assignable to declared type "bytes"' in diagnostic["message"]
    assert diagnostic["start"] == {"line": 2, "column": 18}


# --- review round 1: one deadline, bounded output, allow-listed environment ---------------


def _gone(pid: int, within: float = 3.0) -> bool:
    import os
    import time

    until = time.monotonic() + within
    while time.monotonic() < until:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.02)
    return False


def _started(monkeypatch) -> list:
    started = []
    real = subprocess.Popen

    def recording(*args, **kwargs):
        process = real(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(lsp_tools.subprocess, "Popen", recording)
    return started


@POSIX_ONLY
def test_a_server_that_stops_reading_cannot_block_past_the_deadline(tmp_path, monkeypatch):
    import time

    server = _fake_langserver(tmp_path, mode="noread")
    started = _started(monkeypatch)
    text = "x = 1\n" * (lsp_tools.MAX_TEXT_BYTES // 6)  # about 1 MiB: fills the pipe
    began = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        lsp_tools._pyright_rows(server, tmp_path / "a.py", text, timeout=2.0)
    assert time.monotonic() - began < 4.0  # deadline + the kill and joins
    assert started and started[0].returncode is not None
    assert not [
        t for t in __import__("threading").enumerate() if t.name.startswith("stira-analyzer")
    ]


@POSIX_ONLY
def test_the_whole_process_group_is_ended(tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    server = _fake_langserver(tmp_path, mode="child", argument=str(pid_file))
    with pytest.raises(subprocess.TimeoutExpired):
        lsp_tools._pyright_rows(server, tmp_path / "a.py", "x = 1\n", timeout=2.0)
    assert _gone(int(pid_file.read_text()))  # the server's own child too


@POSIX_ONLY
@pytest.mark.parametrize("mode", ["hugeheader", "hugebody", "flood"])
def test_oversized_server_output_ends_it_and_shows_nothing(tmp_path, monkeypatch, mode):
    import time

    server = _fake_langserver(tmp_path, mode=mode)
    started = _started(monkeypatch)
    began = time.monotonic()
    with pytest.raises(lsp_tools.AnalyzerOutputLimit):
        lsp_tools._pyright_rows(server, tmp_path / "a.py", "x = 1\n", timeout=20.0)
    assert time.monotonic() - began < 15.0  # the bound, not the deadline, ended it
    assert started[0].returncode is not None


@POSIX_ONLY
def test_lsp_diagnostics_reports_an_oversized_analyzer_as_not_diagnosed(
    tmp_path, source, monkeypatch
):
    server = _fake_langserver(tmp_path, mode="hugebody")
    monkeypatch.setattr(
        lsp_tools, "_executable", lambda name: server if "pyright" in name else None
    )
    result = lsp_tools.lsp_diagnostics(str(source))
    assert result["status"] == "error" and result["diagnostics"] == []
    assert "exceeds the limit" in result["warnings"][0]


def _fake_ruff(tmp_path: Path, body: str) -> str:
    script = tmp_path / "bin" / "ruff"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"#!{sys.executable}\n{body}\n")
    script.chmod(0o755)
    return str(script)


@POSIX_ONLY
def test_ruff_output_is_bounded(tmp_path):
    ruff = _fake_ruff(
        tmp_path,
        "import sys\nsys.stdin.buffer.read()\nwhile True:\n    sys.stdout.buffer.write(b'[' * 65536)",
    )
    with pytest.raises(lsp_tools.AnalyzerOutputLimit):
        lsp_tools._run_bounded([ruff, "-"], b"x = 1\n", timeout=20.0)


@POSIX_ONLY
def test_ruff_gets_the_bytes_and_its_exit_code_back(tmp_path):
    ruff = _fake_ruff(
        tmp_path,
        "import sys\ndata = sys.stdin.buffer.read()\nsys.stdout.buffer.write(data[::-1])\n"
        "sys.exit(1)",
    )
    assert lsp_tools._run_bounded([ruff, "-"], b"abc", timeout=20.0) == (1, b"cba")


@POSIX_ONLY
def test_analyzers_do_not_inherit_secrets_from_the_server_environment(
    tmp_path, source, monkeypatch
):
    for name in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "OPENAI_API_KEY", "GH_TOKEN"):
        monkeypatch.setenv(name, "dummy-secret-for-the-test")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    names_file = tmp_path / "pyright-env.json"
    server = _fake_langserver(tmp_path, mode="env", argument=str(names_file))
    lsp_tools._pyright_rows(server, source, "x = 1\n", timeout=20.0)
    ruff = _fake_ruff(
        tmp_path,
        "import json, os, sys\nsys.stdin.buffer.read()\n"
        "sys.stdout.write(json.dumps(sorted(os.environ)))",
    )
    _code, out = lsp_tools._run_bounded([ruff, "-"], b"", timeout=20.0)
    # The launcher adds the fake's own FAKE_LSP_* variables.
    for seen in (
        set(json.loads(names_file.read_text())) - {"FAKE_LSP_MODE", "FAKE_LSP_ARG"},
        set(json.loads(out)),
    ):
        assert not {n for n in seen if "SECRET" in n or "TOKEN" in n or "KEY" in n}, seen
        assert "PATH" in seen and "LC_ALL" in seen
        allowed = lsp_tools._ENV_ALLOWED
        # Only the allow-list (plus LC_*, and what the shell/interpreter set itself).
        extra = {n for n in seen if n not in allowed and not n.startswith("LC_")}
        assert extra <= {"PWD", "SHLVL", "_", "OLDPWD", "__CF_USER_TEXT_ENCODING"}, extra


# --- review round 3: the group after a normal exit, end order, late overflow, locale ----


@POSIX_ONLY
@pytest.mark.parametrize("holds_stdout", [False, True])
def test_a_normal_exit_still_ends_the_analyzers_descendants(tmp_path, holds_stdout):
    import time

    pid_file = tmp_path / "child.pid"
    out = "None" if holds_stdout else "subprocess.DEVNULL"
    ruff = _fake_ruff(
        tmp_path,
        "import subprocess, sys\nsys.stdin.buffer.read()\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'],"
        f" stdout={out})\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "sys.stdout.write('[]')\nsys.stdout.flush()\nsys.exit(0)",
    )
    began = time.monotonic()
    assert lsp_tools._run_bounded([ruff, "-"], b"", timeout=10.0) == (0, b"[]")
    assert time.monotonic() - began < 5.0  # not the deadline: the parent's exit counts
    assert _gone(int(pid_file.read_text()))


@POSIX_ONLY
def test_pyright_end_returns_in_time_when_a_child_keeps_stdout(tmp_path, source):
    import time

    pid_file = tmp_path / "holder.pid"
    server = _fake_langserver(tmp_path, mode="holder", argument=str(pid_file))
    began = time.monotonic()
    rows = lsp_tools._pyright_rows(server, source, "x = 1\n", timeout=10.0)
    assert [r["message"] for r in rows] == ["seen: x = 1"]
    assert time.monotonic() - began < 5.0
    assert _gone(int(pid_file.read_text()))
    assert not [
        t for t in __import__("threading").enumerate() if t.name.startswith("stira-analyzer")
    ]


@POSIX_ONLY
def test_an_overflow_after_the_answer_discards_the_result(tmp_path, source):
    server = _fake_langserver(tmp_path, mode="tailheader")
    with pytest.raises(lsp_tools.AnalyzerOutputLimit):
        lsp_tools._pyright_rows(server, source, "x = 1\n", timeout=10.0)


def test_only_listed_locale_variables_reach_an_analyzer(monkeypatch):
    monkeypatch.setenv("LC_API_TOKEN", "dummy-secret-for-the-test")
    monkeypatch.setenv("LC_SECRET", "dummy-secret-for-the-test")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("LC_MESSAGES", "C")
    env = lsp_tools._analyzer_env()
    assert "LC_API_TOKEN" not in env and "LC_SECRET" not in env
    assert env["LC_ALL"] == "C.UTF-8" and env["LC_MESSAGES"] == "C"


@POSIX_ONLY
def test_the_analyzer_is_started_in_a_group_with_the_allow_listed_environment(
    tmp_path, monkeypatch
):
    calls = []
    real = subprocess.Popen

    def capture(args, **kwargs):
        calls.append((list(args), dict(kwargs)))
        return real(args, **kwargs)

    monkeypatch.setattr(lsp_tools.subprocess, "Popen", capture)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "dummy-secret-for-the-test")
    ruff = _fake_ruff(tmp_path, "import sys\nsys.stdin.buffer.read()\nsys.stdout.write('[]')")
    assert lsp_tools._run_bounded([ruff, "-"], b"", timeout=10.0) == (0, b"[]")
    (_sentinel_args, sentinel), (analyzer_args, analyzer) = calls
    assert analyzer_args == [ruff, "-"]
    assert sentinel["process_group"] == 0  # the group's own leader, held until the end
    assert analyzer["process_group"] > 0 and analyzer["stderr"] == subprocess.DEVNULL
    for kwargs in (sentinel, analyzer):
        assert "AWS_SECRET_ACCESS_KEY" not in kwargs["env"] and "PATH" in kwargs["env"]


@POSIX_ONLY
def test_a_descendant_that_left_the_group_cannot_hold_the_call(tmp_path):
    # Documented limit: it is not ended; the call still returns in time and refuses the
    # output it cannot know complete. The test ends the escaped child itself.
    import os
    import signal
    import time

    pid_file = tmp_path / "escaped.pid"
    ruff = _fake_ruff(
        tmp_path,
        "import subprocess, sys\nsys.stdin.buffer.read()\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3600)'],"
        " start_new_session=True)\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "sys.stdout.write('[]')\nsys.stdout.flush()",
    )
    began = time.monotonic()
    try:
        with pytest.raises(lsp_tools.AnalyzerOutputLimit, match="did not end"):
            lsp_tools._run_bounded([ruff, "-"], b"", timeout=10.0)
        assert time.monotonic() - began < 6.0  # the exit, the kill, the bounded join
    finally:
        os.kill(int(pid_file.read_text()), signal.SIGKILL)

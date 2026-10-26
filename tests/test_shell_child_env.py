"""#457: an approved ``shell`` command gets an allowlisted environment, never the server's keys.

Measured before #457: the upstream ``shell`` starts ``/bin/sh -c <command>`` with
``os.execvp`` in a PTY child, which inherits the whole server environment (provider keys,
``AWS_*``, tokens). Now the module's scoped ``os`` (#362) runs ``execvpe`` with
``child_env.child_environment()``.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from stiracore.tools import tool_consent, tool_registry
from stiracore.tools.child_env import CHILD_ENV_ALLOWED, child_environment
from stiracore.tools.tool_consent import mark_gate_passed

pytest.importorskip("strands_tools.shell")
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="upstream shell uses a PTY")
SECRETS = {
    "AWS_SECRET_ACCESS_KEY": "aws-secret-457",
    "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE457",
    "AWS_SESSION_TOKEN": "aws-session-457",
    "OPENAI_API_KEY": "sk-test-457",
    "GITHUB_TOKEN": "ghp_test457",
    "STIRA_PROVIDER_SECRET": "provider-secret-457",
    "MY_SERVICE_PASSWORD": "password-457",
    "GOOGLE_APPLICATION_CREDENTIALS": "/x/creds-457.json",
    "UNLISTED_PLAIN_VARIABLE": "plain-457",
}


def test_the_child_environment_is_an_allowlist():
    source = {**SECRETS, "PATH": "/usr/bin:/bin", "HOME": "/home/x", "LANG": "C.UTF-8"}
    env = child_environment(source)
    assert env == {"PATH": "/usr/bin:/bin", "HOME": "/home/x", "LANG": "C.UTF-8"}
    assert set(env) <= CHILD_ENV_ALLOWED


def test_an_allowed_name_with_a_secret_value_is_dropped():
    env = child_environment({"PATH": "/bin", "TERM": "sk-ant-api03-" + "a" * 40})
    assert env == {"PATH": "/bin"}


@POSIX_ONLY
def test_an_approved_shell_command_sees_no_key_or_token(tmp_path, monkeypatch):
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    tool = tool_registry.WRITE_TOOLS["shell"]
    use = {
        "toolUseId": "t1",
        "name": "shell",
        "input": {"command": "env > env.txt", "work_dir": str(tmp_path)},
    }
    state: dict = {"agent": None}
    mark_gate_passed(state, use)

    async def collect():
        events = [event async for event in tool.stream(use, state)]
        return getattr(events[-1], "tool_result", None) or events[-1]["tool_result"]

    result = asyncio.run(collect())
    assert result["status"] == "success", result
    seen = (tmp_path / "env.txt").read_text("utf-8")
    names = {line.split("=", 1)[0] for line in seen.splitlines() if "=" in line}
    for name, value in SECRETS.items():
        assert name not in names and value not in seen, name
    assert "PATH" in names
    # Everything the child got is on the allowlist (the shell itself may add PWD/SHLVL/_).
    assert names - CHILD_ENV_ALLOWED <= {"PWD", "OLDPWD", "SHLVL", "_"}


def test_the_scoped_os_routes_exec_through_the_allowlist(monkeypatch):
    calls = []
    monkeypatch.setattr(tool_consent.os, "execvpe", lambda *a: calls.append(a))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-457")
    proxy = tool_consent._ScopedConsentOs("shell")
    proxy.execvp("/bin/sh", ["/bin/sh", "-c", "true"])
    ((file, _args, env),) = calls
    assert file == "/bin/sh" and "OPENAI_API_KEY" not in env
    assert set(env) <= CHILD_ENV_ALLOWED

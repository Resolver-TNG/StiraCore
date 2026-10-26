"""#536: the secret-path floor of reads (#457) applies to writes too.

Measured before #536 (main, the same policy): with ``write_roots=["/"]`` and
``allow_broad_roots=True``, ``WritePathPolicy.check`` allowed ``~/.ssh/authorized_keys``,
``~/.aws/credentials``, a ``.env`` and ``key.pem`` (``on_secret_floor`` was called only by
``check_read``). Now every one is ``secret_path`` / ``E_WRITE_SECRET_PATH`` whatever the
roots, with the same spellings as #243 / #457 (case, NFD, ``..``, symlinks, relative
paths, hard links), and the refusal text is the same whether the target exists or not.
"""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from strands.tools.tools import AgentTool

from stiracore.identity_paths import (
    OUTSIDE_WRITE_ROOTS_ERROR,
    WRITE_SECRET_PATH_CODE,
    WRITE_SECRET_PATH_ERROR,
    WritePathPolicy,
)
from stiracore.tools.identity_tools import IdentityProtectedTool
from stiracore.tools.tool_consent import mark_gate_passed

SECRET = "SECRET-BODY-536"


def _touch(path: Path, text: str = SECRET) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "home").resolve()
    _touch(root / ".ssh" / "authorized_keys")
    _touch(root / ".ssh" / "id_ed25519")
    _touch(root / ".aws" / "credentials")
    _touch(root / ".gnupg" / "pubring.kbx")
    _touch(root / ".config" / "gh" / "hosts.yml")
    _touch(root / ".zshrc", "export X=1\n")
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USERPROFILE", str(root))
    return root


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "ws").resolve()
    _touch(root / "notes.md", "ok")
    elsewhere = tmp_path / "server-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return root


def _broad(ws: Path, **extra) -> WritePathPolicy:
    return WritePathPolicy(base_dir=ws, write_roots=["/"], allow_broad_roots=True, **extra)


def _secret(decision) -> None:
    assert not decision.allowed, decision
    assert decision.code == "secret_path", decision
    assert decision.reason == WRITE_SECRET_PATH_ERROR
    assert decision.path is None


# --- 1. a broad root does not lift the floor ------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        ".ssh/authorized_keys",
        ".ssh/id_ed25519",
        ".ssh/new_key",
        ".aws/credentials",
        ".gnupg/gpg.conf",
        ".config/gh/hosts.yml",
    ],
)
def test_home_secrets_are_refused_with_a_root_of_slash(ws, home, relative):
    _secret(_broad(ws).check(str(home / relative)))
    _secret(_broad(ws).check(f"~/{relative}"))


@pytest.mark.parametrize("name", [".env", ".env.local", "server.pem", "tls.key", "id_rsa"])
def test_secret_names_are_refused_anywhere(ws, tmp_path, name):
    _secret(_broad(ws).check(str(ws / name)))
    _secret(_broad(ws).check(str(tmp_path / "project" / name)))
    # Also inside the narrow default root: the workspace itself.
    _secret(WritePathPolicy(base_dir=ws, write_roots=["@workspace"]).check(name))


def test_a_policy_without_an_allowlist_keeps_the_floor(ws, home):
    _secret(WritePathPolicy(base_dir=ws).check(str(home / ".ssh" / "authorized_keys")))


def test_the_floor_comes_before_the_write_roots(ws, home):
    narrow = WritePathPolicy(base_dir=ws, write_roots=["@workspace"])
    _secret(narrow.check(str(home / ".aws" / "credentials")))
    # A plain file outside the roots still says "outside".
    outside = narrow.check(str(home / ".zshrc"))
    assert outside.code == "outside_write_roots"
    assert OUTSIDE_WRITE_ROOTS_ERROR in outside.reason


def test_settings_additions_apply_to_writes(ws, home):
    _touch(home / "vault" / "token.txt")
    policy = _broad(ws, read_secret_paths=["~/vault", "*.secret"])
    _secret(policy.check(str(home / "vault" / "token.txt")))
    _secret(policy.check(str(ws / "a.secret")))
    assert policy.check(str(ws / "a.txt")).allowed


# --- 2. spellings do not get around it ------------------------------------------------


def test_case_variants_are_refused(ws, home):
    policy = _broad(ws)
    _secret(policy.check(str(home / ".SSH" / "authorized_keys")))
    _secret(policy.check(str(home / ".Aws" / "Credentials")))
    _secret(policy.check(str(ws / ".ENV")))
    _secret(policy.check(str(ws / "Server.PEM")))


def test_nfd_spellings_are_refused(ws, home):
    folder = unicodedata.normalize("NFC", "金庫")
    _touch(home / folder / "a.txt")
    nfd = unicodedata.normalize("NFD", "~/金庫/a.txt")
    policy = _broad(ws, read_secret_paths=[f"~/{folder}"])
    _secret(policy.check(nfd))
    # A combining mark in the final name.
    _secret(policy.check(str(ws / unicodedata.normalize("NFD", ".env"))))


def test_dotdot_and_relative_paths_are_refused(ws, home):
    policy = _broad(ws)
    _secret(policy.check(str(ws / ".." / "home" / ".ssh" / "authorized_keys")))
    relative = os.path.relpath(home / ".aws" / "credentials", ws)
    _secret(policy.check(relative))
    (ws / "sub").mkdir()
    _secret(policy.check("sub/../.env"))


def test_symlinks_to_secrets_are_refused(ws, home):
    policy = _broad(ws)
    (ws / "keys").symlink_to(home / ".ssh")
    (ws / "creds").symlink_to(home / ".aws" / "credentials")
    _secret(policy.check(str(ws / "keys" / "authorized_keys")))
    _secret(policy.check(str(ws / "keys" / "brand_new")))
    _secret(policy.check(str(ws / "creds")))
    _secret(policy.check("creds"))


def test_a_hard_link_of_a_secret_file_is_refused(ws, home):
    link = ws / "innocent.txt"
    os.link(home / ".aws" / "credentials", link)
    _secret(_broad(ws).check(str(link)))


def test_new_parents_on_the_floor_are_refused(ws, home):
    # makedirs would create ~/.kube/new/ — the target name alone is harmless.
    _secret(_broad(ws).check(str(home / ".kube" / "new" / "config.txt")))


def test_a_refusal_is_the_same_whether_the_target_exists(ws, home):
    policy = _broad(ws)
    there = policy.check(str(home / ".ssh" / "authorized_keys"))
    missing = policy.check(str(home / ".ssh" / "missing_file"))
    assert (there.code, there.reason) == (missing.code, missing.reason)
    assert str(home) not in there.reason and "authorized_keys" not in there.reason


# --- 3. ordinary files stay writable --------------------------------------------------


@pytest.mark.parametrize(
    "name", ["notes.md", "env.py", ".envrc.md.txt", "key.txt", "pem.md", "idea.md", "new/x.md"]
)
def test_ordinary_workspace_files_stay_writable(ws, name):
    decision = WritePathPolicy(base_dir=ws, write_roots=["@workspace"]).check(name)
    assert decision.allowed, decision
    assert decision.path == str(ws / name)


def test_ordinary_home_files_stay_writable_with_a_broad_root(ws, home):
    decision = _broad(ws).check(str(home / ".zshrc"))
    assert decision.allowed and decision.path == str(home / ".zshrc")


# --- 4. the tools and the guard -------------------------------------------------------


class FakeWriteTool(AgentTool):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name
        self.paths: list[str] = []

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> Any:
        return {"name": self._name, "description": "fake", "inputSchema": {"json": {}}}

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        path = tool_use["input"]["path"]
        self.paths.append(path)
        Path(path).write_text("injected", encoding="utf-8")
        yield {
            "tool_result": {
                "toolUseId": tool_use["toolUseId"],
                "status": "success",
                "content": [{"text": "ok"}],
            }
        }


def _run(tool: AgentTool, path: str) -> dict:
    async def collect() -> list:
        use = {
            "toolUseId": "t1",
            "name": tool.tool_name,
            "input": {"command": "create", "path": path, "file_text": "x"},
        }
        state: dict = {}
        mark_gate_passed(state, use)  # even with the gate passed (an approval)
        return [event async for event in tool.stream(use, state)]

    events = asyncio.run(collect())
    last = events[-1]
    return last["tool_result"] if isinstance(last, dict) else last.tool_result


@pytest.mark.parametrize("name", ["file_write", "editor"])
@pytest.mark.parametrize("relative", [".ssh/authorized_keys", ".aws/credentials"])
def test_file_write_and_editor_are_refused_even_when_approved(ws, home, name, relative):
    inner = FakeWriteTool(name)
    tool = IdentityProtectedTool(inner, policy=_broad(ws))
    target = home / relative
    result = _run(tool, str(target))
    assert result["status"] == "error"
    assert f"[{WRITE_SECRET_PATH_CODE}]" in result["content"][0]["text"]
    assert inner.paths == [] and target.read_text(encoding="utf-8") == SECRET


@pytest.mark.parametrize("name", ["file_write", "editor"])
def test_file_write_and_editor_still_write_ordinary_files(ws, name):
    inner = FakeWriteTool(name)
    tool = IdentityProtectedTool(inner, policy=_broad(ws))
    result = _run(tool, str(ws / "notes.md"))
    assert result["status"] == "success", result
    assert (ws / "notes.md").read_text(encoding="utf-8") == "injected"


def test_the_guard_refuses_at_every_level(ws, home):
    from stiracore.core import NeuralState, StiraConfig, StiraEngine
    from stiracore.middleware.identity_guard import IdentityGuard

    engine = StiraEngine(state=NeuralState(stability=0.9), config=StiraConfig())
    guard = IdentityGuard(engine, path_policy=_broad(ws))
    denied = guard.check_tool_call(
        "file_write", {"path": str(home / ".ssh" / "authorized_keys")}, hitl_approved=True
    )
    assert not denied.allowed and f"[{WRITE_SECRET_PATH_CODE}]" in denied.reason
    assert guard.check_tool_call("file_write", {"path": str(ws / "a.md")}).allowed


# --- 5. diagnosis, containment, audit -------------------------------------------------


def test_the_write_failure_card_says_secret_path_without_a_retry():
    from stiracore.diagnostics.write_errors import classify_write_failure

    failure = classify_write_failure("file_write", {"path": "/x/.env"}, WRITE_SECRET_PATH_ERROR)
    assert (failure.category, failure.remedy) == ("identity_protected", "secret_path")
    assert not failure.retryable
    assert "layer:secret_path" in failure.evidence


def test_the_refusal_is_a_g1_signal():
    from stiracore.core.containment import CODE_SIGNALS
    from stiracore.middleware.containment_observer import result_codes

    result = {"status": "error", "content": [{"text": WRITE_SECRET_PATH_ERROR}]}
    assert result_codes("file_write", result) == [WRITE_SECRET_PATH_CODE]
    assert CODE_SIGNALS[WRITE_SECRET_PATH_CODE] == "G1"
    # Not counted from a successful result that quotes the text.
    assert result_codes("file_write", {**result, "status": "success"}) == []


def test_the_hook_writes_one_audit_row_with_a_hash_and_the_code(ws, home, monkeypatch):
    from stiracore.diagnostics.write_errors import WriteFailureHook
    from stiracore.tools import file_tools

    rows: list[dict] = []
    monkeypatch.setitem(file_tools._WRITE_AUDIT, "record", lambda **row: rows.append(row))

    event = SimpleNamespace(
        tool_use={
            "toolUseId": "t1",
            "name": "file_write",
            "input": {"path": "~/.ssh/authorized_keys"},
        },
        result={"status": "error", "content": [{"text": WRITE_SECRET_PATH_ERROR}]},
    )
    WriteFailureHook(base_provider=lambda: ws).after_tool(event)
    assert len(rows) == 1
    row = rows[0]
    assert row["tool_name"] == "file_write" and row["reason_code"] == WRITE_SECRET_PATH_CODE
    assert re.fullmatch(r"path:[0-9a-f]{64}", row["target"])
    assert row["target"] == file_tools.path_ref(str(home / ".ssh" / "authorized_keys"))
    assert "authorized_keys" not in repr(row)
    # An ordinary refusal (outside the roots) is no audit row.
    other = SimpleNamespace(tool_use=event.tool_use, result=None)
    other.result = {"status": "error", "content": [{"text": OUTSIDE_WRITE_ROOTS_ERROR}]}
    WriteFailureHook(base_provider=lambda: ws).after_tool(other)
    assert len(rows) == 1


def test_an_audit_failure_never_breaks_the_hook(ws, monkeypatch):
    from stiracore.diagnostics.write_errors import WriteFailureHook
    from stiracore.tools import file_tools

    def boom(**_row):
        raise RuntimeError("store down")

    monkeypatch.setitem(file_tools._WRITE_AUDIT, "record", boom)

    event = SimpleNamespace(
        tool_use={"toolUseId": "t1", "name": "file_write", "input": {"path": ".env"}},
        result={"status": "error", "content": [{"text": WRITE_SECRET_PATH_ERROR}]},
    )
    WriteFailureHook(base_provider=lambda: ws).after_tool(event)
    assert event.result["status"] == "error"


def test_the_audit_chain_row_keeps_only_codes_and_hashes(tmp_path):
    from stiracore.observability.audit_chain import AuditChain

    emitted: list = []

    class Store:
        def emit(self, event):
            emitted.append(event)
            return "e1"

    chain = AuditChain(Store())
    chain.record_write_refusal(
        tool_name="editor", target="path:" + "a" * 64, reason_code=WRITE_SECRET_PATH_CODE
    )
    chain.record_write_refusal(tool_name="editor", target="/home/x/.ssh", reason_code="nope")
    first, second = (event.attributes for event in emitted)
    assert emitted[0].event_type == "audit.write.denied"
    assert first["target"] == "path:" + "a" * 64
    assert first["reason_code"] == WRITE_SECRET_PATH_CODE
    assert second["target"] == "invalid" and second["reason_code"] == "unknown"


# --- 6. mutation: without the floor the same write would pass -------------------------


def test_without_the_floor_the_same_write_would_be_allowed(ws, home, monkeypatch):
    policy = _broad(ws)
    target = str(home / ".ssh" / "authorized_keys")
    _secret(policy.check(target))
    monkeypatch.setattr(WritePathPolicy, "on_secret_floor", lambda self, *a, **k: False)
    assert policy.check(target).allowed


# --- 7. settings: adding a write root is confirmed and recorded ----------------------


def _hub_client(tmp_path):
    pytest.importorskip("flask")
    from tests.test_settings_hub import _client, _store

    store = _store(tmp_path / "config")
    return store, _client(store)


def _hub_save(client, roots, **extra):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(
        "/api/config/hub",
        json={"changes": {"write": {"roots": roots}}, "base_version": version, **extra},
    )


def test_widening_write_roots_needs_a_confirmation_and_is_recorded(tmp_path):
    from stiracore.config.models import AgentConfig as Loaded

    store, client = _hub_client(tmp_path)
    assert client.get("/api/config/hub").get_json()["values"]["write"] == {"roots": ["@workspace"]}
    refused = _hub_save(client, ["@workspace", "/tmp"])
    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["needs_confirmation"] == ["write_roots_widened"]
    assert not store.agent_path.exists()
    validated = client.post(
        "/api/config/hub/validate", json={"changes": {"write": {"roots": ["@workspace", "/tmp"]}}}
    ).get_json()
    assert validated["needs_confirmation"] == ["write_roots_widened"]
    saved = _hub_save(client, ["@workspace", "/tmp"], acknowledged_dangers=["write_roots_widened"])
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["saved"] == ["write"]
    assert Loaded.load(store.agent_path).write_roots == ["@workspace", "/tmp"]
    entry = client.get("/api/config/history").get_json()["entries"][0]
    assert entry["changes"] == [{"field": "write.roots", "added": ["/tmp"], "removed": []}]
    # Narrowing needs no confirmation and is recorded too.
    narrowed = _hub_save(client, ["@workspace"])
    assert narrowed.status_code == 200, narrowed.get_json()
    entry = client.get("/api/config/history").get_json()["entries"][0]
    assert entry["changes"] == [{"field": "write.roots", "added": [], "removed": ["/tmp"]}]


def test_the_settings_page_mirrors_the_write_warning():
    root = Path(__file__).resolve().parents[1]
    script = (root / "dashboard" / "static" / "js" / "settings_hub.js").read_text("utf-8")
    assert 'found.push("write_roots_widened")' in script
    assert 'write_roots_widened: () => t("hub.danger_write_roots_widened")' in script
    import json

    for lang in ("en", "ja"):
        catalog = json.loads(
            (root / "dashboard" / "static" / "i18n" / f"catalog.{lang}.json").read_text("utf-8")
        )
        text = catalog["hub.danger_write_roots_widened"]
        assert "http" not in text
        assert catalog["diagnostics.fix_secret_path"]

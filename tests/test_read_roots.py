"""#457: read roots and the secret-path floor for every file-reading tool.

Measured before #457 (main 4ad82b8, the same tool functions called directly): with the
server's policy ``file_read`` read ``/etc/hosts``, ``~/.ssh/known_hosts``, ``~/.zshrc``
and ``~/.aws/config`` (success), and so did ``view_image`` / ``image_reader`` /
``editor view`` / ``lsp_*`` for files outside the data directory: the #312 floor covered
Stira's own data directory only. Now:

* reads stay inside ``read_roots`` (default: the workspace only) —
  ``E_READ_OUTSIDE_ROOTS``;
* the secret-path floor (``~/.ssh``, ``~/.aws``, ``.env``, ``*.pem``, ``id_*`` …) applies
  even when ``read_roots`` is ``/`` — ``E_READ_SECRET_PATH``;
* the same spellings as #243 do not get around either (case, NFD, ``..``, symlinks,
  relative paths, hard links);
* a refusal is the same whether the file exists or not, and is one audit row (the tool,
  a keyed hash of the path, the code).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import unicodedata
from pathlib import Path

import pytest
from PIL import Image

from stiracore.config.data_dir import build_policy, readable_names
from stiracore.config.models import AgentConfig
from stiracore.identity_paths import (
    OUTSIDE_READ_ROOTS_ERROR,
    READ_FLOOR_ERROR,
    SECRET_PATH_ERROR,
    WritePathPolicy,
)
from stiracore.tools import file_tools, tool_registry
from stiracore.tools.file_tools import DIAGNOSIS_MARKER, read_floor

SECRET = "SECRET-BODY-457"
TOOLS = ("file_read", "view_image", "image_reader", "editor", "lsp_diagnostics", "lsp_hover")


def _touch(path: Path, text: str = SECRET) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home directory with the usual secrets (never the real one)."""
    root = (tmp_path / "home").resolve()
    _touch(root / ".ssh" / "id_rsa")
    _touch(root / ".ssh" / "known_hosts")
    _touch(root / ".aws" / "credentials")
    _touch(root / ".aws" / "config")
    _touch(root / ".config" / "gh" / "hosts.yml")
    _touch(root / ".gnupg" / "pubring.kbx")
    _touch(root / ".zshrc", "export X=1\n")
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("USERPROFILE", str(root))
    return root


@pytest.fixture()
def data_dir(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = (tmp_path / "stira").resolve()
    for name in readable_names():
        (root / name).mkdir(parents=True)
    _touch(root / "workspace" / "notes.md", "readable")
    _png(root / "workspace" / "ok.png")
    _touch(root / "workspace" / "mod.py", "X = 1\n")
    elsewhere = tmp_path / "server-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return root


@pytest.fixture()
def outside(tmp_path: Path) -> Path:
    """A project directory next to the data directory (not in the default roots)."""
    root = (tmp_path / "project").resolve()
    _touch(root / "README.md", SECRET)
    _png(root / "pic.png")
    _touch(root / "mod.py", f"X = '{SECRET}'\n")
    return root


def _tools(data_dir: Path, **config) -> dict:
    policy, _floor = build_policy(AgentConfig(**config), data_dir)
    return {tool.tool_name: tool for tool in tool_registry.get_tools(write_policy=policy)}


def _run(tool, arguments: dict) -> dict:
    use = {"toolUseId": "u1", "name": tool.tool_name, "input": arguments}

    async def collect():
        events = [event async for event in tool.stream(use, {"agent": None})]
        last = events[-1]
        return last["tool_result"] if isinstance(last, dict) else last.tool_result

    return asyncio.run(collect())


def _arguments(name: str, path: str) -> dict:
    if name in ("file_read", "view_image"):
        return {"path": path}
    if name == "image_reader":
        return {"image_path": path}
    if name == "editor":
        return {"command": "view", "path": path}
    if name == "lsp_hover":
        return {"file_path": path, "line": 1, "col": 1}
    return {"file_path": path}


def _diagnosis(result: dict) -> dict | None:
    for block in result["content"]:
        text = block.get("text", "")
        if text.startswith(DIAGNOSIS_MARKER + " "):
            return json.loads(text[len(DIAGNOSIS_MARKER) + 1 :])
    return None


def _refused(result: dict, error: str, reason: str, code: str) -> dict | None:
    assert result["status"] == "error", result
    assert SECRET not in json.dumps(result, default=str)
    assert not any("image" in block for block in result["content"])
    text = result["content"][0]["text"]
    assert error in text and code in text, text
    found = _diagnosis(result)
    if found is not None:
        assert found["reason"] == reason and found["code"] == code
        assert not {"exists", "is_file", "is_directory"} & set(found)
    return found


def _outside(result: dict) -> dict | None:
    return _refused(result, OUTSIDE_READ_ROOTS_ERROR, "outside_read_roots", "E_READ_OUTSIDE_ROOTS")


def _secret(result: dict) -> dict | None:
    return _refused(result, SECRET_PATH_ERROR, "secret_path", "E_READ_SECRET_PATH")


# --- 1. the workspace stays readable ------------------------------------------------------


def test_workspace_files_stay_readable_with_every_read_tool(data_dir):
    tools = _tools(data_dir)
    assert _run(tools["file_read"], {"path": "notes.md"})["content"][0]["text"] == "readable"
    assert _run(tools["view_image"], {"path": "ok.png"})["status"] == "success"
    assert _run(tools["image_reader"], {"image_path": "ok.png"})["status"] == "success"
    viewed = _run(tools["editor"], {"command": "view", "path": "notes.md"})
    assert viewed["status"] == "success" and "readable" in json.dumps(viewed)
    listing = _run(tools["editor"], {"command": "view", "path": "."})
    assert listing["status"] == "success" and "notes.md" in json.dumps(listing)
    module = str(data_dir / "workspace" / "mod.py")
    assert _run(tools["lsp_diagnostics"], {"file_path": module})["status"] == "success"
    assert _run(tools["lsp_hover"], _arguments("lsp_hover", module))["status"] == "success"


def test_the_episodic_layer_and_the_stash_stay_readable(data_dir):
    tools = _tools(data_dir)
    for name in readable_names():
        _touch(data_dir / name / "x.md", "readable")
        result = _run(tools["file_read"], {"path": str(data_dir / name / "x.md")})
        assert result["status"] == "success", (name, result)


# --- 2. outside the read roots ------------------------------------------------------------


@pytest.mark.parametrize("name", TOOLS)
def test_outside_the_workspace_is_refused_by_every_read_tool(data_dir, outside, name):
    tools = _tools(data_dir)
    target = {"view_image": "pic.png", "image_reader": "pic.png"}.get(name, "mod.py")
    if name in ("file_read", "editor"):
        target = "README.md"
    _outside(_run(tools[name], _arguments(name, str(outside / target))))


def test_etc_hosts_and_home_files_are_refused_by_default(data_dir, home):
    tools = _tools(data_dir)
    for path in ("/etc/hosts", "~/.zshrc", str(home / ".zshrc"), "../../home/.zshrc"):
        _outside(_run(tools["file_read"], {"path": path}))


def test_adding_a_read_root_makes_it_readable(data_dir, outside):
    tools = _tools(data_dir, read_roots=["@workspace", str(outside)])
    assert _run(tools["file_read"], {"path": str(outside / "README.md")})["status"] == "success"
    assert _run(tools["view_image"], {"path": str(outside / "pic.png")})["status"] == "success"
    viewed = _run(tools["editor"], {"command": "view", "path": str(outside / "README.md")})
    assert viewed["status"] == "success"
    assert _run(tools["file_read"], {"path": "notes.md"})["status"] == "success"
    # /etc/hosts is still outside these roots.
    _outside(_run(tools["file_read"], {"path": "/etc/hosts"}))


def test_a_root_of_slash_reads_etc_hosts(data_dir):
    tools = _tools(data_dir, read_roots=["/"])
    assert _run(tools["file_read"], {"path": "/etc/hosts"})["status"] == "success"


def test_without_the_workspace_token_the_workspace_is_not_readable(data_dir, outside):
    tools = _tools(data_dir, read_roots=[str(outside)])
    _outside(_run(tools["file_read"], {"path": "notes.md"}))
    assert _run(tools["file_read"], {"path": str(outside / "README.md")})["status"] == "success"


# --- 3. the secret-path floor, even with "/" ----------------------------------------------


@pytest.mark.parametrize(
    "relative",
    [
        ".ssh/id_rsa",
        ".ssh/known_hosts",
        ".aws/credentials",
        ".aws/config",
        ".config/gh/hosts.yml",
        ".gnupg/pubring.kbx",
    ],
)
def test_home_secrets_are_refused_with_a_root_of_slash(data_dir, home, relative):
    tools = _tools(data_dir, read_roots=["/"])
    for path in (str(home / relative), f"~/{relative}"):
        _secret(_run(tools["file_read"], {"path": path}))


@pytest.mark.parametrize("name", [".env", ".env.local", "server.pem", "id_ed25519", "c.p12"])
def test_secret_names_in_the_workspace_are_refused(data_dir, name):
    _touch(data_dir / "workspace" / name)
    for config in ({}, {"read_roots": ["/"]}):
        tools = _tools(data_dir, **config)
        _secret(_run(tools["file_read"], {"path": name}))
        _secret(_run(tools["editor"], {"command": "view", "path": name}))


def test_ordinary_names_close_to_the_secret_names_stay_readable(data_dir):
    tools = _tools(data_dir)
    for name in ("id_utils.py", "env.md", "environment.txt", "pem_notes.md", "keys.md"):
        _touch(data_dir / "workspace" / name, "readable")
        assert _run(tools["file_read"], {"path": name})["status"] == "success", name


def test_secret_paths_are_refused_by_every_read_tool(data_dir, home):
    _png(home / ".ssh" / "key.png")
    _touch(home / ".ssh" / "hook.py", f"X = '{SECRET}'\n")
    tools = _tools(data_dir, read_roots=["/"])
    for name in TOOLS:
        target = "key.png" if name in ("view_image", "image_reader") else "hook.py"
        _secret(_run(tools[name], _arguments(name, str(home / ".ssh" / target))))


def test_etc_shadow_and_the_keychain_are_on_the_floor():
    policy = WritePathPolicy(read_roots=["/"])
    for path in ("/etc/shadow", "/etc/master.passwd", "/etc/sudoers"):
        assert policy.check_read(path).code == "secret_path", path
    home = Path.home()
    for relative in ("Library/Keychains/login.keychain-db", "Library/Safari/History.db"):
        assert policy.check_read(str(home / relative)).code == "secret_path", relative
    assert policy.check_read(str(home / "x" / "vault.kdbx")).code == "secret_path"


def test_settings_add_secret_paths_but_never_remove_the_defaults(data_dir, home):
    _touch(home / "vault" / "a.txt")
    _touch(data_dir / "workspace" / "token.json")
    tools = _tools(
        data_dir, read_roots=["/"], read_secret_paths=["~/vault", "token.json", "*.secret"]
    )
    _secret(_run(tools["file_read"], {"path": str(home / "vault" / "a.txt")}))
    _secret(_run(tools["file_read"], {"path": "token.json"}))
    _secret(_run(tools["file_read"], {"path": "x.SECRET"}))
    _secret(_run(tools["file_read"], {"path": str(home / ".ssh" / "id_rsa")}))


def test_the_data_directory_floor_comes_first(data_dir):
    _touch(data_dir / "tool-config.json")
    tools = _tools(data_dir, read_roots=["/"])
    _refused(
        _run(tools["file_read"], {"path": "../tool-config.json"}),
        READ_FLOOR_ERROR,
        "floor",
        "E_READ_FLOOR",
    )


# --- 4. the #243 spellings ----------------------------------------------------------------


def test_case_variants_are_refused(data_dir, home):
    tools = _tools(data_dir, read_roots=["/"])
    for path in ("~/.SSH/id_rsa", "~/.ssh/ID_RSA", "~/.Aws/Credentials"):
        _secret(_run(tools["file_read"], {"path": path}))
    _touch(data_dir / "workspace" / ".ENV")
    _secret(_run(tools["file_read"], {"path": ".ENV"}))
    # A case variant of the home directory itself.
    upper = Path(str(home).upper()) / ".ssh" / "id_rsa"
    _secret(_run(tools["file_read"], {"path": str(upper)}))


def test_nfd_spellings_are_refused(data_dir, home):
    secret_dir = home / "秘密のデータ"
    _touch(secret_dir / "a.txt")
    tools = _tools(data_dir, read_roots=["/"], read_secret_paths=[str(secret_dir)])
    nfd = unicodedata.normalize("NFD", str(secret_dir / "a.txt"))
    assert nfd != str(secret_dir / "a.txt")
    _secret(_run(tools["file_read"], {"path": nfd}))
    # Outside the roots in NFD: still outside.
    outside_dir = data_dir.parent / "ダウンロード"
    _touch(outside_dir / "b.txt")
    tools = _tools(data_dir)
    _outside(_run(tools["file_read"], {"path": unicodedata.normalize("NFD", str(outside_dir))}))


def test_dotdot_and_relative_paths_are_refused(data_dir, home, outside):
    tools = _tools(data_dir)
    _outside(_run(tools["file_read"], {"path": "../../project/README.md"}))
    _outside(_run(tools["file_read"], {"path": "missing/../../../project/README.md"}))
    _outside(_run(tools["file_read"], {"path": str(data_dir / "workspace" / ".." / "..")}))
    tools = _tools(data_dir, read_roots=["/"])
    relative = os.path.relpath(home / ".ssh" / "id_rsa", data_dir / "workspace")
    _secret(_run(tools["file_read"], {"path": relative}))
    _secret(_run(tools["file_read"], {"path": str(home / "x" / ".." / ".ssh" / "id_rsa")}))


@pytest.fixture()
def own_workspace(tmp_path: Path) -> Path:
    """An explicit ``workspace_dir`` outside the data directory (inside it, any symlink
    and any file with a second name is already on the #312 floor)."""
    root = (tmp_path / "work").resolve()
    _touch(root / "notes.md", "readable")
    return root


def test_symlinks_out_of_the_workspace_are_refused(data_dir, home, outside, own_workspace):
    workspace = own_workspace
    (workspace / "readme").symlink_to(outside / "README.md")
    (workspace / "proj").symlink_to(outside, target_is_directory=True)
    (workspace / "key.txt").symlink_to(home / ".ssh" / "id_rsa")
    (workspace / "dotssh").symlink_to(home / ".ssh", target_is_directory=True)
    (workspace / "inner").symlink_to(workspace / "notes.md")
    tools = _tools(data_dir, workspace_dir=str(workspace))
    assert _run(tools["file_read"], {"path": "inner"})["content"][0]["text"] == "readable"
    _outside(_run(tools["file_read"], {"path": "readme"}))
    _outside(_run(tools["file_read"], {"path": "proj/README.md"}))
    _outside(_run(tools["view_image"], {"path": "proj/pic.png"}))
    _outside(_run(tools["editor"], {"command": "view", "path": "proj"}))
    tools = _tools(data_dir, workspace_dir=str(workspace), read_roots=["/"])
    _secret(_run(tools["file_read"], {"path": "key.txt"}))
    _secret(_run(tools["file_read"], {"path": "dotssh/known_hosts"}))
    # A symlinked home secret directory: the resolved spelling is refused too.
    alias = home / "plain"
    alias.symlink_to(home / ".ssh", target_is_directory=True)
    _secret(_run(tools["file_read"], {"path": str(alias / "known_hosts")}))


def test_a_hard_link_of_a_secret_file_is_refused(data_dir, home, own_workspace):
    os.link(home / ".ssh" / "id_rsa", own_workspace / "harmless.txt")
    os.link(home / ".zshrc", own_workspace / "second-name.txt")  # not a secret
    tools = _tools(data_dir, workspace_dir=str(own_workspace))
    _secret(_run(tools["file_read"], {"path": "harmless.txt"}))
    assert _run(tools["file_read"], {"path": "second-name.txt"})["status"] == "success"


def test_the_editor_listing_hides_secret_names(data_dir):
    _touch(data_dir / "workspace" / ".env")
    _touch(data_dir / "workspace" / "id_rsa")
    tools = _tools(data_dir)
    listing = json.dumps(_run(tools["editor"], {"command": "view", "path": "."}))
    assert "notes.md" in listing
    assert ".env" not in listing and "id_rsa" not in listing


# --- 5. refused and missing look the same ---------------------------------------------------


@pytest.mark.parametrize("name", ["file_read", "view_image", "editor", "lsp_diagnostics"])
def test_a_refusal_is_the_same_whether_the_file_exists(data_dir, home, outside, name):
    cases = [
        ({}, outside / "README.md"),
        ({"read_roots": ["/"]}, home / ".ssh" / "id_rsa"),
        ({"read_roots": ["/"]}, data_dir / "workspace" / ".env"),
    ]
    for config, path in cases:
        tools = _tools(data_dir, **config)
        _touch(path)
        present = _run(tools[name], _arguments(name, str(path)))
        path.unlink()
        missing = _run(tools[name], _arguments(name, str(path)))
        assert present == missing, (path, present, missing)
        assert present["status"] == "error"


def test_a_missing_file_inside_the_roots_still_says_not_found(data_dir):
    result = _run(_tools(data_dir)["file_read"], {"path": "nope.md"})
    assert _diagnosis(result)["reason"] == "path_not_found"


# --- 6. audit rows ------------------------------------------------------------------------


def test_every_refusal_is_one_audit_row_with_a_hash_and_a_code(data_dir, home, outside):
    from stiracore.observability.audit_chain import AuditChain
    from stiracore.observability.event_store import SQLiteEventStore

    store = SQLiteEventStore(data_dir.parent / "events-test.db")
    file_tools.set_read_audit(AuditChain(store).record_read_refusal)
    try:
        tools = _tools(data_dir)
        _outside(_run(tools["file_read"], {"path": str(outside / "README.md")}))
        _outside(_run(tools["editor"], {"command": "view", "path": str(outside)}))
        tools = _tools(data_dir, read_roots=["/"])
        _secret(_run(tools["view_image"], {"path": "~/.ssh/id_rsa"}))
        assert _run(tools["file_read"], {"path": "notes.md"})["status"] == "success"
        rows = store.get_events(limit=50, event_type="audit.read.denied")
    finally:
        file_tools.set_read_audit(None)
        store.close()
    assert [(r.event_type, r.tool_name, r.attributes["reason_code"]) for r in rows] == [
        ("audit.read.denied", "file_read", "E_READ_OUTSIDE_ROOTS"),
        ("audit.read.denied", "editor", "E_READ_OUTSIDE_ROOTS"),
        ("audit.read.denied", "view_image", "E_READ_SECRET_PATH"),
    ]
    for row in rows:
        assert set(row.attributes) == {"target", "reason_code"}
        assert row.attributes["target"].startswith("path:")
        assert len(row.attributes["target"]) == len("path:") + 64
        dumped = json.dumps(row.attributes) + str(row.message or "")
        assert "README" not in dumped and ".ssh" not in dumped and "project" not in dumped


def test_an_audit_failure_never_opens_the_read(data_dir, outside):
    def broken(**_fields):
        raise RuntimeError("store down")

    file_tools.set_read_audit(broken)
    try:
        _outside(_run(_tools(data_dir)["file_read"], {"path": str(outside / "README.md")}))
    finally:
        file_tools.set_read_audit(None)


# --- the active floor, nesting, library use -----------------------------------------------


def test_the_opener_checks_read_roots_inside_the_tool(data_dir, outside):
    policy, _ = build_policy(AgentConfig(), data_dir)
    with read_floor(policy), pytest.raises(file_tools.FileReadError) as caught:
        file_tools.open_for_read(str(outside / "README.md"))
    assert caught.value.reason == "outside_read_roots"


def test_a_delegate_keeps_the_read_roots_and_the_secret_floor(data_dir, outside):
    policy, _ = build_policy(AgentConfig(read_secret_paths=["*.secret"]), data_dir)
    child = policy.narrow(None)
    assert child.read_roots == policy.read_roots
    assert child.read_secret_paths == ("*.secret",)
    assert child.check_read(str(outside / "README.md")).code == "outside_read_roots"
    assert child.with_floor(["x.md"]).read_roots == policy.read_roots


def test_a_policy_without_read_roots_keeps_the_secret_floor(home, outside):
    policy = WritePathPolicy()
    assert policy.check_read(str(outside / "README.md")).allowed
    assert policy.check_read(str(home / ".ssh" / "id_rsa")).code == "secret_path"


def test_the_bare_tool_without_a_policy_is_unchanged(home):
    bare = {t.tool_name: t for t in tool_registry.get_tools()}["file_read"]
    assert _run(bare, {"path": str(home / ".zshrc")})["status"] == "success"


# --- mutation checks: the refusals come from the rules, not from an incidental error ------


def test_without_the_secret_floor_the_same_read_would_succeed(data_dir, home, monkeypatch):
    tools = _tools(data_dir, read_roots=["/"])
    _secret(_run(tools["file_read"], {"path": "~/.ssh/id_rsa"}))
    monkeypatch.setattr(WritePathPolicy, "on_secret_floor", lambda self, *a, **k: False)
    assert _run(tools["file_read"], {"path": "~/.ssh/id_rsa"})["content"][0]["text"] == SECRET


def test_without_the_roots_check_the_same_read_would_succeed(data_dir, outside, monkeypatch):
    tools = _tools(data_dir)
    _outside(_run(tools["file_read"], {"path": str(outside / "README.md")}))
    monkeypatch.setattr(WritePathPolicy, "_within", staticmethod(lambda *a: True))
    result = _run(tools["file_read"], {"path": str(outside / "README.md")})
    assert result["content"][0]["text"] == SECRET


# --- 7. the settings hub: confirmed and recorded --------------------------------------------


def _hub_client(tmp_path):
    pytest.importorskip("flask")
    from tests.test_settings_hub import _client, _store

    store = _store(tmp_path / "config")
    return store, _client(store)


def _hub_save(client, roots, **extra):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(
        "/api/config/hub",
        json={"changes": {"read": {"roots": roots}}, "base_version": version, **extra},
    )


def test_widening_read_roots_needs_a_confirmation_and_is_recorded(tmp_path):
    from stiracore.config.models import AgentConfig as Loaded

    store, client = _hub_client(tmp_path)
    state = client.get("/api/config/hub").get_json()
    assert state["values"]["read"] == {"roots": ["@workspace"]}
    assert state["constraints"]["read_scopes"]["everywhere"] == ["/"]
    refused = _hub_save(client, ["/"])
    assert refused.status_code == 400, refused.get_json()
    assert refused.get_json()["needs_confirmation"] == ["read_roots_widened"]
    assert not store.agent_path.exists()  # nothing saved
    validated = client.post(
        "/api/config/hub/validate", json={"changes": {"read": {"roots": ["/"]}}}
    ).get_json()
    assert validated["needs_confirmation"] == ["read_roots_widened"]
    saved = _hub_save(client, ["/"], acknowledged_dangers=["read_roots_widened"])
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()["saved"] == ["read"]
    assert Loaded.load(store.agent_path).read_roots == ["/"]
    entry = client.get("/api/config/history").get_json()["entries"][0]
    assert entry["changes"] == [{"field": "read.roots", "added": ["/"], "removed": ["@workspace"]}]
    assert entry["before"] == {"read": {"roots": ["@workspace"]}}
    # Every added root asks again, even when another root was added before.
    again = _hub_save(client, ["/", "~/projects"])
    assert again.get_json()["needs_confirmation"] == ["read_roots_widened"]


def test_narrowing_read_roots_needs_no_confirmation_and_is_recorded(tmp_path):
    _store, client = _hub_client(tmp_path)
    _hub_save(client, ["@workspace", "/tmp"], acknowledged_dangers=["read_roots_widened"])
    narrowed = _hub_save(client, ["@workspace"])
    assert narrowed.status_code == 200, narrowed.get_json()
    entry = client.get("/api/config/history").get_json()["entries"][0]
    assert entry["changes"] == [{"field": "read.roots", "added": [], "removed": ["/tmp"]}]


def test_invalid_read_roots_are_refused(tmp_path):
    _store, client = _hub_client(tmp_path)
    for roots, code in (([], "empty"), (["relative/dir"], "invalid_root"), ("x", "not_list")):
        result = _hub_save(client, roots, acknowledged_dangers=["read_roots_widened"])
        assert result.status_code == 400
        assert result.get_json()["errors"][0] == {
            "field": "read.roots",
            "code": code,
            "params": {"roots": roots} if code == "invalid_root" else {},
        }


def test_a_rollback_of_a_narrowing_is_a_widening_again(tmp_path):
    _store, client = _hub_client(tmp_path)
    _hub_save(client, ["@workspace", "/tmp"], acknowledged_dangers=["read_roots_widened"])
    _hub_save(client, ["@workspace"])
    entry = client.get("/api/config/history").get_json()["entries"][0]
    version = client.get("/api/config/hub").get_json()["version"]
    url = f"/api/config/history/{entry['id']}/rollback"
    refused = client.post(url, json={"base_version": version})
    assert refused.status_code == 400
    assert refused.get_json()["needs_confirmation"] == ["read_roots_widened"]
    ok = client.post(
        url, json={"base_version": version, "acknowledged_dangers": ["read_roots_widened"]}
    )
    assert ok.status_code == 200, ok.get_json()


# --- 8. The chat server wires it ------------------------------------------------------------


def test_the_server_enforces_read_roots_and_audits_refusals(tmp_path, home, monkeypatch):
    pytest.importorskip("flask")
    from dashboard import stira_server
    from tests.test_data_dir_floor import _stira_app

    monkeypatch.setattr(file_tools, "_READ_AUDIT", {"record": None})
    captured, data_dir = _stira_app(tmp_path, monkeypatch)
    assert file_tools._READ_AUDIT["record"] is not None  # AuditChain.record_read_refusal
    policy = captured["write_policy"]
    assert policy.read_roots is not None
    tools = {t.tool_name: t for t in stira_server._stira_tools((), set(), write_policy=policy)}
    _outside(_run(tools["file_read"], {"path": "/etc/hosts"}))
    _outside(_run(tools["file_read"], {"path": str(home / ".zshrc")}))
    _touch(data_dir / "workspace" / "mine.md", "mine")
    assert _run(tools["file_read"], {"path": "mine.md"})["content"][0]["text"] == "mine"


def test_the_server_reads_its_saved_read_roots(tmp_path, home, monkeypatch):
    pytest.importorskip("flask")
    from tests.test_data_dir_floor import _stira_app

    monkeypatch.setattr(file_tools, "_READ_AUDIT", {"record": None})
    captured, _data_dir = _stira_app(
        tmp_path, monkeypatch, agent_config={"read_roots": ["/"], "read_secret_paths": ["*.x"]}
    )
    policy = captured["write_policy"]
    assert policy.check_read("/etc/hosts").allowed
    assert policy.check_read(str(home / ".ssh" / "id_rsa")).code == "secret_path"
    assert policy.check_read(str(home / "a.x")).code == "secret_path"


def test_a_policy_without_a_data_directory_is_active_inside_the_tool(home, outside):
    # read_floor() activates every WritePathPolicy (#457), not only one with a data
    # directory (#312): the opener refuses inside the step too.
    policy = WritePathPolicy(read_roots=[str(outside)])
    with read_floor(policy):
        assert file_tools.active_read_floor() is not None
        with pytest.raises(file_tools.FileReadError) as caught:
            file_tools.open_for_read(str(home / ".ssh" / "id_rsa"))
        assert caught.value.reason == "secret_path"
        with pytest.raises(file_tools.FileReadError) as caught:
            file_tools.open_for_read(str(home / ".zshrc"))
        assert caught.value.reason == "outside_read_roots"

"""#270: an allowlist entry holding a secret is never written again, and every tool-rule
save path takes its loosening decision once, from the same function.

(a) A secret-looking command already in the saved rules (from before #190's check) is
not re-saved by an unrelated change: every save that writes the tool rules is refused
until the entry is removed (the removal itself saves), a pre-#190 file drops it on
migration, and the settings hub tells the page which saved entries to remove (masked).
(b) The legacy, new and hub APIs compute the loosening and the names to type once per
save, through ``settings_hub.tool_rule_change``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("flask")

from flask import Flask

from stiracore.observability import config_api
from stiracore.observability import settings_hub as hub
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.tools import tool_policy as tp
from stiracore.tools import tool_registry
from stiracore.tools.tool_config import SecretInAllowlist, ToolConfig, _rules_digest

SECRET = "curl -H Authorization:Bearer " + "ghp_" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp9Oo8"
MARK = b"Zz9Yy8Xx7Ww6"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv(tool_registry.EXEC_ENV_VAR, raising=False)
    monkeypatch.delenv("STIRA_TOOL_RULES_ANCHOR_DIR", raising=False)


def _names() -> list[str]:
    return sorted(set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS))


def _some(category: str) -> str:
    return next(n for n in _names() if tp.category_of(n) == category)


def _client(store, level="normal"):
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, lambda: level))
    return app.test_client()


def _head(store) -> Path:
    return Path(str(store.tool_path) + ".head")


def _store_with_saved_secret(tmp_path):
    """Rules accepted by an older install that still hold a secret-looking command."""
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    read = _some("read")
    ToolConfig(
        mode="custom", enabled_tools={read}, shell_allowlist=["ls"], path=store.tool_path
    ).save()
    data = json.loads(Path(store.tool_path).read_text(encoding="utf-8"))
    data["shell_allowlist"] = ["ls", SECRET]
    Path(store.tool_path).write_text(json.dumps(data), encoding="utf-8")
    head = json.loads(_head(store).read_text(encoding="utf-8"))
    rules = ToolConfig(
        **{k: data[k] for k in ("mode", "enabled_tools", "shell_allowlist", "ask_tools")}
    ).as_dict()
    head["digest"] = _rules_digest(rules, data["generation"])
    _head(store).write_text(json.dumps(head), encoding="utf-8")
    loaded = store.load_tools()
    assert loaded.integrity_error is None and SECRET in loaded.shell_allowlist
    return store


def _hub_post(client, changes, **extra):
    version = client.get("/api/config/hub").get_json()["version"]
    return client.post(
        "/api/config/hub", json={"changes": changes, "base_version": version, **extra}
    )


def _snapshot(store) -> tuple[bytes, bytes]:
    return Path(store.tool_path).read_bytes(), _head(store).read_bytes()


# --- (a) a saved secret is never written again -----------------------------------------


def test_an_unrelated_save_on_every_path_is_refused_while_a_secret_is_saved(tmp_path):
    store = _store_with_saved_secret(tmp_path)
    client = _client(store)
    read = _some("read")
    before = _snapshot(store)
    attempts = {
        # Tightenings, which otherwise need no confirmation at all.
        "legacy_api": client.post("/api/config/tools", json={"name": read, "enabled": False}),
        "legacy_policy": client.post("/api/config/tools", json={"name": read, "policy": "ask"}),
        "tools_api": client.post("/api/tools/config", json={"ask_tools": [read]}),
    }
    for name, response in attempts.items():
        body = response.get_json()
        assert response.status_code == 400, (name, body)
        assert body["error_code"] == "secret_command_saved", (name, body)
        assert body["count"] == 1 and MARK.decode() not in json.dumps(body), name
    hub_response = _hub_post(client, {"tools": {"ask_tools": [read]}})
    assert hub_response.status_code == 400, hub_response.get_json()
    errors = hub_response.get_json()["errors"]
    assert [(e["field"], e["code"]) for e in errors] == [
        ("tools.shell_allowlist", "secret_command_saved")
    ]
    assert errors[0]["params"] == {"count": 1}
    # A preset keeps the saved allowlist: applying it would re-save the secret.
    presets = client.get("/api/config/hub").get_json()["presets"]
    safe = next(p for p in presets if p["id"] == "safe")["values"]
    preset = _hub_post(
        client, {"tools": safe["tools"]}, source="preset:safe", acknowledged_dangers=["breaker_low"]
    )
    assert preset.status_code == 400, preset.get_json()
    assert preset.get_json()["errors"][0]["code"] == "secret_command_saved"
    assert _snapshot(store) == before  # not one byte rewritten


def test_the_validation_endpoint_reports_the_saved_secret_too(tmp_path):
    store = _store_with_saved_secret(tmp_path)
    client = _client(store)
    response = client.post(
        "/api/config/hub/validate", json={"changes": {"tools": {"mode": "custom", "ask_tools": []}}}
    )
    # Unchanged tool values: nothing to refuse.
    assert response.get_json()["ok"] is True
    response = client.post(
        "/api/config/hub/validate", json={"changes": {"tools": {"ask_tools": [_some("read")]}}}
    )
    assert response.get_json()["ok"] is False
    assert response.get_json()["errors"][0]["code"] == "secret_command_saved"


def test_a_save_that_does_not_write_the_tool_rules_is_not_refused(tmp_path):
    store = _store_with_saved_secret(tmp_path)
    client = _client(store)
    before = _snapshot(store)
    response = _hub_post(client, {"classifier": {"choice": "rule"}})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["saved"] == ["classifier"]
    assert _snapshot(store) == before
    assert MARK not in Path(store.history_path).read_bytes()


def test_the_direct_save_floor_refuses_a_saved_secret(tmp_path):
    store = _store_with_saved_secret(tmp_path)
    before = _snapshot(store)
    config = store.load_tools()
    config.ask_tools = {_some("read")}
    with pytest.raises(SecretInAllowlist) as raised:
        config.save()
    assert MARK.decode() not in str(raised.value)
    assert _snapshot(store) == before


@pytest.mark.parametrize("path", ["hub", "tools_api"])
def test_removing_the_secret_saves_and_leaves_it_nowhere(tmp_path, path):
    store = _store_with_saved_secret(tmp_path)
    client = _client(store)
    if path == "hub":
        response = _hub_post(client, {"tools": {"shell_allowlist": ["ls"]}})
    else:
        response = client.post("/api/tools/config", json={"shell_allowlist": ["ls"]})
    assert response.status_code == 200, response.get_json()
    assert store.load_tools().shell_allowlist == ["ls"]
    for file in (store.tool_path, _head(store), store.history_path):
        assert MARK not in Path(file).read_bytes(), file
    # And then an unrelated change saves again.
    ok = client.post("/api/config/tools", json={"name": _some("read"), "policy": "ask"})
    assert ok.status_code == 200, ok.get_json()


def test_removing_the_secret_together_with_another_tightening_saves(tmp_path):
    store = _store_with_saved_secret(tmp_path)
    client = _client(store)
    read = _some("read")
    response = _hub_post(client, {"tools": {"shell_allowlist": ["ls"], "ask_tools": [read]}})
    assert response.status_code == 200, response.get_json()
    assert store.load_tools().policy_of(read) == "ask"


def test_the_hub_names_the_saved_secret_entries_masked(tmp_path):
    store = _store_with_saved_secret(tmp_path)
    state = _client(store).get("/api/config/hub").get_json()
    allowlist = state["values"]["tools"]["shell_allowlist"]
    marks = state["secret_allowlist"]
    assert [m["index"] for m in marks] == [allowlist.index(SECRET)]
    assert MARK.decode() not in marks[0]["masked"] and marks[0]["masked"].startswith("curl")


def test_the_hub_names_nothing_without_a_saved_secret(tmp_path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    ToolConfig(mode="custom", shell_allowlist=["ls"], path=store.tool_path).save()
    assert _client(store).get("/api/config/hub").get_json()["secret_allowlist"] == []


def test_a_pre_190_file_drops_a_secret_entry_on_migration(tmp_path, caplog):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    read = _some("read")
    Path(store.tool_path).write_text(
        json.dumps({"mode": "custom", "enabled_tools": [read], "shell_allowlist": ["ls", SECRET]}),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="stiracore.tools.tool_config"):
        config = store.load_tools()
    assert config.integrity_error is None
    assert config.shell_allowlist == ["ls"] and config.policy_of(read) == "always"
    assert MARK not in Path(store.tool_path).read_bytes()
    assert MARK not in _head(store).read_bytes()
    assert any("secret-looking" in r.getMessage() for r in caplog.records)
    assert all(MARK.decode() not in r.getMessage() for r in caplog.records)
    # The migration happens once; a later load reads the same rules.
    assert store.load_tools().as_dict() == config.as_dict()


# --- (b) one loosening decision per save, from one function ------------------------------


def _count_decisions(monkeypatch):
    calls = {"loosening": 0, "always": 0}
    original_loose = hub.rules_loosening
    original_always = tp.newly_always_writes

    def loose(*args, **kwargs):
        calls["loosening"] += 1
        return original_loose(*args, **kwargs)

    def always(*args, **kwargs):
        calls["always"] += 1
        return original_always(*args, **kwargs)

    # Every module reference to the shared check (a second private copy would not count).
    monkeypatch.setattr(hub, "rules_loosening", loose)
    monkeypatch.setattr(config_api, "rules_loosening", loose, raising=False)
    monkeypatch.setattr(tp, "newly_always_writes", always)
    return calls


@pytest.mark.parametrize(
    "path",
    ["legacy_enabled", "legacy_policy", "tools_api", "tools_api_unconfirmed", "hub"],
)
def test_each_save_takes_its_loosening_decision_once(tmp_path, monkeypatch, path):
    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    read, write = _some("read"), _some("write")
    ToolConfig(
        mode="custom", enabled_tools={read}, shell_allowlist=["ls"], path=store.tool_path
    ).save()
    client = _client(store)
    version = client.get("/api/config/hub").get_json()["version"]
    calls = _count_decisions(monkeypatch)
    if path == "legacy_enabled":
        response = client.post(
            "/api/config/tools",
            json={"name": write, "enabled": True, "confirm": True, "confirm_always": [write]},
        )
    elif path == "legacy_policy":
        response = client.post(
            "/api/config/tools", json={"name": write, "policy": "ask", "confirm": True}
        )
    elif path == "tools_api":
        response = client.post(
            "/api/tools/config", json={"shell_allowlist": ["ls", "pwd"], "confirm": True}
        )
    elif path == "tools_api_unconfirmed":
        response = client.post("/api/tools/config", json={"shell_allowlist": ["ls", "pwd"]})
    else:
        response = client.post(
            "/api/config/hub",
            json={
                "changes": {"tools": {"shell_allowlist": ["ls", "pwd"]}},
                "base_version": version,
                "acknowledged_dangers": ["tool_policy_loosened"],
            },
        )
    expected = 400 if path == "tools_api_unconfirmed" else 200
    assert response.status_code == expected, response.get_json()
    assert calls == {"loosening": 1, "always": 1}, calls


def test_the_apis_and_the_hub_report_the_same_decision():
    read, write = _some("read"), _some("write")
    before = {"mode": "custom", "enabled_tools": [read], "shell_allowlist": [], "ask_tools": []}
    cases = [
        {**before, "shell_allowlist": ["pwd"]},
        {**before, "mode": "trust_all"},
        {**before, "enabled_tools": [read, write]},
        {**before, "enabled_tools": [read, write], "ask_tools": [write]},
        {**before, "enabled_tools": []},
    ]
    for after in cases:
        api = config_api._rule_changes(ToolConfig(**before), ToolConfig(**after))
        shared = hub.tool_rule_change({"tools": before}, {"tools": after})
        assert api == shared, after
        assert set(shared) == {"locked", "loose", "always_writes"}


# --- (a) the page: masked, marked, removable while the shell editor is off --------------


def test_the_settings_page_masks_marks_and_removes_the_saved_secret(tmp_path):
    import os
    import shutil
    import subprocess

    from tests.ui_pages import render_pages

    def skip_or_fail(reason):
        if os.environ.get("STIRA_REQUIRE_AXE") == "1":
            pytest.fail(f"STIRA_REQUIRE_AXE=1 but the jsdom check is unavailable: {reason}")
        pytest.skip(reason)

    node = shutil.which("node")
    if node is None:
        skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "pages")
    except RuntimeError as exc:
        skip_or_fail(str(exc))
    store = _store_with_saved_secret(tmp_path)
    state = _client(store).get("/api/config/hub").get_json()
    page, hub_json = tmp_path / "settings.html", tmp_path / "hub.json"
    page.write_text(pages["settings"], encoding="utf-8")
    hub_json.write_text(json.dumps(state), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            node,
            str(root / "tests" / "a11y" / "settings_secret_allowlist_dom.cjs"),
            str(page),
            str(hub_json),
            MARK.decode(),
        ],
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        skip_or_fail(output.strip())
    assert proc.returncode == 0, (
        f"secret allowlist entry on the page (exit {proc.returncode}):\n{output}"
    )

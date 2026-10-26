"""#540: Windows portability fixes, checked by simulation (no Windows host here)."""

from __future__ import annotations

import ast
import os
import time
from pathlib import Path

import pytest

from stiracore.observability import config_api

ROOT = Path(__file__).resolve().parents[1]


def test_no_os_uname_in_tests_or_src():
    """os.uname() does not exist on Windows; at collect time it kills the run (§2)."""
    offenders = []
    for base in ("tests", "src", "dashboard", "scripts"):
        for path in (ROOT / base).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "uname"
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "os"
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == []


def test_yolo_window_collects_without_os_uname(monkeypatch):
    import importlib.util

    monkeypatch.delattr(os, "uname", raising=False)
    spec = importlib.util.spec_from_file_location("_yw540", ROOT / "tests/test_yolo_window.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # raised AttributeError before the fix


@pytest.fixture
def fresh_cache():
    config_api.clear_sokudan_cache()
    yield
    config_api.clear_sokudan_cache()


def test_sokudan_import_runs_once(monkeypatch, fresh_cache):
    calls = []

    def slow_import(name):
        calls.append(name)
        time.sleep(0.01)
        raise ImportError(name)

    monkeypatch.setattr(config_api, "import_module", slow_import)
    assert config_api._sokudan_available() is False
    assert config_api._sokudan_available() is False
    assert calls == ["sokudan"]


def test_sokudan_cache_clear_rechecks(monkeypatch, fresh_cache):
    monkeypatch.setattr(config_api, "import_module", lambda name: None)
    assert config_api._sokudan_available() is True

    def missing(name):
        raise ImportError(name)

    monkeypatch.setattr(config_api, "import_module", missing)
    assert config_api._sokudan_available() is True  # cached
    config_api.clear_sokudan_cache()
    assert config_api._sokudan_available() is False


def test_pytest_timeout_configured(pytestconfig):
    assert pytestconfig.pluginmanager.hasplugin("timeout")
    assert float(pytestconfig.getini("timeout")) > 0

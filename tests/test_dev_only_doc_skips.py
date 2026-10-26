"""The doc-sync tests skip only when a development-only file is absent (public snapshot).

AGENTS.md and docs/research/ are left out by release-exclude.txt. In the development
repository the files exist, so the tests must run (and fail on a mismatch) as before.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_devonly_{name}", TESTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agents_md_present_is_read_not_skipped(tmp_path, monkeypatch):
    docs_sync = _load("test_docs_sync")
    path = tmp_path / "AGENTS.md"
    path.write_text("### 1. Neural State\nx\n### 2. Circuit Breaker\n", encoding="utf-8")
    monkeypatch.setattr(docs_sync, "AGENTS_MD", path)
    assert docs_sync._section("### 1. Neural State", "### 2.") == "### 1. Neural State\nx\n"


def test_agents_md_present_but_wrong_still_fails(tmp_path, monkeypatch):
    docs_sync = _load("test_docs_sync")
    path = tmp_path / "AGENTS.md"
    path.write_text("no sections here\n", encoding="utf-8")
    monkeypatch.setattr(docs_sync, "AGENTS_MD", path)
    with pytest.raises(pytest.fail.Exception):
        docs_sync._section("### 1. Neural State", "### 2.")


def test_agents_md_absent_skips_with_the_reason(tmp_path, monkeypatch):
    docs_sync = _load("test_docs_sync")
    monkeypatch.setattr(docs_sync, "AGENTS_MD", tmp_path / "AGENTS.md")
    with pytest.raises(pytest.skip.Exception, match="public release snapshot"):
        docs_sync._section("### 1. Neural State", "### 2.")


def test_saturation_report_present_is_read_absent_skips(tmp_path, monkeypatch):
    sat = _load("test_saturation_report")
    doc = tmp_path / "report.md"
    doc.write_text("committed\n", encoding="utf-8")
    monkeypatch.setattr(sat, "DOC", doc)
    assert sat._committed_report() == "committed\n"
    monkeypatch.setattr(sat, "DOC", tmp_path / "missing.md")
    with pytest.raises(pytest.skip.Exception, match="public release snapshot"):
        sat._committed_report()


def test_development_repository_has_the_files_so_nothing_skips():
    # In the development repository (release-exclude.txt lists them as excluded,
    # but they are tracked here) both files exist, so the guarded tests really run.
    root = TESTS.parent
    if not (root / "docs" / "research").is_dir():
        pytest.skip("public release snapshot: docs/research/ is excluded")
    assert (root / "AGENTS.md").is_file()
    assert (root / "docs" / "research" / "neural-saturation-2026-10-16.md").is_file()

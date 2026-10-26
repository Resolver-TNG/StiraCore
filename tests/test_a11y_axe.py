"""axe-core accessibility gate for the shell pages (#197)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")

from tests.ui_pages import render_pages

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "a11y" / "axe_check.cjs"


def _skip_or_fail(reason: str) -> None:
    if os.environ.get("STIRA_REQUIRE_AXE") == "1":
        pytest.fail(f"STIRA_REQUIRE_AXE=1 but axe check unavailable: {reason}")
    pytest.skip(reason)


def test_axe_no_violations(tmp_path):
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    out_dir = tmp_path / "html"
    out_dir.mkdir()
    for name, html in pages.items():
        (out_dir / f"{name}.html").write_text(html, encoding="utf-8")
    proc = subprocess.run(
        [node, str(SCRIPT), str(out_dir)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"axe violations (exit {proc.returncode}):\n{output}"


def test_axe_live_approval_cards_and_keyboard_flow(tmp_path):
    """#189: cards/history rendered by approvals.js pass axe; keyboard-only decisions work."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    page = tmp_path / "approvals.html"
    page.write_text(pages["approvals"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "approvals_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"approval cards a11y (exit {proc.returncode}):\n{output}"


def test_axe_live_permission_cards(tmp_path):
    """#244: permission cards rendered by permission_cards.js on the chat page pass axe."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    page = tmp_path / "chat.html"
    page.write_text(pages["chat"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "permission_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"permission cards a11y (exit {proc.returncode}):\n{output}"


def test_axe_live_settings_diagnostics_section(tmp_path):
    """#267: the Diagnostics section rendered by settings_diagnostics.js passes axe."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "diagnostics_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"diagnostics section a11y (exit {proc.returncode}):\n{output}"


def test_axe_live_settings_yolo_section(tmp_path):
    """ADR 0012 phase 7c: the YOLO section rendered by settings_yolo.js (a pinned value, a
    restart notice, a config warning, an inline error) passes axe."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "yolo_settings_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"YOLO settings section a11y (exit {proc.returncode}):\n{output}"


def test_axe_neural_trend_collapse(tmp_path):
    """#303: collapsible Neural State trend: native toggle, aria-expanded, storage, axe clean."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    page = tmp_path / "chat.html"
    page.write_text(pages["chat"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "neural_trend_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"neural trend a11y (exit {proc.returncode}):\n{output}"


def test_axe_yolo_badge_states(tmp_path):
    """ADR 0012 phase 5b: the YOLO button, every badge state and the tier dialog on the
    chat and Stage pages (rendered with the allowance) pass axe."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    files = []
    for name in ("chat_yolo", "stage_yolo"):
        page = tmp_path / f"{name}.html"
        page.write_text(pages[name], encoding="utf-8")
        files.append(str(page))
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "yolo_axe.cjs"), *files],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"YOLO badge a11y (exit {proc.returncode}):\n{output}"


def test_axe_live_allowance_badge(tmp_path):
    """ADR 0025 PR 2: the allowance badge, its list (revoke, Revoke all) on chat and Stage."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    files = []
    for name in ("chat", "stage"):
        page = tmp_path / f"{name}.html"
        page.write_text(pages[name], encoding="utf-8")
        files.append(str(page))
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "allow_axe.cjs"), *files],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"allowance badge a11y (exit {proc.returncode}):\n{output}"


def test_axe_live_settings_surface(tmp_path):
    """#469: the restart banner, the "Where settings live" card and a pinned input pass axe."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    page = tmp_path / "settings.html"
    page.write_text(pages["settings"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "settings_surface_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"settings surface a11y (exit {proc.returncode}):\n{output}"


def test_axe_live_procedural_review_panel(tmp_path):
    """#463: the proposals panel rendered by procedural_review.js on /memory passes axe;
    bodies stay text, approving needs the shared dialog, a typed reason survives polls."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    page = tmp_path / "forest.html"
    page.write_text(pages["forest"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "procedural_review_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"procedural review a11y (exit {proc.returncode}):\n{output}"


def test_axe_grants_panel(tmp_path):
    """ADR 0011 W4: the goal grants panel (stage C on): every state renders, no goal text
    outside the issuer-only view, the issue's confirmation flow, axe clean."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    assert "sec-grants" not in pages["settings"]  # stage C off: not on the page
    page = tmp_path / "settings_grants.html"
    page.write_text(pages["settings_grants"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "grants_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"grants panel a11y (exit {proc.returncode}):\n{output}"


def test_axe_grants_panel_with_the_heartbeat_start(tmp_path):
    """W6d: the issue form's heartbeat-start switch (labelled, described) is axe clean, and a
    checked switch puts ``heartbeat_start: true`` into the issue body."""
    node = shutil.which("node")
    if node is None:
        _skip_or_fail("node not found on PATH")
    try:
        pages = render_pages(tmp_path / "store")
    except RuntimeError as exc:
        _skip_or_fail(str(exc))
    assert 'id="grants-heartbeat-start"' not in pages["settings_grants"]
    assert 'id="grants-heartbeat-start"' in pages["settings_grants_tick"]
    page = tmp_path / "settings_grants_tick.html"
    page.write_text(pages["settings_grants_tick"], encoding="utf-8")
    proc = subprocess.run(
        [node, str(ROOT / "tests" / "a11y" / "grants_axe.cjs"), str(page)],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=180,
        check=False,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 77:
        _skip_or_fail(output.strip())
    assert proc.returncode == 0, f"grants panel a11y (exit {proc.returncode}):\n{output}"

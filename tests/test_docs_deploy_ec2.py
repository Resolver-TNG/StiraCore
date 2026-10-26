"""The EC2 deployment guide (#318): the unit in the docs is the example file, the links resolve,
the names it uses exist in the code, and the examples open no door by default."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
EXAMPLES = ROOT / "examples" / "deploy-ec2"
GUIDES = (DOCS / "deploy-ec2.md", DOCS / "deploy-ec2_ja.md")
UNIT = EXAMPLES / "stiracore.service"
ENV_EXAMPLE = EXAMPLES / "stira.env.example"

# Names that would open a door or enable something: never assigned by the examples.
FORBIDDEN_ASSIGNMENTS = (
    "STIRA_HOST",
    "STIRA_ALLOW_REMOTE",
    "STIRA_ENABLE_EXEC_TOOLS",
    "STIRA_SCHEDULER",
    "STIRA_HEARTBEAT",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)


def _slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, drop punctuation, spaces become hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip()).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    anchors: set[str] = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        match = None if in_fence else re.match(r"^#{1,6}\s+(.*?)\s*#*$", line)
        if match:
            slug = _slug(match.group(1))
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _links(text: str) -> list[str]:
    """Targets of inline Markdown links outside code fences, external ones dropped."""
    outside = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    targets = re.findall(r"\]\(([^)\s]+)\)", outside)
    return [t for t in targets if not re.match(r"[a-z]+://|mailto:", t)]


def _from_section(path: Path, marker: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(marker)
    return text[start:]


def _check_links(path: Path, text: str) -> list[str]:
    problems = []
    for target in _links(text):
        name, _, fragment = target.partition("#")
        dest = (path.parent / name).resolve() if name else path
        if not dest.exists():
            problems.append(f"{path.name}: {target}: no such file")
            continue
        if fragment and dest.suffix == ".md" and fragment not in _anchors(dest):
            problems.append(f"{path.name}: {target}: no such heading")
    return problems


@pytest.mark.parametrize("guide", GUIDES, ids=lambda p: p.name)
def test_the_unit_in_the_guide_is_the_example_file(guide: Path) -> None:
    text = guide.read_text(encoding="utf-8")
    block = re.search(r"<!-- unit:begin -->\n```ini\n(.*?)```\n<!-- unit:end -->", text, re.DOTALL)
    assert block, f"{guide.name}: unit block markers not found"
    assert block.group(1) == UNIT.read_text(encoding="utf-8")


@pytest.mark.parametrize("guide", GUIDES, ids=lambda p: p.name)
def test_links_in_the_guide_resolve(guide: Path) -> None:
    assert _check_links(guide, guide.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    ("path", "marker"),
    [
        (DOCS / "install.md", "## 11. Environment comparison"),
        (DOCS / "install_ja.md", "## 11. 環境の比較"),
    ],
    ids=["install.md", "install_ja.md"],
)
def test_links_in_the_environment_comparison_resolve(path: Path, marker: str) -> None:
    assert _check_links(path, _from_section(path, marker)) == []


def test_the_guide_is_linked_from_the_indexes() -> None:
    for path, needle in (
        (ROOT / "README.md", "docs/deploy-ec2.md"),
        (ROOT / "README_ja.md", "docs/deploy-ec2_ja.md"),
        (DOCS / "README.md", "deploy-ec2.md"),
        (DOCS / "README.md", "deploy-ec2_ja.md"),
    ):
        assert needle in path.read_text(encoding="utf-8"), (path.name, needle)


def _code_text() -> str:
    parts = []
    for base in (
        ROOT / "src",
        ROOT / "dashboard",
        ROOT / "scripts",
        ROOT / "examples" / "memory-maintenance",
    ):
        for file in base.rglob("*"):
            if file.is_file() and file.suffix in {".py", ".sh", ".ps1", ".service", ".example"}:
                parts.append(file.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def test_environment_variables_named_in_the_guides_exist_in_the_code() -> None:
    code = _code_text()
    for path in (*GUIDES, UNIT, ENV_EXAMPLE, *EXAMPLES.glob("stira-distill.*")):
        names = set(re.findall(r"\bSTIRA_[A-Z][A-Z0-9_]*\b", path.read_text(encoding="utf-8")))
        missing = sorted(n for n in names if n not in code)
        assert missing == [], f"{path.name}: not found in the code: {missing}"


def test_both_guides_name_the_same_environment_variables() -> None:
    sets = [
        set(re.findall(r"\bSTIRA_[A-Z][A-Z0-9_]*\b", g.read_text(encoding="utf-8"))) for g in GUIDES
    ]
    assert sets[0] == sets[1]


@pytest.mark.parametrize(
    "path", [UNIT, ENV_EXAMPLE, *sorted(EXAMPLES.glob("stira-distill.*"))], ids=lambda p: p.name
)
def test_the_examples_assign_nothing_that_opens_a_door(path: Path) -> None:
    active = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    for line in active:
        for name in FORBIDDEN_ASSIGNMENTS:
            assert not re.search(rf"\b{name}\s*=", line), f"{path.name}: {line}"


def test_the_unit_runs_the_server_in_the_foreground_as_a_dedicated_user() -> None:
    unit = UNIT.read_text(encoding="utf-8")
    assert re.search(r"^Type=simple$", unit, re.MULTILINE)
    assert re.search(r"^User=(?!root\b)\w+$", unit, re.MULTILINE)
    assert re.search(r"^ProtectSystem=strict$", unit, re.MULTILINE)
    assert re.search(r"^KillSignal=SIGTERM$", unit, re.MULTILINE)
    # Exit statuses 2 (credentials), 3 (data directory owned), 4 (port in use): see stira_server.
    assert re.search(r"^RestartPreventExitStatus=2 3 4$", unit, re.MULTILINE)
    server = (ROOT / "dashboard" / "stira_server.py").read_text(encoding="utf-8")
    assert server.count("raise SystemExit(2)") >= 1
    assert server.count("raise SystemExit(3)") >= 1
    assert server.count("raise SystemExit(4)") >= 1


_NAMES = ("stiracore.service", "stira-distill.service", "stira-distill.timer")


def _verify(tmp_path: Path, drop: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """``systemd-analyze verify`` on copies whose host paths and accounts exist on this machine.

    The examples name /opt/stira/..., the ``stira`` user and the venv's Python; none of them
    exists on a test machine, and verify reports a missing executable as an error. They are
    pointed at real files so that the exit status judges the unit file itself.
    ``drop`` maps a file name to a line prefix that is removed from that copy (negative tests).
    """
    import grp  # Unix only: imported here so that this file still collects on Windows
    import pwd

    me = pwd.getpwuid(os.getuid()).pw_name
    group = grp.getgrgid(os.getgid()).gr_name
    # Tokens first: sys.executable may itself live under a path that starts with /opt/stira/....
    tokens = {
        "@PYTHON@": sys.executable,
        "@SERVER@": str(ROOT / "dashboard" / "stira_server.py"),
        "@WRAPPER@": str(ROOT / "examples" / "memory-maintenance" / "stira-distill.sh"),
    }
    replacements = (
        ("/opt/stira/StiraCore/.venv/bin/python", "@PYTHON@"),
        ("/opt/stira/StiraCore/dashboard/stira_server.py", "@SERVER@"),
        ("/opt/stira/StiraCore/examples/memory-maintenance/stira-distill.sh", "@WRAPPER@"),
        ("/opt/stira/StiraCore", str(tmp_path)),
    )
    for name in _NAMES:
        text = (EXAMPLES / name).read_text(encoding="utf-8")
        for old, new in replacements:
            text = text.replace(old, new)
        for token, value in tokens.items():
            text = text.replace(token, value)
        text = re.sub(r"^User=.*$", f"User={me}", text, flags=re.MULTILINE)
        text = re.sub(r"^Group=.*$", f"Group={group}", text, flags=re.MULTILINE)
        if drop and name in drop:
            text = "\n".join(line for line in text.splitlines() if not line.startswith(drop[name]))
        (tmp_path / name).write_text(text + "\n", encoding="utf-8")
    return subprocess.run(
        ["systemd-analyze", "verify", *(str(tmp_path / n) for n in _NAMES)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not found")
def test_systemd_accepts_the_units(tmp_path: Path) -> None:
    result = _verify(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not found")
@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("stiracore.service", "ExecStart="),
        ("stira-distill.service", "ExecStart="),
        ("stiracore.service", "[Service]"),
    ],
)
def test_the_systemd_check_fails_for_a_broken_unit(tmp_path: Path, name: str, prefix: str) -> None:
    """Negative control: the verdict above is the exit status, so a broken unit must change it."""
    assert _verify(tmp_path, drop={name: prefix}).returncode != 0

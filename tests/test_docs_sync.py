"""Keep AGENTS.md numbers, docs/configuration*.md tables and the ADR index in sync with the code."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from stiracore.config.models import AgentConfig, GatewayConfig
from stiracore.core.engine import StiraConfig
from stiracore.core.neural_state import BASELINES, DIMS

AGENTS_MD = Path(__file__).resolve().parents[1] / "AGENTS.md"
# AGENTS.md is development-only: release-exclude.txt leaves it out of the public snapshot.
AGENTS_MD_SKIP = "AGENTS.md is not in this checkout (public release snapshot, release-exclude.txt)"


def _agents_md_text() -> str:
    """AGENTS.md text; skip (not fail) only where the file is absent, i.e. in the snapshot."""
    if not AGENTS_MD.is_file():
        pytest.skip(AGENTS_MD_SKIP)
    return AGENTS_MD.read_text(encoding="utf-8")


def _section(start: str, end: str) -> str:
    text = _agents_md_text()
    begin = text.find(start)
    if begin < 0:
        pytest.fail(f"section {start!r} not found in AGENTS.md")
    stop = text.find(end, begin + len(start))
    if stop < 0:
        pytest.fail(f"section end {end!r} not found in AGENTS.md")
    return text[begin:stop]


def _extract(pattern: str, text: str, label: str) -> float:
    match = re.search(pattern, text)
    if match is None:
        pytest.fail(f"{label} not found in AGENTS.md (pattern {pattern!r})")
    return float(match.group(1))


def test_neural_state_baselines_match_implementation() -> None:
    section = _section("### 1. Neural State", "### 2. Circuit Breaker")
    rows = {
        name: float(value)
        for name, value in re.findall(
            r"^\|\s*`(\w+)`\s*\|[^|\n]*\|\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*$",
            section,
            flags=re.MULTILINE,
        )
    }
    missing = [dim for dim in DIMS if dim not in rows]
    if missing:
        pytest.fail(f"Neural State table lacks dimensions: {missing}")
    assert set(rows) == set(BASELINES)
    for dim, baseline in BASELINES.items():
        assert rows[dim] == pytest.approx(baseline), dim


def test_breaker_thresholds_match_config() -> None:
    section = _section("### 2. Circuit Breaker", "### 3.")
    config = StiraConfig()
    num = r"([0-9]+(?:\.[0-9]+)?)"

    checks = {
        "stability_warning": (
            rf"\*\*WARNING\*\*\s*\|\s*stability\s*<\s*{num}",
            config.stability_warning,
        ),
        "stability_critical": (
            rf"\*\*CRITICAL\*\*\s*\|\s*stability\s*<\s*{num}",
            config.stability_critical,
        ),
        "normal_lower_bound": (
            rf"\*\*NORMAL\*\*\s*\|\s*stability\s*≥\s*{num}",
            config.stability_warning,
        ),
        "stability_warning_exit": (
            rf"WARNINGから回復するにはstability\s*>\s*{num}",
            config.stability_warning_exit,
        ),
        "stability_critical_exit": (
            rf"CRITICALからの回復にはstability\s*>\s*{num}",
            config.stability_critical_exit,
        ),
        "critical_cooldown_turns": (
            r"かつ\s*(\d+)\s*ターン",
            float(config.critical_cooldown_turns),
        ),
    }
    for label, (pattern, expected) in checks.items():
        assert _extract(pattern, section, label) == pytest.approx(expected), label


# --- Configuration reference, data directory and ADR index (docs/configuration*.md) ---

ROOT = AGENTS_MD.parent
DOCS = ROOT / "docs"
CONFIG_DOCS = (DOCS / "configuration.md", DOCS / "configuration_ja.md")
_ENV_NAME = re.compile(r"\A_*[A-Z][A-Za-z0-9_]*\Z")
_ENVIRON_NAMES = {"env", "environ"}
# Read by an upstream tool, not by Stira: documented as ignored (tool_consent replaces it).
_UPSTREAM_ONLY = {"BYPASS_TOOL_CONSENT"}


def _runtime_sources() -> list[Path]:
    files = sorted((ROOT / "src" / "stiracore").rglob("*.py"))
    files += [
        p for p in sorted((ROOT / "dashboard").glob("*.py")) if not p.name.startswith("test_")
    ]
    return files


def _marked(path: Path, marker: str) -> str:
    text = path.read_text(encoding="utf-8")
    begin, end = f"<!-- {marker}:begin -->", f"<!-- {marker}:end -->"
    if begin not in text or end not in text:
        pytest.fail(f"{path.name}: markers {begin} / {end} not found")
    return text.split(begin, 1)[1].split(end, 1)[0]


def _table_rows(block: str) -> list[list[str]]:
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|") or set(line) <= {"|", "-", " "}:
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:]  # drop the header


def _code_names(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def _is_environ(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return isinstance(node.value, ast.Name) and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id in _ENVIRON_NAMES


def _literal_strings(node: ast.AST, index: int | None) -> set[str]:
    """String constants of a literal tuple/list (element ``index`` of nested tuples)."""
    found: set[str] = set()
    if not isinstance(node, (ast.Tuple, ast.List)):
        return found
    for element in node.elts:
        if index is not None and isinstance(element, (ast.Tuple, ast.List)):
            element = element.elts[index] if index < len(element.elts) else None
        if isinstance(element, ast.Constant) and isinstance(element.value, str):
            found.add(element.value)
    return found


def _env_references(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """(names, f-string prefixes, unresolved) read from ``os.environ`` / ``env`` / ``environ``."""
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                constants[target.id] = node.value
    loop_values: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.comprehension)):
            iterable = node.iter
            if isinstance(iterable, ast.Name) and iterable.id in constants:
                iterable = constants[iterable.id]
            targets = (
                [(node.target, None)]
                if isinstance(node.target, ast.Name)
                else [
                    (element, position)
                    for position, element in enumerate(getattr(node.target, "elts", []))
                ]
            )
            for target, position in targets:
                if isinstance(target, ast.Name):
                    values = _literal_strings(iterable, position)
                    loop_values.setdefault(target.id, set()).update(values)

    names: set[str] = set()
    prefixes: set[str] = set()
    unresolved: set[str] = set()

    def resolve(arg: ast.AST) -> None:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.add(arg.value)
        elif isinstance(arg, ast.JoinedStr):
            first = arg.values[0] if arg.values else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                prefixes.add(first.value)
            else:
                unresolved.add(ast.unparse(arg))
        elif isinstance(arg, ast.Name):
            value = constants.get(arg.id)
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                names.add(value.value)
            elif loop_values.get(arg.id):
                names.update(v for v in loop_values[arg.id] if _ENV_NAME.match(v))
            else:
                unresolved.add(arg.id)
        else:
            unresolved.add(ast.unparse(arg))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in ("get", "setdefault", "pop")
                and _is_environ(func.value)
            ) or (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                resolve(node.args[0])
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            resolve(node.slice)
        elif (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and _is_environ(node.comparators[0])
        ):
            resolve(node.left)
    return names, prefixes, unresolved


# Lookups whose name is not visible in the code. A new one must be documented by hand
# and added here (tools/tool_consent.py: the per-tool environ proxy for upstream tools).
_DYNAMIC_LOOKUPS = {("tool_consent.py", "key")}


def _code_env_names() -> tuple[set[str], set[str]]:
    from stiracore.providers import credentials

    names: set[str] = set()
    prefixes: set[str] = set()
    dynamic: set[tuple[str, str]] = set()
    for path in _runtime_sources():
        found, found_prefixes, unresolved = _env_references(ast.parse(path.read_text("utf-8")))
        names |= {name for name in found if _ENV_NAME.match(name)}
        prefixes |= found_prefixes
        dynamic |= {(path.name, expression) for expression in unresolved}
    assert dynamic <= _DYNAMIC_LOOKUPS, f"new dynamic environment lookups: {dynamic}"
    # Read indirectly (through a helper) from these registries of variable names.
    names |= set(credentials.DEFAULT_API_KEY_ENV.values())
    names |= {name for group in credentials._ALTERNATE_API_KEY_ENV.values() for name in group}
    names |= set(credentials._AWS_ENV_HINTS)
    return names, prefixes


def _doc_env_names(path: Path, marker: str = "env-vars") -> set[str]:
    names: set[str] = set()
    for row in _table_rows(_marked(path, marker)):
        names |= {name for name in _code_names(row[0]) if _ENV_NAME.match(name)}
    return names


def test_env_reference_collector_sees_known_shapes() -> None:
    tree = ast.parse(
        "import os\n"
        "X = 'STIRA_CONST'\n"
        "a = os.environ.get('STIRA_GET')\n"
        "b = os.environ['STIRA_ITEM']\n"
        "c = 'STIRA_IN' in os.environ\n"
        "d = os.getenv(X)\n"
        "e = os.environ.get(f'WEBHOOK_SECRET_{p}')\n"
        "for key, name in (('k', 'STIRA_LOOP'),):\n"
        "    env[name]\n"
    )
    names, prefixes, _ = _env_references(tree)
    assert names == {"STIRA_CONST", "STIRA_GET", "STIRA_ITEM", "STIRA_IN", "STIRA_LOOP"}
    assert prefixes == {"WEBHOOK_SECRET_"}


@pytest.mark.parametrize("path", CONFIG_DOCS, ids=lambda p: p.name)
def test_documented_environment_variables_match_the_code(path: Path) -> None:
    from stiracore.tools.tool_consent import BYPASS_ENV_VAR

    assert BYPASS_ENV_VAR in _UPSTREAM_ONLY
    code, prefixes = _code_env_names()
    documented = _doc_env_names(path)
    missing = sorted(code - documented)
    assert not missing, f"{path.name}: undocumented environment variables {missing}"
    for prefix in prefixes:
        assert any(name.startswith(prefix) for name in documented), (path.name, prefix)
    stale = sorted(
        name
        for name in documented - code - _UPSTREAM_ONLY
        if not any(name.startswith(prefix) for prefix in prefixes)
    )
    assert not stale, f"{path.name}: documented but never read {stale}"


def test_english_and_japanese_configuration_list_the_same_names() -> None:
    english, japanese = CONFIG_DOCS
    for marker in ("env-vars", "env-vars-dev"):
        assert _doc_env_names(english, marker) == _doc_env_names(japanese, marker), marker
    for marker in ("gateway-fields", "agent-fields", "data-files"):
        first = [row[0] for row in _table_rows(_marked(english, marker))]
        second = [row[0] for row in _table_rows(_marked(japanese, marker))]
        assert first == second, marker


@pytest.mark.parametrize("path", CONFIG_DOCS, ids=lambda p: p.name)
def test_dev_environment_variables_exist_in_scripts_or_tests(path: Path) -> None:
    text = "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for folder in ("scripts", "tests")
        for p in sorted((ROOT / folder).iterdir())
        if p.is_file() and p.suffix in (".py", ".sh", ".ps1") and p.name != "test_docs_sync.py"
    )
    for name in _doc_env_names(path, "env-vars-dev"):
        assert name in text, f"{path.name}: {name} is not used by any script or test"


@pytest.mark.parametrize("path", CONFIG_DOCS, ids=lambda p: p.name)
def test_gateway_config_fields_and_defaults_match(path: Path) -> None:
    defaults = GatewayConfig().as_dict()
    rows = {
        _code_names(row[0])[0]: _code_names(row[1])
        for row in _table_rows(_marked(path, "gateway-fields"))
    }
    assert set(rows) == set(defaults)
    for name, cell in rows.items():
        assert len(cell) == 1, (path.name, name)
        assert json.loads(cell[0]) == defaults[name], (path.name, name)


@pytest.mark.parametrize("path", CONFIG_DOCS, ids=lambda p: p.name)
def test_agent_config_fields_and_defaults_match(path: Path) -> None:
    defaults = AgentConfig().as_dict()
    rows = {
        _code_names(row[0])[0]: _code_names(row[1])
        for row in _table_rows(_marked(path, "agent-fields"))
    }
    assert set(rows) == set(defaults)
    checked = 0
    for name, cell in rows.items():
        if len(cell) != 1:
            continue  # described in words (anchors, nested defaults)
        try:
            value = json.loads(cell[0])
        except ValueError:
            continue
        assert value == defaults[name], (path.name, name)
        checked += 1
    assert checked >= 15


def _source_literals() -> set[str]:
    literals: set[str] = set()
    for path in [*_runtime_sources(), *sorted((ROOT / "scripts").glob("stira_ctl.*"))]:
        text = path.read_text(encoding="utf-8")
        literals |= set(re.findall(r"""["']([^"'\n]+)["']""", text))
        literals |= set(re.findall(r"[\w.-]+", text))
    return literals


def _data_dir_files_in_code() -> set[str]:
    """First path component of every ``DATA_DIR / "..."`` and every owner-lock/log name."""
    from stiracore.config import data_dir_lock
    from stiracore.heartbeat import daemon
    from stiracore.scheduler import scheduler

    tree = ast.parse((ROOT / "dashboard" / "stira_server.py").read_text("utf-8"))
    names = {
        data_dir_lock.LOCK_FILENAME,
        daemon.LOCK_FILENAME,
        scheduler.LOCK_FILENAME,
        scheduler.LOG_FILENAME,
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Div)
            and isinstance(node.left, ast.Name)
            and node.left.id == "DATA_DIR"
            and isinstance(node.right, ast.Constant)
        ):
            names.add(node.right.value)
    func = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_policy_files"
    )
    names |= {
        n.value
        for n in ast.walk(func)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and re.fullmatch(r"[\w.-]+", n.value)
    }
    # SQLite sidecars and the temp file are covered by their main file's row.
    return {name for name in names if not re.search(r"(-wal|-shm|\.tmp)\Z", name)}


@pytest.mark.parametrize("path", CONFIG_DOCS, ids=lambda p: p.name)
def test_data_directory_table_matches_the_code(path: Path) -> None:
    documented = [_code_names(row[0])[0] for row in _table_rows(_marked(path, "data-files"))]
    literals = _source_literals()
    for entry in documented:
        for part in entry.strip("/").split("/"):
            if part.startswith("<"):
                continue
            base = part
            while base not in literals and re.search(r"\.(head|locked)\Z", base):
                base = base.rsplit(".", 1)[0]
            assert base in literals, f"{path.name}: {entry} ({part}) not found in the code"
    first_parts = {entry.strip("/").split("/")[0] for entry in documented}
    missing = sorted(_data_dir_files_in_code() - first_parts)
    assert not missing, f"{path.name}: data-directory files missing from the table {missing}"


def test_adr_index_matches_the_files() -> None:
    index = DOCS / "README.md"
    linked = set(re.findall(r"\]\(adr/([^)#]+\.md)\)", _marked(index, "adr-index")))
    files = {p.name for p in (DOCS / "adr").glob("*.md")}
    assert linked == files
    numbered = sorted(name for name in files if re.match(r"\d{4}-", name))
    rows = [row[0] for row in _table_rows(_marked(index, "adr-index"))]
    assert len(rows) == len(files)
    # Numbered ADRs are listed in order, each with its own number as the label.
    labels = [re.match(r"\[([^\]]+)\]", row).group(1) for row in rows[: len(numbered)]]
    assert labels == [name[:4] for name in numbered]


# --- Glossary (docs/glossary.md) against the Japanese UI catalog ---------------------------

_GLOSSARY_UI_KEYS = {
    "`NORMAL`": "shell.normal",
    "`WARNING`": "shell.warning",
    "`CRITICAL`": "shell.critical",
    "`COHERENCE`": "shell.coherence",
}


def test_glossary_breaker_labels_match_the_japanese_ui() -> None:
    catalog = json.loads(
        (ROOT / "dashboard" / "static" / "i18n" / "catalog.ja.json").read_text(encoding="utf-8")
    )
    rows = {row[0]: row[1] for row in _table_rows((DOCS / "glossary.md").read_text("utf-8"))}
    for term, key in _GLOSSARY_UI_KEYS.items():
        assert rows.get(term) == catalog[key], term
    rules = rows["Rule `always` / `ask` / `never`"]
    assert rules == " / ".join(catalog[f"policy.{name}"] for name in ("always", "ask", "never"))


def test_agents_md_does_not_describe_halt_as_a_stop_before_the_model_call() -> None:
    # NXPAgent keeps calling the model at CRITICAL+HALT and gates each write (#146, ADR 0010).
    section = _section("### 2. Circuit Breaker", "### 3.")
    assert "モデル呼び出し前に停止" not in section
    assert "RuntimeError（モデル呼び出しなし）" not in _agents_md_text()

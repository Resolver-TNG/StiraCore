import inspect

import pytest

from stiracore.core.amygdala import AmygdalaPass
from stiracore.core.engine import StiraConfig, StiraEngine
from stiracore.core.neural_state import NeuralState
from stiracore.middleware.identity_guard import IdentityGuard
from stiracore.tools.memory_store import MemoryStore


@pytest.fixture
def guard():
    parameters = inspect.signature(StiraEngine).parameters
    state_parameter = next(name for name in ("state", "neural_state") if name in parameters)
    engine = StiraEngine(**{state_parameter: NeuralState(), "config": StiraConfig()})
    return IdentityGuard(engine, ["*/soul.md", "*/SOUL.md", "*/AGENTS.md"])


@pytest.fixture
def scorer():
    return AmygdalaPass()


@pytest.mark.parametrize(
    "command",
    [
        "printf replacement > SOUL.md",
        "rm -f './SOUL.md'",
        "python -c \"open('SOUL.md', 'w').write('replacement')\"",
        "python3 -c \"open('SOUL.md', 'w').write('replacement')\"",
        "python.exe -c \"open('SOUL.md', 'w').write('replacement')\"",
        "python3 -c \"open('SOUL.md', mode='a').write('replacement')\"",
        ("python3 -c \"from pathlib import Path; Path('SOUL.md').write_text('replacement')\""),
        ("python -c \"open('agents/offline/SOUL.md', 'w').write('replacement')\""),
        "cat /dev/null >> agents/offline/SOUL.md",
    ],
)
def test_shell_cannot_modify_protected_identity(guard, command):
    result = guard.check_tool_call("shell", {"command": command})
    assert result.allowed is False
    assert result.reason


@pytest.mark.parametrize(
    "command",
    [
        "python -c \"open('notes.txt', 'w').write('replacement')\"",
        "python3 -c \"print('hello')\"",
    ],
)
def test_shell_python_without_protected_paths_is_allowed(guard, command):
    result = guard.check_tool_call("shell", {"command": command})
    assert result.allowed is True


@pytest.mark.parametrize(
    "path",
    [
        "../SOUL.md",
        "../../SOUL.md",
        "workspace/../SOUL.md",
        r"workspace\..\SOUL.md",
    ],
)
def test_path_traversal_cannot_bypass_identity_protection(guard, path):
    result = guard.check_tool_call("write_file", {"path": path, "content": "replacement"})
    assert result.allowed is False
    assert result.reason


def test_guard_allows_reading_protected_identity(guard):
    result = guard.check_tool_call("read_file", {"path": "SOUL.md"})
    assert result.allowed is True


@pytest.fixture
def outside_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    return outside, sentinel


def test_memory_store_rejects_symlink_base(tmp_path, outside_directory):
    outside, sentinel = outside_directory
    link = tmp_path / "linked-base"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink not available")

    with pytest.raises(ValueError):
        MemoryStore(link, "offline")

    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert list(outside.iterdir()) == [sentinel]


def test_memory_store_rejects_symlink_layer(tmp_path, outside_directory):
    outside, sentinel = outside_directory
    base = tmp_path / "store"
    memory = base / "agents" / "offline" / "memory"
    memory.mkdir(parents=True)
    try:
        (memory / "episodic").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink not available")

    with pytest.raises(ValueError):
        MemoryStore(base, "offline")

    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert list(outside.iterdir()) == [sentinel]


@pytest.mark.parametrize(
    "importance",
    [
        None,
        True,
        False,
        "0.9",
        [],
        {},
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.01,
        1.01,
    ],
)
def test_invalid_importance_falls_back_to_heuristics(scorer, importance):
    content = "confirmed decision: fix the error"
    metadata = {"importance": importance, "source": "offline"}

    expected = scorer.score(content, {"source": "offline"})

    assert scorer.score(content, metadata) == expected


@pytest.mark.parametrize("importance", [0.0, 0.25, 0.7, 1.0])
def test_valid_importance_overrides_heuristics(scorer, importance):
    assert scorer.score(
        "confirmed decision: fix the error", {"importance": importance}
    ) == pytest.approx(importance)

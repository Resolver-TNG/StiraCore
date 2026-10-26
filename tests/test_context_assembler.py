"""Offline tests using the supplied context and persistence APIs."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from stiracore.agent.context_assembler import ContextAssembler, estimate_tokens
from stiracore.agent.state_persistence import StatePersistence
from stiracore.tools.memory_store import MemoryLayer


@dataclass
class FakeState:
    energy: float = 0.5
    mood: str = "steady"
    anchors: list[str] = field(default_factory=lambda: ["be honest"])


# Provenance the host-side approval writes (#144); procedural entries without it are skipped.
APPROVED = {
    "source_turn": "chat-1",
    "approved_by": "local:operator",
    "approved_at": "2026-10-16T00:00:00+00:00",
    "review": "approved",
}


@dataclass
class Entry:
    content: str
    metadata: dict = field(default_factory=lambda: dict(APPROVED))


class FakeMemoryStore:
    """Agent scope is constructor-bound; queries take only layer and limit."""

    def __init__(self, agent_id: str = "agent-a") -> None:
        self.agent_id = agent_id
        self.entries: dict[str, list[Any]] = {}
        self.calls: list[tuple[Any, int]] = []

    def list_entries(self, layer: MemoryLayer | str, limit: int = 20) -> list[Any]:
        self.calls.append((layer, limit))
        name = layer.value if isinstance(layer, MemoryLayer) else layer
        return self.entries.get(name, [])[:limit]


class FakeEngine:
    def __init__(self) -> None:
        self.state = FakeState()
        self.last_breaker = {"level": "NORMAL", "actions": 0}
        self.identity = "Be honest and preserve the user's intent."
        self.identity_calls = 0

    def identity_injection(self) -> str:
        self.identity_calls += 1
        return self.identity

    def pre_turn(self) -> None:
        raise AssertionError("Context assembly must not invoke pre_turn")


def make_assembler(tmp_path: Path, **kwargs: Any):
    store = FakeMemoryStore()
    store.entries["procedural"] = [
        Entry("verify before writing"),
        Entry("keep changes small"),
    ]
    store.entries["episodic"] = [
        Entry("latest episode"),
        Entry("older episode"),
    ]
    engine = FakeEngine()
    assembler = ContextAssembler("agent-a", store, engine, tmp_path / "soul.md", **kwargs)
    return assembler, store, engine


@pytest.mark.parametrize(("text", "expected"), [("", 0), ("a", 1), ("abcd", 1), ("abcde", 2)])
def test_estimate_tokens(text: str, expected: int) -> None:
    assert estimate_tokens(text) == expected


def test_load_soul_missing_and_utf8(tmp_path: Path) -> None:
    assembler, _, _ = make_assembler(tmp_path)
    assert assembler.load_soul() == ""
    text = "Be kind. Café.\n"
    (tmp_path / "soul.md").write_text(text, encoding="utf-8")
    assert assembler.load_soul() == text


def test_load_soul_does_not_hide_decode_errors(tmp_path: Path) -> None:
    assembler, _, _ = make_assembler(tmp_path)
    (tmp_path / "soul.md").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        assembler.load_soul()


def test_procedural_rules_signature_order_and_limit(tmp_path: Path) -> None:
    assembler, store, _ = make_assembler(tmp_path)
    store.entries["procedural"] = [
        Entry("  newest  "),
        {"content": "second", "metadata": APPROVED},
        Entry("third"),
    ]
    store.calls.clear()
    assert assembler.load_procedural_rules(2) == "- newest\n- second"
    assert store.calls == [(MemoryLayer.PROCEDURAL, 2)]
    assert assembler.load_procedural_rules() == "- newest\n- second\n- third"
    assert store.calls[-1] == (MemoryLayer.PROCEDURAL, 5)


def test_blank_memories_are_omitted(tmp_path: Path) -> None:
    assembler, store, _ = make_assembler(tmp_path)
    store.entries["procedural"] = [
        Entry(" "),
        {"content": "\n", "metadata": APPROVED},
        Entry("rule"),
    ]
    assert assembler.load_procedural_rules() == "- rule"


def test_memory_content_must_be_string(tmp_path: Path) -> None:
    assembler, store, _ = make_assembler(tmp_path)
    store.entries["procedural"] = [{"content": 123}]
    with pytest.raises(TypeError, match="content must be a string"):
        assembler.load_procedural_rules()


def test_backend_cannot_exceed_requested_limit(tmp_path: Path) -> None:
    class OverReturningStore(FakeMemoryStore):
        def list_entries(self, layer, limit=20):
            return iter([Entry("first"), Entry("second"), Entry("third")])

    assembler = ContextAssembler(
        "agent-a", OverReturningStore(), FakeEngine(), tmp_path / "missing.md"
    )
    assert assembler.load_procedural_rules(1) == "- first"


@pytest.mark.parametrize("name", ["max_tokens", "episodic_limit", "procedural_limit"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "5", None])
def test_invalid_constructor_limits(tmp_path: Path, name: str, value: Any) -> None:
    with pytest.raises(ValueError, match=name):
        make_assembler(tmp_path, **{name: value})


@pytest.mark.parametrize("limit", [-1, True, 1.5, "5", None])
def test_invalid_procedural_limit(tmp_path: Path, limit: Any) -> None:
    assembler, _, _ = make_assembler(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        assembler.load_procedural_rules(limit)


def test_zero_memory_limits_do_not_query_store(tmp_path: Path) -> None:
    assembler, store, _ = make_assembler(tmp_path, procedural_limit=0, episodic_limit=0)
    assert assembler.load_procedural_rules(0) == ""
    assembler.build_system_prompt()
    assert store.calls == []


def test_system_prompt_contains_all_sections(tmp_path: Path) -> None:
    (tmp_path / "soul.md").write_text("Be kind.", encoding="utf-8")
    assembler, store, engine = make_assembler(tmp_path, episodic_limit=1)
    prompt = assembler.build_system_prompt()
    for title in (
        "SOUL",
        "Identity Anchors",
        "Procedural Rules",
        "Neural State",
        "Episodic Memory",
        "Breaker",
    ):
        assert prompt.count(f"## {title}\n") == 1
    for text in (
        "Be kind.",
        engine.identity,
        "- verify before writing",
        "- latest episode",
        '"energy": 0.5',
        '"level": "NORMAL"',
    ):
        assert text in prompt
    assert "older episode" not in prompt
    assert store.calls[-1] == (MemoryLayer.EPISODIC, 1)


def test_startup_is_cached_but_turn_data_is_fresh(tmp_path: Path) -> None:
    soul = tmp_path / "soul.md"
    soul.write_text("original soul", encoding="utf-8")
    assembler, store, engine = make_assembler(tmp_path)
    startup = assembler.build_startup_context()
    soul.write_text("replacement soul", encoding="utf-8")
    engine.identity = "replacement identity"
    store.entries["procedural"] = [Entry("replacement rule")]
    store.entries["episodic"] = [Entry("replacement episode")]
    engine.state.mood = "curious"
    engine.last_breaker = {"level": "CAUTION"}
    assert assembler.build_startup_context() == startup
    assert engine.identity_calls == 1
    assert assembler.load_soul() == "replacement soul"
    assert assembler.load_procedural_rules() == "- replacement rule"
    for prompt in (assembler.build_turn_context(), assembler.build_system_prompt()):
        assert "replacement episode" in prompt
        assert '"mood": "curious"' in prompt
        assert '"level": "CAUTION"' in prompt
        assert "replacement rule" not in prompt


def test_breaker_provider_is_read_fresh_and_takes_precedence(tmp_path: Path) -> None:
    snapshots = iter([{"level": "CAUTION"}, {"level": "STOP"}])
    assembler, _, _ = make_assembler(tmp_path, breaker_state_provider=lambda: next(snapshots))
    assert '"level": "CAUTION"' in assembler.build_turn_context()
    assert '"level": "STOP"' in assembler.build_system_prompt()


def test_missing_read_only_breaker_snapshot(tmp_path: Path) -> None:
    assembler, _, engine = make_assembler(tmp_path)
    del engine.last_breaker
    assert (
        "Unavailable: no read-only breaker snapshot has been supplied."
        in assembler.build_turn_context()
    )


@pytest.mark.parametrize("budget", [0, 1, 8, 20, 50, 100, 200])
def test_each_build_respects_token_budget(tmp_path: Path, budget: int) -> None:
    soul = ("soul " * 100).strip()
    (tmp_path / "soul.md").write_text(soul, encoding="utf-8")
    assembler, _, engine = make_assembler(tmp_path, max_tokens=budget)
    protected = f"## SOUL\n{soul}\n\n## Identity Anchors\n{engine.identity}"
    assert estimate_tokens(assembler.build_turn_context()) <= budget
    for build in (assembler.build_startup_context, assembler.build_system_prompt):
        prompt = build()
        # SOUL and Identity Anchors are never shortened (ADR 0026 review B3) ...
        assert prompt.startswith(protected)
        # ... the rest of the prompt keeps to the budget, or is dropped entirely.
        if estimate_tokens(protected) > budget:
            assert prompt == protected
        else:
            assert estimate_tokens(prompt) <= budget


def test_shared_budget_preserves_soul_over_lower_priorities(tmp_path: Path) -> None:
    soul = "S" * 40
    (tmp_path / "soul.md").write_text(soul, encoding="utf-8")
    assembler, _, engine = make_assembler(tmp_path, max_tokens=12)
    # Every lower section is dropped; SOUL and Identity Anchors stay whole (ADR 0026 B3).
    assert assembler.build_system_prompt() == (
        f"## SOUL\n{soul}\n\n## Identity Anchors\n{engine.identity}"
    )
    assembler, _, _ = make_assembler(tmp_path, max_tokens=12, include_identity=False)
    assert assembler.build_system_prompt() == f"## SOUL\n{soul}"


def test_persistence_save_load_preserves_shared_identity(tmp_path: Path) -> None:
    engine = FakeEngine()
    state = engine.state
    path = tmp_path / "nested" / "state.json"
    persistence = StatePersistence(state, path)
    persistence.save()
    assert json.loads(path.read_text(encoding="utf-8"))["energy"] == 0.5
    state.energy = 0.0
    state.mood = "changed"
    state.anchors.clear()
    assert persistence.load() is state
    assert engine.state is state
    assert state == FakeState()


def test_persistence_missing_file_keeps_defaults(tmp_path: Path) -> None:
    state = FakeState(energy=0.9)
    persistence = StatePersistence(state, tmp_path / "missing.json")
    assert persistence.load() is state
    assert state.energy == 0.9
    assert not persistence.path.exists()


@pytest.mark.parametrize(
    ("payload", "error"),
    [("{", json.JSONDecodeError), ("[]", ValueError), ('{"unknown": 1}', ValueError)],
)
def test_persistence_rejects_invalid_files(tmp_path: Path, payload, error) -> None:
    path = tmp_path / "state.json"
    path.write_text(payload, encoding="utf-8")
    state = FakeState()
    with pytest.raises(error):
        StatePersistence(state, path).load()
    assert state == FakeState()


def test_auto_save_hook_accepts_post_turn_arguments(tmp_path: Path) -> None:
    state = FakeState()
    persistence = StatePersistence(state, tmp_path / "state.json")
    hook = persistence.auto_save_hook()
    state.energy = 0.8
    hook("turn result", turn_id=3)
    assert json.loads(persistence.path.read_text(encoding="utf-8"))["energy"] == 0.8


def test_persistence_thread_safety(tmp_path: Path) -> None:
    state = FakeState()
    persistence = StatePersistence(state, tmp_path / "state.json")
    persistence.save()

    def operation(index: int) -> None:
        # No unsynchronized external state mutations: those are not supported.
        if index % 2:
            persistence.save()
        else:
            assert persistence.load() is state
        data = json.loads(persistence.path.read_text(encoding="utf-8"))
        assert data == {"energy": 0.5, "mood": "steady", "anchors": ["be honest"]}

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(operation, range(100)))
    assert state == FakeState()
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_save_retries_transient_windows_permission_error(tmp_path: Path) -> None:
    """#82: os.replace can raise PermissionError (WinError 5) if the
    destination has a concurrent open handle. save() must retry rather than
    surface the transient failure."""
    state = FakeState()
    persistence = StatePersistence(state, tmp_path / "state.json")

    real_replace = __import__("os").replace
    calls = {"count": 0}

    def flaky_replace(source: Any, destination: Any) -> None:
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(source, destination)

    with patch("stiracore.agent.state_persistence.os.replace", side_effect=flaky_replace):
        persistence.save()

    assert calls["count"] == 3
    data = json.loads(persistence.path.read_text(encoding="utf-8"))
    assert data == {"energy": 0.5, "mood": "steady", "anchors": ["be honest"]}


def test_save_reraises_persistent_windows_permission_error(tmp_path: Path) -> None:
    """A non-transient PermissionError must still surface after retries exhaust."""
    state = FakeState()
    persistence = StatePersistence(state, tmp_path / "state.json")

    with (
        patch(
            "stiracore.agent.state_persistence.os.replace",
            side_effect=PermissionError(5, "Access is denied"),
        ),
        pytest.raises(PermissionError),
    ):
        persistence.save()


@pytest.mark.skip(
    reason="AmygdalaPass scoring source/signatures were not supplied; "
    "a scoring test cannot be implemented without assuming its API."
)
def test_amygdala_scoring_requires_actual_api() -> None:
    """Explicit coverage gap, rather than inventing a scoring method."""


# --- Token estimate for Japanese / CJK text (docs/token-estimate.md) ---------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("日本語", 3),
        ("ひらがなカタカナ", 8),
        ("、。「」！", 5),
        ("あa", 2),
        ("記憶 memory", 4),
        ("ＡＢＣ", 3),
        ("𠀋", 1),
    ],
)
def test_estimate_tokens_counts_cjk_as_one_token(text: str, expected: int) -> None:
    assert estimate_tokens(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Be kind. Café.\n",
        "soul " * 100,
        '{"energy": 0.5, "mood": "steady"}',
        "em dash — arrow → ellipsis … quote ’ bullet ·",
        "Привет мир",
        "😀 emoji",
        "\t\n  def f(x):\n    return x // 4\n",
    ],
)
def test_estimate_tokens_without_cjk_is_unchanged(text: str) -> None:
    # Byte-identical prompts for English-only text: the old formula, exactly.
    assert estimate_tokens(text) == (len(text) + 3) // 4


def test_estimate_tokens_is_non_negative_and_monotonic() -> None:
    import random

    rng = random.Random(23)
    alphabet = "ab Zé—→😀あア漢、。ＡＢ\n{}"
    for _ in range(200):
        text = ""
        previous = estimate_tokens(text)
        assert previous == 0
        for _ in range(rng.randint(1, 40)):
            text += rng.choice(alphabet)
            current = estimate_tokens(text)
            assert current >= previous >= 0
            assert current <= len(text)
            previous = current


@pytest.mark.parametrize("budget", [0, 1, 8, 20, 50, 100, 300, 3000])
def test_japanese_soul_is_never_shortened(tmp_path: Path, budget: int) -> None:
    soul = "".join(f"私は第{i}の約束を守ります。" for i in range(80))
    (tmp_path / "soul.md").write_text(soul, encoding="utf-8")
    assembler, _, _ = make_assembler(tmp_path, max_tokens=budget, include_identity=False)
    assert estimate_tokens(assembler.build_turn_context()) <= budget
    whole = f"## SOUL\n{soul}"
    over = estimate_tokens(whole) > budget
    for prompt in (
        assembler.build_startup_context(),
        assembler.build_system_prompt(),
        assembler.build_prompt_parts().static,
    ):
        assert prompt.startswith(whole)
        if over:
            # The memory sections are dropped; the SOUL alone goes out, over budget.
            assert prompt == whole
        else:
            assert estimate_tokens(prompt) <= budget
    parts = assembler.build_prompt_parts()
    assert estimate_tokens(parts.volatile) + estimate_tokens(parts.recalled) <= budget


def test_protected_overflow_warns_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "soul.md").write_text("あ" * 40, encoding="utf-8")
    assembler, _, _ = make_assembler(tmp_path, max_tokens=12)
    with caplog.at_level("WARNING", logger="stiracore.agent.context_assembler"):
        for _ in range(3):
            prompt = assembler.build_system_prompt()
    assert prompt.startswith("## SOUL\n" + "あ" * 40 + "\n\n## Identity Anchors\n")
    assert "## Procedural Rules" not in prompt and "## Episodic Memory" not in prompt
    warnings = [r for r in caplog.records if "exceed the context budget" in r.getMessage()]
    assert len(warnings) == 1


def _rules_assembler(tmp_path: Path, rule: str, max_tokens: int) -> ContextAssembler:
    store = FakeMemoryStore()
    store.entries["procedural"] = [Entry(rule)]
    return ContextAssembler(
        "agent-a",
        store,
        FakeEngine(),
        tmp_path / "soul.md",
        max_tokens=max_tokens,
        include_identity=False,
    )


def test_japanese_rules_cut_lands_on_the_budget(tmp_path: Path) -> None:
    # "## SOUL\nS\n\n## Procedural Rules\n- " = 33 ASCII chars (8.25 tokens) + 3 wide = 11.25.
    (tmp_path / "soul.md").write_text("S", encoding="utf-8")
    assembler = _rules_assembler(tmp_path, "あ" * 40, max_tokens=12)
    assert assembler.build_startup_context() == "## SOUL\nS\n\n## Procedural Rules\n- あああ"


def test_mixed_rules_cut_keeps_estimate_within_budget(tmp_path: Path) -> None:
    (tmp_path / "soul.md").write_text("S", encoding="utf-8")
    assembler = _rules_assembler(tmp_path, "ab" + "あ" * 40, max_tokens=12)
    # 33 + 2 ASCII chars (8.75 tokens) + 3 wide = 11.75 -> 12; one more wide char would exceed.
    assert assembler.build_startup_context() == "## SOUL\nS\n\n## Procedural Rules\n- abあああ"

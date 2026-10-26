"""ADR 0026 review B1: a turn whose SOUL + Identity Anchors alone would not fit the known
model window is not sent (some local servers silently drop the oldest input = the SOUL)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore.agent.context_assembler import (
    ContextAssembler,
    ProtectedContextOverflow,
    estimate_tokens,
)
from stiracore.agent.context_management import ContextManagementSettings
from stiracore.providers.failure import MESSAGES, REASONS, describe_model_failure
from tests.test_context_assembler import make_assembler

REASON = "context_window_too_small"


def _soul(tmp_path: Path, text: str) -> None:
    (tmp_path / "soul.md").write_text(text, encoding="utf-8")


def _builds(assembler: ContextAssembler):
    return (
        assembler.build_startup_context,
        assembler.build_system_prompt,
        lambda: assembler.build_prompt_parts().static,
    )


def test_unknown_window_keeps_the_over_budget_behaviour(tmp_path: Path) -> None:
    _soul(tmp_path, "あ" * 400)
    assembler, _, _ = make_assembler(tmp_path, max_tokens=50)
    for build in _builds(assembler):
        assert "あ" * 400 in build()


def test_small_known_window_fails_every_build_with_protected_sections(tmp_path: Path) -> None:
    _soul(tmp_path, "私は約束を守ります。" * 40)  # 400 wide characters
    assembler, _, _ = make_assembler(tmp_path, context_window_tokens=200)
    for build in _builds(assembler):
        with pytest.raises(ProtectedContextOverflow) as caught:
            build()
        assert caught.value.window_tokens == 200 and caught.value.estimated_tokens > 400
        text = str(caught.value)
        for hint in ("Increase the context window", "shorten the SOUL", "larger window"):
            assert hint in text
    # Per-turn parts carry no SOUL: they still build within the budget.
    assert estimate_tokens(assembler.build_turn_context()) <= assembler.max_tokens


def test_the_limit_is_ninety_percent_of_the_window(tmp_path: Path) -> None:
    soul = "S" * 352  # "## SOUL\n" + 352 chars = 360 chars = 90 tokens
    _soul(tmp_path, soul)
    fits, _, _ = make_assembler(tmp_path, include_identity=False, context_window_tokens=100)
    assert fits.build_startup_context().startswith(f"## SOUL\n{soul}\n\n")
    _soul(tmp_path, soul + "S")  # 90.25 -> over 90% of 100
    over, _, _ = make_assembler(tmp_path, include_identity=False, context_window_tokens=100)
    with pytest.raises(ProtectedContextOverflow):
        over.build_startup_context()


def test_identity_anchors_count_as_protected(tmp_path: Path) -> None:
    _soul(tmp_path, "S" * 300)
    with_identity, _, _ = make_assembler(tmp_path, context_window_tokens=90)
    with pytest.raises(ProtectedContextOverflow):
        with_identity.build_startup_context()
    without, _, _ = make_assembler(tmp_path, include_identity=False, context_window_tokens=90)
    assert without.build_startup_context().startswith("## SOUL\n")


def test_default_claude_window_never_fires_and_prompts_are_byte_identical(
    tmp_path: Path,
) -> None:
    soul = "Be kind and careful. " * 2000  # ~42k characters, ~10.5k tokens
    _soul(tmp_path, soul)
    for budget in (8000, 500):
        plain, _, _ = make_assembler(tmp_path, max_tokens=budget)
        guarded, _, _ = make_assembler(tmp_path, max_tokens=budget, context_window_tokens=200_000)
        assert guarded.build_system_prompt() == plain.build_system_prompt()
        assert guarded.build_prompt_parts() == plain.build_prompt_parts()


def test_a_callable_window_is_read_on_every_build(tmp_path: Path) -> None:
    _soul(tmp_path, "S" * 400)
    window: dict[str, int | None] = {"value": None}
    assembler, _, _ = make_assembler(tmp_path, context_window_tokens=lambda: window["value"])
    assembler.build_system_prompt()
    window["value"] = 50
    with pytest.raises(ProtectedContextOverflow):
        assembler.build_system_prompt()
    window["value"] = 200_000
    assert "S" * 400 in assembler.build_system_prompt()


@pytest.mark.parametrize("value", [-1, 1.5, True, "8000"])
def test_invalid_window_is_rejected(tmp_path: Path, value) -> None:
    with pytest.raises(ValueError):
        make_assembler(tmp_path, context_window_tokens=value)


def test_failure_names_the_fixed_reason_without_the_exception_text() -> None:
    assert REASON in REASONS
    error = ProtectedContextOverflow(1234, 4096)
    for exc in (error, RuntimeError("wrapped")):
        if exc is not error:
            exc.__cause__ = error
        failure = describe_model_failure(exc, SimpleNamespace(provider="llamacpp"))
        assert failure is not None and failure["reason"] == REASON
        assert failure["code"] == "model_failure" and "http_status" not in failure
        assert failure["error"].startswith(" / ".join(MESSAGES[REASON]))
        assert "1234" not in failure["error"] and "4096" not in failure["error"]


def test_catalogs_and_chat_core_know_the_reason() -> None:
    root = Path(__file__).resolve().parents[1]
    for lang in ("ja", "en"):
        catalog = json.loads(
            (root / f"dashboard/static/i18n/catalog.{lang}.json").read_text(encoding="utf-8")
        )
        assert catalog[f"chat.model_failure_{REASON}"]
    assert f'"{REASON}"' in (root / "dashboard/static/js/chat_core.js").read_text(encoding="utf-8")


def test_server_window_is_none_unless_resolved() -> None:
    from dashboard import stira_server

    agent = SimpleNamespace(
        context_management=ContextManagementSettings(), model=None, capabilities=None
    )
    assert stira_server._known_context_window(agent) is None  # only the 200k default
    agent.context_management = ContextManagementSettings(context_window_tokens=4096)
    assert stira_server._known_context_window(agent) == 4096
    agent.model = SimpleNamespace(context_window_limit=8192)
    agent.context_management = ContextManagementSettings()
    assert stira_server._known_context_window(agent) == 8192
    assert stira_server._known_context_window(SimpleNamespace()) is None


def test_chat_turn_with_a_too_small_window_fails_explicitly(tmp_path: Path) -> None:
    """The real server: the turn is not sent to the model and the chat names the reason."""
    from dashboard import stira_server
    from tests.provider_fakes import FakeModel

    model = FakeModel([[{"text": "should not be sent"}]])
    model.config["context_window_limit"] = 64  # the model's own window (strands Model)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
        patch.setattr(stira_server, "model_kwargs", lambda *a, **k: {"model": model})
        app = stira_server.create_app()
        body = app.test_client().post("/api/chat", json={"message": "hi"}).get_data(as_text=True)
    errors = []
    for block in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if lines.get("event") == "error":
            errors.append(json.loads(lines["data"]))
    assert len(errors) == 1 and errors[0]["reason"] == REASON
    assert "should not be sent" not in body


# --- ADR 0026 re-review B1: the floor is SOUL + Identity Anchors + the tool definitions --------


class _Tool:
    def __init__(self, name: str, chars: int) -> None:
        self.tool_name = name
        self.tool_spec = {
            "name": name,
            "description": "d" * chars,
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }


def test_tool_definition_tokens_is_the_json_estimate() -> None:
    from stiracore.agent.context_assembler import tool_definition_tokens

    tools = [_Tool("a", 100), _Tool("b", 0), SimpleNamespace(tool_spec=None), object()]
    expected = sum(estimate_tokens(json.dumps(t.tool_spec)) for t in tools[:2])
    assert tool_definition_tokens(tools) == expected
    assert tool_definition_tokens([]) == 0


def test_soul_fits_but_soul_plus_tools_do_not(tmp_path: Path) -> None:
    from stiracore.agent.context_assembler import tool_definition_tokens

    # The review's case: an 8k window, ~3k of SOUL, ~8k of tool definitions.
    _soul(tmp_path, "S" * 12_000)
    assembler, _, _ = make_assembler(tmp_path, include_identity=False)
    heavy = [_Tool(f"t{i}", 4_000) for i in range(8)]  # ~8k tokens
    light = heavy[:1]  # what a smaller tool set (e.g. context_profile=small) sends
    assembler.check_protected_fits(8_000, 0)  # the SOUL alone fits
    with pytest.raises(ProtectedContextOverflow) as caught:
        assembler.check_protected_fits(8_000, tool_definition_tokens(heavy))
    assert caught.value.tool_tokens == tool_definition_tokens(heavy)
    assert "context_profile=small or tiny" in str(caught.value)
    assembler.check_protected_fits(8_000, tool_definition_tokens(light))
    assembler.check_protected_fits(200_000, tool_definition_tokens(heavy))
    assembler.check_protected_fits(None, tool_definition_tokens(heavy))  # unknown: no check


def test_failure_text_names_the_tool_definitions_and_the_profile() -> None:
    ja, en = MESSAGES[REASON]
    assert "ツール定義" in ja and "context_profile=small/tiny" in ja
    assert "tool definitions" in en and "context_profile=small or tiny" in en


def _server(tmp_path: Path, model, patch: pytest.MonkeyPatch):
    """The real app; ``patch`` must stay active while it serves (DATA_DIR is read lazily)."""
    from dashboard import stira_server

    seen: dict = {}
    wire = stira_server._wire_system_prompt

    def capture(agent, *args, **kwargs):
        seen["agent"] = agent
        seen["assembler"] = wire(agent, *args, **kwargs)
        return seen["assembler"]

    patch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    patch.setattr(stira_server, "model_kwargs", lambda *a, **k: {"model": model})
    patch.setattr(stira_server, "_wire_system_prompt", capture)
    return stira_server.create_app(), seen


def _errors(body: str) -> list[dict]:
    errors = []
    for block in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if lines.get("event") == "error":
            errors.append(json.loads(lines["data"]))
    return errors


def test_server_refuses_when_the_tools_tip_it_over_and_passes_with_fewer_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from stiracore.agent.context_assembler import tool_definition_tokens
    from tests.provider_fakes import FakeModel, text_turn

    model = FakeModel([text_turn("sent with all tools"), text_turn("sent with fewer tools")])
    app, seen = _server(tmp_path, model, monkeypatch)
    agent, assembler = seen["agent"], seen["assembler"]
    sent: list[int] = []
    check = agent.send_boundary_check

    def recording(turn_tools, identity_notice=""):
        sent.append(tool_definition_tokens(turn_tools))
        check(turn_tools, identity_notice)

    agent.send_boundary_check = recording
    client = app.test_client()
    # A 200k window never fires with every tool.
    model.config["context_window_limit"] = 200_000
    body = client.post("/api/chat", json={"message": "hi"}).get_data(as_text=True)
    assert _errors(body) == [] and "sent with all tools" in body
    tools = sent[-1]
    assert tools > 1000  # the default tool set is large (measured ~3.5k-4.3k)
    with pytest.raises(ProtectedContextOverflow) as probe:
        assembler.check_protected_fits(1)
    soul = probe.value.estimated_tokens  # SOUL (+ Anchors when included), measured
    assert soul > 1000
    # A window the SOUL alone fits (well under 90%) but SOUL + the tool definitions do not.
    window = int((soul + tools) / 0.9) - 50
    assert soul < 0.9 * window
    model.config["context_window_limit"] = window
    requests = len(model.requests)
    body = client.post("/api/chat", json={"message": "hi"}).get_data(as_text=True)
    assert [e["reason"] for e in _errors(body)] == [REASON]
    assert len(model.requests) == requests  # nothing reached the model
    # Fewer tool definitions, as a smaller context profile's turn filter sends (#368 filters
    # in NXPAgent._turn_tools; the server reassigns agent.tools per turn): the turn goes out.
    every = agent._turn_tools
    monkeypatch.setattr(agent, "_turn_tools", lambda breaker: every(breaker)[:2])
    body = client.post("/api/chat", json={"message": "hi"}).get_data(as_text=True)
    assert _errors(body) == [] and "sent with fewer tools" in body
    assert sent[-1] < tools


# --- r578b blocker: the identity notice NXPAgent prepends counts toward the floor ------------

NOTICE = "[STIRA SAFETY NOTICE]\nYour Identity Anchors:\n" + "1. be honest\n" * 40


def _seam(assembler: ContextAssembler, notice: str) -> tuple[int, int]:
    """(window, tools): SOUL + tools fit under 90%; adding the notice goes over."""
    with pytest.raises(ProtectedContextOverflow) as probe:
        assembler.check_protected_fits(1)
    soul = probe.value.estimated_tokens
    window = 10_000
    tools = int(0.9 * window) - soul - estimate_tokens(notice) // 2
    return window, tools


def test_assembler_without_identity_counts_the_prepended_notice(tmp_path: Path) -> None:
    _soul(tmp_path, "S" * 4_000)
    assembler, _, _ = make_assembler(tmp_path, include_identity=False)
    window, tools = _seam(assembler, NOTICE)
    assembler.check_protected_fits(window, tools)  # no notice this turn (NORMAL): fits
    assembler.check_protected_fits(window, tools, "")
    with pytest.raises(ProtectedContextOverflow) as caught:
        assembler.check_protected_fits(window, tools, NOTICE)
    assert caught.value.estimated_tokens == estimate_tokens(NOTICE + "\n\n## SOUL\n" + "S" * 4_000)


def test_assembler_with_identity_counts_its_section_and_never_twice(tmp_path: Path) -> None:
    from tests.test_context_assembler import FakeEngine, FakeMemoryStore

    _soul(tmp_path, "S" * 4_000)
    without, _, _ = make_assembler(tmp_path, include_identity=False)
    window, tools = _seam(without, NOTICE)
    engine = FakeEngine()
    engine.identity = NOTICE  # the assembler's own Identity Anchors section
    with_identity = ContextAssembler("agent-a", FakeMemoryStore(), engine, tmp_path / "soul.md")
    with pytest.raises(ProtectedContextOverflow) as plain:
        with_identity.check_protected_fits(window, tools)
    # Passing the notice as well does not add it a second time.
    with pytest.raises(ProtectedContextOverflow) as once:
        with_identity.check_protected_fits(window, tools, NOTICE)
    assert once.value.estimated_tokens == plain.value.estimated_tokens
    # A window a single count fits but a double count would not: accepted.
    roomy = int((plain.value.estimated_tokens + tools) / 0.9) + 5
    with_identity.check_protected_fits(roomy, tools, NOTICE)


def test_nxp_agent_passes_the_notice_it_prepends() -> None:
    from stiracore.agent.nxp_agent import NXPAgent
    from stiracore.core.engine import BreakAction

    agent = NXPAgent.__new__(NXPAgent)
    agent.engine = SimpleNamespace(identity_injection=lambda: NOTICE)
    inject = SimpleNamespace(actions=BreakAction.INJECT_IDENTITY)
    normal = SimpleNamespace(actions=BreakAction(0))
    assert agent._identity_notice(inject) == NOTICE
    assert agent._identity_notice(normal) == ""
    assert agent._with_identity(inject, "base") == f"{NOTICE}\n\nbase"
    assert agent._with_identity(normal, "base") == "base"


def test_server_check_counts_the_notice_at_the_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server path (include_identity=False): the same seam refuses only with the notice."""
    from tests.provider_fakes import FakeModel

    model = FakeModel()
    _, seen = _server(tmp_path, model, monkeypatch)
    agent, assembler = seen["agent"], seen["assembler"]
    assert assembler.include_identity is False
    window, tools = _seam(assembler, NOTICE)
    model.config["context_window_limit"] = window
    heavy = [_Tool("t", 4 * tools - 120)]
    from stiracore.agent.context_assembler import tool_definition_tokens

    assert abs(tool_definition_tokens(heavy) - tools) <= 30
    agent.send_boundary_check(heavy, "")
    with pytest.raises(ProtectedContextOverflow):
        agent.send_boundary_check(heavy, NOTICE)

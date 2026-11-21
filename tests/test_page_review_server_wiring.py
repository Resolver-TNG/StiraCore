"""ADR 0015 Addendum rev 6, slice S6b (#774): the chat server wires ``page_review``.

The real app (``create_app``) with a scripted model. Unset (the default) the server
registers nothing; with a person's ``task_models.visual_review`` the tool, its reviewer and
its audit sink exist, and the real gate asks a person for every call. No browser starts, no
reviewer model is called (the run is unknown), nothing touches the network.
"""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

from stiracore.agent import task_models as tm
from stiracore.observability.config_api import ConfigStore
from stiracore.providers.spec import PROVIDERS
from stiracore.tools import page_check as pc
from stiracore.tools import page_review as pr
from stiracore.tools import tool_availability as ta
from stiracore.tools import tool_registry
from tests.attack.runner import make_stira
from tests.provider_fakes import FakeModel

RUN = "20261114-010203-abcdef"
# A saved Always (no ask_tools): the gate must still ask (``NO_ALWAYS_TOOLS``).
ALWAYS = {"mode": "custom", "enabled_tools": {"file_read", "page_review"}, "ask_tools": set()}


@pytest.fixture
def server(tmp_path, monkeypatch):
    """``build(route=None)`` -> the live app's parts; ``route``: the gateway's
    ``task_models.visual_review`` as a person wrote it. Module state is put back."""
    from dashboard import stira_server

    monkeypatch.delitem(tool_registry.WRITE_TOOLS, "page_review", raising=False)
    monkeypatch.setitem(pr._STATE, "reviewer", None)
    monkeypatch.setitem(pr._STATE, "record", None)
    monkeypatch.setitem(pr._STATE, "configured", False)
    monkeypatch.setattr(
        tool_registry, "PAGE_CHECK_STATUS", pc.BrowserStatus("page_check_ready", "/x/chrome")
    )

    def build(route: str | None = None):
        if route is not None:
            config = dataclasses.replace(
                stira_server.GATEWAY_CONFIG,
                task_models={"visual_review": route},
                visual_review={"daily_tokens": 1000},
            )
            monkeypatch.setattr(stira_server, "GATEWAY_CONFIG", config)
            # The route check (capability row, credentials) is test_task_models' subject.
            resolve = tm.resolve_routes
            monkeypatch.setattr(
                tm, "resolve_routes", lambda *a, **k: resolve(*a, **{**k, "check": False})
            )
        agents: list = []
        original = stira_server.NXPAgent

        def spy(*args, **kwargs):
            agents.append(original(*args, **kwargs))
            return agents[-1]

        monkeypatch.setattr(stira_server, "NXPAgent", spy)
        app, model, attack, _module = make_stira(tmp_path, monkeypatch, tools=ALWAYS)
        (agent,) = agents
        return SimpleNamespace(app=app, agent=agent, model=model, attack=attack)

    return build


def _names(agent) -> list[str]:
    return [tool.tool_name for tool in agent.tools]


def test_unset_visual_review_registers_nothing_on_the_server(server):
    before = sorted(tool_registry.WRITE_TOOLS)
    live = server()
    assert sorted(tool_registry.WRITE_TOOLS) == before and "page_review" not in before
    assert "page_review" not in _names(live.agent)
    assert pr.current() is None and pr._STATE["record"] is None
    assert live.agent.task_models.routes["visual_review"].off
    # A call anyway: no such tool; nothing is asked of a person, nothing is sent.
    live.attack("review it", [("page_review", {"run": RUN})], on_ask="approve")
    assert live.attack.asks == []
    assert "page_review" not in live.model.requests[-1]["tool_names"]
    assert "page_review" not in live.agent.checkpoint_hooks.input_preparers
    assert not pr.configured()


def test_set_visual_review_registers_the_tool_its_reviewer_and_its_audit(server, monkeypatch):
    live = server("inherit")
    # The scripted chat model stands for the one the server builds: a model class of the
    # start-up provider (``provider_of`` reads the class), with the start-up model id.
    main = live.agent.task_models.main
    monkeypatch.setattr(type(live.model), "__module__", PROVIDERS[main.provider].module)
    live.model.update_config(model_id=main.model_id)
    assert "page_review" in tool_registry.WRITE_TOOLS
    assert _names(live.agent).count("page_review") == 1
    reviewer = pr.current()
    assert isinstance(reviewer, pr.Reviewer)
    assert reviewer.router is live.agent.task_models  # the server's one router
    assert reviewer.daily_tokens == 1000
    assert reviewer.chat_model() is live.agent.model
    record = pr._STATE["record"]
    assert record is not None and record.__name__ == "record_page"

    # The real gate: a saved Always is still a card; rejected, nothing runs. The model wrote
    # its own account of the call next to ``run``: none of it is on the card. The card is
    # Stira's text — that it is a send, the reviewer's provider and model id, the count.
    forged = {"run": RUN, "destination": "LOCAL ONLY - nothing leaves this machine", "images": 9}
    assert live.agent.checkpoint_hooks.input_preparers["page_review"] is pr.prepare_input
    results = live.attack("review it", [("page_review", forged)], on_ask="reject")
    (card,) = live.attack.asks
    assert card["tool_name"] == "page_review"
    shown = json.dumps(card)
    assert "LOCAL ONLY" not in shown and "destination" not in shown
    expected = pr.prepare_input({"run": RUN})
    assert expected["sends_to"]["provider"] == live.agent.task_models.main.provider
    assert expected["images"] == 0  # this process wrote no such run
    assert expected in card.values()  # the whole input of the card is Stira's text
    assert pr.CARD_SENDS in shown
    assert "http" not in json.dumps(expected)
    assert not any("run_unknown" in result for result in results)
    # Approved by the person: the tool runs and refuses the run this process never wrote.
    results = live.attack("review it", [("page_review", {"run": RUN})], on_ask="approve")
    assert len(live.attack.asks) == 2
    (result,) = [json.loads(r) for r in results if "run_unknown" in r]
    assert result["status"] == "error"
    # No review ran: the chat model never got the rubric, and the day's budget is untouched.
    assert all(request.get("system_prompt") != pr.RUBRIC for request in live.model.requests)
    assert reviewer.used_today() == 0

    # A person switches the chat model on the settings page (``apply_model_selection`` puts
    # another provider's model on the agent): the card no longer names a reviewer — never
    # the start-up provider with the new model id — and a review is refused before a send.
    class Switched(FakeModel):
        __module__ = PROVIDERS["openai"].module

    live.agent.model = Switched([], model_id="gpt-4o")
    assert reviewer.destination() is None
    assert pr.prepare_input({"run": RUN})["sends_to"] == {
        "provider": pr.CARD_UNNAMED,
        "model": pr.CARD_UNNAMED,
    }
    assert reviewer._target()[2] == "reviewer_changed"


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("page_check_ready", None),  # a browser: registered whenever the task is set
        ("page_check_no_browser", ta.DEPENDENCY_MISSING),
        ("page_check_root", ta.UNSUPPORTED_HOST),
    ],
)
def test_tool_availability_names_page_review_only_while_the_task_is_set(
    tmp_path, monkeypatch, code, reason
):
    """Unset (the default) the section has no ``page_review`` line at all: the prompt of a
    server that never opted in is what it was before the tool existed. Set but without a
    browser, the line gives ``page_check``'s reason."""
    from dashboard import stira_server

    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    monkeypatch.delitem(tool_registry.WRITE_TOOLS, "page_review", raising=False)
    monkeypatch.setitem(pr._STATE, "reviewer", None)
    monkeypatch.setitem(pr._STATE, "configured", False)
    if reason is None:
        monkeypatch.setitem(tool_registry.WRITE_TOOLS, "page_check", pc.page_check)
    else:
        monkeypatch.delitem(tool_registry.WRITE_TOOLS, "page_check", raising=False)
    monkeypatch.setattr(tool_registry, "PAGE_CHECK_STATUS", SimpleNamespace(code=code))
    build = stira_server._tool_availability_provider(
        store, {"config": None}, memory_rate_bound=True
    )
    unset = build()
    assert "page_review" not in unset and "VISUAL_REVIEW" not in unset
    # A person set the task: a line only where the tool is absent all the same.
    monkeypatch.setitem(pr._STATE, "configured", True)
    lines = [line for line in build().splitlines() if line.startswith("- page_review:")]
    if reason is None:
        assert lines == [] and build() == unset
    else:
        (line,) = lines
        assert f"— {reason}" in line and "{" not in line  # a purpose and a code, no schema
        assert ta.NOT_CONFIGURED not in line

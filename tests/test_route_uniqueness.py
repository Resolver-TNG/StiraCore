"""#703: no rule × method is registered twice in an app's ``url_map``.

Flask raises nothing when a second view is registered for a rule and method the app already
has: both rules stay in the ``url_map``, the one registered first answers and the other is
dead code that still looks live (#286 was ``/api/turns``). Blueprint factories in
``stiracore.observability`` do declare the same rules (``sse`` and ``turns_view``,
``tool_config_api`` and ``config_api``); each app registers one of a pair, and these tests
keep it that way for every route of the two servers.

Offline: the apps of ``tests/test_admin_api_permissions.py`` and ``tests/test_yolo_ui.py``
(no model is called).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from flask import Flask

from tests.test_admin_api_permissions import stira_app  # noqa: F401 - the fixture
from tests.test_yolo_ui import make_app  # noqa: F401 - the fixture


def _duplicates(app) -> dict[tuple[str, str], list[str]]:
    """(rule, method) -> its endpoints, for every pair registered more than once."""
    owners: dict[tuple[str, str], list[str]] = {}
    for rule in app.url_map.iter_rules():
        # Flask adds HEAD (to GET) and OPTIONS itself; no view registers them.
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            owners.setdefault((rule.rule, method), []).append(rule.endpoint)
    return {pair: sorted(names) for pair, names in owners.items() if len(names) > 1}


def _rules(app) -> set[str]:
    return {rule.rule for rule in app.url_map.iter_rules()}


def test_flask_accepts_a_second_view_and_the_first_one_answers():
    """Why this file exists: nothing fails at start, so only a test sees the duplicate."""
    app = Flask(__name__)
    app.add_url_rule("/twice", "first", lambda: "first")
    app.add_url_rule("/twice", "second", lambda: "second")
    app.add_url_rule("/twice", "post", lambda: "post", methods=["POST"])  # another method
    assert _duplicates(app) == {("/twice", "GET"): ["first", "second"]}
    assert app.test_client().get("/twice").get_data(as_text=True) == "first"


def test_no_rule_is_registered_twice_in_the_chat_server(stira_app):  # noqa: F811
    assert _duplicates(stira_app) == {}


def test_no_rule_is_registered_twice_with_the_stage_and_yolo_routes(make_app):  # noqa: F811
    app = make_app("internal")  # STIRA_STAGE=1 and the YOLO allowance: their routes too
    assert {"/stage/", "/api/yolo/status"} <= _rules(app)
    assert _duplicates(app) == {}


@pytest.mark.parametrize("chat", [False, True])
def test_no_rule_is_registered_twice_in_the_observer(tmp_path, chat):
    from dashboard.app import create_app

    # chat_agent adds the chat blueprint and drops the observer's own /chat redirect.
    app = create_app(
        tmp_path / "state.json",
        tmp_path / "gw.json",
        tmp_path / "agent.json",
        tmp_path / "t.json",
        chat_agent=Mock() if chat else None,
    )
    assert ("/api/chat" in _rules(app)) is chat
    assert _duplicates(app) == {}

"""ADR 0012 phase 5b on the real chat server app (offline, no model call, browser channel):
the pages carry the YOLO button / badge slot and its script only with the allowance, the
status answer says when the button is offered, and an automatic approval of a real turn
(scripted model, one ``file_write``) becomes one log line in ``status.auto_approvals``.
The rendering itself is pinned by ``tests/yolo_ui_test.cjs``.
"""

from __future__ import annotations

import json
import re
from unittest.mock import Mock

import pytest

from stiracore.yolo import own_dashboard, runtime
from stiracore.yolo.allowance import YoloSettings
from tests.provider_fakes import FakeModel, text_turn, tool_turn
from tests.test_yolo_browser_confirm import BROWSER
from tests.test_yolo_window_routes import _gate_call, _open

SLOT = re.compile(r"""class="yolo-slot""")
SCRIPT = "js/yolo_ui.js"


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(own_dashboard, "armed", lambda: object())
    monkeypatch.setenv("STIRA_STAGE", "1")

    def make(allowance="internal", model=None):
        from dashboard import stira_server
        from stiracore.agent import nxp_agent, stimulus_classifier
        from stiracore.agent.stimulus_classifier import StimulusResult
        from stiracore.observability.config_api import ConfigStore

        base = tmp_path / str(len(made))
        monkeypatch.setattr(stira_server, "DATA_DIR", base / "data")
        monkeypatch.setattr(
            nxp_agent, "BedrockModel", Mock(return_value=model if model else Mock())
        )
        if model is None:
            monkeypatch.setattr(
                nxp_agent, "Agent", Mock(side_effect=AssertionError("model called"))
            )
        monkeypatch.setattr(
            stimulus_classifier,
            "get_classifier",
            lambda: Mock(
                model_id=None,
                classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5)),
            ),
        )
        runtime._reset_for_tests(YoloSettings(allowance, host_confirm="browser"))
        store = ConfigStore(base / "gw.json", base / "agent.json", base / "tools.json")
        real = stira_server.NXPAgent
        agents = []

        def capture(*args, **kwargs):
            agents.append(real(*args, **kwargs))
            return agents[-1]

        monkeypatch.setattr(stira_server, "NXPAgent", capture)
        app = stira_server.create_app(config_store=store)
        monkeypatch.setattr(stira_server, "NXPAgent", real)
        app.extensions["test_agent"] = agents[0]
        made.append(app)
        return app

    try:
        yield make
    finally:
        for app in made:
            app.extensions["lifecycle"].shutdown()
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


@pytest.mark.parametrize("page", ["/chat", "/stage/"])
def test_without_the_allowance_no_page_carries_yolo(make_app, page):
    app = make_app("off")
    client = app.test_client()
    html = client.get(page).get_data(as_text=True)
    assert client.get(page).status_code == 200
    # (the i18n catalog embedded in every page names YOLO keys; no element, no script)
    assert not SLOT.search(html) and SCRIPT not in html and 'id="yolo-' not in html
    assert client.get("/api/yolo/status").status_code == 404


@pytest.mark.parametrize("page", ["/chat", "/stage/"])
def test_with_the_allowance_the_page_has_one_slot_and_the_script(make_app, page):
    client = make_app("internal").test_client()
    html = client.get(page).get_data(as_text=True)
    assert len(SLOT.findall(html)) == 1 and html.count(SCRIPT) == 1


def test_offered_follows_the_request_the_window_and_the_latch(make_app):
    from tests.test_yolo_browser_confirm import _press

    client = make_app("internal").test_client()
    assert client.get("/api/yolo/status").get_json()["offered"] is True
    _press(client, "internal")
    pending = client.get("/api/yolo/status").get_json()
    assert pending["offered"] is False and pending["unavailable"] == ["yolo_request_pending"]
    client.post("/api/yolo/stop")
    stopped = client.get("/api/yolo/status").get_json()
    assert stopped["offered"] is True, stopped
    _open(client)
    assert client.get("/api/yolo/status").get_json()["unavailable"] == ["yolo_active"]
    # An abnormal end latches: no button until unlatch (the badge offers the unlatch).
    runtime.desk().yolo.fail_closed("revoked_clock_anomaly")
    latched = client.get("/api/yolo/status").get_json()
    assert latched["offered"] is False and "yolo_latched" in latched["unavailable"]
    assert latched["latches"][0]["subject"] == "window"


def test_an_automatic_approval_is_one_log_line_and_a_voided_one_none(make_app, monkeypatch):
    from stiracore.yolo import origin

    app = make_app("internal")
    client = app.test_client()
    agent = app.extensions["test_agent"]
    workspace = agent.write_policy.workspace
    opened = _open(client)
    with origin.human_turn(agent.agent_id):
        event = _gate_call(agent, "file_write", {"path": str(workspace / "a.md"), "content": "x"})
    assert event.cancel_tool is None
    lines = client.get("/api/yolo/status").get_json()["auto_approvals"]
    assert lines["window"] == opened["window"]["id"]
    [line] = lines["items"]
    assert set(line) == {"n", "tool", "ref", "limit", "tier"}
    assert (line["n"], line["tool"], line["limit"], line["tier"]) == (
        1,
        "file_write",
        2000,
        "internal",
    )
    assert re.fullmatch(r"[0-9a-f]{4}", line["ref"])
    # Approved but never consumed (a later check refused it): its slot returns, no line.
    with origin.human_turn(agent.agent_id):
        grant, _ = runtime.desk().yolo.approve(
            tool_name="file_write",
            args_digest="0" * 64,
            tool_use_id="voided-1",
            level="normal",
            agent_id=agent.agent_id,
        )
    assert grant is not None and runtime.desk().yolo.void(grant)
    assert len(client.get("/api/yolo/status").get_json()["auto_approvals"]["items"]) == 1
    # Stop: the lines of that window stay until the next one opens.
    client.post("/api/yolo/stop")
    ended = client.get("/api/yolo/status").get_json()
    assert ended["window"] is None and len(ended["auto_approvals"]["items"]) == 1
    _open(client)
    assert client.get("/api/yolo/status").get_json()["auto_approvals"]["items"] == []


def test_a_scripted_turn_with_one_file_write_gives_one_log_line(make_app, tmp_path):
    """The fake model asks for one ``file_write`` inside the workspace in a real chat turn
    (``/api/chat``, the Stage's route too): no card, the file is written, one line."""
    model = FakeModel([])
    app = make_app("internal", model=model)
    client = app.test_client()
    ask = client.get("/api/tools/config").get_json()["ask_tools"]
    assert "file_write" in ask, ask  # a fresh install asks before a local write
    workspace = app.extensions["test_agent"].write_policy.workspace
    target = workspace / "yolo-5b.md"
    model.script = [
        tool_turn("file_write", {"path": str(target), "content": "yolo"}, "use-5b"),
        text_turn("written"),
    ]
    _open(client)
    response = client.post(
        "/api/chat",
        json={"message": "write it"},
        headers={
            **BROWSER,
            "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"],
            "Idempotency-Key": "t5b",
        },
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200, body
    assert target.read_text() == "yolo"
    assert app.extensions["hitl_broker"].pending() == []
    status = client.get("/api/yolo/status").get_json()
    assert status["window"]["used"] == 1
    [line] = status["auto_approvals"]["items"]
    assert (line["n"], line["tool"], line["limit"]) == (1, "file_write", 2000)


def test_a_scripted_external_call_in_an_external_window_on_the_real_app(make_app, monkeypatch):
    """Phase 6 on the real app: the fake model asks for ``web_search`` (an external tool,
    Ask on a fresh install) in a window of tier external — no card, the tool runs (an
    endpoint is configured, #583, but the download is stubbed: no network access), one
    line that says external and counts the sub-cap."""
    from stiracore.tools import web_tools

    monkeypatch.setenv("STIRA_WEB_SEARCH_ENDPOINT", "https://search.example/q")
    downloads = []

    def stub(url, params=None):
        downloads.append((url, params))
        return '{"results": []}', "application/json", url

    monkeypatch.setattr(web_tools, "_download", stub)
    model = FakeModel([])
    app = make_app("external", model=model)
    client = app.test_client()
    assert "web_search" in client.get("/api/tools/config").get_json()["ask_tools"]
    model.script = [
        tool_turn("web_search", {"query": "release notes"}, "use-6"),
        text_turn("searched"),
    ]
    from tests.test_yolo_browser_confirm import _confirm_request, _press

    data = _press(client, "external")
    confirmed = _confirm_request(client, data, external_acknowledged=True)
    assert confirmed.status_code == 200, confirmed.get_json()
    opened = client.get("/api/yolo/status").get_json()
    assert opened["window"]["tier"] == "external"
    assert opened["window"]["external_limit"] == 200
    response = client.post(
        "/api/chat",
        json={"message": "search it"},
        headers={
            **BROWSER,
            "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"],
            "Idempotency-Key": "t6",
        },
    )
    body = response.get_data(as_text=True)  # the turn runs while the stream is read
    assert response.status_code == 200, body
    assert app.extensions["hitl_broker"].pending() == []
    status = client.get("/api/yolo/status").get_json()
    window = status["window"]
    assert (window["used"], window["external_used"], window["effective_tier"]) == (
        1,
        1,
        "external",
    )
    [line] = status["auto_approvals"]["items"]
    assert (line["tool"], line["tier"], line["external_n"], line["external_limit"]) == (
        "web_search",
        "external",
        1,
        200,
    )
    # The tool ran (the stubbed download, no network), and no argument text reaches the
    # status answer the page draws.
    assert downloads == [("https://search.example/q", {"q": "release notes"})]
    assert "release notes" not in json.dumps(status)


def test_the_destination_host_never_reaches_the_page_the_log_or_the_status(
    make_app, monkeypatch, caplog
):
    """PR #438 review (minor): beyond ``lineText``'s unit test, a real ``/api/chat`` turn
    with one external call to a named host — the host is in no page, no stream, no status
    answer, no log line and no audit row (only its keyed hash is stored)."""
    import logging

    host = "collector-unique.invalid"  # never resolves: nothing leaves this machine
    model = FakeModel([])
    app = make_app("external", model=model)
    client = app.test_client()
    model.script = [
        tool_turn("web_fetch", {"url": f"https://{host}/feed"}, "use-6b"),
        text_turn("fetched"),
    ]
    from tests.test_yolo_browser_confirm import _confirm_request, _press

    caplog.set_level(logging.INFO)  # the SDK's own DEBUG lines echo any tool input
    data = _press(client, "external")
    assert _confirm_request(client, data, external_acknowledged=True).status_code == 200
    response = client.post(
        "/api/chat",
        json={"message": "fetch it"},
        headers={
            **BROWSER,
            "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"],
            "Idempotency-Key": "t6b",
        },
    )
    stream = response.get_data(as_text=True)
    assert response.status_code == 200, stream
    status = client.get("/api/yolo/status").get_json()
    [line] = status["auto_approvals"]["items"]  # approved: the call was not a card
    assert (line["tool"], line["tier"], line["external_n"]) == ("web_fetch", "external", 1)
    assert app.extensions["hitl_broker"].pending() == []
    pages = "".join(client.get(page).get_data(as_text=True) for page in ("/chat", "/stage/"))
    audit_log = json.dumps(status) + caplog.text  # every INFO+ server log line
    assert "yolo-slot" in pages  # the real pages were read
    for clear in (host, "collector-unique"):
        assert clear not in pages, "page"
        assert clear not in status.__repr__(), "status"
    # The YOLO log line is drawn from the status items alone: no host, URL or argument.
    assert set(line) == {"n", "tool", "ref", "limit", "tier", "external_n", "external_limit"}
    yolo_lines = [r.getMessage() for r in caplog.records if "Auto-approved" in r.getMessage()]
    assert yolo_lines and all(host not in text for text in yolo_lines)
    assert "collector-unique" not in audit_log

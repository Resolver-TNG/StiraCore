"""ADR 0012 phase 6: the external tier is in effect (§4, §10, §11 Open (10), §12).

In a window of tier **external**, the Ask of a tool in ``EXTERNAL_TOOLS`` is approved
automatically in a human turn, under every mitigation of §10:

* arguments that ``mask_secrets`` would change (a URL query, userinfo, a header, a
  body, a host name, a secret-named key) — a person's card; also a credential taken from
  the host by reference, and arguments that are not plain JSON;
* the external sub-cap (``STIRA_YOLO_MAX_EXTERNAL``): used up or temporarily full —
  external tools ask again, the window stays active for internal tools;
* the destination only as a keyed hash in ``audit.yolo.auto_approved`` (Open (10) (b));
  never in clear in a row, the server log, the status answer or the log line;
* Never, the floors, the breaker, ``CRITICAL_HALT``, exec and MCP stay a person's.

Each check is shown to be the reason: the same call passes when only that check is
removed (monkeypatched). Offline, fake clocks and the fake audit chain of phase 3.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from stiracore.core import hitl_reasons
from stiracore.tools import tool_registry
from stiracore.tools.tool_consent import EXEC_TOOLS
from stiracore.yolo import origin, tiers
from stiracore.yolo.audit_schema import KNOWN_TOOLS
from stiracore.yolo.tiers import (
    EXTERNAL_TOOLS,
    destination,
    external_refusal,
    scope_refusal,
)
from stiracore.yolo.window import WindowMarker
from tests.test_yolo_auto_approve import (
    AGENT,
    Breaker,
    _approve,
    _call,
    _execute,
    _gate,
    _scope,
    _window,
)
from tests.test_yolo_host_ops import Clocks
from tests.test_yolo_requests import Recorder
from tests.test_yolo_window import _open, _service

HOST = "collector.example.org"


@pytest.fixture
def clock():
    return Clocks()


@pytest.fixture
def audit():
    return Recorder()


@pytest.fixture
def breaker():
    return Breaker()


@pytest.fixture
def yolo(audit, clock, tmp_path, breaker):
    return _service(audit, clock, WindowMarker(tmp_path), breaker=breaker)


@pytest.fixture
def human():
    with origin.human_turn(AGENT):
        yield


def _ext(audit, clock, tmp_path, external_cap, cap=2000):
    return _service(audit, clock, WindowMarker(tmp_path), cap=cap, external_cap=external_cap)


# --- §4: the class table, and what each tier answers ----------------------------------------


def test_the_external_class_is_the_network_egress_row_plus_perplexity():
    """``docs/tool-execution-paths.md``'s "network egress" row and ``perplexity_search``
    (§4); every one is a registry name a row may carry in clear."""
    from pathlib import Path

    doc = Path(__file__).resolve().parents[1] / "docs" / "tool-execution-paths.md"
    row = next(line for line in doc.read_text().splitlines() if "network egress" in line)
    listed = {part.strip(" `") for part in row.split("|")[1].split(",")}
    assert EXTERNAL_TOOLS == listed | {"perplexity_search"}
    assert EXTERNAL_TOOLS <= KNOWN_TOOLS


_PLAIN = {
    "http_request": {"method": "GET", "url": f"https://{HOST}/feed"},
    "web_fetch": {"url": f"https://{HOST}/page"},
    "web_search": {"query": "weather tomorrow"},
    "webhook_send": {"url": f"https://{HOST}/hook", "payload": {"text": "done"}},
    "oauth_api_call": {"provider": "github", "method": "GET", "url": f"https://{HOST}/user"},
    "rss": {"action": "fetch", "url": f"https://{HOST}/rss.xml"},
    "speak": {"text": "hello", "mode": "fast"},
    "memory": {"action": "retrieve", "query": "notes"},
    "perplexity_search": {"query": "release notes", "count": 3},
}


def test_every_external_tool_has_a_plain_example():
    assert set(_PLAIN) == EXTERNAL_TOOLS


@pytest.mark.parametrize("name", sorted(EXTERNAL_TOOLS))
@pytest.mark.parametrize("tier", ["internal", "external"])
def test_each_external_tool_through_the_gate_in_each_tier(yolo, human, name, tier):
    _open(yolo, tier)
    hooks, _engine, asked = _gate(yolo)
    event = _call(hooks, name, dict(_PLAIN[name]))
    if tier == "external":
        assert event.cancel_tool is None and asked == [], name
        _execute(hooks, event)
        assert event.cancel_tool is None
        assert _window(yolo)["external_used"] == 1 and _window(yolo)["used"] == 1
    else:
        # A window of tier internal: every external tool keeps its card (as before).
        assert asked == [(name, asked[0][1])] and event.cancel_tool.startswith("HITL rejected")
        assert _window(yolo)["auto_approved"] == 0


@pytest.mark.parametrize(
    "name", sorted(EXEC_TOOLS | {"diagram", "page_check", "mcp_github_issue", "x"})
)
def test_exec_person_tools_and_unclassified_names_never_in_an_external_window(yolo, human, name):
    _open(yolo, "external")
    grant, code = _approve(yolo, name)
    assert grant is None and code == "tier_not_covered"


def test_an_mcp_tool_is_a_card_whatever_its_name(yolo, human, monkeypatch):
    """An MCP server that registers a listed name (``web_fetch`` when the standard one
    is not loaded, ``memory_read`` …) is never answered: the check is the reason."""
    _open(yolo, "external")
    hooks, _engine, asked = _gate(yolo)
    monkeypatch.setattr(tool_registry, "mcp_tool_names", lambda: frozenset({"web_fetch"}))
    assert scope_refusal("web_fetch", dict(_PLAIN["web_fetch"]), _scope()) == "mcp_tool"
    event = _call(hooks, "web_fetch", dict(_PLAIN["web_fetch"]))
    assert [name for name, _ in asked] == ["web_fetch"] and event.cancel_tool
    monkeypatch.setattr(tool_registry, "mcp_tool_names", lambda: frozenset())
    event = _call(hooks, "web_fetch", dict(_PLAIN["web_fetch"]))
    assert event.cancel_tool is None and len(asked) == 1


def test_mcp_tool_names_reads_the_mcp_registries(monkeypatch):
    fake = SimpleNamespace(registry={"mcp_search": object()})
    monkeypatch.setitem(tool_registry._MCP_REGISTRIES, -1, fake)
    assert "mcp_search" in tool_registry.mcp_tool_names()
    assert tiers.mcp_tool("mcp_search") is True and tiers.mcp_tool(None) is True
    assert scope_refusal("mcp_search", {}, _scope()) == "mcp_tool"


def test_a_non_human_turn_is_never_answered_in_an_external_window(yolo):
    _open(yolo, "external")
    grant, code = _approve(yolo, "web_fetch")
    assert grant is None and code == "origin_not_covered"


# --- §10: a secret in the arguments → a card -------------------------------------------------


def _j(*parts: str) -> str:
    """Secret-looking fixtures are assembled at run time, so the file itself carries no
    string a secret scanner (or a masking pipeline) would rewrite."""
    return "".join(parts)


_SECRETS = [
    ("web_fetch", {"url": _j(f"https://{HOST}/c?api", "_key=Zx81kq0PLm")}),  # query
    ("web_fetch", {"url": _j("https://user:", "hunter2pass", f"@{HOST}/")}),  # userinfo
    ("web_fetch", {"url": _j(f"https://{HOST}/", "AKIA", "IOSFODNN7EXAMPLE")}),  # path
    ("web_fetch", {"url": _j("https://sk", "-live0123456789abcdef.example.org/")}),  # host
    ("web_fetch", {"url": _j("https://gh", "p_abcdefghijklmnopqrstuvwxyz0123.example.org/")}),
    (
        "http_request",
        {
            "method": "GET",
            "url": f"https://{HOST}/",
            "headers": {_j("Author", "ization"): _j("Bea", "rer abc.def")},
        },
    ),
    (
        "http_request",
        {"method": "POST", "url": f"https://{HOST}/", "body": _j("pass", "word=correcthorse")},
    ),
    ("http_request", {"method": "GET", "url": f"https://{HOST}/", _j("auth_", "token"): "t0k-v"}),
    ("webhook_send", {"url": f"https://{HOST}/h", "payload": {_j("sec", "ret"): "s3-value"}}),
    (
        "webhook_send",
        {"url": f"https://{HOST}/h", "payload": {"text": _j("see sk", "-abcdefghijklmnop")}},
    ),
    (
        "webhook_send",
        {"url": _j("https://hooks.slack.com/services/", "T000/B000/XXXX"), "payload": {}},
    ),
    (
        "oauth_api_call",
        {
            "provider": "github",
            "method": "POST",
            "url": f"https://{HOST}/",
            "body": {_j("client_", "secret"): "abcd1234"},
        },
    ),
    (
        "rss",
        {
            "action": "subscribe",
            "url": f"https://{HOST}/r",
            "auth_username": "me",
            _j("auth_pass", "word"): "pw1234",
        },
    ),
    ("perplexity_search", {"query": _j("to", "ken=", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig")}),
    (
        "web_search",
        {
            "query": _j(
                "-----BEGIN RSA PRI", "VATE KEY-----\nMIIE\n-----END RSA PRI", "VATE KEY-----"
            )
        },
    ),
    ("speak", {"text": _j("my pass", "word: hunter22")}),
]


@pytest.mark.parametrize(("name", "args"), _SECRETS)
def test_a_secret_in_the_arguments_goes_to_a_person(yolo, human, monkeypatch, name, args):
    _open(yolo, "external")
    assert external_refusal(name, args, _scope()) == "secret_in_args"
    hooks, _engine, asked = _gate(yolo)
    event = _call(hooks, name, json.loads(json.dumps(args)))
    assert [n for n, _ in asked] == [name] and event.cancel_tool.startswith("HITL rejected")
    assert _window(yolo)["auto_approved"] == 0 and _window(yolo)["external_used"] == 0
    # The masking check is the reason: without it the same call is answered.
    monkeypatch.setattr(tiers, "mask_secrets", lambda value: value)
    event = _call(hooks, name, json.loads(json.dumps(args)))
    assert event.cancel_tool is None and len(asked) == 1


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("http_request", {"method": "GET", "url": f"https://{HOST}/", "auth_env_var": "GH"}),
        ("http_request", {"method": "GET", "url": f"https://{HOST}/", "aws_auth": True}),
    ],
)
def test_a_credential_taken_from_the_host_goes_to_a_person(yolo, human, monkeypatch, name, args):
    _open(yolo, "external")
    assert external_refusal(name, args, _scope()) == "credential_by_reference"
    hooks, _engine, asked = _gate(yolo)
    _call(hooks, name, dict(args))
    assert len(asked) == 1
    monkeypatch.setattr(tiers, "_BY_REFERENCE", {})
    assert _call(hooks, name, dict(args)).cancel_tool is None and len(asked) == 1


def test_speak_writes_its_audio_only_inside_the_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = _scope(workspace)
    monkeypatch.chdir(workspace)
    inside = {"text": "hi", "mode": "polly", "output_path": "out/a.mp3"}
    assert external_refusal("speak", inside, policy) is None
    assert external_refusal("speak", {"text": "hi", "mode": "polly"}, policy) is None
    outside = {"text": "hi", "mode": "polly", "output_path": "~/.zshrc"}
    assert external_refusal("speak", outside, policy) == "scope_outside"
    assert external_refusal("speak", {**inside, "output_path": 3}, policy) == "scope_unknown"
    assert external_refusal("speak", inside, None) == "scope_unknown"
    monkeypatch.setattr(tiers, "_LOCAL_OUTPUT", {})
    assert external_refusal("speak", outside, policy) is None  # the rule is the reason


class _Dict(dict):
    pass


@pytest.mark.parametrize(
    "args",
    [
        _Dict(url=f"https://{HOST}/"),
        {"url": (f"https://{HOST}/",)},
        {"url": b"https://example.org/"},
        {"url": f"https://{HOST}/", "n": float("nan")},
        {"url": f"https://{HOST}/", "n": float("inf")},
        {1: "x"},
        {"url": f"https://{HOST}/", "x": object()},
        "https://example.org/",
        None,
    ],
)
def test_arguments_that_are_not_plain_json_go_to_a_person(args):
    assert external_refusal("web_fetch", args, _scope()) == "args_unknown"


def test_deep_or_huge_arguments_are_not_evaluated():
    deep: dict = {}
    node = deep
    for _ in range(40):
        node["a"] = {}
        node = node["a"]
    assert external_refusal("webhook_send", {"url": "https://x.org/", "payload": deep}, None) == (
        "args_unknown"
    )
    huge = {"url": "https://x.org/", "payload": {"items": list(range(20000))}}
    assert external_refusal("webhook_send", huge, None) == "args_unknown"


def test_plain_arguments_pass_and_an_unlisted_tool_never_does():
    for name, args in _PLAIN.items():
        assert external_refusal(name, dict(args), _scope()) is None, name
    assert external_refusal("file_write", {"path": "a"}, _scope()) == "scope_unknown"
    assert external_refusal(None, {}, _scope()) == "scope_unknown"


# --- §6: the external sub-cap ------------------------------------------------------------


def test_a_used_up_sub_cap_sends_external_tools_back_to_ask(audit, clock, tmp_path):
    yolo = _ext(audit, clock, tmp_path, external_cap=2)
    _open(yolo, "external")
    with origin.human_turn(AGENT):
        for _ in range(2):
            grant, code = _approve(yolo, "web_fetch")
            assert yolo.consume(grant) == (True, "consumed"), code
        assert _window(yolo)["effective_tier"] == "internal"
        grant, code = _approve(yolo, "web_fetch")
        assert grant is None and code == "external_cap_used"
        # The window stays active; internal tools are still answered.
        assert _window(yolo)["state"] == "active"
        grant, code = _approve(yolo, "file_write")
        assert grant is not None and yolo.consume(grant)[0]
    window = _window(yolo)
    assert (window["external_used"], window["external_limit"], window["used"]) == (2, 2, 3)


def test_a_temporarily_full_sub_cap_is_a_card_and_a_void_returns_the_slot(audit, clock, tmp_path):
    yolo = _ext(audit, clock, tmp_path, external_cap=1)
    _open(yolo, "external")
    with origin.human_turn(AGENT):
        first, _ = _approve(yolo, "web_fetch")
        assert first is not None and _window(yolo)["external_reserved"] == 1
        assert _window(yolo)["effective_tier"] == "external"
        grant, code = _approve(yolo, "web_search")
        assert grant is None and code == "external_cap_full"
        assert yolo.void(first) is True
        assert _window(yolo)["external_reserved"] == 0 and _window(yolo)["reserved"] == 0
        second, code = _approve(yolo, "web_search")
        assert second is not None, code


def test_an_audit_failure_approves_no_external_call_and_revokes(audit, clock, tmp_path):
    yolo = _ext(audit, clock, tmp_path, external_cap=1)
    _open(yolo, "external")
    with origin.human_turn(AGENT):
        audit.fail.add("audit.yolo.auto_approved")
        grant, code = _approve(yolo, "web_fetch")
        assert grant is None and code == "audit_failed"
    # The window is revoked (fail closed); nothing stays reserved.
    assert yolo.machine.window is None or not yolo.machine.window.open


def test_sub_cap_zero_opens_the_window_and_only_external_tools_ask(audit, clock, tmp_path):
    yolo = _ext(audit, clock, tmp_path, external_cap=0)
    _open(yolo, "external")  # issuance is not blocked (§6a)
    assert audit.of("started")[-1]["external_cap"] == 0
    with origin.human_turn(AGENT):
        grant, code = _approve(yolo, "web_fetch")
        assert grant is None and code == "external_cap_used"
        grant, _ = _approve(yolo, "file_read")
        assert grant is not None
    assert _window(yolo)["effective_tier"] == "internal"


def test_the_total_cap_also_bounds_external_tools(audit, clock, tmp_path):
    yolo = _ext(audit, clock, tmp_path, external_cap=2, cap=2)
    _open(yolo, "external")
    with origin.human_turn(AGENT):
        a, _ = _approve(yolo, "file_write")
        b, _ = _approve(yolo, "web_fetch")
        grant, code = _approve(yolo, "web_fetch")
        assert grant is None and code == "cap_full"
        yolo.consume(a)
        yolo.consume(b)
    # used == cap ends the window (expired_cap), with the external count in its row.
    ended = audit.of("ended")[-1]
    assert ended["end_reason"] == "expired_cap" and ended["external_used"] == 1


def test_an_internal_window_records_no_external_count(yolo, audit, human):
    _open(yolo, "internal")
    yolo.consume(_approve(yolo, "file_write")[0])
    yolo.fence("stop")
    assert "external_used" not in audit.of("ended")[-1]


# --- §11 / Open (10): the destination only as a keyed hash ----------------------------------


def test_the_row_carries_tier_external_and_a_keyed_hash_of_the_host(yolo, audit, human, caplog):
    _open(yolo, "external")
    hooks, _engine, _asked = _gate(yolo)
    caplog.set_level(logging.INFO, logger="stiracore.yolo.requests")
    for path in ("a", "b"):
        _execute(hooks, _call(hooks, "web_fetch", {"url": f"https://{HOST}/{path}"}))
    _execute(hooks, _call(hooks, "web_fetch", {"url": "https://other.example.net/"}))
    _execute(hooks, _call(hooks, "file_write"))
    rows = audit.of("auto_approved")
    assert [r["tier"] for r in rows] == ["external", "external", "external", "internal"]
    assert [r["external"] for r in rows] == [True, True, True, False]
    refs = [r.get("destination_ref") for r in rows]
    assert refs[0] == refs[1] != refs[2] and refs[3] is None
    assert refs[0] == yolo._ref("destination:" + HOST)  # the service's audit key
    # Never in clear: no row, no server log line, no status answer, no log line item.
    dump = json.dumps([row for row in audit.rows]) + caplog.text + json.dumps(yolo.status())
    for clear in (HOST, "other.example.net", "example"):
        assert clear not in dump, clear
    assert "Auto-approved (YOLO external): web_fetch #" in caplog.text
    items = yolo.status()["auto_approvals"]["items"]
    assert set(items[0]) == {"n", "tool", "ref", "limit", "tier", "external_n", "external_limit"}
    assert items[0]["tier"] == "external" and items[-1]["tier"] == "internal"
    assert [i.get("external_n") for i in items] == [1, 2, 3, None]


@pytest.mark.parametrize(
    ("name", "args", "host"),
    [
        ("web_fetch", {"url": "HTTPS://Example.ORG:8443/x"}, "example.org"),
        ("webhook_send", {"url": "https://h.example.org/", "payload": {}}, "h.example.org"),
        ("web_search", {"query": "q"}, None),
        ("web_fetch", {"url": 3}, None),
        ("web_fetch", {"url": "not a url"}, None),
        ("web_fetch", {"url": "http://[::1"}, None),
        ("file_write", {"url": "https://example.org/"}, None),
    ],
)
def test_destination_is_the_url_host(name, args, host):
    assert destination(name, args) == host


def test_a_non_ascii_or_odd_destination_is_not_recorded(yolo, audit, human):
    _open(yolo, "external")
    for value in ("例え.jp", 3, ""):
        grant, _ = _approve(yolo, "web_fetch", destination=value)
        assert grant is not None
    assert all("destination_ref" not in row for row in audit.of("auto_approved"))


# --- §5 / §9: Never, the floors, the breaker, CRITICAL stay a person's ----------------------


def test_never_and_the_floor_hold_in_an_external_window(yolo, human):
    _open(yolo, "external")
    floor = SimpleNamespace(reason="protected destination file")
    hooks, _engine, asked = _gate(
        yolo,
        rules={"http_request": "never"},
        path_check=lambda name, args: floor if "SOUL" in str(args) else None,
    )
    never = _call(hooks, "http_request", dict(_PLAIN["http_request"]))
    assert never.cancel_tool.startswith("Tool policy is Never")
    floored = _call(hooks, "webhook_send", {"url": f"https://{HOST}/", "payload": {"f": "SOUL.md"}})
    assert floored.cancel_tool.startswith("protected destination file")
    assert asked == [] and _window(yolo)["auto_approved"] == 0


def test_the_breakers_write_block_and_critical_halt_hold_for_external_tools(yolo, human):
    _open(yolo, "external")
    hooks, engine, asked = _gate(yolo)
    engine.state.stability = 0.4  # WARNING: write-capable (web_fetch is) blocked
    engine.pre_turn()
    assert _call(hooks, "web_fetch", dict(_PLAIN["web_fetch"])).cancel_tool.startswith(
        "Writes are blocked"
    )
    engine.config.critical_action = "halt"
    engine.state.stability = 0.1
    engine.pre_turn()
    event = _call(hooks, "web_fetch", dict(_PLAIN["web_fetch"]))
    assert asked == [("web_fetch", hitl_reasons.CRITICAL_HALT)]
    assert event.cancel_tool.startswith("HITL rejected")
    assert _window(yolo)["auto_approved"] == 0


def test_an_external_tool_needs_a_window_of_tier_external(yolo, human):
    """The tier check is the reason a window of tier internal keeps the card."""
    _open(yolo, "internal")
    grant, code = _approve(yolo, "web_fetch")
    assert grant is None and code == "tier_not_covered"
    grant, _ = _approve(yolo, "file_write")
    assert grant is not None


def test_an_internal_allowance_cannot_start_an_external_window(audit, clock, tmp_path):
    from stiracore.yolo import state as sm

    yolo = _service(audit, clock, WindowMarker(tmp_path), allowance="internal")
    with pytest.raises(sm.Refused) as refused:
        yolo.press("external")
    assert refused.value.code == "yolo_tier_not_allowed"


def test_ext_slots_are_counted_per_window(audit, clock, tmp_path):
    yolo = _ext(audit, clock, tmp_path, external_cap=1)
    _open(yolo, "external")
    with origin.human_turn(AGENT):
        yolo.consume(_approve(yolo, "web_fetch")[0])
        assert _approve(yolo, "web_fetch")[1] == "external_cap_used"
    yolo.fence("stop")
    _open(yolo, "external")
    with origin.human_turn(AGENT):
        assert _approve(yolo, "web_fetch")[0] is not None


def test_an_external_window_closes_with_external_used_in_the_real_schema(
    yolo, audit, human, monkeypatch
):
    """PR #438 review (major, unverified): the closing row of an external window carries
    ``external_used`` and is accepted by ``audit_schema.build`` (the Recorder runs the
    real contract). A refused row would latch the window — it must not."""
    from stiracore.yolo import audit_schema

    seen = []
    real = audit_schema.build

    def spy(event_type, **fields):
        row = real(event_type, **fields)
        seen.append(row)
        return row

    monkeypatch.setattr("tests.test_yolo_requests.build", spy)
    _open(yolo, "external")
    yolo.consume(_approve(yolo, "web_fetch", destination=HOST)[0])
    yolo.fence("stop")
    [ended] = [r for r in seen if r["event_type"] == "audit.yolo.ended"]
    assert ended["attributes"]["external_used"] == 1
    assert yolo.status()["latched"] is False


# --- PR #438 review, minor 1: a registry that cannot be read is a card ---------------------


def test_a_registry_that_cannot_be_read_goes_to_a_person_not_out_of_the_hook(
    yolo, human, monkeypatch
):
    _open(yolo, "external")
    hooks, _engine, asked = _gate(yolo)

    def broken():
        raise RuntimeError("dictionary changed size during iteration")

    monkeypatch.setattr(tool_registry, "mcp_tool_names", broken)
    for name, args in (("web_fetch", _PLAIN["web_fetch"]), ("file_write", None)):
        event = _call(hooks, name, dict(args) if args else None)
        assert event.cancel_tool.startswith("HITL rejected"), name  # the person's card
    assert [n for n, _ in asked] == ["web_fetch", "file_write"]
    assert _window(yolo)["auto_approved"] == 0


# --- minor 2: header names that carry a credential ------------------------------------------


@pytest.mark.parametrize(
    "header",
    ["Cookie", "Set-Cookie", "X-Api-Key", "x-api-key", "Proxy-Authorization", "X-Auth-Token"],
)
def test_a_credential_header_makes_the_call_a_card(yolo, human, header):
    _open(yolo, "external")
    args = {
        "method": "GET",
        "url": f"https://{HOST}/",
        "headers": {header: "a1b2c3d4e5f6"},
    }
    assert external_refusal("http_request", args, _scope()) == "secret_in_args"
    hooks, _engine, asked = _gate(yolo)
    event = _call(hooks, "http_request", json.loads(json.dumps(args)))
    assert [n for n, _ in asked] == ["http_request"] and event.cancel_tool
    assert _window(yolo)["auto_approved"] == 0


# --- minor 3: a host that is not plain ASCII is a card ---------------------------------------


def test_an_ascii_host_is_recorded_by_its_keyed_hash_only(yolo, audit, human):
    _open(yolo, "external")
    hooks, _engine, asked = _gate(yolo)
    url = "https://Example.ORG/path"
    assert destination("web_fetch", {"url": url}) == "example.org"
    _execute(hooks, _call(hooks, "web_fetch", {"url": url}))
    assert asked == []
    [row] = audit.of("auto_approved")
    assert row["destination_ref"] == yolo._ref("destination:example.org")
    dump = json.dumps(audit.rows) + json.dumps(yolo.status())
    assert "example.org" not in dump.lower()


@pytest.mark.parametrize(
    "url",
    [
        "https://例え.jp/",  # not ASCII: IDNA 2003/2008 can read it differently (faß.de)
        "https://faß.de/",
        "https://" + "あ" * 70 + ".jp/",
        "https://a..example.org/",  # an empty label
        "not a url",
        "https:///path",  # no host at all
    ],
)
def test_a_url_whose_host_cannot_be_normalized_is_a_card(yolo, human, url):
    _open(yolo, "external")
    assert external_refusal("web_fetch", {"url": url}, _scope()) == "destination_unknown"
    hooks, _engine, asked = _gate(yolo)
    event = _call(hooks, "web_fetch", {"url": url})
    assert [n for n, _ in asked] == ["web_fetch"] and event.cancel_tool
    assert _window(yolo)["auto_approved"] == 0

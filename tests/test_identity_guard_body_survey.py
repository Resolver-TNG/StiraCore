"""#464 follow-up: the same false refusal outside ``memory_write`` / ``journal``.

a429569 declared the bodies of ``memory_write`` / ``journal`` (and ``file_write`` /
``editor`` before it, #305). Every other write-capable tool is still scanned in full, so a
lone ``/``, ``~``, ``.`` or ``..`` in any of its text arguments is read as the ancestor of a
read-only file and refused as "SOUL/identity files are read-only" (measured on every
write-capable tool of a live server, 2026-10-22).

* ``web_search.query`` has no target at all (it is sent as ``?q=`` to the configured
  endpoint): it is declared as body. "React / Vue 比較" or "~ の意味" were refused before.
* Kept scanned on purpose (fail closed), each pinned below with the reason:
  ``speak.text`` — macOS ``say`` reads a leading ``-o<path>`` in the text as its output file
  (measured: it wrote that file), so the text *is* a write target;
  ``think`` / ``use_agent`` / ``swarm`` / ``graph`` (and ``delegate`` on a live server) — the
  text is the prompt of another agent, which may act on it; ``journal.task`` — kept by #464's acceptance;
  ``webhook_send.payload`` — a nested object (only a plain string under a declared key is
  skipped).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stiracore.core import NeuralState, StiraConfig, StiraEngine
from stiracore.identity_paths import READ_ONLY_ERROR, WORKSPACE_ROOT, WritePathPolicy
from stiracore.middleware import identity_guard
from stiracore.middleware.identity_guard import IdentityGuard, _scanned_strings


@pytest.fixture()
def guard(tmp_path: Path) -> IdentityGuard:
    data = tmp_path / "stira"
    (data / "workspace").mkdir(parents=True)
    for name in ("tool-config.json", "neural-state.json"):
        (data / name).write_text("{}")
    read_only = [str(data / n) for n in ("tool-config.json", "neural-state.json")]
    policy = WritePathPolicy(
        read_only + ["SOUL.md"], base_dir=data / "workspace", write_roots=[WORKSPACE_ROOT]
    )
    engine = StiraEngine(state=NeuralState(stability=0.9), config=StiraConfig())
    return IdentityGuard(engine, path_policy=policy)


QUERIES = [
    "React / Vue 比較",
    "~ の意味",
    "cd .. とは",
    "1 / 2 の計算",
    "/etc/hosts の書き方",
    "SOUL.md とは",
    "https://example.com/a/b の意味",
]


@pytest.mark.parametrize("query", QUERIES)
def test_a_web_search_query_is_not_a_write_target(guard: IdentityGuard, query: str) -> None:
    """Refused on main ("argument: query; matched: /"), allowed with the declaration."""
    result = guard.check_tool_call("web_search", {"query": query})
    assert result.allowed, result.reason


def test_web_search_only_skips_a_plain_string_query() -> None:
    """A nested value or another key of web_search is still scanned (fail closed)."""
    assert "SOUL.md" in list(_scanned_strings("web_search", {"query": {"x": "SOUL.md"}}))
    assert "SOUL.md" in list(_scanned_strings("web_search", {"query": "a", "path": "SOUL.md"}))


def test_the_declared_body_arguments_are_exactly_these() -> None:
    """A new declaration is a decision (it stops scanning that argument): pin the table."""
    assert {k: set(v) for k, v in identity_guard._BODY_ARGUMENTS.items()} == {
        "file_write": {"content", "file_text", "new_str", "old_str"},
        "editor": {"content", "file_text", "new_str", "old_str"},
        "memory_write": {"content"},
        "handoff_write": {"goal", "state", "decisions", "open_questions"},
        "journal": {"content"},
        "propose_procedure": {"body", "rationale"},
        "web_search": {"query"},
    }


# Measured false refusals that stay (each needs a decision before it is declared as body).
KEPT = [
    ("speak", {"text": "A / B"}, "text"),
    ("think", {"thought": "案A / 案B を比べる"}, "thought"),
    ("think", {"thought": "x", "system_prompt": "A / B で考える"}, "system_prompt"),
    ("delegate", {"task": "A / B を調べる", "label": "x"}, "task"),
    ("use_agent", {"prompt": "A / B を要約"}, "prompt"),
    ("swarm", {"task": "A / B を調べる"}, "task"),
    ("graph", {"action": "create", "task": "A / B"}, "task"),
    ("journal", {"action": "add_task", "task": "A / B を確認"}, "task"),
    ("webhook_send", {"url": "https://example.org/", "payload": {"text": "A / B"}}, "payload"),
]


@pytest.mark.parametrize(("tool", "args", "argument"), KEPT, ids=[k[0] for k in KEPT])
def test_the_kept_scans_still_refuse_a_lone_slash(guard, tool, args, argument, monkeypatch):
    # ``delegate`` is registered by the live server (not in the module registry).
    monkeypatch.setattr(IdentityGuard, "_registered_write", staticmethod(lambda name: True))
    result = guard.check_tool_call(tool, args)
    assert not result.allowed
    assert result.reason.startswith(READ_ONLY_ERROR)
    assert f'argument: "{argument}' in result.reason


@pytest.mark.parametrize("text", ["-o/tmp/x.aiff hello", "-v Alex hi", "--help", "-"])
def test_a_speak_text_that_say_would_read_as_options_is_refused(guard, text) -> None:
    """``say -o<path>`` writes a file (measured on macOS): a leading ``-`` is refused."""
    result = guard.check_tool_call("speak", {"text": text, "mode": "fast"})
    assert not result.allowed and 'must not start with "-"' in result.reason, result.reason


@pytest.mark.parametrize("text", ["hello -o x", " -o not an option", "A - B", "こんにちは"])
def test_a_speak_text_with_a_dash_later_is_not_refused_for_it(guard, text) -> None:
    result = guard.check_tool_call("speak", {"text": text, "mode": "fast"})
    assert 'must not start with "-"' not in result.reason


def test_speak_hands_the_text_to_say_as_one_argument(monkeypatch) -> None:
    """The call the refusal is about, without running ``say``: one argv item, so only a
    leading ``-`` can be an option."""
    speak = pytest.importorskip("strands_tools.speak")
    calls = []
    monkeypatch.setattr(speak.subprocess, "run", lambda argv, **kw: calls.append(argv))
    tool = {"toolUseId": "s-1", "input": {"text": "hello -o x", "mode": "fast"}}
    result = speak.speak(tool)
    assert result["status"] == "success", result
    assert calls == [["say", "hello -o x"]]


def test_a_protected_target_in_speak_text_is_refused(guard: IdentityGuard) -> None:
    result = guard.check_tool_call("speak", {"text": "see /somewhere/SOUL.md hello"})
    assert not result.allowed and result.reason.startswith(READ_ONLY_ERROR)


# web_search.query is no target: the registered tool sends it as ``?q=`` and nothing else.


def test_the_registered_web_search_is_the_stira_tool() -> None:
    from stiracore.tools import tool_registry, web_tools

    registered = tool_registry.WRITE_TOOLS["web_search"]
    assert registered.tool_name == "web_search"
    assert getattr(registered, "_tool_func", None) is web_tools.web_search._tool_func


@pytest.mark.parametrize("query", ["SOUL.md / ~", "/etc/passwd", "../neural-state.json"])
def test_web_search_sends_the_query_only_as_a_search_parameter(monkeypatch, query) -> None:
    import builtins

    from stiracore.tools import web_tools

    sent = []

    def download(url, params=None):
        sent.append((url, params))
        return '{"results": []}', "application/json", url

    def no_open(*args, **kwargs):
        raise AssertionError(f"web_search opened a file: {args!r}")

    monkeypatch.setenv("STIRA_WEB_SEARCH_ENDPOINT", "https://search.example/q")
    monkeypatch.setattr(web_tools, "_download", download)
    monkeypatch.setattr(builtins, "open", no_open)
    result = web_tools.web_search(query=query)
    assert result["status"] == "success", result
    assert sent == [("https://search.example/q", {"q": query.strip()})]

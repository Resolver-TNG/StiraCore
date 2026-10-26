"""#585: web_fetch reads a PDF's text (separate bounded process, classified failures) and
returns a summary on request (task ``tool_summary``: no tools, framed, counted, recorded)."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import traceback
from unittest.mock import MagicMock

import pytest

from stiracore.agent import task_models as tm
from stiracore.observability.audit_chain import AuditChain
from stiracore.providers.spec import ModelSpec
from stiracore.tools import pdf_extract, tool_summary, web_tools
from stiracore.tools.web_tools import web_fetch
from tests.pdf_fixtures import blank_pdf, encrypted_pdf, text_pdf
from tests.provider_fakes import FakeModel, text_turn

# Only the tests that parse a real PDF need the optional extra (review r592 #5); the
# summary, limit, audit and compatibility tests run without it.
needs_pypdf = pytest.mark.skipif(
    importlib.util.find_spec("pypdf") is None, reason="the optional extra [pdf] is not installed"
)
TEXT = " ".join(f"Advisory line {i}: patch CVE-2026-{1000 + i} before Friday." for i in range(60))

PAGES = [f"Advisory page {i}: patch CVE-2026-{1000 + i} before Friday" for i in range(30)]
MAIN = ModelSpec("bedrock", "global.anthropic.claude-sonnet-5", {"region_name": "ap-northeast-1"})


@pytest.fixture
def http(monkeypatch):
    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.url = "https://example.test/advisory.pdf?token=abc123secretvalue9f8e7d6c5b4a&lang=ja"
    response.headers = {"Content-Type": "application/pdf"}
    response.iter_content.return_value = [text_pdf(PAGES)]
    session.get.return_value.__enter__.return_value = response
    factory = MagicMock()
    factory.return_value.__enter__.return_value = session
    monkeypatch.setattr(web_tools.requests, "Session", factory)
    return session, response


@pytest.fixture
def audit(monkeypatch):
    rows = []
    monkeypatch.setitem(web_tools._FETCH_AUDIT, "record", lambda **row: rows.append(row))
    return rows


@pytest.fixture
def summarizer(monkeypatch):
    """A summarizer on a scripted chat model (inherit), recording like the server does."""
    records = []
    chat = FakeModel([], model_id="chat-model")
    router = tm.TaskModelRouter(tm.resolve_routes({}, MAIN, {}), MAIN, on_record=records.append)
    state = {"turn": "t1"}
    made = tool_summary.ToolSummarizer(router, lambda: chat, lambda: state["turn"])
    monkeypatch.setitem(tool_summary._STATE, "summarizer", made)
    return made, chat, records, state


def _text_body(http, body: str = TEXT, url: str | None = None):
    http[1].headers = {"Content-Type": "text/plain"}
    http[1].iter_content.return_value = [body.encode()]
    if url is not None:
        http[1].url = url


# --- PDF ------------------------------------------------------------------------------------


@needs_pypdf
def test_a_text_pdf_of_thirty_pages_returns_its_text(http):
    result = web_fetch(http[1].url)
    assert result["status"] == "success"
    text = result["content"][0]["text"]
    assert "Advisory page 0" in text and "Advisory page 29" in text
    assert result["pages"] == 30 and result["truncated"] is False


@needs_pypdf
def test_a_pdf_sent_as_a_generic_binary_is_recognised_by_its_magic_bytes(http):
    http[1].headers = {"Content-Type": "application/octet-stream"}
    assert "Advisory page 3" in web_fetch(http[1].url)["content"][0]["text"]
    http[1].iter_content.return_value = [b"\x89PNG not a pdf"]
    assert web_fetch(http[1].url)["code"] == "unsupported_type"


@needs_pypdf
@pytest.mark.parametrize(
    ("make", "code"),
    [
        (lambda: b"%PDF-1.4\nthis is not a pdf body", "pdf_broken"),
        (lambda: b"<html>not a pdf</html>", "pdf_broken"),
        (encrypted_pdf, "pdf_encrypted"),
        (lambda: blank_pdf(3), "pdf_no_text"),
    ],
    ids=["broken", "no_magic", "encrypted", "no_text"],
)
def test_each_pdf_failure_has_its_code(http, audit, make, code):
    http[1].iter_content.return_value = [make()]
    result = web_fetch(http[1].url)
    assert (result["status"], result["code"]) == ("error", code)
    assert code in web_tools.FETCH_CODES
    assert [row["reason_code"] for row in audit] == [code]
    assert "abc123secret" not in json.dumps(result)


@needs_pypdf
def test_too_many_pages_and_too_many_bytes_are_too_large(http, audit, monkeypatch):
    monkeypatch.setattr(pdf_extract, "MAX_PDF_PAGES", 10)
    monkeypatch.setattr(
        pdf_extract,
        "extract_text",
        lambda data, _real=pdf_extract.extract_text: _real(data, max_pages=10),
    )
    result = web_fetch(http[1].url)
    assert (result["code"], result["limit"]) == ("too_large", "pages")
    assert "10 pages" in result["content"][0]["text"]
    monkeypatch.setattr(pdf_extract, "MAX_PDF_BYTES", 100)
    result = web_fetch(http[1].url)
    assert result["code"] == "too_large" and "limit" not in result
    assert [row["reason_code"] for row in audit] == ["too_large", "too_large"]


def test_a_missing_pdf_library_is_a_classified_error(http, monkeypatch):
    # The child cannot import pypdf (an install without the extra).
    monkeypatch.setattr(
        pdf_extract,
        "_CHILD",
        pdf_extract._CHILD.replace("from pypdf import", "from nopypdf import"),
    )
    result = web_fetch(http[1].url)
    assert result["code"] == "pdf_unavailable"
    assert "stiracore[pdf]" in result["content"][0]["text"]


def test_a_slow_extraction_is_stopped_by_the_time_limit(monkeypatch):
    monkeypatch.setattr(pdf_extract, "_CHILD", "import time\ntime.sleep(30)\n" + pdf_extract._CHILD)
    result = pdf_extract.extract_text(text_pdf(["x"]), timeout=0.5)
    assert result.code == "pdf_timeout"


@needs_pypdf
def test_the_parser_runs_in_a_child_without_the_server_environment(monkeypatch):
    monkeypatch.setenv("STIRA_TEST_SECRET_FOR_CHILD", "do-not-pass")
    seen = {}
    real = pdf_extract.subprocess.Popen

    def spy(command, **kwargs):
        if command[0] == sys.executable:  # the parser child, not the ``ps`` watchdog
            seen.update(command=command, env=kwargs.get("env"))
        return real(command, **kwargs)

    monkeypatch.setattr(pdf_extract.subprocess, "Popen", spy)
    assert pdf_extract.extract_text(text_pdf(["hello pdf"])).code == "ok"
    assert seen["command"][1] == "-I"
    assert "STIRA_TEST_SECRET_FOR_CHILD" not in (seen["env"] or {})


# --- summarize ----------------------------------------------------------------------------


def test_default_output_is_unchanged_without_summarize(http, summarizer):
    _made, chat, _records, _state = summarizer
    http[1].headers = {"Content-Type": "text/plain"}
    http[1].iter_content.return_value = [b"hello"]
    result = web_fetch(http[1].url)
    assert result == {
        "status": "success",
        "content": [{"text": "hello"}],
        "url": http[1].url,
        "truncated": False,
    }
    assert chat.requests == []


def test_a_summary_comes_framed_with_source_time_length_and_truncation(http, summarizer):
    _made, chat, records, _state = summarizer
    _text_body(http, url="https://example.test/advisory?token=***&lang=ja")
    chat.script = [
        text_turn("Thirty pages of CVE patches.", usage={"inputTokens": 900, "outputTokens": 12})
    ]
    result = web_fetch(http[1].url, summarize=True, focus="deadline")
    assert result["status"] == "success" and result["summarized"] is True
    own = json.loads(result["content"][0]["text"])
    assert own["source_chars"] == len(TEXT) and own["truncated"] is False
    assert own["fetched_at"].endswith("+00:00")
    # The source URL without the secret-looking query value.
    assert own["source_url"] == "https://example.test/advisory?token=***&lang=ja"
    assert own["source_url_shortened"] is False
    assert "abc123secret" not in json.dumps(result)
    framed = result["content"][1]["text"]
    assert framed.startswith(tool_summary.SUMMARY_NOTE)
    assert f"<{tool_summary.SUMMARY_TAG}>\nThirty pages of CVE patches.\n" in framed
    # One isolated call: no tools, the page framed as data, the focus framed too.
    (request,) = chat.requests
    assert request["tool_names"] == []
    assert request["system_prompt"] == tool_summary.SUMMARY_SYSTEM_PROMPT
    sent = request["messages"][0]["content"][0]["text"]
    assert "<untrusted-source>\nAdvisory line 0" in sent and "<focus>\ndeadline\n</focus>" in sent
    # The tokens are recorded (inherit too: the task records its calls).
    (record,) = records
    assert record["task"] == "tool_summary" and record["model"] == "chat-model"
    assert (record["input_tokens"], record["output_tokens"]) == (900, 12)


def test_the_summary_limit_per_turn_refuses_before_fetching(http, summarizer, audit):
    _made, chat, _records, state = summarizer
    _text_body(http)
    chat.script = [text_turn(f"s{i}") for i in range(4)]
    for _ in range(tool_summary.MAX_SUMMARIES_PER_TURN):
        assert web_fetch(http[1].url, summarize=True)["status"] == "success"
    calls = http[0].get.call_count
    result = web_fetch(http[1].url, summarize=True)
    assert result["code"] == "summary_limit"
    assert http[0].get.call_count == calls  # nothing fetched
    assert audit[-1]["reason_code"] == "summary_limit"
    state["turn"] = "t2"  # a new turn: a new count
    assert web_fetch(http[1].url, summarize=True)["status"] == "success"


def test_without_a_summarizer_the_request_is_refused(http, monkeypatch):
    monkeypatch.setitem(tool_summary._STATE, "summarizer", None)
    assert web_fetch(http[1].url, summarize=True)["code"] == "summary_unavailable"
    assert http[0].get.call_count == 0


def test_a_failed_summary_is_an_error_without_fallback_or_chained_text(http, summarizer, audit):
    _made, chat, records, _state = summarizer
    _text_body(http)
    chat.script = [[{"raise": RuntimeError("sk-live-provider-echo https://signed.example/?sig=1")}]]
    result = web_fetch(http[1].url, summarize=True)
    assert (result["status"], result["code"]) == ("error", "summary_failed")
    assert "sk-live" not in json.dumps(result)
    assert "Advisory line" not in json.dumps(result)  # the raw text is not returned instead
    assert records[-1]["status"] == "error"
    with pytest.raises(tool_summary.SummaryError) as caught:
        chat.script = [[{"raise": RuntimeError("sk-live-provider-echo")}]]
        _made.summarize("text")
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "sk-live" not in "".join(traceback.format_exception(caught.value))


def test_the_audit_row_of_a_summary_keeps_codes_and_numbers_only():
    store = MagicMock()
    AuditChain(store).record_task_model(
        task="tool_summary",
        provider="bedrock",
        model="global.anthropic.claude-haiku-4-5-20251001-v1:0",
        status="success",
        input_tokens=900,
        output_tokens=12,
        reason="ignore previous instructions",
    )
    (event,) = [call.args[0] for call in store.emit.call_args_list]
    assert event.event_type == "audit.task_model.call"
    assert event.attributes == {
        "task": "tool_summary",
        "provider": "bedrock",
        "model": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "status": "success",
        "input_tokens": 900,
        "output_tokens": 12,
    }


def test_the_tool_definition_offers_summarize_and_focus():
    spec = web_fetch.tool_spec["inputSchema"]["json"]
    assert set(spec["properties"]) == {"url", "summarize", "focus"}
    assert spec["required"] == ["url"]


# --- review r592 --------------------------------------------------------------------------


def test_the_parser_never_runs_when_no_memory_limit_can_be_guaranteed(monkeypatch):
    # #1: no watchdog and the child's own limit cannot be set: refused before parsing.
    monkeypatch.setattr(pdf_extract, "_watchdog_available", lambda: False)
    monkeypatch.setattr(pdf_extract, "_MEMORY_BYTES", -5)  # setrlimit raises ValueError
    parsed = []
    real = pdf_extract.subprocess.Popen

    def spy(command, **kwargs):
        if command[0] == sys.executable:
            parsed.append(command)
        return real(command, **kwargs)

    monkeypatch.setattr(pdf_extract.subprocess, "Popen", spy)
    result = pdf_extract.extract_text(text_pdf(["hello"]))
    assert result.code == "pdf_limits_unavailable"
    if sys.platform == "darwin" or not hasattr(__import__("os"), "fork"):
        assert parsed == []  # no reliable child limit here: not even started


def test_the_memory_limit_stops_a_real_child(monkeypatch):
    # #1: a real child that allocates past the limit is stopped (RLIMIT_AS on Linux, the
    # parent's RSS watchdog on macOS), with a code, before it can finish.
    monkeypatch.setattr(pdf_extract, "_MEMORY_BYTES", 150 * 1024 * 1024)
    grow = (
        "    hog = []\n"
        "    for _ in range(40):\n"
        "        hog.append(b'x' * (20 * 1024 * 1024))\n"
        "        time.sleep(0.05)\n"
        "    out(code='ok', text='should not get here', pages=1, chars=1)\n"
    )
    monkeypatch.setattr(pdf_extract, "_CHILD", pdf_extract._CHILD.replace("    # PARSE\n", grow))
    result = pdf_extract.extract_text(text_pdf(["hello"]), timeout=15)
    assert (result.code, result.limit) == ("too_large", "memory")


def _reserve_all(made, state, turn, n):
    state["turn"] = turn
    out = []
    for _ in range(n):
        try:
            made.reserve()
            out.append("ok")
        except tool_summary.SummaryError as error:
            out.append(error.code)
    return out


def test_the_limit_is_per_turn_also_when_turns_interleave(summarizer):
    # #2: A x3, then B, then A again: A stays at its limit.
    made, _chat, _records, state = summarizer
    assert _reserve_all(made, state, "A", 3) == ["ok"] * 3
    assert _reserve_all(made, state, "B", 1) == ["ok"]
    assert _reserve_all(made, state, "A", 1) == ["summary_limit"]
    # A finished turn stays closed: a late call from it is refused, B is unaffected.
    made.finish("B")
    assert _reserve_all(made, state, "B", 1) == ["summary_limit"]
    assert _reserve_all(made, state, "C", 1) == ["ok"]


def test_concurrent_reservations_from_different_turns_keep_each_count():
    local = threading.local()
    made = tool_summary.ToolSummarizer(None, lambda: None, lambda: local.turn)
    barrier = threading.Barrier(8)
    per_thread: list[tuple[str, list[str]]] = []
    guard = threading.Lock()

    def worker(turn):
        local.turn = turn
        barrier.wait()
        got = []
        for _ in range(5):
            try:
                made.reserve()
                got.append("ok")
            except tool_summary.SummaryError as error:
                got.append(error.code)
        with guard:
            per_thread.append((turn, got))

    threads = [threading.Thread(target=worker, args=(f"T{i % 4}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Two threads per turn, 10 attempts per turn: exactly 3 granted per turn, in total.
    assert len(per_thread) == 8
    granted = {turn: 0 for turn in ("T0", "T1", "T2", "T3")}
    for turn, got in per_thread:
        granted[turn] += got.count("ok")
        assert set(got) <= {"ok", "summary_limit"}
    assert granted == dict.fromkeys(granted, tool_summary.MAX_SUMMARIES_PER_TURN)


def test_a_model_builder_failure_is_summary_failed_with_an_audit_row(http, audit, monkeypatch):
    # #3: the routed model is built on first use; its failure is the tool's summary_failed.
    _text_body(http)
    routes = tm.resolve_routes(
        {"tool_summary": "global.anthropic.claude-haiku-4-5-20251001-v1:0"},
        MAIN,
        {"AWS_PROFILE": "x"},
    )

    def build(spec):
        raise RuntimeError("sk-liv…f00d builder failed")

    router = tm.TaskModelRouter(routes, MAIN, build=build)
    made = tool_summary.ToolSummarizer(router, lambda: FakeModel([]), lambda: "t1")
    monkeypatch.setitem(tool_summary._STATE, "summarizer", made)
    result = web_fetch(http[1].url, summarize=True)
    assert (result["status"], result["code"]) == ("error", "summary_failed")
    assert audit[-1]["reason_code"] == "summary_failed"
    assert "sk-live" not in json.dumps(result)
    with pytest.raises(tool_summary.SummaryError) as caught:
        made.summarize("text")
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_a_failing_chat_model_getter_is_summary_failed(http, audit, monkeypatch):
    _text_body(http)

    def broken():
        raise RuntimeError("sk-liv…cafe")

    made = tool_summary.ToolSummarizer(None, broken, lambda: "t1")
    monkeypatch.setitem(tool_summary._STATE, "summarizer", made)
    result = web_fetch(http[1].url, summarize=True)
    assert result["code"] == "summary_failed" and "sk-live" not in json.dumps(result)


@needs_pypdf
def test_a_pdf_longer_than_the_extraction_cap_reports_its_full_length(
    http, summarizer, monkeypatch
):
    # #4a: the child keeps at most MAX_PDF_CHARS but counts every character.
    _made, chat, _records, _state = summarizer
    chat.script = [text_turn("long")]
    monkeypatch.setattr(pdf_extract, "MAX_PDF_CHARS", 500)
    lines = [f"page {i} " + "x" * 80 for i in range(30)]
    http[1].iter_content.return_value = [text_pdf(lines)]
    own = json.loads(web_fetch(http[1].url, summarize=True)["content"][0]["text"])
    full = sum(len(line) for line in lines) + 2 * (len(lines) - 1)
    assert own["source_chars"] == full > 500
    assert own["truncated"] is True and own["pages"] == 30


def test_a_long_path_keeps_a_shortened_source(http, summarizer):
    # #4b: a URL too long for the redirect display still gives a (shortened) source.
    _made, chat, _records, _state = summarizer
    chat.script = [text_turn("ok")]
    long_url = "https://example.test/" + "segment/" * 120 + "doc?api_key=SECRETVALUE123"
    _text_body(http, url=long_url)
    result = web_fetch(long_url, summarize=True)
    own = json.loads(result["content"][0]["text"])
    assert own["source_url"].startswith("https://example.test/segment/")
    assert own["source_url_shortened"] is True
    assert len(own["source_url"]) <= web_tools.MAX_SOURCE_URL_CHARS
    assert "SECRET" not in json.dumps(result) and result["url"] == own["source_url"]


def test_the_tool_description_says_a_summary_is_no_evidence_of_safety():
    assert "no evidence" in web_fetch.tool_spec["description"]


def test_the_child_itself_refuses_to_parse_without_a_memory_limit():
    # The child's own guard: not watched and no enforced RLIMIT_AS (macOS, or setrlimit
    # failing) -> it stops before importing the parser or reading the input.
    import subprocess as sp

    argv = [sys.executable, "-I", "-c", pdf_extract._CHILD, "10", "1000", "5", "-5", "0"]
    done = sp.run(argv, input=text_pdf(["hi"]), capture_output=True, timeout=30, check=False)
    assert json.loads(done.stdout) == {"code": "pdf_limits_unavailable"}


def test_the_server_closes_a_turn_on_turn_completed():
    from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore

    recorder = TurnEventRecorder(TurnEventStore(":memory:"))
    made = tool_summary.ToolSummarizer(None, lambda: None, recorder.active.current)
    recorder.subscribe(
        lambda event: (
            made.finish(event.get("turn_id")) if event.get("type") == "turn.completed" else None
        )
    )
    recorder.active.set("turn-1")
    made.reserve()
    recorder.emit("turn.completed", {})
    with pytest.raises(tool_summary.SummaryError):
        made.reserve()  # a late call of the finished turn


# --- review r592b --------------------------------------------------------------------------


def test_a_turn_whose_record_is_gone_never_starts_from_zero(summarizer, monkeypatch):
    # B2: with small tables, many open turns never evict A; new turns are refused instead.
    made, _chat, _records, state = summarizer
    monkeypatch.setattr(tool_summary, "MAX_OPEN_TURNS", 3)
    monkeypatch.setattr(tool_summary, "MAX_FINISHED_TURNS", 2)
    assert _reserve_all(made, state, "A", 3) == ["ok"] * 3
    for turn in ("B", "C"):
        assert _reserve_all(made, state, turn, 1) == ["ok"]
    assert _reserve_all(made, state, "D", 1) == ["summary_limit"]  # table full: refused
    assert _reserve_all(made, state, "A", 1) == ["summary_limit"]  # A kept its count
    # Finished turns beyond the memory: no finished id is forgotten into a fresh count.
    for turn in ("B", "C"):
        made.finish(turn)
    made.finish("A")  # the third finished id overflows the memory: fail closed
    for turn in ("A", "B", "C", "E"):
        assert _reserve_all(made, state, turn, 1) == ["summary_limit"], turn


def test_the_server_authority_says_which_turns_are_finished():
    # The server passes the turn store: a finished turn is refused even after this
    # summarizer forgot it (or never saw it).
    finished = {"old-turn"}
    state = {"turn": "old-turn"}
    made = tool_summary.ToolSummarizer(
        None, lambda: None, lambda: state["turn"], is_finished=finished.__contains__
    )
    with pytest.raises(tool_summary.SummaryError):
        made.reserve()
    state["turn"] = "new-turn"
    made.reserve()


def test_no_active_turn_is_refused():
    made = tool_summary.ToolSummarizer(None, lambda: None, lambda: None)
    with pytest.raises(tool_summary.SummaryError) as caught:
        made.reserve()
    assert caught.value.code == "summary_unavailable"


_PARSED = "    out(code='ok', text='parsed', pages=1, chars=6)\n"


def test_an_unreadable_rss_at_start_refuses_before_any_parsing(monkeypatch):
    # B1: the watchdog must read the child's RSS before the PDF is handed over.
    monkeypatch.setattr(pdf_extract, "_rss_bytes", lambda pid: None)
    monkeypatch.setattr(pdf_extract, "_CHILD", pdf_extract._CHILD.replace("    # PARSE\n", _PARSED))
    result = pdf_extract.extract_text(text_pdf(["hello"]), timeout=10)
    assert result.code == "pdf_limits_unavailable"


def test_an_rss_that_becomes_unreadable_stops_the_child(monkeypatch):
    # B1: three readings in a row fail while parsing: the child is stopped (not the timeout).
    readings = iter([50 * 1024 * 1024])
    monkeypatch.setattr(pdf_extract, "_rss_bytes", lambda pid: next(readings, None))
    slow = "    time.sleep(8)\n" + _PARSED
    monkeypatch.setattr(pdf_extract, "_CHILD", pdf_extract._CHILD.replace("    # PARSE\n", slow))
    started = __import__("time").monotonic()
    result = pdf_extract.extract_text(text_pdf(["hello"]), timeout=6)
    assert result.code == "pdf_limits_unavailable"
    assert __import__("time").monotonic() - started < 3


def test_the_checked_ps_is_the_one_that_runs(monkeypatch):
    calls = []
    real = pdf_extract.subprocess.run

    def spy(command, **kwargs):
        calls.append(command[0])
        return real(command, **kwargs)

    monkeypatch.setattr(pdf_extract.subprocess, "run", spy)
    path = pdf_extract._ps_path()
    assert path is None or path.startswith("/")
    if path is not None:
        pdf_extract._rss_bytes(1)
        assert calls == [path]

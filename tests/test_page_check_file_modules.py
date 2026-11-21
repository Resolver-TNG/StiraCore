"""#766 (ADR 0015 addendum rev 6, O6 / slice S9): ES modules and sub-resources of a
workspace file target, served by Stira through CDP ``Fetch.fulfillRequest``.

Chrome refuses ``<script type="module">`` (and ``import``) from an opaque ``file://``
origin. ``page_check`` already pauses every ``file://`` request; a request inside the
workspace that the read floor allows is now answered with the file's bytes, a type from a
closed table and ``Access-Control-Allow-Origin: *``. Outside the workspace, through a
symlink that leaves it, or on the read floor: refused and counted as before.

The pure parts run everywhere; the real-browser tests need Chrome / Chromium / Edge and are
skipped without one unless ``STIRA_REQUIRE_BROWSER=1``.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import time
from pathlib import Path

import pytest

from stiracore.tools import page_check as pc

BROWSER = pc.browser_status()
REQUIRE_BROWSER = os.environ.get("STIRA_REQUIRE_BROWSER", "").strip() == "1"
needs_browser = pytest.mark.skipif(
    not BROWSER.available and not REQUIRE_BROWSER,
    reason=f"no browser for page_check ({BROWSER.code}); set STIRA_REQUIRE_BROWSER=1 to fail",
)
FIXTURES = Path(__file__).parent / "fixtures" / "page_check"
OUTSIDE_WORD = "outside-canary-4711"
FLOOR_WORD = "floor-canary-4712"


def _policy(ws, **extra):
    from stiracore.identity_paths import WritePathPolicy

    return WritePathPolicy(base_dir=ws, write_roots=[ws], **extra)


@pytest.fixture
def ws(tmp_path):
    """The module fixture as a workspace, a file outside it, and a file on the read floor
    (``data/`` here is given to the policy as the floor tree by the tests that need it)."""
    root = (tmp_path / "ws").resolve()
    shutil.copytree(FIXTURES / "module_page", root)
    (root / "private").mkdir()
    (root / "private" / "kept.js").write_text(f"export const v = '{FLOOR_WORD}';\n", "utf-8")
    (root.parent / "outside.js").write_text(f"export const v = '{OUTSIDE_WORD}';\n", "utf-8")
    return root


class _FakeBrowser:
    def __init__(self):
        self.sent = []
        self.inbox = queue.Queue()

    def send(self, method, params=None, session=None):
        self.sent.append((method, params, session))
        return len(self.sent)


def _fake_run(ws, **policy):
    run = pc._Run(_FakeBrowser(), _policy(ws, **policy), time.monotonic() + 5)
    run.main_target, run.main_session, run.known_pages = "MAIN", "S", {"MAIN"}
    return run


def _pause(run, url, kind="Script", *, method="GET", headers=None, frame="F"):
    request = {"url": url, "method": method, "headers": headers or {}}
    params = {"requestId": "r", "request": request, "resourceType": kind, "frameId": frame}
    run.on_event({"method": "Fetch.requestPaused", "params": params, "sessionId": "S"})
    return run.b.sent[-1]


def _headers(params):
    return {h["name"]: h["value"] for h in params["responseHeaders"]}


# A page that reads each named URL and shows what it got (a classic script: it does not
# depend on module loading itself).
_PROBE = """<!doctype html><meta charset="utf-8"><title>Probe</title><pre id="out">pending</pre>
<script>
const targets = %s;
const read = (url) => new Promise((done) => {
  const x = new XMLHttpRequest();
  x.onload = () => done(x.responseText || "empty");
  x.onerror = () => done("refused");
  try { x.open("GET", url); x.send(); } catch (e) { done("refused"); }
});
(async () => {
  const out = [];
  for (const [name, url] of Object.entries(targets)) out.push(name + "=" + (await read(url)));
  document.getElementById("out").textContent = out.join(" ");
})();
</script>
"""


def _probe(ws, targets):
    (ws / "probe.html").write_text(_PROBE % json.dumps(targets), encoding="utf-8")
    return pc.run_page_check("probe.html", _policy(ws), wait_ms=600, screenshot=False)


# --- the module page ---------------------------------------------------------------------


@needs_browser
def test_module_page_starts_over_file_target(ws):
    """Before #766: the text stayed ``Title screen`` and nothing was reported."""
    result = pc.run_page_check("index.html", _policy(ws), wait_ms=600, screenshot=False)
    assert result["status"] == "ok" and result["loaded"], result
    assert "Started: oolong" in result["text"] and "Title screen" not in result["text"], result
    assert result["page_errors"]["count"] == result["console_errors"]["count"] == 0, result
    assert result["missing_files"]["count"] == 0, result
    # No listener, no port, no network: nothing reached the proxy from the page.
    assert result["network"]["blocked"] == 0 and result["network"]["hosts"] == [], result
    assert all(count == 0 for count in result["blocked"].values()), result
    assert result["open_yourself"]["url"].startswith("file://"), result


@needs_browser
def test_module_page_origin_stays_opaque(ws):
    (ws / "origin.html").write_text(
        "<!doctype html><title>Origin</title><p id=o>pending</p>"
        "<script type=module>"
        "document.getElementById('o').textContent ="
        " 'origin=' + self.origin + ' scheme=' + location.protocol;"
        "</script>",
        encoding="utf-8",
    )
    result = pc.run_page_check("origin.html", _policy(ws), wait_ms=300, screenshot=False)
    assert "origin=null" in result["text"] and "scheme=file:" in result["text"], result


# --- what is served ------------------------------------------------------------------------


def test_fulfilled_response_is_inside_workspace_only(ws):
    run = _fake_run(ws)
    inside = ws / "src" / "greeting.js"
    method, params, session = _pause(run, inside.as_uri() + "?v=1#top")
    assert (method, session) == ("Fetch.fulfillRequest", "S")
    assert params["requestId"] == "r" and params["responseCode"] == 200
    assert base64.b64decode(params["body"]) == inside.read_bytes()
    assert _headers(params) == {
        "Content-Type": "text/javascript",
        "Access-Control-Allow-Origin": "*",
    }
    assert run.blocked["outside_workspace"] == 0
    for url in (
        (ws.parent / "outside.js").as_uri(),
        (ws / ".." / "outside.js").as_uri(),
        "file://example.com" + inside.as_posix(),
        "file:///etc/hosts",
    ):
        run = _fake_run(ws)
        method, params, _session = _pause(run, url)
        assert method == "Fetch.failRequest" and "body" not in params, url
        assert run.blocked["outside_workspace"] == 1, url
        assert not any(m == "Fetch.fulfillRequest" for m, _p, _s in run.b.sent), url


def test_documents_and_unusual_requests_take_the_old_path(ws):
    """Only a plain ``GET`` of a sub-resource is answered by Stira. A document (the page
    itself, a frame), another method, a ``Range`` request and a file over the size cap go
    on as before #766 (Chrome reads the checked ``file://`` URL itself)."""
    page = (ws / "index.html").as_uri()
    script = (ws / "src" / "main.js").as_uri()
    run = _fake_run(ws)
    assert _pause(run, page, "Document", frame="MAIN")[0] == "Fetch.continueRequest"
    assert _pause(run, page, "Document")[0] == "Fetch.continueRequest"
    assert _pause(run, script, method="POST")[0] == "Fetch.continueRequest"
    assert _pause(run, script, headers={"range": "bytes=0-1"})[0] == "Fetch.continueRequest"
    assert _pause(run, script)[0] == "Fetch.fulfillRequest"
    assert run.blocked["outside_workspace"] == 0 and run.missing_count == 0


def test_a_missing_sub_resource_is_failed_and_listed(ws):
    """A missing module or ``fetch()`` target is a CORS-mode request: Chrome's own failure
    of it is not ``ERR_FILE_NOT_FOUND``, so it was in no count. Stira fails it and lists it."""
    run = _fake_run(ws)
    method, params, _session = _pause(run, (ws / "src" / "absent.js").as_uri())
    assert (method, params["errorReason"]) == ("Fetch.failRequest", "Failed")
    assert (run.missing_count, run.missing) == (1, [os.path.join("src", "absent.js")])
    assert run.blocked["outside_workspace"] == 0
    _pause(run, (ws / "data" / "absent.json").as_uri(), "Fetch")
    assert run.missing_count == 2
    # A missing document, or one asked for with another method, is Chrome's as before.
    assert _pause(run, (ws / "no.html").as_uri(), "Document")[0] == "Fetch.continueRequest"
    assert _pause(run, (ws / "no.js").as_uri(), method="POST")[0] == "Fetch.continueRequest"
    assert run.missing_count == 2


def test_a_file_over_the_size_cap_is_not_read_into_memory(ws, monkeypatch):
    monkeypatch.setattr(pc, "FULFIL_MAX_BYTES", 8)
    (ws / "big.js").write_bytes(b"x" * 9)
    (ws / "small.js").write_bytes(b"x" * 8)
    policy = _policy(ws)
    assert pc.workspace_file_bytes(policy, str(ws / "big.js")) is None
    assert pc.workspace_file_bytes(policy, str(ws / "small.js")) == b"x" * 8
    assert pc.workspace_file_bytes(policy, str(ws / "src")) is None  # a directory
    assert pc.workspace_file_bytes(policy, str(ws / "absent.js")) is None
    # The size is decided on the open file before anything is read from it.
    reads = []
    real_read = os.read
    monkeypatch.setattr(os, "read", lambda fd, n: reads.append(n) or real_read(fd, n))
    assert pc.read_workspace_file(policy, str(ws / "big.js")) == (pc.FULFIL_UNSERVED, None)
    assert reads == []
    assert pc.read_workspace_file(policy, str(ws / "small.js")) == (pc.FULFIL_SERVED, b"x" * 8)
    assert reads


@needs_browser
def test_outside_workspace_request_still_refused_and_counted(ws):
    outside = (ws.parent / "outside.js").as_uri()
    (ws / "leaving.html").write_text(
        "<!doctype html><title>Leaving</title><p id=o>Title screen</p>"
        f"<script type=module>import {{ v }} from '{outside}';"
        "document.getElementById('o').textContent = v;</script>",
        encoding="utf-8",
    )
    result = pc.run_page_check("leaving.html", _policy(ws), wait_ms=400, screenshot=False)
    assert result["blocked"]["outside_workspace"] == 1, result
    assert OUTSIDE_WORD not in json.dumps(result), result
    probed = _probe(ws, {"out": outside, "in": "data/menu.json"})
    assert "out=refused" in probed["text"] and 'in={"item": "oolong"}' in probed["text"], probed
    assert probed["blocked"]["outside_workspace"] == 1, probed
    assert OUTSIDE_WORD not in json.dumps(probed), probed


def test_symlink_out_of_workspace_refused(ws):
    (ws / "link.js").symlink_to(ws.parent / "outside.js")
    (ws / "linked").symlink_to(ws.parent, target_is_directory=True)
    (ws / "alias.js").symlink_to(ws / "src" / "greeting.js")
    policy = _policy(ws)
    for name in ("link.js", "linked/outside.js"):
        assert pc.workspace_file_bytes(policy, str(ws / name)) is None
        run = _fake_run(ws)
        method, params, _session = _pause(run, ws.as_uri() + "/" + name)
        assert method == "Fetch.failRequest" and "body" not in params, name
        assert run.blocked["outside_workspace"] == 1, name
    # A link that stays inside the workspace is the file it points at.
    run = _fake_run(ws)
    method, params, _session = _pause(run, (ws / "alias.js").as_uri())
    assert method == "Fetch.fulfillRequest"
    assert base64.b64decode(params["body"]) == (ws / "src" / "greeting.js").read_bytes()


@needs_browser
def test_symlink_out_of_workspace_refused_in_a_browser(ws):
    (ws / "link.js").symlink_to(ws.parent / "outside.js")
    probed = _probe(ws, {"link": "link.js"})
    assert "link=refused" in probed["text"], probed
    assert probed["blocked"]["outside_workspace"] == 1, probed
    assert OUTSIDE_WORD not in json.dumps(probed), probed


def test_fulfilled_bodies_respect_read_floor(ws):
    floor = ws / "private"
    kept = floor / "kept.js"
    assert pc.workspace_file_bytes(_policy(ws), str(kept)) == kept.read_bytes()
    assert pc.workspace_file_bytes(_policy(ws, floor_tree=floor), str(kept)) is None
    run = _fake_run(ws, floor_tree=floor)
    method, params, _session = _pause(run, kept.as_uri())
    assert method == "Fetch.failRequest" and "body" not in params
    assert run.blocked["outside_workspace"] == 1
    # The same decision as the read floor of ``file_read``, asked for the resolved path.
    asked = []

    class Asking:
        workspace = ws

        def check_read(self, path):
            asked.append(path)
            return type("Decision", (), {"allowed": False})()

    assert pc.workspace_file_bytes(Asking(), str(ws / "src" / ".." / "src" / "main.js")) is None
    assert asked == [str(ws / "src" / "main.js")]


@needs_browser
def test_fulfilled_bodies_respect_read_floor_in_a_browser(ws):
    (ws / "probe.html").write_text(
        _PROBE % json.dumps({"kept": "private/kept.js", "menu": "data/menu.json"}), "utf-8"
    )
    policy = _policy(ws, floor_tree=ws / "private")
    result = pc.run_page_check("probe.html", policy, wait_ms=600, screenshot=False)
    assert "kept=refused" in result["text"] and "menu={" in result["text"], result
    assert result["blocked"]["outside_workspace"] == 1, result
    assert FLOOR_WORD not in json.dumps(result), result


# --- a path swapped between the check and the open (review of #766, r2) -----------------------


def _at_open(monkeypatch, victim, swap):
    """Run ``swap()`` once, immediately before the real ``os.open`` of ``victim``: after
    every check that precedes the open. Returns the list that gets the open's flags."""
    real_open = os.open
    flags_seen = []

    def opening(path, flags, *args, **kwargs):
        if os.fspath(path) == str(victim) and not flags_seen:
            flags_seen.append(flags)
            swap()
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", opening)
    return flags_seen


def _refused_not_served(run, sent, word):
    method, params, _session = sent
    assert (method, params.get("errorReason")) == ("Fetch.failRequest", "BlockedByClient")
    assert run.blocked["outside_workspace"] == 1 and run.missing_count == 0
    assert not any(
        m in ("Fetch.fulfillRequest", "Fetch.continueRequest") for m, _p, _s in run.b.sent
    )
    assert word not in json.dumps(run.b.sent)
    assert base64.b64encode(word.encode()).decode() not in json.dumps(run.b.sent)


@pytest.mark.parametrize("case", ["floor", "outside", "read_roots", "secret_path"])
def test_parent_directory_swapped_after_the_check_is_not_served(ws, monkeypatch, case):
    """``src/`` becomes a link to another directory after the checks, before the open."""
    victim = ws / "src" / "greeting.js"
    target = {"floor": ws / "private", "secret_path": ws / "keys"}.get(case, ws.parent / "out")
    target.mkdir(exist_ok=True)
    (target / "greeting.js").write_text(f"export const v = '{FLOOR_WORD}';\n", "utf-8")
    policy = {
        "floor": {"floor_tree": ws / "private"},
        "outside": {},
        "read_roots": {"read_roots": [ws]},
        "secret_path": {"read_secret_paths": [str(ws / "keys")]},
    }[case]
    run = _fake_run(ws, **policy)
    if case != "outside":  # the place itself is refused without any swap
        assert pc.workspace_file_bytes(run.policy, str(target / "greeting.js")) is None

    def swap():
        os.rename(ws / "src", ws / "src.real")
        os.symlink(target, ws / "src", target_is_directory=True)

    swapped = _at_open(monkeypatch, victim, swap)
    sent = _pause(run, victim.as_uri())
    assert swapped and (ws / "src").is_symlink()
    _refused_not_served(run, sent, FLOOR_WORD)


def test_hard_link_of_a_floor_file_swapped_in_after_the_check_is_not_served(ws, monkeypatch):
    victim = ws / "src" / "greeting.js"
    run = _fake_run(ws, floor_tree=ws / "private")

    def swap():
        os.link(ws / "private" / "kept.js", ws / "src" / "tmp.js")
        os.replace(ws / "src" / "tmp.js", victim)

    swapped = _at_open(monkeypatch, victim, swap)
    sent = _pause(run, victim.as_uri())
    assert swapped and victim.read_text("utf-8").count(FLOOR_WORD) == 1
    _refused_not_served(run, sent, FLOOR_WORD)


def test_last_component_swapped_to_a_link_is_not_followed(ws, monkeypatch):
    victim = ws / "src" / "greeting.js"
    run = _fake_run(ws)

    def swap():
        victim.unlink()
        victim.symlink_to(ws.parent / "outside.js")

    flags = _at_open(monkeypatch, victim, swap)
    sent = _pause(run, victim.as_uri())
    _refused_not_served(run, sent, OUTSIDE_WORD)
    # The open itself neither follows a last link nor waits for a FIFO's writer.
    for name in ("O_NOFOLLOW", "O_NONBLOCK"):
        assert flags[0] & getattr(os, name), name


def _within(seconds, call):
    """``call()``'s value from a thread, or ``"hung"`` after ``seconds``."""
    import threading

    got = []
    thread = threading.Thread(target=lambda: got.append(call()), daemon=True)
    thread.start()
    thread.join(seconds)
    return got[0] if got else "hung"


def _release(fifo):
    """Let a reader that blocks in ``open`` go (only a failing run has one)."""
    try:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_is_not_served_and_never_blocks(ws, monkeypatch):
    """Only a regular file: a FIFO in the workspace (there before the check, or put in
    place of the checked file right before the open) is not answered and the thread that
    drives the browser does not wait for a writer."""
    policy = _policy(ws)
    pipe = ws / "pipe.js"
    os.mkfifo(pipe)
    try:
        assert _within(5, lambda: pc.read_workspace_file(policy, str(pipe))) == (
            pc.FULFIL_UNSERVED,
            None,
        )
        run = _fake_run(ws)
        assert _within(5, lambda: _pause(run, pipe.as_uri()))[0] == "Fetch.continueRequest"
    finally:
        _release(pipe)
    victim = ws / "src" / "greeting.js"

    def swap():
        victim.unlink()
        os.mkfifo(victim)

    swapped = _at_open(monkeypatch, victim, swap)
    try:
        code, data = _within(5, lambda: pc.read_workspace_file(policy, str(victim)))
    finally:
        _release(victim)
    assert swapped and (code, data) == (pc.FULFIL_UNSERVED, None)


@needs_browser
def test_directory_flipped_while_the_page_reads_never_leaks_the_floor(ws):
    """A local writer flips ``real/`` between a directory and a link into the read floor
    while the page reads ``real/a.js`` again and again (the review's race, in a browser)."""
    import threading

    (ws / "real").mkdir()
    (ws / "real" / "a.js").write_text("plain", "utf-8")
    (ws / "private" / "a.js").write_text(FLOOR_WORD, "utf-8")
    (ws / "race.html").write_text(
        "<!doctype html><title>Race</title><pre id=out>pending</pre><script>"
        "const seen = new Set(); let n = 0;"
        "const once = () => new Promise((done) => { const x = new XMLHttpRequest();"
        " x.onload = () => { seen.add(x.responseText); done(); }; x.onerror = () => done();"
        " try { x.open('GET', 'real/a.js?' + n++); x.send(); } catch (e) { done(); } });"
        "(async () => { for (;;) { await once();"
        " document.getElementById('out').textContent = [...seen].join('|'); } })();"
        "</script>",
        encoding="utf-8",
    )
    stop = threading.Event()

    def flip():
        while not stop.is_set():
            try:
                os.rename(ws / "real", ws / "real.keep")
                os.symlink(ws / "private", ws / "real", target_is_directory=True)
                time.sleep(0.0005)
                os.unlink(ws / "real")
                os.rename(ws / "real.keep", ws / "real")
                time.sleep(0.0005)
            except OSError:
                return

    writer = threading.Thread(target=flip, daemon=True)
    writer.start()
    try:
        policy = _policy(ws, floor_tree=ws / "private")
        result = pc.run_page_check("race.html", policy, wait_ms=3000, screenshot=False)
    finally:
        stop.set()
        writer.join(5)
    assert result["loaded"], result
    assert FLOOR_WORD not in json.dumps(result), result
    assert result["blocked"]["outside_workspace"] >= 1, result


# --- the total answered in one call ----------------------------------------------------------


def test_total_bytes_answered_in_one_call_are_capped(ws, monkeypatch):
    monkeypatch.setattr(pc, "FULFIL_TOTAL_MAX_BYTES", 10)
    for name in ("a.bin", "b.bin"):
        (ws / name).write_bytes(b"123456")
    (ws / "c.bin").write_bytes(b"1")
    run = _fake_run(ws)
    assert _pause(run, (ws / "a.bin").as_uri(), "Fetch")[0] == "Fetch.fulfillRequest"
    assert (run.fulfilled_bytes, run.reason) == (6, None)
    # 6 + 6 > 10: not answered by Stira (Chrome's own read, as before #766), and nothing
    # after it is either, also a file that would still fit.
    assert _pause(run, (ws / "b.bin").as_uri(), "Fetch")[0] == "Fetch.continueRequest"
    assert _pause(run, (ws / "c.bin").as_uri(), "Fetch")[0] == "Fetch.continueRequest"
    assert (run.fulfilled_bytes, run.reason) == (6, "fulfil_limit")
    assert run.blocked["outside_workspace"] == 0 and run.missing_count == 0
    # The rule itself still applies after the total was reached.
    assert _pause(run, (ws.parent / "outside.js").as_uri())[0] == "Fetch.failRequest"
    assert run.blocked["outside_workspace"] == 1
    assert pc.FULFIL_MAX_BYTES < 64 * 1024 * 1024  # one file is under the default total


@needs_browser
def test_missing_module_and_fetch_target_are_listed_in_a_browser(ws):
    (ws / "gone.html").write_text(
        "<!doctype html><title>Gone</title><p id=o>Title screen</p>"
        "<script type=module>"
        "try { await fetch('data/nope.json'); } catch (e) {}"
        "try { await import('./src/nope.js'); } catch (e) {}"
        "document.getElementById('o').textContent = 'done';</script>",
        encoding="utf-8",
    )
    result = pc.run_page_check("gone.html", _policy(ws), wait_ms=500, screenshot=False)
    assert "done" in result["text"], result
    assert result["missing_files"]["count"] == 2, result
    assert sorted(result["missing_files"]["first"]) == ["data/nope.json", "src/nope.js"], result
    assert "script_load_failed" in result["page_errors"]["first"], result


def test_fulfilled_type_table_is_fixed():
    assert pc.FULFIL_TYPES == {
        ".html": "text/html",
        ".htm": "text/html",
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".map": "application/json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".wasm": "application/wasm",
        ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json",
        ".bin": "application/octet-stream",
        ".txt": "text/plain",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
        ".ttf": "font/ttf",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
    }
    assert pc.fulfil_type("a/b/Main.JS") == "text/javascript"
    for name in ("x.php", "x.js.exe", "noext", ".js", "x.", "x.html\n", "x.sh"):
        assert pc.fulfil_type(name) == "application/octet-stream", name
    # The type never comes from the page, the file's content or the system's MIME table.
    assert pc.fulfil_type("x.xhtml") == pc.fulfil_type("x.pdf") == "application/octet-stream"


def test_fulfilled_headers_are_the_same_two_for_every_type(ws):
    for name, kind in ((".css", "text/css"), (".weird", "application/octet-stream")):
        (ws / f"file{name}").write_bytes(b"\x00\xff")
        run = _fake_run(ws)
        method, params, _session = _pause(run, (ws / f"file{name}").as_uri(), "Other")
        assert method == "Fetch.fulfillRequest"
        assert _headers(params) == {"Content-Type": kind, "Access-Control-Allow-Origin": "*"}
        assert base64.b64decode(params["body"]) == b"\x00\xff"


# --- a script that did not load is visible ---------------------------------------------------


def _failed(run, request_id, url, kind, error="net::ERR_FAILED"):
    run.on_event(
        {
            "method": "Network.requestWillBeSent",
            "params": {"requestId": request_id, "request": {"url": url}},
        }
    )
    run.on_event(
        {
            "method": "Network.loadingFailed",
            "params": {"requestId": request_id, "type": kind, "errorText": error},
        }
    )


def test_failed_module_load_is_a_page_error(ws):
    run = _fake_run(ws)
    _failed(run, "1", (ws / "src" / "absent.js").as_uri(), "Script", "net::ERR_FILE_NOT_FOUND")
    assert (run.error_count, run.errors) == (1, ["script_load_failed"])
    assert run.missing_count == 1  # still counted as a missing file, as before
    # Not a script, or not a workspace file request (the network has its own counters).
    _failed(run, "2", (ws / "absent.png").as_uri(), "Image")
    _failed(run, "3", "https://cdn.test/x.js", "Script")
    _failed(run, "4", "http://localhost:8000/x.js", "Script")
    assert (run.error_count, run.errors) == (1, ["script_load_failed"])
    for request_id in "567":
        _failed(run, request_id, (ws.parent / "outside.js").as_uri(), "Script")
    assert run.error_count == 4 and run.errors == ["script_load_failed"] * pc.SUMMARY_COUNT
    assert pc.for_log({"page_errors": {"count": 4, "first": run.errors}}) == {"page_errors": 4}


@needs_browser
def test_failed_module_load_is_a_page_error_in_a_browser(ws):
    (ws / "src" / "greeting.js").unlink()  # main.js imports it
    result = pc.run_page_check("index.html", _policy(ws), wait_ms=500, screenshot=False)
    assert "Title screen" in result["text"], result
    assert result["page_errors"]["count"] >= 1, result
    assert "script_load_failed" in result["page_errors"]["first"], result
    assert "greeting" not in json.dumps(result["page_errors"]), result
    assert "file:" not in json.dumps(result["page_errors"]), result

"""Root test hook: auto-attach the CSRF token to unsafe Flask test-client requests."""

import contextlib
import os
import tempfile
from pathlib import Path

from flask.testing import FlaskClient
from werkzeug.datastructures import Headers

_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

# --- Never read the checkout's .local/stira at import (#365 R-4) ----------------------
# ``dashboard/stira_server`` reads its data directory's gateway-config.json once, when it
# is imported (model id, region, port, ...). Without this, a developer's running instance
# under ``<repo>/.local/stira`` (or a STIRA_DATA_DIR in the shell) changes what the
# tests build, and the result depends on the machine. The module is imported here with an
# empty data directory; then the variable is removed again, so subprocesses, ``stira
# doctor`` and the tests that set their own STIRA_DATA_DIR see an unset one, as before.
_TEST_DATA_DIR = tempfile.TemporaryDirectory(prefix="stira-test-data-")
os.environ.pop("STIRA_DATA_DIR", None)
os.environ["STIRA_DATA_DIR"] = os.path.realpath(_TEST_DATA_DIR.name)
try:
    with contextlib.suppress(ImportError):  # optional dashboard dependencies
        import dashboard.stira_server  # noqa: F401  (its config is read now)
finally:
    del os.environ["STIRA_DATA_DIR"]

# --- Test HTTP helpers: no reverse DNS at bind (#662) ---------------------------------
# ``http.server.HTTPServer.server_bind`` asks ``socket.getfqdn(host)``. On a CI macOS
# runner that reverse lookup can take tens of seconds (it blocked the fake helper servers
# past their 20-30 s timeouts: four smoke failures and "fake model did not start"). Every
# helper here binds 127.0.0.1 and no test reads ``server_name``, so the name is the
# address. Product code does not use ``http.server``; subprocess helpers do the same in
# their own source.
import http.server as _http_server
import socketserver as _socketserver

if not getattr(_http_server.HTTPServer.server_bind, "_stira_no_reverse_dns", False):

    def _server_bind(self):
        _socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    _server_bind._stira_no_reverse_dns = True
    _http_server.HTTPServer.server_bind = _server_bind


if not getattr(FlaskClient.open, "_stira_csrf_wrapped", False):
    _original_open = FlaskClient.open

    def _open(self, *args, **kwargs):
        method = str(kwargs.get("method", "GET")).upper()
        if method in _UNSAFE:
            headers = Headers(kwargs.get("headers") or {})
            token = self.application.config.get("STIRA_CSRF_TOKEN")
            if "X-Test-No-Csrf" in headers:
                headers.remove("X-Test-No-Csrf")
            elif token and "X-CSRF-Token" not in headers:
                headers["X-CSRF-Token"] = token
            kwargs["headers"] = headers
        return _original_open(self, *args, **kwargs)

    _open._stira_csrf_wrapped = True
    FlaskClient.open = _open


# --- No cloud calls from the default test run (#209) ----------------------
# The suite must pass without AWS credentials or network. Any test that would
# actually send a request through botocore (Bedrock, KB, SageMaker, ...) fails
# loudly instead of silently depending on the developer's AWS profile.
# Opt-in live tests are marked ``live`` and are exempt.

import pytest


@pytest.fixture(autouse=True)
def _block_aws_network(request, monkeypatch):
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    try:
        from botocore.endpoint import Endpoint
    except ImportError:  # pragma: no cover - botocore is a core dependency
        yield
        return

    def _blocked(self, *args, **kwargs):
        raise RuntimeError(
            "Tests must not call AWS. Mock the model (see tests/provider_fakes.py) "
            "or mark the test 'live'."
        )

    monkeypatch.setattr(Endpoint, "_send", _blocked)
    yield


# --- Tool rules: never touch the checkout's real .local/stira (#190) ------------
# Loading the rules records them (format 2 + head) and an integrity failure leaves a
# lock marker. Tests that build ConfigStore() with its defaults get a temporary
# directory instead of the developer's .local/stira.


@pytest.fixture(autouse=True)
def _isolate_default_config_paths(tmp_path_factory, monkeypatch):
    # A developer's opt-in rollback anchor (#273) must not collect test rule files.
    monkeypatch.delenv("STIRA_TOOL_RULES_ANCHOR_DIR", raising=False)
    try:
        from stiracore.observability import config_api
    except ImportError:  # pragma: no cover - optional dashboard dependencies
        yield
        return
    root = tmp_path_factory.mktemp("default-config")
    monkeypatch.setattr(config_api, "DEFAULT_TOOL_CONFIG_PATH", root / "tool-config.json")
    monkeypatch.setattr(config_api, "DEFAULT_GATEWAY_CONFIG_PATH", root / "gateway-config.json")
    monkeypatch.setattr(config_api, "DEFAULT_AGENT_CONFIG_PATH", root / "agent-config.json")
    yield


@pytest.fixture(autouse=True)
def _isolate_default_checkpoints(tmp_path_factory, monkeypatch):
    """A StiraEngine built without a store writes to the working directory's
    ``.local/stira/checkpoints`` (#272): the dashboard tests outside ``tests/`` (which
    has its own fixture) left conversations in the checkout. Point it at a temp dir."""
    from stiracore.core import engine
    from stiracore.core.checkpoint import FileCheckpointStore

    root = tmp_path_factory.mktemp("default-checkpoints")
    monkeypatch.setattr(engine, "FileCheckpointStore", lambda: FileCheckpointStore(root))


# --- YOLO: off unless a test asks for it (ADR 0012 addendum 2026-10-24) ---------------
# Unset, the allowance is ``external`` (the button is offered; nothing is approved until a
# person presses and confirms). Most tests were written for a server without YOLO: they
# get the explicit ``off`` here, so each keeps what it checks. A test that needs YOLO sets
# its value (``monkeypatch.setenv``); a test of the default removes the variable
# (``monkeypatch.delenv("STIRA_YOLO_ALLOW")``, tests/test_yolo_showcase_default.py).


@pytest.fixture(autouse=True)
def _yolo_off_unless_asked(monkeypatch):
    if "STIRA_YOLO_ALLOW" not in os.environ:
        monkeypatch.setenv("STIRA_YOLO_ALLOW", "off")
    yield


# --- YOLO clocks off macOS: the runtime gate sees a measured clock (#296) ---------------
# ADR 0012 §7 measured the clocks on macOS only, so the product refuses a YOLO window
# elsewhere (``clock_not_measured``). The window logic itself is platform independent:
# off macOS the tests see the runtime gate (``stiracore.yolo.runtime.clock_support``) as
# measured, and the readings stay the product's own fallback (``host_ops.default_clock``:
# wall + ``time.monotonic``). Nothing changes on macOS. The refusal itself stays covered
# with explicit platforms / ``clocks_supported=False`` (test_yolo_allowance,
# test_yolo_requests); a test that needs the real gate uses ``@pytest.mark.real_yolo_clock``.


@pytest.fixture(autouse=True)
def _yolo_clock_gate_measured_off_macos(request, monkeypatch):
    try:
        from stiracore.yolo import allowance, runtime
    except ImportError:  # pragma: no cover
        yield
        return
    real = allowance.clock_support()
    if not real.supported and request.node.get_closest_marker("real_yolo_clock") is None:
        measured = allowance.ClockSupport(True, None, real.platform)
        monkeypatch.setattr(runtime, "clock_support", lambda: measured)
    yield


# --- Server apps built by a test are stopped and closed after it (#296) ----------------
# ``create_app`` registers its stops with ``atexit`` (one app per process in production)
# and holds ``server.lock`` until the process exits. A test process builds hundreds of
# apps: each kept its SQLite connections (with -wal/-shm) and its lock descriptor open,
# ~14 descriptors per app, which exhausted the macOS runner's limit of 256 (EMFILE).
# After each test the app ``atexit`` stops it registered (``stiracore`` / ``dashboard``
# functions only) run (idempotent) and are removed,
# and the data-directory locks it took are released. Higher-scoped fixtures are untouched.


@pytest.fixture(autouse=True)
def _close_what_the_test_built(monkeypatch):
    import atexit
    import gc

    from stiracore import owner_lock

    registered, locks = [], []
    real_register, real_acquire = atexit.register, owner_lock.OwnerLockFile.acquire

    def register(function, *args, **kwargs):
        # Only the app's own stops: pytest's (tmp_path cleanup) and others stay at exit.
        if getattr(function, "__module__", "").startswith(
            ("stiracore", "dashboard", "stira_server")
        ):
            registered.append(function)
        return real_register(function, *args, **kwargs)

    def acquire(self, *args, **kwargs):
        result = real_acquire(self, *args, **kwargs)
        locks.append(self)
        return result

    monkeypatch.setattr(atexit, "register", register)
    monkeypatch.setattr(owner_lock.OwnerLockFile, "acquire", acquire)
    yield
    for function in reversed(registered):
        atexit.unregister(function)
        with contextlib.suppress(Exception):
            function()
    for lock in locks:
        if lock.held:
            with contextlib.suppress(Exception):
                lock.release()
    if registered or locks:
        gc.collect()


# --- YOLO host dialog: never on the developer's screen (ADR 0012 §8b) ------------------
# A test that builds the server with STIRA_YOLO_ALLOW would otherwise check for, and
# show, the macOS host-confirmation dialog. Every osascript call answers "no" here (no
# dialog channel; a dialog that is asked for is a denial), and is counted.


@pytest.fixture(autouse=True)
def _no_yolo_host_dialog(monkeypatch):
    try:
        from stiracore.yolo import host_dialog
    except ImportError:  # pragma: no cover
        yield
        return
    calls = []

    def _fake(argv, timeout):
        calls.append(list(argv))
        return 1, ""

    monkeypatch.setattr(host_dialog, "run_osascript", _fake)
    yield calls


# --- Checkpoints: never into the checkout's .local/stira (#514) -----------------------
# ``StiraEngine`` without a ``checkpoint_store`` writes ``FileCheckpointStore()``, whose
# default is ``.local/stira/checkpoints`` under the working directory. Every test (the
# dashboard ones too, not only ``tests/``) gets its own temporary directory instead.


@pytest.fixture(autouse=True)
def _isolate_default_checkpoints(tmp_path, monkeypatch):
    try:
        from stiracore.core.checkpoint import FileCheckpointStore
    except ImportError:  # pragma: no cover
        yield
        return
    monkeypatch.setattr(
        "stiracore.core.engine.FileCheckpointStore",
        lambda: FileCheckpointStore(tmp_path / "checkpoints"),
    )
    yield


# --- The run leaves the checkout as it found it (#514) ----------------------------------
# Files the run creates in (or removes from) the checkout, ``.local/`` included (git
# ignores it), fail the run with the list; what is skipped is in tests/tree_guard.py.
# ``--no-tree-guard`` turns the check off.

_REPO = Path(__file__).resolve().parent


def pytest_addoption(parser):
    parser.addoption(
        "--no-tree-guard",
        action="store_true",
        help="do not fail the run for files it leaves in (or removes from) the checkout",
    )


def pytest_sessionstart(session):
    if session.config.getoption("--no-tree-guard", default=False):
        return
    from tests import tree_guard

    skip = tuple(tree_guard.owned_data_dirs(_REPO))
    # Left out of both snapshots, decided once here: the report file STIRA_CONTRACT_REPORT
    # asks for (CI) is an intended output, whether or not it exists already.
    exclude = tree_guard.excluded_paths(_REPO, os.environ)
    session.config._stira_tree = (skip, exclude, tree_guard.tree_files(_REPO, skip, exclude))


def pytest_sessionfinish(session, exitstatus):
    guard = getattr(session.config, "_stira_tree", None)
    if guard is None:
        return
    from tests import tree_guard

    skip, exclude, before = guard
    after = tree_guard.tree_files(_REPO, skip, exclude)
    changes = tree_guard.tree_changes(before, after)
    if not changes:
        return
    shown = "\n".join(f"  {line}" for line in changes[:30])
    more = f"\n  ... and {len(changes) - 30} more" if len(changes) > 30 else ""
    message = (
        "\nThe test run changed the checkout (#514): a test writes outside tmp_path.\n"
        f"{shown}{more}\n"
        "Point that test at tmp_path; --no-tree-guard turns this check off.\n"
    )
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(message, red=True)
    else:  # pragma: no cover
        print(message)
    if session.exitstatus == 0:
        session.exitstatus = 1

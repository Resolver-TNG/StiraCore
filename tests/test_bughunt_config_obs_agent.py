"""#601 pre-release review, area D2 (config / observability / agent): regressions.

Most tests reproduce one verified finding and fail on the code before the fix. The
guards that also pass before it are marked "(guard)": they pin what the fix must keep
(finished recoveries stay resolved, metadata and masked text stay as they are, the
mirror's kind set). Offline: fakes, temporary files and the offline chat server app.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import stat
import threading
import time
from types import SimpleNamespace

import pytest

from stiracore.agent.checkpoint_hooks import ApprovalBindingVerifier
from stiracore.core.approval import ApprovalBinding, OperationLedger, args_hash
from stiracore.observability.masking import mask_secrets

INPUT = {"path": "notes.txt", "content": "x"}


def _engine(ledger=None, blocked=("ext_write",)):
    return SimpleNamespace(
        agent_id="agent-1",
        _now_fn=time.time,
        operation_ledger=ledger or OperationLedger(),
        config=SimpleNamespace(blocked_tool_names=frozenset(blocked)),
    )


def _event(tool_use_id="tu-1", name="ext_write", state=None):
    return SimpleNamespace(
        tool_use={"toolUseId": tool_use_id, "name": name, "input": dict(INPUT)},
        invocation_state={} if state is None else state,
        cancel_tool=False,
    )


# --- D2: a ledger that fails after an approval refuses the call (no exception) ------


def test_approved_call_whose_ledger_claim_raises_is_cancelled_not_raised():
    class BrokenLedger:
        def mark_started(self, *args, **kwargs):
            raise OSError("disk full")

    verifier = ApprovalBindingVerifier(_engine(ledger=BrokenLedger()))
    event = _event()
    binding = ApprovalBinding(
        request_id="req-1",
        agent_id="agent-1",
        tool_name="ext_write",
        args_hash=args_hash(INPUT),
        expires_at=time.time() + 60,
    )
    from stiracore.agent import checkpoint_hooks

    event.invocation_state[checkpoint_hooks.HITL_BINDINGS_KEY] = {"tu-1": binding}
    verifier.before_tool(event)  # must not raise
    assert event.cancel_tool
    assert "ledger" in event.cancel_tool


# --- D5: "not recovered yet" is not a resolution ------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "障害はまだ復旧していない",
        "障害の復旧作業中",
        "サーバーが落ちて復旧していない",
        "障害が復旧せず困っている",
        "障害はまだ収束していない",
        "障害が起きて元に戻らない",
    ],
)
def test_unfinished_recovery_keeps_the_threat(text):
    from stiracore.agent.ja_hybrid.classifier import JaHybridClassifier

    verdict = JaHybridClassifier(emotion_layer=False).analyze(text)
    assert "resolved" not in verdict.discourse_tags
    assert verdict.category == "threat"


@pytest.mark.parametrize(
    "text",
    [
        "障害は復旧した",
        "障害が発生しましたが、復旧しました",
        "障害の復旧作業が完了しました",
        "障害の復旧作業は終了し、復旧しました",
    ],
)
def test_finished_recovery_still_resolves(text):
    """(guard; the 復旧作業 cases fail on round 2 of #607)"""
    from stiracore.agent.ja_hybrid.classifier import JaHybridClassifier

    verdict = JaHybridClassifier(emotion_layer=False).analyze(text)
    assert "resolved" in verdict.discourse_tags
    assert verdict.category != "threat"  # "完了しました" may read as an achievement


# --- D6: load and the SDK copy take the shared state lock ---------------------------


class _RecordingLock:
    def __init__(self):
        self._lock = threading.Lock()  # not reentrant (#601 review): nested use would hang
        self.entered = 0

    def __enter__(self):
        assert self._lock.acquire(timeout=2), "state_lock taken twice (nested)"
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


def test_state_lock_guards_load_and_inject(tmp_path):
    from stiracore.agent.state_persistence import StatePersistence
    from stiracore.core import NeuralState

    lock = _RecordingLock()
    persistence = StatePersistence(NeuralState(), tmp_path / "s.json", state_lock=lock)
    persistence.save()
    saved = lock.entered
    persistence.load()
    assert lock.entered > saved, "load must restore under the state lock"
    loaded = lock.entered
    agent = SimpleNamespace(state=SimpleNamespace(set=lambda key, value: None))
    persistence.inject_agent_state(agent)
    assert lock.entered > loaded, "the SDK copy must be read under the state lock"


# --- D7: a child refuses a conversation reset (its delegate is not rebuilt) ----------


def test_subagent_refuses_start_new_conversation_and_keeps_its_delegate():
    from stiracore.agent.subagent import NXPSubagent

    child = NXPSubagent.__new__(NXPSubagent)
    child.session_manager = None
    sentinel = object()
    child.agent = sentinel
    with pytest.raises(RuntimeError):
        child.start_new_conversation()
    assert child.agent is sentinel


# --- D8: a Windows junction (reparse point) is not a plain directory ----------------


def test_reparse_point_is_refused(tmp_path, monkeypatch):
    from stiracore.config import data_dir

    (tmp_path / "workspace").mkdir()
    real = os.lstat

    def fake_lstat(path, *args, **kwargs):
        info = real(path, *args, **kwargs)
        if os.fspath(path).endswith("workspace"):
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(data_dir.os, "lstat", fake_lstat)
    problem = data_dir.name_problem(tmp_path, "workspace")
    assert problem is not None and "reparse" in problem


# --- D9/D10: changing workspace_dir ends session allowances at save -----------------


def test_workspace_dir_change_bumps_roots(tmp_path, monkeypatch):
    from stiracore.config import models

    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"write_roots": ["@workspace"], "workspace_dir": "/a"}))
    bumps = []

    class Table:
        def bump_roots(self):
            bumps.append(1)

    from stiracore.yolo import session_allow

    monkeypatch.setattr(session_allow, "table", lambda: Table())
    models._publish_roots_change(path, {"write_roots": ["@workspace"], "workspace_dir": "/b"})
    assert bumps == [1]
    models._publish_roots_change(path, {"write_roots": ["@workspace"], "workspace_dir": "/a"})
    assert bumps == [1]


# --- D25: a failed episode record does not replace a successful reply ---------------


def test_chat_reply_survives_a_failed_episode_record(monkeypatch):
    from stiracore.integrations import runtime

    session = runtime.ChatSession(lambda message: "hello back", store=object(), emotion=False)
    session.status = dict

    def broken_record(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(runtime, "record_episode", broken_record)
    monkeypatch.setattr(runtime, "emotion_snapshot_enabled", lambda _e: False)
    result = session.runner("hi")
    assert result.text == "hello back"


# --- D27/D28: the mirror's kind set matches the approval store's --------------------


def test_mirror_kinds_match_the_approval_store_kinds():
    """(guard)"""
    from stiracore.core.approval_store import EVENT_KINDS
    from stiracore.observability.audit_chain import APPROVAL_KINDS

    assert APPROVAL_KINDS == EVENT_KINDS


# --- D29: any acquisition timeout of the decision guard is BUSY ---------------------


def test_decision_guard_timeout_error_is_busy():
    from stiracore.observability.chat_api import DecideOutcome, HITLApprovalBroker

    @contextlib.contextmanager
    def guard():
        raise TimeoutError("host lock")
        yield  # pragma: no cover

    broker = HITLApprovalBroker(decision_guard=guard)
    assert broker.decide("missing", "approve") == DecideOutcome.BUSY


# --- D30: an unreadable approval store refuses a conversation reset -----------------


def test_strict_pending_raises_on_store_failure():
    from stiracore.core.approval_store import ApprovalStoreError
    from stiracore.observability.chat_api import HITLApprovalBroker

    class BrokenStore:
        def expire_due(self):
            raise ApprovalStoreError("unreadable")

        def pending(self):  # pragma: no cover - not reached
            return []

    broker = HITLApprovalBroker()
    broker._store = BrokenStore()
    broker.sync_from_store = lambda: None
    assert broker.pending() == []  # the display list stays best effort
    with pytest.raises(ApprovalStoreError):
        broker.pending(strict=True)


@pytest.fixture
def stira(tmp_path, monkeypatch):
    from tests.test_new_session import make_stira

    yield from make_stira(tmp_path, monkeypatch)


def test_new_session_is_refused_while_the_approval_store_cannot_be_read(stira, monkeypatch):
    """/new through the API: an unreadable store is 503 approvals_unavailable, nothing is
    reset; the display list stays best effort; once readable, the same key runs."""
    from stiracore.core.approval_store import ApprovalStoreError

    app, client = stira["app"], stira["client"]
    stira["chat"]("hello")
    store = app.extensions["approval_store"]

    def unreadable(*args, **kwargs):
        raise ApprovalStoreError("unreadable")

    monkeypatch.setattr(store, "pending", unreadable)
    refused = stira["new"]("d30")
    assert refused.status_code == 503
    assert refused.get_json()["code"] == "approvals_unavailable"
    assert len(client.get("/api/chat/history").get_json()["turns"]) == 1
    assert client.get("/api/hitl/pending").status_code == 200
    monkeypatch.setattr(store, "pending", type(store).pending.__get__(store))
    assert stira["new"]("d30").status_code == 200
    assert client.get("/api/chat/history").get_json()["turns"] == []


# --- D31: an unbindable value is refused without breaking the store -----------------


@pytest.mark.parametrize(
    "fields",
    [{"duration_ms": 10**100}, {"model_id": {"not": "text"}}],
)
def test_unbindable_event_is_refused_and_the_store_keeps_writing(tmp_path, fields):
    from stiracore.observability.event_store import SQLiteEventStore
    from stiracore.observability.hooks import ObservabilityEvent

    store = SQLiteEventStore(tmp_path / "events.db")
    with pytest.raises(ValueError):
        store.emit(ObservabilityEvent(event_type="turn", message="bad", **fields))
    assert store.error is None
    good = ObservabilityEvent(event_type="turn", message="good")
    store.emit(good)
    assert store.error is None
    receipt = store.receipt(good.event_id)  # written and read back, not silently dropped
    assert receipt is not None and receipt["event_type"] == "turn"
    assert store.verify_chain() == (True, None)


def test_a_refused_event_never_breaks_the_turn_hooks(tmp_path):
    """The dashboard hooks isolate sink failures (#601 review M2): a refused row is
    dropped with a warning, the next one is stored."""
    from stiracore.observability.event_store import SQLiteEventStore
    from stiracore.observability.hooks import DashboardHookProvider, ObservabilityEvent

    store = SQLiteEventStore(tmp_path / "events.db")
    hooks = DashboardHookProvider(store)
    hooks._emit(ObservabilityEvent(event_type="turn", duration_ms=10**100))  # no raise
    good = ObservabilityEvent(event_type="turn", message="good")
    hooks._emit(good)
    assert store.receipt(good.event_id) is not None


# --- D32/D33: secret-named values are masked whole ----------------------------------


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("password=123456", "123456"),
        ("pwd=abc", "abc"),
        ("password=realpassword***", "realpassword"),
        ('password="alpha beta"', "beta"),
        ("password='alpha beta'", "beta"),
        ("password=" + "a" * 5000, "a" * 8),
        ('password="' + "a" * 4096 + ' tailsecret"', "tailsecret"),
        ('password="ab\\"cd tailsecret"', "tailsecret"),
        ('password="unclosed tailsecret', "tailsecret"),
        # (guard) passes on main too; it failed on round 1 of the fix (review of #607)
        ('message="request failed password=banana"', "banana"),
        ("message='request failed' password=\"two words\"", "words"),
        (r'password="alpha ?token=abc\" tailsecret"', "tailsecret"),
        ("https://example.test/?token=" + "a" * 4096 + ",tailsecret", "tailsecret"),
        ("https://example.test/?token=a," + "b" * 5000, "b" * 8),
        ('{"password": "a b"}', "b"),
        ('{"token": "x\\"y z", "user": "bob"}', "z"),
    ],
    ids=[
        "digits",
        "short",
        "trailing-mask",
        "dq-space",
        "sq-space",
        "over-4096",
        "quoted-over-4096-with-space",
        "escaped-quote",
        "unclosed-quote",
        "inside-a-non-secret-quoted-value",
        "after-a-non-secret-quoted-value",
        "url-param-inside-escaped-quote",
        "url-param-over-4096-with-comma",
        "url-param-comma-then-long",
        "json-space",
        "json-escaped",
    ],
)
def test_secret_named_value_is_masked_whole(text, leak):
    assert leak not in mask_secrets(text)


@pytest.mark.parametrize(
    "text",
    [
        "max_tokens=4096",
        "token_count=12",
        "password=***",
        'password="***"',
        "Authorization: Bearer ***",
        "flag=true",
        'message="request failed: disk full" status=error',
        "note='a b c' user=bob",
        '{"model_id": "m 1", "max_tokens": 4096}',
        '{"password": "***", "user": "bob"}',
        "https://example.test/?q=a,b&page=2",
    ],
)
def test_masking_keeps_metadata_and_is_idempotent(text):
    """(guard)"""
    assert mask_secrets(text) == text


# --- D35: an unexpected construction error becomes a result -------------------------


def test_connection_test_builder_error_is_a_result():
    from stiracore.providers.connection_test import run_connection_test
    from stiracore.providers.spec import ModelSpec

    def builder(_spec):
        raise RuntimeError("secret-ish detail")

    spec = ModelSpec(provider="bedrock", model_id="m")
    result = run_connection_test(spec, model_builder=builder, environ={})
    assert result.ok is False
    assert "secret-ish" not in json.dumps(result.as_dict())


# --- D33 round 4: every form of a secret-named assignment, escaped and wrapped -------

_TAIL = "Zq9tailsecret"


def _secret_forms():
    two = "alpha " + _TAIL
    return {
        "eq-bare": f"password={_TAIL}",
        "colon-bare": f"password: {_TAIL}",
        "eq-dq": f'password="{two}"',
        "eq-sq": f"password='{two}'",
        "colon-dq": f'password: "{two}"',
        "json": json.dumps({"password": two}),
        "json-compact": '{"password":"' + two + '"}',
        "json-nl": '{"password":\n  "' + two + '"}',
        "json-crlf": '{"password":\r\n"' + two + '"}',
        "json-indent": json.dumps({"password": two, "user": "bob"}, indent=2),
        "api_key-json": json.dumps({"api_key": two}),
        "escaped-quote-inside": 'token="ab\\"cd ' + _TAIL + '"',
        # Round 5 (review of #607): JSON's own \u0022, YAML '', long JSON whitespace.
        "unicode-quote": '{"message":"password=\\u0022' + two + '\\u0022 user=bob"}',
        "unicode-sq": '{"message":"password=\\u0027' + two + '\\u0027 user=bob"}',
        "yaml-sq-doubled": "password: 'alpha'' " + _TAIL + "' user=bob",
        "json-long-gap-lf": '{"password":' + " " * 80 + '\n"' + two + '","user":"bob"}',
        "json-long-gap-crlf": '{"password":' + " " * 80 + '\r\n"' + two + '","user":"bob"}',
        "trailing-backslash": 'password="alpha ' + _TAIL + "\\" * 2 + '" user=bob',
    }


def _wrapped(text):
    return {
        "plain": text,
        "in-json": json.dumps({"message": text, "user": "bob"}),
        "in-json-twice": json.dumps(json.dumps({"message": text, "user": "bob"})),
        "in-log-line": f"2026-10-24 INFO request {text} done user=bob",
    }


_COMBOS = [
    (f"{form}/{wrap}", text)
    for form, base in _secret_forms().items()
    for wrap, text in _wrapped(base).items()
]


@pytest.mark.parametrize("text", [t for _, t in _COMBOS], ids=[i for i, _ in _COMBOS])
def test_no_form_of_a_secret_named_value_leaves_its_body(text):
    """72 combinations: = / : / JSON, quoted or not, escaped once or twice inside a JSON
    string, with or without a line break before a JSON value, \\u0022, YAML '', long
    JSON whitespace. The secret body never remains, and what follows the value (the
    "user": "bob" metadata) is never masked with it."""
    out = mask_secrets(text)
    assert "tailsecret" not in out
    assert out.count("bob") == text.count("bob"), out


def test_review_r4_cases():
    escaped = json.dumps({"message": 'password="alpha tailsecret"'})
    assert "tailsecret" not in mask_secrets(escaped)
    assert "tailsecret" not in mask_secrets(json.dumps(json.dumps({"m": escaped})))
    assert "tailsecret" not in mask_secrets('{"password":\n  "alpha tailsecret"}')


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_line_break_ends_a_quoted_value_even_after_a_backslash(newline):
    out = mask_secrets(f'password="ab\\{newline}next line stays" user=bob')
    assert "ab" not in out.split(newline)[0].split("password=")[1]
    assert out.split(newline)[1] == 'next line stays" user=bob'


@pytest.mark.parametrize(
    "text",
    [
        'password:\n"unrelated" log line',  # a bare key never reaches the next line
        "password:\nnext line",
        '{"password": "***", "user": "bob"}',
        json.dumps({"message": "user=bob status=ok"}),
    ],
)
def test_masking_does_not_reach_past_its_value(text):
    """(guard)"""
    assert mask_secrets(text) == text


def test_review_r5_cases():
    """The four inputs of the round-5 review (each failed on round 4)."""
    unicode_quoted = r'{"message":"password=\u0022alpha tail secret\u0022"}'
    assert " tail " not in mask_secrets(unicode_quoted)
    yaml = "password: 'alpha'' tail secret' user=bob"
    assert " tail " not in mask_secrets(yaml)
    assert mask_secrets(yaml).endswith(" user=bob")
    for newline in ("\n", "\r\n"):
        gap = '{"password":' + " " * 80 + newline + '"alpha tail secret","user":"bob"}'
        out = mask_secrets(gap)
        assert " tail " not in out
        assert out.endswith('"user":"bob"}')
    nested = json.dumps({"message": 'password="alpha' + "\\" * 2 + '" user=bob'})
    assert "user=bob" in mask_secrets(nested)


def test_unrecognized_value_forms_fall_back_to_the_rest_of_the_line():
    """A secret-named key with a separator never leaves its value: a form the masker
    does not know is masked to the end of its line (not the next line)."""
    for text in (
        "password=&quot;alpha SECRETTAIL&quot;",
        'password=ab"cd SECRETTAIL"',
        "password=<alpha SECRETTAIL>",
    ):
        out = mask_secrets(text + "\nnext line")
        assert "SECRETTAIL" not in out, out
        assert out.endswith("\nnext line")


def _fuzz_value(rng):
    alphabet = ["a", "b", "Z", "9", " ", "\\", '"', "'", "\\u0022", "''", "\t", ":", "=", ","]
    body = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6)))
    tail = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6)))
    return body + "SECRETTAIL" + tail


def _fuzz_case(rng):
    key = rng.choice(["password", "token", "api_key", "client_secret", "passwd"])
    value = _fuzz_value(rng)
    mode = rng.choice(["bare", "dq", "sq-yaml", "json-key", "unicode"])
    sep = rng.choice(["=", ": ", ":", " = "])
    if mode == "bare":
        bare = "".join(c for c in value if c.isalnum() or c in "\\:=")
        text = f"{key}{sep}{bare}"
    elif mode == "dq":
        text = f"{key}{sep}" + json.dumps(value.replace("\n", " "))
    elif mode == "sq-yaml":
        text = f"{key}{sep}'" + value.replace("'", "''") + "'"
    elif mode == "json-key":
        gap = rng.choice(["", " ", "\n  ", "\r\n", " " * rng.randint(9, 120)])
        text = "{" + json.dumps(key) + ":" + gap + json.dumps(value) + ', "user": "bob"}'
    else:  # the value quoted with JSON's \u0022 inside a JSON string
        inner = f"{key}{sep}" + json.dumps(value)
        encoded = json.dumps(inner)[1:-1]
        encoded = re.sub(r'(?<!\\)((?:\\\\)*)\\"', lambda m: m.group(1) + "\\u0022", encoded)
        text = '{"message":"' + encoded + '","user":"bob"}'
    for _ in range(rng.randint(0, 2)):
        text = json.dumps({"message": text, "user": "bob"})
    if rng.random() < 0.3:
        text = "INFO " + text + " user=bob"
    return text


def test_fuzz_a_secret_body_never_survives():
    """3,000 generated inputs: secret-named key + separator + a value of quotes,
    backslashes, \\u0022, YAML '', tabs and spaces (encoded as its form requires),
    wrapped in JSON 0-2 times. SECRETTAIL never remains in the output."""
    rng = random.Random(601)
    for _ in range(3000):
        text = _fuzz_case(rng)
        out = mask_secrets(text)
        assert "SECRETTAIL" not in out, (text, out)


# --- D33 round 6: the key side (whitespace before the separator, escapes) -----------

_PW = "pass" + "word"
_ST = "SECRET" + "TAIL"


@pytest.mark.parametrize(
    "text",
    [
        '{"' + _PW + '"' + " " * 9 + ': "alpha ' + _ST + '"}',
        '{"' + _PW + '"' + " " * 200 + ': "alpha ' + _ST + '"}',
        '{"' + _PW + '"\n: "alpha ' + _ST + '"}',
        '{"' + _PW + '"\r\n\t: "alpha ' + _ST + '"}',
        _PW + "\u00a0=alpha_" + _ST,
        _PW + "\u3000:\u3000alpha_" + _ST,
        '{"pass\\u0077ord": "alpha ' + _ST + '", "user": "bob"}',
        '{"PASS\\u0057ORD": "alpha ' + _ST + '", "user": "bob"}',
        '{"' + _PW + "\\u0022: \\u0022alpha " + _ST + "\\u0022}",
        "\\" * 15 + '"' + _PW + "\\" * 15 + '": ' + "\\" * 15 + '"alpha ' + _ST + "\\" * 15 + '"',
    ],
    ids=[
        "9-spaces-before-colon",
        "200-spaces-before-colon",
        "lf-before-colon",
        "crlf-tab-before-colon",
        "nbsp-before-equals",
        "ideographic-space",
        "unicode-escaped-key",
        "unicode-escaped-upper-key",
        "unicode-quote-key-close",
        "15-backslashes-key-close",
    ],
)
def test_review_r6_key_side(text):
    assert _ST not in mask_secrets(text)


def test_four_level_json_wrapping_masks_and_keeps_metadata():
    text = json.dumps({_PW: "alpha " + _ST, "user": "bob"})
    for _ in range(4):
        text = json.dumps({"m": text, "user": "bob"})
    out = mask_secrets(text)
    assert _ST not in out
    assert out.count("bob") == text.count("bob")


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
@pytest.mark.parametrize("value", ["123456", "true", "null", "word9"])
def test_json_value_after_a_line_break_is_masked(newline, value):
    out = mask_secrets('{"' + _PW + '":' + newline + value + ', "user": "bob"}')
    if value not in ("true", "null"):  # kept as non-values ("password: null")
        assert value not in out
    assert out.endswith('"user": "bob"}')


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"], ids=["lf", "crlf", "cr"])
def test_a_bare_key_never_reaches_the_next_line(newline):
    """(guard) a bare "password:" / "password" at a line end leaves the next line."""
    for text in (
        _PW + ":" + newline + "next line 123456",
        _PW + newline + ": next line 123456",
        "INFO " + _PW + "=" + newline + "user=bob",
    ):
        assert mask_secrets(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "tokens=1024",
        "max_tokens: 4096",
        "token_count=12",
        _PW + "_policy: strict",
        _PW + "_min_length=12",
        '{"' + _PW + '_policy": "strict", "user": "bob"}',
        "secret_type: aws",
        '{"token_usage": {"input": 10}}',
        "passwordless=true",
    ],
)
def test_non_secret_names_are_not_masked(text):
    """(guard) names about a secret, and counters, are metadata."""
    assert mask_secrets(text) == text


_SPACES_LINE = [" ", "\t", "\u00a0", "\u3000", "\u2003"]
_SPACES_ANY = [*_SPACES_LINE, "\n", "\r", "\r\n"]


def _fuzz_key(rng):
    name = rng.choice([_PW, "to" + "ken", "api_key", "client_secret", "pass" + "wd"])
    if rng.random() < 0.3:
        name = "".join(c.upper() if rng.random() < 0.5 else c for c in name)
    if rng.random() < 0.3:
        name = "".join(
            f"\\u{ord(c):04x}" if c.isalpha() and rng.random() < 0.3 else c for c in name
        )
    return name


def _fuzz_value_r6(rng, quoted_key):
    body = "".join(
        rng.choice(["a", "Z", "9", " ", "\\", '"', "'", "\\u0022", "\t", ",", "}", "]"])
        for _ in range(rng.randint(0, 5))
    )
    kind = rng.choice(["string", "number", "bare", "array", "object"])
    if kind == "string":
        return json.dumps(body + _ST + body), _ST
    if kind == "number":
        return "987654321", "987654321"
    if kind == "bare":
        return "w9" + _ST, _ST
    if kind == "array":
        return "[1, " + json.dumps(body + _ST) + ', {"x": [2]}]', _ST
    return '{"v": ' + json.dumps(_ST + body) + ', "n": {"m": 1}}', _ST


def _fuzz_r6(rng):
    quoted = rng.random() < 0.7
    key = _fuzz_key(rng)
    spaces = _SPACES_ANY if quoted else _SPACES_LINE
    before = "".join(rng.choice(spaces) for _ in range(rng.choice([0, 1, 3, 9, 80, 200])))
    after = "".join(rng.choice(spaces) for _ in range(rng.choice([0, 1, 2, 12])))
    value, secret = _fuzz_value_r6(rng, quoted)
    if quoted:
        text = '{"' + key + '"' + before + ":" + after + value + ', "user": "bob"}'
    else:
        if value.startswith(("[", "{", '"')):
            value = "w9" + _ST
            secret = _ST
        text = "INFO " + key + before + rng.choice(["=", ":"]) + after + value + " user=bob"
    for _ in range(rng.randint(0, 3)):
        text = json.dumps({"message": text, "user": "bob"}, ensure_ascii=rng.random() < 0.5)
    if rng.random() < 0.3:
        text = "line one user=bob\n" + text + "\nline three user=bob"
    return text, secret


@pytest.mark.parametrize("seed", [601, 602, 603])
def test_fuzz_r6_key_side_and_value_types(seed):
    """2,000 inputs per seed over: key spelling (case, \\uXXXX escapes), whitespace
    before the separator (0-200, ASCII/tab/Unicode, line breaks after a quoted key),
    after it, value type (string/number/bare/array/object), JSON wrapping 0-3 levels,
    one or several lines. The secret never remains; the following "user": "bob" /
    user=bob metadata (and the other lines) always do."""
    rng = random.Random(seed)
    for _ in range(2000):
        text, secret = _fuzz_r6(rng)
        out = mask_secrets(text)
        assert secret not in out, (text, out)
        assert out.count("bob") == text.count("bob"), (text, out)


def test_a_key_right_after_an_escaped_line_break_is_found():
    """A multi-line log inside a JSON string: "...\\npassword=..." (the key follows
    an escaped line break, not a name character)."""
    text = json.dumps({"log": "line one\n" + _PW + "=w9" + _ST + " user=bob", "user": "bob"})
    out = mask_secrets(text)
    assert _ST not in out
    assert out.count("bob") == 2

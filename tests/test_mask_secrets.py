"""#185: secret masking formats, false positives, and coverage of every exposure path.

Fake credentials are assembled at runtime so that no credential-shaped literal is
committed (and secret scanners do not flag the test file).
"""

from __future__ import annotations

import hashlib
import json
import random
import string
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from flask import Flask

from stiracore.observability import masking, security, turn_events
from stiracore.observability.chat_api import HITLApprovalBroker, create_chat_blueprint
from stiracore.observability.hooks import create_dashboard_hooks
from stiracore.observability.masking import MASK, looks_high_entropy, mask_secrets
from stiracore.observability.turn_events import (
    TurnEventRecorder,
    TurnEventStore,
    UIEventSink,
    capture_payload,
    make_preview,
    sanitize_data,
)

_RNG = random.Random(185)
_B62 = string.ascii_letters + string.digits


def _rand(n: int, alphabet: str = _B62) -> str:
    # Guarantee the mixed classes a real random credential has with high probability.
    while True:
        text = "".join(_RNG.choice(alphabet) for _ in range(n))
        if any(c.isupper() for c in text) and any(c.isdigit() for c in text):
            return text


GH_CLASSIC = "gh" + "p_" + _rand(36)
GH_OAUTH = "gh" + "o_" + _rand(36)
GH_PAT = "github" + "_pat_" + _rand(22) + "_" + _rand(59)
SLACK_BOT = "xo" + "xb-" + "1234567890-" + "0987654321-" + _rand(24)
SLACK_APP = "xa" + "pp-" + "1-A0B1C2D3E4F-" + _rand(30)
GOOGLE = "AI" + "za" + _rand(35, _B62 + "_-")
JWT = ".".join(
    ["ey" + "J" + _rand(30, _B62 + "_-"), "ey" + "J" + _rand(40, _B62 + "_-"), _rand(43, _B62)]
)
PASSWORD = "S3cr3t-" + _rand(10)
URL_CREDS = f"https://deploy:{PASSWORD}@git.example.test/org/repo.git"
PEM_BODY = "\n".join(_rand(64, _B62 + "+/") for _ in range(4))
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\n" + PEM_BODY + "\n-----END " + "RSA PRIVATE KEY-----"
HIGH_ENTROPY = _rand(40)
AWS_ID = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
ENV_VALUE = _rand(40, _B62 + "/+")
ENV_LINE = f"AWS_SECRET_ACCESS_KEY={ENV_VALUE}"

# (raw text containing a secret, the secret itself, what must remain visible)
POSITIVE = [
    (f"token {GH_CLASSIC} end", GH_CLASSIC[4:], "gh" + "p_" + MASK),
    (f"oauth {GH_OAUTH}", GH_OAUTH[4:], "gh" + "o_" + MASK),
    (f"pat {GH_PAT}", GH_PAT[11:], "github" + "_pat_" + MASK),
    (f"slack {SLACK_BOT}", SLACK_BOT[5:], "xo" + "xb-" + MASK),
    (f"slack {SLACK_APP}", SLACK_APP[5:], "xa" + "pp-" + MASK),
    (f"maps {GOOGLE}", GOOGLE[4:], "AI" + "za" + MASK),
    (f"jwt {JWT}", JWT[3:], "jwt ey" + "J" + MASK),
    (
        f"git clone {URL_CREDS}",
        PASSWORD,
        "https://deploy:" + MASK + "@git.example.test/org/repo.git",
    ),
    (f"key:\n{PEM}\nafter", PEM_BODY.split("\n")[1], "PRIVATE KEY-----" + MASK + "-----END"),
    (f"rand {HIGH_ENTROPY} end", HIGH_ENTROPY, f"rand {MASK} end"),
    (ENV_LINE, ENV_VALUE, "AWS_SECRET_ACCESS_KEY=" + MASK),
    (f'export OPENAI_API_KEY="{HIGH_ENTROPY}"', HIGH_ENTROPY, f'OPENAI_API_KEY="{MASK}"'),
    ('{"password": "' + PASSWORD + '"}', PASSWORD, '{"password": "' + MASK + '"}'),
    (f"GET /cb?access_token={PASSWORD}&x=1", PASSWORD, f"access_token={MASK}&x=1"),
    (f"Authorization: Basic {_rand(24)}=", None, "Authorization: " + MASK),
    (f"id {AWS_ID} here", AWS_ID[4:], "id AKIA****"),
    (f"use Bearer {_rand(20)}.{_rand(8)} ok", None, "Bearer " + MASK + " ok"),
    (f"hook https://hooks.slack.com/services/T0/B0/{_rand(24)}", None, "services/" + MASK),
]


@pytest.mark.parametrize(("text", "secret", "visible"), POSITIVE)
def test_known_formats_are_masked(text, secret, visible):
    masked = mask_secrets(text)
    if secret is not None:
        assert secret not in masked
    assert visible in masked
    assert mask_secrets(masked) == masked  # idempotent: callers mask more than once


def test_truncated_pem_preview_is_masked():
    # A preview cut before -----END must still hide the key body.
    cut = PEM[: len(PEM) // 2]
    masked = mask_secrets(f"out: {cut}")
    assert PEM_BODY.split("\n")[0] not in masked
    assert masked.endswith(MASK + "-----END " + "RSA PRIVATE KEY-----")


SHA1 = hashlib.sha1(b"stira").hexdigest()
SHA256 = hashlib.sha256(b"stira").hexdigest()
NEGATIVE = [
    f"commit {SHA1}",
    f"sha256:{SHA256}",
    f"digest {SHA256.upper()}",
    f"uuid {uuid.UUID(int=185)} hex {uuid.UUID(int=186).hex}",
    "snowflake 123456789012345678901234567890123456",
    "/Users/me/projects/StiraCore/src/stiracore/observability/masking.py",
    "repos/Resolver-TNG/StiraCore/pulls/221/files?page=2",
    "class AbstractSingletonProxyFactoryBean2Impl3 extends HTTPServerRequest2HandlerFactory",
    "getUserById2AndThenFetchOrders3FromDatabase",
    "very_long_function_name_with_words_and_version_2",
    "global.anthropic.claude-sonnet-5-5 / anthropic-claude-sonnet-5-5-20261001-v1",
    "chat.tool_input_must_be_an_object",
    "tooluse_AbCdEf123456789xyzABCd",
    "https://github.com/example-org/example-repo/issues/185",
    "https://user@example.test/path",
    "-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIU\n-----END CERTIFICATE-----",
    "max_tokens=4096 input_tokens: 120 token_count=abcdef",
    "the token is invalid; password: no",
    "primary_key=user_id monkey=bananas keyboard: qwerty",
    "gh" + "p_short",
    "sk-x",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_ordinary_ids_and_text_are_not_masked(text):
    assert mask_secrets(text) == text


def test_mapping_keys_use_word_segments():
    masked = mask_secrets(
        {
            "api_key": "a",
            "apiKey": "b",
            "x-api-key": "c",
            "Authorization": "d",
            "session_token": "e",
            "client_secret": "f",
            "password": "g",
            "aws_secret_access_key": "h",
            "nested": [{"privateKey": "i"}],
            # Not credentials: memory_write's key, usage counters, substrings.
            "key": "episodic-1",
            "max_tokens": 5,
            "inputTokens": 3,
            "token_count": 7,
            "tokenizer": "bpe",
            "monkey": "m",
            "keyboard": "k",
        }
    )
    for name in (
        "api_key",
        "apiKey",
        "x-api-key",
        "Authorization",
        "session_token",
        "client_secret",
        "password",
        "aws_secret_access_key",
    ):
        assert masked[name] == MASK, name
    assert masked["nested"] == [{"privateKey": MASK}]
    assert masked["key"] == "episodic-1" and masked["max_tokens"] == 5
    assert masked["inputTokens"] == 3 and masked["token_count"] == 7
    assert (masked["tokenizer"], masked["monkey"], masked["keyboard"]) == ("bpe", "m", "k")


def test_high_entropy_rule_rates():
    """Measured: random credentials are caught; hex digests and UUIDs never are."""
    rng = random.Random(1850)

    def rnd(n, alphabet=_B62):
        return "".join(rng.choice(alphabet) for _ in range(n))

    caught = sum(looks_high_entropy(rnd(40)) for _ in range(1000))
    # Measured with this seed: 910/1000 (40 chars), 978/1000 (64 chars). A heuristic
    # fallback only; known credential formats above are matched exactly.
    assert caught >= 880
    assert sum(looks_high_entropy(rnd(64, _B62 + "_-")) for _ in range(1000)) >= 950
    assert not any(looks_high_entropy(rnd(40, "0123456789abcdef")) for _ in range(1000))
    assert not any(looks_high_entropy(rnd(64, "0123456789ABCDEF")) for _ in range(1000))
    assert not any(looks_high_entropy(str(uuid.UUID(int=rng.getrandbits(128)))) for _ in range(500))
    assert not looks_high_entropy(_rand(31))  # below the length floor


@pytest.mark.parametrize(
    "text",
    [
        "x" * (1 << 20),
        "a." * (1 << 19),
        "a=b " * (1 << 18),
        "http:" * (1 << 18),
        "Ab1+" * (1 << 18),
        "token:" * (1 << 17),
    ],
    ids=["run", "dots", "kv", "scheme", "b64", "seps"],
)
def test_masking_is_linear_on_retained_payload_sizes(text):
    # Payloads are retained up to 1 MiB (#188); measured < 0.5 s each locally.
    started = time.perf_counter()
    mask_secrets(text)
    assert time.perf_counter() - started < 5.0


# --- Coverage: every exposure path goes through the same function ------------

SECRETS = [
    GH_CLASSIC,
    GH_PAT,
    SLACK_BOT,
    GOOGLE,
    JWT,
    PASSWORD,
    PEM_BODY.split("\n")[0],
    HIGH_ENTROPY,
    ENV_VALUE,
    AWS_ID,
]
TOOL_INPUT = {
    "command": f"git clone {URL_CREDS} && echo {GH_CLASSIC}",
    "env": ENV_LINE,
    "headers": {"X-Api-Key": PASSWORD, "Accept": f"text/plain; {SLACK_BOT}"},
}
TOOL_OUTPUT_TEXT = "\n".join(
    [f"gh: {GH_PAT}", f"maps: {GOOGLE}", f"jwt={JWT}", PEM, f"rand {HIGH_ENTROPY}", AWS_ID]
)


def _leaks(blob: str) -> list[str]:
    return [secret for secret in SECRETS if secret in blob]


def test_masking_entry_points_share_one_function():
    assert security.mask_secrets is masking.mask_secrets
    assert turn_events.mask_secrets is masking.mask_secrets
    from stiracore.observability import langfuse_exporter

    assert langfuse_exporter.mask_secrets is masking.mask_secrets


def test_each_turn_event_path_calls_mask_secrets(monkeypatch, tmp_path):
    calls: list[str] = []

    def spy(value):
        calls.append(type(value).__name__)
        return mask_secrets(value)

    monkeypatch.setattr(turn_events, "mask_secrets", spy)
    paths = {
        "make_preview": lambda: make_preview(TOOL_INPUT),
        "capture_payload": lambda: capture_payload("input", TOOL_INPUT),
        "sanitize_data": lambda: sanitize_data({"input_preview": TOOL_OUTPUT_TEXT}),
    }
    store = TurnEventStore(tmp_path / "t.db")
    paths["put_payload"] = lambda: store.put_payload("t", "u", "output", {"text": TOOL_OUTPUT_TEXT})
    paths["payload_page"] = lambda: store.payload_page("t", "u", "output")
    for name, run in paths.items():
        calls.clear()
        result = run()
        assert calls, f"{name} bypassed mask_secrets"
        assert not _leaks(json.dumps(result, default=str)), name


def _hook(**kwargs):
    return SimpleNamespace(agent=SimpleNamespace(), invocation_state={}, **kwargs)


def _run_tool(provider):
    use = {"name": "shell", "toolUseId": "u1", "input": TOOL_INPUT}
    before = _hook(tool_use=use)
    before.cancel_tool = False
    provider.on_before_tool_call(before)
    after = _hook(
        tool_use=use,
        result={"status": "success", "content": [{"text": TOOL_OUTPUT_TEXT}]},
        exception=None,
        retry=False,
        cancel_message=None,
    )
    after.invocation_state = before.invocation_state
    after.agent = before.agent
    provider.on_after_tool_call(after)


def test_no_secret_reaches_sse_replay_cards_or_turn_list(tmp_path):
    """Leak inspection with test values: chat SSE, replay API, tool-card pages, turn list."""
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    provider = create_dashboard_hooks(UIEventSink(recorder), payload_capture=capture_payload)

    def runner(message):
        _run_tool(provider)
        return "done"

    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(SimpleNamespace(), runner=runner, turn_recorder=recorder)
    )
    client = app.test_client()
    sse = client.post("/api/chat", json={"message": "run it"}).get_data(as_text=True)
    assert "tool_call.updated" in sse  # the tool events really went through SSE
    turn_id = client.get("/api/turns").get_json()["turns"][0]["turn_id"]
    bodies = {
        "sse": sse,
        "turns": client.get("/api/turns").get_data(as_text=True),
        "replay": client.get(f"/api/turns/{turn_id}/events").get_data(as_text=True),
    }
    for kind in ("input", "output"):
        page = client.get(f"/api/turns/{turn_id}/tools/u1/{kind}")
        assert page.status_code == 200
        bodies[kind] = page.get_data(as_text=True)
    assert MASK in bodies["output"] and "AKIA****" in bodies["output"]
    for name, body in bodies.items():
        # JSON escapes newlines in the PEM line; secrets are single-line values.
        assert not _leaks(body), name
        assert not _leaks(json.dumps(body)), name


def test_langfuse_export_masks_events():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from stiracore.observability.langfuse_exporter import LangfuseExporter

    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    exporter = LangfuseExporter("", "", "")
    exporter._tracer = provider.get_tracer("test")
    trace_id = exporter.trace_turn(
        "t1",
        [
            {"type": "tool", "input": TOOL_INPUT, "output": TOOL_OUTPUT_TEXT},
            {"type": "auth", "api_key": PASSWORD},
            f"note {GH_CLASSIC}",
        ],
    )
    assert len(trace_id) == 32
    (span,) = spans.get_finished_spans()
    exported = json.dumps([dict(event.attributes) for event in span.events])
    assert not _leaks(exported)
    assert [event.name for event in span.events] == ["tool", "auth", "event"]
    provider.shutdown()


def _hitl_app(broker):
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(SimpleNamespace(), runner=lambda m: "x", hitl_broker=broker)
    )
    return app.test_client()


def _ask(broker, tool_input, results, use_id="u1"):
    request = SimpleNamespace(
        tool_name="http_request",
        tool_input=tool_input,
        break_level=SimpleNamespace(value="critical"),
        reason="r",
        tool_use_id=use_id,
    )
    worker = threading.Thread(target=lambda: results.append(broker.request(request)), daemon=True)
    worker.start()
    for _ in range(200):
        if any(p["tool_use_id"] == use_id for p in broker.pending()):
            break
        time.sleep(0.01)
    return worker


SECRET_INPUT = {"url": URL_CREDS, "headers": {"Authorization": f"Bearer {HIGH_ENTROPY}"}}


def test_hitl_pending_does_not_expose_raw_tool_input():
    """#185: the queue shows masked arguments; the binding still names the real ones."""
    from stiracore.core import HITLAction, HITLDecision
    from stiracore.core.approval import args_hash

    broker = HITLApprovalBroker()
    results = []
    worker = _ask(broker, SECRET_INPUT, results)
    try:
        client = _hitl_app(broker)
        response = client.get("/api/hitl/pending")
        body = response.get_data(as_text=True)
        assert PASSWORD not in body and HIGH_ENTROPY not in body
        (item,) = response.json["requests"]
        assert item["editable"] is False and item["masked"] is True
        assert item["args_hash"] == args_hash(SECRET_INPUT)
        assert MASK in item["normalized_input"]
        assert json.loads(item["normalized_input"]) == item["tool_input"]
        # An edit of masked arguments would be blind: refused, the request stays.
        refused = client.post(
            f"/api/hitl/{item['id']}/decision",
            json={"action": "edit", "args_hash": item["args_hash"], "edited_input": {"u": 1}},
        )
        assert refused.status_code == 409 and refused.json["code"] == "unsupported"
        assert len(broker.pending()) == 1
        # Approving the displayed binding hands the worker the real arguments' decision.
        approved = client.post(
            f"/api/hitl/{item['id']}/decision",
            json={"action": "approve", "args_hash": item["args_hash"]},
        )
        assert approved.status_code == 202
        worker.join(2)
        assert results[0].action == HITLAction.APPROVE
        assert results[0].args_hash == args_hash(SECRET_INPUT)
        history = client.get("/api/hitl/history").get_data(as_text=True)
        assert PASSWORD not in history and HIGH_ENTROPY not in history
    finally:
        for pending in broker.pending():
            broker.decide(pending["id"], HITLDecision(HITLAction.REJECT))
        worker.join(2)


def _ask_with(broker, tool_input, reason, results, use_id="u1"):
    request = SimpleNamespace(
        tool_name="shell",
        tool_input=tool_input,
        break_level=SimpleNamespace(value="critical"),
        reason=reason,
        tool_use_id=use_id,
    )
    worker = threading.Thread(target=lambda: results.append(broker.request(request)), daemon=True)
    worker.start()
    for _ in range(200):
        if any(p["tool_use_id"] == use_id for p in broker.pending()):
            break
        time.sleep(0.01)
    return worker


def test_hitl_free_text_beside_the_arguments_is_masked_everywhere_it_is_shown():
    """#185 review, measured: cwd, the request reason and the operator's reason used to
    reach /api/hitl/history and the approval events raw (reason also the queue)."""
    events = []
    broker = HITLApprovalBroker(on_event=lambda kind, data: events.append((kind, data)))
    results = []
    worker = _ask_with(
        broker, {"command": "ls", "cwd": f"/tmp/{GH_CLASSIC}"}, f"why {GH_CLASSIC}", results
    )
    try:
        client = _hitl_app(broker)
        (item,) = client.get("/api/hitl/pending").json["requests"]
        assert not _leaks(json.dumps(item)) and item["editable"] is False
        rejected = client.post(
            f"/api/hitl/{item['id']}/decision",
            json={"action": "reject", "reason": f"no, {GH_CLASSIC}"},
        )
        assert rejected.status_code == 202
        worker.join(2)
        # The operator's own words still reach the agent unchanged: only displays mask.
        assert GH_CLASSIC in results[0].reason
        (entry,) = client.get("/api/hitl/history").json["history"]
        assert not _leaks(json.dumps(entry))
        assert entry["cwd"].startswith("/tmp/") and entry["resolution_reason"].startswith("no, ")
        assert events and not _leaks(json.dumps(events))
    finally:
        worker.join(2)


def test_hitl_display_masking_keeps_legitimate_values():
    """No over-masking: ids, hashes, a SHA-named cwd and the server's reason sentence
    are shown as they are, and the known reason still maps to its code."""
    from stiracore.core import HITLAction, HITLDecision, hitl_reasons

    broker = HITLApprovalBroker()
    results = []
    sha = hashlib.sha1(b"commit").hexdigest()
    cwd = f"/srv/stira/worktrees/{sha}"
    plain = {"command": "git status", "cwd": cwd}
    reason = hitl_reasons.CRITICAL_HALT
    worker = _ask_with(broker, plain, reason, results)
    try:
        client = _hitl_app(broker)
        (item,) = client.get("/api/hitl/pending").json["requests"]
        assert item["tool_input"] == plain and item["cwd"] == cwd and item["reason"] == reason
        assert "editable" not in item and MASK not in json.dumps(item)
        raw = broker.pending()[0]
        for key in ("id", "args_hash", "tool_use_id", "reason_code"):
            assert item[key] == raw[key], key
        client.post(f"/api/hitl/{item['id']}/decision", json={"action": "reject", "reason": "x"})
        worker.join(2)
        (entry,) = client.get("/api/hitl/history").json["history"]
        assert entry["cwd"] == cwd and MASK not in json.dumps(entry)
    finally:
        for pending in broker.pending():
            broker.decide(pending["id"], HITLDecision(HITLAction.REJECT))
        worker.join(2)


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_hitl_decision_applies_to_the_real_arguments_while_the_queue_is_being_masked(action):
    """Masking for display, concurrently with the decision, never touches what the
    worker gets: the real arguments' binding (approve) or a plain rejection."""
    from stiracore.core import HITLAction
    from stiracore.core.approval import args_hash

    broker = HITLApprovalBroker()
    results = []
    worker = _ask(broker, SECRET_INPUT, results)
    client = _hitl_app(broker)
    (item,) = client.get("/api/hitl/pending").json["requests"]
    stop = threading.Event()
    seen = []

    def display():
        local = _hitl_app(broker)
        while not stop.is_set():
            seen.append(local.get("/api/hitl/pending").json["requests"])

    readers = [threading.Thread(target=display, daemon=True) for _ in range(4)]
    for reader in readers:
        reader.start()
    try:
        time.sleep(0.05)
        body = {"action": action, "args_hash": item["args_hash"]}
        assert client.post(f"/api/hitl/{item['id']}/decision", json=body).status_code == 202
        worker.join(2)
    finally:
        stop.set()
        for reader in readers:
            reader.join(2)
    assert len(seen) > 4
    assert results[0].action == (HITLAction.APPROVE if action == "approve" else HITLAction.REJECT)
    if action == "approve":
        assert results[0].args_hash == args_hash(SECRET_INPUT)
    # The internal copy the server binds to stays the real input: every display was masked.
    for requests in seen:
        assert not _leaks(json.dumps(requests))


def test_hitl_pending_without_secrets_is_unchanged_and_editable():
    from stiracore.core import HITLAction, HITLDecision

    broker = HITLApprovalBroker()
    results = []
    plain = {"url": "https://example.test/a", "method": "GET"}
    worker = _ask(broker, plain, results)
    try:
        client = _hitl_app(broker)
        (item,) = client.get("/api/hitl/pending").json["requests"]
        assert item["tool_input"] == plain and "editable" not in item and "masked" not in item
        edited = client.post(
            f"/api/hitl/{item['id']}/decision",
            json={"action": "edit", "args_hash": item["args_hash"], "edited_input": {"u": 1}},
        )
        assert edited.status_code == 202
        worker.join(2)
        assert results[0].action == HITLAction.EDIT
    finally:
        for pending in broker.pending():
            broker.decide(pending["id"], HITLDecision(HITLAction.REJECT))
        worker.join(2)


# ADR 0012 phase 6: a URL's first query parameter was never examined — the key/value
# rule matched from the scheme (``https`` + ``:``) over the whole URL.
_SECRET_NAME = "api" + "_key"


@pytest.mark.parametrize(
    "text",
    [
        "https://x.example/c?" + _SECRET_NAME + "=Zx81kq0PLm",
        "https://x.example/c?a=1&" + _SECRET_NAME + "=Zx81kq0PLm",
        "https://x.example/c#" + _SECRET_NAME + "=Zx81kq0PLm",
        "url: https://x.example/?" + "to" + "ken=abcd1234",
    ],
)
def test_a_url_query_parameter_is_masked(text):
    masked = mask_secrets(text)
    assert "Zx81kq0PLm" not in masked and "abcd1234" not in masked
    assert masked.count("***") == 1 and mask_secrets(masked) == masked


@pytest.mark.parametrize(
    "text",
    [
        "https://x.example/?q=hello&page=2",
        "https://x.example/?max_tokens=5000&key=abc12345",
        "https://x.example/search?q=password+reset",
    ],
)
def test_ordinary_url_parameters_are_kept(text):
    assert mask_secrets(text) == text

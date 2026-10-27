"""A local stand-in for Bedrock's ConverseStream API (offline E2E, #242/#243).

The real ``BedrockModel`` (boto3) is pointed here with
``AWS_ENDPOINT_URL_BEDROCK_RUNTIME``, so a test drives the unmodified provider
code path and the real server process without a cloud call or a credential.

Script, taken from the last message of each request:

- user text ``CALL <tool> <json>`` -> one ``toolUse`` of ``<tool>`` with that input;
- user text ``CUT <tool> <raw>`` -> a ``toolUse`` whose arguments are the raw text as given
  (cut off mid-string) and ``stopReason: max_tokens``, with ``outputTokens`` equal to the
  request's ``inferenceConfig.maxTokens`` (#363);
- user text ``THINK <text>`` -> a reasoning block holding ``<text>`` and ``stopReason:
  max_tokens`` before any reply or tool call (adaptive thinking that used the whole cap,
  #416), ``outputTokens`` = the request's ``maxTokens`` as for ``CUT``;
- user text that *contains* ``CHILDCALL <tool> <json> ENDCALL`` (a child agent's prompt
  built by an upstream tool around the text it was given, #362) -> the same;
- a message holding ``toolResult`` blocks -> the text ``RESULT <status> <first text>``;
- user text ``SAY <text>`` -> that text as the reply (a scripted reply, e.g. Markdown with an
  ```svg block for the board, #453);
- user text ``SLOW <seconds> [<script>]`` -> after that many seconds (a long model call,
  #452) the text ``OK``, or the answer to ``<script>`` (e.g. ``CALL current_time {}``);
- anything else -> the text ``OK``.

``FakeBedrock.limits`` (model id -> output limit) makes the endpoint refuse a request whose
``inferenceConfig.maxTokens`` is above that model's limit with HTTP 400
``ValidationException``, as Bedrock does (#416).

Every request body is kept in ``requests`` (and appended to ``log_path`` as JSON lines
when given). Only ``POST /model/<id>/converse-stream`` is answered; anything else is
HTTP 400, so an unexpected API call fails visibly.

Manual use (``scripts/stira_ctl.sh`` against it, docs/research/headless-file-tools-e2e-2026-10-16.md):

    python tests/fake_bedrock_endpoint.py PORT [LOG.jsonl]
"""

from __future__ import annotations

import binascii
import json
import socketserver
import struct
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self


def _header(name: str, value: str) -> bytes:
    raw_name, raw_value = name.encode(), value.encode()
    return (
        struct.pack("B", len(raw_name))
        + raw_name
        + b"\x07"  # header value type 7: string
        + struct.pack(">H", len(raw_value))
        + raw_value
    )


def encode_event(kind: str, body: dict) -> bytes:
    """One ``application/vnd.amazon.eventstream`` message (prelude, headers, CRCs)."""
    headers = (
        _header(":event-type", kind)
        + _header(":content-type", "application/json")
        + _header(":message-type", "event")
    )
    payload = json.dumps(body).encode()
    prelude = struct.pack(">II", 12 + len(headers) + len(payload) + 4, len(headers))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + headers + payload
    return message + struct.pack(">I", binascii.crc32(message) & 0xFFFFFFFF)


def _text_events(text: str) -> list[bytes]:
    return [
        encode_event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": text}}),
        encode_event("contentBlockStop", {"contentBlockIndex": 0}),
        encode_event("messageStop", {"stopReason": "end_turn"}),
    ]


def respond(body: dict, counter: list[int]) -> bytes:
    """The scripted ConverseStream answer to one request body."""
    last = (body.get("messages") or [{}])[-1]
    blocks = last.get("content") or []
    events = [encode_event("messageStart", {"role": "assistant"})]
    cut = False
    results = [block["toolResult"] for block in blocks if "toolResult" in block]
    text = " ".join(block.get("text", "") for block in blocks if "text" in block).strip()
    if results:
        first = results[0]
        shown = next((c.get("text", "") for c in first.get("content", []) if "text" in c), "")
        events += _text_events(f"RESULT {first.get('status')} {shown}")
    elif text.startswith("SAY "):
        events += _text_events(text[4:])
    elif text.startswith("SLOW "):
        _, seconds, *rest = text.split(" ", 2)
        time.sleep(min(float(seconds), 120.0))
        if not rest:
            events += _text_events("OK")
        else:  # ``SLOW <seconds> <script>``: the rest is answered after the wait
            return respond({**body, "messages": [{"content": [{"text": rest[0]}]}]}, counter)
    elif text.startswith("CUT "):
        _, tool, raw = text.split(" ", 2)
        cut = True
        counter[0] += 1
        start = {"toolUse": {"toolUseId": f"tooluse_fake_{counter[0]}", "name": tool}}
        events += [
            encode_event("contentBlockStart", {"contentBlockIndex": 0, "start": start}),
            encode_event(
                "contentBlockDelta",
                {"contentBlockIndex": 0, "delta": {"toolUse": {"input": raw}}},
            ),
            encode_event("contentBlockStop", {"contentBlockIndex": 0}),
            encode_event("messageStop", {"stopReason": "max_tokens"}),
        ]
    elif text.startswith("THINK "):
        cut = True
        reasoning = {"reasoningContent": {"text": text[6:]}}
        signature = {"reasoningContent": {"signature": "fake-signature"}}
        events += [
            encode_event("contentBlockDelta", {"contentBlockIndex": 0, "delta": reasoning}),
            encode_event("contentBlockDelta", {"contentBlockIndex": 0, "delta": signature}),
            encode_event("contentBlockStop", {"contentBlockIndex": 0}),
            encode_event("messageStop", {"stopReason": "max_tokens"}),
        ]
    elif text.startswith("CALL ") or ("CHILDCALL " in text and " ENDCALL" in text):
        if text.startswith("CALL "):
            _, tool, raw = text.split(" ", 2)
        else:
            inner = text.split("CHILDCALL ", 1)[1].split(" ENDCALL", 1)[0]
            _, tool, raw = ("CHILDCALL " + inner).split(" ", 2)
        # With prompt caching the turn's volatile block follows the user's text in the same
        # message (#254): keep only the JSON value the script wrote (#463).
        try:
            raw = raw[: json.JSONDecoder().raw_decode(raw)[1]]
        except ValueError:
            pass
        counter[0] += 1
        start = {"toolUse": {"toolUseId": f"tooluse_fake_{counter[0]}", "name": tool}}
        events += [
            encode_event("contentBlockStart", {"contentBlockIndex": 0, "start": start}),
            encode_event(
                "contentBlockDelta",
                {"contentBlockIndex": 0, "delta": {"toolUse": {"input": raw}}},
            ),
            encode_event("contentBlockStop", {"contentBlockIndex": 0}),
            encode_event("messageStop", {"stopReason": "tool_use"}),
        ]
    else:
        events += _text_events("OK")
    usage = {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}
    if cut:
        cap = (body.get("inferenceConfig") or {}).get("maxTokens") or 0
        usage = {"inputTokens": 10, "outputTokens": cap, "totalTokens": 10 + cap}
    events.append(encode_event("metadata", {"usage": usage, "metrics": {"latencyMs": 1}}))
    return b"".join(events)


class FakeBedrock:
    """``with FakeBedrock() as fake: fake.url`` — a threaded local endpoint."""

    def __init__(self, port: int = 0, log_path: str | Path | None = None) -> None:
        self.requests: list[dict] = []
        self.limits: dict[str, int] = {}
        self.log_path = Path(log_path) if log_path else None
        counter = [0]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # quiet
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append({"path": self.path, "body": body})
                if owner.log_path is not None:
                    with owner.log_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"path": self.path, "body": body}) + "\n")
                if not self.path.endswith("/converse-stream"):
                    self.send_response(400)
                    self.end_headers()
                    return
                model_id = urllib.parse.unquote(self.path.split("/model/", 1)[-1])
                model_id = model_id.rsplit("/converse-stream", 1)[0]
                limit = owner.limits.get(model_id)
                asked = (body.get("inferenceConfig") or {}).get("maxTokens")
                if limit is not None and isinstance(asked, int) and asked > limit:
                    refusal = json.dumps(
                        {"message": f"maxTokens {asked} exceeds this model's limit of {limit}"}
                    ).encode()
                    self.send_response(400)
                    self.send_header("x-amzn-ErrorType", "ValidationException")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(refusal)))
                    self.end_headers()
                    self.wfile.write(refusal)
                    return
                data = respond(body, counter)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.amazon.eventstream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        class Server(ThreadingHTTPServer):
            def server_bind(self):
                # No socket.getfqdn() (#662): the reverse lookup for 127.0.0.1 can take
                # tens of seconds on a CI macOS runner, and this process is started with a
                # timeout by the callers that wait for the URL line below.
                socketserver.TCPServer.server_bind(self)
                self.server_name, self.server_port = self.server_address[:2]

        self.server = Server(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def tool_results(self) -> list[dict]:
        """Every ``toolResult`` block the server process sent back to the model."""
        found = []
        for request in self.requests:
            last = (request["body"].get("messages") or [{}])[-1]
            found += [b["toolResult"] for b in last.get("content", []) if "toolResult" in b]
        return found

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


def offline_env(url: str) -> dict[str, str]:
    """Environment that sends every AWS call to ``url`` with dummy, unusable credentials."""
    return {
        "AWS_ENDPOINT_URL": url,
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": url,
        "AWS_ACCESS_KEY_ID": "offline-test-key",
        "AWS_SECRET_ACCESS_KEY": "offline-fake-secret",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
    }


def strip_cloud_env(env: dict[str, str]) -> dict[str, str]:
    """``env`` without any real AWS credential/profile or provider selection."""
    drop = ("AWS_", "STIRA_PROVIDER", "STIRA_BASE_URL", "STIRA_API_KEY_ENV", "STIRA_MODEL")
    return {k: v for k, v in env.items() if not k.startswith(drop)}


if __name__ == "__main__":
    fake = FakeBedrock(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"fake bedrock endpoint on {fake.url}", flush=True)
    fake.server.serve_forever()

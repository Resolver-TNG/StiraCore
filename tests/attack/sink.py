"""A record-only HTTP sink on a free loopback port: the "outside world" of the attacks.

Every request is recorded (method, path, body) and answered ``200 SINK``; nothing is
forwarded. The port is chosen by the OS (never 5555). Stop it with ``close()``.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Sink:
    def __init__(self) -> None:
        self.hits: list[tuple[str, str, str]] = []
        self.body = "SINK-OK"  # what every request is answered (a planted page)
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def _record(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                sink.hits.append((self.command, self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(sink.body.encode("utf-8"))

            do_GET = do_POST = do_PUT = _record

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        assert self.port != 5555
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

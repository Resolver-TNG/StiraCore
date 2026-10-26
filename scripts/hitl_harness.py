"""Approval UI verification harness (#189): the chat server with fake approval requests.

Starts the real chat server app (temporary data dir, no model call is ever made) and
injects HITL requests into its live broker, so the chat cards, the nav chip and
/approvals can be checked in a browser without a model or credentials:

    STIRA_PORT=5612 python scripts/hitl_harness.py

Injected: three pending requests (one with a cwd, one re-review of an edit, one
whose arguments hold a fake credential, shown masked and not editable, #185) and a
history of approved / rejected (with reason) / expired decisions. Nothing executes:
the waiting threads only receive the decision.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from dashboard import stira_server
from stiracore.core import BreakLevel, HITLAction, HITLDecision, HITLRequest
from stiracore.core.approval import args_hash

REASON = "CRITICAL HALT requires human approval for this write operation."


def _request(tool_input, *, tool="memory_write", ttl=600.0, supersedes=None, use_id="demo"):
    return HITLRequest(
        tool_name=tool,
        tool_input=tool_input,
        break_level=BreakLevel.CRITICAL,
        reason=REASON,
        tool_use_id=use_id,
        agent_id="stira",
        expires_at=time.time() + ttl,
        supersedes=supersedes,
    )


def _wait_for(broker, tool_use_id):
    for _ in range(200):
        for item in broker.pending():
            if item["tool_use_id"] == tool_use_id:
                return item
        time.sleep(0.01)
    raise RuntimeError(f"request {tool_use_id} did not appear")


def inject(broker) -> None:
    broker._turn_id_provider = lambda: "harness-turn-0001"  # harness only

    def ask(request):
        threading.Thread(target=broker.request, args=(request,), daemon=True).start()

    # History: approved, rejected with a reason, expired (nobody answered).
    ask(_request({"layer": "episodic", "content": "morning log"}, use_id="h-approved"))
    item = _wait_for(broker, "h-approved")
    broker.decide(item["id"], HITLDecision(HITLAction.APPROVE), args_hash=item["args_hash"])
    ask(_request({"layer": "semantic", "content": "promote rule"}, use_id="h-rejected"))
    item = _wait_for(broker, "h-rejected")
    broker.decide(item["id"], HITLDecision(HITLAction.REJECT, reason="対象の層が違う"))
    ask(_request({"layer": "episodic", "content": "late"}, ttl=0.3, use_id="h-expired"))
    time.sleep(0.8)

    # Pending: a request with a cwd, and the re-review of an edited request.
    ask(
        _request(
            {"path": "notes/today.md", "content": "hello", "cwd": "/srv/stira"},
            tool="file_write",
            use_id="p-cwd",
        )
    )
    original = {"layer": "episodic", "content": "draft"}
    ask(_request(original, use_id="p-edit"))
    first = _wait_for(broker, "p-edit")
    broker.decide(
        first["id"],
        HITLDecision(HITLAction.EDIT, edited_input={"layer": "episodic", "content": "edited"}),
        args_hash=args_hash(original),
    )
    ask(
        _request(
            {"layer": "episodic", "content": "edited"},
            supersedes=first["id"],
            use_id="p-edit-2",
        )
    )
    _wait_for(broker, "p-edit-2")
    # #185: a request whose arguments hold a credential-shaped value (fake, built at
    # run time): the queue shows it masked and offers no edit.
    fake = "harness-" + "0" * 24
    ask(
        _request(
            {"url": "https://example.invalid/api", "headers": {"Authorization": f"Bearer {fake}"}},
            tool="http_request",
            use_id="p-masked",
        )
    )
    _wait_for(broker, "p-masked")


def main() -> None:
    stira_server.DATA_DIR = Path(tempfile.mkdtemp(prefix="stira-hitl-harness-"))
    app = stira_server.create_app()
    inject(app.extensions["hitl_broker"])
    port = int(os.environ.get("STIRA_PORT", "5612"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

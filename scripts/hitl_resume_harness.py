"""Durable approval + resume verification harness (#240): the chat server with a scripted model.

No model is ever called. Run the two phases against the same data directory:

    STIRA_PORT=5623 python scripts/hitl_resume_harness.py pause /tmp/stira-240
        # a chat turn pauses on a CRITICAL memory_write approval; stop the process
        # (Ctrl-C / kill) while the request is pending = an unplanned restart
    STIRA_PORT=5623 python scripts/hitl_resume_harness.py serve /tmp/stira-240
        # the restarted server lists the request as restored on /approvals;
        # approving it there resumes the paused turn and runs memory_write once

The scripted model asks for exactly one memory_write in the first phase and only
answers with text afterwards. Stability is pinned low so the HITL gate is active.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from strands.models import Model

from dashboard import stira_server

CALL = {
    "toolUseId": "harness-write-1",
    "name": "memory_write",
    "input": {"layer": "episodic", "key": "resume-check", "content": "approved after restart"},
}


class ScriptedModel(Model):
    """Streams scripted assistant messages; never touches the network."""

    def __init__(self, responses):
        self.responses = list(responses)

    def get_config(self):
        return {"model_id": "harness-offline"}

    def update_config(self, **kwargs):
        pass

    async def structured_output(self, *args, **kwargs):
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        response = self.responses.pop(0) if self.responses else [{"text": "(no script)"}]
        yield {"messageStart": {"role": "assistant"}}
        reason = "end_turn"
        for block in response:
            if "toolUse" in block:
                reason = "tool_use"
                use = block["toolUse"]
                start = {"toolUse": {"name": use["name"], "toolUseId": use["toolUseId"]}}
                yield {"contentBlockStart": {"start": start}}
                delta = {"toolUse": {"input": json.dumps(use["input"])}}
                yield {"contentBlockDelta": {"delta": delta}}
                yield {"contentBlockStop": {}}
            else:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": block}}
                yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": reason}}


def build_app(data_dir: Path, responses):
    created = {}

    class HarnessAgent(stira_server.NXPAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.model = ScriptedModel(responses)
            self._stimulus_classifier = SimpleNamespace(classify=lambda **_kwargs: None)
            self._classifier_resolved = True
            self.state.stability = 0.1  # CRITICAL + HALT: writes need a human approval
            created["agent"] = self

    stira_server.DATA_DIR = data_dir
    stira_server.NXPAgent = HarnessAgent
    stira_server.apply_model_selection = lambda *_args, **_kwargs: False
    app = stira_server.create_app()
    return app, created["agent"]


def start_paused_turn(app) -> None:
    client = app.test_client()
    headers = {
        "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"],
        "Origin": "http://localhost",
        "Idempotency-Key": "harness-turn-1",
    }

    def run():
        response = client.post(
            "/api/chat", json={"message": "記録して"}, headers=headers, buffered=False
        )
        for _chunk in response.response:
            pass

    threading.Thread(target=run, daemon=True).start()
    broker = app.extensions["hitl_broker"]
    for _ in range(500):
        if broker.pending():
            return
        time.sleep(0.02)
    raise RuntimeError("the turn did not pause on an approval")


def main() -> None:
    phase, data_dir = sys.argv[1], Path(sys.argv[2]).resolve()
    if phase == "pause":
        app, _agent = build_app(data_dir, [[{"toolUse": CALL}], [{"text": "止まりました"}]])
        start_paused_turn(app)
    elif phase == "serve":
        app, _agent = build_app(data_dir, [[{"text": "再開して記録しました"}]])
    else:
        raise SystemExit("usage: hitl_resume_harness.py pause|serve DATA_DIR")
    port = int(os.environ.get("STIRA_PORT", "5623"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()

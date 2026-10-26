"""Offline chat server process for the data-directory owner tests (#285). No model is called.

    python tests/stira_owner_harness.py pause DATA_DIR   # a turn waits on a file_write approval
    python tests/stira_owner_harness.py serve DATA_DIR   # serve only (scripted text replies)
    python tests/stira_owner_harness.py slow-start DATA_DIR
        # start-up stalls after heartbeat, scheduler and approval store exist (SIGTERM probe)

Both phases go through ``stira_server.main`` (the real entry point: owner lock,
SIGTERM handling, bounded stops); only the model and the classifier are scripted.
The configs live in DATA_DIR; ``file_write`` is enabled with an Ask rule, so the
paused request is an ordinary approval at NORMAL, as in the #285 report.
"""

from __future__ import annotations

import json
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
from stiracore.observability.config_api import ConfigStore
from stiracore.tools.tool_config import ToolConfig


class ScriptedModel(Model):
    def __init__(self, responses):
        self.responses = list(responses)

    def get_config(self):
        return {"model_id": "owner-harness-offline"}

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


def factory(data_dir: Path, responses):
    def build(lifecycle):
        class HarnessAgent(stira_server.NXPAgent):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.model = ScriptedModel(responses)
                self._stimulus_classifier = SimpleNamespace(classify=lambda **_kwargs: None)
                self._classifier_resolved = True

        stira_server.DATA_DIR = data_dir
        stira_server.NXPAgent = HarnessAgent
        stira_server.apply_model_selection = lambda *_args, **_kwargs: False
        store = ConfigStore(
            data_dir / "gateway-config.json",
            data_dir / "agent-config.json",
            data_dir / "tool-config.json",
        )
        if not Path(store.tool_path).exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            ToolConfig(
                mode="custom",
                enabled_tools={"file_write"},
                ask_tools={"file_write"},
                path=store.tool_path,
            ).save()
        return stira_server.create_app(store, lifecycle=lifecycle)

    return build


def pause_after_start(app_holder: dict, target: Path) -> None:
    """Send one chat turn that waits on the file_write approval."""
    deadline = time.monotonic() + 30
    while "app" not in app_holder and time.monotonic() < deadline:
        time.sleep(0.02)
    app = app_holder["app"]
    client = app.test_client()
    headers = {
        "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"],
        "Origin": "http://localhost",
        "Idempotency-Key": "owner-harness-turn-1",
    }

    def run():
        response = client.post("/api/chat", json={"message": "write"}, headers=headers)
        for _chunk in response.response:
            pass

    threading.Thread(target=run, daemon=True).start()
    broker = app.extensions["hitl_broker"]
    while not broker.pending() and time.monotonic() < deadline:
        time.sleep(0.02)
    print(f"HARNESS paused={len(broker.pending())} target={target.name}", flush=True)


def main() -> None:
    phase, data_dir = sys.argv[1], Path(sys.argv[2]).resolve()
    target = data_dir / "workspace" / "owner-check.txt"
    call = {
        "toolUseId": "owner-write-1",
        "name": "file_write",
        "input": {"path": str(target), "content": "written once"},
    }
    if phase == "pause":
        responses = [[{"toolUse": call}], [{"text": "stopped"}]]
    elif phase == "serve":
        responses = [[{"text": "resumed and written"}]]
    elif phase == "slow-start":
        responses = []
        real_blueprint = stira_server.create_scheduler_blueprint

        def stalled_blueprint(*args, **kwargs):
            # Called after the heartbeat and scheduler started and the stores opened.
            print("HARNESS starting (stalled)", flush=True)
            time.sleep(60)
            return real_blueprint(*args, **kwargs)

        stira_server.create_scheduler_blueprint = stalled_blueprint
    else:
        raise SystemExit("usage: stira_owner_harness.py pause|serve|slow-start DATA_DIR")
    holder: dict = {}
    build = factory(data_dir, responses)

    def build_and_hold(lifecycle):
        holder["app"] = build(lifecycle)
        if phase == "pause":
            threading.Thread(target=pause_after_start, args=(holder, target), daemon=True).start()
        return holder["app"]

    stira_server.main(app_factory=build_and_hold)


if __name__ == "__main__":
    main()

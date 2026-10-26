"""Offline chat server for trying the YOLO button by hand (ADR 0012 phases 5b, 6). No model is called.

    STIRA_YOLO_ALLOW=internal STIRA_STAGE=1 STIRA_PORT=<port> \\
        python tests/stira_yolo_harness.py DATA_DIR

Every chat message makes the scripted model ask for one ``file_write`` inside the
workspace (an Ask tool on a fresh install), then answer with a short text. Without a
YOLO window that write is an approval card; inside one it is approved automatically
and the page shows the log line instead. A message that contains ``search`` asks for one
``web_search`` instead (an external tool, phase 6: approved automatically only in an
"External allowed" window; without ``STIRA_WEB_SEARCH_ENDPOINT`` the tool is not offered,
#583, and the call answers "tool not found" without sending anything). Phase 7: with ``STIRA_ENABLE_EXEC_TOOLS=1`` (and ``STIRA_YOLO_ALLOW=external``)
a message that contains ``shell`` asks for one ``shell`` call (``printf`` into
``<DATA_DIR>-sandbox``); on a fresh data directory the harness makes ``shell`` and ``batch``
Ask (exec tools are Never on a fresh install) so the "Include exec" window has something
to answer. Everything else is the real entry point
(``stira_server.main``: owner lock, the YOLO allowance, the own-dashboard guard).
"""

from __future__ import annotations

import json
import sys
from itertools import count
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from strands.models import Model

from dashboard import stira_server
from stiracore.observability.config_api import ConfigStore


class WriteOnceModel(Model):
    """One ``file_write`` per user message, then a text reply."""

    def __init__(self, workspace, sandbox=None):
        self.workspace = workspace
        self.sandbox = sandbox
        self.ids = count(1)

    def get_config(self):
        return {"model_id": "yolo-harness-offline"}

    def update_config(self, **kwargs):
        pass

    async def structured_output(self, *args, **kwargs):
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        last = messages[-1] if messages else {}
        answered = any("toolResult" in block for block in last.get("content", []))
        yield {"messageStart": {"role": "assistant"}}
        if answered:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "Done (offline harness)."}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
            return
        n = next(self.ids)
        text = " ".join(block.get("text", "") for block in last.get("content", []))
        if "shell" in text and self.sandbox is not None:
            use = {"name": "shell", "toolUseId": f"harness-shell-{n}"}
            arguments = {"command": f"printf ok >> yolo-harness-{n}.txt", "work_dir": self.sandbox}
        elif "search" in text:
            use = {"name": "web_search", "toolUseId": f"harness-search-{n}"}
            arguments = {"query": f"harness query {n}"}
        else:
            use = {"name": "file_write", "toolUseId": f"harness-write-{n}"}
            target = Path(self.workspace()) / f"yolo-harness-{n}.md"
            arguments = {"path": str(target), "content": f"note {n}\n"}
        yield {"contentBlockStart": {"start": {"toolUse": use}}}
        delta = {"toolUse": {"input": json.dumps(arguments)}}
        yield {"contentBlockDelta": {"delta": delta}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def main() -> None:
    data_dir = Path(sys.argv[1]).resolve()
    fresh = not data_dir.exists()
    data_dir.mkdir(parents=True, exist_ok=True)
    sandbox = data_dir.parent / f"{data_dir.name}-sandbox"
    sandbox.mkdir(exist_ok=True)

    class HarnessAgent(stira_server.NXPAgent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.model = WriteOnceModel(lambda: self.write_policy.workspace, str(sandbox))
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
    from stiracore.tools import tool_registry

    if fresh and tool_registry.exec_tools_enabled():
        from stiracore.tools.tool_config import ToolConfig

        current = store.load_tools()
        ToolConfig(
            mode="custom",
            enabled_tools=set(current.enabled_tools) | {"shell", "batch"},
            ask_tools=set(current.ask_tools) | {"shell", "batch", "file_write", "web_search"},
            path=store.tool_path,
        ).save()
    stira_server.main(app_factory=lambda lc: stira_server.create_app(store, lifecycle=lc))


if __name__ == "__main__":
    main()

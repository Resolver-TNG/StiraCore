"""Fingerprint of a chat server as the model and the UI see it (one process per run).

``python tests/yolo_fingerprint.py <out.json> <base dir>`` builds the real chat server app
offline (no model call) in a fresh process — so ``STIRA_YOLO_ALLOW`` is read exactly as
at a real start — and writes: the tool-spec hash and order handed to the agent, the
system prompt (wall-clock stamps masked), the extra hook types, the route inventory,
the existing read APIs, and what two save routes answer to a body naming ``yolo``.
Used by ``tests/test_yolo_default_unchanged.py``; it imports nothing YOLO-specific
unless the module exists, so it also runs on a checkout without YOLO (main).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
READS = (
    "/api/gateway/status",
    "/api/config",
    "/api/config/tools",
    "/api/tools/config",
    "/api/tools/list",
    "/api/config/hub",
    "/api/hitl/pending",
    "/api/scheduler",
    "/api/config/history",
)
_VOLATILE = frozenset({"server_time"})
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?")
_EPOCH = re.compile(r"\b1[0-9]{9}\.[0-9]+")


def _strip(value):
    if isinstance(value, dict):
        return {
            k: "<checkout path>"
            if k == "path" and isinstance(v, str) and v.endswith(".md")
            else _strip(v)
            for k, v in value.items()
            if k not in _VOLATILE
        }
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _mask(text: str, base: Path) -> str:
    text = text.replace(str(base.resolve()), "<base>").replace(str(base), "<base>")
    return _EPOCH.sub("<t>", _STAMP.sub("<ts>", text))


def main(out: str, base_dir: str) -> None:
    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    os.chdir(base)
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.nxp_agent import NXPAgent
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.observability.config_api import ConfigStore

    captured = {}

    def capture(*args, **kwargs):
        agent = NXPAgent(*args, **kwargs)
        captured.update(agent=agent, kwargs=kwargs)
        return agent

    classifier = Mock(
        model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
    )
    with (
        patch.object(stira_server, "NXPAgent", capture),
        patch.object(stira_server, "DATA_DIR", base / "data"),
        patch.object(nxp_agent, "BedrockModel", Mock(return_value=Mock())),
        patch.object(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called"))),
        patch.object(stimulus_classifier, "get_classifier", lambda: classifier),
    ):
        store = ConfigStore(base / "gw.json", base / "agent.json", base / "tools.json")
        app = stira_server.create_app(config_store=store)
        client = app.test_client()
        token = app.config["STIRA_CSRF_TOKEN"]
        tools = captured["kwargs"]["tools"]
        specs = [
            getattr(t, "tool_spec", None) or {"name": getattr(t, "tool_name", "")} for t in tools
        ]
        result = {
            "tool_spec_sha256": hashlib.sha256(
                json.dumps(specs, sort_keys=True, default=str).encode()
            ).hexdigest(),
            "tool_names": [getattr(t, "tool_name", None) for t in tools],
            "prompt": _mask(captured["agent"].system_prompt, base),
            "extra_hooks": [type(h).__name__ for h in captured["kwargs"]["extra_hooks"]],
            # Phase 5a: the YOLO approver at the gate (None without the allowance).
            "auto_approver": type(
                getattr(captured["agent"].checkpoint_hooks, "auto_approver", None)
            ).__name__,
            "routes": sorted(
                f"{rule.rule} {','.join(sorted(rule.methods - {'HEAD', 'OPTIONS'}))}"
                for rule in app.url_map.iter_rules()
            ),
            "reads": {},
            "save_with_yolo_key": {},
        }
        for path in READS:
            response = client.get(path)
            body = json.dumps(_strip(response.get_json()), sort_keys=True, ensure_ascii=False)
            result["reads"][path] = [response.status_code, _mask(body, base)]
        probes = {
            "/api/config/tools": {"name": "file_read", "enabled": True, "yolo": "external"},
            "/api/config/hub": {"changes": {"yolo": {"tier": "external"}}},
        }
        for path, body in probes.items():
            response = client.post(path, json=body, headers={"X-CSRF-Token": token})
            data = response.get_json() or {}
            result["save_with_yolo_key"][path] = [response.status_code, data.get("error_code")]
        try:
            from stiracore.yolo import runtime
        except ImportError:
            result["loosening_while_tool_runs"] = None
        else:
            token_object = object()
            runtime.TRACKER.enter(token_object)
            try:
                response = client.post(
                    "/api/config/tools",
                    json={"name": "http_request", "policy": "ask", "confirm": True},
                    headers={"X-CSRF-Token": token},
                )
                data = response.get_json() or {}
                result["loosening_while_tool_runs"] = [response.status_code, data.get("error_code")]
            finally:
                runtime.TRACKER.leave(token_object)
        before = client.get("/api/tools/config").get_json()
        response = client.post(
            "/api/config/tools",
            json={"name": "http_request", "policy": "ask", "confirm": True},
            headers={"X-CSRF-Token": token},
        )
        data = response.get_json() or {}
        result["loosening_after_tool_call"] = [response.status_code, data.get("error_code")]
        result["rules_before"] = json.dumps(before, sort_keys=True)
        result["rules_after"] = json.dumps(
            client.get("/api/tools/config").get_json(), sort_keys=True
        )
        app.extensions["lifecycle"].shutdown()
    Path(out).write_text(json.dumps(result, sort_keys=True, ensure_ascii=False), "utf-8")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])

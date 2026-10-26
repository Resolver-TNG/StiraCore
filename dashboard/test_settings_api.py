"""Settings dashboard Config API tests. No live state changes: temp files only."""

import tempfile
import unittest
from pathlib import Path

from app import create_app

from stiracore.agent.model_factory import DEFAULT_MODELS
from stiracore.config import AgentConfig, GatewayConfig
from stiracore.tools.tool_config import ToolConfig


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state_path = root / "state.json"
        self.gateway_path = root / "gateway-config.json"
        self.agent_path = root / "agent-config.json"
        self.tool_path = root / "tool-config.json"

        from stiracore.core import NeuralState

        NeuralState(stability=0.9).save(self.state_path)

        self.app = create_app(
            state_path=self.state_path,
            gateway_config_path=self.gateway_path,
            agent_config_path=self.agent_path,
            tool_config_path=self.tool_path,
        )
        self.client = self.app.test_client()

    # --- GET /api/config -------------------------------------------------

    def test_get_config_returns_resolved_shape(self):
        response = self.client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        data = response.json
        for key in (
            "model_id",
            "region",
            "port",
            "jev_enabled",
            "identity_anchors",
            "tool_config",
            "stira_config",
        ):
            self.assertIn(key, data)
        self.assertEqual(data["model_id"], GatewayConfig().model_id)
        self.assertIn("stability_warning", data["stira_config"])
        self.assertIn("enabled_tools", data["tool_config"])

    def test_settings_page_renders(self):
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"settings.js", response.data)

    # --- Models ------------------------------------------------------------

    def test_get_models_lists_catalog_and_current(self):
        response = self.client.get("/api/config/models")
        self.assertEqual(response.status_code, 200)
        data = response.json
        ids = {model["id"] for model in data["models"]}
        self.assertTrue(set(DEFAULT_MODELS.values()).issubset(ids))
        self.assertIsNone(data["agent_override"])
        self.assertEqual(data["effective_model_id"], data["gateway_model_id"])

    def test_post_models_switches_and_persists(self):
        target = next(iter(DEFAULT_MODELS.values()))
        response = self.client.post(
            "/api/config/models",
            json={"model_id": target},
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["agent_override"], target)
        self.assertEqual(response.json["effective_model_id"], target)

        # Persisted: a fresh AgentConfig load reflects the switch.
        reloaded = AgentConfig.load(self.agent_path, tool_config_path=self.tool_path)
        self.assertEqual(reloaded.model_override, target)

        # And a subsequent GET reflects it too.
        follow_up = self.client.get("/api/config/models")
        self.assertEqual(follow_up.json["effective_model_id"], target)

    def test_post_models_can_clear_override(self):
        target = next(iter(DEFAULT_MODELS.values()))
        self.client.post("/api/config/models", json={"model_id": target})
        response = self.client.post("/api/config/models", json={"model_id": None})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIsNone(response.json["agent_override"])

    def test_post_models_rejects_unknown_model_id(self):
        response = self.client.post(
            "/api/config/models",
            json={"model_id": "definitely-not-a-real-model"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json)

    def test_post_models_rejects_malformed_body(self):
        for bad in (None, [], {}, {"model_id": ""}, {"model_id": 5}):
            response = self.client.post("/api/config/models", json=bad)
            self.assertEqual(response.status_code, 400, bad)

    # --- Jev classifier ------------------------------------------------------

    def test_get_jev_lists_classifiers_and_current(self):
        response = self.client.get("/api/config/jev")
        self.assertEqual(response.status_code, 200)
        data = response.json
        ids = {item["id"] for item in data["classifiers"]}
        self.assertEqual(ids, {"auto", "rule", "sokudan"})
        # jev_enabled false is "auto", as the runtime reads it (#162).
        self.assertEqual(data["current"], "auto")
        self.assertFalse(data["jev_enabled"])

    def test_post_jev_switches_classifier_and_persists(self):
        response = self.client.post("/api/config/jev", json={"classifier": "sokudan"})
        self.assertEqual(response.status_code, 200, response.json)

        reloaded = GatewayConfig.load(self.gateway_path)
        self.assertEqual(reloaded.jev_classifier, "sokudan")
        # The posted classifier is the choice the runtime uses (#162): it no longer
        # stays behind a jev_enabled switch that was never toggled.
        self.assertTrue(reloaded.jev_enabled)

        follow_up = self.client.get("/api/config/jev")
        self.assertEqual(follow_up.json["current"], "sokudan")

        disable = self.client.post("/api/config/jev", json={"jev_enabled": False})
        self.assertEqual(disable.status_code, 200, disable.json)
        self.assertEqual(disable.json["current"], "auto")
        enable = self.client.post("/api/config/jev", json={"jev_enabled": True})
        self.assertEqual(enable.status_code, 200, enable.json)
        self.assertEqual(enable.json["current"], "sokudan")
        self.assertTrue(enable.json["jev_enabled"])

    def test_post_jev_rejects_unknown_classifier(self):
        response = self.client.post("/api/config/jev", json={"classifier": "llm:gpt"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json)

    def test_post_jev_rejects_non_boolean_enabled(self):
        response = self.client.post("/api/config/jev", json={"jev_enabled": "yes"})
        self.assertEqual(response.status_code, 400)

    def test_post_jev_rejects_empty_body(self):
        response = self.client.post("/api/config/jev", json={})
        self.assertEqual(response.status_code, 400)

    # --- Tool management -------------------------------------------------

    def test_get_tools_lists_known_read_and_write_tools(self):
        response = self.client.get("/api/config/tools")
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertGreater(len(data["tools"]), 0)
        for tool in data["tools"]:
            self.assertIn(tool["category"], ("read", "write", "exec"))
            self.assertIn("configured", tool)
            self.assertIn("enabled", tool)
            self.assertIn("blocked_by_breaker", tool)

    def test_post_tools_toggle_persists(self):
        listing = self.client.get("/api/config/tools").json
        write_tool = next(t["name"] for t in listing["tools"] if t["category"] == "write")

        off = self.client.post("/api/config/tools", json={"name": write_tool, "enabled": False})
        self.assertEqual(off.status_code, 200, off.json)
        self.assertNotIn(write_tool, off.json["enabled_tools"])

        reloaded = ToolConfig.load(self.tool_path)
        self.assertNotIn(write_tool, reloaded.enabled_tools)

        # Turning a tool back on loosens its rule: it needs a confirmation (#190).
        refused = self.client.post("/api/config/tools", json={"name": write_tool, "enabled": True})
        self.assertEqual(refused.status_code, 400, refused.json)
        on = self.client.post(
            "/api/config/tools", json={"name": write_tool, "enabled": True, "confirm": True}
        )
        self.assertEqual(on.status_code, 200, on.json)
        self.assertIn(write_tool, on.json["enabled_tools"])

    def test_post_tools_rejects_unknown_tool_name(self):
        response = self.client.post(
            "/api/config/tools", json={"name": "not_a_real_tool", "enabled": False}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json)

    def test_post_tools_rejects_unknown_bulk_names(self):
        response = self.client.post(
            "/api/config/tools", json={"enabled_tools": ["not_a_real_tool"]}
        )
        self.assertEqual(response.status_code, 400)

    def test_post_tools_rejects_malformed_body(self):
        for bad in (None, [], {}, {"name": "shell"}, {"name": 5, "enabled": True}):
            response = self.client.post("/api/config/tools", json=bad)
            self.assertEqual(response.status_code, 400, bad)

    # --- Breaker thresholds ------------------------------------------------

    def test_old_breaker_route_is_gone(self):
        # #494: breaker values are saved through the settings hub only (its
        # confirmation and history); tests/test_breaker_write_paths.py pins the rest.
        self.assertEqual(self.client.get("/api/config/breaker").status_code, 404)
        response = self.client.post("/api/config/breaker", json={"stability_warning": 0.55})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.agent_path.exists())

    # --- SOUL preview --------------------------------------------------------

    def test_get_soul_falls_back_to_default_persona_path(self):
        # No agent.soul_path is configured, so the endpoint falls back to
        # the bundled default persona SOUL.md; report whatever its real
        # on-disk state is rather than assuming it is absent.
        from stiracore.observability.config_api import DEFAULT_SOUL_PATH

        response = self.client.get("/api/config/soul")
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual(data["path"], str(DEFAULT_SOUL_PATH))
        self.assertEqual(data["exists"], DEFAULT_SOUL_PATH.exists())

    def test_get_soul_reports_missing_configured_file(self):
        missing = Path(self.tmp.name) / "missing-SOUL.md"
        AgentConfig(soul_path=str(missing), path=self.agent_path).save()

        response = self.client.get("/api/config/soul")
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual(data["path"], str(missing))
        self.assertEqual(data["exists"], False)
        self.assertEqual(data["preview"], "")

    def test_get_soul_reads_configured_path(self):
        soul_file = Path(self.tmp.name) / "SOUL.md"
        soul_file.write_text("# Test SOUL\nIdentity anchor.\n", encoding="utf-8")
        AgentConfig(soul_path=str(soul_file), path=self.agent_path).save()

        response = self.client.get("/api/config/soul")
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertTrue(data["exists"])
        self.assertIn("Identity anchor", data["preview"])


if __name__ == "__main__":
    unittest.main()

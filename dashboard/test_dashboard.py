"""No live state changes: all tests use a temporary file. Run with unittest."""

import json
import tempfile
import unittest
from pathlib import Path

from app import NeuralState, create_app


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        NeuralState(stability=0.9).save(self.path)
        self.app = create_app(self.path)
        self.client = self.app.test_client()

    def test_observation(self):
        original = self.path.read_bytes()
        self.assertEqual(self.client.get("/").status_code, 200)
        for asset in ("style.css", "app.js"):
            with self.client.get("/static/" + asset) as response:
                self.assertEqual(response.status_code, 200)
        for _ in range(3):
            self.assertEqual(self.client.get("/api/state").json["signal"], "GO")
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual(self.client.get("/api/history").json, [])

    def test_unmeasured_is_told_from_completely_stable(self):
        # #337: both score 1.0; the sample count and `bc_measured` tell them apart.
        data = self.client.get("/api/state").json
        self.assertEqual(
            (data["bc_score"], data["bc_samples"], data["bc_measured"]), (1.0, 0, False)
        )
        for window, measured in (([0.7] * 4, False), ([0.7] * 5, True), ([0.9, 0.5] * 5, True)):
            with self.subTest(samples=len(window)):
                NeuralState(stability=0.9, bc_window=window).save(self.path)
                data = self.client.get("/api/state").json
                self.assertEqual(data["bc_samples"], len(window))
                self.assertIs(data["bc_measured"], measured)
                self.assertEqual(data["bc_min_samples"], 5)

    def test_validation(self):
        cases = [None, [], {"stimulus": "curiosity"}, {"stimulus": []}]
        cases += [
            {"stimulus": "joy", "intensity": v}
            for v in [True, 2, -0.1, "0.8", float("nan"), float("inf")]
        ]
        for bad in cases:
            self.assertEqual(self.client.post("/api/stimulus", json=bad).status_code, 400, bad)
        self.assertEqual(
            self.client.post(
                "/api/stimulus", json={"stimulus": "joy"}, headers={"Origin": "https://evil.test"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get("/api/state", headers={"Host": "evil.test"}).status_code, 403
        )

    def test_mutation_and_restart(self):
        for _ in range(12):
            result = self.client.post("/api/stimulus", json={"stimulus": "joy", "intensity": 0.8})
            self.assertEqual(result.status_code, 200, result.json)
        history = self.client.get("/api/history").json
        self.assertEqual(len(history), 10)
        self.assertEqual(history[-1]["turn"], 12)
        self.assertEqual(create_app(self.path).test_client().get("/api/history").json, history)
        self.assertEqual(
            NeuralState.load(self.path).to_dict(),
            self.app.extensions["state_store"].state.to_dict(),
        )

    def test_thresholds_and_external_refresh(self):
        for stability, bc, expected in [
            (0.3, 1, "HOLD"),
            (0.5, 1, "GO"),
            (0.29, 1, "NO-GO"),
            (0.8, 0.59, "HOLD"),
            (0.8, 0.6, "GO"),
        ]:
            with self.subTest(stability=stability, bc_score=bc):
                # Entry thresholds are independent cases, not recovery transitions.
                # Construct before the external write to still exercise refresh().
                NeuralState(stability=0.9).save(self.path)
                client = create_app(self.path).test_client()
                data = json.loads(self.path.read_text())
                data.update(stability=stability, bc_score=bc)
                self.path.write_text(json.dumps(data))
                self.assertEqual(client.get("/api/state").json["signal"], expected)

    def test_warning_hysteresis_survives_external_refresh(self):
        for stability, expected in [
            (0.3, "HOLD"),
            (0.5, "HOLD"),
            (0.6, "HOLD"),
            (0.61, "GO"),
        ]:
            with self.subTest(stability=stability):
                data = json.loads(self.path.read_text())
                data.update(stability=stability)
                self.path.write_text(json.dumps(data))
                self.assertEqual(self.client.get("/api/state").json["signal"], expected)

    def test_coherence_hysteresis_survives_external_refresh(self):
        for bc_score, expected in [(0.59, "HOLD"), (0.6, "HOLD"), (0.7, "HOLD"), (0.71, "GO")]:
            with self.subTest(bc_score=bc_score):
                data = json.loads(self.path.read_text())
                data.update(bc_score=bc_score)
                self.path.write_text(json.dumps(data))
                self.assertEqual(self.client.get("/api/state").json["signal"], expected)

    def test_polling_does_not_advance_critical_cooldown(self):
        data = json.loads(self.path.read_text())
        data.update(stability=0.29)
        self.path.write_text(json.dumps(data))
        self.assertEqual(self.client.get("/api/state").json["signal"], "NO-GO")

        data.update(stability=0.9)
        self.path.write_text(json.dumps(data))
        original = self.path.read_bytes()
        store = self.app.extensions["state_store"]
        before = store.state.to_dict()
        before.update(stability=0.9)
        for _ in range(store.engine.config.critical_cooldown_turns + 2):
            self.assertEqual(self.client.get("/api/state").json["signal"], "NO-GO")
        self.assertEqual(store.state.to_dict(), before)
        self.assertEqual(store.engine._turn_count, 0)
        self.assertEqual(store.engine._critical_turns, 0)
        self.assertEqual(self.path.read_bytes(), original)

    def test_corrupt_state_fails_closed(self):
        self.path.write_text("{bad json")
        self.assertEqual(self.client.get("/api/state").status_code, 503)
        self.assertEqual(
            self.client.post("/api/stimulus", json={"stimulus": "joy"}).status_code, 503
        )


if __name__ == "__main__":
    unittest.main()

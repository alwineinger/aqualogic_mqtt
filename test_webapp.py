import os
import unittest
from unittest.mock import patch

from aqualogic_mqtt.webapp import create_app


class WebApiContractTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    @patch("aqualogic_mqtt.webapp.controls.get_equipment_status")
    def test_equipment_status_contract(self, status):
        status.return_value = {"available": True, "mode": "pool", "auto_heat": False}
        response = self.client.get("/api/equipment")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mode"], "pool")

    @patch("aqualogic_mqtt.webapp.controls.set_manual_override")
    def test_manual_override_api(self, set_manual):
        set_manual.return_value = {"desired": {"source": "manual"}}
        response = self.client.post("/api/automation/manual", json={"mode": "spa"})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        set_manual.assert_called_once_with({"mode": "spa"})

    def test_ui_contains_all_semantic_controls(self):
        static_dir = os.path.join(os.path.dirname(__file__), "aqualogic_mqtt", "static")
        response = create_app(static_dir=static_dir).test_client().get("/")
        html = response.get_data(as_text=True)
        for marker in (
            'data-mode="pool"',
            'data-mode="spa"',
            'data-mode="spillover"',
            'data-speed="speed1"',
            'data-speed="speed4"',
            'data-switch="auto_heat"',
            'data-switch="heater_relay"',
            'data-switch="lights"',
            'data-switch="blower"',
            '>Auto Heat</button>',
        ):
            self.assertIn(marker, html)
        response.close()

    def test_ui_does_not_treat_scheduled_vsp_hold_as_control_blocker(self):
        static_dir = os.path.join(os.path.dirname(__file__), "aqualogic_mqtt", "static")
        response = create_app(static_dir=static_dir).test_client().get("/")
        html = response.get_data(as_text=True)
        self.assertIn("const automationEnabled = state.automation?.enabled === true", html)
        self.assertIn("!automationEnabled && (state.busy || state.vsp?.busy)", html)
        self.assertIn("state.automation?.pool_heat_enabled === true", html)
        response.close()

    @patch("aqualogic_mqtt.webapp.controls.activate_openclaw_spa")
    def test_openclaw_spa_start_endpoint(self, activate):
        activate.return_value = {"desired": {"source": "calendar", "mode": "spa"}}
        response = self.client.post("/api/openclaw/spa", json={
            "session_id": "openclaw-test",
        })
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        activate.assert_called_once()


if __name__ == "__main__":
    unittest.main()

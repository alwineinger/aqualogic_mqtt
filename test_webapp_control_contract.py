import unittest
from unittest.mock import patch

from aqualogic_mqtt.webapp import create_app


class SemanticControlContractTest(unittest.TestCase):
    def setUp(self):
        self.client = create_app().test_client()

    @patch("aqualogic_mqtt.webapp.controls.request_equipment_mode")
    def test_mode_requires_target_field(self, request_mode):
        response = self.client.post("/api/control/mode", json={"mode": "spillover"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("'target' is required", response.get_json()["error"])
        request_mode.assert_not_called()

    @patch("aqualogic_mqtt.webapp.controls.request_equipment_mode")
    def test_mode_forwards_target_field(self, request_mode):
        request_mode.return_value = {"mode": "spillover"}

        response = self.client.post("/api/control/mode", json={"target": "spillover"})

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        request_mode.assert_called_once_with("spillover")

    @patch("aqualogic_mqtt.webapp.controls.request_vsp_preset")
    def test_pump_speed_requires_target_field(self, request_preset):
        response = self.client.post("/api/control/pump-speed", json={"preset": "speed1"})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("'target' is required", response.get_json()["error"])
        request_preset.assert_not_called()

    @patch("aqualogic_mqtt.webapp.controls.request_vsp_preset")
    def test_pump_speed_forwards_target_and_lease(self, request_preset):
        request_preset.return_value = {"target_preset": "speed1"}

        response = self.client.post(
            "/api/control/pump-speed",
            json={"target": "speed1", "lease_seconds": 90},
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        request_preset.assert_called_once_with("speed1", 90)


if __name__ == "__main__":
    unittest.main()

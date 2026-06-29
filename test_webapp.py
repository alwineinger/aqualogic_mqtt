import os
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from aqualogic_mqtt import controls
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

    def test_ui_hides_raw_filter_and_pool_spa_buttons(self):
        static_dir = os.path.join(os.path.dirname(__file__), "aqualogic_mqtt", "static")
        response = create_app(static_dir=static_dir).test_client().get("/")
        html = response.get_data(as_text=True)
        self.assertNotIn('data-k="filter"', html)
        self.assertNotIn('data-k="pool_spa"', html)
        for marker in ('data-k="menu"', 'data-k="plus"', 'data-k="minus"', 'data-k="left"', 'data-k="right"'):
            self.assertIn(marker, html)
        response.close()

    def test_navigation_keys_work_while_vsp_lease_is_holding(self):
        vsp = MagicMock()
        vsp.is_busy.return_value = True
        vsp.is_menu_busy.return_value = False
        automation = MagicMock()
        automation.hardware_busy.return_value = False
        sender = MagicMock()
        with (
            patch.object(controls, "_vsp_driver", vsp),
            patch.object(controls, "_automation", automation),
            patch.object(controls, "_key_sender", sender),
        ):
            client = create_app().test_client()
            for key in ("menu", "plus", "minus", "left", "right", "filter", "pool_spa"):
                response = client.post(f"/api/key/{key}")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["ok"])
        self.assertEqual(sender.call_count, 7)

    def test_navigation_keys_remain_blocked_during_vsp_menu_write(self):
        vsp = MagicMock()
        vsp.is_menu_busy.return_value = True
        sender = MagicMock()
        with (
            patch.object(controls, "_vsp_driver", vsp),
            patch.object(controls, "_key_sender", sender),
        ):
            response = create_app().test_client().post("/api/key/menu")
        self.assertFalse(response.get_json()["ok"])
        sender.assert_not_called()

    def test_ui_does_not_treat_scheduled_vsp_hold_as_control_blocker(self):
        static_dir = os.path.join(os.path.dirname(__file__), "aqualogic_mqtt", "static")
        response = create_app(static_dir=static_dir).test_client().get("/")
        html = response.get_data(as_text=True)
        self.assertIn("const automationEnabled = state.automation?.enabled === true", html)
        self.assertIn("controlsLocked = state.controls_locked === true || pendingControl !== null", html)
        self.assertIn("The backend excludes a scheduled VSP `holding` lease", html)
        self.assertIn("state.automation?.pool_heat_enabled === true", html)
        response.close()

    def test_ui_visibly_locks_all_controls_during_automated_work(self):
        static_dir = os.path.join(os.path.dirname(__file__), "aqualogic_mqtt", "static")
        response = create_app(static_dir=static_dir).test_client().get("/")
        html = response.get_data(as_text=True)
        self.assertIn('id="control-lock"', html)
        self.assertIn("controls temporarily locked", html)
        self.assertIn("document.querySelectorAll('button[data-k]')", html)
        self.assertIn("if (controlsLocked)", html)
        response.close()

    def test_ui_shows_optimistic_pending_state_until_hardware_confirmation(self):
        static_dir = os.path.join(os.path.dirname(__file__), "aqualogic_mqtt", "static")
        response = create_app(static_dir=static_dir).test_client().get("/")
        html = response.get_data(as_text=True)
        for marker in (
            "let pendingControl = null",
            "function commandCompleted(state, command)",
            "active: modePending == null && btn.dataset.mode === state.mode",
            "active: !speedGroupPending && btn.dataset.speed === state.vsp?.target_name",
            "setButtonState(btn, {active: !switchPending && active, pending: switchPending})",
            "pendingControl?.token === commandToken",
            "Command in progress — controls temporarily locked",
            ".direct-controls button.pending:disabled",
        ):
            self.assertIn(marker, html)
        response.close()

    def test_ui_pending_state_runtime(self):
        result = subprocess.run(
            ["node", os.path.join(os.path.dirname(__file__), "test_webui_pending.js")],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("WebUI pending-state tests passed", result.stdout)

    def test_equipment_status_locks_during_vsp_menu_work_but_not_holding(self):
        equipment = MagicMock()
        equipment.status.return_value = {"busy": False, "phase": "idle"}
        vsp = MagicMock()
        vsp.status.return_value = {
            "busy": True,
            "phase": "applying",
            "hardware_priming": False,
        }
        automation = MagicMock()
        automation.status.return_value = {"enabled": True, "phase": "setting_speed"}

        with (
            patch.object(controls, "_equipment", equipment),
            patch.object(controls, "_vsp_driver", vsp),
            patch.object(controls, "_automation", automation),
        ):
            active = controls.get_equipment_status()
            self.assertTrue(active["controls_locked"])
            self.assertIn("Pump control in progress", active["control_lock_reason"])

            vsp.status.return_value["phase"] = "holding"
            automation.status.return_value["phase"] = "holding_speed"
            holding = controls.get_equipment_status()
            self.assertFalse(holding["controls_locked"])
            self.assertIsNone(holding["control_lock_reason"])

    def test_equipment_status_locks_for_automation_reconciliation_phase(self):
        equipment = MagicMock()
        equipment.status.return_value = {"busy": False, "phase": "complete"}
        vsp = MagicMock()
        vsp.status.return_value = {
            "busy": False,
            "phase": "complete",
            "hardware_priming": False,
        }
        automation = MagicMock()
        automation.status.return_value = {"enabled": True, "phase": "setting_lights"}

        with (
            patch.object(controls, "_equipment", equipment),
            patch.object(controls, "_vsp_driver", vsp),
            patch.object(controls, "_automation", automation),
        ):
            status = controls.get_equipment_status()

        self.assertTrue(status["controls_locked"])
        self.assertIn("setting lights", status["control_lock_reason"])

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

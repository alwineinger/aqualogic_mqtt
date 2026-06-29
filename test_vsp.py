import os
import json
import tempfile
import time
import unittest

from aqualogic.keys import Keys

from aqualogic_mqtt import controls
from aqualogic_mqtt.vsp import PanelPumpState, VspDriver, VspInterlockError, _page_key
from aqualogic_mqtt.webapp import create_app


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class FakePanel:
    def send_key(self, _key):
        pass


class FakeController:
    TOP_LEVEL = ["Settings Menu", "Timers Menu", "Diagnostic Menu", "Configuration Menu-Locked", "Default Menu"]
    SETTINGS = ["Spa Heater1", "Pool Heater1 Manual Off", "VSP Speed Settings + to enter"]

    def __init__(self, active_preset=1):
        self.screen = "Filter Speed 70% Speed1"
        self.active_preset = active_preset
        self.presets = {1: 70, 2: 95, 3: 55, 4: 40}
        self.keys = []
        self.driver = None
        self.cache_value = f"Speed{self.active_preset}"
        self.cache_fresh = True

    def display(self):
        return {"lines": [self.screen, "", "", ""]}

    def cache(self):
        return {"values": {"pumpSpeedName": {"value": self.cache_value, "fresh": self.cache_fresh}}}

    def send_key(self, key):
        self.keys.append(key)
        normalized = " ".join(self.screen.lower().split())
        if key == Keys.MENU:
            if normalized in {value.lower() for value in self.TOP_LEVEL}:
                index = [value.lower() for value in self.TOP_LEVEL].index(normalized)
                self.screen = self.TOP_LEVEL[(index + 1) % len(self.TOP_LEVEL)]
            elif normalized.startswith("filter speed") or normalized.startswith("vsp speed"):
                self.screen = "Timers Menu"
            else:
                self.screen = "Settings Menu"
            return

        if key == Keys.RIGHT:
            if normalized == "settings menu":
                self.screen = self.SETTINGS[0]
                return
            for index, value in enumerate(self.SETTINGS):
                if normalized.startswith(value.lower().split(" +")[0]):
                    self.screen = self.SETTINGS[min(index + 1, len(self.SETTINGS) - 1)]
                    return
            if normalized.startswith("filter speed"):
                number = int(normalized.split("speed", 1)[1].split()[0])
                number = min(4, number + 1)
                self.screen = f"Filter Speed{number} {self.presets[number]}%"
                return

        if key == Keys.PLUS and normalized.startswith("vsp speed settings"):
            self.screen = f"Filter Speed1 {self.presets[1]}%"
            return

        if key in (Keys.PLUS, Keys.MINUS) and normalized.startswith("filter speed"):
            number = int(normalized.split("speed", 1)[1].split()[0])
            self.presets[number] += 5 if key == Keys.PLUS else -5
            self.screen = f"Filter Speed{number} {self.presets[number]}%"
            if number == self.active_preset and self.driver is not None:
                self.driver.observe(PanelPumpState(
                    requested_speed_pct=self.presets[number],
                    pump_power_w=300,
                    filter_on=True,
                    service_mode=False,
                ))


class VspDriverTest(unittest.TestCase):
    def make_driver(self, controller, **kwargs):
        driver = VspDriver(
            FakePanel(),
            enabled=kwargs.pop("enabled", True),
            enable_file=kwargs.pop("enable_file", None),
            default_lease_seconds=kwargs.pop("default_lease_seconds", 0.03),
            poll_interval_seconds=0.001,
            key_timeout_seconds=0.1,
            key_settle_seconds=0,
            key_sender=controller.send_key,
            display_reader=controller.display,
            menu_cache_reader=controller.cache,
            rollback_file=kwargs.pop("rollback_file", None),
            **kwargs,
        )
        controller.driver = driver
        driver.observe(PanelPumpState(
            requested_speed_pct=controller.presets[controller.active_preset],
            pump_power_w=400,
            filter_on=True,
            service_mode=False,
        ))
        return driver

    def test_edits_active_preset_without_filter_and_restores_after_lease(self):
        controller = FakeController(active_preset=1)
        driver = self.make_driver(controller)
        accepted = driver.request_preset("speed3")
        self.assertEqual(accepted["target_pct"], 55)
        self.assertTrue(wait_until(lambda: not driver.is_busy()))
        status = driver.status()
        self.assertEqual(status["phase"], "complete")
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["edited_preset"], "speed1")
        self.assertEqual(status["original_pct"], 70)
        self.assertEqual(controller.presets[1], 70)
        self.assertNotIn(Keys.FILTER, controller.keys)
        self.assertEqual(controller.screen, "Default Menu")

    def test_adopts_matching_observed_speed_without_menu_navigation(self):
        controller = FakeController(active_preset=1)
        driver = self.make_driver(controller)

        status = driver.adopt_observed_preset("speed1", source="schedule")

        self.assertFalse(status["busy"])
        self.assertEqual(status["phase"], "observed")
        self.assertEqual(status["target_name"], "speed1")
        self.assertTrue(status["verified"])
        self.assertEqual(controller.keys, [])

    def test_clears_observed_adoption_without_menu_navigation(self):
        controller = FakeController(active_preset=1)
        driver = self.make_driver(controller)
        driver.adopt_observed_preset("speed1")

        status = driver.clear_target()

        self.assertEqual(status["phase"], "idle")
        self.assertIsNone(status["target_name"])
        self.assertEqual(controller.keys, [])

    def test_lcd_detail_refresh_does_not_look_like_page_navigation(self):
        self.assertEqual(_page_key("Spa Heater1"), _page_key("Spa Heater1 Manual Off"))
        self.assertEqual(_page_key("VSP Speed Settings"), _page_key("VSP Speed Settings + to enter"))

    def test_enable_file_removal_cancels_and_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            enable_file = os.path.join(directory, "enabled")
            with open(enable_file, "w", encoding="utf-8"):
                pass
            controller = FakeController(active_preset=2)
            driver = self.make_driver(
                controller,
                enabled=False,
                enable_file=enable_file,
                default_lease_seconds=5,
            )
            driver.request_preset("speed4")
            self.assertTrue(wait_until(lambda: driver.status()["phase"] == "holding"))
            self.assertEqual(controller.presets[2], 40)
            os.unlink(enable_file)
            driver.tick()
            self.assertTrue(wait_until(lambda: not driver.is_busy()))
            self.assertEqual(controller.presets[2], 95)
            self.assertFalse(driver.status()["enabled"])

    def test_same_target_renews_active_lease_without_menu_reentry(self):
        controller = FakeController(active_preset=1)
        driver = self.make_driver(controller, default_lease_seconds=0.06)
        driver.request_preset("speed3")
        self.assertTrue(wait_until(lambda: driver.status()["phase"] == "holding"))
        operation_id = driver.status()["operation_id"]
        keys_before = list(controller.keys)
        time.sleep(0.03)
        renewed = driver.request_preset("speed3", lease_seconds=0.08)
        self.assertEqual(renewed["operation_id"], operation_id)
        self.assertEqual(controller.keys, keys_before)
        time.sleep(0.04)
        self.assertTrue(driver.is_busy())
        self.assertTrue(wait_until(lambda: not driver.is_busy()))
        self.assertEqual(controller.presets[1], 70)

    def test_holding_lease_does_not_block_manual_menu_navigation(self):
        controller = FakeController(active_preset=1)
        driver = self.make_driver(controller, default_lease_seconds=5)
        driver.request_preset("speed3")
        self.assertTrue(wait_until(lambda: driver.status()["phase"] == "holding"))
        self.assertTrue(driver.is_busy())
        self.assertFalse(driver.is_menu_busy())
        driver.clear_target()
        self.assertTrue(wait_until(lambda: not driver.is_busy()))
        self.assertFalse(driver.is_menu_busy())

    def test_persisted_rollback_waits_for_explicit_recovery_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            rollback_file = os.path.join(directory, "rollback.json")
            with open(rollback_file, "w", encoding="utf-8") as handle:
                json.dump({
                    "preset": "speed1",
                    "original_pct": 70,
                    "target_pct": 55,
                    "created_at_utc": "2026-06-27T14:00:00Z",
                }, handle)
            controller = FakeController(active_preset=1)
            controller.presets[1] = 55
            controller.screen = "Filter Speed 55% Speed1"
            driver = self.make_driver(
                controller,
                enabled=False,
                rollback_file=rollback_file,
            )
            driver.observe(PanelPumpState(
                requested_speed_pct=55,
                pump_power_w=230,
                filter_on=True,
                service_mode=False,
            ))
            self.assertFalse(driver.tick())
            self.assertFalse(driver.is_busy())
            self.assertEqual(controller.keys, [])
            self.assertEqual(driver.status()["rollback_target_pct"], 55)

            driver.recover_pending()
            self.assertTrue(wait_until(lambda: not driver.is_busy()))
            self.assertEqual(controller.presets[1], 70)
            self.assertFalse(os.path.exists(rollback_file))
            self.assertEqual(driver.status()["phase"], "recovered")

    def test_matching_persisted_lease_is_adopted_without_menu_navigation(self):
        with tempfile.TemporaryDirectory() as directory:
            rollback_file = os.path.join(directory, "rollback.json")
            with open(rollback_file, "w", encoding="utf-8") as handle:
                json.dump({
                    "preset": "speed1",
                    "original_pct": 70,
                    "target_pct": 55,
                    "created_at_utc": "2026-06-27T14:00:00Z",
                }, handle)
            controller = FakeController(active_preset=1)
            controller.presets[1] = 55
            driver = self.make_driver(controller, rollback_file=rollback_file)
            driver.observe(PanelPumpState(
                requested_speed_pct=55,
                pump_power_w=230,
                filter_on=True,
                service_mode=False,
            ))

            status = driver.adopt_observed_preset("speed3")

            self.assertEqual(status["phase"], "observed")
            self.assertEqual(status["target_name"], "speed3")
            self.assertEqual(status["edited_preset"], "speed1")
            self.assertEqual(status["original_pct"], 70)
            self.assertTrue(status["rollback_pending"])
            self.assertTrue(status["verified"])
            self.assertEqual(controller.keys, [])

    def test_service_mode_blocks_request(self):
        controller = FakeController()
        driver = self.make_driver(controller)
        driver.observe(PanelPumpState(filter_on=True, service_mode=True))
        with self.assertRaisesRegex(VspInterlockError, "Service mode"):
            driver.request_preset("speed2")

    def test_filter_off_blocks_request(self):
        controller = FakeController()
        driver = self.make_driver(controller)
        driver.observe(PanelPumpState(filter_on=False, service_mode=False))
        with self.assertRaisesRegex(VspInterlockError, "not confirmed on"):
            driver.request_preset("speed2")

    def test_filter_restart_does_not_create_software_prime_timer(self):
        now = [100.0]
        controller = FakeController()
        driver = self.make_driver(controller, clock=lambda: now[0])
        driver.observe(PanelPumpState(filter_on=False, service_mode=False, observed_at=now[0]))
        now[0] += 31
        driver.observe(PanelPumpState(filter_on=True, service_mode=False, observed_at=now[0]))
        self.assertFalse(driver.status()["hardware_priming"])

    def test_hardware_prime_display_blocks_speed_until_released(self):
        controller = FakeController()
        driver = self.make_driver(controller)
        controller.screen = "Filter Speed 100% Priming"
        self.assertTrue(driver.status()["hardware_priming"])
        with self.assertRaisesRegex(VspInterlockError, "hardware priming"):
            driver.request_preset("speed2")

        controller.screen = "Filter Speed 70% Speed1"
        self.assertFalse(driver.status()["hardware_priming"])

    def test_hardware_prime_cache_must_be_fresh(self):
        controller = FakeController()
        driver = self.make_driver(controller)
        controller.cache_value = "Start Delay"
        self.assertTrue(driver.status()["hardware_priming"])

        controller.cache_fresh = False
        self.assertFalse(driver.status()["hardware_priming"])


class VspApiTest(unittest.TestCase):
    def test_api_accepts_transactional_menu_operation(self):
        controller = FakeController()
        driver = VspDriver(
            FakePanel(),
            enabled=True,
            rollback_file=None,
            default_lease_seconds=0.02,
            poll_interval_seconds=0.001,
            key_timeout_seconds=0.1,
            key_settle_seconds=0,
            key_sender=controller.send_key,
            display_reader=controller.display,
            menu_cache_reader=controller.cache,
        )
        controller.driver = driver
        driver.observe(PanelPumpState(requested_speed_pct=70, filter_on=True, service_mode=False))
        controls.set_vsp_driver(driver)
        client = create_app().test_client()
        response = client.post("/api/vsp/speed", json={"preset": "speed3", "lease_seconds": 0.02})
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["ok"])
        self.assertTrue(wait_until(lambda: not driver.is_busy()))
        self.assertEqual(driver.status()["phase"], "complete")


if __name__ == "__main__":
    unittest.main()

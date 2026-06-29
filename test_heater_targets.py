import json
import tempfile
import time
import unittest

from aqualogic.keys import Keys
from aqualogic.states import States

from aqualogic_mqtt.heater_targets import (
    HeaterTargetDriver,
    HeaterTargetError,
    _page,
    parse_heater_target,
)


class FakePanel:
    def __init__(self, pool=85, spa=102):
        self.page = "default"
        self.targets = {"pool": pool, "spa": spa}
        self.service = False
        self.keys = []

    def get_state(self, state):
        if state == States.SERVICE:
            return self.service
        return False

    def display(self):
        if self.page == "default":
            line = "Default Menu"
        elif self.page == "settings":
            line = "Settings Menu"
        elif self.page in ("timers", "diagnostic", "configuration"):
            line = f"{self.page.title()} Menu"
        else:
            value = self.targets[self.page]
            line = f"{self.page.title()} Heater1 {value}°F" if value is not None else f"{self.page.title()} Heater1 Off"
        return {"lines": [line]}

    def send_key(self, key):
        self.keys.append(key)
        if key == Keys.MENU:
            top = ["settings", "timers", "diagnostic", "configuration", "default"]
            if self.page in top:
                self.page = top[(top.index(self.page) + 1) % len(top)]
            else:
                self.page = "timers"
        elif key == Keys.RIGHT and self.page == "settings":
            self.page = "spa"
        elif key == Keys.RIGHT and self.page == "spa":
            self.page = "pool"
        elif key in (Keys.PLUS, Keys.MINUS) and self.page in ("pool", "spa"):
            current = self.targets[self.page]
            if current is None:
                current = 65
            else:
                current += 1 if key == Keys.PLUS else -1
            self.targets[self.page] = current


def wait_complete(driver):
    deadline = time.time() + 2
    while driver.is_busy() and time.time() < deadline:
        time.sleep(0.005)
    assert not driver.is_busy(), driver.status()
    return driver.status()


class HeaterTargetDriverTest(unittest.TestCase):
    def make_driver(self, panel, state_file=None):
        return HeaterTargetDriver(
            panel,
            key_sender=panel.send_key,
            display_reader=panel.display,
            state_file=state_file,
            poll_interval_seconds=0.001,
            key_timeout_seconds=0.2,
            key_settle_seconds=0,
        )

    def test_uses_injected_normalized_service_mode_reader(self):
        panel = FakePanel()
        panel.get_state = lambda _state: (_ for _ in ()).throw(KeyError("desired_states"))
        driver = HeaterTargetDriver(
            panel,
            key_sender=panel.send_key,
            display_reader=panel.display,
            service_mode_reader=lambda: False,
            state_file=None,
            poll_interval_seconds=0.001,
            key_timeout_seconds=0.2,
            key_settle_seconds=0,
        )
        driver.request_refresh()
        self.assertEqual(wait_complete(driver)["phase"], "complete")

    def test_parse_numeric_and_off_targets(self):
        self.assertEqual(parse_heater_target("Spa Heater1 102°F"), ("spa", 102))
        self.assertEqual(parse_heater_target("Pool Heater1 Off"), ("pool", None))
        self.assertEqual(parse_heater_target("Pool Heater1 Manual Off"), ("pool", None))
        self.assertEqual(_page("Spa Heater1"), "spa_heater")
        self.assertEqual(_page("Pool Heater1"), "pool_heater")

    def test_refresh_reads_both_targets_without_changing_them(self):
        panel = FakePanel()
        driver = self.make_driver(panel, state_file=None)
        driver.request_refresh()
        status = wait_complete(driver)
        self.assertEqual(status["targets"], {"pool": 85, "spa": 102})
        self.assertEqual(status["known"], {"pool": True, "spa": True})
        self.assertNotIn(Keys.PLUS, panel.keys)
        self.assertNotIn(Keys.MINUS, panel.keys)
        self.assertEqual(panel.page, "default")

    def test_refresh_recovers_from_an_arbitrary_top_level_menu(self):
        panel = FakePanel()
        panel.page = "timers"
        driver = self.make_driver(panel, state_file=None)
        driver.request_refresh()
        status = wait_complete(driver)
        self.assertEqual(status["phase"], "complete")
        self.assertEqual(status["targets"], {"pool": 85, "spa": 102})
        self.assertEqual(panel.page, "default")

    def test_set_updates_only_requested_target_and_persists_cache(self):
        panel = FakePanel()
        with tempfile.TemporaryDirectory() as tmp:
            state_file = f"{tmp}/targets.json"
            driver = self.make_driver(panel, state_file=state_file)
            driver.request_set("pool", 87)
            status = wait_complete(driver)
            self.assertEqual(status["phase"], "complete")
            self.assertEqual(panel.targets, {"pool": 87, "spa": 102})
            with open(state_file, encoding="utf-8") as handle:
                saved = json.load(handle)
                self.assertEqual(saved["targets"]["pool"], 87)
                self.assertTrue(saved["known"]["pool"])

    def test_off_is_known_and_distinct_from_not_yet_read(self):
        panel = FakePanel(pool=None, spa=None)
        driver = self.make_driver(panel, state_file=None)
        self.assertEqual(driver.status()["known"], {"pool": False, "spa": False})
        driver.request_refresh()
        status = wait_complete(driver)
        self.assertEqual(status["targets"], {"pool": None, "spa": None})
        self.assertEqual(status["known"], {"pool": True, "spa": True})

    def test_rejects_out_of_range_and_service_mode(self):
        panel = FakePanel()
        driver = self.make_driver(panel, state_file=None)
        with self.assertRaisesRegex(ValueError, "between 65F and 104F"):
            driver.request_set("spa", 105)
        panel.service = True
        with self.assertRaisesRegex(HeaterTargetError, "Service mode"):
            driver.request_refresh()


if __name__ == "__main__":
    unittest.main()

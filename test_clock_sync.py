import time
import unittest
from datetime import datetime, timedelta, timezone

from aqualogic.keys import Keys

from aqualogic_mqtt.clock_sync import (
    ClockSyncDriver,
    clock_difference_minutes,
    display_hour_minute,
    display_weekday,
    parse_controller_clock,
)


UTC = timezone.utc


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


class FakeClockPanel:
    SETTINGS = [
        "Spa Heater1 Manual Off",
        "Pool Heater1 Manual Off",
        "VSP Speed Settings + to enter",
        "Super Chlorinate Off",
        "Spa Chlorinator 10%",
        "Pool Chlorinator 30%",
    ]
    TOP = ["Settings Menu", "Timers Menu", "Diagnostic Menu", "Configuration Menu-Locked", "Default Menu"]

    def __init__(self, controller_time):
        self.value = controller_time
        self.screen = "Default Menu"
        self.field = 0
        self.keys = []
        self.blink_after_edit = False
        self.blank_reads = 0

    def clock_line(self):
        hour = self.value.hour % 12 or 12
        suffix = "P" if self.value.hour >= 12 else "A"
        return f"Set Day and Time {self.value:%A} {hour}:{self.value:%M}{suffix}"

    def display(self):
        if self.blank_reads > 0 and self.screen.startswith("Set Day"):
            self.blank_reads -= 1
            return {"lines": ["Set Day and Time Saturday 10:  A", "", "", ""]}
        return {"lines": [self.screen, "", "", ""]}

    def cache(self):
        line = self.clock_line().replace("Set Day and Time ", "")
        return {"values": {"controllerClock": {"value": line, "fresh": True}}}

    def send_key(self, key):
        self.keys.append(key)
        if key == Keys.MENU:
            normalized = " ".join(self.screen.split())
            if normalized in self.TOP:
                self.screen = self.TOP[(self.TOP.index(normalized) + 1) % len(self.TOP)]
            else:
                self.screen = "Timers Menu"
            return
        if key == Keys.RIGHT:
            normalized = " ".join(self.screen.split())
            if normalized == "Settings Menu":
                self.screen = self.SETTINGS[0]
            elif normalized in self.SETTINGS:
                index = self.SETTINGS.index(normalized)
                self.screen = self.SETTINGS[index + 1] if index + 1 < len(self.SETTINGS) else self.clock_line()
                if self.screen.startswith("Set Day"):
                    self.field = 0
            elif self.screen.startswith("Set Day"):
                self.field += 1
                self.screen = "Display Light On for 60 sec" if self.field == 3 else self.clock_line()
            return
        if key not in (Keys.PLUS, Keys.MINUS) or not self.screen.startswith("Set Day"):
            return
        amount = 1 if key == Keys.PLUS else -1
        if self.field == 0:
            self.value += timedelta(days=amount)
        elif self.field == 1:
            self.value += timedelta(hours=amount)
        else:
            self.value += timedelta(minutes=amount)
        self.screen = self.clock_line()
        if self.blink_after_edit:
            self.blank_reads = 2


class ClockParsingTest(unittest.TestCase):
    def test_controller_clock_is_placed_near_reference_in_new_york(self):
        reference = datetime(2026, 6, 27, 14, 44, tzinfo=UTC)
        parsed = parse_controller_clock("Saturday 10 43A", reference)
        self.assertEqual(parsed.isoformat(), "2026-06-27T10:43:00-04:00")
        self.assertEqual(clock_difference_minutes("Saturday 10 43A", reference), -1)

    def test_dst_offsets_are_applied_from_the_reference_date(self):
        winter = parse_controller_clock("Saturday 10:43A", datetime(2026, 1, 3, 15, 44, tzinfo=UTC))
        summer = parse_controller_clock("Saturday 10:43A", datetime(2026, 6, 27, 14, 44, tzinfo=UTC))
        self.assertEqual(winter.utcoffset(), timedelta(hours=-5))
        self.assertEqual(summer.utcoffset(), timedelta(hours=-4))

    def test_blink_field_parsers_do_not_require_the_other_fields(self):
        self.assertEqual(display_hour_minute("Set Day and Time              10:56A"), (10, 56))
        self.assertEqual(display_weekday("Set Day and Time Saturday      :56A"), 5)


class ClockSyncDriverTest(unittest.TestCase):
    def make_driver(self, panel, now):
        return ClockSyncDriver(
            key_sender=panel.send_key,
            display_reader=panel.display,
            menu_cache_reader=panel.cache,
            state_file=None,
            now=lambda: now[0],
            poll_interval_seconds=0.001,
            key_settle_seconds=0,
            field_settle_seconds=0.001,
            key_timeout_seconds=0.1,
        )

    def test_weekly_check_does_not_touch_menu_when_clock_matches(self):
        now = [datetime(2026, 6, 27, 14, 44, tzinfo=UTC)]
        panel = FakeClockPanel(datetime(2026, 6, 27, 10, 44))
        driver = self.make_driver(panel, now)
        self.assertFalse(driver.check_or_start())
        self.assertEqual(panel.keys, [])
        self.assertEqual(driver.status()["phase"], "checked")
        self.assertFalse(driver.due(now[0] + timedelta(days=6)))
        self.assertTrue(driver.due(now[0] + timedelta(days=7)))

    def test_drift_sync_uses_guarded_day_hour_minute_transaction(self):
        now = [datetime(2026, 6, 27, 14, 50, tzinfo=UTC)]
        panel = FakeClockPanel(datetime(2026, 6, 27, 10, 47))
        panel.blink_after_edit = True
        driver = self.make_driver(panel, now)
        self.assertTrue(driver.check_or_start())
        self.assertTrue(wait_until(lambda: not driver.is_busy()))
        self.assertEqual(panel.value, datetime(2026, 6, 27, 10, 50))
        self.assertEqual(panel.screen, "Default Menu")
        self.assertEqual(panel.keys.count(Keys.PLUS), 3)
        self.assertEqual(driver.status()["phase"], "synced")


if __name__ == "__main__":
    unittest.main()

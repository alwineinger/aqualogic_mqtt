import unittest

from aqualogic_mqtt.default_menu import DefaultMenuCache


class DefaultMenuCacheTest(unittest.TestCase):
    def test_accumulates_default_menu_values(self):
        cache = DefaultMenuCache(stale_after_sec=45, clock=lambda: 100.0)
        cache.observe_display(
            [
                "Pool Temp  84°F",
                "Air Temp   79°F",
                "Pool Chlorinator 30%",
                "Salt Level 3100 PPM",
                "Heater1 Manual Off",
                "Filter Speed       55% Speed3",
            ],
            leds={"POOL": True, "SPA": False},
            observed_at=100.0,
        )

        snapshot = cache.as_dict()

        self.assertTrue(snapshot["complete"])
        self.assertTrue(snapshot["fresh"])
        self.assertEqual(snapshot["values"]["poolTempF"]["value"], 84)
        self.assertEqual(snapshot["values"]["ambientF"]["value"], 79)
        self.assertEqual(snapshot["values"]["poolChlorinatorPct"]["value"], 30)
        self.assertEqual(snapshot["values"]["saltPpm"]["value"], 3100)
        self.assertEqual(snapshot["values"]["pumpSpeedPct"]["value"], 55)
        self.assertEqual(snapshot["values"]["pumpSpeedName"]["value"], "Speed3")
        self.assertEqual(snapshot["values"]["poolSpaMode"]["value"], "pool")

    def test_state_changing_key_invalidates_previous_values(self):
        now = 100.0
        cache = DefaultMenuCache(stale_after_sec=45, clock=lambda: now)
        cache.observe_display(
            [
                "Pool Temp 84°F",
                "Air Temp 79°F",
                "Pool Chlorinator 30%",
                "Salt Level 3100 PPM",
                "Heater1 Manual Off",
                "Filter Speed 55% Speed3",
            ],
            observed_at=100.0,
        )
        self.assertTrue(cache.as_dict()["fresh"])

        cache.invalidate_for_key("filter", observed_at=101.0)
        snapshot = cache.as_dict()
        self.assertFalse(snapshot["fresh"])
        self.assertIn("filter", snapshot["missing_groups"])
        self.assertEqual(snapshot["values"]["pumpSpeedPct"]["stale_reason"], "key:filter")

        cache.observe_display(["Pump Off"], observed_at=102.0)
        snapshot = cache.as_dict()
        self.assertEqual(snapshot["values"]["filterState"]["display"], "Off")
        self.assertTrue(snapshot["values"]["filterState"]["fresh"])
        self.assertFalse(snapshot["complete"])

    def test_age_marks_values_stale(self):
        # Freshness stale threshold is 10s; age 90s < removal threshold 180s,
        # so value/display/age remain present even while comfort-fresh is False.
        now = 190.0
        cache = DefaultMenuCache(stale_after_sec=10, clock=lambda: now)
        cache.observe_display(["Pool Temp 84°F"], observed_at=100.0)

        snapshot = cache.as_dict()

        self.assertFalse(snapshot["values"]["poolTempF"]["fresh"])
        self.assertEqual(snapshot["values"]["poolTempF"]["stale_reason"], "stale")
        self.assertEqual(snapshot["values"]["poolTempF"]["value"], 84)
        self.assertEqual(snapshot["values"]["poolTempF"]["age_sec"], 90.0)

    def test_mode_uses_spillover_leds(self):
        cache = DefaultMenuCache(stale_after_sec=45, clock=lambda: 100.0)

        cache.observe_display([], leds={"POOL": True, "SPA": True}, observed_at=100.0)
        self.assertEqual(cache.as_dict()["values"]["poolSpaMode"]["display"], "Spa Overflow")

        cache.observe_display([], leds={"SPILLOVER": True}, observed_at=101.0)
        self.assertEqual(cache.as_dict()["values"]["poolSpaMode"]["value"], "spa_overflow")

    def test_spa_mode_pump_preset_is_recognized(self):
        cache = DefaultMenuCache(stale_after_sec=45, clock=lambda: 100.0)

        cache.observe_display(["Filter Speed 95% Spa Mode"], observed_at=100.0)
        snapshot = cache.as_dict()

        self.assertEqual(snapshot["values"]["pumpSpeedPct"]["value"], 95)
        self.assertEqual(snapshot["values"]["pumpSpeedName"]["value"], "Spa Mode")

    def test_spa_countdown_screen_is_recognized(self):
        cache = DefaultMenuCache(stale_after_sec=45, clock=lambda: 100.0)

        cache.observe_display(["Spa-CountDn 00:24"], observed_at=100.0)
        snapshot = cache.as_dict()

        self.assertEqual(snapshot["values"]["spaCountdown"]["value"], "00:24")
        self.assertEqual(snapshot["values"]["poolSpaMode"]["display"], "Spa")
        self.assertIn("spa_countdown", snapshot["pages"])

    def test_heater_status_comes_from_screen_and_output_from_led(self):
        cache = DefaultMenuCache(stale_after_sec=45, clock=lambda: 100.0)

        cache.observe_display(
            ["Heater1 Auto Control"],
            leds={"HEATER1": False},
            observed_at=100.0,
        )
        snapshot = cache.as_dict()

        self.assertEqual(snapshot["values"]["heater1Status"]["value"], "Auto Control")
        self.assertEqual(snapshot["values"]["heater1Status"]["display"], "Auto Control")
        self.assertEqual(snapshot["values"]["heaterRun"]["value"], False)
        self.assertEqual(snapshot["values"]["heaterRun"]["display"], "Off")
        self.assertEqual(snapshot["values"]["heaterRun"]["raw"], "led:heater")

    def test_heater_screen_does_not_overwrite_led_output(self):
        cache = DefaultMenuCache(stale_after_sec=45, clock=lambda: 100.0)

        cache.observe_display(
            ["Heater1 Manual Off"],
            leds={"HEATER1": True},
            observed_at=100.0,
        )
        snapshot = cache.as_dict()

        self.assertEqual(snapshot["values"]["heater1Status"]["value"], "Manual Off")
        self.assertEqual(snapshot["values"]["heaterRun"]["value"], True)
        self.assertEqual(snapshot["values"]["heaterRun"]["display"], "On")
        self.assertEqual(snapshot["values"]["heaterRun"]["raw"], "led:heater")


class StalenessRemovalTest(unittest.TestCase):
    """3-minute removal threshold tests for value/display/age_sec in webUI payload."""

    def test_value_under_3min_is_present(self):
        # age ~ 90s < 180s => value, display ("reading"), and age_sec are populated
        now = 190.0
        cache = DefaultMenuCache(stale_after_sec=45, clock=lambda: now)
        cache.observe_display(["Pool Temp 78°F"], observed_at=100.0)

        v = cache.as_dict()["values"]["poolTempF"]
        self.assertEqual(v["value"], 78)
        self.assertEqual(v["display"], "78F")
        self.assertAlmostEqual(v["age_sec"], 90.0, places=1)
        self.assertIsNotNone(v["observed_at"])

    def test_value_over_3min_has_value_display_age_removed(self):
        # age ~ 300s > 180s => value/display/age cleared for poolTempF;
        # a later sample remains unaffected
        now = 400.0
        cache = DefaultMenuCache(stale_after_sec=45, clock=lambda: now)
        cache.observe_display(["Pool Temp 78°F"], observed_at=100.0)
        cache.observe_display(["Air Temp 71°F"], observed_at=350.0)

        vals = cache.as_dict()["values"]
        stale = vals["poolTempF"]
        fresh = vals["ambientF"]

        self.assertIsNone(stale["value"])
        self.assertIsNone(stale["display"])
        self.assertIsNone(stale["age_sec"])
        self.assertIsNotNone(stale.get("observed_at"))
        self.assertEqual(fresh["value"], 71)
        self.assertEqual(fresh["display"], "71F")

    def test_value_exactly_at_3min_is_kept(self):
        # Threshold is strict ">"; exactly 180s still returns data.
        now = 280.0
        cache = DefaultMenuCache(stale_after_sec=45, clock=lambda: now)
        cache.observe_display(["Pool Temp 78°F"], observed_at=100.0)

        v = cache.as_dict()["values"]["poolTempF"]
        self.assertEqual(v["value"], 78)
        self.assertEqual(v["display"], "78F")
        self.assertAlmostEqual(v["age_sec"], 180.0, places=1)


if __name__ == "__main__":
    unittest.main()

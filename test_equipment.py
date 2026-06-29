import time
import unittest

from aqualogic.keys import Keys
from aqualogic.states import States

from aqualogic_mqtt.equipment import EquipmentController, EquipmentError


class FakePanel:
    def __init__(self):
        self.states = {
            States.POOL: True,
            States.SPA: False,
            States.SPILLOVER: False,
            States.SERVICE: False,
            States.FILTER: True,
            States.HEATER_AUTO_MODE: False,
            States.HEATER_1: False,
            States.AUX_2: True,
            States.LIGHTS: False,
            States.AUX_1: False,
        }
        self.set_calls = []
        self.key_calls = []

    def get_state(self, state):
        return self.states.get(state, False)

    def set_state(self, state, enabled):
        self.set_calls.append((state, enabled))
        self.states[state] = enabled
        return True

    def send_key(self, key):
        self.key_calls.append(key)
        if key == Keys.POOL_SPA:
            current = self.mode()
            target = {"pool": "spa", "spa": "spillover", "spillover": "pool"}[current]
            self.states[States.POOL] = target in ("pool", "spillover")
            self.states[States.SPA] = target in ("spa", "spillover")
            self.states[States.SPILLOVER] = target == "spillover"

    def mode(self):
        if self.states[States.SPILLOVER] or (self.states[States.POOL] and self.states[States.SPA]):
            return "spillover"
        if self.states[States.SPA]:
            return "spa"
        return "pool"


class TransientPanel(FakePanel):
    def __init__(self):
        super().__init__()
        self.transient_reads = 0

    def get_state(self, state):
        if self.transient_reads > 0:
            self.transient_reads -= 1
            raise KeyError("desired_states")
        return super().get_state(state)

    def send_key(self, key):
        super().send_key(key)
        self.transient_reads = 5


class RejectIntermediateSpaObservationPanel(FakePanel):
    """Fail if control waits on Spa between Pool and Spillover keypresses."""

    def get_state(self, state):
        if self.mode() == "spa" and state in (States.POOL, States.SPA, States.SPILLOVER):
            raise AssertionError("spillover transition observed intermediate Spa mode")
        return super().get_state(state)


class StartupUnknownPanel(FakePanel):
    def __init__(self, unknown_mode_reads=6):
        super().__init__()
        self.unknown_mode_reads = unknown_mode_reads

    def get_state(self, state):
        if state in (States.POOL, States.SPA, States.SPILLOVER) and self.unknown_mode_reads > 0:
            self.unknown_mode_reads -= 1
            return False
        return super().get_state(state)

    def send_key(self, key):
        super().send_key(key)
        self.filter_reads_remaining = 4


class MissSecondSelectionPanel(FakePanel):
    """Apply Pool→Spa, then simulate the second selection being missed."""

    def send_key(self, key):
        self.key_calls.append(key)
        if key == Keys.POOL_SPA and len(self.key_calls) == 1:
            self.states[States.POOL] = False
            self.states[States.SPA] = True
            self.states[States.SPILLOVER] = False


class EquipmentControllerTest(unittest.TestCase):
    def test_switch_mapping_preserves_aux_assignments(self):
        panel = FakePanel()
        controller = EquipmentController(panel, valve_settle_seconds=0)
        controller.set_switch("blower", True)
        controller.set_switch("heater_relay", False)
        controller.set_switch("auto_heat", True)
        self.assertEqual(panel.set_calls, [
            (States.AUX_1, True),
            (States.AUX_2, False),
            (States.HEATER_AUTO_MODE, True),
        ])

    def test_status_exposes_hardware_heater_running_state(self):
        panel = FakePanel()
        controller = EquipmentController(panel, valve_settle_seconds=0)
        self.assertFalse(controller.status()["heater_running"])
        panel.states[States.HEATER_1] = True
        self.assertTrue(controller.status()["heater_running"])

    def test_auto_heat_waits_for_authoritative_heater_page(self):
        panel = FakePanel()
        panel.states[States.HEATER_AUTO_MODE] = True
        cache = {"values": {}}
        controller = EquipmentController(
            panel,
            valve_settle_seconds=0,
            menu_cache_reader=lambda: cache,
        )

        startup = controller.status()
        self.assertTrue(startup["auto_heat"])
        self.assertFalse(startup["auto_heat_confirmed"])

        cache["values"]["heater1Status"] = {
            "value": "Manual Off", "fresh": True, "observed_at": 1.0,
        }
        confirmed = controller.status()
        self.assertFalse(confirmed["auto_heat"])
        self.assertTrue(confirmed["auto_heat_confirmed"])

    def test_auto_heat_command_is_not_repeated_while_confirmation_is_pending(self):
        panel = FakePanel()
        panel.states[States.HEATER_AUTO_MODE] = True
        cache = {
            "values": {
                "heater1Status": {
                    "value": "Auto Control", "fresh": True, "observed_at": 1.0,
                },
            },
        }
        controller = EquipmentController(
            panel,
            valve_settle_seconds=0,
            menu_cache_reader=lambda: cache,
        )

        first = controller.set_switch("auto_heat", False)
        second = controller.set_switch("auto_heat", False)

        self.assertEqual(panel.set_calls, [(States.HEATER_AUTO_MODE, False)])
        self.assertTrue(first["status"]["busy"])
        self.assertTrue(second["busy"])
        self.assertFalse(second["auto_heat"])

        cache["values"]["heater1Status"] = {
            "value": "Manual Off", "fresh": True, "observed_at": 2.0,
        }
        settled = controller.status()
        self.assertFalse(settled["busy"])
        self.assertIsNone(settled["pending_switch"])
        self.assertEqual(settled["phase"], "complete")

    def test_auto_heat_timeout_blocks_retry_until_a_new_heater_page(self):
        now = [0.0]
        panel = FakePanel()
        panel.states[States.HEATER_AUTO_MODE] = True
        cache = {
            "values": {
                "heater1Status": {
                    "value": "Auto Control", "fresh": True, "observed_at": 1.0,
                },
            },
        }
        controller = EquipmentController(
            panel,
            clock=lambda: now[0],
            switch_confirmation_seconds=2,
            menu_cache_reader=lambda: cache,
        )

        controller.set_switch("auto_heat", False)
        now[0] = 3.0
        timed_out = controller.status()
        self.assertEqual(timed_out["switch_retry_block"]["target"], False)

        controller.set_switch("auto_heat", False)
        self.assertEqual(panel.set_calls, [(States.HEATER_AUTO_MODE, False)])

        cache["values"]["heater1Status"] = {
            "value": "Auto Control", "fresh": True, "observed_at": 2.0,
        }
        self.assertIsNone(controller.status()["switch_retry_block"])

    def test_service_mode_blocks_switch(self):
        panel = FakePanel()
        panel.states[States.SERVICE] = True
        with self.assertRaisesRegex(EquipmentError, "Service mode"):
            EquipmentController(panel, valve_settle_seconds=0).set_switch("lights", True)

    def test_mode_reaches_explicit_spillover(self):
        panel = RejectIntermediateSpaObservationPanel()
        controller = EquipmentController(panel, poll_interval_seconds=0.001, valve_settle_seconds=0)
        accepted = controller.request_mode("spillover")
        self.assertTrue(accepted["busy"])
        deadline = time.monotonic() + 1
        while controller.status()["busy"] and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(controller.status()["mode"], "spillover")
        self.assertEqual(controller.status()["phase"], "complete")
        self.assertEqual(panel.key_calls, [Keys.POOL_SPA, Keys.POOL_SPA])

    def test_mode_waits_for_stable_startup_observation_before_sending_key(self):
        panel = StartupUnknownPanel()
        controller = EquipmentController(panel, poll_interval_seconds=0.001, valve_settle_seconds=0)
        controller.request_mode("spa")
        deadline = time.monotonic() + 1
        while controller.status()["busy"] and time.monotonic() < deadline:
            time.sleep(0.001)

        status = controller.status()
        self.assertEqual(status["phase"], "complete")
        self.assertIsNone(status["last_error"])
        self.assertEqual(status["mode"], "spa")
        self.assertEqual(panel.key_calls, [Keys.POOL_SPA])

    def test_recovered_mode_observation_clears_transient_error(self):
        panel = FakePanel()
        panel.states[States.POOL] = False
        controller = EquipmentController(
            panel,
            mode_timeout_seconds=0.005,
            poll_interval_seconds=0.001,
            valve_settle_seconds=0,
        )
        controller.request_mode("pool")
        deadline = time.monotonic() + 1
        while controller.status()["busy"] and time.monotonic() < deadline:
            time.sleep(0.001)

        failed = controller.status()
        self.assertEqual(failed["phase"], "failed")
        self.assertIn("timed out waiting for current PL-PLUS mode", failed["last_error"])

        panel.states[States.POOL] = True
        recovered = controller.status()
        self.assertEqual(recovered["phase"], "recovered")
        self.assertIsNone(recovered["last_error"])
        self.assertEqual(recovered["mode"], "pool")

    def test_transient_state_map_reset_does_not_fail_or_duplicate_key(self):
        panel = TransientPanel()
        controller = EquipmentController(panel, poll_interval_seconds=0.001, valve_settle_seconds=0)
        controller.request_mode("spa")
        deadline = time.monotonic() + 1
        while controller.status()["busy"] and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(controller.status()["phase"], "complete")
        self.assertEqual(controller.status()["mode"], "spa")
        self.assertEqual(panel.key_calls, [Keys.POOL_SPA])

    def test_spillover_from_pool_does_not_wait_in_spa(self):
        panel = RejectIntermediateSpaObservationPanel()
        sleep_calls = []
        controller = EquipmentController(
            panel,
            sleep=lambda seconds: sleep_calls.append(seconds),
            poll_interval_seconds=0.001,
            valve_settle_seconds=0,
        )
        controller.request_mode("spillover")
        deadline = time.monotonic() + 1
        while controller.status()["busy"] and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(controller.status()["phase"], "complete")
        self.assertEqual(controller.status()["mode"], "spillover")
        self.assertEqual(panel.key_calls, [Keys.POOL_SPA, Keys.POOL_SPA])
        self.assertEqual(panel.set_calls, [])
        self.assertEqual(sleep_calls.count(0.5), 1)

    def test_spillover_confirmation_uses_15_second_deadline(self):
        panel = MissSecondSelectionPanel()
        elapsed = [0.0]

        def clock():
            return elapsed[0]

        def sleep(seconds):
            elapsed[0] += seconds

        controller = EquipmentController(
            panel,
            clock=clock,
            sleep=sleep,
            mode_timeout_seconds=60,
            spillover_timeout_seconds=15,
            poll_interval_seconds=0.25,
            valve_settle_seconds=0,
        )
        controller.request_mode("spillover")
        deadline = time.monotonic() + 1
        while controller.status()["busy"] and time.monotonic() < deadline:
            time.sleep(0.001)

        status = controller.status()
        self.assertEqual(status["phase"], "failed")
        self.assertIn("timed out waiting for spillover mode", status["last_error"])
        self.assertLess(elapsed[0], 20)


if __name__ == "__main__":
    unittest.main()

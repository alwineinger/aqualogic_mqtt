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


class ValveDelayPanel(FakePanel):
    def __init__(self):
        super().__init__()
        self.filter_reads_remaining = 0

    def get_state(self, state):
        if state == States.FILTER and self.filter_reads_remaining > 0:
            self.filter_reads_remaining -= 1
            return False
        return super().get_state(state)

    def send_key(self, key):
        super().send_key(key)
        self.filter_reads_remaining = 4


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

    def test_service_mode_blocks_switch(self):
        panel = FakePanel()
        panel.states[States.SERVICE] = True
        with self.assertRaisesRegex(EquipmentError, "Service mode"):
            EquipmentController(panel, valve_settle_seconds=0).set_switch("lights", True)

    def test_mode_reaches_explicit_spillover(self):
        panel = FakePanel()
        controller = EquipmentController(panel, poll_interval_seconds=0.001, valve_settle_seconds=0)
        accepted = controller.request_mode("spillover")
        self.assertTrue(accepted["busy"])
        deadline = time.monotonic() + 1
        while controller.status()["busy"] and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(controller.status()["mode"], "spillover")
        self.assertEqual(controller.status()["phase"], "complete")
        self.assertEqual(panel.key_calls, [Keys.POOL_SPA, Keys.POOL_SPA])

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

    def test_next_mode_key_waits_for_filter_after_valve_transition(self):
        panel = ValveDelayPanel()
        controller = EquipmentController(panel, poll_interval_seconds=0.001, valve_settle_seconds=0)
        controller.request_mode("spillover")
        deadline = time.monotonic() + 1
        while controller.status()["busy"] and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(controller.status()["phase"], "complete")
        self.assertEqual(controller.status()["mode"], "spillover")
        self.assertEqual(panel.key_calls, [Keys.POOL_SPA, Keys.POOL_SPA])


if __name__ == "__main__":
    unittest.main()

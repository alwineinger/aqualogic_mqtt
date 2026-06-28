"""Serialized desired-state controls for PL-PLUS equipment outputs."""

from __future__ import annotations

import time
import uuid
import logging
from threading import Lock, Thread
from typing import Callable, Optional

from aqualogic.keys import Keys
from aqualogic.states import States


logger = logging.getLogger("aqualogic_mqtt.equipment")


class EquipmentError(RuntimeError):
    pass


class EquipmentBusyError(EquipmentError):
    pass


SWITCH_STATES = {
    "filter": States.FILTER,
    "auto_heat": States.HEATER_AUTO_MODE,
    "heater_relay": States.AUX_2,
    "lights": States.LIGHTS,
    "blower": States.AUX_1,
}

MODE_ORDER = ("pool", "spa", "spillover")


class EquipmentController:
    def __init__(
        self,
        panel: object,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        mode_timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 0.25,
        valve_settle_seconds: float = 35.0,
    ):
        self._panel = panel
        self._clock = clock
        self._sleep = sleep
        self._mode_timeout_seconds = float(mode_timeout_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._valve_settle_seconds = float(valve_settle_seconds)
        self._lock = Lock()
        self._worker: Optional[Thread] = None
        self._operation_id: Optional[str] = None
        self._phase = "idle"
        self._target_mode: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_states: dict[States, bool] = {}

    def _read_state(self, state: States) -> tuple[bool, bool]:
        try:
            value = bool(self._panel.get_state(state))
            self._last_states[state] = value
            return value, True
        except Exception as exc:
            logger.debug("transient PL-PLUS state read failed for %s: %s", state, exc)
            return self._last_states.get(state, False), False

    def _state(self, state: States) -> bool:
        return self._read_state(state)[0]

    @staticmethod
    def _mode_from_states(pool: bool, spa: bool, spill: bool) -> str:
        if spill or (pool and spa):
            return "spillover"
        if spa:
            return "spa"
        if pool:
            return "pool"
        return "unknown"

    def _mode_snapshot(self) -> tuple[str, bool]:
        pool, pool_fresh = self._read_state(States.POOL)
        spa, spa_fresh = self._read_state(States.SPA)
        spill, spill_fresh = self._read_state(States.SPILLOVER)
        return self._mode_from_states(pool, spa, spill), pool_fresh and spa_fresh and spill_fresh

    def mode(self) -> str:
        return self._mode_snapshot()[0]

    def status(self) -> dict:
        with self._lock:
            mode = self.mode()
            busy = self._worker is not None and self._worker.is_alive()
            recovered_mode_observation = (
                mode in MODE_ORDER
                and not busy
                and self._phase == "failed"
                and bool(self._last_error)
                and (
                    self._last_error.startswith("current PL-PLUS mode is unknown")
                    or self._last_error.startswith("timed out waiting for current PL-PLUS mode")
                )
            )
            if recovered_mode_observation:
                self._phase = "recovered"
                self._last_error = None
            return {
                "mode": mode,
                "service_mode": self._state(States.SERVICE),
                "filter_on": self._state(States.FILTER),
                "auto_heat": self._state(States.HEATER_AUTO_MODE),
                "heater_relay": self._state(States.AUX_2),
                "heater_running": self._state(States.HEATER_1),
                "lights": self._state(States.LIGHTS),
                "blower": self._state(States.AUX_1),
                "operation_id": self._operation_id,
                "phase": self._phase,
                "target_mode": self._target_mode,
                "busy": busy,
                "last_error": self._last_error,
            }

    def set_switch(self, control: str, enabled: bool) -> dict:
        name = str(control or "").strip().lower()
        if name not in SWITCH_STATES:
            raise ValueError(f"unsupported equipment control: {control}")
        if not isinstance(enabled, bool):
            raise ValueError("switch target must be a boolean")
        if self._state(States.SERVICE):
            raise EquipmentError("hardware Service mode is active")
        try:
            accepted = self._panel.set_state(SWITCH_STATES[name], enabled)
        except Exception as exc:
            raise EquipmentError(f"PL-PLUS failed to set {name}={enabled}: {exc}") from exc
        if accepted is False:
            raise EquipmentError(f"PL-PLUS rejected {name}={enabled}")
        return {"ok": True, "control": name, "target": enabled, "status": self.status()}

    def request_mode(self, target: str) -> dict:
        mode = str(target or "").strip().lower()
        if mode not in MODE_ORDER:
            raise ValueError("mode must be pool, spa, or spillover")
        if self._state(States.SERVICE):
            raise EquipmentError("hardware Service mode is active")
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise EquipmentBusyError("a mode transition is already active")
            self._operation_id = uuid.uuid4().hex
            self._phase = "queued"
            self._target_mode = mode
            self._last_error = None
            worker = Thread(target=self._run_mode, args=(mode,), daemon=True, name="plplus-mode")
            self._worker = worker
            worker.start()
        return self.status()

    def _wait_mode(self, expected: str) -> None:
        deadline = self._clock() + self._mode_timeout_seconds
        stable_reads = 0
        while self._clock() < deadline:
            if self._state(States.SERVICE):
                raise EquipmentError("hardware Service mode became active")
            mode, fresh = self._mode_snapshot()
            stable_reads = stable_reads + 1 if fresh and mode == expected else 0
            if stable_reads >= 3:
                return
            self._sleep(self._poll_interval_seconds)
        raise EquipmentError(f"timed out waiting for {expected} mode (current={self.mode()})")

    def _wait_current_mode(self) -> str:
        """Wait for a stable initial mode before deciding which keys to send."""
        deadline = self._clock() + self._mode_timeout_seconds
        stable_reads = 0
        last_mode = "unknown"
        while self._clock() < deadline:
            if self._state(States.SERVICE):
                raise EquipmentError("hardware Service mode became active")
            mode, fresh = self._mode_snapshot()
            last_mode = mode
            stable_reads = stable_reads + 1 if fresh and mode in MODE_ORDER else 0
            if stable_reads >= 3:
                return mode
            self._sleep(self._poll_interval_seconds)
        raise EquipmentError(f"timed out waiting for current PL-PLUS mode (current={last_mode})")

    def _settle_valves(self) -> None:
        deadline = self._clock() + self._valve_settle_seconds
        with self._lock:
            self._phase = "valve_settling"
        while self._clock() < deadline:
            if self._state(States.SERVICE):
                raise EquipmentError("hardware Service mode became active during valve settling")
            self._sleep(min(self._poll_interval_seconds, max(0.0, deadline - self._clock())))

    def _run_mode(self, target: str) -> None:
        try:
            with self._lock:
                self._phase = "transitioning"
            current = self._wait_current_mode()
            current_index = MODE_ORDER.index(current)
            target_index = MODE_ORDER.index(target)
            steps = (target_index - current_index) % len(MODE_ORDER)
            for offset in range(1, steps + 1):
                expected = MODE_ORDER[(current_index + offset) % len(MODE_ORDER)]
                self._panel.send_key(Keys.POOL_SPA)
                self._wait_mode(expected)
                self._settle_valves()
            with self._lock:
                self._phase = "complete"
                self._last_error = None
        except Exception as exc:
            with self._lock:
                self._phase = "failed"
                self._last_error = str(exc)
        finally:
            with self._lock:
                self._target_mode = None

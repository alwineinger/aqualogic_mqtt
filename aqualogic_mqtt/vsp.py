"""No-power-cycle VSP control through PL-PLUS-owned settings.

Directly injecting controller-to-pump request frames is not stable because the
PL-PLUS continues broadcasting the speed selected by its active timer.  This
driver instead edits the percentage of the *currently active* Filter Speed
preset in Settings -> VSP Speed Settings.  PL-PLUS then owns and continuously
broadcasts the resulting speed without turning Filter off.
"""

from __future__ import annotations

import logging
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, Lock, Thread
from typing import Callable, Optional

from aqualogic.keys import Keys

logger = logging.getLogger("aqualogic_mqtt.vsp")

PRESET_SPEEDS = {
    "speed1": 70,
    "speed2": 95,
    "speed3": 55,
    "speed4": 40,
}

_FILTER_SPEED_RE = re.compile(r"^filter\s+speed\s*([1-4])(?:\s+(\d+)\s*%)?$", re.I)


class VspError(RuntimeError):
    """Base exception for VSP control errors."""


class VspDisabledError(VspError):
    """Raised when the commissioned driver is disabled by its interlock."""


class VspInterlockError(VspError):
    """Raised when hardware state makes a speed request unsafe."""


class VspBusyError(VspError):
    """Raised when another menu operation is still active."""


@dataclass(frozen=True)
class PanelPumpState:
    requested_speed_pct: Optional[int] = None
    pump_power_w: Optional[int] = None
    filter_on: Optional[bool] = None
    service_mode: Optional[bool] = None
    observed_at: Optional[float] = None


def _normalize(value: object) -> str:
    return " ".join(str(value or "").replace("\x00", " ").lower().split())


def _canonical_preset(value: object) -> Optional[str]:
    match = re.search(r"(?:speed|spd)\s*([1-4])", str(value or ""), re.I)
    return f"speed{match.group(1)}" if match else None


def _page_key(value: object) -> str:
    text = _normalize(value)
    match = _FILTER_SPEED_RE.match(text)
    if match:
        return f"filter_speed{match.group(1)}"
    if text == "settings menu":
        return "settings_menu"
    if text == "timers menu":
        return "timers_menu"
    if text == "diagnostic menu":
        return "diagnostic_menu"
    if text == "configuration menu-locked":
        return "configuration_menu"
    if text == "default menu":
        return "default_menu"
    if text.startswith("spa heater1"):
        return "spa_heater"
    if text.startswith("pool heater1"):
        return "pool_heater"
    if text.startswith("vsp speed settings"):
        return "vsp_settings"
    if text.startswith("super chlorinate"):
        return "super_chlorinate"
    return text


class VspDriver:
    """Edits the active PL-PLUS VSP preset for a short, reversible lease."""

    def __init__(
        self,
        panel: object,
        *,
        enabled: bool = False,
        enable_file: Optional[str] = None,
        rollback_file: Optional[str] = ".vsp-rollback.json",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        default_lease_seconds: float = 60.0,
        max_lease_seconds: float = 15 * 60,
        prime_seconds: float = 3 * 60,
        prime_off_threshold_seconds: float = 30.0,
        poll_interval_seconds: float = 0.1,
        key_timeout_seconds: float = 6.0,
        key_retries: int = 3,
        key_settle_seconds: float = 0.75,
        key_sender: Optional[Callable[[object], None]] = None,
        display_reader: Optional[Callable[[], object]] = None,
        menu_cache_reader: Optional[Callable[[], dict]] = None,
    ):
        self._panel = panel
        self._enabled = bool(enabled)
        self._enable_file = str(enable_file) if enable_file else None
        self._rollback_file = str(rollback_file) if rollback_file else None
        self._clock = clock
        self._sleep = sleep
        self._default_lease_seconds = float(default_lease_seconds)
        self._max_lease_seconds = float(max_lease_seconds)
        self._prime_seconds = float(prime_seconds)
        self._prime_off_threshold_seconds = float(prime_off_threshold_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._key_timeout_seconds = float(key_timeout_seconds)
        self._key_retries = int(key_retries)
        self._key_settle_seconds = float(key_settle_seconds)
        self._key_sender = key_sender or getattr(panel, "send_key")
        self._display_reader = display_reader or (lambda: {"lines": [""]})
        self._menu_cache_reader = menu_cache_reader or (lambda: {})

        self._lock = Lock()
        self._operation_lock = Lock()
        self._cancel = Event()
        self._state = PanelPumpState()
        self._filter_off_since: Optional[float] = None
        self._prime_until: Optional[float] = None
        self._operation_id: Optional[str] = None
        self._phase = "idle"
        self._target_pct: Optional[int] = None
        self._target_name: Optional[str] = None
        self._edited_preset: Optional[str] = None
        self._original_pct: Optional[int] = None
        self._lease_expires_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._worker: Optional[Thread] = None

    def _is_enabled_locked(self) -> bool:
        return self._enabled or bool(self._enable_file and os.path.isfile(self._enable_file))

    def observe(self, state: PanelPumpState) -> None:
        now = self._clock() if state.observed_at is None else float(state.observed_at)
        with self._lock:
            previous_filter = self._state.filter_on
            self._state = PanelPumpState(
                requested_speed_pct=state.requested_speed_pct,
                pump_power_w=state.pump_power_w,
                filter_on=state.filter_on,
                service_mode=state.service_mode,
                observed_at=now,
            )
            if state.filter_on is False:
                if previous_filter is not False or self._filter_off_since is None:
                    self._filter_off_since = now
                self._prime_until = None
            elif state.filter_on is True and previous_filter is False:
                off_since = self._filter_off_since
                self._filter_off_since = None
                if off_since is not None and now - off_since > self._prime_off_threshold_seconds:
                    self._prime_until = now + self._prime_seconds

    def _assert_interlocks_locked(self, now: float) -> None:
        if not self._is_enabled_locked():
            raise VspDisabledError("no-power-cycle VSP driver is disabled pending commissioning")
        self._assert_hardware_interlocks_locked(now)

    def _assert_hardware_interlocks_locked(self, now: float) -> None:
        if self._state.service_mode is True:
            raise VspInterlockError("hardware Service mode is active")
        if self._state.filter_on is not True:
            raise VspInterlockError("filter pump is not confirmed on; refusing to change speed")
        if self._prime_until is not None and now < self._prime_until:
            remaining = max(0, int(round(self._prime_until - now)))
            raise VspInterlockError(f"PL-PLUS priming window is active for approximately {remaining}s")

    def request_preset(
        self,
        preset: str,
        *,
        source: str = "manual",
        lease_seconds: Optional[float] = None,
    ) -> dict:
        name = _canonical_preset(preset)
        if name not in PRESET_SPEEDS:
            raise ValueError("pump preset must be Speed 1, Speed 2, Speed 3, or Speed 4")
        duration = self._default_lease_seconds if lease_seconds is None else float(lease_seconds)
        if duration <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        if duration > self._max_lease_seconds:
            raise ValueError(f"lease_seconds cannot exceed {self._max_lease_seconds:g}")

        with self._lock:
            now = self._clock()
            self._assert_interlocks_locked(now)
            if self._worker is not None and self._worker.is_alive():
                if self._phase == "holding" and self._target_name == name:
                    self._lease_expires_at = max(
                        self._lease_expires_at or now,
                        now + duration,
                    )
                    renew_existing = True
                else:
                    raise VspBusyError("a VSP menu operation is already active")
            else:
                renew_existing = False
            if renew_existing:
                operation_id = None
            else:
                operation_id = uuid.uuid4().hex
            if self._rollback_pending():
                if not renew_existing:
                    raise VspBusyError("a persisted VSP rollback must complete before a new request")
            if not renew_existing:
                self._operation_id = operation_id
                self._phase = "queued"
                self._target_name = name
                self._target_pct = PRESET_SPEEDS[name]
                self._edited_preset = None
                self._original_pct = None
                self._lease_expires_at = None
                self._last_error = None
                self._cancel.clear()
                worker = Thread(
                    target=self._run_lease,
                    args=(self._target_pct, duration, str(source or "manual")),
                    name=f"plplus-vsp-{operation_id[:8]}",
                    daemon=True,
                )
                self._worker = worker
                worker.start()
        return self.status()

    def clear_target(self) -> dict:
        self._cancel.set()
        return self.status()

    def tick(self) -> bool:
        with self._lock:
            enabled = self._is_enabled_locked()
            active = self._worker is not None and self._worker.is_alive()
        if active and not enabled:
            self._cancel.set()
        if active or not self._rollback_pending():
            return False

        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            try:
                self._assert_hardware_interlocks_locked(self._clock())
            except VspInterlockError as exc:
                self._last_error = f"rollback pending: {exc}"
                return False
            operation_id = uuid.uuid4().hex
            self._operation_id = operation_id
            self._phase = "recovery_queued"
            self._last_error = None
            worker = Thread(
                target=self._run_recovery,
                name=f"plplus-vsp-recover-{operation_id[:8]}",
                daemon=True,
            )
            self._worker = worker
            worker.start()
        return True

    def is_busy(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def is_menu_busy(self) -> bool:
        """Return whether a manual keypress could collide with VSP menu work.

        A scheduled lease remains busy for its entire holding period, but the
        driver has already returned PL-PLUS to the Default Menu by then. Raw
        navigation is safe while holding and unsafe during every other live
        worker phase.
        """
        with self._lock:
            active = self._worker is not None and self._worker.is_alive()
            return active and self._phase != "holding"

    def _line(self) -> str:
        value = self._display_reader()
        if isinstance(value, dict):
            lines = value.get("lines") or []
            return str(lines[0]) if lines else ""
        return str(value or "")

    def _wait_for(self, predicate: Callable[[str], bool], timeout: Optional[float] = None) -> str:
        deadline = self._clock() + (self._key_timeout_seconds if timeout is None else timeout)
        last = self._line()
        while self._clock() < deadline:
            self._check_runtime_interlocks()
            last = self._line()
            if predicate(last):
                return last
            self._sleep(self._poll_interval_seconds)
        raise VspError(f"timed out waiting for PL-PLUS display (last={last!r})")

    def _press_until(
        self,
        key: object,
        predicate: Callable[[str], bool],
        label: str,
        *,
        safe_page: Optional[str] = None,
    ) -> str:
        last_error: Optional[Exception] = None
        for _attempt in range(self._key_retries):
            self._check_runtime_interlocks()
            if safe_page is not None and _page_key(self._line()) != safe_page:
                raise VspError(
                    f"refusing {getattr(key, 'name', key)} on unexpected page "
                    f"{self._line()!r}; expected {safe_page}"
                )
            self._key_sender(key)
            try:
                result = self._wait_for(predicate)
                self._sleep(self._key_settle_seconds)
                return result
            except VspError as exc:
                last_error = exc
                if predicate(self._line()):
                    result = self._line()
                    self._sleep(self._key_settle_seconds)
                    return result
                if safe_page is not None and _page_key(self._line()) != safe_page:
                    raise VspError(
                        f"keypress {getattr(key, 'name', key)} left expected page "
                        f"without reaching {label} (current={self._line()!r})"
                    ) from exc
        raise VspError(f"keypress {getattr(key, 'name', key)} did not reach {label}: {last_error}")

    def _check_runtime_interlocks(self) -> None:
        with self._lock:
            if self._state.service_mode is True:
                raise VspInterlockError("hardware Service mode became active")
            if self._state.filter_on is not True:
                raise VspInterlockError("filter pump turned off during VSP operation")

    def _active_preset(self) -> str:
        cache = self._menu_cache_reader() or {}
        item = (cache.get("values") or {}).get("pumpSpeedName") or {}
        if item.get("fresh") is False:
            raise VspError("cached pump preset is stale")
        preset = _canonical_preset(item.get("value"))
        if preset is None:
            with self._lock:
                requested = self._state.requested_speed_pct
            matches = [name for name, percent in PRESET_SPEEDS.items() if percent == requested]
            if len(matches) == 1:
                preset = matches[0]
        if preset is None:
            raise VspError("current PL-PLUS pump preset is unknown")
        return preset

    def _navigate_to_settings(self) -> None:
        top_level = {
            "settings_menu",
            "timers_menu",
            "diagnostic_menu",
            "configuration_menu",
            "default_menu",
        }
        for _ in range(6):
            if _page_key(self._line()) == "settings_menu":
                return
            previous = _page_key(self._line())
            self._press_until(
                Keys.MENU,
                lambda line, old=previous: _page_key(line) in top_level and _page_key(line) != old,
                "a top-level menu page",
            )
        raise VspError("could not reach Settings Menu")

    def _navigate_to_preset(self, preset: str) -> int:
        self._navigate_to_settings()
        for source, target in (
            ("settings_menu", "spa_heater"),
            ("spa_heater", "pool_heater"),
            ("pool_heater", "vsp_settings"),
        ):
            self._press_until(
                Keys.RIGHT,
                lambda line, expected=target: _page_key(line) == expected,
                target,
                safe_page=source,
            )

        self._press_until(
            Keys.PLUS,
            lambda line: _page_key(line) == "filter_speed1",
            "Filter Speed1",
            safe_page="vsp_settings",
        )
        target_number = int(preset[-1])
        for expected_number in range(2, target_number + 1):
            self._press_until(
                Keys.RIGHT,
                lambda line, n=expected_number: bool(
                    (match := _FILTER_SPEED_RE.match(_normalize(line))) and int(match.group(1)) == n
                ),
                f"Filter Speed{expected_number}",
                safe_page=f"filter_speed{expected_number - 1}",
            )
        line = self._wait_for(
            lambda value: bool(
                (match := _FILTER_SPEED_RE.match(_normalize(value)))
                and int(match.group(1)) == target_number
                and match.group(2) is not None
            ),
            timeout=5.0,
        )
        match = _FILTER_SPEED_RE.match(_normalize(line))
        assert match is not None and match.group(2) is not None
        return int(match.group(2))

    def _adjust_current_preset(
        self,
        preset: str,
        current_pct: int,
        target_pct: int,
        *,
        verify_request: bool,
    ) -> None:
        if (target_pct - current_pct) % 5 != 0:
            raise VspError(f"target {target_pct}% is not reachable from {current_pct}% in 5% steps")
        key = Keys.PLUS if target_pct > current_pct else Keys.MINUS
        value = current_pct
        while value != target_pct:
            value += 5 if key == Keys.PLUS else -5
            expected = value
            self._press_until(
                key,
                lambda line, pct=expected, number=int(preset[-1]): bool(
                    (match := _FILTER_SPEED_RE.match(_normalize(line)))
                    and int(match.group(1)) == number
                    and match.group(2) is not None
                    and int(match.group(2)) == pct
                ),
                f"{preset} at {expected}%",
                safe_page=f"filter_speed{int(preset[-1])}",
            )
        if verify_request:
            self._wait_for(lambda _line: self._state.requested_speed_pct == target_pct, timeout=10.0)

    def _set_preset_percent(self, preset: str, target_pct: int, *, verify_request: bool) -> int:
        current_pct = self._navigate_to_preset(preset)
        self._adjust_current_preset(
            preset,
            current_pct,
            target_pct,
            verify_request=verify_request,
        )
        return current_pct

    def _rollback_pending(self) -> bool:
        return bool(self._rollback_file and os.path.isfile(self._rollback_file))

    def _write_rollback(self, preset: str, original_pct: int, target_pct: int) -> None:
        if not self._rollback_file:
            return
        payload = {
            "preset": preset,
            "original_pct": original_pct,
            "target_pct": target_pct,
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        temp_path = f"{self._rollback_file}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self._rollback_file)

    def _read_rollback(self) -> dict:
        if not self._rollback_file:
            raise VspError("rollback journal is not configured")
        with open(self._rollback_file, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        preset = _canonical_preset(payload.get("preset"))
        original_pct = payload.get("original_pct")
        if preset is None or not isinstance(original_pct, int):
            raise VspError("persisted VSP rollback journal is invalid")
        return {**payload, "preset": preset, "original_pct": original_pct}

    def _clear_rollback(self) -> None:
        if not self._rollback_file:
            return
        try:
            os.unlink(self._rollback_file)
        except FileNotFoundError:
            pass

    def _return_to_default(self) -> None:
        # Navigation-only cleanup is safe even after a control interlock trips.
        top_level = {
            "settings_menu",
            "timers_menu",
            "diagnostic_menu",
            "configuration_menu",
            "default_menu",
        }
        for _ in range(7):
            previous = _page_key(self._line())
            if previous == "default_menu":
                return
            self._key_sender(Keys.MENU)
            deadline = self._clock() + self._key_timeout_seconds
            while self._clock() < deadline:
                current = _page_key(self._line())
                if current in top_level and current != previous:
                    break
                self._sleep(self._poll_interval_seconds)

    def _run_lease(self, target_pct: int, duration: float, source: str) -> None:
        del source  # retained in the API contract for later priority integration
        active_preset: Optional[str] = None
        original_pct: Optional[int] = None
        target_applied = False
        with self._operation_lock:
            try:
                with self._lock:
                    self._phase = "applying"
                active_preset = self._active_preset()
                original_pct = self._navigate_to_preset(active_preset)
                if original_pct != target_pct:
                    self._write_rollback(active_preset, original_pct, target_pct)
                    target_applied = True
                self._adjust_current_preset(
                    active_preset,
                    original_pct,
                    target_pct,
                    verify_request=True,
                )
                with self._lock:
                    self._edited_preset = active_preset
                    self._original_pct = original_pct
                    self._phase = "returning_to_default"
                    self._lease_expires_at = self._clock() + duration
                self._return_to_default()
                with self._lock:
                    self._phase = "holding"

                while not self._cancel.wait(min(0.25, duration)):
                    with self._lock:
                        expires = self._lease_expires_at
                    if expires is None or self._clock() >= expires:
                        break
                    self._check_runtime_interlocks()

                if original_pct != target_pct:
                    with self._lock:
                        self._phase = "restoring"
                    self._set_preset_percent(active_preset, original_pct, verify_request=True)
                    self._clear_rollback()
                with self._lock:
                    self._phase = "complete"
                    self._last_error = None
            except Exception as exc:
                logger.exception("VSP menu operation failed")
                with self._lock:
                    self._phase = "failed"
                    self._last_error = str(exc)
                if target_applied and active_preset and original_pct is not None and original_pct != target_pct:
                    try:
                        self._set_preset_percent(active_preset, original_pct, verify_request=True)
                        self._clear_rollback()
                    except Exception:
                        logger.exception("VSP rollback failed")
            finally:
                try:
                    self._return_to_default()
                except Exception:
                    logger.exception("Failed to return PL-PLUS to Default Menu")
                with self._lock:
                    self._target_pct = None
                    self._target_name = None
                    self._lease_expires_at = None
                    self._cancel.clear()

    def _run_recovery(self) -> None:
        with self._operation_lock:
            try:
                with self._lock:
                    self._phase = "recovering"
                rollback = self._read_rollback()
                active = self._active_preset()
                self._set_preset_percent(
                    rollback["preset"],
                    rollback["original_pct"],
                    verify_request=active == rollback["preset"],
                )
                self._clear_rollback()
                with self._lock:
                    self._phase = "recovered"
                    self._last_error = None
            except Exception as exc:
                logger.exception("Persisted VSP rollback recovery failed")
                with self._lock:
                    self._phase = "recovery_failed"
                    self._last_error = str(exc)
            finally:
                try:
                    self._return_to_default()
                except Exception:
                    logger.exception("Failed to return PL-PLUS to Default Menu after recovery")

    def status(self) -> dict:
        now = self._clock()
        with self._lock:
            lease_remaining = None
            if self._lease_expires_at is not None:
                lease_remaining = max(0.0, self._lease_expires_at - now)
            prime_remaining = None
            if self._prime_until is not None and now < self._prime_until:
                prime_remaining = max(0.0, self._prime_until - now)
            return {
                "enabled": self._is_enabled_locked(),
                "enable_file": self._enable_file,
                "rollback_file": self._rollback_file,
                "rollback_pending": self._rollback_pending(),
                "operation_id": self._operation_id,
                "phase": self._phase,
                "busy": self._worker is not None and self._worker.is_alive(),
                "target_pct": self._target_pct,
                "target_name": self._target_name,
                "edited_preset": self._edited_preset,
                "original_pct": self._original_pct,
                "lease_remaining_sec": lease_remaining,
                "requested_speed_pct": self._state.requested_speed_pct,
                "pump_power_w": self._state.pump_power_w,
                "filter_on": self._state.filter_on,
                "service_mode": self._state.service_mode,
                "prime_remaining_sec": prime_remaining,
                "verified": self._phase == "holding" and self._state.requested_speed_pct == self._target_pct,
                "last_error": self._last_error,
            }

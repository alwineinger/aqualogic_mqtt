"""Query and set PL-PLUS Pool/Spa Heater1 target temperatures."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import time
import uuid
from threading import Lock, Thread
from typing import Callable, Optional

from aqualogic.keys import Keys
from aqualogic.states import States


MIN_TARGET_F = 65
MAX_TARGET_F = 104
_TARGET_RE = re.compile(
    r"^(spa|pool)\s+heater1\s+(?:(?:manual|auto)\s+)?(?:(\d{2,3})\s*(?:°\s*)?f|off)\b",
    re.I,
)


class HeaterTargetError(RuntimeError):
    pass


class HeaterTargetBusyError(HeaterTargetError):
    pass


def _normalize(value: object) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())


def parse_heater_target(line: object) -> tuple[str, Optional[int]]:
    text = _normalize(line)
    match = _TARGET_RE.match(text)
    if not match:
        raise ValueError(f"unrecognized PL-PLUS heater target: {line!r}")
    body, number = match.groups()
    return body.lower(), int(number) if number is not None else None


def _page(line: object) -> str:
    text = _normalize(line).lower()
    if text == "settings menu":
        return "settings"
    if text == "default menu":
        return "default"
    # The selected value blinks off periodically, leaving only the stable
    # setting label. Page identity must not depend on the value being visible.
    if text.startswith("spa heater1"):
        return "spa_heater"
    if text.startswith("pool heater1"):
        return "pool_heater"
    try:
        body, _target = parse_heater_target(line)
        return f"{body}_heater"
    except ValueError:
        pass
    if text.endswith("menu") or text.endswith("menu-locked"):
        return "top"
    return text


class HeaterTargetDriver:
    def __init__(
        self,
        panel: object,
        *,
        key_sender: Optional[Callable[[object], None]] = None,
        display_reader: Optional[Callable[[], object]] = None,
        service_mode_reader: Optional[Callable[[], bool]] = None,
        state_file: Optional[str] = ".heater-target-state.json",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = 0.1,
        key_timeout_seconds: float = 6.0,
        key_settle_seconds: float = 0.75,
    ):
        self._panel = panel
        self._key_sender = key_sender or getattr(panel, "send_key")
        self._display_reader = display_reader or (lambda: {"lines": [""]})
        self._service_mode_reader = service_mode_reader
        self._state_file = str(state_file) if state_file else None
        self._clock = clock
        self._sleep = sleep
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._key_timeout_seconds = float(key_timeout_seconds)
        self._key_settle_seconds = float(key_settle_seconds)
        self._lock = Lock()
        self._worker: Optional[Thread] = None
        self._operation_id: Optional[str] = None
        self._phase = "idle"
        self._target_body: Optional[str] = None
        self._target_f: Optional[int] = None
        self._targets: dict[str, Optional[int]] = {"pool": None, "spa": None}
        self._observed_at_utc: Optional[str] = None
        self._last_error: Optional[str] = None
        self._load()

    def _load(self) -> None:
        if not self._state_file or not os.path.isfile(self._state_file):
            return
        try:
            with open(self._state_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            targets = payload.get("targets") or {}
            for body in ("pool", "spa"):
                value = targets.get(body)
                self._targets[body] = value if isinstance(value, int) else None
            self._observed_at_utc = payload.get("observed_at_utc")
        except Exception as exc:
            self._last_error = f"heater target state load failed: {exc}"

    def _save_locked(self) -> None:
        if not self._state_file:
            return
        payload = {
            "version": 1,
            "targets": dict(self._targets),
            "observed_at_utc": self._observed_at_utc,
        }
        parent = os.path.dirname(os.path.abspath(self._state_file))
        os.makedirs(parent, exist_ok=True)
        temp_path = f"{self._state_file}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self._state_file)

    def is_busy(self) -> bool:
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def _assert_available(self) -> None:
        try:
            service_mode = (
                self._service_mode_reader()
                if self._service_mode_reader is not None
                else self._panel.get_state(States.SERVICE)
            )
            if bool(service_mode):
                raise HeaterTargetError("hardware Service mode is active")
        except HeaterTargetError:
            raise
        except Exception as exc:
            raise HeaterTargetError(f"could not confirm PL-PLUS Service mode: {exc}") from exc

    def request_refresh(self) -> dict:
        return self._start(None, None)

    def request_set(self, body: str, target_f: int) -> dict:
        name = str(body or "").strip().lower()
        if name not in ("pool", "spa"):
            raise ValueError("heater body must be pool or spa")
        if isinstance(target_f, bool) or not isinstance(target_f, int):
            raise ValueError("heater target must be an integer Fahrenheit value")
        if target_f < MIN_TARGET_F or target_f > MAX_TARGET_F:
            raise ValueError(f"heater target must be between {MIN_TARGET_F}F and {MAX_TARGET_F}F")
        return self._start(name, target_f)

    def _start(self, body: Optional[str], target_f: Optional[int]) -> dict:
        self._assert_available()
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise HeaterTargetBusyError("a heater target menu operation is already active")
            self._operation_id = uuid.uuid4().hex
            self._phase = "queued"
            self._target_body = body
            self._target_f = target_f
            self._last_error = None
            worker = Thread(
                target=self._run,
                args=(body, target_f),
                daemon=True,
                name=f"plplus-heater-target-{self._operation_id[:8]}",
            )
            self._worker = worker
            worker.start()
        return self.status()

    def _line(self) -> str:
        value = self._display_reader()
        if isinstance(value, dict):
            lines = value.get("lines") or []
            return str(lines[0]) if lines else ""
        return str(value or "")

    def _wait_for(self, predicate: Callable[[str], bool]) -> str:
        deadline = self._clock() + self._key_timeout_seconds
        last = self._line()
        while self._clock() < deadline:
            self._assert_available()
            last = self._line()
            try:
                if predicate(last):
                    return last
            except (ValueError, IndexError):
                pass
            self._sleep(self._poll_interval_seconds)
        raise HeaterTargetError(f"timed out waiting for PL-PLUS display (last={last!r})")

    def _press(
        self,
        key: object,
        predicate: Callable[[str], bool],
        *,
        safe_page: Optional[str] = None,
    ) -> str:
        if safe_page is not None and _page(self._line()) != safe_page:
            raise HeaterTargetError(
                f"refusing {getattr(key, 'name', key)} on unexpected page {self._line()!r}"
            )
        self._key_sender(key)
        result = self._wait_for(predicate)
        self._sleep(self._key_settle_seconds)
        return result

    def _navigate_spa(self) -> None:
        for _ in range(7):
            if _page(self._line()) == "settings":
                break
            previous = _page(self._line())
            self._press(Keys.MENU, lambda line, old=previous: _page(line) != old)
        else:
            raise HeaterTargetError("could not reach Settings Menu")
        for _ in range(5):
            if _page(self._line()) == "spa_heater":
                return
            previous = _page(self._line())
            self._press(Keys.RIGHT, lambda line, old=previous: _page(line) != old)
        raise HeaterTargetError("could not reach Spa Heater1 setting")

    def _read_target(self, body: str) -> Optional[int]:
        line = self._wait_for(lambda value: parse_heater_target(value)[0] == body)
        parsed_body, target = parse_heater_target(line)
        if parsed_body != body:
            raise HeaterTargetError(f"expected {body} heater page, got {line!r}")
        return target

    def _adjust_target(self, body: str, current: Optional[int], target: int) -> int:
        page = f"{body}_heater"
        if current is None:
            self._press(
                Keys.PLUS,
                lambda line: parse_heater_target(line)[0] == body
                and parse_heater_target(line)[1] is not None,
                safe_page=page,
            )
            current = self._read_target(body)
        if current is None:
            raise HeaterTargetError(f"could not move {body} heater target out of Off")
        key = Keys.PLUS if target > current else Keys.MINUS
        while current != target:
            expected = current + (1 if key == Keys.PLUS else -1)
            self._press(
                key,
                lambda line, want=expected: parse_heater_target(line) == (body, want),
                safe_page=page,
            )
            current = expected
        return current

    def _return_default(self) -> None:
        for _ in range(8):
            self._assert_available()
            if _page(self._line()) == "default":
                return
            previous = _page(self._line())
            self._key_sender(Keys.MENU)
            try:
                self._wait_for(lambda line, old=previous: _page(line) != old)
            except HeaterTargetError:
                pass

    def _run(self, body: Optional[str], target_f: Optional[int]) -> None:
        try:
            with self._lock:
                self._phase = "reading"
            self._navigate_spa()
            spa = self._read_target("spa")
            if body == "spa" and target_f is not None:
                with self._lock:
                    self._phase = "setting_spa"
                spa = self._adjust_target("spa", spa, target_f)

            self._press(
                Keys.RIGHT,
                lambda line: _page(line) == "pool_heater",
                safe_page="spa_heater",
            )
            pool = self._read_target("pool")
            if body == "pool" and target_f is not None:
                with self._lock:
                    self._phase = "setting_pool"
                pool = self._adjust_target("pool", pool, target_f)

            with self._lock:
                self._targets = {"pool": pool, "spa": spa}
                self._observed_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                self._phase = "returning_to_default"
                self._save_locked()
            self._return_default()
            with self._lock:
                self._phase = "complete"
                self._last_error = None
        except Exception as exc:
            with self._lock:
                self._phase = "failed"
                self._last_error = str(exc)
        finally:
            try:
                self._return_default()
            except Exception:
                pass
            with self._lock:
                self._target_body = None
                self._target_f = None

    def status(self) -> dict:
        with self._lock:
            return {
                "available": True,
                "busy": self._worker is not None and self._worker.is_alive(),
                "operation_id": self._operation_id,
                "phase": self._phase,
                "target_body": self._target_body,
                "target_f": self._target_f,
                "targets": dict(self._targets),
                "observed_at_utc": self._observed_at_utc,
                "last_error": self._last_error,
                "minimum_f": MIN_TARGET_F,
                "maximum_f": MAX_TARGET_F,
            }

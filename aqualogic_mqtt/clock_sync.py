"""Weekly PL-PLUS clock comparison and guarded Settings-menu synchronization."""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
import re
import time
from threading import Lock, Thread
from typing import Callable, Optional

from aqualogic.keys import Keys

from .automation import LOCAL_TIMEZONE, format_utc, parse_utc, utc_now


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
CLOCK_RE = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"(\d{1,2})(?:\s*:\s*|\s+)(\d{2})([AP])\b",
    re.I,
)
TIME_RE = re.compile(r"\b(\d{1,2})\s*:\s*(\d{2})([AP])\b", re.I)


class ClockSyncError(RuntimeError):
    pass


def parse_controller_clock(line: object, reference: datetime) -> datetime:
    match = CLOCK_RE.search(" ".join(str(line or "").replace("\x00", " ").split()))
    if not match:
        raise ValueError(f"unrecognized PL-PLUS clock: {line!r}")
    weekday_name, hour_text, minute_text, meridiem = match.groups()
    hour = int(hour_text) % 12
    if meridiem.upper() == "P":
        hour += 12
    minute = int(minute_text)
    local_reference = parse_utc(reference).astimezone(LOCAL_TIMEZONE)
    target_weekday = next(i for i, name in enumerate(WEEKDAYS) if name.lower() == weekday_name.lower())
    candidates = []
    for delta in range(-3, 4):
        date = (local_reference + timedelta(days=delta)).date()
        if date.weekday() == target_weekday:
            candidates.append(datetime.combine(date, datetime.min.time(), LOCAL_TIMEZONE).replace(
                hour=hour, minute=minute
            ))
    if not candidates:
        raise ValueError(f"could not place PL-PLUS weekday near {local_reference.date()}")
    return min(candidates, key=lambda value: abs((value - local_reference).total_seconds()))


def clock_difference_minutes(line: object, reference: datetime) -> int:
    controller = parse_controller_clock(line, reference)
    local = parse_utc(reference).astimezone(LOCAL_TIMEZONE).replace(second=0, microsecond=0)
    return int(round((controller - local).total_seconds() / 60))


def display_weekday(line: object) -> int:
    text = " ".join(str(line or "").replace("\x00", " ").split()).lower()
    for index, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name.lower()}\b", text):
            return index
    raise ValueError(f"weekday is currently blank on PL-PLUS clock page: {line!r}")


def display_hour_minute(line: object) -> tuple[int, int]:
    match = TIME_RE.search(" ".join(str(line or "").replace("\x00", " ").split()))
    if not match:
        raise ValueError(f"time is currently blank on PL-PLUS clock page: {line!r}")
    hour_text, minute_text, meridiem = match.groups()
    hour = int(hour_text) % 12
    if meridiem.upper() == "P":
        hour += 12
    return hour, int(minute_text)


class ClockSyncDriver:
    def __init__(
        self,
        *,
        key_sender: Callable[[object], None],
        display_reader: Callable[[], dict],
        menu_cache_reader: Callable[[], dict],
        state_file: Optional[str] = ".clock-sync-state.json",
        now: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        interval: timedelta = timedelta(days=7),
        retry_interval: timedelta = timedelta(hours=1),
        threshold_minutes: int = 1,
        poll_interval_seconds: float = 0.1,
        key_settle_seconds: float = 0.75,
        field_settle_seconds: float = 1.25,
        key_timeout_seconds: float = 6.0,
    ):
        self._key_sender = key_sender
        self._display_reader = display_reader
        self._menu_cache_reader = menu_cache_reader
        self._state_file = str(state_file) if state_file else None
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep
        self._interval = interval
        self._retry_interval = retry_interval
        self._threshold_minutes = int(threshold_minutes)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._key_settle_seconds = float(key_settle_seconds)
        self._field_settle_seconds = float(field_settle_seconds)
        self._key_timeout_seconds = float(key_timeout_seconds)
        self._lock = Lock()
        self._worker: Optional[Thread] = None
        self._phase = "idle"
        self._last_check_utc: Optional[datetime] = None
        self._last_attempt_utc: Optional[datetime] = None
        self._last_sync_utc: Optional[datetime] = None
        self._last_difference_minutes: Optional[int] = None
        self._last_error: Optional[str] = None
        self._load()

    def _load(self) -> None:
        if not self._state_file or not os.path.isfile(self._state_file):
            return
        try:
            with open(self._state_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            for name in ("last_check_utc", "last_attempt_utc", "last_sync_utc"):
                value = payload.get(name)
                setattr(self, f"_{name}", parse_utc(value) if value else None)
            self._last_difference_minutes = payload.get("last_difference_minutes")
        except Exception as exc:
            self._last_error = f"clock state load failed: {exc}"

    def _save_locked(self) -> None:
        if not self._state_file:
            return
        payload = {
            "version": 1,
            "last_check_utc": format_utc(self._last_check_utc) if self._last_check_utc else None,
            "last_attempt_utc": format_utc(self._last_attempt_utc) if self._last_attempt_utc else None,
            "last_sync_utc": format_utc(self._last_sync_utc) if self._last_sync_utc else None,
            "last_difference_minutes": self._last_difference_minutes,
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

    def due(self, now: Optional[datetime] = None) -> bool:
        current = parse_utc(now or self._now())
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            if self._last_check_utc is not None:
                return current - self._last_check_utc >= self._interval
            if self._last_attempt_utc is not None:
                return current - self._last_attempt_utc >= self._retry_interval
            return True

    def _cached_line(self) -> str:
        cache = self._menu_cache_reader() or {}
        value = (cache.get("values") or {}).get("controllerClock") or {}
        if value.get("fresh") is False:
            raise ClockSyncError("cached PL-PLUS clock is stale")
        line = value.get("value") or value.get("raw")
        if not line:
            raise ClockSyncError("PL-PLUS clock has not been observed")
        return str(line)

    def check_or_start(self) -> bool:
        now = parse_utc(self._now())
        if not self.due(now):
            return False
        try:
            line = self._cached_line()
            parse_controller_clock(line, now)
            difference = clock_difference_minutes(line, now)
        except Exception as exc:
            with self._lock:
                self._last_attempt_utc = now
                self._phase = "check_failed"
                self._last_error = str(exc)
                self._save_locked()
            return False

        with self._lock:
            self._last_attempt_utc = now
            self._last_difference_minutes = difference
            if abs(difference) < self._threshold_minutes:
                self._last_check_utc = now
                self._phase = "checked"
                self._last_error = None
                self._save_locked()
                return False
            self._phase = "queued"
            self._last_error = None
            worker = Thread(
                target=self._run,
                args=(),
                daemon=True,
                name="plplus-clock-sync",
            )
            self._worker = worker
            self._save_locked()
            worker.start()
        return True

    def _line(self) -> str:
        value = self._display_reader() or {}
        lines = value.get("lines") if isinstance(value, dict) else None
        return str(lines[0] if lines else value or "")

    @staticmethod
    def _page(line: object) -> str:
        text = " ".join(str(line or "").replace("\x00", " ").lower().split())
        if text.startswith("set day and time"):
            return "clock"
        if text == "settings menu":
            return "settings"
        if text == "default menu":
            return "default"
        if text.endswith("menu") or text.endswith("menu-locked"):
            return "top"
        return text

    def _wait_for(self, predicate: Callable[[str], bool]) -> str:
        deadline = self._monotonic() + self._key_timeout_seconds
        last = self._line()
        while self._monotonic() < deadline:
            last = self._line()
            try:
                if predicate(last):
                    return last
            except (ValueError, IndexError):
                # Selected clock fields disappear during their blink-off
                # phase. Keep sampling until the value is visible again.
                pass
            self._sleep(self._poll_interval_seconds)
        raise ClockSyncError(f"timed out waiting for PL-PLUS display (last={last!r})")

    def _press(self, key: object, predicate: Callable[[str], bool], *, safe_clock: bool = False) -> str:
        if safe_clock and self._page(self._line()) != "clock":
            raise ClockSyncError(f"refusing clock edit on unexpected page {self._line()!r}")
        self._key_sender(key)
        result = self._wait_for(predicate)
        self._sleep(self._key_settle_seconds)
        return result

    def _navigate_clock(self) -> None:
        for _ in range(7):
            if self._page(self._line()) == "settings":
                break
            previous = self._page(self._line())
            self._press(Keys.MENU, lambda line, old=previous: self._page(line) != old)
        else:
            raise ClockSyncError("could not reach Settings Menu")
        for _ in range(9):
            if self._page(self._line()) == "clock":
                return
            previous = self._line()
            self._press(Keys.RIGHT, lambda line, old=previous: line != old)
        raise ClockSyncError("could not reach Set Day and Time")

    @staticmethod
    def _cyclic_step(current: int, target: int, size: int) -> tuple[object, int]:
        forward = (target - current) % size
        backward = (current - target) % size
        return (Keys.PLUS, forward) if forward <= backward else (Keys.MINUS, backward)

    def _adjust(self, current: int, target: int, size: int, reader: Callable[[str], int]) -> None:
        key, count = self._cyclic_step(current, target, size)
        value = current
        for _ in range(count):
            expected = (value + (1 if key == Keys.PLUS else -1)) % size
            self._press(key, lambda line, want=expected: reader(line) == want, safe_clock=True)
            value = expected

    @staticmethod
    def _parts(line: str, reference: datetime) -> tuple[int, int, int]:
        parsed = parse_controller_clock(line, reference)
        return parsed.weekday(), parsed.hour, parsed.minute

    def _move_clock_field(self) -> None:
        if self._page(self._line()) != "clock":
            raise ClockSyncError(f"refusing field navigation on unexpected page {self._line()!r}")
        self._key_sender(Keys.RIGHT)
        # Field selection changes only blink metadata, which this panel library
        # does not expose reliably. Allow a full blink/update cycle, then guard
        # that the transaction is still on the clock page.
        self._sleep(self._field_settle_seconds)
        if self._page(self._line()) != "clock":
            raise ClockSyncError(f"clock field navigation left page unexpectedly: {self._line()!r}")

    def _read_visible(self, reader: Callable[[str], object]) -> object:
        line = self._wait_for(lambda value: self._reader_available(reader, value))
        return reader(line)

    @staticmethod
    def _reader_available(reader: Callable[[str], object], line: str) -> bool:
        try:
            reader(line)
            return True
        except (ValueError, IndexError):
            return False

    def _local_now(self) -> datetime:
        return parse_utc(self._now()).astimezone(LOCAL_TIMEZONE)

    def _return_default(self) -> None:
        for _ in range(8):
            if self._page(self._line()) == "default":
                return
            previous = self._page(self._line())
            self._key_sender(Keys.MENU)
            try:
                self._wait_for(lambda line, old=previous: self._page(line) != old)
            except ClockSyncError:
                pass

    def _run(self) -> None:
        try:
            with self._lock:
                self._phase = "syncing"
            self._navigate_clock()
            current_day = int(self._read_visible(display_weekday))
            self._adjust(current_day, self._local_now().weekday(), 7, display_weekday)
            self._move_clock_field()
            hour_reader = lambda line: display_hour_minute(line)[0]
            current_hour = int(self._read_visible(hour_reader))
            self._adjust(current_hour, self._local_now().hour, 24, hour_reader)
            self._move_clock_field()
            minute_reader = lambda line: display_hour_minute(line)[1]
            current_minute = int(self._read_visible(minute_reader))
            local_now = self._local_now()
            if local_now.second >= 45:
                self._sleep(61 - local_now.second)
                current_minute = int(self._read_visible(minute_reader))
                local_now = self._local_now()
            self._adjust(current_minute, local_now.minute, 60, minute_reader)
            self._press(Keys.RIGHT, lambda line: self._page(line) != "clock", safe_clock=True)
            with self._lock:
                completed = parse_utc(self._now())
                self._last_check_utc = completed
                self._last_sync_utc = completed
                self._phase = "synced"
                self._last_error = None
                self._save_locked()
        except Exception as exc:
            with self._lock:
                self._phase = "failed"
                self._last_error = str(exc)
                self._save_locked()
        finally:
            try:
                self._return_default()
            except Exception:
                pass

    def status(self) -> dict:
        with self._lock:
            busy = self._worker is not None and self._worker.is_alive()
            result = {
                "busy": busy,
                "phase": self._phase,
                "threshold_minutes": self._threshold_minutes,
                "interval_days": self._interval.total_seconds() / 86400,
                "last_check_utc": format_utc(self._last_check_utc) if self._last_check_utc else None,
                "last_sync_utc": format_utc(self._last_sync_utc) if self._last_sync_utc else None,
                "last_difference_minutes": self._last_difference_minutes,
                "last_error": self._last_error,
            }
        result["due"] = False if busy else self.due(self._now())
        return result

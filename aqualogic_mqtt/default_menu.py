from __future__ import annotations

import os
import re
import time
from threading import Lock
from typing import Any, Callable, Dict, List, Optional


DEFAULT_STALE_AFTER_SEC = float(os.getenv("AQUALOGIC_DEFAULT_MENU_STALE_SEC", "45"))

STATE_CHANGING_KEYS = {
    "plus",
    "minus",
    "filter",
    "pool_spa",
    "poolspa",
    "pool_spa_toggle",
}

ROW_DEFS = [
    ("poolSpaMode", "Mode"),
    ("poolTempF", "Pool Temp"),
    ("spaTempF", "Spa Temp"),
    ("ambientF", "Air Temp"),
    ("poolChlorinatorPct", "Pool Chlorinator"),
    ("spaChlorinatorPct", "Spa Chlorinator"),
    ("saltPpm", "Salt"),
    ("heater1Status", "Heater1"),
    ("heaterRun", "Heater Output"),
    ("filterState", "Filter"),
    ("pumpSpeedPct", "Pump Speed"),
    ("pumpSpeedName", "Pump Preset"),
    ("systemMsg", "System"),
    ("controllerClock", "Controller Clock"),
]

REQUIRED_GROUPS = {
    "water_temp": ("poolTempF", "spaTempF"),
    "air_temp": ("ambientF",),
    "chlorinator": ("poolChlorinatorPct", "spaChlorinatorPct"),
    "salt": ("saltPpm",),
    "heater": ("heater1Status", "heaterRun"),
    "filter": ("filterState", "pumpSpeedPct", "pumpSpeedName"),
}

WEEKDAY_RE = re.compile(
    r"^(sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b\s*(.*)$",
    re.I,
)


def normalize_line(line: Any) -> str:
    return re.sub(r"\s+", " ", str(line or "").replace("\x00", " ").strip())


def normalize_led_name(name: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(name or "").strip().upper()).strip("_")


def number_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number.is_integer():
        return int(number)
    return number


def on_off(value: bool) -> str:
    return "On" if value else "Off"


def truthy_led(value: Any) -> Optional[bool]:
    if value is True or value is False:
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"on", "true", "1"}:
            return True
        if text in {"off", "false", "0"}:
            return False
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


class DefaultMenuCache:
    """Thread-safe cache of recently observed PL-PLUS default-menu values."""

    def __init__(
        self,
        stale_after_sec: float = DEFAULT_STALE_AFTER_SEC,
        clock: Callable[[], float] = time.time,
    ):
        self.stale_after_sec = stale_after_sec
        self._clock = clock
        self._values: Dict[str, Dict[str, Any]] = {}
        self._pages: Dict[str, Dict[str, Any]] = {}
        self._lock = Lock()
        self._updated_at: Optional[float] = None
        self._invalidated_at: Optional[float] = None
        self._invalidation_reason: Optional[str] = None
        self._last_complete_cycle_at: Optional[float] = None

    def observe_display(
        self,
        lines: Optional[List[str]],
        leds: Optional[dict] = None,
        observed_at: Optional[float] = None,
    ) -> None:
        ts = observed_at if observed_at is not None else self._clock()
        clean_lines = [normalize_line(line) for line in (lines or []) if normalize_line(line)]
        with self._lock:
            self._updated_at = ts
            if leds:
                self._observe_leds_locked(leds, ts)
            for line in clean_lines:
                self._observe_line_locked(line, ts)
            if self._is_complete_locked(ts):
                self._last_complete_cycle_at = ts

    def invalidate_for_key(self, key: str, observed_at: Optional[float] = None) -> bool:
        normalized = str(key or "").strip().lower()
        if normalized not in STATE_CHANGING_KEYS:
            return False
        ts = observed_at if observed_at is not None else self._clock()
        with self._lock:
            self._invalidated_at = ts
            self._invalidation_reason = f"key:{normalized}"
            self._last_complete_cycle_at = None
        return True

    def as_dict(self) -> dict:
        now = self._clock()
        with self._lock:
            rows = [
                self._row_for_value_locked(key, label, now)
                for key, label in ROW_DEFS
            ]
            pages = {
                key: self._with_freshness(dict(page), now)
                for key, page in sorted(self._pages.items())
            }
            missing_groups = self._missing_groups_locked(now)
            complete = not missing_groups
            return {
                "ok": True,
                "complete": complete,
                "fresh": complete,
                "stale_after_sec": self.stale_after_sec,
                "updated_at": self._updated_at,
                "invalidated_at": self._invalidated_at,
                "invalidation_reason": self._invalidation_reason,
                "last_complete_cycle_at": self._last_complete_cycle_at,
                "missing_groups": missing_groups,
                "values": {
                    key: self._with_freshness(dict(value), now)
                    for key, value in sorted(self._values.items())
                },
                "rows": rows,
                "pages": pages,
            }

    def _observe_leds_locked(self, leds: dict, ts: float) -> None:
        normalized = {
            normalize_led_name(key): truthy_led(value)
            for key, value in (leds or {}).items()
        }
        if normalized.get("SPA") is True:
            self._set_value_locked("poolSpaMode", "Mode", "spa", "Spa", None, "led:spa", ts)
        elif normalized.get("POOL") is True:
            self._set_value_locked("poolSpaMode", "Mode", "pool", "Pool", None, "led:pool", ts)

        if "FILTER" in normalized and normalized["FILTER"] is not None:
            value = normalized["FILTER"]
            self._set_value_locked("filterState", "Filter", value, on_off(value), None, "led:filter", ts)

        heater = self._first_led(normalized, ["HEATER_1", "HEATER1", "HEATER"])
        if heater is not None:
            self._set_value_locked("heaterRun", "Heater Output", heater, on_off(heater), None, "led:heater", ts)

    def _observe_line_locked(self, line: str, ts: float) -> None:
        page_key = self._page_key_for_line(line)
        self._pages[page_key] = {
            "key": page_key,
            "line": line,
            "observed_at": ts,
        }

        match = re.match(r"^(Pool|Spa|Air) Temp\s+(-?\d+(?:\.\d+)?)\s*(?:[^0-9A-Za-z]?F|F)?$", line, re.I)
        if match:
            label, value = match.group(1).lower(), number_or_none(match.group(2))
            key = {"pool": "poolTempF", "spa": "spaTempF", "air": "ambientF"}[label]
            row_label = {"pool": "Pool Temp", "spa": "Spa Temp", "air": "Air Temp"}[label]
            self._set_value_locked(key, row_label, value, f"{value}F", "F", line, ts)
            return

        match = re.match(r"^(Pool|Spa) Chlorinator\s+(\d+)\s*%$", line, re.I)
        if match:
            label, value = match.group(1).lower(), int(match.group(2))
            key = "poolChlorinatorPct" if label == "pool" else "spaChlorinatorPct"
            row_label = "Pool Chlorinator" if label == "pool" else "Spa Chlorinator"
            self._set_value_locked(key, row_label, value, f"{value}%", "%", line, ts)
            return

        match = re.match(r"^Salt Level\s+(\d+)\s*PPM$", line, re.I)
        if match:
            value = int(match.group(1))
            self._set_value_locked("saltPpm", "Salt", value, f"{value} ppm", "ppm", line, ts)
            return

        match = re.match(r"^Heater1\s+(?:(Manual|Auto)\s+)?(On|Off)$", line, re.I)
        if match:
            mode = (match.group(1) or "").strip()
            state = match.group(2).capitalize()
            display = f"{mode} {state}".strip()
            self._set_value_locked("heater1Status", "Heater1", display, display, None, line, ts)
            self._set_value_locked("heaterRun", "Heater Output", state == "On", state, None, line, ts)
            return

        match = re.match(r"^(?:Filter|VSP)\s+Speed\s+(\d+)\s*%(?:\s+(?:(?:Speed|Spd)\s*([1-4])|(?:Speed|Spd)([1-4])))?", line, re.I)
        if match:
            pct = int(match.group(1))
            speed_num = match.group(2) or match.group(3)
            speed_name = f"Speed{speed_num}" if speed_num else None
            self._set_value_locked("filterState", "Filter", True, "On", None, line, ts)
            self._set_value_locked("pumpSpeedPct", "Pump Speed", pct, f"{pct}%", "%", line, ts)
            if speed_name:
                self._set_value_locked("pumpSpeedName", "Pump Preset", speed_name, speed_name, None, line, ts)
            return

        match = re.match(r"^Filter On:?\s*Spd\s*([1-4])", line, re.I)
        if match:
            speed_name = f"Spd{match.group(1)}"
            self._set_value_locked("filterState", "Filter", True, "On", None, line, ts)
            self._set_value_locked("pumpSpeedName", "Pump Preset", speed_name, speed_name, None, line, ts)
            return

        if re.match(r"^(Pump|Filter)\s+Off$", line, re.I):
            self._set_value_locked("filterState", "Filter", False, "Off", None, line, ts)
            self._set_value_locked("pumpSpeedPct", "Pump Speed", None, "Off", "%", line, ts)
            self._set_value_locked("pumpSpeedName", "Pump Preset", "Off", "Off", None, line, ts)
            return

        match = WEEKDAY_RE.match(line)
        if match:
            display = f"{match.group(1).capitalize()} {match.group(2).strip()}".strip()
            self._set_value_locked("controllerClock", "Controller Clock", display, display, None, line, ts)
            return

        if line.lower().startswith("check system"):
            value = line[len("check system"):].strip() or "Check System"
            self._set_value_locked("systemMsg", "System", value, value, None, line, ts)
            return

        match = re.match(r"^Super Chlorinate\s+(On|Off)$", line, re.I)
        if match:
            state = match.group(1).capitalize()
            self._set_value_locked("systemMsg", "System", f"Super Chlorinate {state}", f"Super Chlorinate {state}", None, line, ts)
            return

    def _set_value_locked(
        self,
        key: str,
        label: str,
        value: Any,
        display: str,
        unit: Optional[str],
        raw: str,
        ts: float,
    ) -> None:
        self._values[key] = {
            "key": key,
            "label": label,
            "value": value,
            "unit": unit,
            "display": display,
            "raw": raw,
            "observed_at": ts,
        }

    def _row_for_value_locked(self, key: str, label: str, now: float) -> dict:
        value = self._values.get(key)
        if value is None:
            return {
                "key": key,
                "label": label,
                "value": None,
                "unit": None,
                "display": "--",
                "raw": None,
                "observed_at": None,
                "age_sec": None,
                "fresh": False,
                "stale_reason": "not_observed",
            }
        return self._with_freshness(dict(value), now)

    def _with_freshness(self, item: dict, now: float) -> dict:
        observed_at = item.get("observed_at")
        if observed_at is None:
            item["age_sec"] = None
            item["fresh"] = False
            item["stale_reason"] = "not_observed"
            return item

        age = max(0, now - float(observed_at))
        item["age_sec"] = age

        if self._invalidated_at is not None and observed_at <= self._invalidated_at:
            item["fresh"] = False
            item["stale_reason"] = self._invalidation_reason or "invalidated"
        elif age > self.stale_after_sec:
            item["fresh"] = False
            item["stale_reason"] = "stale"
        else:
            item["fresh"] = True
            item["stale_reason"] = None
        return item

    def _missing_groups_locked(self, now: float) -> List[str]:
        missing = []
        for group, keys in REQUIRED_GROUPS.items():
            if not any(self._value_is_fresh_locked(key, now) for key in keys):
                missing.append(group)
        return missing

    def _is_complete_locked(self, now: float) -> bool:
        return not self._missing_groups_locked(now)

    def _value_is_fresh_locked(self, key: str, now: float) -> bool:
        value = self._values.get(key)
        if not value:
            return False
        return self._with_freshness(dict(value), now)["fresh"]

    def _page_key_for_line(self, line: str) -> str:
        lower = line.lower()
        if re.match(r"^(pool|spa|air) temp\b", lower):
            return f"{lower.split()[0]}_temp"
        if re.match(r"^(pool|spa) chlorinator\b", lower):
            return f"{lower.split()[0]}_chlorinator"
        if lower.startswith("salt level"):
            return "salt_level"
        if lower.startswith("heater1"):
            return "heater1"
        if re.match(r"^(filter|vsp)\s+speed\b", lower) or re.match(r"^(pump|filter)\s+off$", lower):
            return "filter_speed"
        if lower.startswith("filter on"):
            return "filter_speed_change"
        if lower.startswith("check system"):
            return "check_system"
        if WEEKDAY_RE.match(line):
            return "controller_clock"
        return re.sub(r"[^a-z0-9]+", "_", lower).strip("_")[:48] or "unknown"

    @staticmethod
    def _first_led(leds: dict, keys: List[str]) -> Optional[bool]:
        for key in keys:
            if key in leds:
                return leds[key]
        return None

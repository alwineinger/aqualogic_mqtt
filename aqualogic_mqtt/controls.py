# aqualogic_mqtt/controls.py
from __future__ import annotations
import time
import logging
from typing import Callable, List, Tuple, Optional
from collections import deque
from threading import Lock
try:
    # Keys enum from swilson/aqualogic
    from aqualogic.keys import Keys
except Exception:  # keep runtime resilient if import fails during edits
    class Keys:
        MENU = 'MENU'
        LEFT = 'LEFT'
        RIGHT = 'RIGHT'
        MINUS = 'MINUS'
        PLUS = 'PLUS'
        FILTER = 'FILTER'

logger = logging.getLogger("aqualogic_mqtt.controls")

# ---- Shared display state for the Web UI ----
class DisplayState:
    def __init__(self):
        self.lines: List[str] = ["", "", "", ""]
        self.blink: List[Tuple[int, int]] = []
        self.leds: dict = {}
        self.updated_at: float = time.time()
        self._lock = Lock()

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "lines": list(self.lines),
                "blink": list(self.blink),
                "leds": dict(self.leds),
                "updated_at": self.updated_at,
            }

    def update(self, lines: Optional[List[str]], blink: Optional[List[Tuple[int, int]]], leds: Optional[dict]):
        with self._lock:
            if lines is not None:
                self.lines = (list(lines) + ["", "", "", ""])[:4]
            if blink is not None:
                self.blink = list(blink)
            if leds is not None:
                self.leds = dict(leds)
            self.updated_at = time.time()

_state = DisplayState()

def update_display(lines: Optional[List[str]], blink: Optional[List[Tuple[int, int]]], leds: Optional[dict]) -> None:
    _state.update(lines, blink, leds)

def get_display() -> dict:
    return _state.as_dict()

# Convenience for when only text is known
def ingest_display_lines(lines: List[str]) -> None:
    update_display(lines, None, None)

def _clean_lines(lines_like) -> List[str]:
    out: List[str] = []
    try:
        for s in list(lines_like)[:4]:
            out.append(str(s).replace("\x00", "").rstrip())
    except Exception:
        pass
    while len(out) < 4:
        out.append("")
    return out

# ---- Key queue + sender plumbing ----
_key_sender: Optional[Callable[[object], None]] = None
_key_q = deque()
_key_lock = Lock()

_KEY_MAP = {
    "menu": Keys.MENU,
    "left": Keys.LEFT,
    "right": Keys.RIGHT,
    "minus": Keys.MINUS,
    "plus": Keys.PLUS,
    "filter": Keys.FILTER,
}

def set_key_sender(sender: Callable[[object], None]) -> None:
    """Provide the low-level function that actually sends a key to the panel."""
    global _key_sender
    _key_sender = sender
    logger.debug("controls: key sender registered")

def enqueue_key(name: str) -> bool:
    """Queue a keypress by name (menu/left/right/minus/plus/filter)."""
    k = (name or "").strip().lower()
    if k not in _KEY_MAP:
        logger.debug(f"controls: unknown key '{name}'")
        return False
    with _key_lock:
        _key_q.append(_KEY_MAP[k])
    logger.info(f"controls: queued key {k}")
    return True

def drain_keypresses() -> None:
    """Send all queued keypresses to the panel; call this right after a panel update."""
    global _key_sender
    if _key_sender is None:
        return
    sent = 0
    while True:
        with _key_lock:
            if not _key_q:
                break
            key = _key_q.popleft()
        try:
            _key_sender(key)
            sent += 1
        except Exception as e:
            logger.debug(f"controls: send key failed: {e}")
            break
    if sent:
        logger.debug(f"controls: sent {sent} key(s)")

# ---- Optional: hook into panel display callbacks when available ----
def register_with_panel(panel: object) -> None:
    """Attach to panel display updates in whatever form the lib exposes."""
    def _push(lines):
        try:
            ingest_display_lines(_clean_lines(lines))
            logger.debug(f"controls: ingested display via callback: {_clean_lines(lines)!r}")
        except Exception as e:
            logger.debug(f"controls: display callback failed: {e}")

    # Try method callback
    try:
        m = getattr(panel, "on_display_update", None)
        if callable(m):
            logger.debug("controls: using on_display_update(handler)")
            m(_push)
            return
    except Exception:
        pass

    # Try attribute assignment
    try:
        if hasattr(panel, "on_display_update") and not callable(getattr(panel, "on_display_update")):
            logger.debug("controls: assigning on_display_update = handler")
            setattr(panel, "on_display_update", _push)
            return
    except Exception:
        pass

    # Try generic event bus
    try:
        add_listener = getattr(panel, "add_listener", None)
        if callable(add_listener):
            logger.debug("controls: using add_listener('display', handler)")
            def _listener(kind, payload):
                if kind == "display":
                    _push(payload)
            add_listener(_listener)
            return
    except Exception:
        pass

    logger.debug("controls: no compatible display callback on panel; relying on PanelManager forwarding")
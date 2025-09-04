# aqualogic_mqtt/controls.py
# -*- coding: utf-8 -*-
"""
Web/UI control adapter for Aqualogic.

This module provides:
- A simple key map (plus/minus/left/right/menu)
- A thread-safe command queue for keypresses
- A display state container (lines, blink positions, LEDs)
- Hooks for the serial worker to drain queued keypresses right after a keepalive
- A pluggable "sender" function so you can wire this to your existing
  controller without tight coupling.

Minimal integration (add these two lines where you have access to your
controller object and to your keepalive/read loop):

    from aqualogic_mqtt import controls
    controls.set_key_sender(controller.send_key)  # send_key("PLUS"|"MENU"|...)

Then, immediately *after* you detect a keepalive (or at your existing write
window), drain queued keypresses:

    controls.drain_keypresses()  # executes any queued keys via the sender

Also, whenever your parser updates the LCD/LEDs, call:

    controls.update_display(lines, blink, leds)

where:
- lines: list[str] (up to 4), each a single line of LCD text
- blink: list[tuple[int,int]] positions that should blink [(row, col), ...]
- leds: dict[str,bool] like {"filter": True, "aux1": False, ...}

Your web server (webapp.py) reads state using controls.get_display().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
import threading
import queue
import time

# Exported button names → low-level key names expected by the controller
KEYMAP: Dict[str, str] = {
    "plus": "PLUS",
    "minus": "MINUS",
    "left": "LEFT",
    "right": "RIGHT",
    "menu": "MENU",
}

# Optional: extend later when the backend supports them
# KEYMAP.update({"service": "SERVICE", "pool_spa": "POOL_SPA"})

# Thread-safe queue for keypress requests (strings in KEYMAP values)
_keypress_q: "queue.Queue[str]" = queue.Queue()

# A pluggable function that actually transmits a low-level key to the controller.
# Signature: sender(low_level_key: str) -> bool (True if accepted/sent)
_key_sender_lock = threading.Lock()
_key_sender: Optional[Callable[[str], bool]] = None


def set_key_sender(sender: Callable[[str], bool]) -> None:
    """Register the function used to actually send key codes to the controller.
    Call this once at startup when your controller is ready.
    """
    global _key_sender
    with _key_sender_lock:
        _key_sender = sender


def press(key: str) -> None:
    """Queue a UI-level keypress ("plus", "minus", "left", "right", "menu").
    Raises KeyError if the key is unknown. Non-blocking.
    """
    low = KEYMAP[key.lower()]
    _keypress_q.put_nowait(low)


def drain_keypresses(max_to_send: int = 4) -> int:
    """Send up to `max_to_send` queued keypresses via the registered sender.
    Intended to be called right after a keepalive / within the write window.
    Returns the number of keypresses successfully dispatched.
    """
    sent = 0
    global _key_sender
    with _key_sender_lock:
        sender = _key_sender
    if sender is None:
        return 0

    while sent < max_to_send:
        try:
            low = _keypress_q.get_nowait()
        except queue.Empty:
            break
        try:
            ok = sender(low)
        except Exception:
            ok = False
        if ok:
            sent += 1
        # If not ok, drop it; UI will retry on next button press if needed.
    return sent


@dataclass
class DisplayState:
    lines: List[str] = field(default_factory=lambda: ["", "", "", ""])
    blink: List[Tuple[int, int]] = field(default_factory=list)  # (row, col)
    leds: Dict[str, bool] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "lines": self.lines,
            "blink": self.blink,
            "leds": self.leds,
            "updated_at": self.updated_at,
        }


_state_lock = threading.Lock()
_state = DisplayState()


def update_display(lines: List[str], blink: Optional[List[Tuple[int, int]]] = None,
                   leds: Optional[Dict[str, bool]] = None) -> None:
    """Replace the display state from your parser/reader thread."""
    global _state
    with _state_lock:
        # Normalize to max 4 lines
        norm_lines = list(lines[:4]) + [""] * max(0, 4 - len(lines))
        _state.lines = norm_lines
        if blink is not None:
            _state.blink = blink
        if leds is not None:
            _state.leds = leds
        _state.updated_at = time.time()


def get_display() -> Dict:
    with _state_lock:
        return _state.to_dict()


# Convenience: no-op sender for testing
def _noop_sender(low: str) -> bool:  # pragma: no cover
    return True


# If you want this module to be functional for quick tests,
# uncomment the following line so queued keys don't error out:
# set_key_sender(_noop_sender)
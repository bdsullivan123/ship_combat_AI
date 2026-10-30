"""Flies the ship: the autopilot, built up a step at a time.

First step: `align`, which turns the ship to face the targeted system using
the nav compass, then stops. No throttle, no jump.

Keys go to the game as hardware scan codes (Elite ignores ordinary simulated
keypresses), and only while Elite is the active window. The binds are the
autopilot's secondary slots in the commander's Custom.4.2.binds. Right Ctrl aborts
at any time; so does switching away from the game.

The owner chose to build this knowing Frontier's terms forbid automation.
"""

from __future__ import annotations

import ctypes
import json
import time
from ctypes import wintypes
from pathlib import Path

from .flags import Flags

# Scan codes for the binds TACCOM added (see the README's autopilot section).
# Not the F-keys: on the commander's setup the game only acts on those with Ctrl held.
KEYS = {
    "yaw_left": 0x33,  # ,
    "yaw_right": 0x34,  # .
    "pitch_up": 0x27,  # ;
    "pitch_down": 0x37,  # numpad *
}
ABORT_VK = 0xA3  # Right Ctrl, unbound in the game

ALIGNED = 2.0  # degrees; close enough to jump
MAX_PULSE = 0.35  # seconds a turn key is held per step
MIN_PULSE = 0.07  # shorter taps barely turned the Fer-de-Lance in the first live test
SETTLE = 0.12  # seconds for the ship to stop turning and the HUD to catch up


class Aborted(Exception):
    pass


# -- keyboard -----------------------------------------------------------------

class _KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        # Sized for the largest member (MOUSEINPUT) so SendInput accepts the struct.
        _fields_ = [("ki", _KeyBdInput), ("pad", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x2, 0x8


class Keyboard:
    """Presses keys as scan codes. Releases everything it holds on close."""

    def __init__(self):
        self.held: set[int] = set()

    def _send(self, scan: int, up: bool) -> None:
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
        inp = _Input()
        inp.type = 1  # INPUT_KEYBOARD
        inp.ki = _KeyBdInput(0, scan, flags, 0, 0)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def down(self, scan: int) -> None:
        self._send(scan, False)
        self.held.add(scan)

    def up(self, scan: int) -> None:
        self._send(scan, True)
        self.held.discard(scan)

    def release_all(self) -> None:
        for scan in list(self.held):
            self.up(scan)


def abort_pressed() -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(ABORT_VK) & 0x8000)


# -- steering -------------------------------------------------------------------

def steer(reading) -> dict[str, float]:
    """Which turn keys to hold, and for how long, to bring the compass dot to the centre.

    Behind the ship the dot still shows the quicker way round, so the same
    directions apply, just held longer.
    """
    if reading.behind:
        # Turn hardest on the axis the dot leans toward; pitch is the faster turn.
        if abs(reading.y) >= abs(reading.x) * 0.5 or (reading.x == 0 and reading.y == 0):
            return {"pitch_up" if reading.y >= 0 else "pitch_down": MAX_PULSE}
        return {"yaw_right" if reading.x > 0 else "yaw_left": MAX_PULSE}
    keys = {}
    for axis, value, pos, neg, gain in (("y", reading.y, "pitch_up", "pitch_down", 0.30),
                                        ("x", reading.x, "yaw_right", "yaw_left", 0.55)):
        if abs(value) > 0.02:
            keys[pos if value > 0 else neg] = min(MAX_PULSE, MIN_PULSE + gain * abs(value))
    return keys


def read_status(journal_dir: Path) -> dict:
    try:
        return json.loads((journal_dir / "Status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def current_ship(journal_dir: Path) -> str | None:
    """The ship type from the newest journal's last Loadout or LoadGame, e.g. "ferdelance"."""
    from .sources import newest_journal

    path = newest_journal(journal_dir)
    ship = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"Loadout"' in line or '"LoadGame"' in line or '"ShipyardSwap"' in line:
                    event = json.loads(line)
                    ship = event.get("Ship") or event.get("ShipType") or ship
    except (OSError, TypeError, ValueError):
        pass
    return ship.lower() if ship else None


def align(journal_dir: Path, timeout: float = 90.0, say=print) -> bool:
    """Turn to face the targeted system. True once aligned; raises Aborted if stopped."""
    from .capture import ScreenGrabber, elite_window_rect
    from .compass import read_compass

    grabber, keys = ScreenGrabber(), Keyboard()
    ship = current_ship(journal_dir)
    say(f"Flying the {ship or 'unknown ship'}.")
    deadline = time.monotonic() + timeout
    steady = misses = 0
    try:
        while time.monotonic() < deadline:
            if abort_pressed():
                raise Aborted("Right Ctrl pressed")
            rect = elite_window_rect()
            if rect is None:
                raise Aborted("Elite isn't the active window")
            flags = read_status(journal_dir).get("Flags", 0)
            if flags & (Flags.DOCKED | Flags.LANDED):
                raise Aborted("docked or landed")
            frame = grabber.grab(rect)
            reading = read_compass(frame, ship) if frame is not None else None
            if reading is None:
                misses += 1
                if misses > 40:
                    raise Aborted("can't see the compass (no target selected?)")
                time.sleep(0.05)
                continue
            misses = 0
            if not reading.behind and reading.angle < ALIGNED:
                steady += 1
                if steady >= 3:
                    say(f"Aligned, {reading.angle:.1f} degrees off.")
                    return True
                time.sleep(SETTLE)
                continue
            steady = 0
            plan = steer(reading)
            say(f"{'behind' if reading.behind else 'ahead '} {reading.angle:5.1f} deg  "
                + " ".join(f"{k} {t:.2f}s" for k, t in plan.items()))
            _pulse(keys, plan)
            time.sleep(SETTLE)
        say("Couldn't align in time.")
        return False
    finally:
        keys.release_all()


def _pulse(keys: Keyboard, plan: dict[str, float]) -> None:
    """Hold each key for its time, all starting together, watching for the abort key."""
    start = time.monotonic()
    for name in plan:
        keys.down(KEYS[name])
    try:
        for name, hold in sorted(plan.items(), key=lambda kv: kv[1]):
            while time.monotonic() - start < hold:
                if abort_pressed():
                    raise Aborted("Right Ctrl pressed")
                time.sleep(0.01)
            keys.up(KEYS[name])
    finally:
        keys.release_all()

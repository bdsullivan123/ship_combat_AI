"""Screen capture of the Elite Dangerous window (Stage 2 groundwork).

FrameCollector builds a labelled data set for calibrating the HUD readers.
During combat it saves frames, and it saves one immediately whenever the
journal reports something the HUD also shows: hull damage, shields failing,
a target scan with shield and hull health. Each frame gets a JSON sidecar
with what the game files said at that moment, which is the answer key the
screen readers are checked against.

Only the Elite window is ever captured, and only while it is the active
window, so nothing else on the desktop ends up on disk.
"""

from __future__ import annotations

import ctypes
import json
import queue
import threading
import time
from dataclasses import asdict
from pathlib import Path

from .flags import Flags
from .state import ShipState

ELITE_WINDOW_TITLE = "Elite - Dangerous (CLIENT)"

# Journal events whose values are also visible on the HUD.
LABEL_EVENTS = {"HullDamage", "ShieldState", "ShipTargeted", "HeatWarning", "HeatDamage",
                "UnderAttack", "Bounty", "FighterDestroyed", "CockpitBreached"}
UNDER_ATTACK_EVERY = 5.0  # seconds; it fires constantly in a fight


def _make_dpi_aware() -> None:
    # Without this, Windows display scaling makes window coordinates lie.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def elite_window_rect() -> tuple[int, int, int, int] | None:
    """Screen rectangle of Elite's client area, or None unless Elite is the active window."""
    import win32gui

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd or win32gui.GetWindowText(hwnd) != ELITE_WINDOW_TITLE:
        return None
    _, _, width, height = win32gui.GetClientRect(hwnd)
    x, y = win32gui.ClientToScreen(hwnd, (0, 0))
    return x, y, x + width, y + height


class ScreenGrabber:
    """Grabs a screen region as a BGR numpy array. Uses DXGI (dxcam), falling back to mss."""

    def __init__(self):
        _make_dpi_aware()
        self._dxcam = None
        self._mss = None
        try:
            import dxcam

            self._dxcam = dxcam.create(output_color="BGR")
        except Exception:
            import mss

            self._mss = mss.mss()

    def grab(self, rect: tuple[int, int, int, int]):
        if self._dxcam is not None:
            for _ in range(5):
                frame = self._dxcam.grab(region=rect)
                if frame is not None:  # dxcam returns None until the screen changes
                    return frame
                time.sleep(0.01)
            return None
        import numpy as np

        left, top, right, bottom = rect
        shot = self._mss.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
        return np.asarray(shot)[:, :, :3]


def state_labels(state: ShipState) -> dict:
    return {
        "ship": state.ship,
        "hull": state.hull,
        "shields_up": state.shields_up,
        "pips": state.pips,
        "fire_group": state.fire_group,
        "flags": [f.name for f in Flags if state.flags & f],
        "in_combat": state.in_combat,
        "target": asdict(state.target) if state.target else None,
    }


class FrameCollector:
    def __init__(self, directory: Path, interval: float = 2.0, max_frames: int = 1500,
                 grabber: ScreenGrabber | None = None, window=elite_window_rect):
        self.directory = directory
        self.interval = interval
        self.max_frames = max_frames
        self.saved = 0
        self.skipped_not_focused = 0
        self._grabber = grabber
        self._window = window
        self._last_periodic = float("-inf")
        self._last_attack_frame = float("-inf")
        self._jobs: queue.Queue = queue.Queue(maxsize=8)
        self._thread = threading.Thread(target=self._run, name="taccom-capture", daemon=True)
        self._thread.start()

    # Called by the app ----------------------------------------------------

    def on_journal(self, event: dict, state: ShipState, now: float) -> None:
        name = event.get("event")
        if name not in LABEL_EVENTS:
            return
        if name == "ShipTargeted" and not event.get("TargetLocked"):
            return
        if name == "UnderAttack":
            if now - self._last_attack_frame < UNDER_ATTACK_EVERY:
                return
            self._last_attack_frame = now
        self._request(name, event, state)

    def tick(self, state: ShipState, now: float) -> None:
        fighting = state.in_combat or bool(state.flags & Flags.HARDPOINTS_DEPLOYED)
        if fighting and now - self._last_periodic >= self.interval:
            self._last_periodic = now
            self._request("periodic", None, state)

    def close(self) -> None:
        self._jobs.put(None)
        self._thread.join(timeout=5)

    # Worker ---------------------------------------------------------------

    def _request(self, reason: str, event: dict | None, state: ShipState) -> None:
        if self.saved >= self.max_frames:
            return
        labels = {"time": time.time(), "reason": reason, "event": event, "state": state_labels(state)}
        try:
            self._jobs.put_nowait(labels)
        except queue.Full:
            pass  # capture is falling behind; drop rather than delay the alerts

    def _run(self) -> None:
        import cv2

        if self._grabber is None:
            self._grabber = ScreenGrabber()
        self.directory.mkdir(parents=True, exist_ok=True)
        while True:
            labels = self._jobs.get()
            if labels is None:
                return
            rect = self._window()
            if rect is None:
                self.skipped_not_focused += 1
                continue
            frame = self._grabber.grab(rect)
            if frame is None:
                continue
            stem = f"{time.strftime('%H%M%S')}_{self.saved:04d}_{labels['reason']}"
            cv2.imwrite(str(self.directory / f"{stem}.jpg"), frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
            (self.directory / f"{stem}.json").write_text(json.dumps(labels, indent=1, default=str))
            self.saved += 1

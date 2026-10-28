"""Reads values off the cockpit HUD (Stage 2).

The HUD panels sit at fixed screen positions for a given ship, resolution and
field of view, but the cockpit shakes on boost and when hit. So each region is
padded, and OCR finds the text inside it before reading it, rather than
assuming exact pixel positions.

Calibrated from the Fer-de-Lance at 1920x1080 with the default orange HUD.
Other ships put the panels elsewhere; frames from `taccom run --capture` are
what new layouts get calibrated from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# (left, top, right, bottom) at 1920x1080. Scaled for other resolutions.
LAYOUTS = {
    "ferdelance": {
        "secondary_weapons": (395, 525, 715, 712),
        "heat": (845, 738, 975, 798),
        "hull": (1365, 845, 1485, 898),
        "target": (262, 742, 572, 852),
    },
}
BASE_SIZE = (1920, 1080)

# Small text near a region's edge is missed at native size; enlarging fixes it.
UPSCALE = {"secondary_weapons": 1.5}

COUNT = re.compile(r"^(\d{1,4})\s*/\s*(\d{1,5})$")
PERCENT = re.compile(r"(\d{1,3})\s*%")
DISTANCE = re.compile(r"^([\d.,]+)\s*(m|km|Ls)$", re.IGNORECASE)


@dataclass
class TextLine:
    text: str
    score: float
    x: int  # top-left, in full-frame pixels
    y: int


@dataclass
class Weapon:
    name: str
    clip: int
    reserve: int

    @property
    def total(self) -> int:
        return self.clip + self.reserve


@dataclass
class HudReading:
    weapons: list[Weapon] = field(default_factory=list)  # secondary fire group, top to bottom
    heat: int | None = None  # percent
    hull: int | None = None  # percent
    target_lines: list[str] = field(default_factory=list)
    target_distance: str | None = None

    def utility(self, name: str) -> Weapon | None:
        return next((w for w in self.weapons if w.name == name), None)


class HudReader:
    MIN_SCORE = 0.8

    def __init__(self, layout: str = "ferdelance", threads: int = 2):
        from rapidocr import RapidOCR

        self.regions = LAYOUTS[layout]
        # Few threads on purpose: the game needs the CPU more than we do.
        self._ocr = RapidOCR(params={
            "Global.use_cls": False,
            "Global.log_level": "warning",
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            # The default enlarges every input to 736px or more, which makes
            # small HUD crops take seconds. Cap the size instead.
            "Det.limit_type": "max",
            "Det.limit_side_len": 1280,
        })

    def read(self, frame, regions: tuple[str, ...] | None = None) -> HudReading:
        reading = HudReading()
        h, w = frame.shape[:2]
        sx, sy = w / BASE_SIZE[0], h / BASE_SIZE[1]
        for name, (x1, y1, x2, y2) in self.regions.items():
            if regions is not None and name not in regions:
                continue
            x1, y1, x2, y2 = int(x1 * sx), int(y1 * sy), int(x2 * sx), int(y2 * sy)
            lines = self._lines(frame[y1:y2, x1:x2], x1, y1, UPSCALE.get(name, 1.0))
            getattr(self, f"_parse_{name}")(lines, reading)
        return reading

    def _lines(self, crop, ox: int, oy: int, scale: float = 1.0) -> list[TextLine]:
        if scale != 1.0:
            import cv2

            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        result = self._ocr(crop)
        if result.txts is None:
            return []
        lines = [TextLine(t.strip(), float(s), int(b[0][0] / scale) + ox, int(b[0][1] / scale) + oy)
                 for b, t, s in zip(result.boxes, result.txts, result.scores) if s >= self.MIN_SCORE]
        return sorted(lines, key=lambda line: (line.y, line.x))

    # -- parsers ---------------------------------------------------------

    @staticmethod
    def _parse_secondary_weapons(lines: list[TextLine], reading: HudReading) -> None:
        # Each weapon is a name line with its "clip/reserve" count just below.
        labels = [ln for ln in lines if not COUNT.match(ln.text)]
        for ln in lines:
            m = COUNT.match(ln.text)
            if not m:
                continue
            above = [lb for lb in labels if 0 < ln.y - lb.y < 40]
            if not above:
                continue
            label = min(above, key=lambda lb: (ln.y - lb.y) + abs(ln.x - lb.x) / 4)
            name = label.text.upper().replace(" ", "")
            reading.weapons.append(Weapon(name, int(m.group(1)), int(m.group(2))))

    @staticmethod
    def _parse_heat(lines: list[TextLine], reading: HudReading) -> None:
        for ln in lines:
            m = PERCENT.search(ln.text)
            if m:
                reading.heat = int(m.group(1))
                return

    @staticmethod
    def _parse_hull(lines: list[TextLine], reading: HudReading) -> None:
        for ln in lines:
            m = PERCENT.search(ln.text)
            if m and int(m.group(1)) <= 100:
                reading.hull = int(m.group(1))
                return

    @staticmethod
    def _parse_target(lines: list[TextLine], reading: HudReading) -> None:
        for ln in lines:
            if DISTANCE.match(ln.text) and reading.target_distance is None:
                reading.target_distance = ln.text
            else:
                reading.target_lines.append(ln.text)


class HudWatcher:
    """Reads the HUD on a background thread while it is worth reading.

    The main loop calls update() with the current ship state and collects
    finished readings with readings(), so alert rules stay on one thread.
    """

    def __init__(self, interval: float = 1.5, reader: HudReader | None = None, grabber=None, window=None):
        import queue
        import threading

        from .capture import elite_window_rect

        self.interval = interval
        self._reader = reader
        self._grabber = grabber
        self._window = window or elite_window_rect
        self._layout: str | None = None
        self._results: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="taccom-hud", daemon=True)
        self._thread.start()

    @staticmethod
    def layout_for(state) -> str | None:
        """The layout to read with, or None when the combat HUD isn't showing."""
        from .flags import Flags

        if state.ship_type not in LAYOUTS or state.gui_focus != 0:
            return None
        need = Flags.IN_MAIN_SHIP | Flags.HARDPOINTS_DEPLOYED
        avoid = Flags.HUD_ANALYSIS_MODE | Flags.DOCKED | Flags.LANDED | Flags.SUPERCRUISE
        if (state.flags & need) != need or state.flags & avoid:
            return None
        return state.ship_type

    def update(self, state) -> None:
        self._layout = self.layout_for(state)

    def readings(self) -> list[HudReading]:
        out = []
        while not self._results.empty():
            out.append(self._results.get_nowait())
        return out

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def _run(self) -> None:
        import time

        from .capture import ScreenGrabber

        if self._grabber is None:
            self._grabber = ScreenGrabber()
        readers: dict[str, HudReader] = {}
        while not self._stop.wait(self.interval):
            layout = self._layout
            if layout is None:
                continue
            rect = self._window()
            if rect is None:
                continue
            frame = self._grabber.grab(rect)
            if frame is None:
                continue
            if layout not in readers:
                readers[layout] = self._reader or HudReader(layout)
            try:
                self._results.put(readers[layout].read(frame))
            except Exception as exc:  # a bad read must never take the copilot down
                print(f"[taccom] HUD read failed: {exc}")
                time.sleep(5)

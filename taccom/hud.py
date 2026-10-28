"""Reads values off the cockpit HUD (Stage 2).

The pilot's head moves: G-forces, impacts and looking around shift, tilt and
zoom the whole cockpit by up to ~250 px and ~7 degrees. So every frame is
first aligned to a reference frame of the same cockpit, using features on the
physical dashboard (ORB keypoints + RANSAC), and warped back into that pose.
Only then are the HUD regions read, with OCR finding the text inside padded
regions, because the holographic panels float a little on top of that.

When alignment can't be trusted (looking far off to the side, the dashboard
out of view), the frame is skipped rather than guessed at.

Calibrated from the Fer-de-Lance at 1920x1080 with the default orange HUD.
New cockpits get a reference frame and regions from `taccom run --capture`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

LAYOUT_DIR = Path(__file__).resolve().parent / "layouts"
BASE_SIZE = (1920, 1080)

# What a routine read covers. The weapon panel costs as much as the rest put
# together and ammo changes slowly, so it is read less often (see HudWatcher).
# The target name panel isn't read routinely: the journal already says who it is.
FAST_REGIONS = ("own_ship", "target_ship", "heat")
WEAPON_REGIONS = ("secondary_weapons",)


@dataclass(frozen=True)
class Layout:
    reference: str  # file in taccom/layouts/, 1920x1080
    dashboard: tuple[int, int, int, int]  # physical cockpit area used for alignment
    # name -> ((left, top, right, bottom), upscale), in reference coordinates
    regions: dict[str, tuple[tuple[int, int, int, int], float]]
    # reading field -> (region, (x, y) where that number's centre sits in the reference)
    numbers: dict[str, tuple[str, tuple[int, int]]]
    # Around the target hologram: shield rings and the shield number are drawn
    # in blue while the target's shields are up, and red or not at all once
    # they're down. Counting blue pixels doesn't depend on OCR.
    target_glow: tuple[int, int, int, int]


LAYOUTS = {
    "ferdelance": Layout(
        reference="ferdelance_1080p.jpg",
        dashboard=(480, 690, 1500, 1080),
        regions={
            "secondary_weapons": ((290, 455, 730, 715), 1.5),
            "own_ship": ((1240, 820, 1500, 965), 1.5),
            "target_ship": ((580, 815, 830, 950), 1.5),
            "heat": ((815, 715, 990, 790), 1.0),
            "target_panel": ((185, 700, 530, 875), 1.0),
        },
        numbers={
            "shields": ("own_ship", (1383, 885)),
            "hull": ("own_ship", (1321, 922)),
            "target_shields": ("target_ship", (675, 870)),
            "target_hull": ("target_ship", (734, 904)),
            "heat": ("heat", (885, 752)),
        },
        target_glow=(580, 790, 830, 950),
    ),
}

COUNT = re.compile(r"^(\d{1,4})\s*/\s*(\d{1,5})$")
PERCENT = re.compile(r"([0-9OoDQ]{1,3})\s*[%#]")
DISTANCE = re.compile(r"^([\d.,]+)\s*(m|km|Ls)$", re.IGNORECASE)
NUMBER_REACH = 70  # px: how far a number may sit from where the layout expects it


@dataclass
class TextLine:
    text: str
    score: float
    x: int  # top-left, in reference-frame pixels
    y: int
    cx: int = 0  # centre
    cy: int = 0


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
    heat: int | None = None  # all percentages
    shields: int | None = None
    hull: int | None = None
    target_shields: int | None = None
    target_hull: int | None = None
    target_shields_up: bool | None = None  # None: couldn't tell from this frame
    target_lines: list[str] = field(default_factory=list)
    target_distance: str | None = None
    weapons_read: bool = False  # False: the weapon panel wasn't part of this read

    def utility(self, name: str) -> Weapon | None:
        return next((w for w in self.weapons if w.name == name), None)


class Aligner:
    """Finds how far the cockpit has moved from the reference pose, and undoes it."""

    MIN_INLIERS = 30
    MAX_ROTATION = 10.0  # degrees
    SCALE_RANGE = (0.75, 1.3)

    def __init__(self, reference, dashboard: tuple[int, int, int, int]):
        import cv2
        import numpy as np

        self._cv2, self._np = cv2, np
        self.size = (reference.shape[1], reference.shape[0])
        self._orb = cv2.ORB_create(nfeatures=3000, fastThreshold=10)
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        mask = np.zeros(reference.shape[:2], np.uint8)
        x1, y1, x2, y2 = dashboard
        mask[y1:y2, x1:x2] = 255
        self._kp, self._des = self._orb.detectAndCompute(cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY), mask)
        # Where to look in a live frame: the dashboard can be anywhere in the
        # lower part of the screen, so search wide.
        self._search = np.zeros(reference.shape[:2], np.uint8)
        self._search[reference.shape[0] * 2 // 5:, :] = 255
        self.last_inliers = 0

    def align(self, frame):
        """The frame warped into the reference pose, or None if alignment isn't trustworthy."""
        cv2, np = self._cv2, self._np
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        kp, des = self._orb.detectAndCompute(gray, self._search)
        self.last_inliers = 0
        if des is None or len(kp) < self.MIN_INLIERS:
            return None
        matches = self._matcher.match(self._des, des)
        if len(matches) < self.MIN_INLIERS:
            return None
        src = np.float32([kp[m.trainIdx].pt for m in matches])
        dst = np.float32([self._kp[m.queryIdx].pt for m in matches])
        matrix, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0)
        if matrix is None:
            return None
        self.last_inliers = int(inliers.sum())
        scale = float(np.hypot(matrix[0, 0], matrix[1, 0]))
        rotation = abs(float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0]))))
        if (self.last_inliers < self.MIN_INLIERS or rotation > self.MAX_ROTATION
                or not self.SCALE_RANGE[0] <= scale <= self.SCALE_RANGE[1]):
            return None
        return cv2.warpAffine(frame, matrix, self.size)


class HudReader:
    MIN_SCORE = 0.8

    def __init__(self, layout: str = "ferdelance", threads: int = 2):
        import cv2
        from rapidocr import RapidOCR

        self.layout = LAYOUTS[layout]
        reference = cv2.imread(str(LAYOUT_DIR / self.layout.reference))
        if reference is None:
            raise FileNotFoundError(LAYOUT_DIR / self.layout.reference)
        self.aligner = Aligner(reference, self.layout.dashboard)
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

    def read(self, frame, regions: tuple[str, ...] | None = None) -> HudReading | None:
        """Read the HUD, or None when the cockpit couldn't be lined up with the reference."""
        import cv2

        if (frame.shape[1], frame.shape[0]) != BASE_SIZE:
            frame = cv2.resize(frame, BASE_SIZE, interpolation=cv2.INTER_AREA)
        aligned = self.aligner.align(frame)
        if aligned is None:
            return None
        reading = HudReading()
        found: dict[str, list[TextLine]] = {}
        for name, ((x1, y1, x2, y2), scale) in self.layout.regions.items():
            if regions is None or name in regions:
                found[name] = self._lines(aligned[y1:y2, x1:x2], x1, y1, scale)
        reading.weapons_read = "secondary_weapons" in found
        if "secondary_weapons" in found:
            self._parse_weapons(found["secondary_weapons"], reading)
        if "target_panel" in found:
            self._parse_target_panel(found["target_panel"], reading)
        self._parse_numbers(found, reading)
        if "target_ship" in found:
            reading.target_shields_up = self._target_shields_up(aligned, reading)
        return reading

    def _lines(self, crop, ox: int, oy: int, scale: float) -> list[TextLine]:
        import cv2

        if scale != 1.0:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        result = self._ocr(crop)
        if result.txts is None:
            return []
        lines = []
        for box, text, score in zip(result.boxes, result.txts, result.scores):
            if score < self.MIN_SCORE:
                continue
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            lines.append(TextLine(text.strip(), float(score),
                                  int(min(xs) / scale) + ox, int(min(ys) / scale) + oy,
                                  int(sum(xs) / 4 / scale) + ox, int(sum(ys) / 4 / scale) + oy))
        return sorted(lines, key=lambda ln: (ln.y, ln.x))

    # -- parsers ---------------------------------------------------------

    @staticmethod
    def _parse_weapons(lines: list[TextLine], reading: HudReading) -> None:
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

    def _parse_numbers(self, found: dict[str, list[TextLine]], reading: HudReading) -> None:
        # Percentages are told apart by where they sit: each goes to the
        # nearest expected position, and each text is used at most once.
        candidates = []
        for fld, (region, (ax, ay)) in self.layout.numbers.items():
            for i, ln in enumerate(found.get(region, [])):
                value = percent(ln.text)
                if value is not None and fld != "heat" and value > 100:
                    continue  # shields and hull top out at 100; "190%" is a misread "90%"
                dist = ((ln.cx - ax) ** 2 + (ln.cy - ay) ** 2) ** 0.5
                if value is not None and dist <= NUMBER_REACH:
                    candidates.append((dist, fld, (region, i), value))
        taken_fields, taken_lines = set(), set()
        for dist, fld, line_id, value in sorted(candidates):
            if fld in taken_fields or line_id in taken_lines:
                continue
            taken_fields.add(fld)
            taken_lines.add(line_id)
            setattr(reading, fld, value)

    def _target_shields_up(self, aligned, reading: HudReading) -> bool | None:
        x1, y1, x2, y2 = self.layout.target_glow
        blue = _blue_pixels(aligned[y1:y2, x1:x2])
        if blue >= SHIELD_GLOW_UP:
            return True
        # No blue could also mean there's no hologram on screen at all, so
        # "down" needs the hologram's hull number to have been readable.
        if blue <= SHIELD_GLOW_DOWN and (reading.target_hull is not None or reading.target_shields == 0):
            return False
        return None

    @staticmethod
    def _parse_target_panel(lines: list[TextLine], reading: HudReading) -> None:
        for ln in lines:
            if DISTANCE.match(ln.text) and reading.target_distance is None:
                reading.target_distance = ln.text
            else:
                reading.target_lines.append(ln.text)


SHIELD_GLOW_UP = 40  # blue pixels: shields up (measured 47 to 3,000 on real targets)
SHIELD_GLOW_DOWN = 5  # with the hologram in view, downed shields measured exactly 0


def _blue_pixels(crop) -> int:
    import cv2

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    return int(((h >= 80) & (h <= 112) & (s >= 90) & (v >= 140)).sum())


def percent(text: str) -> int | None:
    m = PERCENT.search(text)
    if not m:
        return None
    digits = m.group(1).translate(str.maketrans("OoDQ", "0000"))
    value = int(digits)
    return value if value <= 999 else None


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
        self.unaligned = 0
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

        if state.ship_type not in LAYOUTS or state.gui_focus != 0 or state.destroyed:
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
        reads = 0
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
                regions = FAST_REGIONS + (WEAPON_REGIONS if reads % 3 == 0 else ())
                reads += 1
                reading = readers[layout].read(frame, regions)
                if reading is None:
                    self.unaligned += 1  # looking away from the dashboard; skip, don't guess
                else:
                    self._results.put(reading)
            except Exception as exc:  # a bad read must never take the copilot down
                print(f"[taccom] HUD read failed: {exc}")
                time.sleep(5)

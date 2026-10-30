"""Reads the nav compass: where the targeted destination lies relative to the nose (autopilot groundwork).

The compass is the small globe left of the radar. A filled dot means the
target is ahead, a hollow ring means it's behind; the dot's offset from the
centre is the direction to steer. The compass drifts around the screen with
the pilot's head and changes colour with the light (orange, or cyan and white
near a bright star), so it is found by shape each frame: a ring of the right
size, matched against local contrast so star glare doesn't wash it out.

The dot is the one cyan thing on an orange HUD, so it is found by colour.

Calibrated from the Dolphin at 1920x1080 with the default orange HUD.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

BASE_SIZE = (1920, 1080)
SEARCH = (350, 600, 1150, 1080)  # where the compass can be, head movement included
RADII = range(22, 42, 2)  # ring radius in px; leaning in or out changes it
MIN_RING_SCORE = 0.42  # below this the compass isn't on screen (galaxy map, hyperspace, looking away)
MIN_DOT = 25  # band-pass strength of the dot in the blue channel
LIT_RING = 100  # blue level of a ring lit cyan or white by a nearby star


@dataclass(frozen=True)
class CompassReading:
    x: float  # -1 (left edge) .. 1 (right edge)
    y: float  # -1 (bottom) .. 1 (top)
    behind: bool  # hollow dot: the target is behind the ship
    centre: tuple[int, int]  # compass centre on screen
    radius: int

    @property
    def angle(self) -> float:
        """Degrees between the nose and the target: 0 dead ahead, 180 directly behind."""
        ahead = math.degrees(math.asin(min(1.0, math.hypot(self.x, self.y))))
        return 180.0 - ahead if self.behind else ahead


def _ring(radius: int):
    import cv2

    size = 2 * radius + 16
    t = np.zeros((size, size), np.float32)
    cv2.circle(t, (size // 2, size // 2), radius, 1.0, 2, cv2.LINE_AA)
    return t


_RINGS = {r: _ring(r) for r in RADII}


def find_compass(frame) -> tuple[int, int, int, float] | None:
    """(x, y, radius, score) of the compass ring in a BGR frame, or None if it isn't visible."""
    import cv2

    x0, y0, x1, y1 = _scaled(SEARCH, frame)
    v = frame[y0:y1, x0:x1].max(axis=2).astype(np.float32)
    v = v - cv2.GaussianBlur(v, (0, 0), 6)  # local contrast: glare and dashboard shading drop out
    if v.std() < 2:
        return None  # a blank screen (hyperspace tunnel, loading) matches anything
    best = None
    for r, t in _RINGS.items():
        scores = cv2.matchTemplate(v, t, cv2.TM_CCOEFF_NORMED)
        _, score, _, (lx, ly) = cv2.minMaxLoc(scores)
        if best is None or score > best[3]:
            best = (x0 + lx + t.shape[1] // 2, y0 + ly + t.shape[0] // 2, r, score)
    return best if best and best[3] >= MIN_RING_SCORE else None


def read_compass(frame) -> CompassReading | None:
    """Where the compass dot points, or None if the compass or its dot can't be seen."""
    import cv2

    found = find_compass(frame)
    if found is None:
        return None
    cx, cy, r, _ = found
    reach = r + 4  # a dot on the rim sits slightly over the ring
    x0, y0 = max(0, cx - reach - 8), max(0, cy - reach - 8)
    blue = frame[y0:cy + reach + 9, x0:cx + reach + 9, 0].astype(np.float32)
    # Dot-sized bright spots in the blue channel: the HUD's orange has little
    # blue, and a band-pass (difference of Gaussians) favours a round dot over
    # the thin lines of the ring and grid, even when those turn cyan.
    spots = cv2.GaussianBlur(blue, (0, 0), 1.5) - cv2.GaussianBlur(blue, (0, 0), 5)
    yy, xx = np.ogrid[:spots.shape[0], :spots.shape[1]]
    dist = np.sqrt((xx + x0 - cx) ** 2 + (yy + y0 - cy) ** 2)
    if np.median(blue[np.abs(dist - r) <= 3]) > LIT_RING:
        reach = r - 4  # a lit ring's glow outshines a dot on the rim; trust only the inside
    spots[dist > reach] = 0
    _, peak, _, (px, py) = cv2.minMaxLoc(spots)
    if peak < MIN_DOT:
        return None
    # The blob around the peak: a filled dot is bright in the middle, a hollow ring isn't.
    win = blue[max(0, py - 6):py + 7, max(0, px - 6):px + 7]
    _, mask = cv2.threshold(win, (win.max() + np.median(win)) / 2, 255, cv2.THRESH_BINARY)
    mask = mask.astype(np.uint8)
    _, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    label = labels[min(py, 6), min(px, 6)]
    w, h, area = stats[label, cv2.CC_STAT_WIDTH], stats[label, cv2.CC_STAT_HEIGHT], stats[label, cv2.CC_STAT_AREA]
    my, mx = (np.argwhere(labels == label).mean(axis=0) + (max(0, py - 6), max(0, px - 6)))
    behind = w >= 5 and h >= 5 and area / (w * h) < 0.6
    dx, dy = (mx + x0 - cx) / r, (cy - (my + y0)) / r
    return CompassReading(float(np.clip(dx, -1, 1)), float(np.clip(dy, -1, 1)), bool(behind), (cx, cy), r)


def _scaled(box, frame):
    sx, sy = frame.shape[1] / BASE_SIZE[0], frame.shape[0] / BASE_SIZE[1]
    return int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy)

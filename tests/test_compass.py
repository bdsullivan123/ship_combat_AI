"""The nav compass reader, on frames from the Dolphin's flight home (2026-10-29)."""

import math
from pathlib import Path

import pytest

from taccom.compass import SEARCH, CompassReading, read_compass

FIXTURES = Path(__file__).parent / "fixtures"


def frame(label):
    """A full 1080p frame rebuilt from the saved crop of the compass search area."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    x0, y0, x1, y1 = SEARCH
    full = np.zeros((1080, 1920, 3), np.uint8)
    full[y0:y1, x0:x1] = cv2.imread(str(FIXTURES / f"compass_dolphin_{label}.jpg"))
    return full


def test_target_dead_ahead():
    r = read_compass(frame("ahead"))
    assert not r.behind and r.angle < 3


def test_target_ahead_and_off_to_the_top_right():
    r = read_compass(frame("ahead_top_right"))
    assert not r.behind
    assert r.x == pytest.approx(0.45, abs=0.1) and r.y == pytest.approx(0.84, abs=0.1)


def test_target_behind_reads_the_hollow_dot():
    r = read_compass(frame("behind"))
    assert r.behind and r.x > 0.6 and r.y < -0.3
    assert r.angle > 90


def test_ring_lit_by_a_star_doesnt_hide_the_dot():
    r = read_compass(frame("lit_ring"))
    assert not r.behind and r.angle < 3


@pytest.mark.parametrize("label", ["galaxy_map", "hyperspace"])
def test_no_reading_without_the_compass(label):
    assert read_compass(frame(label)) is None


def test_angle_from_the_dot_offset():
    assert CompassReading(0, 0, False, (0, 0), 30).angle == 0
    assert CompassReading(1, 0, False, (0, 0), 30).angle == pytest.approx(90)
    assert CompassReading(0, 0, True, (0, 0), 30).angle == pytest.approx(180)
    assert CompassReading(0.5, 0, False, (0, 0), 30).angle == pytest.approx(math.degrees(math.asin(0.5)))

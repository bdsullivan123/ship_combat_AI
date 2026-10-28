from pathlib import Path

import pytest

from taccom.alerts import AlertEngine
from taccom.flags import Flags
from taccom.hud import HudReader, HudReading, HudWatcher, TextLine, Weapon
from taccom.state import ShipState, ammo_capacity

FIXTURES = Path(__file__).parent / "fixtures"


# -- reading a real frame -----------------------------------------------------

@pytest.fixture(scope="module")
def frame():
    cv2 = pytest.importorskip("cv2")
    return cv2.imread(str(FIXTURES / "hud_fdl_1080p_res.jpg"))


def test_reads_real_fer_de_lance_hud(frame):
    pytest.importorskip("rapidocr")
    reading = HudReader("ferdelance").read(frame)
    assert [(w.name, w.clip, w.reserve) for w in reading.weapons] == [
        ("MULTI-CANNON", 90, 1200), ("MULTI-CANNON", 28, 570), ("HEATSINK", 1, 2),
        ("MULTI-CANNON", 90, 480), ("CHAFF", 1, 10)]
    assert reading.heat == 32
    assert reading.hull == 100
    assert reading.target_lines == ["SINANN", "PYTHON", "DAVID MCLAREN", "COMPETENT"]
    assert reading.target_distance == "737m"


def test_weapon_counts_pair_with_the_label_above_them():
    lines = [TextLine("HEATSINK", 1, 423, 605), TextLine("MULTI-CANNON", 1, 526, 598),
             TextLine("1/2", 1, 471, 627), TextLine("28/570", 1, 604, 618)]
    reading = HudReading()
    HudReader._parse_secondary_weapons(sorted(lines, key=lambda ln: (ln.y, ln.x)), reading)
    assert [(w.name, w.clip, w.reserve) for w in reading.weapons] == [
        ("MULTI-CANNON", 28, 570), ("HEATSINK", 1, 2)]


# -- when to read ---------------------------------------------------------------

def combat_state(**overrides):
    state = ShipState(ship_type="ferdelance")
    state.flags = Flags.IN_MAIN_SHIP | Flags.HARDPOINTS_DEPLOYED | Flags.SHIELDS_UP
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_reads_only_with_the_combat_hud_showing():
    assert HudWatcher.layout_for(combat_state()) == "ferdelance"
    assert HudWatcher.layout_for(combat_state(gui_focus=6)) is None  # galaxy map open
    assert HudWatcher.layout_for(combat_state(ship_type="python")) is None  # not calibrated yet
    assert HudWatcher.layout_for(combat_state(flags=Flags.IN_MAIN_SHIP)) is None  # hardpoints away
    assert HudWatcher.layout_for(combat_state(
        flags=Flags.IN_MAIN_SHIP | Flags.HARDPOINTS_DEPLOYED | Flags.HUD_ANALYSIS_MODE)) is None


# -- screen-based alerts --------------------------------------------------------

def reading(heat=30, guns=(2190, 2190, 2190), sinks=2, chaff=11):
    weapons = [Weapon("MULTI-CANNON", 0, g) for g in guns]
    weapons += [Weapon("HEATSINK", 0, sinks), Weapon("CHAFF", 0, chaff)]
    return HudReading(weapons=weapons, heat=heat)


def feed(engine, state, *readings):
    said = []
    for i, r in enumerate(readings):
        state.apply_hud(r)
        said += [a.text for a in engine.on_hud(r, state, float(i * 2))]
    return said


def fdl_state():
    state = combat_state()
    state.ammo_capacity = ammo_capacity([
        {"Item": "hpt_multicannon_gimbal_huge", "AmmoInClip": 90, "AmmoInHopper": 2100},
        {"Item": "hpt_multicannon_gimbal_medium", "AmmoInClip": 90, "AmmoInHopper": 2100},
        {"Item": "hpt_multicannon_gimbal_medium", "AmmoInClip": 90, "AmmoInHopper": 2100},
        {"Item": "hpt_heatsinklauncher_turret_tiny", "AmmoInClip": 1, "AmmoInHopper": 2},
    ])
    state.modules = ["hpt_heatsinklauncher_turret_tiny"]
    return state


def test_heat_warning_needs_two_reads():
    engine, state = AlertEngine(), fdl_state()
    assert feed(engine, state, reading(heat=30), reading(heat=92), reading(heat=30)) == []
    assert feed(engine, state, reading(heat=88), reading(heat=91)) == ["Heat 88 percent."]


def test_ammo_thresholds_against_loadout_capacity():
    engine, state = AlertEngine(), fdl_state()  # 6570 rounds when full
    said = feed(engine, state,
                reading(guns=(2190, 2190, 2190)),
                reading(guns=(1500, 1500, 300)), reading(guns=(1490, 1490, 290)),  # ~50%
                reading(guns=(900, 600, 100)), reading(guns=(890, 590, 90)),  # ~24%
                reading(guns=(880, 580, 80)))
    assert said == ["Multicannon ammo 50 percent.", "Multicannon ammo 25 percent."]


def test_one_misread_does_not_alert():
    engine, state = AlertEngine(), fdl_state()
    # "1200" read as "120" once: the totals disagree, so nothing is said.
    assert feed(engine, state, reading(guns=(2190, 2190, 2190)), reading(guns=(219, 2190, 2190)),
                reading(guns=(2190, 2190, 2190))) == []


def test_smaller_fire_group_is_not_low_ammo():
    engine, state = AlertEngine(), fdl_state()
    state.fire_group = 1
    # Only one multicannon in this fire group: a third of the ship's total.
    assert feed(engine, state, reading(guns=(2190,)), reading(guns=(2180,))) == []


def test_dry_gun_and_empty_heat_sinks():
    engine, state = AlertEngine(), fdl_state()
    said = feed(engine, state, reading(guns=(2000, 2000, 5), sinks=1),
                reading(guns=(2000, 2000, 0), sinks=0), reading(guns=(1990, 1990, 0), sinks=0))
    assert said == ["One multicannon is dry.", "Heat sinks empty."]
    assert state.heatsinks_left == 0
    state.flags |= Flags.OVERHEATING
    assert [a.text for a in engine.on_journal({"event": "HeatWarning"}, state, 10.0)] == [
        "Overheating. No heat sinks left."]


def test_rearming_rearms_the_warnings():
    engine, state = AlertEngine(), fdl_state()
    low = reading(guns=(500, 500, 500))
    full = reading(guns=(2190, 2190, 2190))
    assert feed(engine, state, low, low) == ["Multicannon ammo 25 percent."]
    assert feed(engine, state, full, full, low, low) == ["Multicannon ammo 25 percent."]


def test_stock_phrases_cover_screen_alerts():
    phrases = AlertEngine.stock_phrases()
    for line in ("Heat 88 percent.", "Multicannon ammo 25 percent.", "One multicannon is dry.",
                 "Heat sinks empty.", "Overheating. No heat sinks left."):
        assert line in phrases

from pathlib import Path

import pytest

from taccom.alerts import AlertEngine
from taccom.flags import Flags
from taccom.hud import HudReader, HudReading, HudWatcher, TextLine, Weapon
from taccom.state import ShipState, ammo_capacity

FIXTURES = Path(__file__).parent / "fixtures"


# -- reading real frames -----------------------------------------------------

ALL_REGIONS = ("secondary_weapons", "own_ship", "target_ship", "heat", "target_panel")


@pytest.fixture(scope="module")
def reader():
    pytest.importorskip("rapidocr")
    return HudReader("ferdelance")


def load(name):
    cv2 = pytest.importorskip("cv2")
    return cv2.imread(str(FIXTURES / name))


def test_reads_real_fer_de_lance_hud(reader):
    reading = reader.read(load("hud_fdl_1080p_res.jpg"), ALL_REGIONS)
    assert [(w.name, w.clip, w.reserve) for w in reading.weapons] == [
        ("MULTI-CANNON", 90, 1200), ("MULTI-CANNON", 28, 570), ("HEATSINK", 1, 2),
        ("CHAFF", 1, 10), ("MULTI-CANNON", 90, 480)]
    assert (reading.heat, reading.shields, reading.hull) == (32, 100, 74)
    assert reading.target_lines == ["SINANN", "PYTHON", "DAVID MCLAREN", "COMPETENT"]
    assert reading.target_distance == "737m"


def test_reads_through_head_movement(reader):
    # Mid-fight, the cockpit shifted ~105 px; alignment puts it back.
    reading = reader.read(load("hud_fdl_1080p_moved.jpg"), ALL_REGIONS)
    assert (reading.shields, reading.hull, reading.heat) == (62, 100, 43)
    assert (reading.target_shields, reading.target_hull) == (86, 100)
    assert [w.total for w in reading.weapons if w.name == "MULTI-CANNON"] == [2180, 2164, 2164]


def test_skips_frames_it_cannot_align(reader):
    # Looking far off to the side: better to skip than to guess.
    assert reader.read(load("hud_fdl_1080p_looking_away.jpg")) is None


def test_weapon_counts_pair_with_the_label_above_them():
    lines = [TextLine("HEATSINK", 1, 423, 605), TextLine("MULTI-CANNON", 1, 526, 598),
             TextLine("1/2", 1, 471, 627), TextLine("28/570", 1, 604, 618)]
    reading = HudReading()
    HudReader._parse_weapons(sorted(lines, key=lambda ln: (ln.y, ln.x)), reading)
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
    assert HudWatcher.layout_for(combat_state(destroyed=True)) is None  # rebuy screen


# -- screen-based alerts --------------------------------------------------------

def reading(heat=30, guns=(2190, 2190, 2190), sinks=2, chaff=11):
    weapons = [Weapon("MULTI-CANNON", 0, g) for g in guns]
    weapons += [Weapon("HEATSINK", 0, sinks), Weapon("CHAFF", 0, chaff)]
    return HudReading(weapons=weapons, heat=heat, weapons_read=True)


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


# -- shields, hull and target from the screen -----------------------------------

def numbers(shields=None, hull=None, target_shields=None, target_hull=None):
    return HudReading(shields=shields, hull=hull, target_shields=target_shields, target_hull=target_hull)


def say(engine, state, *readings, start=0.0):
    return [a.text for i, r in enumerate(readings) for a in engine.on_hud(r, state, start + i * 2)]


def test_shield_steps_only_while_shields_are_up():
    engine, state = AlertEngine(), fdl_state()
    state.shields_up, state.pips = True, (4, 4, 4)
    assert say(engine, state, numbers(shields=72), numbers(shields=48), numbers(shields=47),
               numbers(shields=24), numbers(shields=22)) == [
        "Shields 50 percent.", "Shields 25 percent. Four pips to systems."]
    # Down, the number is recharge progress, not shield strength.
    engine, state = AlertEngine(), fdl_state()
    state.shields_up = False
    assert say(engine, state, numbers(shields=20), numbers(shields=21)) == []


def test_screen_hull_covers_what_the_journal_stops_reporting():
    engine, state = AlertEngine(), fdl_state()
    state.pips = (2, 8, 2)
    said = say(engine, state, numbers(hull=22), numbers(hull=18), numbers(hull=14), numbers(hull=14),
               numbers(hull=10), numbers(hull=9), numbers(hull=4), numbers(hull=4))
    assert said == ["Hull critical, 14 percent. Disengage now.", "Hull critical, 10 percent. Disengage now.",
                    "Hull critical, 4 percent. Disengage now."]


def test_target_calls_once_per_target():
    from taccom.state import Target

    engine, state = AlertEngine(), fdl_state()
    state.target = Target("Federal Assault Ship", 3, "Peter Smith")
    said = say(engine, state, numbers(target_shields=12, target_hull=100), numbers(target_shields=0, target_hull=100),
               numbers(target_shields=0, target_hull=60), numbers(target_shields=0, target_hull=22),
               numbers(target_shields=0, target_hull=21), numbers(target_shields=0, target_hull=20))
    assert said == ["Target shields down.", "Target hull 22 percent."]
    state.target = Target("Python", 3, "James Moss")
    said = say(engine, state, numbers(target_shields=0, target_hull=30), numbers(target_shields=0, target_hull=30),
               numbers(target_shields=0, target_hull=30), start=100.0)
    assert said == ["Target shields down."]


def test_target_switch_between_reads_is_ignored():
    from taccom.state import Target

    engine, state = AlertEngine(), fdl_state()
    state.target = Target("Python", 3, "James Moss")
    engine.on_hud(numbers(target_shields=0), state, 0.0)
    state.target = Target("Viper Mk III", 3, "Someone Else")
    # One read of each target is not two reads of the same target.
    assert engine.on_hud(numbers(target_shields=0), state, 2.0) == []


def test_nothing_after_the_ship_is_lost():
    engine, state = AlertEngine(), fdl_state()
    state.destroyed = True
    assert say(engine, state, numbers(hull=0), numbers(hull=0)) == []

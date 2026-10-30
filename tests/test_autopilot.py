"""The autopilot's steering decisions (no keys are sent here)."""

from taccom.autopilot import MAX_PULSE, steer
from taccom.compass import CompassReading


def reading(x, y, behind=False):
    return CompassReading(x, y, behind, (0, 0), 30)


def test_turns_toward_a_dot_up_and_to_the_right():
    plan = steer(reading(0.4, 0.2))
    assert set(plan) == {"pitch_up", "yaw_right"}
    assert plan["yaw_right"] > plan["pitch_up"]  # yaw turns slower, so it is held longer


def test_turns_toward_a_dot_down_and_to_the_left():
    assert set(steer(reading(-0.3, -0.5))) == {"pitch_down", "yaw_left"}


def test_small_offsets_get_short_taps():
    assert all(t < 0.15 for t in steer(reading(0.05, -0.05)).values())


def test_target_behind_turns_hard_the_short_way_round():
    assert steer(reading(0.1, 0.8, behind=True)) == {"pitch_up": MAX_PULSE}
    assert steer(reading(-0.9, 0.1, behind=True)) == {"yaw_left": MAX_PULSE}


def test_centred_means_no_keys():
    assert steer(reading(0.01, -0.01)) == {}


def test_jump_target_needs_another_star_system():
    from taccom.autopilot import jump_target

    here = {"event": "SupercruiseExit", "StarSystem": "Putas", "SystemAddress": 1}
    vogulu = {"event": "FSDTarget", "Name": "Vogulu", "SystemAddress": 2}
    assert jump_target([here, vogulu], {"Destination": {"System": 2, "Body": 0}}) == ("Vogulu", "Putas")
    # A station in this system, or no target at all, is not a jump.
    assert jump_target([here, vogulu], {"Destination": {"System": 1, "Body": 5}}) is None
    assert jump_target([here], {}) is None


def test_preflight_stops_for_what_it_cannot_fix():
    from taccom.autopilot import preflight_problem
    from taccom.flags import Flags

    flying = int(Flags.IN_MAIN_SHIP | Flags.SHIELDS_UP)
    assert preflight_problem({"Flags": flying}) is None
    # Gear, scoop and hardpoints are fixed with a key press, so they aren't problems here.
    assert preflight_problem({"Flags": flying | Flags.LANDING_GEAR_DOWN | Flags.HARDPOINTS_DEPLOYED}) is None
    assert "mass-locked" in preflight_problem({"Flags": flying | Flags.FSD_MASS_LOCKED})
    assert "interdiction" in preflight_problem({"Flags": flying | Flags.BEING_INTERDICTED})
    assert "fuel" in preflight_problem({"Flags": flying | Flags.LOW_FUEL})
    assert "panel" in preflight_problem({"Flags": flying, "GuiFocus": 6})
    assert "own ship" in preflight_problem({"Flags": flying, "Flags2": 1})
    assert "docked" in preflight_problem({"Flags": flying | Flags.DOCKED})


def test_extended_keys_are_sent_with_the_extended_flag(monkeypatch):
    import ctypes

    from taccom import autopilot

    sent = []

    class FakeUser32:
        @staticmethod
        def SendInput(n, ref, size):
            inp = ctypes.cast(ref, ctypes.POINTER(autopilot._Input)).contents
            sent.append((inp.ki.wScan, inp.ki.dwFlags))
            return 1

    monkeypatch.setattr(ctypes.windll, "user32", FakeUser32, raising=False)
    kb = autopilot.Keyboard()
    kb.down(autopilot.CARGO_SCOOP)
    kb.up(autopilot.CARGO_SCOOP)
    assert sent == [(0x47, 0x8 | 0x1), (0x47, 0x8 | 0x1 | 0x2)]

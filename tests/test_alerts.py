from taccom.alerts import AlertEngine, Priority
from taccom.flags import Flags
from taccom.state import ShipState

FLYING = Flags.IN_MAIN_SHIP | Flags.SHIELDS_UP


def flying_state(pips=(4, 4, 4), flags=FLYING):
    state = ShipState()
    state.apply_status({"Flags": int(flags), "Pips": list(pips)})
    return state


def status(engine, state, flags, now, pips=(4, 4, 4)):
    rising, falling = state.apply_status({"Flags": int(flags), "Pips": list(pips)})
    return engine.on_status(rising, falling, state, now)


def journal(engine, state, event, now):
    state.apply_journal(event)
    return engine.on_journal(event, state, now)


def texts(alerts):
    return [a.text for a in alerts]


def test_shields_down_advises_sys_pips_when_low():
    engine, state = AlertEngine(), flying_state()
    alerts = status(engine, state, Flags.IN_MAIN_SHIP, 1.0)
    assert texts(alerts) == ["Shields down. Four pips to systems."]
    assert alerts[0].priority == Priority.HIGH


def test_shields_down_skips_advice_when_sys_already_full():
    engine, state = AlertEngine(), flying_state(pips=(8, 2, 2))
    assert texts(status(engine, state, Flags.IN_MAIN_SHIP, 1.0, pips=(8, 2, 2))) == ["Shields down."]


def test_status_and_journal_shield_loss_speak_once():
    engine, state = AlertEngine(), flying_state()
    assert status(engine, state, Flags.IN_MAIN_SHIP, 1.0)
    assert journal(engine, state, {"event": "ShieldState", "ShieldsUp": False}, 1.2) == []


def test_first_status_read_is_not_a_change():
    engine, state = AlertEngine(), ShipState()
    rising, falling = state.apply_status({"Flags": int(Flags.IN_MAIN_SHIP | Flags.OVERHEATING)})
    assert engine.on_status(rising, falling, state, 0.0) == []


def test_no_alerts_while_docked():
    engine = AlertEngine()
    state = flying_state(flags=FLYING | Flags.DOCKED)
    assert status(engine, state, Flags.IN_MAIN_SHIP | Flags.DOCKED | Flags.LOW_FUEL, 1.0) == []


def test_switching_to_fighter_is_not_shield_loss():
    engine, state = AlertEngine(), flying_state()
    assert status(engine, state, Flags.IN_FIGHTER, 1.0) == []


def test_hull_escalates_through_cooldown():
    engine, state = AlertEngine(), flying_state(pips=(4, 2, 6))
    hit = lambda h, t: journal(engine, state, {"event": "HullDamage", "Health": h, "PlayerPilot": True, "Fighter": False}, t)
    assert texts(hit(0.55, 0.0)) == ["Hull 55 percent."]
    assert texts(hit(0.36, 0.5)) == ["Hull 36 percent. Consider disengaging."]
    assert texts(hit(0.18, 1.0)) == ["Hull critical, 18 percent. Disengage now. Four pips to engines."]
    assert state.hull == 0.18


def test_fighter_hull_is_labelled():
    engine, state = AlertEngine(), flying_state()
    alerts = journal(engine, state, {"event": "HullDamage", "Health": 0.4, "PlayerPilot": True, "Fighter": True}, 0.0)
    assert texts(alerts) == ["Fighter hull 40 percent. Consider disengaging."]
    assert state.hull is None  # the mothership's hull is untouched


def test_overheating_cooldown_and_heat_sink_advice():
    engine, state = AlertEngine(), flying_state()
    state.apply_journal({"event": "Loadout", "Ship": "ferdelance",
                         "Modules": [{"Item": "hpt_heatsinklauncher_turret_tiny"}]})
    assert texts(journal(engine, state, {"event": "HeatWarning"}, 0.0)) == ["Overheating. Heat sink advised."]
    assert journal(engine, state, {"event": "HeatWarning"}, 10.0) == []
    assert journal(engine, state, {"event": "HeatWarning"}, 25.0)


def test_taking_fire_only_at_start_of_engagement():
    engine, state = AlertEngine(), flying_state()
    attacked = lambda t: journal(engine, state, {"event": "UnderAttack", "Target": "You"}, t)
    assert texts(attacked(0.0)) == ["Taking fire."]
    # Constant fire keeps the engagement open, even past the quiet period.
    for t in range(1, 120):
        assert attacked(float(t)) == []
    # A minute of peace, then a new attack.
    assert attacked(200.0)


def test_self_destruct_silences_heat_and_hull():
    engine, state = AlertEngine(), flying_state()
    journal(engine, state, {"event": "SelfDestruct"}, 0.0)
    assert journal(engine, state, {"event": "HeatDamage"}, 1.0) == []
    assert journal(engine, state, {"event": "HullDamage", "Health": 0.2, "PlayerPilot": True}, 2.0) == []
    journal(engine, state, {"event": "Resurrect"}, 3.0)
    assert journal(engine, state, {"event": "HeatDamage"}, 4.0)


def test_wanted_target_announced_once_and_clean_ignored():
    engine, state = AlertEngine(), flying_state()
    wanted = {"event": "ShipTargeted", "TargetLocked": True, "Ship": "python", "ScanStage": 3,
              "PilotName_Localised": "James Moss", "PilotRank": "Deadly", "ShieldHealth": 40.2,
              "HullHealth": 100.0, "LegalStatus": "Wanted", "Bounty": 281687}
    clean = {"event": "ShipTargeted", "TargetLocked": True, "Ship": "type9", "Ship_Localised": "Type-9 Heavy",
             "ScanStage": 3, "PilotName_Localised": "Cunningham", "LegalStatus": "Clean"}
    assert texts(journal(engine, state, wanted, 0.0)) == [
        "Wanted Python. Caution, Deadly pilot. Shields 40 percent. Bounty 282 thousand credits."]
    assert journal(engine, state, clean, 1.0) == []
    assert journal(engine, state, wanted, 2.0) == []  # re-targeting the same ship
    journal(engine, state, {"event": "SupercruiseEntry"}, 3.0)
    assert journal(engine, state, wanted, 4.0)  # new instance, announce again


def test_kill_confirmed_reads_credits():
    engine, state = AlertEngine(), flying_state()
    alerts = journal(engine, state, {"event": "Bounty", "TotalReward": 1_250_000}, 0.0)
    assert texts(alerts) == ["Kill confirmed. 1.2 million credits."]

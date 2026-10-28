"""Replays a real fight from the journal and checks what TACCOM would have said.

The fixture is the commander's Fer-de-Lance losing to a Dangerous-rank Imperial
Clipper in a Hazardous RES on 2026-10-26: 389 journal lines, 375 of them
UnderAttack.
"""

import json
from pathlib import Path

from taccom.app import SessionRecorder, Taccom, read_inputs, replay
from taccom.speech import ConsoleSpeaker

FIXTURES = Path(__file__).parent / "fixtures"


def run(path, **kwargs):
    lines = []
    app = Taccom(ConsoleSpeaker(), echo=lines.append)
    replay(app, read_inputs(path, **kwargs), speed=None)
    return [line.split("  ", 2)[2].strip() for line in lines], app


def test_clipper_fight():
    said, app = run(FIXTURES / "clipper_fight_2026-10-26.log")
    assert said == [
        "Overheating.",
        "Taking fire.",
        "Wanted Imperial Clipper. Caution, Dangerous pilot. Shields 65 percent. Bounty 566 thousand credits.",
        "Overheating.",
        "Shields down.",
        "Hull 80 percent.",
        "Hull 55 percent.",
        "Hull 36 percent. Consider disengaging.",
        "Hull critical, 18 percent. Disengage now.",
        "Ship lost.",
    ]


def test_since_builds_state_silently():
    said, app = run(FIXTURES / "clipper_fight_2026-10-26.log", since="2026-10-26T23:44:00Z")
    # Joining mid-fight, the first hit in the window opens the engagement.
    assert said[:2] == ["Taking fire.", "Shields down."]
    assert app.state.target is None  # cleared by Died


def test_recorded_session_round_trip(tmp_path):
    recorder = SessionRecorder(tmp_path)
    live = Taccom(ConsoleSpeaker(), recorder, echo=lambda _: None)
    live.handle_status({"Flags": 1 << 24 | 1 << 3, "Pips": [2, 4, 6]}, 0.0, announce=False)
    live.handle_status({"Flags": 1 << 24, "Pips": [2, 4, 6]}, 1.0)
    recorder.close()

    lines = [json.loads(line) for line in recorder.path.read_text().splitlines()]
    assert [line["src"] for line in lines] == ["status", "status", "alert"]
    assert lines[0]["live"] is False

    said, _ = run(recorder.path)
    assert said == ["Shields down. Four pips to systems."]


def test_federal_assault_ship_fight_with_screen_reading():
    """2026-10-27: journal and status as recorded, plus HUD readings from the captured frames.

    The journal's last hull report was 20%; the HUD carried on down to 2%.
    """
    said, app = run(FIXTURES / "fas_fight_2026-10-27.jsonl")
    assert said == [
        "Taking fire.",
        "Shields down. Four pips to systems.",
        "Hull 80 percent.",
        "Hull 60 percent.",
        "Hull 38 percent. Consider disengaging.",
        "Hull critical, 20 percent. Disengage now. Four pips to engines.",
        "Hull critical, 14 percent. Disengage now. Four pips to engines.",
        "Hull critical, 10 percent. Disengage now. Four pips to engines.",
        "Hull critical, 4 percent. Disengage now.",
        "Taking fire.",
        "Ship lost.",
    ]
    assert not app.state.destroyed  # the recording runs on through the rebuy and respawn


def test_res_bounty_run_target_shields():
    """2026-10-27, second session: five kills in a Hazardous RES, replayed with HUD readings.

    The Cobra's shields failed, then came back online (blue 38%) just before the kill.
    """
    said, _ = run(FIXTURES / "res_bounty_run_2026-10-27.jsonl")
    shield_calls = [s for s in said if s.startswith("Target shields")]
    assert shield_calls == ["Target shields down.",  # Viper Mk IV
                            "Target shields down.", "Target shields back online.",  # Cobra Mk III
                            "Target shields down.",  # Adder
                            "Target shields down."]  # Sidewinder
    assert said.count("Kill confirmed. 116 thousand credits.") == 1

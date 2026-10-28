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

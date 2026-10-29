"""The console's telemetry, its handling of Claude Code's stream, and the voice text."""

import json
import sys

from taccom import console
from taccom.console import Console, Telemetry, Voice, speakable, tool_detail

# Stands in for `claude -p --output-format stream-json`: reads the prompt from
# stdin, records its arguments, and streams a reply with one tool call.
FAKE_CLAUDE = r'''
import json, sys
prompt = sys.stdin.read()
open(sys.argv[1], "a").write(json.dumps(sys.argv[2:]) + "\n")
def out(o): print(json.dumps(o), flush=True)
out({"type": "system", "subtype": "init", "session_id": "x"})
out({"type": "stream_event", "event": {"type": "message_start"}})
out({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}})
out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Checking."}}})
out({"type": "stream_event", "event": {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "name": "Bash"}}})
out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"description\": \"Read the jour"}}})
out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "nal\", \"command\": \"x\"}"}}})
out({"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}})
out({"type": "stream_event", "event": {"type": "message_start"}})
out({"type": "stream_event", "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}})
out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "You heard: " + prompt}}})
out({"type": "result", "subtype": "success", "result": "You heard: " + prompt})
'''


def journal_dir(tmp_path):
    events = [
        {"event": "LoadGame", "Commander": "Test", "Ship": "Dolphin", "ShipName": "", "Credits": 1000,
         "FuelLevel": 16.0, "FuelCapacity": 16.0},
        {"event": "Loadout", "Ship": "dolphin", "ShipName": "Blue Moon", "MaxJumpRange": 30.93,
         "FuelCapacity": {"Main": 16.0, "Reserve": 0.5}, "HullHealth": 1.0},
        {"event": "FSDJump", "StarSystem": "Sifi WA-F d11-47", "JumpDist": 21.6, "FuelLevel": 12.5},
        {"event": "JetConeBoost", "BoostValue": 4.0},
    ]
    (tmp_path / "Journal.2026-10-29T200000.01.log").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    route = [{"StarSystem": s} for s in ("Sifi WA-F d11-47", "Sifi FS-U b44-2", "Sifi VP-G d10-54")]
    (tmp_path / "NavRoute.json").write_text(json.dumps({"event": "NavRoute", "Route": route}))
    (tmp_path / "Status.json").write_text(json.dumps({"Flags": 1 << 4, "Fuel": {"FuelMain": 12.0}, "Balance": 5_000_000}))
    return tmp_path


def test_telemetry_from_game_files(tmp_path):
    t = Telemetry(journal_dir(tmp_path))
    t.poll()
    d = t.snapshot()
    assert d["system"] == "Sifi WA-F d11-47"
    assert d["ship"] == "Dolphin" and d["ship_name"] == "Blue Moon"
    assert d["supercharged"] is True
    assert d["destination"] == "Sifi VP-G d10-54" and d["route_jumps"] == 2
    assert d["fuel"] == 12.0 and d["credits"] == 5_000_000 and d["mode"] == "supercruise"
    assert "(FSD supercharged)" in t.summary()


def test_supercharge_ends_with_the_next_jump(tmp_path):
    t = Telemetry(journal_dir(tmp_path))
    with open(tmp_path / "Journal.2026-10-29T200000.01.log", "a") as f:
        f.write(json.dumps({"event": "FSDJump", "StarSystem": "Sifi FS-U b44-2", "FuelLevel": 12.4}) + "\n")
    t.poll()
    d = t.snapshot()
    assert d["supercharged"] is False
    assert d["route_jumps"] == 1  # the route counts from where the ship is now


def test_ask_streams_reply_and_resumes_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(console, "STATE_FILE", tmp_path / "console.json")
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    calls = tmp_path / "calls.txt"
    c = Console([sys.executable, str(script), str(calls)], Telemetry(journal_dir(tmp_path)), Voice(None, 1.1))

    events = []
    c.ask("Status report", events.append)
    assert [e["t"] for e in events] == ["text", "tool", "text", "done"]
    assert events[1] == {"t": "tool", "name": "Bash", "detail": "Read the journal"}
    reply = c.state["messages"][-1]
    assert reply["text"] == "Checking.\n\nYou heard: Status report"

    c.ask("Again", lambda e: None)
    first, second = (json.loads(line) for line in calls.read_text().splitlines())
    sid = c.state["session_id"]
    assert first[first.index("--session-id") + 1] == sid and "--resume" not in first
    assert second[second.index("--resume") + 1] == sid
    assert "Sifi WA-F d11-47" in first[first.index("--append-system-prompt") + 1]

    # The transcript survives a restart of the console.
    again = Console([sys.executable, str(script), str(calls)], Telemetry(tmp_path), Voice(None, 1.1))
    assert again.state["session_id"] == sid and len(again.state["messages"]) == 4


def test_speakable_strips_markdown():
    text = ("**Barba City** is 291 ly away.\n\n| Station | Jumps |\n|---|---|\n| Barba City | 10 |\n\n"
            "- Sell at [Vista Genomics](https://example.com)\n1. `Plot route`")
    spoken = speakable(text)
    assert spoken.startswith("Barba City is 291 ly away.")
    assert spoken.endswith("Plot route.")
    assert "*" not in spoken and "|" not in spoken and "http" not in spoken and "`" not in spoken
    assert "Vista Genomics" in spoken


def test_tool_detail_prefers_the_description():
    assert tool_detail('{"command": "ls", "description": "List files"}') == "List files"
    assert tool_detail('{"file_path": "C:/x/y/state.py"}') == "state.py"
    assert tool_detail("not json") == ""

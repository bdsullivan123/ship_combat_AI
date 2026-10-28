"""Wires the readers, ship state, alert rules and speech together."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from .alerts import Alert, AlertEngine, Priority
from .sources import JournalTailer, StatusReader
from .state import ShipState

POLL_INTERVAL = 0.1  # seconds; Status.json updates a few times a second at most


class Taccom:
    def __init__(self, speaker, recorder: "SessionRecorder | None" = None, echo=print, collector=None,
                 hud=None):
        self.state = ShipState()
        self.engine = AlertEngine()
        self.speaker = speaker
        self.recorder = recorder
        self.collector = collector  # capture.FrameCollector, when --capture is on
        self.hud = hud  # hud.HudWatcher, when screen reading is on
        self.replaying = False
        self._last_ts: str | None = None
        self.echo = echo

    def handle_journal(self, event: dict, now: float, announce: bool = True) -> list[Alert]:
        if self.recorder:
            self.recorder.write("journal", event, live=announce)
        self._last_ts = event.get("timestamp", self._last_ts)
        self.state.apply_journal(event)
        if self.collector and announce:
            self.collector.on_journal(event, self.state, now)
        alerts = self.engine.on_journal(event, self.state, now) if announce else []
        self._emit(alerts, event.get("timestamp"))
        return alerts

    def handle_status(self, status: dict, now: float, announce: bool = True) -> list[Alert]:
        if self.recorder:
            self.recorder.write("status", status, live=announce)
        self._last_ts = status.get("timestamp", self._last_ts)
        rising, falling = self.state.apply_status(status)
        alerts = self.engine.on_status(rising, falling, self.state, now) if announce else []
        self._emit(alerts, status.get("timestamp"))
        return alerts

    def handle_hud(self, reading, now: float) -> list[Alert]:
        if self.recorder:
            self.recorder.write("hud", reading_to_dict(reading))
        self.state.apply_hud(reading)
        alerts = self.engine.on_hud(reading, self.state, now)
        # Screen reads carry no game timestamp. Live, "now" is right; in a
        # replay, the last game event's time is the closest thing to it.
        self._emit(alerts, self._last_ts if self.replaying else None)
        return alerts

    def say(self, text: str, priority: Priority = Priority.LOW, now: float = 0.0) -> None:
        self._emit([Alert("system", text, priority, now)], None)

    def _emit(self, alerts: list[Alert], timestamp: str | None) -> None:
        for a in alerts:
            when = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone() if timestamp else datetime.now()
            stamp = when.strftime("%H:%M:%S")  # local time, whatever the source
            self.echo(f"{stamp}  {a.priority.name:<8}  {a.text}")
            if self.recorder:
                self.recorder.write("alert", {"key": a.key, "text": a.text, "priority": a.priority.name})
            self.speaker.say(a)


def run_live(app: Taccom, journal_dir: Path) -> None:
    tailer = JournalTailer(journal_dir)
    status = StatusReader(journal_dir / "Status.json")

    # Catch up on the current session silently so we know the ship, loadout
    # and target, without announcing things that happened before we started.
    backlog = tailer.read_existing()
    for event in backlog:
        app.handle_journal(event, time.monotonic(), announce=False)
    first = status.poll()
    if first:
        app.handle_status(first, time.monotonic(), announce=False)

    where = tailer.path.name if tailer.path else "no journal yet"
    app.echo(f"[taccom] Watching {journal_dir} ({where}, {len(backlog)} past events). Ctrl+C to stop.")
    app.say("TACCOM online." + (f" {app.state.ship} systems nominal." if app.state.ship else ""))

    while True:
        now = time.monotonic()
        for event in tailer.poll():
            app.handle_journal(event, now)
        snapshot = status.poll()
        if snapshot:
            app.handle_status(snapshot, now)
        if app.collector:
            app.collector.tick(app.state, now)
        if app.hud:
            app.hud.update(app.state)
            for reading in app.hud.readings():
                app.handle_hud(reading, now)
        time.sleep(POLL_INTERVAL)


# -- recording and replay ----------------------------------------------------

class SessionRecorder:
    """Writes every input (and every alert) to a JSON-lines file for later replay."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"session_{datetime.now():%Y-%m-%d_%H%M%S}.jsonl"
        self._file = open(self.path, "a", encoding="utf-8")
        self._start = time.monotonic()

    def write(self, source: str, data: dict, live: bool = True) -> None:
        line = {"t": round(time.monotonic() - self._start, 3), "src": source, "data": data}
        if not live:
            line["live"] = False  # catch-up at startup: replay applies it silently
        self._file.write(json.dumps(line) + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def reading_to_dict(reading) -> dict:
    from dataclasses import asdict

    return asdict(reading)


def reading_from_dict(data: dict):
    from .hud import HudReading, Weapon

    return HudReading(**{**data, "weapons": [Weapon(**w) for w in data.get("weapons", [])]})


def _parse_ts(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def read_inputs(path: Path, since: str | None = None, until: str | None = None) -> Iterator[tuple[float, str, dict, bool]]:
    """Yield (seconds, source, data, live) from a recorded session or a raw game journal."""
    lo = _parse_ts(since) if since else None
    hi = _parse_ts(until) if until else None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "src" in obj:  # recorded session
                if obj["src"] == "alert":
                    continue
                t, src, data, live = obj["t"], obj["src"], obj["data"], obj.get("live", True)
            else:  # raw Journal.*.log
                if "timestamp" not in obj:
                    continue
                t, src, data, live = _parse_ts(obj["timestamp"]), "journal", obj, True
            ts = _parse_ts(data["timestamp"]) if data.get("timestamp") else None
            if ts and hi and ts > hi:
                break
            if ts and lo and ts < lo:
                live = False  # before the window: still builds ship state, silently
            yield t, src, data, live


def replay(app: Taccom, inputs: Iterable[tuple[float, str, dict, bool]], speed: float | None) -> None:
    """Feed recorded inputs through the app. speed=None runs instantly."""
    app.replaying = True
    start_t = None
    start_wall = time.monotonic()
    for t, src, data, live in inputs:
        if not live:
            # Startup catch-up, or before --since: state only, no pacing.
            (app.handle_journal if src == "journal" else app.handle_status)(data, t, announce=False)
            continue
        if start_t is None:
            start_t = t
        if speed:
            delay = (t - start_t) / speed - (time.monotonic() - start_wall)
            if delay > 0:
                time.sleep(delay)
        if src == "journal":
            app.handle_journal(data, t)
        elif src == "status":
            app.handle_status(data, t)
        elif src == "hud":
            app.handle_hud(reading_from_dict(data), t)

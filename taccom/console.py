"""TACCOM console: talk to Claude Code from a ship's-computer window.

Each message runs Claude Code headless (`claude -p`) in this repo, resuming one
console session, so the answers come from the same Claude Code with the same
tools and memory as the editor. A small local web server streams the reply to
the page and serves live telemetry from the game files beside it.

The server listens on 127.0.0.1 only: whoever reaches it can run Claude Code
with tools on this PC.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .sources import JournalTailer, StatusReader, default_journal_dir
from .state import ship_display_name

ROOT = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).resolve().parent / "console.html"
STATE_FILE = ROOT / "sessions" / "console.json"
DEFAULT_PORT = 7700
HISTORY_LIMIT = 200  # messages kept for redrawing the page
# With its own window, the console shuts down once the window has been closed this long.
# Browsers slow timers in hidden windows to once a minute, so this stays well above that.
IDLE_SHUTDOWN = 180

PERSONA = """\
The commander is talking to you through the TACCOM console: a ship's-computer window \
on their desktop, used while they play Elite Dangerous. You are still Claude Code, with \
your usual tools, memory and judgment, working in the TACCOM repository.

Answer like a capable ship's computer: brief, plain and practical. Lead with the answer. \
Prefer short paragraphs and short lists to big tables, and keep markdown light, because \
replies may be read aloud. Do not pretend to see or control the game beyond what the \
files and tools show you.

The game writes its journal, Status.json and NavRoute.json to: {journal_dir}
Live telemetry when this message was sent:
{telemetry}"""


def find_claude() -> str | None:
    """The Claude Code executable: $TACCOM_CLAUDE, then PATH, then the VS Code extension's copy."""
    explicit = os.environ.get("TACCOM_CLAUDE")
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    extensions = Path.home() / ".vscode" / "extensions"

    def version(p: Path) -> tuple[int, ...]:
        m = re.search(r"anthropic\.claude-code-([\d.]+)", str(p))
        return tuple(int(x) for x in m.group(1).split(".") if x) if m else ()

    bundled = sorted(extensions.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"), key=version)
    return str(bundled[-1]) if bundled else None


class Telemetry:
    """What the ship is doing right now, from the journal, Status.json and NavRoute.json."""

    def __init__(self, journal_dir: Path):
        self.journal_dir = journal_dir
        self._journal = JournalTailer(journal_dir)
        self._status = StatusReader(journal_dir / "Status.json")
        self._lock = threading.Lock()
        self.data: dict = {"system": None, "ship": None, "ship_name": None, "commander": None,
                           "fuel": None, "fuel_capacity": None, "jump_range": None, "credits": None,
                           "station": None, "body": None, "supercharged": False, "next_jump": None,
                           "route_jumps": None, "destination": None, "hull": None, "legal": None,
                           "mode": None, "updated": None}
        for event in self._journal.read_existing():
            self._apply(event)
        self._read_route()

    def poll(self) -> None:
        for event in self._journal.poll():
            self._apply(event)
        status = self._status.poll()
        if status:
            self._apply_status(status)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.data)

    def summary(self) -> str:
        d = self.snapshot()
        parts = []
        if d["ship"]:
            parts.append(f"Ship: {d['ship']}" + (f" \"{d['ship_name']}\"" if d["ship_name"] else ""))
        if d["system"]:
            where = d["system"] + (f", docked at {d['station']}" if d["station"] else "") \
                + (f", near {d['body']}" if d["body"] and not d["station"] else "")
            parts.append(f"Location: {where}")
        if d["fuel"] is not None and d["fuel_capacity"]:
            parts.append(f"Fuel: {d['fuel']:.1f} of {d['fuel_capacity']:g} t")
        if d["jump_range"]:
            parts.append(f"Max jump range: {d['jump_range']:.1f} ly" + (" (FSD supercharged)" if d["supercharged"] else ""))
        if d["destination"]:
            parts.append(f"Plotted route: {d['route_jumps']} jumps to {d['destination']}")
        if d["hull"] is not None:
            parts.append(f"Hull: {d['hull']:.0f}%")
        if d["credits"] is not None:
            parts.append(f"Credits: {d['credits']:,}")
        if d["mode"]:
            parts.append(f"Flight mode: {d['mode']}")
        return "\n".join(f"- {p}" for p in parts) or "- (no game data found yet)"

    def _set(self, **values) -> None:
        with self._lock:
            self.data.update(values)
            self.data["updated"] = time.time()

    def _apply(self, e: dict) -> None:
        kind = e.get("event")
        if kind == "LoadGame":
            self._set(commander=e.get("Commander"), ship=ship_display_name(e), ship_name=e.get("ShipName") or None,
                      credits=e.get("Credits"), fuel=e.get("FuelLevel"), fuel_capacity=e.get("FuelCapacity"))
        elif kind == "Loadout":
            cap = e.get("FuelCapacity") or {}
            hull = e.get("HullHealth")
            self._set(ship=ship_display_name(e), ship_name=e.get("ShipName") or None,
                      jump_range=e.get("MaxJumpRange"), fuel_capacity=cap.get("Main"),
                      hull=hull * 100 if hull is not None else None)
        elif kind in ("Location", "FSDJump", "CarrierJump"):
            self._set(system=e.get("StarSystem"), body=e.get("Body") if kind == "Location" else None,
                      station=e.get("StationName") if e.get("Docked") else None, supercharged=False,
                      **({"fuel": e["FuelLevel"]} if "FuelLevel" in e else {}))
            self._read_route()
        elif kind == "Docked":
            self._set(station=e.get("StationName"))
        elif kind == "Undocked":
            self._set(station=None)
        elif kind == "ApproachBody":
            self._set(body=e.get("Body"))
        elif kind == "LeaveBody":
            self._set(body=None)
        elif kind == "JetConeBoost":
            self._set(supercharged=True)
        elif kind == "FSDTarget":
            self._set(next_jump=e.get("Name"))
        elif kind == "NavRoute":
            self._read_route()
        elif kind == "NavRouteClear":
            self._set(destination=None, route_jumps=None)
        elif kind == "HullDamage" and e.get("PlayerPilot", True):
            self._set(hull=e.get("Health", 0) * 100)
        elif kind in ("RefuelAll", "RefuelPartial", "FuelScoop"):
            if "Total" in e:
                self._set(fuel=e["Total"])

    def _apply_status(self, s: dict) -> None:
        flags = s.get("Flags", 0)
        if flags & (1 << 0):
            mode = "docked"
        elif flags & (1 << 1):
            mode = "landed"
        elif flags & (1 << 4):
            mode = "supercruise"
        elif flags & (1 << 30):
            mode = "hyperspace"
        elif flags:
            mode = "normal space"
        else:
            mode = "on foot or in menus"
        fuel = (s.get("Fuel") or {}).get("FuelMain")
        self._set(mode=mode, credits=s.get("Balance", self.data["credits"]), legal=s.get("LegalState"),
                  **({"fuel": fuel} if fuel is not None else {}))

    def _read_route(self) -> None:
        try:
            route = json.loads((self.journal_dir / "NavRoute.json").read_text(encoding="utf-8")).get("Route") or []
        except (OSError, ValueError):
            return
        here = self.data.get("system")
        names = [r.get("StarSystem") for r in route]
        if here in names:
            names = names[names.index(here):]
        if len(names) > 1:
            self._set(destination=names[-1], route_jumps=len(names) - 1)
        else:
            self._set(destination=None, route_jumps=None)


def speakable(text: str) -> str:
    """Markdown reply to plain sentences for the voice."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links: keep the label
    text = re.sub(r"https?://\S+", "the link", text)
    text = re.sub(r"^\s*\|?[-:| ]+\|[-:| ]*$", "", text, flags=re.M)  # table rules
    text = text.replace("|", ", ")
    text = re.sub(r"[*_`#>]+", "", text)
    text = re.sub(r"^\s*(?:[-•]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"\s*\n\s*", ". ", text.strip())
    text = re.sub(r"(?:\s*[.,]\s*){2,}", ". ", text)
    return re.sub(r"\s{2,}", " ", text).strip(" .,") + "."


class Voice:
    """Reads replies aloud with the TACCOM voice. Loads the model on first use."""

    def __init__(self, voice: str | None, speed: float):
        self.voice, self.speed = voice, speed
        self.enabled = False
        self.error: str | None = None
        self._backend = None
        self._lock = threading.Lock()

    def say(self, text: str) -> None:
        if self.enabled and text.strip():
            threading.Thread(target=self._say, args=(speakable(text),), daemon=True).start()

    def stop(self) -> None:
        if self._backend:
            self._backend.close()

    def _say(self, text: str) -> None:
        with self._lock:
            try:
                if self._backend is None:
                    from .speech import DEFAULT_KOKORO_VOICE, KokoroBackend

                    self._backend = KokoroBackend(self.voice or DEFAULT_KOKORO_VOICE, self.speed)
                audio = self._backend.render(text, cache=False)
                if self.enabled:
                    self._backend.play(audio)
            except Exception as exc:  # no model, no audio device: keep the console working
                self.error = str(exc)


class Console:
    """One console session of Claude Code, and the transcript shown on the page."""

    def __init__(self, claude: str | list[str], telemetry: Telemetry, voice: Voice, model: str | None = None):
        self.claude = [claude] if isinstance(claude, str) else list(claude)  # a list lets tests run a stand-in
        self.telemetry, self.voice, self.model = telemetry, voice, model
        self.busy = threading.Lock()
        self.last_seen = time.time()
        self._proc: subprocess.Popen | None = None
        self.state = {"session_id": None, "started": False, "messages": []}
        try:
            self.state.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.state["messages"] = self.state["messages"][-HISTORY_LIMIT:]
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.state, indent=1), encoding="utf-8")

    def new_session(self) -> None:
        self.state = {"session_id": None, "started": False, "messages": []}
        self.save()

    def stop(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            proc.kill()

    def ask(self, text: str, emit) -> None:
        """Run one turn, calling emit(dict) for each piece of the reply as it arrives."""
        if not self.state["session_id"]:
            self.state["session_id"] = str(uuid.uuid4())
        sid = self.state["session_id"]
        persona = PERSONA.format(journal_dir=self.telemetry.journal_dir, telemetry=self.telemetry.summary())
        cmd = [*self.claude, "-p", "--output-format", "stream-json", "--include-partial-messages", "--verbose",
               "--permission-mode", "auto", "--permission-prompts", "none", "--append-system-prompt", persona]
        cmd += ["--resume", sid] if self.state["started"] else ["--session-id", sid, "--name", "TACCOM console"]
        if self.model:
            cmd += ["--model", self.model]

        self.state["messages"].append({"role": "user", "text": text, "time": time.time()})
        reply = {"role": "assistant", "text": "", "tools": [], "time": time.time()}
        final = ""
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._proc = proc = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                                             creationflags=flags)
        proc.stdin.write(text)
        proc.stdin.close()
        tool_input: dict[int, str] = {}
        tool_name: dict[int, str] = {}
        new_block = False
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "system" and msg.get("subtype") == "init":
                self.state["started"] = True
            elif kind == "stream_event":
                ev = msg.get("event", {})
                if ev.get("type") == "content_block_start":
                    block = ev.get("content_block", {})
                    if block.get("type") == "tool_use":
                        tool_name[ev["index"]], tool_input[ev["index"]] = block.get("name", "tool"), ""
                    elif block.get("type") == "text" and reply["text"]:
                        new_block = True
                elif ev.get("type") == "content_block_delta":
                    delta = ev.get("delta", {})
                    if delta.get("type") == "text_delta":
                        chunk = ("\n\n" if new_block else "") + delta.get("text", "")
                        new_block = False
                        reply["text"] += chunk
                        emit({"t": "text", "d": chunk})
                    elif delta.get("type") == "input_json_delta" and ev.get("index") in tool_input:
                        tool_input[ev["index"]] += delta.get("partial_json", "")
                elif ev.get("type") == "content_block_stop" and ev.get("index") in tool_name:
                    i = ev["index"]
                    tool = {"name": tool_name.pop(i), "detail": tool_detail(tool_input.pop(i))}
                    reply["tools"].append(tool)
                    emit({"t": "tool", **tool})
                elif ev.get("type") == "message_start":
                    tool_name.clear()
                    tool_input.clear()
            elif kind == "result":
                final = msg.get("result") or ""
                if msg.get("is_error") and not reply["text"]:
                    reply["text"] = final or "The request failed."
                    emit({"t": "text", "d": reply["text"]})
        code = proc.wait()
        self._proc = None
        if code and not reply["text"]:
            err = proc.stderr.read().strip()[-600:] or f"Claude Code exited with code {code}."
            reply["text"] = f"Link failure: {err}"
            emit({"t": "error", "d": reply["text"]})
        self.state["messages"].append(reply)
        self.save()
        self.voice.say(final or reply["text"])
        emit({"t": "done"})


def tool_detail(raw: str) -> str:
    try:
        args = json.loads(raw) if raw else {}
    except ValueError:
        return ""
    for key in ("description", "query", "url", "file_path", "pattern", "prompt", "command"):
        if args.get(key):
            value = str(args[key]).replace("\n", " ")
            if key == "file_path":
                value = Path(value).name
            return value[:120]
    return ""


def make_handler(console: Console):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def setup(self) -> None:
            super().setup()
            console.last_seen = time.time()

        def _json(self, payload, code: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return {}

        def _local_origin(self) -> bool:
            # Refuse requests sent by other web pages open in the browser.
            origin = self.headers.get("Origin")
            return origin is None or origin == f"http://{self.headers.get('Host')}"

        def do_GET(self) -> None:
            if self.path == "/":
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/telemetry":
                console.telemetry.poll()
                self._json({**console.telemetry.snapshot(), "voice": console.voice.enabled,
                            "voice_error": console.voice.error, "busy": console.busy.locked()})
            elif self.path == "/api/history":
                self._json({"messages": console.state["messages"], "session": console.state["session_id"]})
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if not self._local_origin():
                self.send_error(403)
                return
            data = self._body()
            if self.path == "/api/ask":
                self._ask(str(data.get("text", "")).strip())
            elif self.path == "/api/stop":
                console.stop()
                console.voice.stop()
                self._json({"ok": True})
            elif self.path == "/api/voice":
                console.voice.enabled = bool(data.get("on"))
                console.voice.error = None
                if not console.voice.enabled:
                    console.voice.stop()
                self._json({"voice": console.voice.enabled})
            elif self.path == "/api/new":
                if console.busy.locked():
                    self._json({"error": "busy"}, 409)
                else:
                    console.new_session()
                    self._json({"ok": True})
            else:
                self.send_error(404)

        def _ask(self, text: str) -> None:
            if not text:
                self._json({"error": "empty"}, 400)
                return
            if not console.busy.acquire(blocking=False):
                self._json({"error": "busy"}, 409)
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                gone = False

                def emit(payload: dict) -> None:
                    nonlocal gone
                    if gone:
                        return
                    try:
                        self.wfile.write((json.dumps(payload) + "\n").encode())
                        self.wfile.flush()
                    except OSError:
                        gone = True  # page closed: let the turn finish anyway

                console.ask(text, emit)
            finally:
                console.busy.release()
                self.close_connection = True

    return Handler


def open_window(url: str) -> None:
    """A chromeless app window in Edge or Chrome, else the default browser."""
    candidates = [
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for exe in candidates:
        if exe.is_file():
            subprocess.Popen([str(exe), f"--app={url}", "--window-size=1280,860"])
            return
    import webbrowser

    webbrowser.open(url)


def serve(port: int = DEFAULT_PORT, voice: str | None = None, speed: float = 1.1, speak: bool = False,
          model: str | None = None, journal_dir: Path | None = None, window: bool = True) -> int:
    claude = find_claude()
    if not claude:
        print("Claude Code not found. Install it, or set TACCOM_CLAUDE to the path of claude.exe.")
        return 1
    telemetry = Telemetry(journal_dir or default_journal_dir())
    speech = Voice(voice, speed)
    speech.enabled = speak
    console = Console(claude, telemetry, speech, model)
    url = f"http://127.0.0.1:{port}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(console))
    except OSError:
        # Already running (launched twice): just open another window on it.
        print(f"Port {port} is in use; opening the console that is already running.")
        if window:
            open_window(url)
        return 0
    print(f"TACCOM console at {url}  (Claude Code: {claude})")
    print("Ctrl+C to shut down." + (" It also shuts down a few minutes after its window closes." if window else ""))
    if window:
        open_window(url)

        def watchdog() -> None:
            while time.time() - console.last_seen < IDLE_SHUTDOWN or console.busy.locked():
                time.sleep(15)
            server.shutdown()

        threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        console.stop()
        server.server_close()
    return 0

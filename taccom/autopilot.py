"""Flies the ship: the autopilot, built up a step at a time.

`align` turns the ship to face the targeted system using the nav compass.
`jump_once` aligns, jumps, and on arrival cuts the throttle and pitches up
off the star, then stops. `fly_route` repeats that along the plotted route,
scooping fuel on the way if the ship has a scoop.

Keys go to the game as hardware scan codes (Elite ignores ordinary simulated
keypresses), and only while Elite is the active window. The binds are the
autopilot's secondary slots in the commander's Custom.4.2.binds. Right Ctrl aborts
at any time; so does switching away from the game.

The owner chose to build this knowing Frontier's terms forbid automation.
"""

from __future__ import annotations

import ctypes
import json
import time
from ctypes import wintypes
from pathlib import Path

from .flags import Flags

# Scan codes for the binds TACCOM added (see the README's autopilot section).
# Not the F-keys: on the commander's setup the game only acts on those with Ctrl held.
KEYS = {
    "yaw_left": 0x33,  # ,
    "yaw_right": 0x34,  # .
    "pitch_up": 0x27,  # ;
    "pitch_down": 0x37,  # numpad *
}
JUMP = 0x24  # J: frame shift drive (hyperspace when a system is targeted)
THROTTLE_ZERO = 0x2D  # X
THROTTLE_25 = 0x53  # numpad .
THROTTLE_75 = 0x4A  # numpad -
TARGET_NEXT = 0x25  # K: next system in route
THROTTLE_FULL = 0x29  # ` (100%); a hyperspace jump only engages at full throttle
LANDING_GEAR = 0x26  # L
HARDPOINTS = 0x16  # U
CARGO_SCOOP = 0xE047  # Home (an extended key)
ABORT_VK = 0xA3  # Right Ctrl, unbound in the game

ALIGNED = 3.5  # degrees; the game jumps from further off than this, and chasing less made the aim hunt
HOLD_ALIGNED = 8.0  # while the drive charges; tighter made it hunt, and the game engaged at ~6
MAX_PULSE = 0.35  # seconds a turn key is held per step
MIN_PULSE = 0.07  # shorter taps barely turned the Fer-de-Lance in the first live test
SETTLE = 0.12  # seconds for the ship to stop turning and the HUD to catch up
SETTLE_NEAR = 0.3  # within NEAR_ANGLE the ship is still coasting from the last tap; wait it out
NEAR_ANGLE = 10.0
JUMP_SUSPECT = 25.0  # degrees; the ship can't really turn this far between two readings
ARRIVAL_PITCH = 6.0  # seconds of pitch up after arriving; 4 left the star filling the lower canopy
SCOOPABLE = set("KGBFOAM")
SCOOP_BELOW = 0.6  # scoop when the tank is under this fraction of capacity
SCOOP_FULL = 0.97
SCOOP_TIMEOUT = 120.0


class Aborted(Exception):
    pass


# -- keyboard -----------------------------------------------------------------

class _KeyBdInput(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        # Sized for the largest member (MOUSEINPUT) so SendInput accepts the struct.
        _fields_ = [("ki", _KeyBdInput), ("pad", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_SCANCODE = 0x1, 0x2, 0x8


class Keyboard:
    """Presses keys as scan codes. Releases everything it holds on close."""

    def __init__(self):
        self.held: set[int] = set()

    def _send(self, scan: int, up: bool) -> None:
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
        if scan > 0xFF:  # 0xE0xx: extended keys such as Home
            flags |= KEYEVENTF_EXTENDEDKEY
        inp = _Input()
        inp.type = 1  # INPUT_KEYBOARD
        inp.ki = _KeyBdInput(0, scan & 0xFF, flags, 0, 0)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

    def down(self, scan: int) -> None:
        self._send(scan, False)
        self.held.add(scan)

    def up(self, scan: int) -> None:
        self._send(scan, True)
        self.held.discard(scan)

    def release_all(self) -> None:
        for scan in list(self.held):
            self.up(scan)


def abort_pressed() -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(ABORT_VK) & 0x8000)


# -- steering -------------------------------------------------------------------

def steer(reading) -> dict[str, float]:
    """Which turn keys to hold, and for how long, to bring the compass dot to the centre.

    Behind the ship the dot still shows the quicker way round, so the same
    directions apply, just held longer.
    """
    if reading.behind:
        # Turn hardest on the axis the dot leans toward; pitch is the faster turn.
        if abs(reading.y) >= abs(reading.x) * 0.5 or (reading.x == 0 and reading.y == 0):
            return {"pitch_up" if reading.y >= 0 else "pitch_down": MAX_PULSE}
        return {"yaw_right" if reading.x > 0 else "yaw_left": MAX_PULSE}
    keys = {}
    # Ships yaw about a third as fast as they pitch, so yaw taps start longer.
    for value, pos, neg, base, gain in ((reading.y, "pitch_up", "pitch_down", MIN_PULSE, 0.30),
                                        (reading.x, "yaw_right", "yaw_left", MIN_PULSE * 1.5, 0.55)):
        if abs(value) > 0.02:
            keys[pos if value > 0 else neg] = min(MAX_PULSE, base + gain * abs(value))
    return keys


def read_status(journal_dir: Path) -> dict:
    try:
        return json.loads((journal_dir / "Status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def current_ship(journal_dir: Path) -> str | None:
    """The ship type from the newest journal's last Loadout or LoadGame, e.g. "ferdelance"."""
    from .sources import newest_journal

    path = newest_journal(journal_dir)
    ship = None
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"Loadout"' in line or '"LoadGame"' in line or '"ShipyardSwap"' in line:
                    event = json.loads(line)
                    ship = event.get("Ship") or event.get("ShipType") or ship
    except (OSError, TypeError, ValueError):
        pass
    return ship.lower() if ship else None


_grabber = None


def _screen():
    global _grabber
    if _grabber is None:
        from .capture import ScreenGrabber

        _grabber = ScreenGrabber()  # one per process: the capture library refuses a second
    return _grabber


def align(journal_dir: Path, timeout: float = 90.0, say=print, until=None,
          tolerance: float = ALIGNED) -> bool:
    """Turn to face the targeted system. True once aligned; raises Aborted if stopped.

    With `until` (a function called each step), keep the aim within `tolerance`
    instead of returning when aligned, and return True once `until()` is true.
    """
    from .capture import elite_window_rect
    from .compass import read_compass

    grabber, keys = _screen(), Keyboard()
    ship = current_ship(journal_dir)
    deadline = time.monotonic() + timeout
    steady = misses = 0
    last = suspect = None
    try:
        while time.monotonic() < deadline:
            if until is not None and until():
                return True
            if abort_pressed():
                raise Aborted("Right Ctrl pressed")
            rect = elite_window_rect()
            if rect is None:
                raise Aborted("Elite isn't the active window")
            problem = in_flight_problem(read_status(journal_dir))
            if problem:
                raise Aborted(problem)
            frame = grabber.grab(rect)
            near = (last.x, last.y) if last is not None else None
            reading = read_compass(frame, ship, near) if frame is not None else None
            if reading is None:
                misses += 1
                if misses > 40 and until is None:
                    raise Aborted("can't see the compass (no target selected?)")
                time.sleep(0.05)
                continue
            misses = 0
            # A glint can fake a dot for one frame. A big jump from the last
            # reading has to repeat before the ship acts on it.
            if last is not None and abs(reading.angle - last.angle) > JUMP_SUSPECT and suspect is None:
                suspect = reading
                time.sleep(0.03)
                continue
            suspect, last = None, reading
            if not reading.behind and reading.angle < tolerance:
                steady += 1
                if steady >= 3 and until is None:
                    say(f"Aligned, {reading.angle:.1f} degrees off.")
                    return True
                time.sleep(SETTLE)
                continue
            steady = 0
            plan = steer(reading)
            say(f"{'behind' if reading.behind else 'ahead '} {reading.angle:5.1f} deg  "
                + " ".join(f"{k} {t:.2f}s" for k, t in plan.items()))
            _pulse(keys, plan)
            time.sleep(SETTLE_NEAR if reading.angle < NEAR_ANGLE else SETTLE)
        say("Couldn't align in time.")
        return False
    finally:
        keys.release_all()


def _pulse(keys: Keyboard, plan: dict[str, float]) -> None:
    """Hold each key for its time, all starting together, watching for the abort key."""
    start = time.monotonic()
    for name in plan:
        keys.down(KEYS[name])
    try:
        for name, hold in sorted(plan.items(), key=lambda kv: kv[1]):
            while time.monotonic() - start < hold:
                if abort_pressed():
                    raise Aborted("Right Ctrl pressed")
                time.sleep(0.01)
            keys.up(KEYS[name])
    finally:
        keys.release_all()


# -- safety ------------------------------------------------------------------------

# Things that stop the frame shift drive and that a key press puts right.
FIXABLE = (
    (Flags.LANDING_GEAR_DOWN, LANDING_GEAR, "landing gear"),
    (Flags.CARGO_SCOOP_DEPLOYED, CARGO_SCOOP, "cargo scoop"),
    (Flags.HARDPOINTS_DEPLOYED, HARDPOINTS, "hardpoints"),
)


def in_flight_problem(status: dict) -> str | None:
    """A reason to stop flying at once, or None. Checked every step."""
    flags = status.get("Flags", 0)
    if flags & (Flags.DOCKED | Flags.LANDED):
        return "docked or landed"
    if flags & Flags.BEING_INTERDICTED:
        return "interdiction! Your ship, Commander"
    if flags & Flags.IN_DANGER:
        return "the game says we're in danger"
    if flags & Flags.OVERHEATING:
        return "overheating"
    return None


def preflight_problem(status: dict) -> str | None:
    """A reason the jump can't go ahead that the autopilot can't fix, or None."""
    problem = in_flight_problem(status)
    if problem:
        return problem
    flags, flags2 = status.get("Flags", 0), status.get("Flags2", 0)
    if not flags & Flags.IN_MAIN_SHIP or flags2 & 0b111:  # on foot, in a taxi, or multicrew
        return "not flying our own ship"
    if status.get("GuiFocus"):
        return "a panel or map is open; close it first"
    if flags & Flags.FSD_MASS_LOCKED:
        return "mass-locked; fly clear of the station or ship first"
    if flags & Flags.LOW_FUEL:
        return "fuel is low; scoop or refuel first"
    return None


def preflight(journal_dir: Path, keys: Keyboard, say=print) -> None:
    """Put right what a key press fixes (gear, cargo scoop, hardpoints); raise Aborted for the rest."""
    status = read_status(journal_dir)
    problem = preflight_problem(status)
    if problem:
        raise Aborted(problem)
    for flag, key, name in FIXABLE:
        if status.get("Flags", 0) & flag:
            say(f"Retracting the {name}.")
            _tap(keys, key)
            _wait(journal_dir, lambda f, flag=flag: not f & flag, 5, f"the {name} wouldn't retract")
            status = read_status(journal_dir)


# -- one jump ---------------------------------------------------------------------

def jump_target(events: list[dict], status: dict) -> tuple[str, str] | None:
    """(target system, current system) when a different star system is targeted, else None."""
    here = target = None
    for e in events:
        if e.get("event") in ("Location", "FSDJump", "CarrierJump", "SupercruiseEntry", "SupercruiseExit"):
            here = (e.get("StarSystem"), e.get("SystemAddress"))
        elif e.get("event") == "FSDTarget":
            target = (e.get("Name"), e.get("SystemAddress"))
    dest = status.get("Destination") or {}
    if not target or dest.get("System") != target[1] or dest.get("Body"):
        return None  # nothing targeted, or a body or station in this system
    if here and here[1] == target[1]:
        return None
    return target[0], here[0] if here else "?"


def jump_once(journal_dir: Path, say=print) -> bool:
    """Jump to the targeted system, then clear the arrival star. True on arrival; raises Aborted if stopped."""
    from .sources import JournalTailer

    tail = JournalTailer(journal_dir)
    target = jump_target(tail.read_existing(), read_status(journal_dir))
    if target is None:
        raise Aborted("no other star system is targeted")
    keys = Keyboard()
    try:
        _jump(journal_dir, keys, tail, target, say)
        _clear_star(keys)
        say("Star cleared. Holding at zero throttle.")
        return True
    finally:
        keys.release_all()


def _jump(journal_dir: Path, keys: Keyboard, tail, target: tuple[str, str], say) -> tuple[dict, str]:
    """Checks, align, full throttle, J, hold the aim, ride it out. Returns (FSDJump event, star class)."""
    say(f"Jump: {target[1]} to {target[0]}.")
    preflight(journal_dir, keys, say)
    _wait(journal_dir, lambda f: not f & Flags.FSD_COOLDOWN, 60, "the drive is still cooling down")
    if not align(journal_dir, say=say):
        raise Aborted("couldn't line up")
    say("Aligned.")
    _tap(keys, THROTTLE_FULL)
    if read_status(journal_dir).get("Flags", 0) & Flags.FSD_CHARGING:
        say("Full throttle. The drive was already charging.")  # J again would cancel it
    else:
        _tap(keys, JUMP)
        say("Full throttle. Frame shift drive charging.")
    jumped = arrived = None

    def started():
        nonlocal jumped
        for e in tail.poll():
            if e.get("event") == "StartJump" and e.get("JumpType") == "Hyperspace":
                jumped = e
        return jumped is not None

    # Hold the aim through the charge: moving forward drifts the nose.
    if not align(journal_dir, timeout=45, say=lambda _: None, until=started, tolerance=HOLD_ALIGNED):
        raise Aborted("the jump didn't start")
    star = jumped.get("StarClass", "")
    say(f"Jumping to {jumped.get('StarSystem', target[0])} (class {star or '?'} star).")
    deadline = time.monotonic() + 90
    while not arrived:
        if time.monotonic() > deadline:
            raise Aborted("no arrival in the journal")
        for e in tail.poll():
            if e.get("event") == "FSDJump":
                arrived = e
        time.sleep(0.2)
    say(f"Arrived in {arrived.get('StarSystem')}.")
    time.sleep(1.5)  # the arrival animation swallows the first moments of input
    _tap(keys, THROTTLE_ZERO)
    return arrived, star


def _clear_star(keys: Keyboard) -> None:
    _tap(keys, THROTTLE_ZERO)
    _pulse(keys, {"pitch_up": ARRIVAL_PITCH})


# -- along a route ------------------------------------------------------------------

def ship_fit(events: list[dict]) -> dict:
    """From the last Loadout: ship, fuel capacity, whether a fuel scoop is fitted,
    and the most fuel a recent jump has used in it."""
    fit = {"ship": None, "capacity": None, "scoop": False, "jump_fuel": None}
    used: list[float] = []
    for e in events:
        if e.get("event") == "Loadout":
            fit = {"ship": e.get("Ship"), "capacity": (e.get("FuelCapacity") or {}).get("Main"),
                   "scoop": any("fuelscoop" in m.get("Item", "").lower() for m in e.get("Modules", [])),
                   "jump_fuel": None}
            used = []
        elif e.get("event") == "FSDJump" and e.get("FuelUsed"):
            used.append(float(e["FuelUsed"]))
    if used:
        fit["jump_fuel"] = max(used[-10:])
    return fit


def fuel_for_next_jump(fit: dict, fuel: float) -> bool:
    """Enough fuel for another jump, with a margin. Unknown usage assumes a quarter tank."""
    need = fit["jump_fuel"] or (fit["capacity"] or 8.0) * 0.25
    return fuel >= need * 1.3


def read_route(journal_dir: Path) -> list[str]:
    try:
        route = json.loads((journal_dir / "NavRoute.json").read_text(encoding="utf-8")).get("Route") or []
    except (OSError, ValueError):
        return []
    return [hop.get("StarSystem") for hop in route]


def fly_route(journal_dir: Path, say=print) -> bool:
    """Jump along the plotted route to its end, scooping on the way if the ship can.
    True on arrival at the final system; raises Aborted if stopped."""
    from .sources import JournalTailer

    tail = JournalTailer(journal_dir)
    events = tail.read_existing()
    route = read_route(journal_dir)
    if len(route) < 2:
        raise Aborted("no route plotted")
    destination = route[-1]
    say(f"Route to {destination}: {len(route) - 1} jumps.")
    keys = Keyboard()
    try:
        while True:
            status = read_status(journal_dir)
            fit = ship_fit(events)
            target = jump_target(events, status)
            if target is None:
                # The game normally targets the next hop after each jump; if not, ask it to.
                _tap(keys, TARGET_NEXT)
                time.sleep(1.0)
                events += tail.poll()
                target = jump_target(events, read_status(journal_dir))
                if target is None:
                    raise Aborted("couldn't target the next system in the route")
            fuel = (read_status(journal_dir).get("Fuel") or {}).get("FuelMain", 0.0)
            if not fuel_for_next_jump(fit, fuel):
                raise Aborted(f"{fuel:.1f} t of fuel isn't enough for the next jump"
                              + ("" if fit["scoop"] else " and there's no fuel scoop fitted"))
            arrived, star = _jump(journal_dir, keys, tail, target, say)
            events = tail.read_existing()  # the game may have started a new journal file mid-route
            if arrived.get("StarSystem") == destination:
                _clear_star(keys)
                say(f"Arrived at {destination}. Route complete. Holding at zero throttle.")
                return True
            fuel = arrived.get("FuelLevel", 0.0)
            capacity = fit["capacity"] or 0
            if fit["scoop"] and star[:1] in SCOOPABLE and capacity and fuel < capacity * SCOOP_BELOW:
                _scoop(journal_dir, keys, capacity, say)
            else:
                _clear_star(keys)
            remaining = len(route) - 1 - route.index(arrived.get("StarSystem")) if arrived.get("StarSystem") in route else "?"
            say(f"{remaining} jumps to go.")
    finally:
        keys.release_all()


def _scoop(journal_dir: Path, keys: Keyboard, capacity: float, say) -> None:
    """Skim the arrival star until the tank is full. The ship arrives facing the star,
    close enough to scoop: turn it below the nose, creep along, and back off if hot."""
    say("Scooping fuel.")
    _pulse(keys, {"pitch_up": ARRIVAL_PITCH / 2})  # star at the bottom of the canopy
    _tap(keys, THROTTLE_25)
    deadline = time.monotonic() + SCOOP_TIMEOUT
    last_scooping = time.monotonic()
    while time.monotonic() < deadline:
        _check_abort()
        status = read_status(journal_dir)
        flags, fuel = status.get("Flags", 0), (status.get("Fuel") or {}).get("FuelMain", 0.0)
        if flags & Flags.BEING_INTERDICTED:
            raise Aborted("interdiction! Your ship, Commander")
        if flags & Flags.OVERHEATING:
            say("Too hot. Backing off the star.")
            _tap(keys, THROTTLE_75)
            _pulse(keys, {"pitch_up": 1.5})
            break
        if fuel >= capacity * SCOOP_FULL:
            say(f"Tank full, {fuel:.1f} t.")
            break
        if flags & Flags.SCOOPING_FUEL:
            last_scooping = time.monotonic()
        elif time.monotonic() - last_scooping > 15:
            say(f"Out of scooping range; stopping at {fuel:.1f} t.")
            break
        time.sleep(0.5)
    _clear_star(keys)


# -- small helpers --------------------------------------------------------------------

def _check_abort() -> None:
    from .capture import elite_window_rect

    if abort_pressed():
        raise Aborted("Right Ctrl pressed")
    if elite_window_rect() is None:
        raise Aborted("Elite isn't the active window")


def _wait(journal_dir: Path, ready, timeout: float, why: str) -> None:
    deadline = time.monotonic() + timeout
    while not ready(read_status(journal_dir).get("Flags", 0)):
        if time.monotonic() > deadline:
            raise Aborted(why)
        _check_abort()
        time.sleep(0.2)


def _tap(keys: Keyboard, scan: int, hold: float = 0.1) -> None:
    keys.down(scan)
    time.sleep(hold)
    keys.up(scan)

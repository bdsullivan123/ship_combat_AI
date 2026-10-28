"""Rules that turn ship-state changes into spoken alerts.

Everything here is deterministic and local: no network, no AI model. Each
alert has a key (for cooldowns) and a priority (for interrupting speech).
`now` is always passed in, so a replayed log behaves exactly like live play.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .flags import Flags
from .state import ShipState, ammo_kind


class Priority(IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class Alert:
    key: str
    text: str
    priority: Priority
    time: float


# Seconds before the same key may speak again. A more urgent alert on the
# same key always gets through (hull 55% -> hull 18% must not be swallowed).
COOLDOWNS = {
    "shields_down": 5,
    "shields_up": 5,
    "hull": 2,
    "heat": 20,
    "heat_damage": 10,
    "low_fuel": 300,
    "interdiction": 30,
    "canopy": 30,
    "fighter": 10,
}

# "Taking fire" only speaks at the start of an engagement: after this many
# seconds without being shot at, the next hit counts as a new engagement.
ATTACK_QUIET_PERIOD = 60.0

DANGEROUS_RANKS = {"Dangerous", "Deadly", "Elite"}

# Screen-read (Stage 2) thresholds.
HEAT_WARNING = 85  # percent; the game's own overheating flag only fires past 100
AMMO_STEPS = (50, 25, 10, 0)  # percent of full, announced once each per magazine load
UTILITY_NAMES = {"heatsink": "Heat sinks", "chaff": "Chaff", "shieldcell": "Shield cells", "ecm": "ECM"}
WEAPON_NAMES = {"multicannon": "Multicannon", "railgun": "Railgun", "fragcannon": "Frag cannon",
                "cannon": "Cannon", "missile": "Missile", "torpedo": "Torpedo", "mine": "Mine",
                "plasmaaccelerator": "Plasma accelerator"}


def spoken_percent(fraction: float) -> int:
    return max(0, round(fraction * 100))


def spoken_credits(amount: int) -> str:
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f} million credits".replace(".0 ", " ")
    if amount >= 1_000:
        return f"{round(amount / 1_000)} thousand credits"
    return f"{amount} credits"


class AlertEngine:
    def __init__(self, cooldowns: dict[str, float] | None = None, attack_quiet: float = ATTACK_QUIET_PERIOD):
        self.cooldowns = dict(COOLDOWNS if cooldowns is None else cooldowns)
        self.attack_quiet = attack_quiet
        self._last_spoken: dict[str, tuple[float, Priority]] = {}
        self._last_attacked: float | None = None
        self._announced_targets: set[tuple] = set()
        self._hud_prev = None
        self._ammo_announced: dict[str, set[int]] = {}
        self._ammo_seen_max: dict[tuple, int] = {}
        self._dry_announced: set[tuple] = set()

    # -- inputs ---------------------------------------------------------

    def on_status(self, rising: Flags, falling: Flags, state: ShipState, now: float) -> list[Alert]:
        if not state.flying or state.self_destructing:
            return []
        out: list[Alert] = []
        craft_changed = (rising | falling) & (Flags.IN_MAIN_SHIP | Flags.IN_FIGHTER)
        if not craft_changed:
            if falling & Flags.SHIELDS_UP:
                out.append(self._shields_down(state, now))
            if rising & Flags.SHIELDS_UP:
                out.append(Alert("shields_up", "Shields back online.", Priority.NORMAL, now))
        if rising & Flags.OVERHEATING:
            out.append(self._overheating(state, now))
        if rising & Flags.LOW_FUEL:
            out.append(Alert("low_fuel", "Fuel low.", Priority.HIGH, now))
        if rising & Flags.BEING_INTERDICTED:
            out.append(Alert("interdiction", "Interdiction! Evade or submit.", Priority.CRITICAL, now))
        return self._gate(out)

    def on_journal(self, e: dict, state: ShipState, now: float) -> list[Alert]:
        ev = e.get("event")
        if ev in ("LoadGame", "Died", "SupercruiseEntry", "FSDJump"):
            self._announced_targets.clear()
            self._last_attacked = None
        if state.self_destructing:
            return []
        out: list[Alert] = []

        if ev == "ShieldState":
            if e.get("ShieldsUp"):
                out.append(Alert("shields_up", "Shields back online.", Priority.NORMAL, now))
            else:
                out.append(self._shields_down(state, now))
        elif ev == "HullDamage":
            out.append(self._hull(e, state, now))
        elif ev == "HeatWarning":
            out.append(self._overheating(state, now))
        elif ev == "HeatDamage":
            sink = state.has_module("heatsinklauncher") and state.heatsinks_left != 0
            out.append(Alert("heat_damage", "Heat damage! Fire a heat sink or cool down now."
                             if sink else "Heat damage! Cool down now.",
                             Priority.CRITICAL, now))
        elif ev == "UnderAttack":
            out.extend(self._under_attack(e, now))
        elif ev == "ShipTargeted":
            out.extend(self._target(state, now))
        elif ev == "Interdicted":
            who = e.get("Interdictor_Localised") or e.get("Interdictor")
            out.append(Alert("interdiction", f"Interdicted by {who}." if who else "Interdicted.",
                             Priority.HIGH, now))
        elif ev == "CockpitBreached":
            out.append(Alert("canopy", "Canopy breached. Life support on reserve.", Priority.CRITICAL, now))
        elif ev == "FighterDestroyed":
            out.append(Alert("fighter", "Fighter destroyed.", Priority.HIGH, now))
        elif ev == "Bounty":
            reward = e.get("TotalReward") or e.get("Reward") or 0
            out.append(Alert("kill", f"Kill confirmed. {spoken_credits(reward)}.", Priority.LOW, now))
        elif ev == "FactionKillBond":
            out.append(Alert("kill", f"Kill confirmed. {spoken_credits(e.get('Reward', 0))} in bonds.",
                             Priority.LOW, now))
        elif ev == "Died":
            out.append(Alert("died", "Ship lost.", Priority.LOW, now))
        return self._gate(out)

    def on_hud(self, reading, state: ShipState, now: float) -> list[Alert]:
        """Alerts from a screen reading (hud.HudReading).

        Screen reads can be wrong, so a condition must hold in two reads in a
        row before anything is said. One misread "1200" as "120" stays quiet.
        """
        prev, self._hud_prev = self._hud_prev, reading
        if prev is None or state.self_destructing:
            return []
        out: list[Alert] = []

        if (reading.heat is not None and prev.heat is not None
                and min(reading.heat, prev.heat) >= HEAT_WARNING
                and not state.flags & Flags.OVERHEATING):
            out.append(Alert("heat", f"Heat {min(reading.heat, prev.heat)} percent.", Priority.HIGH, now))

        now_by_kind, prev_by_kind = _ammo_by_kind(reading.weapons), _ammo_by_kind(prev.weapons)
        for kind, cur in now_by_kind.items():  # in panel order, so alerts come out in a stable order
            old = prev_by_kind.get(kind)
            if old is None:
                continue
            if len(cur) != len(old):
                continue  # a line was missed in one read; positions can't be matched
            out.extend(self._ammo(kind, cur, old, state, now))
        return self._gate(out)

    def _ammo(self, kind: str, cur: list[int], old: list[int], state: ShipState, now: float) -> list[Alert]:
        total = max(sum(cur), sum(old))  # the higher read: both must agree it's low
        if kind in UTILITY_NAMES:
            announced = self._ammo_announced.setdefault(kind, set())
            if total == 0 and 0 not in announced:
                announced.add(0)
                return [Alert(f"ammo:{kind}", f"{UTILITY_NAMES[kind]} empty.", Priority.NORMAL, now)]
            if total > 0:
                announced.clear()
            return []

        name = WEAPON_NAMES.get(kind, kind.title())
        # The panel shows only the current fire group, which may hold fewer
        # guns of this kind than the ship has, so "full" is tracked per group.
        group = (state.fire_group, kind, len(cur))
        out = []
        for i, (a, b) in enumerate(zip(cur, old)):
            gun = (*group, i)
            if max(a, b) == 0 and gun not in self._dry_announced and len(cur) > 1:
                self._dry_announced.add(gun)
                out.append(Alert(f"dry:{kind}:{i}", f"One {name.lower()} is dry.", Priority.NORMAL, now))
            elif min(a, b) > 0:
                self._dry_announced.discard(gun)

        self._ammo_seen_max[group] = max(self._ammo_seen_max.get(group, 0), min(sum(cur), sum(old)))
        loadout = state.ammo_capacity.get(kind, [])
        full = max(sum(loadout) if len(loadout) == len(cur) else 0, self._ammo_seen_max[group])
        if not full:
            return out
        pct = 100 * total / full
        announced = self._ammo_announced.setdefault(kind, set())
        announced -= {step for step in announced if pct > step + 5}  # rearmed or synthesised
        crossed = [step for step in AMMO_STEPS if pct <= step and step not in announced]
        if crossed:
            step = min(crossed)
            announced.update(st for st in AMMO_STEPS if st >= step)
            text = f"{name} ammo out." if step == 0 else f"{name} ammo {step} percent."
            priority = Priority.HIGH if step <= 10 else Priority.NORMAL
            out.append(Alert(f"ammo:{kind}", text, priority, now))
        return out

    # -- rules ----------------------------------------------------------

    def _shields_down(self, state: ShipState, now: float) -> Alert:
        text = "Shields down."
        if state.pips and state.pips[0] < 8:
            text += " Four pips to systems."
        return Alert("shields_down", text, Priority.HIGH, now)

    def _overheating(self, state: ShipState, now: float) -> Alert:
        text = "Overheating."
        if state.flags & Flags.SILENT_RUNNING:
            text += " Silent running is on."
        elif state.heatsinks_left == 0:
            text += " No heat sinks left."
        elif state.has_module("heatsinklauncher"):
            text += " Heat sink advised."
        return Alert("heat", text, Priority.HIGH, now)

    def _hull(self, e: dict, state: ShipState, now: float) -> Alert:
        health = e.get("Health", 1.0)
        pct = spoken_percent(health)
        if e.get("Fighter"):
            who = "Fighter hull"
        elif not e.get("PlayerPilot"):
            who = "Mothership hull"
        else:
            who = "Hull"
        if health < 0.25:
            text = f"{who} critical, {pct} percent. Disengage now."
            if who == "Hull" and state.pips and state.pips[1] < 8:
                text += " Four pips to engines."
            return Alert("hull", text, Priority.CRITICAL, now)
        if health < 0.5:
            return Alert("hull", f"{who} {pct} percent. Consider disengaging.", Priority.HIGH, now)
        return Alert("hull", f"{who} {pct} percent.", Priority.NORMAL, now)

    def _under_attack(self, e: dict, now: float) -> list[Alert]:
        first_hit = self._last_attacked is None or now - self._last_attacked >= self.attack_quiet
        self._last_attacked = now
        if not first_hit:
            return []
        victim = e.get("Target", "You")
        text = {"Fighter": "Our fighter is under attack.",
                "Mothership": "The mothership is under attack."}.get(victim, "Taking fire.")
        return [Alert("under_attack", text, Priority.NORMAL, now)]

    def _target(self, state: ShipState, now: float) -> list[Alert]:
        t = state.target
        if t is None or t.scan_stage < 3 or t.key in self._announced_targets:
            return []
        self._announced_targets.add(t.key)
        if t.legal_status in (None, "Clean"):
            return []
        parts = [f"{t.legal_status} {t.ship}."]
        if t.rank in DANGEROUS_RANKS:
            parts.append(f"Caution, {t.rank} pilot.")
        if t.shield is not None and t.shield < 99.5:
            parts.append(f"Shields {round(t.shield)} percent.")
        if t.hull is not None and t.hull < 99.5:
            parts.append(f"Hull {round(t.hull)} percent.")
        if t.bounty:
            parts.append(f"Bounty {spoken_credits(t.bounty)}.")
        return [Alert("target", " ".join(parts), Priority.LOW, now)]

    # -- stock phrases --------------------------------------------------

    @classmethod
    def stock_phrases(cls) -> list[str]:
        """Every fixed line the rules can say, most urgent first.

        Built by running the real rules, so it can't drift from them. The
        neural voice renders these ahead of time so they play instantly.
        """
        engine = cls()
        low_pips, full_pips = ShipState(pips=(4, 4, 4)), ShipState(pips=(8, 8, 8))
        sink = ShipState(modules=["hpt_heatsinklauncher_turret_tiny"])
        no_sinks = ShipState(modules=["hpt_heatsinklauncher_turret_tiny"], heatsinks_left=0)
        silent = ShipState(flags=Flags.SILENT_RUNNING)
        phrases: list[str] = []
        for h in range(25):
            phrases.append(engine._hull({"Health": h / 100, "PlayerPilot": True}, low_pips, 0).text)
            phrases.append(engine._hull({"Health": h / 100, "PlayerPilot": True}, full_pips, 0).text)
        phrases += [
            "Heat damage! Fire a heat sink or cool down now.", "Heat damage! Cool down now.",
            "Interdiction! Evade or submit.", "Canopy breached. Life support on reserve.",
        ]
        for state in (low_pips, full_pips):
            phrases.append(engine._shields_down(state, 0).text)
        for state in (full_pips, sink, no_sinks, silent):
            phrases.append(engine._overheating(state, 0).text)
        phrases += [f"Heat {h} percent." for h in range(HEAT_WARNING, 101)]
        for name in WEAPON_NAMES.values():
            phrases += [f"{name} ammo {step} percent." for step in AMMO_STEPS if step]
            phrases += [f"{name} ammo out.", f"One {name.lower()} is dry."]
        phrases += [f"{name} empty." for name in UTILITY_NAMES.values()]
        phrases += ["Fuel low.", "Fighter destroyed.", "Shields back online.", "Taking fire."]
        for h in range(25, 101):
            phrases.append(engine._hull({"Health": h / 100, "PlayerPilot": True}, full_pips, 0).text)
        phrases += ["Ship lost.", "Our fighter is under attack.", "The mothership is under attack."]
        return list(dict.fromkeys(phrases))

    # -- cooldowns ------------------------------------------------------

    def _gate(self, alerts: list[Alert]) -> list[Alert]:
        allowed = []
        for a in alerts:
            last = self._last_spoken.get(a.key)
            cooldown = self.cooldowns.get(a.key, 0)
            if last and a.time - last[0] < cooldown and a.priority <= last[1]:
                continue
            self._last_spoken[a.key] = (a.time, a.priority)
            allowed.append(a)
        return allowed


def _ammo_by_kind(weapons) -> dict[str, list[int]]:
    """{"multicannon": [total per gun, top to bottom], ...}"""
    out: dict[str, list[int]] = {}
    for w in weapons:
        out.setdefault(ammo_kind(w.name), []).append(w.total)
    return out

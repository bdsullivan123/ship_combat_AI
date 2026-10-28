"""Rules that turn ship-state changes into spoken alerts.

Everything here is deterministic and local: no network, no AI model. Each
alert has a key (for cooldowns) and a priority (for interrupting speech).
`now` is always passed in, so a replayed log behaves exactly like live play.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from .flags import Flags
from .state import ShipState


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
            out.append(Alert("heat_damage", "Heat damage! Fire a heat sink or cool down now."
                             if state.has_module("heatsinklauncher") else "Heat damage! Cool down now.",
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

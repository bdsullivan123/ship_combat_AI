"""The live picture of the ship, built from Status.json and journal events."""

from __future__ import annotations

from dataclasses import dataclass, field

from .flags import Flags, Flags2

# Journal names for ships that sometimes arrive without a _Localised field.
SHIP_NAMES = {
    "ferdelance": "Fer-de-Lance",
    "python": "Python",
    "python_nx": "Python Mk II",
    "anaconda": "Anaconda",
    "federation_corvette": "Federal Corvette",
    "cutter": "Imperial Cutter",
    "krait_mkii": "Krait Mk II",
    "krait_light": "Krait Phantom",
    "empire_trader": "Imperial Clipper",
    "empire_eagle": "Imperial Eagle",
    "empire_courier": "Imperial Courier",
    "federation_dropship": "Federal Dropship",
    "federation_dropship_mkii": "Federal Assault Ship",
    "federation_gunship": "Federal Gunship",
    "independant_trader": "Keelback",
    "independent_fighter": "Taipan",
    "asp": "Asp Explorer",
    "asp_scout": "Asp Scout",
    "viper": "Viper Mk III",
    "viper_mkiv": "Viper Mk IV",
    "cobramkiii": "Cobra Mk III",
    "cobramkiv": "Cobra Mk IV",
    "diamondback": "Diamondback Scout",
    "diamondbackxl": "Diamondback Explorer",
    "type6": "Type-6 Transporter",
    "type7": "Type-7 Transporter",
    "type9": "Type-9 Heavy",
    "type9_military": "Type-10 Defender",
    "typex": "Alliance Chieftain",
    "typex_2": "Alliance Crusader",
    "typex_3": "Alliance Challenger",
    "vulture": "Vulture",
    "mamba": "Mamba",
    "orca": "Orca",
    "belugaliner": "Beluga Liner",
    "dolphin": "Dolphin",
    "eagle": "Eagle",
    "hauler": "Hauler",
    "adder": "Adder",
    "sidewinder": "Sidewinder",
    "mandalay": "Mandalay",
}


def ship_display_name(event: dict, key: str = "Ship") -> str:
    localised = event.get(f"{key}_Localised")
    if localised:
        return localised
    internal = str(event.get(key, "")).lower()
    return SHIP_NAMES.get(internal, internal.replace("_", " ").title() or "unknown ship")


@dataclass
class Target:
    ship: str
    scan_stage: int = 0
    pilot: str | None = None
    rank: str | None = None
    shield: float | None = None  # percent, 0-100, as of the last scan update
    hull: float | None = None
    legal_status: str | None = None
    bounty: int | None = None
    faction: str | None = None

    @property
    def key(self) -> tuple:
        return (self.ship, self.pilot)


@dataclass
class ShipState:
    commander: str | None = None
    ship: str | None = None
    ship_type: str | None = None  # journal's internal name, e.g. "ferdelance"
    flags: Flags = Flags(0)
    flags2: Flags2 = Flags2(0)
    pips: tuple[int, int, int] | None = None  # SYS, ENG, WEP in half-pips (0-8)
    fire_group: int | None = None
    gui_focus: int = 0  # 0 = cockpit view; anything else is a panel or map covering the HUD
    fuel_main: float | None = None
    fuel_reservoir: float | None = None
    fuel_capacity: float | None = None
    legal_state: str | None = None
    hull: float | None = None  # 0-1
    shields_up: bool | None = None
    modules: list[str] = field(default_factory=list)
    ammo_capacity: dict[str, list[int]] = field(default_factory=dict)  # rounds per gun when full
    heatsinks_left: int | None = None  # read off the HUD; None until seen
    target: Target | None = None
    in_combat: bool = False
    self_destructing: bool = False
    status_seen: bool = False

    @property
    def flying(self) -> bool:
        """In a ship or fighter and actually in space, not sitting on a pad."""
        if not self.status_seen:
            return True  # Journal-only (e.g. replaying a log): assume yes.
        in_craft = self.flags & (Flags.IN_MAIN_SHIP | Flags.IN_FIGHTER)
        parked = self.flags & (Flags.DOCKED | Flags.LANDED)
        return bool(in_craft) and not parked

    def has_module(self, fragment: str) -> bool:
        return any(fragment in m for m in self.modules)

    def apply_status(self, status: dict) -> tuple[Flags, Flags]:
        """Update from Status.json. Returns (flags that turned on, flags that turned off)."""
        new = Flags(status.get("Flags", 0))
        if self.status_seen:
            rising, falling = new & ~self.flags, self.flags & ~new
        else:
            rising = falling = Flags(0)  # First read: nothing "changed".
        self.status_seen = True
        self.flags = new
        self.flags2 = Flags2(status.get("Flags2", 0))
        pips = status.get("Pips")
        self.pips = tuple(pips) if pips and len(pips) == 3 else None
        self.fire_group = status.get("FireGroup")
        self.gui_focus = status.get("GuiFocus", 0)
        fuel = status.get("Fuel")
        if fuel:
            self.fuel_main = fuel.get("FuelMain")
            self.fuel_reservoir = fuel.get("FuelReservoir")
        self.legal_state = status.get("LegalState", self.legal_state)
        if new & (Flags.IN_MAIN_SHIP | Flags.IN_FIGHTER):
            self.shields_up = bool(new & Flags.SHIELDS_UP)
        return rising, falling

    def apply_hud(self, reading) -> None:
        sink = reading.utility("HEATSINK")
        if sink is not None:
            self.heatsinks_left = sink.total

    def apply_journal(self, e: dict) -> None:
        ev = e.get("event")
        if ev == "LoadGame":
            self.commander = e.get("Commander")
            self.ship = ship_display_name(e)
            self.ship_type = str(e.get("Ship", "")).lower() or None
            self.fuel_capacity = e.get("FuelCapacity")
            self.target = None
            self.in_combat = False
            self.self_destructing = False
        elif ev == "Loadout":
            self.ship = ship_display_name(e)
            self.ship_type = str(e.get("Ship", "")).lower() or None
            self.hull = e.get("HullHealth", self.hull)
            self.modules = [m.get("Item", "").lower() for m in e.get("Modules", [])]
            self.ammo_capacity = ammo_capacity(e.get("Modules", []))
            cap = e.get("FuelCapacity")
            if isinstance(cap, dict):
                self.fuel_capacity = cap.get("Main")
        elif ev == "HullDamage" and e.get("PlayerPilot") and not e.get("Fighter"):
            self.hull = e.get("Health")
        elif ev == "ShieldState":
            self.shields_up = bool(e.get("ShieldsUp"))
        elif ev == "ShipTargeted":
            self.target = target_from_event(e)
        elif ev == "Music":
            self.in_combat = str(e.get("MusicTrack", "")).startswith("Combat")
        elif ev == "SelfDestruct":
            self.self_destructing = True
        elif ev in ("Died", "Resurrect"):
            self.target = None
            self.in_combat = False
            if ev == "Resurrect":
                self.self_destructing = False
                self.hull = 1.0
        elif ev == "RepairAll":
            self.hull = 1.0


def ammo_kind(name: str) -> str:
    """'MULTI-CANNON' (HUD) and 'hpt_multicannon_gimbal_huge' (journal) -> 'multicannon'."""
    letters = "".join(c for c in name.lower() if c.isalpha())
    if "fragment" in letters or "slugshot" in letters:  # the journal calls frag cannons "slugshot"
        return "fragcannon"
    for kind in ("multicannon", "railgun", "plasmaaccelerator", "cannon", "missile",
                 "torpedo", "mine", "heatsink", "chaff", "shieldcell", "ecm"):
        if kind in letters:
            return kind
    return letters


def ammo_capacity(modules: list[dict]) -> dict[str, list[int]]:
    """Rounds per gun, by kind: {"multicannon": [2190, 2190, 2190]}.

    Loadout is usually written after rearming, so this is normally "full".
    If it was written mid-flight it undercounts; the alert rules take the
    larger of this and what they have seen on the HUD.
    """
    guns: dict[str, list[int]] = {}
    for m in modules:
        if "AmmoInHopper" in m:
            kind = ammo_kind(m.get("Item", ""))
            guns.setdefault(kind, []).append(m.get("AmmoInClip", 0) + m["AmmoInHopper"])
    return guns


def target_from_event(e: dict) -> Target | None:
    if not e.get("TargetLocked"):
        return None
    return Target(
        ship=ship_display_name(e),
        scan_stage=e.get("ScanStage", 0),
        pilot=e.get("PilotName_Localised") or e.get("PilotName"),
        rank=e.get("PilotRank"),
        shield=e.get("ShieldHealth"),
        hull=e.get("HullHealth"),
        legal_status=e.get("LegalStatus"),
        bounty=e.get("Bounty"),
        faction=e.get("Faction"),
    )

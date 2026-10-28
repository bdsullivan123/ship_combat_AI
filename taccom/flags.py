"""Bit flags from Status.json, as documented in the Elite Dangerous journal manual."""

from enum import IntFlag


class Flags(IntFlag):
    DOCKED = 1 << 0
    LANDED = 1 << 1
    LANDING_GEAR_DOWN = 1 << 2
    SHIELDS_UP = 1 << 3
    SUPERCRUISE = 1 << 4
    FLIGHT_ASSIST_OFF = 1 << 5
    HARDPOINTS_DEPLOYED = 1 << 6
    IN_WING = 1 << 7
    LIGHTS_ON = 1 << 8
    CARGO_SCOOP_DEPLOYED = 1 << 9
    SILENT_RUNNING = 1 << 10
    SCOOPING_FUEL = 1 << 11
    SRV_HANDBRAKE = 1 << 12
    SRV_TURRET_VIEW = 1 << 13
    SRV_TURRET_RETRACTED = 1 << 14
    SRV_DRIVE_ASSIST = 1 << 15
    FSD_MASS_LOCKED = 1 << 16
    FSD_CHARGING = 1 << 17
    FSD_COOLDOWN = 1 << 18
    LOW_FUEL = 1 << 19
    OVERHEATING = 1 << 20
    HAS_LAT_LONG = 1 << 21
    IN_DANGER = 1 << 22
    BEING_INTERDICTED = 1 << 23
    IN_MAIN_SHIP = 1 << 24
    IN_FIGHTER = 1 << 25
    IN_SRV = 1 << 26
    HUD_ANALYSIS_MODE = 1 << 27
    NIGHT_VISION = 1 << 28
    ALTITUDE_FROM_AVERAGE_RADIUS = 1 << 29
    FSD_JUMP = 1 << 30
    SRV_HIGH_BEAM = 1 << 31


class Flags2(IntFlag):
    ON_FOOT = 1 << 0
    IN_TAXI = 1 << 1
    IN_MULTICREW = 1 << 2
    ON_FOOT_IN_STATION = 1 << 3
    ON_FOOT_ON_PLANET = 1 << 4
    AIM_DOWN_SIGHT = 1 << 5
    LOW_OXYGEN = 1 << 6
    LOW_HEALTH = 1 << 7
    COLD = 1 << 8
    HOT = 1 << 9
    VERY_COLD = 1 << 10
    VERY_HOT = 1 << 11
    GLIDE_MODE = 1 << 12
    ON_FOOT_IN_HANGAR = 1 << 13
    ON_FOOT_SOCIAL_SPACE = 1 << 14
    ON_FOOT_EXTERIOR = 1 << 15
    BREATHABLE_ATMOSPHERE = 1 << 16
    TELEPRESENCE_MULTICREW = 1 << 17
    PHYSICAL_MULTICREW = 1 << 18
    FSD_HYPERDRIVE_CHARGING = 1 << 19

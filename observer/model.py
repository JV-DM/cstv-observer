"""Core data model shared by the live GSI path and the offline demo benchmark."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

Vec = tuple[float, float, float]

STEAM64_BASE = 76561197960265728


def parse_vec(s: str | None) -> Vec | None:
    if not s:
        return None
    try:
        x, y, z = (float(v) for v in s.split(","))
        return (x, y, z)
    except ValueError:
        return None


def sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def norm(a: Vec) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def dist_weighted(a: Vec, b: Vec, z_weight: float = 1.6) -> float:
    """Distance with vertical separation exaggerated: players on different floors
    are usually not in a fight even when their XY distance is small."""
    dx, dy, dz = a[0] - b[0], a[1] - b[1], (a[2] - b[2]) * z_weight
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def fwd_from_angles(pitch_deg: float, yaw_deg: float) -> Vec:
    """Source engine convention: positive pitch looks down."""
    p, y = math.radians(pitch_deg), math.radians(yaw_deg)
    cp = math.cos(p)
    return (cp * math.cos(y), cp * math.sin(y), -math.sin(p))


def steam64_to_accountid(steamid: str) -> int | None:
    try:
        v = int(steamid)
    except (TypeError, ValueError):
        return None
    if v <= STEAM64_BASE:
        return None
    return v - STEAM64_BASE


# --------------------------------------------------------------------------- weapons
_SNIPER = ("awp", "ssg08", "g3sg1", "scar20")
_PISTOL = ("glock", "usp", "hkp2000", "p2000", "p250", "fiveseven", "tec9", "cz75",
           "deagle", "desert", "elite", "dualberettas", "revolver", "r8")
_SMG = ("mac10", "mp9", "mp7", "mp5", "ump45", "ump", "p90", "bizon")
_SHOTGUN = ("nova", "xm1014", "mag7", "sawedoff")
_UTILITY = ("grenade", "flashbang", "molotov", "incgrenade", "incendiary", "decoy", "smoke", "c4", "taser", "zeus")

# effective engagement range in Hammer units, per weapon class
WEAPON_RANGE = {
    "sniper": 4500.0, "rifle": 3000.0, "mg": 2600.0, "smg": 1700.0, "pistol": 1700.0,
    "shotgun": 900.0, "knife": 350.0, "utility": 1000.0, "none": 600.0,
}

_class_cache: dict[str, str] = {}


def weapon_class(name: str | None) -> str:
    if not name:
        return "none"
    c = _class_cache.get(name)
    if c is not None:
        return c
    n = "".join(ch for ch in name.lower().replace("weapon_", "") if ch.isalnum())
    if "knife" in n or "bayonet" in n or "karambit" in n or "dagger" in n:
        c = "knife"
    elif any(k in n for k in _UTILITY):
        c = "utility"
    elif any(n.startswith(k) for k in _SNIPER):
        c = "sniper"
    elif any(n.startswith(k) for k in _PISTOL):
        c = "pistol"
    elif any(n.startswith(k) for k in _SMG):
        c = "smg"
    elif any(n.startswith(k) for k in _SHOTGUN):
        c = "shotgun"
    elif n.startswith("negev") or n.startswith("m249"):
        c = "mg"
    else:
        c = "rifle"
    _class_cache[name] = c
    return c


# --------------------------------------------------------------------------- snapshots
@dataclass(slots=True)
class PlayerSnap:
    steamid: str
    name: str
    team: str                      # "CT" / "T"
    slot: int | None = None
    health: int = 100
    armor: int = 0
    flashed: int = 0               # 0..255
    smoked: int = 0
    burning: int = 0
    pos: Vec | None = None
    fwd: Vec | None = None
    round_kills: int = 0
    kills: int = 0
    deaths: int = 0
    weapon: str | None = None      # active weapon
    ammo_clip: int | None = None
    equip_value: int = 0

    @property
    def alive(self) -> bool:
        return self.health > 0

    @property
    def wclass(self) -> str:
        return weapon_class(self.weapon)


@dataclass(slots=True)
class Snapshot:
    t: float                       # seconds, local clock (broadcast timeline)
    map_name: str = ""
    round_no: int = 0
    phase: str = "live"            # freezetime / live / bomb / defuse / over / warmup
    bomb_state: str | None = None  # carried / dropped / planting / planted / defusing / defused / exploded
    bomb_pos: Vec | None = None
    bomb_player: str | None = None
    spectated: str | None = None   # steamid currently on camera
    players: dict[str, PlayerSnap] = field(default_factory=dict)

    def alive_players(self):
        return [p for p in self.players.values() if p.alive and p.pos is not None]

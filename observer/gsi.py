"""Parse CS2 Game State Integration payloads (spectator / CSTV client) into Snapshots."""
from __future__ import annotations

from .model import PlayerSnap, Snapshot, parse_vec


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _active_weapon(weapons: dict | None) -> tuple[str | None, int | None]:
    if not weapons:
        return None, None
    for w in weapons.values():
        if isinstance(w, dict) and w.get("state") == "active":
            clip = w.get("ammo_clip")
            return w.get("name"), (_int(clip) if clip is not None else None)
    return None, None


def parse_payload(data: dict, t: float) -> Snapshot:
    m = data.get("map") or {}
    rnd = data.get("round") or {}
    pc = data.get("phase_countdowns") or {}
    phase = pc.get("phase") or rnd.get("phase") or m.get("phase") or "live"
    if m.get("phase") == "warmup":
        phase = "warmup"

    snap = Snapshot(t=t, map_name=m.get("name", ""), round_no=_int(m.get("round")), phase=phase)

    bomb = data.get("bomb") or {}
    if bomb:
        snap.bomb_state = bomb.get("state")
        snap.bomb_pos = parse_vec(bomb.get("position"))
        snap.bomb_player = bomb.get("player")
    elif rnd.get("bomb"):
        snap.bomb_state = rnd.get("bomb")

    player = data.get("player") or {}
    if player.get("steamid") and player.get("activity", "playing") != "menu":
        snap.spectated = player.get("steamid")

    for sid, p in (data.get("allplayers") or {}).items():
        st = p.get("state") or {}
        ms = p.get("match_stats") or {}
        weapon, clip = _active_weapon(p.get("weapons"))
        snap.players[sid] = PlayerSnap(
            steamid=sid,
            name=p.get("name", sid),
            team=p.get("team", ""),
            slot=p.get("observer_slot"),
            health=_int(st.get("health")),
            armor=_int(st.get("armor")),
            flashed=_int(st.get("flashed")),
            smoked=_int(st.get("smoked")),
            burning=_int(st.get("burning")),
            pos=parse_vec(p.get("position")),
            fwd=parse_vec(p.get("forward")),
            round_kills=_int(st.get("round_kills")),
            kills=_int(ms.get("kills")),
            deaths=_int(ms.get("deaths")),
            weapon=weapon,
            ammo_clip=clip,
            equip_value=_int(st.get("equip_value")),
        )
    return snap


GSI_CFG_TEMPLATE = '''"cstv_observer"
{{
    "uri"        "http://127.0.0.1:{port}/gsi"
    "timeout"    "1.0"
    "buffer"     "0.0"
    "throttle"   "0.05"
    "heartbeat"  "5.0"
    "auth"
    {{
        "token"  "{token}"
    }}
    "output"
    {{
        "precision_time"     "3"
        "precision_position" "1"
        "precision_vector"   "3"
    }}
    "data"
    {{
        "provider"               "1"
        "map"                    "1"
        "round"                  "1"
        "phase_countdowns"       "1"
        "bomb"                   "1"
        "player_id"              "1"
        "player_state"           "1"
        "allplayers_id"          "1"
        "allplayers_state"       "1"
        "allplayers_match_stats" "1"
        "allplayers_weapons"     "1"
        "allplayers_position"    "1"
    }}
}}
'''


def render_gsi_cfg(port: int, token: str) -> str:
    return GSI_CFG_TEMPLATE.format(port=port, token=token)

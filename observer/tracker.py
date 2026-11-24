"""Turns a stream of Snapshots into per-player temporal features and inferred kill events.

GSI only gives instantaneous state, so everything time-based (who is shooting,
who just took damage, who just got a kill, who killed whom, velocities) is
derived here by diffing consecutive snapshots.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .model import Snapshot, Vec, dist_weighted, dot, norm, sub

NEG_INF = -1e9


@dataclass(slots=True)
class KillEvent:
    t: float
    victim: str
    killer: str | None


@dataclass
class Track:
    sid: str
    hist: deque = field(default_factory=lambda: deque(maxlen=12))   # (t, pos)
    vel: Vec = (0.0, 0.0, 0.0)
    last_shot: float = NEG_INF
    shots: deque = field(default_factory=lambda: deque(maxlen=32))  # shot timestamps
    last_hurt: float = NEG_INF
    hurt_amount: float = 0.0       # decaying damage taken
    last_kill: float = NEG_INF
    died_at: float | None = None
    killed_by: str | None = None
    weapon: str | None = None
    ammo: int | None = None
    health: int = 100
    round_kills: int = 0
    alive: bool = True

    def shots_in(self, now: float, window: float) -> int:
        return sum(1 for s in self.shots if now - s <= window)


class Tracker:
    def __init__(self, pending_window: float = 0.6):
        self.tracks: dict[str, Track] = {}
        self.round_no: int | None = None
        self.prev: Snapshot | None = None
        self.kills: list[KillEvent] = []
        self.pending_window = pending_window
        self._pending_victims: list[tuple[float, str, Vec | None]] = []
        self._pending_killers: list[tuple[float, str]] = []

    # ------------------------------------------------------------------ helpers
    def get(self, sid: str) -> Track:
        tr = self.tracks.get(sid)
        if tr is None:
            tr = self.tracks[sid] = Track(sid)
        return tr

    def _reset_round(self):
        for tr in self.tracks.values():
            tr.hist.clear()
            tr.vel = (0.0, 0.0, 0.0)
            tr.last_shot = tr.last_hurt = tr.last_kill = NEG_INF
            tr.shots.clear()
            tr.hurt_amount = 0.0
            tr.died_at = None
            tr.killed_by = None
            tr.round_kills = 0
            tr.ammo = None
        self._pending_victims.clear()
        self._pending_killers.clear()

    # ------------------------------------------------------------------ main
    def update(self, snap: Snapshot) -> list[KillEvent]:
        now = snap.t
        new_round = self.round_no is not None and snap.round_no != self.round_no
        if new_round or (self.prev and self.prev.phase != "freezetime" and snap.phase == "freezetime"):
            self._reset_round()
        self.round_no = snap.round_no

        dt_prev = (now - self.prev.t) if self.prev else 0.0
        for sid, p in snap.players.items():
            tr = self.get(sid)
            # ---- movement
            if p.pos is not None and p.alive:
                tr.hist.append((now, p.pos))
                if len(tr.hist) >= 2:
                    t0, p0 = tr.hist[0]
                    span = now - t0
                    if span > 0.05:
                        v = sub(p.pos, p0)
                        tr.vel = (v[0] / span, v[1] / span, v[2] / span)
            # ---- shooting: clip went down while holding the same weapon
            if p.alive and p.ammo_clip is not None:
                if tr.ammo is not None and p.weapon == tr.weapon and p.ammo_clip < tr.ammo:
                    n = min(tr.ammo - p.ammo_clip, 10)
                    for _ in range(n):
                        tr.shots.append(now)
                    tr.last_shot = now
            tr.weapon, tr.ammo = p.weapon, p.ammo_clip
            # ---- damage taken
            if tr.alive and p.health < tr.health:
                dmg = tr.health - p.health
                decay = math.exp(-max(0.0, now - tr.last_hurt) / 1.5) if tr.last_hurt > NEG_INF else 0.0
                tr.hurt_amount = tr.hurt_amount * decay + dmg
                tr.last_hurt = now
            # ---- death
            if tr.alive and not p.alive and not new_round:
                tr.died_at = now
                prev_pos = self.prev.players[sid].pos if self.prev and sid in self.prev.players else p.pos
                self._pending_victims.append((now, sid, prev_pos))
            # ---- kills (round_kills counter increments)
            if p.round_kills > tr.round_kills and not new_round:
                for _ in range(p.round_kills - tr.round_kills):
                    self._pending_killers.append((now, sid))
                tr.last_kill = now
            tr.round_kills = p.round_kills
            tr.health = p.health
            tr.alive = p.alive

        events = self._match_kills(snap)
        self.prev = snap
        return events

    def _match_kills(self, snap: Snapshot) -> list[KillEvent]:
        now = snap.t
        out: list[KillEvent] = []
        if self._pending_victims and self._pending_killers:
            # greedy cheapest assignment: nearby + aiming at the victim = likely killer
            cands = []
            for vi, (tv, vsid, vpos) in enumerate(self._pending_victims):
                vteam = snap.players[vsid].team if vsid in snap.players else ""
                for ki, (tk, ksid) in enumerate(self._pending_killers):
                    if abs(tv - tk) > self.pending_window:
                        continue
                    kp = snap.players.get(ksid)
                    if kp is None or ksid == vsid:
                        continue
                    cost = 1500.0
                    if kp.pos is not None and vpos is not None:
                        d = dist_weighted(kp.pos, vpos)
                        aim = 0.0
                        if kp.fwd is not None and d > 1:
                            aim = max(0.0, dot(kp.fwd, sub(vpos, kp.pos)) / d)
                        cost = d * (1.6 - aim)
                    if kp.team == vteam:
                        cost *= 4.0
                    cands.append((cost, vi, ki))
            cands.sort()
            used_v, used_k = set(), set()
            for cost, vi, ki in cands:
                if vi in used_v or ki in used_k:
                    continue
                used_v.add(vi)
                used_k.add(ki)
                tv, vsid, _ = self._pending_victims[vi]
                ksid = self._pending_killers[ki][1]
                ev = KillEvent(t=tv, victim=vsid, killer=ksid)
                self.get(vsid).killed_by = ksid
                out.append(ev)
            self._pending_victims = [v for i, v in enumerate(self._pending_victims) if i not in used_v]
            self._pending_killers = [k for i, k in enumerate(self._pending_killers) if i not in used_k]
        # expire: victims without a killer (bomb, fall damage, suicide) after the window
        keep = []
        for tv, vsid, vpos in self._pending_victims:
            if now - tv > self.pending_window:
                out.append(KillEvent(t=tv, victim=vsid, killer=None))
            else:
                keep.append((tv, vsid, vpos))
        self._pending_victims = keep
        self._pending_killers = [k for k in self._pending_killers if now - k[0] <= self.pending_window]
        self.kills.extend(out)
        return out

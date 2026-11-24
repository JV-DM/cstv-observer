"""Action-potential scoring: how likely is each player to be part of a kill in the next few seconds?

The score for player P combines:
  * engagement   – for every enemy E in weapon range: proximity x aim (does P look at E? does E look at P?
                   both = a duel), closing speed (time-to-contact), and P's advantage in that duel
                   (health, who has the angle, sniper). Several nearby enemies stack (multi-kill potential).
  * activity     – P is shooting right now, P is taking damage, P just got a kill (trade / multi-kill window),
                   an enemy P is aiming at was just damaged (P is probably the one hitting them).
  * situation    – clutches, planting, defusing, retakes, kill streak in the round.
  * modifiers    – flashed players are worse to watch; snipers slightly better.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, fields

from .model import WEAPON_RANGE, Snapshot, dist_weighted, dot, norm, sub
from .tracker import Tracker


@dataclass
class Weights:
    # engagement
    eng: float = 2.2
    prox_scale: float = 700     # hammer units at 3000-range weapon
    base: float = 0.05            # proximity alone
    aim_self: float = 1.01         # P aims at E
    aim_other: float = 0.25        # E aims at P
    mutual: float = 1.76           # both aim at each other -> duel
    aim_sigma_deg: float = 7.0
    aim_width: float = 50       # widens aim cone at close range (units)
    approach: float = 0.49
    ttc_scale: float = 2.5
    adv: float = 0.34             # duel-advantage multiplier strength
    second_enemy: float = 0.62
    dealing: float = 0.84          # aiming at an enemy that is being damaged
    # activity
    shoot: float = 0.67
    shoot_tau: float = 0.65
    hurt: float = 0.88
    hurt_tau: float = 1.29
    kill_momentum: float = 0.47
    kill_tau: float = 1.86
    # situation
    clutch: float = 0.4
    plant: float = 1.47
    defuse: float = 0.75
    retake: float = 0.34
    hot_hand: float = 0.08
    # modifiers
    flashed_penalty: float = 0.55
    sniper_bonus: float = 0.16
    utility_penalty: float = 0.2
    # director policy
    min_dwell: float = 2.32
    min_dwell_urgent: float = 1.3
    margin: float = 0.36
    abs_margin: float = 0.3
    fight_lock: float = 1.0
    death_hold: float = 1.09
    killer_follow: float = 0.61
    urgent_level: float = 2.9
    idle_level: float = 0.39
    idle_switch_after: float = 3.3
    confirm: float = 0.4          # challenger must stay ahead this long before a normal cut
    return_window: float = 6.0    # going back to the player just left within this many seconds...
    return_margin: float = 0.6    # ...needs this much extra relative margin (no ping-pong)

    @classmethod
    def from_dict(cls, d: dict) -> "Weights":
        names = {f.name for f in fields(cls)}
        return cls(**{k: float(v) for k, v in d.items() if k in names})

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(slots=True)
class Score:
    total: float = 0.0
    eng: float = 0.0
    act: float = 0.0
    situ: float = 0.0
    duel_peak: float = 0.0        # strongest single pair term
    in_fight: float = 0.0         # 0..1 "currently fighting"
    reason: str = ""


def _decay(now: float, t: float, tau: float) -> float:
    dt = now - t
    if dt < 0 or dt > tau * 6:
        return 0.0
    return math.exp(-dt / tau)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class Scorer:
    def __init__(self, w: Weights):
        self.w = w

    def _aim(self, fwd, d_vec, d) -> float:
        if fwd is None or d < 1.0:
            return 0.0
        c = dot(fwd, d_vec) / (d * (norm(fwd) or 1.0))
        c = max(-1.0, min(1.0, c))
        if c < 0.5:
            return 0.0
        ang = math.degrees(math.acos(c))
        sigma = self.w.aim_sigma_deg + math.degrees(math.atan(self.w.aim_width / d))
        return math.exp(-(ang / sigma) ** 2)

    def score(self, snap: Snapshot, tr: Tracker, now: float, hints: dict[str, float] | None = None) -> dict[str, Score]:
        w = self.w
        alive = snap.alive_players()
        out: dict[str, Score] = {}
        if not alive:
            return out
        team_alive: dict[str, int] = {}
        for p in alive:
            team_alive[p.team] = team_alive.get(p.team, 0) + 1
        live = snap.phase not in ("freezetime", "warmup")

        tracks = tr.tracks
        for p in alive:
            ptr = tracks.get(p.steamid)
            s = Score()
            pcls = p.wclass
            prange = WEAPON_RANGE.get(pcls, 2000.0)
            pairs = []
            reason = ""
            best_pair = 0.0
            for e in alive:
                if e.team == p.team:
                    continue
                d = dist_weighted(p.pos, e.pos)
                rng = max(prange, WEAPON_RANGE.get(e.wclass, 2000.0))
                if d > rng * 1.3:
                    continue
                d_vec = sub(e.pos, p.pos)
                # eye (+64) to chest (+45): aim vectors are measured from the eyes
                d_pe = (d_vec[0], d_vec[1], d_vec[2] - 19.0)
                d_ep = (-d_vec[0], -d_vec[1], -d_vec[2] - 19.0)
                prox = math.exp(-d / (w.prox_scale * rng / 3000.0))
                a_pe = self._aim(p.fwd, d_pe, norm(d_pe))
                a_ep = self._aim(e.fwd, d_ep, norm(d_ep))
                duel = prox * (w.base + w.aim_self * a_pe + w.aim_other * a_ep + w.mutual * a_pe * a_ep)
                # approach / time-to-contact
                etr = tracks.get(e.steamid)
                if ptr is not None and etr is not None and d > 1:
                    u = (d_vec[0] / d, d_vec[1] / d, d_vec[2] / d)
                    closing = dot(ptr.vel, u) - dot(etr.vel, u)
                    if closing > 40.0:
                        ttc = d / closing
                        duel += w.approach * math.exp(-ttc / w.ttc_scale) * math.exp(-d / (2.5 * w.prox_scale))
                # P is aiming at an enemy who is taking damage -> P is likely hitting them
                if etr is not None and a_pe > 0.3:
                    duel += w.dealing * a_pe * _decay(now, etr.last_hurt, 1.0) * prox ** 0.5
                # who wins this duel? (health, angle, weapon)
                x = (p.health - e.health) / 40.0 + 1.8 * (a_pe - a_ep)
                if pcls == "sniper":
                    x += 0.4
                if e.wclass == "sniper":
                    x -= 0.4
                if p.flashed > 120:
                    x -= 1.5
                if e.flashed > 120:
                    x += 1.5
                duel *= (1.0 - w.adv) + 2.0 * w.adv * _sigmoid(x)
                pairs.append(duel)
                if duel > best_pair:
                    best_pair = duel
            pairs.sort(reverse=True)
            eng = 0.0
            f = 1.0
            for v in pairs[:3]:
                eng += f * v
                f *= w.second_enemy
            s.eng = w.eng * eng
            s.duel_peak = best_pair

            # activity
            act = 0.0
            fight = 0.0
            if ptr is not None:
                sh = _decay(now, ptr.last_shot, w.shoot_tau)
                hu = _decay(now, ptr.last_hurt, w.hurt_tau) * min(1.0, 0.4 + ptr.hurt_amount / 60.0)
                km = _decay(now, ptr.last_kill, w.kill_tau)
                act = w.shoot * sh * (0.4 + 0.6 * min(1.0, eng * 2)) + w.hurt * hu + w.kill_momentum * km * min(1.0, 0.3 + eng * 2)
                fight = max(sh, hu)
                if sh > 0.5:
                    reason = "shooting"
                elif hu > 0.5:
                    reason = "taking damage"
            s.act = act
            s.in_fight = max(fight, min(1.0, best_pair / 0.8))

            # situation
            situ = 0.0
            if live:
                enemies = sum(v for t, v in team_alive.items() if t != p.team)
                if team_alive.get(p.team, 0) == 1 and enemies >= 1:
                    situ += w.clutch * (1.0 + 0.25 * (enemies - 1))
                    reason = reason or f"clutch 1v{enemies}"
                if snap.bomb_player == p.steamid:
                    if snap.bomb_state == "planting":
                        situ += w.plant
                        reason = "planting"
                    elif snap.bomb_state == "defusing":
                        situ += w.defuse
                        reason = "defusing"
                if snap.bomb_state in ("planted", "defusing") and snap.bomb_pos is not None and p.team == "CT":
                    situ += w.retake * math.exp(-dist_weighted(p.pos, snap.bomb_pos) / 900.0)
                situ += w.hot_hand * p.round_kills
            else:
                # freeze time / warmup: prefer the round's likely protagonists
                situ += 0.05 * p.kills / (p.deaths + 1) + (0.1 if pcls == "sniper" else 0.0)
            if hints:
                situ += hints.get(p.steamid, 0.0)
            s.situ = situ

            total = s.eng + s.act + s.situ
            if p.flashed > 100:
                total *= 1.0 - w.flashed_penalty * (p.flashed / 255.0)
            if pcls == "sniper":
                total *= 1.0 + w.sniper_bonus
            elif pcls == "utility":
                total *= 1.0 - w.utility_penalty
            s.total = total
            if not reason:
                reason = "duel" if best_pair > 0.5 else ("contact" if best_pair > 0.15 else "positioning")
            s.reason = reason
            out[p.steamid] = s
        return out

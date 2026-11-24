"""Offline benchmark: replay CS2 demos through the director and measure how many kills the camera catches.

A demo is converted to the same Snapshot stream the live GSI feed produces (sampled at ~16 Hz,
like GSI with throttle 0.05), so the exact same tracker / scorer / director code is exercised.

Metrics
  coverage        % of kills where the camera was on the killer or the victim for the
                  `pre` seconds before the kill (default 0.75 s, i.e. the viewer saw it coming)
  killer_pov      % of kills seen from the killer's POV
  anticipated     % of kills where the camera was on killer/victim >= 2 s before the kill
  cuts_per_min    camera cuts per minute of live round time
  short_shots     % of shots shorter than 1.5 s (jumpy camera)
"""
from __future__ import annotations

import bisect
import os
import pickle
import random
from dataclasses import dataclass, field

from .model import PlayerSnap, Snapshot, fwd_from_angles
from .tracker import Tracker

TICKRATE = 64


@dataclass
class DemoData:
    name: str
    frames: list[Snapshot]
    deaths: list[tuple[float, str, str | None]]          # (t, victim, attacker)
    hurts: list[tuple[float, str, str | None, int]]     # (t, victim, attacker, dmg)
    live_seconds: float = 0.0
    rounds: list[tuple[int, float, float]] = field(default_factory=list)  # (round, freeze_end_t, end_t)


def _sid(v) -> str | None:
    if v is None:
        return None
    try:
        if v != v:  # NaN
            return None
    except Exception:
        pass
    s = str(v)
    if s in ("0", "", "None", "nan"):
        return None
    return s.split(".")[0]


def load_demo(path: str, step: int = 4, cache_dir: str | None = ".cache") -> DemoData:
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        st = os.stat(path)
        key = f"{os.path.basename(path)}.{st.st_size}.{int(st.st_mtime)}.{step}.pkl"
        cp = os.path.join(cache_dir, key)
        if os.path.exists(cp):
            with open(cp, "rb") as f:
                return pickle.load(f)
    data = _parse_demo(path, step)
    if cache_dir:
        with open(cp, "wb") as f:
            pickle.dump(data, f)
    return data


def _events(parser, name, **kw):
    try:
        df = parser.parse_event(name, **kw)
        return df.to_dict("records") if df is not None and len(df) else []
    except Exception:
        return []


def _parse_demo(path: str, step: int) -> DemoData:
    from demoparser2 import DemoParser  # optional dependency

    parser = DemoParser(path)
    header = parser.parse_header()
    deaths_raw = _events(parser, "player_death")
    hurts_raw = _events(parser, "player_hurt")
    freeze_end = _events(parser, "round_freeze_end", other=["total_rounds_played"])
    round_end = _events(parser, "round_end", other=["total_rounds_played"])
    begin_plant = _events(parser, "bomb_beginplant")
    abort_plant = _events(parser, "bomb_abortplant")
    planted = _events(parser, "bomb_planted", player=["X", "Y", "Z"])
    begin_defuse = _events(parser, "bomb_begindefuse")
    abort_defuse = _events(parser, "bomb_abortdefuse")
    defused = _events(parser, "bomb_defused")
    exploded = _events(parser, "bomb_exploded")

    max_tick = max([e["tick"] for e in deaths_raw + round_end] or [0]) + 5 * TICKRATE
    fields_ = ["X", "Y", "Z", "pitch", "yaw", "health", "team_num", "active_weapon_name",
               "active_weapon_ammo", "total_rounds_played", "is_freeze_period", "is_warmup_period",
               "kills_total", "deaths_total"]
    ticks = list(range(0, max_tick, step))
    df = parser.parse_ticks(fields_, ticks=ticks)
    df = df.sort_values("tick")

    # round_kills from death events (GSI provides this directly)
    deaths = sorted((int(e["tick"]), _sid(e.get("user_steamid")), _sid(e.get("attacker_steamid"))) for e in deaths_raw)
    hurts = sorted((int(e["tick"]), _sid(e.get("user_steamid")), _sid(e.get("attacker_steamid")),
                    int(e.get("dmg_health") or 0)) for e in hurts_raw)

    # bomb timeline -> list of (tick, state, player, pos)
    bomb_tl: list[tuple[int, str, str | None, tuple | None]] = []
    for e in begin_plant:
        bomb_tl.append((int(e["tick"]), "planting", _sid(e.get("user_steamid")), None))
    for e in abort_plant:
        bomb_tl.append((int(e["tick"]), "carried", None, None))
    for e in planted:
        pos = (e.get("user_X"), e.get("user_Y"), e.get("user_Z"))
        pos = tuple(float(v) for v in pos) if all(v is not None for v in pos) else None
        bomb_tl.append((int(e["tick"]), "planted", None, pos))
    for e in begin_defuse:
        bomb_tl.append((int(e["tick"]), "defusing", _sid(e.get("user_steamid")), "keep"))
    for e in abort_defuse:
        bomb_tl.append((int(e["tick"]), "planted", None, "keep"))
    for e in defused + exploded:
        bomb_tl.append((int(e["tick"]), "over", None, None))
    for e in freeze_end:
        bomb_tl.append((int(e["tick"]) - 1, "carried", None, None))
    bomb_tl.sort(key=lambda x: x[0])

    end_ticks = sorted(int(e["tick"]) for e in round_end)
    fe_ticks = sorted(int(e["tick"]) for e in freeze_end)

    frames: list[Snapshot] = []
    di = 0
    round_kills: dict[str, int] = {}
    cur_round = None
    bi = 0
    bomb_state, bomb_player, bomb_pos = None, None, None
    live_ticks = 0
    map_name = header.get("map_name", "")

    for tick, g in df.groupby("tick", sort=True):
        rows = g.to_dict("records")
        r0 = rows[0]
        rnd = int(r0.get("total_rounds_played") or 0)
        if rnd != cur_round:
            cur_round = rnd
            round_kills = {}
        while di < len(deaths) and deaths[di][0] <= tick:
            _, v, a = deaths[di]
            if a and a != v:
                round_kills[a] = round_kills.get(a, 0) + 1
            di += 1
        while bi < len(bomb_tl) and bomb_tl[bi][0] <= tick:
            _, st, pl, pos = bomb_tl[bi]
            bomb_state, bomb_player = st, pl
            if pos != "keep":
                bomb_pos = pos if st in ("planted",) else (bomb_pos if st == "defusing" else None)
            bi += 1
        warm = bool(r0.get("is_warmup_period"))
        freeze = bool(r0.get("is_freeze_period"))
        # round over: between round_end and next freeze end
        k = bisect.bisect_right(end_ticks, tick) - 1
        j = bisect.bisect_right(fe_ticks, tick) - 1
        over = k >= 0 and (j < 0 or end_ticks[k] > fe_ticks[j])
        phase = "warmup" if warm else ("freezetime" if freeze else ("over" if over else "live"))
        if phase == "live":
            live_ticks += step
        snap = Snapshot(t=tick / TICKRATE, map_name=map_name, round_no=rnd, phase=phase,
                        bomb_state=bomb_state, bomb_pos=bomb_pos, bomb_player=bomb_player)
        for r in rows:
            sid = _sid(r.get("steamid"))
            if not sid:
                continue
            tn = r.get("team_num")
            team = "T" if tn == 2 else ("CT" if tn == 3 else "")
            if not team:
                continue
            hp = r.get("health")
            hp = int(hp) if hp == hp and hp is not None else 0
            x, y, z = r.get("X"), r.get("Y"), r.get("Z")
            pos = (float(x), float(y), float(z)) if x == x and x is not None else None
            pitch, yaw = r.get("pitch"), r.get("yaw")
            fwd = fwd_from_angles(float(pitch), float(yaw)) if pitch == pitch and pitch is not None else None
            ammo = r.get("active_weapon_ammo")
            ammo = int(ammo) if ammo == ammo and ammo is not None else None
            wname = r.get("active_weapon_name")
            wname = wname if isinstance(wname, str) else None
            snap.players[sid] = PlayerSnap(
                steamid=sid, name=str(r.get("name")), team=team, health=hp, pos=pos, fwd=fwd,
                round_kills=round_kills.get(sid, 0), weapon=wname, ammo_clip=ammo,
                kills=int(r.get("kills_total") or 0), deaths=int(r.get("deaths_total") or 0))
        frames.append(snap)

    rounds = []
    for fe in fe_ticks:
        k = bisect.bisect_right(end_ticks, fe)
        end = end_ticks[k] if k < len(end_ticks) else max_tick
        rounds.append((len(rounds), fe / TICKRATE, end / TICKRATE))

    # only keep deaths that happen during the match (not warmup)
    warm_end = 0.0
    for f in frames:
        if f.phase != "warmup":
            warm_end = f.t
            break
    last_end = (max(end_ticks) / TICKRATE + 8.0) if end_ticks else 1e12
    deaths_t = [(t / TICKRATE, v, a) for t, v, a in deaths if v and warm_end <= t / TICKRATE <= last_end]
    hurts_t = [(t / TICKRATE, v, a, d) for t, v, a, d in hurts if v and t / TICKRATE >= warm_end]
    return DemoData(name=os.path.basename(path), frames=frames, deaths=deaths_t, hurts=hurts_t,
                    live_seconds=live_ticks / TICKRATE, rounds=rounds)


# ============================================================================ simulation
def simulate(demo: DemoData, make_director, latency: float = 0.1, oracle: bool = False):
    """Run a director over the demo. Returns camera timeline [(t, target)].

    `latency` models the netcon round trip: a cut decided at t shows on screen at t+latency.
    If `oracle` is True the director receives the future kill/hurt events (lookahead mode).
    """
    tracker = Tracker()
    director = make_director()
    if oracle and hasattr(director, "set_future"):
        director.set_future(
            kills=[(t, v, a) for t, v, a in demo.deaths],
            hurts=[(t, v, a, d) for t, v, a, d in demo.hurts],
        )
    timeline: list[tuple[float, str | None]] = []
    on_screen: str | None = None
    pending: list[tuple[float, str]] = []
    for snap in demo.frames:
        now = snap.t
        while pending and pending[0][0] <= now:
            on_screen = pending.pop(0)[1]
        snap.spectated = on_screen
        kills = tracker.update(snap)
        if snap.phase == "warmup":
            continue
        dec = director.decide(snap, tracker, now, kills)
        if dec.target and dec.switched:
            if latency <= 0:
                on_screen = dec.target
            else:
                pending.append((now + latency, dec.target))
        elif dec.target and on_screen is None and not pending:
            on_screen = dec.target
        timeline.append((now, on_screen))
    return timeline


def evaluate(demo: DemoData, timeline, pre: float = 0.75, anticip: float = 2.0, rounds: set[int] | None = None) -> dict:
    ts = [t for t, _ in timeline]

    def target_at(t):
        i = bisect.bisect_right(ts, t) - 1
        return timeline[i][1] if i >= 0 else None

    def on_during(sids, t0, t1):
        i0 = max(0, bisect.bisect_left(ts, t0))
        i1 = bisect.bisect_right(ts, t1)
        if i0 >= i1:
            return False
        return all(timeline[i][1] in sids for i in range(i0, i1))

    n = seen = killer_pov = anticip_n = 0
    deaths = demo.deaths
    if rounds is not None:
        spans = [(s, e) for r, s, e in demo.rounds if r in rounds]
        deaths = [d for d in deaths if any(s - 1 <= d[0] <= e + 6 for s, e in spans)]
    for t, v, a in deaths:
        n += 1
        sids = {v} | ({a} if a else set())
        if on_during(sids, t - pre, t):
            seen += 1
            if a and target_at(t) == a:
                killer_pov += 1
            if on_during(sids, t - anticip, t):
                anticip_n += 1
    # shots
    cuts = 0
    shot_lengths = []
    last_t, last = None, None
    for t, tgt in timeline:
        if tgt != last:
            if last is not None:
                cuts += 1
                shot_lengths.append(t - last_t)
            last_t, last = t, tgt
    live_min = max((timeline[-1][0] - timeline[0][0]) / 60.0 if timeline else 0.0, 1e-6)
    short = sum(1 for s in shot_lengths if s < 1.5)
    return {
        "kills": n,
        "coverage": seen / max(n, 1),
        "killer_pov": killer_pov / max(n, 1),
        "anticipated": anticip_n / max(n, 1),
        "cuts_per_min": cuts / live_min,
        "short_shots": short / max(len(shot_lengths), 1),
        "median_shot": sorted(shot_lengths)[len(shot_lengths) // 2] if shot_lengths else 0.0,
    }


def objective(m: dict) -> float:
    """What the tuner maximizes: kills seen, rewarding anticipation and killer POV,
    penalizing a jumpy camera (above ~9 cuts/min) and very short shots."""
    return (m["coverage"] + 0.35 * m["anticipated"] + 0.15 * m["killer_pov"]
            - 0.01 * max(0.0, m["cuts_per_min"] - 9.0) - 0.15 * m["short_shots"])


# ============================================================================ baselines
class RandomDirector:
    """Switch to a random alive player every 6 s (or on death)."""

    def __init__(self, seed=1, every=6.0):
        self.rng = random.Random(seed)
        self.every = every
        self.cur, self.since = None, -1e9

    def decide(self, snap, tr, now, kills=None):
        from .director import Decision
        alive = [p.steamid for p in snap.alive_players()]
        if not alive:
            return Decision(self.cur, "")
        cur_ok = self.cur in snap.players and snap.players[self.cur].alive
        if not cur_ok or now - self.since > self.every:
            new = self.rng.choice(alive)
            sw = new != self.cur
            self.cur, self.since = new, now
            return Decision(new, "random", sw)
        return Decision(self.cur, "", False)


class ReactiveDirector:
    """What most simple auto-observers do: cut to whoever just got a kill; on death cut to the killer;
    otherwise stay. Fallback to the player with the most kills."""

    def __init__(self):
        self.cur, self.since = None, -1e9

    def decide(self, snap, tr, now, kills=None):
        from .director import Decision
        alive = {p.steamid: p for p in snap.alive_players()}
        if not alive:
            return Decision(self.cur, "")
        target = self.cur
        for k in kills or []:
            if k.killer in alive:
                target = k.killer
        if target not in alive:
            ctr = tr.tracks.get(self.cur) if self.cur else None
            died = ctr.died_at if ctr and ctr.died_at is not None else -1e9
            if ctr and now - died < 0.9:
                return Decision(self.cur, "hold", False)
            if ctr and ctr.killed_by in alive:
                target = ctr.killed_by
            else:
                target = max(alive, key=lambda s: (alive[s].round_kills, alive[s].kills))
        if target is None:
            target = next(iter(alive))
        sw = target != self.cur
        if sw:
            self.cur, self.since = target, now
        return Decision(self.cur, "reactive", sw)


class NearestEnemyDirector:
    """Greedy: whoever is closest to an enemy, re-evaluated every 2 s."""

    def __init__(self):
        self.cur, self.since = None, -1e9

    def decide(self, snap, tr, now, kills=None):
        from .director import Decision
        from .model import dist_weighted
        alive = snap.alive_players()
        if not alive:
            return Decision(self.cur, "")
        cur_ok = self.cur in snap.players and snap.players[self.cur].alive
        if cur_ok and now - self.since < 2.0:
            return Decision(self.cur, "", False)
        best, bd = None, 1e18
        for p in alive:
            for e in alive:
                if e.team != p.team:
                    d = dist_weighted(p.pos, e.pos)
                    if d < bd:
                        bd, best = d, p.steamid
        sw = best != self.cur
        self.cur, self.since = best, now
        return Decision(best, "nearest", sw)

"""Lookahead mode: use the real-time game (via the server plugin) to know about kills before the
delayed CSTV broadcast shows them, and plan the camera so that it is on the right player *before*
each kill happens.

CSTV is delayed by `tv_delay` seconds (often 90-120 s on match servers). A CounterStrikeSharp plugin
on the game server streams player_death / player_hurt / round events in real time. Here we

  1. ClockSync   – map server game-time to the local broadcast timeline, anchored on events that are
                   visible on both sides (freeze-time end, bomb plant), with tv_delay as a fallback.
  2. Planner     – weighted interval scheduling over upcoming kills: choose, for each kill, whether to
                   watch it from the killer or the victim (or skip it) so that the number of kills shown
                   (weighted by how early the camera arrives) is maximal, subject to a minimum shot length.
  3. LookaheadDirector – follows the plan inside kill windows and falls back to the heuristic director
                   (boosted by "this player will be in a fight in a few seconds" hints) in between.
"""
from __future__ import annotations

import bisect
import statistics
import threading
from dataclasses import dataclass

from .director import Decision, Director
from .model import Snapshot
from .scoring import Weights
from .tracker import KillEvent, Tracker


@dataclass
class PlanConfig:
    lead_ideal: float = 2.5      # be on the player this long before the kill
    lead_min: float = 0.8        # below this the kill is not worth planning (too late)
    tail: float = 0.9            # stay after the kill
    min_shot: float = 1.3        # no shot shorter than this
    victim_value: float = 0.75   # value of seeing a kill from the victim's POV (killer = 1.0)
    hurt_hint: float = 1.2       # heuristic bonus for players who take/deal damage soon
    hurt_horizon: float = 4.0


@dataclass(slots=True)
class Window:
    start: float
    end: float
    target: str
    kill_t: float
    kind: str        # "killer" / "victim"


class Planner:
    def __init__(self, cfg: PlanConfig):
        self.cfg = cfg

    def plan(self, kills: list[tuple[float, str, str | None]], now: float, current: str | None,
             current_since: float) -> list[Window]:
        """kills: (t, victim, attacker) in local time, sorted. Returns windows sorted by start."""
        c = self.cfg
        ks = [k for k in kills if k[0] > now - c.tail]
        if not ks:
            return []
        death_t: dict[str, float] = {}
        for t, v, _ in kills:
            death_t.setdefault(v, t)

        # candidates: (kill index, pov player, kind, value)
        cands = []
        for i, (t, v, a) in enumerate(ks):
            if a and a != v:
                cands.append((i, a, "killer", 1.0))
            cands.append((i, v, "victim", c.victim_value))

        def alive_at(pid, t):
            dt = death_t.get(pid)
            return dt is None or dt >= t - 1e-6

        n = len(cands)
        # dp over candidates sorted by kill time: best[j] = (value, prev index, start)
        # best[j] = (value, prev candidate, window start, start of the on-screen shot)
        best: list[tuple[float, int, float, float]] = []
        for j, (i, pid, kind, val) in enumerate(cands):
            t = ks[i][0]
            # option A: first planned shot (transition from the current camera)
            if pid == current:
                # camera is already there: full value if we simply stay
                start_a = max(now, t - c.lead_ideal)
                opt = (val, -1, start_a, current_since if start_a <= now else start_a)
            else:
                start_a = max(now, t - c.lead_ideal)
                lead = t - start_a
                q = min(1.0, lead / c.lead_ideal)
                opt = (val * (0.4 + 0.6 * q), -1, start_a, start_a) if lead >= c.lead_min else (-1e9, -1, 0.0, 0.0)
            # option B: extend a previous candidate
            for p in range(j):
                pv, _, pstart, pshot = best[p]
                if pv <= -1e8:
                    continue
                pi, ppid, pkind, _ = cands[p]
                if pi == i:
                    continue  # two POVs of the same kill
                pt = ks[pi][0]
                if ppid == pid:
                    # same player: one continuous shot, no cut; he must survive the earlier kill
                    if not alive_at(pid, pt + 0.01) and pkind == "victim":
                        continue
                    if not alive_at(pid, t - 0.01) and kind == "killer":
                        continue
                    v2 = pv + val
                    if v2 > opt[0]:
                        opt = (v2, p, max(pt, t - c.lead_ideal), pshot)
                else:
                    # cut after the previous kill's tail (shortened if needed, but >= 0.3 s)
                    free = pt + c.tail
                    start = max(free, t - c.lead_ideal)
                    lead = t - start
                    if lead < c.lead_min:
                        # try shortening the previous tail
                        start = max(pt + 0.3, t - c.lead_ideal)
                        lead = t - start
                        if lead < c.lead_min:
                            continue
                    if start - pshot < c.min_shot:
                        continue
                    q = min(1.0, lead / c.lead_ideal)
                    v2 = pv + val * (0.4 + 0.6 * q)
                    if v2 > opt[0]:
                        opt = (v2, p, start, start)
            best.append(opt)

        if not best:
            return []
        j = max(range(n), key=lambda x: best[x][0])
        if best[j][0] <= 0:
            return []
        chain = []
        while j >= 0:
            chain.append(j)
            j = best[j][1]
        chain.reverse()

        windows: list[Window] = []
        for idx, j in enumerate(chain):
            i, pid, kind, _ = cands[j]
            t = ks[i][0]
            start = best[j][2]
            end = t + c.tail
            if idx + 1 < len(chain):
                nxt = chain[idx + 1]
                if cands[nxt][1] != pid:
                    end = min(end, best[nxt][2])
            if windows and windows[-1].target == pid and start - windows[-1].end < c.min_shot + c.lead_ideal:
                windows[-1].end = max(windows[-1].end, end)   # keep rolling: same player, short gap
                windows[-1].kill_t = t
                continue
            windows.append(Window(start=start, end=end, target=pid, kill_t=t, kind=kind))
        return windows


class ClockSync:
    """server game-time -> local broadcast time:  local = server + offset.

    A rough offset comes from every feed batch (local receive time - server time + tv_delay).
    It is refined with anchors seen on both sides (freeze-time end, bomb plant): the local
    anchor is paired with the server anchor of the same kind whose predicted local time is
    closest (rounds are ~100 s apart, so pairing is unambiguous even if round numbers differ).
    """

    def __init__(self, match_window: float = 20.0):
        self.match_window = match_window
        self._anchors: list[float] = []
        self._fallback: float | None = None
        self._server: dict[str, list[float]] = {}
        self._pending_local: list[tuple[str, float]] = []

    @property
    def offset(self) -> float | None:
        if self._anchors:
            return statistics.median(self._anchors[-5:])
        return self._fallback

    def on_feed_batch(self, server_now: float, local_now: float, tv_delay: float):
        self._fallback = local_now - server_now + tv_delay
        pend, self._pending_local = self._pending_local, []
        for kind, t in pend:
            self.local_anchor(kind, t)

    def server_anchor(self, kind: str, server_t: float):
        lst = self._server.setdefault(kind, [])
        lst.append(server_t)
        del lst[:-20]

    def local_anchor(self, kind: str, local_t: float):
        guess = self.offset
        if guess is None:
            self._pending_local.append((kind, local_t))
            return
        cands = self._server.get(kind, [])
        if not cands:
            self._pending_local.append((kind, local_t))
            self._pending_local = self._pending_local[-5:]
            return
        st = min(cands, key=lambda s: abs(s + guess - local_t))
        if abs(st + guess - local_t) <= self.match_window:
            self._anchors.append(local_t - st)
            self._anchors = self._anchors[-20:]


def _pid(ev: dict, key: str) -> str | None:
    """Prefer steamid64; bots have none, so fall back to 'name:<name>' (resolved against GSI later)."""
    sid = ev.get(key)
    if sid and str(sid) not in ("0", "None"):
        return str(sid)
    name = ev.get(key + "_name")
    return f"name:{name}" if name else None


class FutureFeed:
    """Thread-safe store of upcoming events in *server* time; converted with ClockSync on read."""

    def __init__(self, sync: ClockSync):
        self.sync = sync
        self.lock = threading.Lock()
        self.kills: list[tuple[float, str, str | None]] = []
        self.hurts: list[tuple[float, str, str | None, int]] = []
        self.version = 0

    def ingest(self, batch: dict, local_now: float):
        """batch = {"now": server_time, "tv_delay": float, "events": [...]} from the plugin."""
        with self.lock:
            for ev in batch.get("events", []):
                kind, t = ev.get("type"), float(ev.get("t", 0))
                if kind == "death":
                    v = _pid(ev, "victim")
                    if v:
                        self.kills.append((t, v, _pid(ev, "attacker")))
                elif kind == "hurt":
                    v = _pid(ev, "victim")
                    if v:
                        self.hurts.append((t, v, _pid(ev, "attacker"), int(ev.get("dmg", 0))))
                elif kind in ("freeze_end", "bomb_planted"):
                    self.sync.server_anchor(kind, t)
                elif kind == "map_start":
                    self.kills.clear()
                    self.hurts.clear()
            if "now" in batch:
                self.sync.on_feed_batch(float(batch["now"]), local_now, float(batch.get("tv_delay", 0)))
            self.kills.sort()
            self.hurts.sort()
            # keep memory bounded (events older than ~10 min of server time are useless)
            if self.kills:
                horizon = self.kills[-1][0] - 600
                self.kills = [k for k in self.kills if k[0] >= horizon]
                self.hurts = [h for h in self.hurts if h[0] >= horizon]
            self.version += 1

    def local_events(self):
        off = self.sync.offset
        if off is None:
            return None, None
        with self.lock:
            return ([(t + off, v, a) for t, v, a in self.kills],
                    [(t + off, v, a, d) for t, v, a, d in self.hurts])


class LookaheadDirector:
    """Plan-following director. Works offline (set_future with local times) or live (FutureFeed)."""

    def __init__(self, weights: Weights | None = None, cfg: PlanConfig | None = None, feed: FutureFeed | None = None):
        self.cfg = cfg or PlanConfig()
        self.base = Director(weights)
        self.planner = Planner(self.cfg)
        self.feed = feed
        self._kills: list = []
        self._hurts: list = []
        self._hurt_ts: list[float] = []
        self._plan: list[Window] = []
        self._plan_t = -1e9
        self._feed_version = -1
        self._prev_phase = None
        self._prev_bomb = None
        self._needs_names = False

    @property
    def current(self):
        return self.base.current

    def set_future(self, kills, hurts):
        self._kills = sorted(kills)
        self._hurts = sorted(hurts)
        self._hurt_ts = [h[0] for h in self._hurts]

    def _refresh_feed(self, snap: Snapshot):
        if self.feed is None:
            return
        # local sync anchors from what the broadcast shows
        if self._prev_phase == "freezetime" and snap.phase not in ("freezetime", "warmup"):
            self.feed.sync.local_anchor("freeze_end", snap.t)
        if snap.bomb_state == "planted" and self._prev_bomb not in ("planted", "defusing", None):
            self.feed.sync.local_anchor("bomb_planted", snap.t)
        self._prev_phase, self._prev_bomb = snap.phase, snap.bomb_state
        if self.feed.version != self._feed_version or self._needs_names:
            kills, hurts = self.feed.local_events()
            if kills is not None:
                names = {p.name: sid for sid, p in snap.players.items()}
                unresolved = False

                def r(pid):
                    nonlocal unresolved
                    if pid and pid.startswith("name:"):
                        sid = names.get(pid[5:])
                        unresolved |= sid is None
                        return sid or pid
                    return pid
                kills = [(t, r(v), r(a)) for t, v, a in kills]
                hurts = [(t, r(v), r(a), d) for t, v, a, d in hurts]
                self._needs_names = unresolved
                self.set_future(kills, hurts)
                self._feed_version = self.feed.version
                self._plan_t = -1e9

    def _hints(self, now: float) -> dict[str, float]:
        h: dict[str, float] = {}
        if not self._hurt_ts:
            return h
        i0 = bisect.bisect_left(self._hurt_ts, now)
        i1 = bisect.bisect_right(self._hurt_ts, now + self.cfg.hurt_horizon)
        for t, v, a, dmg in self._hurts[i0:i1]:
            soon = 1.0 - (t - now) / self.cfg.hurt_horizon
            for pid, f in ((a, 1.0), (v, 0.7)):
                if pid:
                    h[pid] = max(h.get(pid, 0.0), self.cfg.hurt_hint * f * soon)
        return h

    def decide(self, snap: Snapshot, tr: Tracker, now: float, kills: list[KillEvent] | None = None) -> Decision:
        self._refresh_feed(snap)
        b = self.base
        if not self._kills or snap.phase == "warmup":
            return b.decide(snap, tr, now, kills)
        if now - self._plan_t > 0.5:
            self._plan = self.planner.plan(self._kills, now, b.current, b.since)
            self._plan_t = now
        alive = {p.steamid for p in snap.alive_players()}
        active = next((w for w in self._plan if w.start <= now < w.end), None)
        if active is not None:
            if active.target in alive or (active.target == b.current):
                if active.target != b.current:
                    self._plan_t = -1e9   # replan after every cut
                    return b._cut(active.target, now, f"lookahead: {active.kind} in {active.kill_t - now:.1f}s",
                                  b.last_scores)
                return Decision(b.current, f"lookahead: {active.kind} in {active.kill_t - now:.1f}s", False,
                                b.last_scores)
        # between windows: heuristic, but don't produce a shot that the next window would cut short
        nxt = next((w for w in self._plan if w.start > now), None)
        dec = b.decide(snap, tr, now, kills, hints=self._hints(now))
        if nxt is not None and nxt.start - now < self.cfg.min_shot:
            if nxt.target in alive and nxt.target != b.current and dec.switched:
                # we were going to cut anyway: cut straight to the upcoming window's player
                b.current = nxt.target
                return Decision(nxt.target, "lookahead: early", True, dec.scores)
        return dec

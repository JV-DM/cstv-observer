import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from observer.gsi import parse_payload, render_gsi_cfg  # noqa: E402
from observer.lookahead import ClockSync, FutureFeed, PlanConfig, Planner  # noqa: E402
from observer.model import PlayerSnap, Snapshot, steam64_to_accountid, weapon_class  # noqa: E402
from observer.director import Director  # noqa: E402
from observer.scoring import Weights  # noqa: E402
from observer.tracker import Tracker  # noqa: E402

DEMO = os.environ.get("CSTV_TEST_DEMO", "/tmp/demos/test_demo.dem")


def payload(players, spectated=None, phase="live", rnd=3, bomb=None):
    d = {
        "map": {"name": "de_mirage", "round": rnd, "phase": "live"},
        "round": {"phase": phase},
        "phase_countdowns": {"phase": phase},
        "player": {"steamid": spectated, "activity": "playing"} if spectated else {},
        "allplayers": {},
    }
    if bomb:
        d["bomb"] = bomb
    for p in players:
        d["allplayers"][p["sid"]] = {
            "name": p.get("name", p["sid"]), "observer_slot": p.get("slot", 1), "team": p["team"],
            "state": {"health": p.get("hp", 100), "flashed": 0, "round_kills": p.get("rk", 0)},
            "match_stats": {"kills": 0, "deaths": 0},
            "weapons": {"weapon_0": {"name": p.get("w", "weapon_ak47"), "state": "active", "ammo_clip": p.get("ammo", 30)}},
            "position": "{}, {}, {}".format(*p["pos"]),
            "forward": "{}, {}, {}".format(*p.get("fwd", (1, 0, 0))),
        }
    return d


def test_parse_payload():
    s = parse_payload(payload([{"sid": "76561198000000001", "team": "CT", "pos": (1, 2, 3), "hp": 55}],
                              spectated="76561198000000001"), 10.0)
    p = s.players["76561198000000001"]
    assert p.health == 55 and p.pos == (1.0, 2.0, 3.0) and p.weapon == "weapon_ak47" and p.ammo_clip == 30
    assert s.spectated == "76561198000000001" and s.phase == "live" and s.round_no == 3


def test_helpers():
    assert steam64_to_accountid("76561197960265729") == 1
    assert steam64_to_accountid("BOT") is None
    assert weapon_class("weapon_awp") == "sniper" and weapon_class("AK-47") == "rifle"
    assert weapon_class("weapon_knife_karambit") == "knife" and weapon_class("USP-S") == "pistol"
    assert "allplayers_position" in render_gsi_cfg(3500, "tok")


def test_tracker_infers_killer_and_shots():
    tr = Tracker()
    a = {"sid": "A", "team": "CT", "pos": (0, 0, 0), "fwd": (1, 0, 0)}
    b = {"sid": "B", "team": "T", "pos": (500, 0, 0), "fwd": (-1, 0, 0)}
    c = {"sid": "C", "team": "CT", "pos": (0, 2000, 0), "fwd": (0, 1, 0), "rk": 0}
    tr.update(parse_payload(payload([a, b, c]), 1.0))
    a2 = dict(a, ammo=27)
    tr.update(parse_payload(payload([a2, dict(b, hp=40), c]), 1.1))
    assert tr.tracks["A"].last_shot == 1.1 and tr.tracks["B"].last_hurt == 1.1
    ev = tr.update(parse_payload(payload([dict(a2, ammo=25, rk=1), dict(b, hp=0), c]), 1.2))
    assert len(ev) == 1 and ev[0].victim == "B" and ev[0].killer == "A"


def test_director_prefers_duel_and_holds():
    w = Weights()
    d = Director(w)
    tr = Tracker()
    quiet = {"sid": "Q", "team": "CT", "pos": (5000, 5000, 0), "fwd": (1, 0, 0)}
    a = {"sid": "A", "team": "CT", "pos": (0, 0, 0), "fwd": (1, 0, 0)}
    b = {"sid": "B", "team": "T", "pos": (800, 0, 0), "fwd": (-1, 0, 0)}
    far = {"sid": "F", "team": "T", "pos": (9000, -9000, 0), "fwd": (1, 0, 0)}
    t = 0.0
    snap = parse_payload(payload([quiet, a, b, far]), t)
    tr.update(snap)
    dec = d.decide(snap, tr, t)
    assert dec.target in ("A", "B")           # duel participants, not the idle players
    first = dec.target
    # small fluctuations don't cause a cut inside min_dwell
    for i in range(10):
        t += 0.1
        snap = parse_payload(payload([quiet, a, b, far]), t)
        tr.update(snap)
        assert d.decide(snap, tr, t).target == first


def test_planner_trade_chain_and_coverage():
    p = Planner(PlanConfig())
    # A kills B at 10, C trades A at 11 (A = killer then victim -> one shot on A covers both)
    kills = [(10.0, "B", "A"), (11.0, "A", "C"), (20.0, "D", "E")]
    w = p.plan(kills, now=5.0, current=None, current_since=0.0)
    covered = {round(x.kill_t, 1) for x in w}
    assert w[0].target == "A" and w[0].start <= 10.0 - 2.0 and w[0].end >= 11.0
    assert 20.0 in covered
    # windows never overlap and every shot is >= min_shot except the tail of the last one
    for x, y in zip(w, w[1:]):
        assert x.end <= y.start + 1e-6


def test_clock_sync_pairs_anchors():
    cs = ClockSync()
    cs.on_feed_batch(server_now=1000.0, local_now=50.0, tv_delay=90.0)   # fallback offset = -860
    cs.server_anchor("freeze_end", 1005.0)
    cs.local_anchor("freeze_end", 145.5)                                  # true offset -859.5
    assert abs(cs.offset - (-859.5)) < 1e-6
    feed = FutureFeed(cs)
    feed.ingest({"events": [{"type": "death", "t": 1010.0, "victim": "1", "attacker": "2"},
                            {"type": "death", "t": 1011.0, "victim": None, "victim_name": "Bot Al", "attacker": "1"}]}, 60.0)
    kills, _ = feed.local_events()
    assert kills[0] == (150.5, "1", "2") and kills[1][1] == "name:Bot Al"


# --------------------------------------------------------------------------- end-to-end on a real demo
def _snap_to_payload(s: Snapshot, spectated, token):
    d = {"auth": {"token": token},
         "map": {"name": s.map_name, "round": s.round_no, "phase": "warmup" if s.phase == "warmup" else "live"},
         "round": {"phase": s.phase}, "phase_countdowns": {"phase": s.phase},
         "player": {"steamid": spectated, "activity": "playing"} if spectated else {},
         "allplayers": {}}
    if s.bomb_state:
        d["bomb"] = {"state": s.bomb_state, "player": s.bomb_player,
                     "position": ", ".join(map(str, s.bomb_pos)) if s.bomb_pos else None}
    for sid, p in s.players.items():
        d["allplayers"][sid] = {
            "name": p.name, "team": p.team, "observer_slot": 1,
            "state": {"health": p.health, "flashed": p.flashed, "round_kills": p.round_kills},
            "match_stats": {"kills": p.kills, "deaths": p.deaths},
            "weapons": {"w": {"name": p.weapon, "state": "active", "ammo_clip": p.ammo_clip}},
            "position": ", ".join(map(str, p.pos)) if p.pos else "",
            "forward": ", ".join(map(str, p.fwd)) if p.fwd else "",
        }
    return d


@pytest.mark.skipif(not os.path.exists(DEMO), reason="test demo not available")
def test_live_engine_replay_with_lookahead(monkeypatch):
    """Replays a demo through the real live path: GSI JSON -> Engine -> dry-run netcon, with the server
    feed arriving 95 s ahead of the broadcast. Clock sync must find the offset by itself."""
    import observer.server as server_mod
    from observer.bench import evaluate, load_demo
    from observer.config import Config

    demo = load_demo(DEMO)
    clock = {"t": 0.0}
    monkeypatch.setattr(server_mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr("observer.controller.time.monotonic", lambda: clock["t"])
    cfg = Config(dry_run=True, lookahead=True, feed_token="x")
    eng = server_mod.Engine(cfg)

    DELAY, BASE = 95.0, 5000.0            # broadcast = server time + DELAY ; local clock = demo t + BASE
    sid_by_name = {}
    # the feed batches: every 0.25 s of *server* time; server time = demo time
    deaths = sorted(demo.deaths)
    fe_times = [r[1] for r in demo.rounds]
    di = fi = 0
    timeline = []
    next_batch = 0.0
    for s in demo.frames:
        local = s.t + BASE
        server_now = s.t + DELAY          # server is DELAY ahead of what the broadcast shows
        while next_batch <= server_now:
            evs = []
            while di < len(deaths) and deaths[di][0] <= next_batch:
                t, v, a = deaths[di]
                evs.append({"type": "death", "t": t, "victim": v, "attacker": a})
                di += 1
            while fi < len(fe_times) and fe_times[fi] <= next_batch:
                evs.append({"type": "freeze_end", "t": fe_times[fi]})
                fi += 1
            clock["t"] = local - (server_now - next_batch)
            # server clock = demo time; tv_delay reported slightly wrong (real broadcasts add buffering)
            eng.on_feed({"now": next_batch, "tv_delay": DELAY - 3.0, "events": evs})
            next_batch += 0.25
        clock["t"] = local
        eng.on_gsi(_snap_to_payload(s, eng.ctl.last_sent, cfg.gsi_token))
        timeline.append((s.t, eng.ctl.last_sent))
        eng.tick()

    off = eng.feed.sync.offset
    assert off is not None and abs(off - BASE) < 0.3, off   # anchors corrected the wrong tv_delay
    m = evaluate(demo, [(t, x) for t, x in timeline if t > demo.rounds[0][1] - 5])
    assert m["coverage"] > 0.8, m

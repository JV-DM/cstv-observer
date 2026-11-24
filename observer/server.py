"""Live runtime: GSI HTTP endpoint + director loop + netcon camera control + status dashboard.

Endpoints
  POST /gsi      CS2 game state integration (configured by gamestate_integration_cstv_observer.cfg)
  POST /feed     real-time events from the CounterStrikeSharp plugin (lookahead mode)
  GET  /status   JSON status
  GET  /         small live dashboard
  POST /control  {"action": "pause"|"resume"}
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .controller import NetconController
from .director import Director
from .gsi import parse_payload
from .lookahead import ClockSync, FutureFeed, LookaheadDirector
from .model import Snapshot
from .tracker import Tracker

log = logging.getLogger("observer")


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.tracker = Tracker()
        self.feed = FutureFeed(ClockSync()) if cfg.lookahead else None
        if cfg.lookahead:
            self.director = LookaheadDirector(cfg.weights, cfg.plan, feed=self.feed)
        else:
            self.director = Director(cfg.weights)
        self.ctl = NetconController(cfg.netcon_host, cfg.netcon_port, cfg.command_template,
                                    cfg.fallback_template, cfg.init_commands, dry_run=cfg.dry_run)
        self.snap: Snapshot | None = None
        self.paused_until = 0.0
        self.paused_manual = False
        self.last_reason = ""
        self.stats = {"kills": 0, "seen": 0, "cuts": 0}
        self.cut_log: list[dict] = []
        self._last_cut_t = 0.0
        self.last_gsi: float | None = None
        self._next_connect = 0.0

    # ------------------------------------------------------------------ inputs
    def on_gsi(self, data: dict):
        now = time.monotonic()
        snap = parse_payload(data, now)
        self.last_gsi = now
        with self.lock:
            if self.snap is not None and snap.map_name != self.snap.map_name:
                self.tracker = Tracker()
            kills = self.tracker.update(snap)
            for k in kills:
                self.stats["kills"] += 1
                if snap.spectated in (k.victim, k.killer) or (self.snap and self.snap.spectated in (k.victim, k.killer)):
                    self.stats["seen"] += 1
            self.ctl.observe_feedback(snap.spectated)
            last = snap.players.get(self.ctl.last_sent or "")
            if (last is not None and last.alive and self.ctl.human_override(snap.spectated)
                    and now - self._last_cut_t > 1.5 and snap.spectated in snap.players):
                if now >= self.paused_until:
                    log.info("manual camera change detected -> pausing auto director for %.0fs",
                             self.cfg.manual_override_seconds)
                self.paused_until = now + self.cfg.manual_override_seconds
                self.ctl.last_sent = snap.spectated
                base = self.director.base if isinstance(self.director, LookaheadDirector) else self.director
                base.current, base.since = snap.spectated, now
            self.snap = snap
            self._step(now, kills)

    def on_feed(self, batch: dict):
        if self.feed is not None:
            self.feed.ingest(batch, time.monotonic())

    def tick(self):
        now = time.monotonic()
        if not self.ctl.connected and now >= self._next_connect:
            self._next_connect = now + 3.0
            self.ctl.try_connect()
        with self.lock:
            if self.snap is not None:
                self._step(time.monotonic(), [])

    # ------------------------------------------------------------------ core
    def _step(self, now: float, kills):
        snap = self.snap
        if snap is None or not snap.players or snap.phase == "warmup":
            return
        if self.paused_manual or now < self.paused_until:
            return
        dec = self.director.decide(snap, self.tracker, now, kills)
        self.last_reason = dec.reason
        if dec.switched and dec.target in snap.players:
            p = snap.players[dec.target]
            if self.ctl.spectate(p):
                self.stats["cuts"] += 1
                self._last_cut_t = now
                self.cut_log.append({"t": round(now, 2), "to": p.name, "reason": dec.reason})
                self.cut_log = self.cut_log[-30:]
                log.info("CUT -> %-20s %s", p.name, dec.reason)
        elif dec.target and snap.spectated and snap.spectated != dec.target and self.ctl._pending is None \
                and now - self._last_cut_t > 2.0 and dec.target in snap.players:
            # camera drifted (CS auto-switched after a death, etc.): re-assert
            self.ctl.spectate(snap.players[dec.target])
            self._last_cut_t = now

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        with self.lock:
            snap = self.snap
            base = self.director.base if isinstance(self.director, LookaheadDirector) else self.director
            scores = base.last_scores
            players = []
            if snap:
                for sid, p in snap.players.items():
                    s = scores.get(sid)
                    players.append({
                        "name": p.name, "team": p.team, "hp": p.health, "alive": p.alive,
                        "score": round(s.total, 2) if s else 0, "reason": s.reason if s else "",
                        "eng": round(s.eng, 2) if s else 0, "act": round(s.act, 2) if s else 0,
                        "situ": round(s.situ, 2) if s else 0,
                        "on_camera": sid == snap.spectated, "target": sid == base.current,
                    })
                players.sort(key=lambda x: -x["score"])
            out = {
                "connected": snap is not None,
                "map": snap.map_name if snap else None,
                "round": snap.round_no if snap else None,
                "phase": snap.phase if snap else None,
                "reason": self.last_reason,
                "paused": self.paused_manual or time.monotonic() < self.paused_until,
                "stats": self.stats,
                "command_template": self.ctl.templates[self.ctl.tpl_idx],
                "players": players,
                "cuts": self.cut_log[-12:][::-1],
                "lookahead": None,
            }
            if self.feed is not None:
                off = self.feed.sync.offset
                out["lookahead"] = {"synced": off is not None, "anchors": len(self.feed.sync._anchors),
                                    "events": len(self.feed.kills),
                                    "plan": [{"in": round(w.start - time.monotonic(), 1), "target":
                                              snap.players[w.target].name if snap and w.target in snap.players else w.target,
                                              "kind": w.kind} for w in getattr(self.director, "_plan", [])[:5]]}
            return out


class Host:
    """What the HTTP handler and the ticker talk to. `engine` can be swapped (settings change)."""

    def __init__(self, engine: Engine, app=None):
        self._engine = engine
        self.app = app

    @property
    def engine(self) -> Engine:
        return self.app.engine if self.app is not None else self._engine


def make_handler(host: Host):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _body(self) -> dict | None:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return None

        def _reply(self, code=200, body=b"", ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._reply(code, json.dumps(obj).encode(), "application/json")

        def _local_ui_ok(self) -> bool:
            # UI/control endpoints: only from this computer, and not from other web pages (CSRF)
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin.startswith(("http://127.0.0.1", "http://localhost"))

        def do_POST(self):
            engine = host.engine
            cfg = engine.cfg
            data = self._body()
            if data is None:
                return self._reply(400)
            if self.path.startswith("/gsi") or self.path == "/":
                tok = (data.get("auth") or {}).get("token")
                if cfg.gsi_token and tok != cfg.gsi_token:
                    return self._reply(403)
                engine.on_gsi(data)
                return self._reply(200)
            if self.path.startswith("/feed"):
                tok = self.headers.get("Authorization", "").removeprefix("Bearer ").strip() or data.get("token")
                if not cfg.lookahead or tok != cfg.feed_token:
                    return self._reply(403)
                engine.on_feed(data)
                return self._reply(200)
            if not self._local_ui_ok():
                return self._reply(403)
            if self.path.startswith("/api/action"):
                if host.app is not None:
                    return self._json(host.app.action(data))
                a = data.get("action")
                if a in ("pause", "resume"):
                    engine.paused_manual = a == "pause"
                    if a == "resume":
                        engine.paused_until = 0.0
                    return self._json({"ok": True})
                return self._json({"ok": False, "message": "not available"})
            if self.path.startswith("/control"):
                a = data.get("action")
                engine.paused_manual = a == "pause"
                if a == "resume":
                    engine.paused_until = 0.0
                return self._reply(200)
            return self._reply(404)

        def do_GET(self):
            if not self._local_ui_ok():
                return self._reply(403)
            if self.path.startswith("/status"):
                return self._json(host.engine.status())
            if self.path.startswith("/api/setup"):
                return self._json(host.app.setup_status() if host.app else {})
            if self.path == "/" or self.path.startswith("/index"):
                from .ui import PAGE
                return self._reply(200, PAGE.encode(), "text/html; charset=utf-8")
            return self._reply(404)

    return H


def _serve(host: Host, listen_host: str, port: int, tick_hz: float, stop: threading.Event):
    srv = ThreadingHTTPServer((listen_host, port), make_handler(host))
    srv.daemon_threads = True

    def ticker():
        period = 1.0 / max(1.0, tick_hz)
        while not stop.is_set():
            try:
                host.engine.tick()
            except Exception:  # never let the loop die
                log.exception("tick failed")
            stop.wait(period)

    threading.Thread(target=ticker, daemon=True).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("listening on http://%s:%s", listen_host, port)
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()


def run(cfg: Config):
    """CLI entry point (python -m observer run)."""
    log.info("lookahead=%s  dry_run=%s  (dashboard: http://127.0.0.1:%s/)", cfg.lookahead, cfg.dry_run, cfg.listen_port)
    _serve(Host(Engine(cfg)), cfg.listen_host, cfg.listen_port, cfg.tick_hz, threading.Event())


def serve(app):
    """App entry point: runs until the user clicks Quit."""
    cfg = app.engine.cfg
    _serve(Host(None, app), cfg.listen_host, cfg.listen_port, cfg.tick_hz, app.quit_event)

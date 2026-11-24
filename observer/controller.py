"""Camera control through CS2's network console (launch CS2 with  -netconport 2121 ).

Commands are templates so they can be changed without code if a CS2 update breaks one:
    {accountid}  Steam account id (steamid64 - 76561197960265728)
    {steamid}    steamid64
    {slot}       observer slot from GSI (0-9)
    {name}       player name

The controller verifies every cut against the GSI `player.steamid` (who is actually on screen).
If the primary template does not take effect twice in a row it switches to the fallback template.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

from .model import PlayerSnap, steam64_to_accountid

log = logging.getLogger("observer.netcon")


class NetconController:
    def __init__(self, host: str = "127.0.0.1", port: int = 2121,
                 template: str = "spec_player_by_accountid {accountid}",
                 fallback_template: str = 'spec_player "{name}"',
                 init_commands: list[str] | None = None,
                 verify_timeout: float = 1.5, dry_run: bool = False):
        self.host, self.port = host, port
        self.templates = [template, fallback_template] if fallback_template else [template]
        self.tpl_idx = 0
        self.init_commands = init_commands if init_commands is not None else ["spec_autodirector 0"]
        self.verify_timeout = verify_timeout
        self.dry_run = dry_run
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._pending: tuple[str, float] | None = None   # (target steamid, sent at)
        self._failures = 0
        self.last_sent: str | None = None
        self.last_command: str | None = None

    # ------------------------------------------------------------------ socket
    @property
    def connected(self) -> bool:
        return self.dry_run or self._sock is not None

    def try_connect(self) -> bool:
        """Background reconnect attempt (quiet)."""
        with self._lock:
            return self._connect(quiet=True)

    def _connect(self, quiet: bool = False) -> bool:
        if self.dry_run:
            return True
        if self._sock is not None:
            return True
        try:
            s = socket.create_connection((self.host, self.port), timeout=2.0)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(0.5)
            self._sock = s
            log.info("connected to CS2 netcon %s:%s", self.host, self.port)
            threading.Thread(target=self._drain, args=(s,), daemon=True).start()
            for c in self.init_commands:
                self._send_raw(c)
            return True
        except OSError as e:
            (log.debug if quiet else log.warning)(
                "netcon connect failed (%s). Is CS2 running with -netconport %s ?", e, self.port)
            self._sock = None
            return False

    def _drain(self, s: socket.socket):
        """Read and discard console output so CS2's send buffer never fills up."""
        while self._sock is s:
            try:
                if not s.recv(65536):
                    break
            except socket.timeout:
                continue
            except OSError:
                break
        if self._sock is s:
            self._sock = None

    def _send_raw(self, cmd: str) -> bool:
        self.last_command = cmd
        if self.dry_run:
            log.info("[dry-run] %s", cmd)
            return True
        try:
            assert self._sock is not None
            self._sock.sendall((cmd + "\n").encode("utf-8"))
            return True
        except (OSError, AssertionError) as e:
            log.warning("netcon send failed: %s", e)
            try:
                if self._sock:
                    self._sock.close()
            finally:
                self._sock = None
            return False

    def send(self, cmd: str) -> bool:
        with self._lock:
            if not self._connect():
                return False
            return self._send_raw(cmd)

    # ------------------------------------------------------------------ camera
    def render(self, p: PlayerSnap, tpl: str | None = None) -> str | None:
        tpl = tpl or self.templates[self.tpl_idx]
        acc = steam64_to_accountid(p.steamid)
        if "{accountid}" in tpl and acc is None:
            return None          # bots have no account id
        if "{slot}" in tpl and p.slot is None:
            return None
        return tpl.format(accountid=acc, steamid=p.steamid, slot=p.slot, name=p.name.replace('"', ""))

    def spectate(self, p: PlayerSnap) -> bool:
        cmd = self.render(p)
        if cmd is None:
            for tpl in self.templates:
                cmd = self.render(p, tpl)
                if cmd:
                    break
        if not cmd:
            return False
        ok = self.send(cmd)
        if ok:
            self.last_sent = p.steamid
            self._pending = (p.steamid, time.monotonic())
        return ok

    def observe_feedback(self, spectated: str | None):
        """Call with GSI player.steamid on every payload."""
        if self._pending is None:
            return
        target, sent = self._pending
        if spectated == target:
            self._pending = None
            self._failures = 0
            return
        if time.monotonic() - sent > self.verify_timeout:
            self._pending = None
            self._failures += 1
            if self._failures >= 2 and len(self.templates) > 1:
                self.tpl_idx = (self.tpl_idx + 1) % len(self.templates)
                self._failures = 0
                log.warning("camera did not follow; switching command template to: %s", self.templates[self.tpl_idx])

    def human_override(self, spectated: str | None, grace: float = 1.0) -> bool:
        """True if someone changed the camera by hand (spectated != our last target after the grace period)."""
        if spectated is None or self.last_sent is None or self._pending is not None:
            return False
        return spectated != self.last_sent

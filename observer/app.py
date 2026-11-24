"""Double-click app for non-technical users.

What it does on start:
  * loads (or creates) settings in %APPDATA%\\CSTV Observer\\settings.json
  * finds the CS2 install through Steam and installs the GSI config file automatically
  * starts the observer and opens the control page in the default browser
  * the page shows a setup checklist, a "Launch CS2" button (starts CS2 through Steam with the
    right launch options), Start/Pause, live info and a Quit button

Run from source with:  python -m observer app
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config
from .gsi import render_gsi_cfg

APP_NAME = "CSTV Observer"
CFG_FILENAME = "gamestate_integration_cstv_observer.cfg"
CS2_APPID = 730
log = logging.getLogger("observer.app")


# --------------------------------------------------------------------------- settings
def data_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(Path.home(), ".config")
    p = Path(base) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Settings:
    port: int = 3500
    netcon_port: int = 2121
    gsi_token: str = ""
    insecure: bool = True           # -insecure launch option (needed for console control on recent CS2)
    weights_file: str = ""
    cs2_cfg_dir: str = ""           # user override if auto-detect fails

    @classmethod
    def load(cls) -> "Settings":
        path = data_dir() / "settings.json"
        s = cls()
        if path.exists():
            try:
                d = json.loads(path.read_text("utf-8"))
                names = {f.name for f in fields(cls)}
                for k, v in d.items():
                    if k in names:
                        setattr(s, k, v)
            except (OSError, ValueError):
                log.warning("settings.json unreadable, using defaults")
        changed = False
        if not s.gsi_token:
            s.gsi_token, changed = secrets.token_hex(8), True
        if changed or not path.exists():
            s.save()
        return s

    def save(self):
        (data_dir() / "settings.json").write_text(json.dumps(asdict(self), indent=2), "utf-8")

    def to_config(self) -> Config:
        cfg = Config()
        cfg.listen_host = "127.0.0.1"   # only this computer can talk to the app
        cfg.listen_port = int(self.port)
        cfg.gsi_token = self.gsi_token
        cfg.netcon_port = int(self.netcon_port)
        cfg.lookahead = False
        if self.weights_file and os.path.exists(self.weights_file):
            from .scoring import Weights
            with open(self.weights_file) as f:
                cfg.weights = Weights.from_dict({**cfg.weights.to_dict(), **json.load(f)})
        return cfg

    def launch_options(self) -> str:
        opts = f"-netconport {int(self.netcon_port)}"
        if self.insecure:
            opts += " -insecure"
        return opts


# --------------------------------------------------------------------------- CS2 detection
def steam_path() -> Path | None:
    if sys.platform == "win32":
        try:
            import winreg
            for hive, key, val in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                   (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        p = Path(winreg.QueryValueEx(k, val)[0])
                        if p.exists():
                            return p
                except OSError:
                    continue
        except ImportError:
            pass
        for p in (Path(r"C:\Program Files (x86)\Steam"), Path(r"C:\Program Files\Steam")):
            if p.exists():
                return p
        return None
    for p in (Path.home() / ".steam/steam", Path.home() / ".local/share/Steam"):
        if p.exists():
            return p
    return None


def library_folders(steam: Path) -> list[Path]:
    libs = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text("utf-8", errors="replace")
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(m.group(1).replace("\\\\", "\\"))
            if p not in libs:
                libs.append(p)
    except OSError:
        pass
    return libs


def find_cs2_cfg_dir(override: str = "") -> Path | None:
    if override and Path(override).is_dir():
        return Path(override)
    steam = steam_path()
    if not steam:
        return None
    for lib in library_folders(steam):
        cfg = lib / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game" / "csgo" / "cfg"
        if cfg.is_dir():
            return cfg
    return None


def install_gsi_cfg(settings: Settings) -> tuple[bool, str]:
    cfg_dir = find_cs2_cfg_dir(settings.cs2_cfg_dir)
    if cfg_dir is None:
        return False, "Counter-Strike 2 was not found on this computer."
    target = cfg_dir / CFG_FILENAME
    content = render_gsi_cfg(settings.port, settings.gsi_token)
    try:
        if target.exists() and target.read_text("utf-8", errors="replace") == content:
            return True, str(target)
        target.write_text(content, "utf-8")
        return True, str(target)
    except OSError as e:
        return False, f"Could not write {target}: {e}"


def gsi_cfg_installed(settings: Settings) -> bool:
    cfg_dir = find_cs2_cfg_dir(settings.cs2_cfg_dir)
    if cfg_dir is None:
        return False
    try:
        return (cfg_dir / CFG_FILENAME).read_text("utf-8", errors="replace") == render_gsi_cfg(settings.port, settings.gsi_token)
    except OSError:
        return False


def launch_cs2(settings: Settings) -> tuple[bool, str]:
    """Start CS2 through Steam with our launch options. Steam asks the user to confirm the options once."""
    url = f"steam://run/{CS2_APPID}//{urllib.parse.quote(settings.launch_options())}/"
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            webbrowser.open(url)
        return True, url
    except OSError as e:
        return False, str(e)


# --------------------------------------------------------------------------- app controller
class App:
    """Glue between the web UI and the engine; also owns settings and can rebuild the engine."""

    def __init__(self, settings: Settings, dry_run: bool = False):
        self.settings = settings
        self.dry_run = dry_run
        self.quit_event = threading.Event()
        self.engine = None
        self.message = ""
        self.rebuild()

    def rebuild(self):
        from .server import Engine
        cfg = self.settings.to_config()
        cfg.dry_run = self.dry_run
        old = self.engine
        self.engine = Engine(cfg)
        if old is not None:
            self.engine.paused_manual = old.paused_manual

    def setup_status(self) -> dict:
        s = self.settings
        eng = self.engine
        cfg_dir = find_cs2_cfg_dir(s.cs2_cfg_dir)
        now = time.monotonic()
        gsi_ok = eng.last_gsi is not None and now - eng.last_gsi < 10
        snap = eng.snap
        return {
            "cs2_found": cfg_dir is not None,
            "cs2_dir": str(cfg_dir) if cfg_dir else "",
            "cfg_installed": gsi_cfg_installed(s),
            "gsi_live": gsi_ok,
            "spectating": bool(gsi_ok and snap and snap.players),
            "camera_control": eng.ctl.connected or self.dry_run,
            "launch_options": s.launch_options(),
            "settings": asdict(s),
            "message": self.message,
            "dry_run": self.dry_run,
        }

    def action(self, data: dict) -> dict:
        a = data.get("action")
        if a == "install_cfg":
            ok, msg = install_gsi_cfg(self.settings)
            self.message = ("Game config installed. If CS2 is already open, restart it." if ok else msg)
            return {"ok": ok, "message": self.message}
        if a == "launch_cs2":
            install_gsi_cfg(self.settings)
            ok, msg = launch_cs2(self.settings)
            self.message = ("Starting CS2… If Steam asks about launch options, click Continue / OK."
                            if ok else f"Could not start CS2: {msg}")
            return {"ok": ok, "message": self.message}
        if a == "save_settings":
            d = data.get("settings") or {}
            port_before = self.settings.port
            for k in ("netcon_port", "insecure", "cs2_cfg_dir", "weights_file"):
                if k in d:
                    setattr(self.settings, k, type(getattr(self.settings, k))(d[k]))
            self.settings.save()
            install_gsi_cfg(self.settings)
            self.rebuild()
            self.message = "Settings saved."
            if self.settings.port != port_before:
                self.message += " Restart the app to use the new port."
            return {"ok": True, "message": self.message}
        if a == "quit":
            self.quit_event.set()
            return {"ok": True}
        if a in ("pause", "resume"):
            self.engine.paused_manual = a == "pause"
            if a == "resume":
                self.engine.paused_until = 0.0
            return {"ok": True}
        return {"ok": False, "message": f"unknown action {a}"}


# --------------------------------------------------------------------------- entry point
def _setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(data_dir() / "observer.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def selftest() -> int:
    """Used by the build pipeline: exercises the bundled code paths without needing CS2."""
    from .director import Director
    from .gsi import parse_payload
    from .lookahead import LookaheadDirector
    from .tracker import Tracker
    payload = {
        "map": {"name": "de_test", "round": 1},
        "phase_countdowns": {"phase": "live"},
        "allplayers": {
            "76561198000000001": {"name": "a", "team": "CT", "state": {"health": 100},
                                  "position": "0, 0, 0", "forward": "1, 0, 0"},
            "76561198000000002": {"name": "b", "team": "T", "state": {"health": 100},
                                  "position": "600, 0, 0", "forward": "-1, 0, 0"},
        },
    }
    snap = parse_payload(payload, 1.0)
    tr = Tracker()
    tr.update(snap)
    d1 = Director().decide(snap, tr, 1.0)
    d2 = LookaheadDirector().decide(snap, tr, 1.0)
    assert d1.target and d2.target, "director produced no target"
    assert render_gsi_cfg(3500, "x").startswith('"cstv_observer"')
    s = Settings()
    assert "-netconport" in s.launch_options()
    print(f"selftest OK (steam={steam_path()}, cs2_cfg={find_cs2_cfg_dir()})")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    _setup_logging()
    settings = Settings.load()
    url = f"http://127.0.0.1:{settings.port}/"
    if _port_in_use(settings.port):
        # already running: just bring the page up again
        webbrowser.open(url)
        return 0
    app = App(settings, dry_run="--dry-run" in argv)
    ok, msg = install_gsi_cfg(settings)
    log.info("GSI config: %s", msg if ok else f"not installed ({msg})")
    from .server import serve
    if "--no-browser" not in argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    serve(app)
    return 0


if __name__ == "__main__":
    sys.exit(main())

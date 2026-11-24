from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field, fields

from .lookahead import PlanConfig
from .scoring import Weights


@dataclass
class Config:
    listen_host: str = "127.0.0.1"
    listen_port: int = 3500
    gsi_token: str = "cstv-observer"
    netcon_host: str = "127.0.0.1"
    netcon_port: int = 2121
    command_template: str = "spec_player_by_accountid {accountid}"
    fallback_template: str = 'spec_player "{name}"'
    init_commands: list[str] = field(default_factory=lambda: ["spec_autodirector 0"])
    dry_run: bool = False
    manual_override_seconds: float = 8.0
    tick_hz: float = 10.0
    # lookahead
    lookahead: bool = False
    feed_token: str = "change-me"
    weights: Weights = field(default_factory=Weights)
    plan: PlanConfig = field(default_factory=PlanConfig)


def _apply(obj, d: dict):
    names = {f.name for f in fields(obj)}
    for k, v in d.items():
        if k in names and not isinstance(v, dict):
            setattr(obj, k, v)


def load_config(path: str | None, weights_path: str | None = None) -> Config:
    cfg = Config()
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        _apply(cfg, data.get("observer", {}))
        _apply(cfg, data.get("netcon", {}))
        _apply(cfg, data.get("lookahead", {}))
        _apply(cfg.plan, data.get("lookahead", {}).get("plan", {}))
        cfg.weights = Weights.from_dict({**cfg.weights.to_dict(), **data.get("weights", {})})
        weights_path = weights_path or data.get("observer", {}).get("weights_file")
    if weights_path:
        with open(weights_path) as f:
            cfg.weights = Weights.from_dict({**cfg.weights.to_dict(), **json.load(f)})
    return cfg

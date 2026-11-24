"""CSTV Observer command line.

  python -m observer run      [--config observer.toml] [--dry-run] [--lookahead]
  python -m observer gsi-cfg  [--config observer.toml] > gamestate_integration_cstv_observer.cfg
  python -m observer bench    demo1.dem [demo2.dem ...] [--weights weights.json]
  python -m observer tune     demo1.dem ... --iters 200 --out weights.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="observer", description="Auto-director for CS2 CSTV that maximizes kills on screen")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the live auto observer")
    r.add_argument("--config", default=None)
    r.add_argument("--weights", default=None, help="weights.json produced by `tune`")
    r.add_argument("--dry-run", action="store_true", help="don't connect to CS2, just log cuts")
    r.add_argument("--lookahead", action="store_true", help="enable lookahead mode (needs the server plugin)")
    r.add_argument("-v", "--verbose", action="store_true")

    g = sub.add_parser("gsi-cfg", help="print the CS2 GSI config file")
    g.add_argument("--config", default=None)

    b = sub.add_parser("bench", help="measure kill coverage on demos vs. baseline observers")
    b.add_argument("demos", nargs="+")
    b.add_argument("--weights", default=None)
    b.add_argument("--latency", type=float, default=0.1)

    t = sub.add_parser("tune", help="tune weights on demos")
    t.add_argument("demos", nargs="+")
    t.add_argument("--iters", type=int, default=150)
    t.add_argument("--workers", type=int, default=2)
    t.add_argument("--holdout", type=int, default=0, help="number of demos kept out for validation")
    t.add_argument("--weights", default=None, help="start from these weights")
    t.add_argument("--out", default="weights.json")
    t.add_argument("--seed", type=int, default=0)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if getattr(a, "verbose", False) else logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")

    if a.cmd == "run":
        from .config import load_config
        from .server import run
        cfg = load_config(a.config, a.weights)
        if a.dry_run:
            cfg.dry_run = True
        if a.lookahead:
            cfg.lookahead = True
        run(cfg)

    elif a.cmd == "gsi-cfg":
        from .config import load_config
        from .gsi import render_gsi_cfg
        cfg = load_config(a.config)
        sys.stdout.write(render_gsi_cfg(cfg.listen_port, cfg.gsi_token))

    elif a.cmd == "bench":
        from .bench import (NearestEnemyDirector, RandomDirector, ReactiveDirector, evaluate, load_demo,
                            objective, simulate)
        from .director import Director
        from .lookahead import LookaheadDirector
        from .scoring import Weights
        w = Weights()
        if a.weights:
            with open(a.weights) as f:
                w = Weights.from_dict({**w.to_dict(), **json.load(f)})
        rows = [
            ("random (every 6s)", lambda: RandomDirector(), False),
            ("reactive (follow killer)", lambda: ReactiveDirector(), False),
            ("nearest enemy", lambda: NearestEnemyDirector(), False),
            ("cstv-observer heuristic", lambda: Director(w), False),
            ("cstv-observer lookahead", lambda: LookaheadDirector(w), True),
        ]
        totals = {name: [] for name, _, _ in rows}
        for path in a.demos:
            d = load_demo(path)
            print(f"\n{d.name}: {len(d.deaths)} kills, {len(d.rounds)} rounds")
            print(f"  {'director':28s} {'coverage':>8s} {'killerPOV':>9s} {'early2s':>8s} {'cuts/min':>8s} {'short%':>7s} {'score':>6s}")
            for name, mk, oracle in rows:
                m = evaluate(d, simulate(d, mk, latency=a.latency, oracle=oracle))
                totals[name].append(m)
                print(f"  {name:28s} {m['coverage']:8.1%} {m['killer_pov']:9.1%} {m['anticipated']:8.1%} "
                      f"{m['cuts_per_min']:8.1f} {m['short_shots']:7.1%} {objective(m):6.3f}")
        if len(a.demos) > 1:
            print("\nmean over demos:")
            for name, ms in totals.items():
                cov = sum(m["coverage"] for m in ms) / len(ms)
                print(f"  {name:28s} coverage {cov:.1%}")

    elif a.cmd == "tune":
        from .tune import save, tune
        start = None
        if a.weights:
            with open(a.weights) as f:
                start = json.load(f)
        best = tune(a.demos, iters=a.iters, seed=a.seed, workers=a.workers, start=start, holdout=a.holdout)
        save(best, a.out)
        print(f"saved {a.out}")


if __name__ == "__main__":
    main()

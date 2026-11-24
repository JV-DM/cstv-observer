"""Tune director weights on your own demos with a (1+lambda) evolution strategy.

    python -m observer tune demos/*.dem --iters 200 --out weights.json

Each candidate perturbs a random subset of weights (log-normal), is evaluated on every demo
in parallel, and is kept if the mean objective improves. Use many demos (10+) to avoid
overfitting; with `--holdout` the last demo(s) are only used for reporting.
"""
from __future__ import annotations

import json
import math
import random
from concurrent.futures import ProcessPoolExecutor

from .bench import evaluate, load_demo, objective, simulate
from .director import Director
from .scoring import Weights

# parameters that are not worth perturbing / must stay in range
_BOUNDS = {
    "min_dwell": (0.8, 4.0), "min_dwell_urgent": (0.3, 2.0), "death_hold": (0.3, 1.8),
    "margin": (0.0, 1.5), "fight_lock": (0.2, 1.0), "killer_follow": (0.0, 1.0),
    "adv": (0.0, 0.9), "second_enemy": (0.0, 1.0), "flashed_penalty": (0.0, 0.9),
    "utility_penalty": (0.0, 0.9), "aim_sigma_deg": (2.0, 25.0),
    "confirm": (0.0, 1.0), "return_window": (0.0, 10.0), "return_margin": (0.0, 2.0),
}

_DEMOS: list = []


def _init(paths, step):
    global _DEMOS
    _DEMOS = [load_demo(p, step=step) for p in paths]


def _eval(wd: dict) -> list[dict]:
    w = Weights.from_dict(wd)
    return [evaluate(d, simulate(d, lambda: Director(w))) for d in _DEMOS]


def mutate(wd: dict, rng: random.Random, sigma: float) -> dict:
    out = dict(wd)
    keys = list(out)
    for k in rng.sample(keys, k=max(1, len(keys) // 5)):
        v = out[k]
        if v == 0:
            v = 0.05
        v *= math.exp(rng.gauss(0, sigma))
        lo, hi = _BOUNDS.get(k, (0.0, 50.0))
        out[k] = min(hi, max(lo, v))
    return out


def tune(paths: list[str], iters: int = 150, seed: int = 0, workers: int = 2, start: dict | None = None,
         holdout: int = 0, step: int = 4, log=print) -> dict:
    train, test = (paths[:-holdout], paths[-holdout:]) if holdout else (paths, [])
    rng = random.Random(seed)
    best = {**Weights().to_dict(), **(start or {})}
    with ProcessPoolExecutor(max_workers=workers, initializer=_init, initargs=(train, step)) as ex:
        def score(wd):
            ms = list(ex.map(_eval, [wd]))[0]
            return sum(objective(m) for m in ms) / len(ms), ms

        best_obj, best_ms = score(best)
        log(f"start objective={best_obj:.4f} coverage={_mean(best_ms, 'coverage'):.3f}")
        sigma = 0.35
        it = 0
        while it < iters:
            cands = [mutate(best, rng, sigma) for _ in range(workers)]
            results = list(ex.map(_eval, cands))
            it += len(cands)
            improved = False
            for wd, ms in zip(cands, results):
                o = sum(objective(m) for m in ms) / len(ms)
                if o > best_obj + 1e-6:
                    best, best_obj, best_ms, improved = wd, o, ms, True
            if improved:
                log(f"[{it:4d}] objective={best_obj:.4f} coverage={_mean(best_ms, 'coverage'):.3f} "
                    f"anticipated={_mean(best_ms, 'anticipated'):.3f} cuts/min={_mean(best_ms, 'cuts_per_min'):.1f}")
            sigma = max(0.08, sigma * 0.995)
    if test:
        _init(test, step)
        ms = _eval(best)
        log(f"holdout coverage={_mean(ms, 'coverage'):.3f} objective={sum(objective(m) for m in ms) / len(ms):.4f}")
    return best


def _mean(ms, k):
    return sum(m[k] for m in ms) / max(len(ms), 1)


def save(wd: dict, path: str):
    with open(path, "w") as f:
        json.dump({k: round(v, 4) for k, v in wd.items()}, f, indent=2)

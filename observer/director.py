"""Camera policy: turn per-player scores into a stable, watchable sequence of cuts.

Rules (in priority order):
  1. Current target died      -> hold the death for `death_hold` s, then cut to the killer if he is still
                                 in action (trade / multi-kill potential), otherwise to the best player.
  2. Never cut before `min_dwell` s (or `min_dwell_urgent` when something urgent is happening elsewhere
     and the current view is quiet).
  3. Don't leave a fight: if the current player is in a fight, the challenger must be much better.
  4. Cut when the best player beats the current one by a relative + absolute margin (hysteresis).
  5. If the current view has been idle for a while, take the best alternative with a smaller margin.
  6. A normal cut (rules 4-5) needs the challenger to stay ahead for `confirm` s, so a one-frame score
     spike doesn't move the camera; going back to the player just left needs an extra margin.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Snapshot
from .scoring import Score, Scorer, Weights
from .tracker import KillEvent, Tracker


@dataclass
class Decision:
    target: str | None
    reason: str
    switched: bool = False
    scores: dict[str, Score] = field(default_factory=dict)


class Director:
    def __init__(self, weights: Weights | None = None):
        self.w = weights or Weights()
        self.scorer = Scorer(self.w)
        self.current: str | None = None
        self.since: float = -1e9
        self.idle_since: float | None = None
        self.previous: str | None = None
        self.challenger: str | None = None
        self.challenger_since: float = 0.0
        self.last_scores: dict[str, Score] = {}

    def reset(self):
        self.current = None
        self.since = -1e9
        self.idle_since = None
        self.previous = None
        self.challenger = None

    def _cut(self, target: str, now: float, reason: str, scores) -> Decision:
        switched = target != self.current
        if switched:
            self.previous = self.current
            self.current = target
            self.since = now
            self.idle_since = None
        self.challenger = None
        return Decision(target, reason, switched, scores)

    def _confirmed(self, target: str, now: float) -> bool:
        """True once `target` has been the wanted cut for `confirm` seconds in a row."""
        if self.challenger != target:
            self.challenger, self.challenger_since = target, now
        return now - self.challenger_since >= self.w.confirm

    def decide(self, snap: Snapshot, tr: Tracker, now: float, kills: list[KillEvent] | None = None,
               hints: dict[str, float] | None = None, allowed: set[str] | None = None) -> Decision:
        w = self.w
        scores = self.scorer.score(snap, tr, now, hints)
        self.last_scores = scores
        if allowed is not None:
            scores_c = {k: v for k, v in scores.items() if k in allowed}
        else:
            scores_c = scores
        if not scores_c:
            return Decision(self.current, "no candidates", False, scores)

        best_id = max(scores_c, key=lambda k: scores_c[k].total)
        best = scores_c[best_id]
        cur_id = self.current
        cur_p = snap.players.get(cur_id) if cur_id else None
        dwell = now - self.since

        # 1. nobody selected yet / player left
        if cur_p is None:
            return self._cut(best_id, now, f"start: {best.reason}", scores)

        # 2. current target is dead
        if not cur_p.alive:
            ctr = tr.tracks.get(cur_id)
            died = ctr.died_at if ctr and ctr.died_at is not None else self.since
            if now - died < w.death_hold:
                return Decision(cur_id, "showing death", False, scores)
            killer = ctr.killed_by if ctr else None
            if killer and killer in scores_c and scores_c[killer].total >= w.killer_follow * best.total:
                return self._cut(killer, now, "follow killer", scores)
            return self._cut(best_id, now, f"after death: {best.reason}", scores)

        cur = scores.get(cur_id, Score())
        if best_id == cur_id:
            self.idle_since = None if cur.total > w.idle_level else (self.idle_since or now)
            self.challenger = None
            return Decision(cur_id, cur.reason, False, scores)

        # idle bookkeeping
        if cur.total < w.idle_level:
            if self.idle_since is None:
                self.idle_since = now
        else:
            self.idle_since = None

        urgent = best.total >= w.urgent_level and cur.total < 0.5 * best.total
        if dwell < (w.min_dwell_urgent if urgent else w.min_dwell):
            self.challenger = None
            return Decision(cur_id, "dwell", False, scores)

        margin = w.margin
        if cur.in_fight > w.fight_lock:
            margin = margin * 2.5 + 0.5           # don't leave a live fight lightly
        if best_id == self.previous and dwell < w.return_window:
            margin += w.return_margin             # don't bounce straight back
        need = cur.total * (1.0 + margin) + w.abs_margin
        if best.total > need:
            if urgent or self._confirmed(best_id, now):
                return self._cut(best_id, now, best.reason, scores)
            return Decision(cur_id, cur.reason, False, scores)

        if self.idle_since is not None and now - self.idle_since > w.idle_switch_after \
                and best.total > cur.total + w.abs_margin * 0.5:
            if self._confirmed(best_id, now):
                return self._cut(best_id, now, f"idle -> {best.reason}", scores)
            return Decision(cur_id, cur.reason, False, scores)

        self.challenger = None
        return Decision(cur_id, cur.reason, False, scores)

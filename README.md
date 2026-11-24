# cstv-observer

An auto-director for CS2 CSTV (GOTV) that tries to have the camera on the right player **before** each kill.

It runs next to a CS2 client that is spectating a match (CSTV relay, GOTV, or a demo). It reads Game State
Integration (GSI) about 20 times a second, scores every player on how likely they are to be in a kill in the
next few seconds, and cuts the camera through CS2's network console (`-netconport`).

There are two modes:

| mode | needs | what it knows |
|---|---|---|
| **heuristic** (default) | only the spectating client | predicts fights from positions, aim, shooting and damage |
| **lookahead** | a small plugin on the game server | CSTV is delayed (`tv_delay`), so the server already knows every kill 90+ s before viewers see it; the camera is planned around them |

## Easiest way: the Windows app

1. Open the repo's **Releases** page and download **CSTV-Observer.exe**.
2. Double-click it. If Windows says "Windows protected your PC", click **More info → Run anyway**
   (the app isn't code-signed).
3. A page opens in your browser. Click **Start CS2** and confirm the launch options in Steam.
4. Open a match to watch in CS2. The camera now follows the action by itself.

The app finds CS2 and installs everything itself. Settings and a log file are kept in
`%APPDATA%\CSTV Observer`. The app uses the normal (heuristic) mode; lookahead is only available
from the command line.

**Making a new release:** push a tag such as `v0.2.0`, or run "Build Windows app" from the
Actions tab. GitHub builds the `.exe` on Windows, tests it, and attaches it to the release.
Normal pushes don't trigger a build.

## Results

Benchmarked by replaying a real CS2 match demo (de_mirage, 9 rounds, 65 kills) through the exact same code
used live (`python -m observer bench your.dem`). A kill counts as *seen* when the camera was on the killer
or the victim for the 0.75 s before it happened.

| director | kills seen | from killer's POV | on it ≥2 s early | cuts/min |
|---|---|---|---|---|
| random player every 6 s | 29% | 20% | 28% | 8.4 |
| reactive: cut to whoever got a kill (typical simple auto-observer) | 40% | 18% | 37% | 3.4 |
| nearest enemy, re-checked every 2 s | 62% | 35% | 52% | 7.0 |
| **cstv-observer heuristic** | **78%** | 51% | 52% | 9.7 |
| **cstv-observer lookahead** | **91%** | 77% | 77% | 8.9 |

Most of the remaining 9% in lookahead mode are kills less than 0.5 s apart in different parts of the map,
which one camera can't show both of. One demo is a small sample. Run `bench` on your own demos for numbers
that fit your matches, and `tune` to fit the weights to them.

## How it decides

**1. Features per player** (`tracker.py`), taken from consecutive GSI snapshots: velocity, shots fired (the
magazine count goes down), damage taken, kills (the `round_kills` count goes up), and who killed whom.
The killer is worked out from distance and aim, because GSI doesn't report it.

**2. Action score** (`scoring.py`). For every enemy pair in weapon range:
- proximity × aim. Is P looking at E (within a few degrees, with the cone widening at close range)? Is E
  looking at P? Both at once is a duel and scores highest.
- time to contact, from closing speed: two players walking into each other score before they meet.
- "dealing damage": P aims at an enemy who is losing HP, so P is probably the one shooting.
- duel advantage: health, who has the angle, sniper, flashed. The likely winner is the better POV because
  the action continues after the kill.
- a second and third enemy nearby add more (multi-kill potential).

On top of that it adds activity (shooting, taking damage, kill momentum) and situation (clutch, planting,
defusing, retake, kills this round), and lowers the score for flashed players and players holding utility.

**3. Camera policy** (`director.py`): hysteresis so the camera isn't jumpy.
- When the player on camera dies, it holds about 1 s on the death, then cuts to the killer if he is still in
  action, otherwise to the best player.
- Minimum shot length is about 2.3 s, shortened when something urgent happens and the current view is quiet.
- A cut needs the challenger to beat the current player by a relative and an absolute margin.
- After about 3 s of a quiet view, it switches with a smaller margin.

**4. Lookahead planner** (`lookahead.py`). With future kills known, choosing which kills to show is a
weighted interval-scheduling problem, solved exactly with dynamic programming on every replan. For each kill
the planner picks the killer's POV, the victim's POV, or skip. It aims for 2.5 s of lead before the kill and
a 0.9 s tail after it, and never makes a shot shorter than 1.3 s. A player who gets a kill and is then
traded is one continuous shot covering both kills. Between planned windows the heuristic director runs,
boosted by "this player takes or deals damage in the next 4 s" hints.

**Clock sync.** Server game time is mapped to your local broadcast time using events both sides see (end of
freeze time, bomb plant). If the server's `tv_delay` is used as a first guess, broadcast buffering doesn't
break it. In the end-to-end test the reported delay was 3 s off and the sync still landed within one frame.

## Setup (heuristic mode)

Needs Python 3.11 or newer. The live observer has no dependencies.

1. Install the GSI config into CS2:
   ```
   python -m observer gsi-cfg > "<Steam>/steamapps/common/Counter-Strike Global Offensive/game/csgo/cfg/gamestate_integration_cstv_observer.cfg"
   ```
2. Add `-netconport 2121` to CS2's launch options. Several existing observer tools report that CS2 only
   accepts netcon commands with `-insecure` as well, which disables VAC for that session. Use a dedicated
   observer PC or account for that.
3. Start CS2, connect to the CSTV / GOTV relay (or play a demo), then run:
   ```
   python -m observer run                      # or: --config observer.toml
   python -m observer run --dry-run            # logs cuts without touching CS2
   ```
4. Open http://127.0.0.1:3500 for a live dashboard showing scores, the reason for each cut, kills
   seen, and a pause/resume button. If you change the camera by hand, the director backs off for 8 s.

Cuts are sent as `spec_player_by_accountid <id>`. Every cut is checked against the player GSI says is on
screen. If two cuts in a row don't take effect, it switches to the fallback `spec_player "<name>"`. Both
command templates can be changed in `observer.example.toml` in case a CS2 update breaks one.

## Setup (lookahead mode)

1. Build the plugin and install it on the game server
   ([CounterStrikeSharp](https://github.com/roflmuffin/CounterStrikeSharp) is required):
   ```
   cd plugin/CstvObserverFeed && dotnet build -c Release
   ```
   Copy `bin/Release/net8.0/CstvObserverFeed.dll` to `addons/counterstrikesharp/plugins/CstvObserverFeed/`.
   Then edit `addons/counterstrikesharp/configs/plugins/CstvObserverFeed/CstvObserverFeed.json`:
   ```json
   { "Url": "http://OBSERVER_PC_IP:3500/feed", "Token": "a-long-random-secret", "SendHurt": true, "FlushInterval": 0.25 }
   ```
2. In `observer.toml`, set `listen_host = "0.0.0.0"`, `lookahead = true` and the same `feed_token`. Then
   run `python -m observer run --config observer.toml`. The dashboard shows when the clock is synced and
   the upcoming planned cuts.

> **Integrity:** the feed is live match information. Send it only to the observer machine, keep that
> machine away from teams, and restrict port 3500 to the game server's IP.

## Tune it on your own demos

```
pip install -r requirements.txt
python -m observer bench demos/*.dem                          # compare against the baselines
python -m observer tune  demos/*.dem --iters 300 --holdout 2 --out weights.json
python -m observer run --weights weights.json
```
The tuner maximizes kills seen, with bonuses for arriving early and for the killer's POV and penalties for
a jumpy camera. Use 10 or more demos and keep some as a holdout. With one demo it will overfit.

## Project layout

```
observer/
  gsi.py         GSI payload -> Snapshot, GSI cfg generator
  tracker.py     temporal features, kill / killer inference
  scoring.py     action-potential score (all weights in `Weights`)
  director.py    camera policy (hysteresis, death handling, idle)
  lookahead.py   clock sync, future-event feed, DP planner, lookahead director
  controller.py  netcon client with cut verification + fallback command
  server.py      HTTP: /gsi, /feed, /status, dashboard
  bench.py       demo -> snapshot replay, metrics, baseline directors
  tune.py        evolution-strategy weight tuner
plugin/CstvObserverFeed/   CounterStrikeSharp plugin (C#)
tests/                     unit tests + an end-to-end replay of a real demo through the live engine
```

## Not yet verified

Everything above is tested against real CS2 demo data, synthetic GSI, and an end-to-end replay through the
live engine with a fake 95 s broadcast delay. No CS2 client or game server was available, so these still
need a first real run:

- that `spec_player_by_accountid` moves the camera on the current CS2 build (the fallback covers the case
  where it doesn't), and whether `-insecure` is needed
- the C# plugin: every API it uses was checked against the CounterStrikeSharp source, but it hasn't been
  compiled

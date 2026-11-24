# cstv-observer

Auto-director for CS2 CSTV/GOTV. It reads Game State Integration from a spectating CS2 client, predicts
which player is about to be in a kill, and moves the camera there through CS2's network console.

It's a CS2 port of the GOTV observer I used before.

Two modes:

- **heuristic** (default): needs only the spectating client. Predicts fights from positions, aim,
  shooting and damage.
- **lookahead**: needs a plugin on the game server. CSTV is delayed by `tv_delay`, so the server knows
  every kill before viewers see it and the camera is planned around them.

## Windows app

1. Download `CSTV-Observer.exe` from Releases and run it (it isn't signed: **More info → Run anyway**).
2. In the page that opens, click **Start CS2**.
3. Open a match or demo to watch.

It finds CS2, installs the GSI config and keeps settings in `%APPDATA%\CSTV Observer`. Heuristic mode
only.

### Required launch options

The executable only works if CS2 is started with both:

```
-netconport 2121 -insecure
```

- `-netconport 2121` opens the console port the app uses to move the camera.
- `-insecure` is needed too: without it CS2 ignores camera commands from other programs. It disables VAC
  for that session, so only watch with it, don't play.

**Start CS2** tries to add them. If the camera-control step stays red, close CS2, then in Steam go to
**Counter-Strike 2 → Properties → General → Launch Options**, paste the line above and start CS2 again.
Launch options only apply when CS2 starts. Remove them to play normally.

To publish a release: bump `__version__` in `observer/__init__.py`, then **Actions → Build Windows app → Run workflow** (leave the version empty).

## From source

Python 3.11+, no dependencies.

```
python -m observer gsi-cfg > "<Steam>/steamapps/common/Counter-Strike Global Offensive/game/csgo/cfg/gamestate_integration_cstv_observer.cfg"
python -m observer run              # --config observer.toml, --dry-run to only log cuts
```

Launch CS2 with `-netconport 2121 -insecure` (both required, see
[Required launch options](#required-launch-options)). `-insecure` disables VAC for that session, so use a
dedicated observer account. The dashboard is at http://127.0.0.1:3500. Changing the camera by
hand pauses the director for 8 s.

Settings are in `observer.example.toml` (copy to `observer.toml`), including the console command used to
cut. If `spec_player_by_accountid` stops working, it falls back to `spec_player "<name>"`.

## Lookahead mode

Requires [CounterStrikeSharp](https://github.com/roflmuffin/CounterStrikeSharp) on the server.

```
cd plugin/CstvObserverFeed && dotnet build -c Release
```

Copy `bin/Release/net8.0/CstvObserverFeed.dll` to `addons/counterstrikesharp/plugins/CstvObserverFeed/`
and set `addons/counterstrikesharp/configs/plugins/CstvObserverFeed/CstvObserverFeed.json`:

```json
{ "Url": "http://OBSERVER_PC_IP:3500/feed", "Token": "a-long-random-secret", "SendHurt": true, "FlushInterval": 0.25 }
```

In `observer.toml` set `listen_host = "0.0.0.0"`, `lookahead = true` and the same `feed_token`.

The feed is live match information: only allow the game server's IP on port 3500 and keep the observer
PC away from the teams.

## Results

Two pro demos (345 kills). A kill counts as seen if the camera was on the killer or victim for the 0.75 s
before it.

| director | kills seen | cuts/min |
|---|---|---|
| random player every 6 s | 33% | 8.7 |
| cut to whoever got a kill | 36% | 2.4 |
| nearest enemy | 59% | 7.0 |
| **heuristic** | **63%** | 6.1 |
| **lookahead** | **90%** | 6.0 |

## Benchmark and tune

```
pip install -r requirements.txt
python -m observer bench demos/*.dem
python -m observer tune demos/*.dem --iters 300 --holdout 2 --out weights.json
python -m observer run --weights weights.json
```

Tune on 10+ demos and keep some as holdout.

## Layout

```
observer/
  model.py       shared data types, weapon ranges
  gsi.py         GSI parsing, cfg generator
  tracker.py     per-player features, kill detection
  scoring.py     action score and all weights
  director.py    camera rules
  lookahead.py   clock sync, kill planner
  controller.py  netcon client
  server.py      HTTP server and dashboard
  app.py, ui.py  Windows app
  bench.py       demo replay and metrics
  tune.py        weight tuner
plugin/          CounterStrikeSharp plugin (C#)
tests/
```

## Untested

- The C# plugin has not been compiled or run on a server.
- Whether `spec_player_by_accountid` moves the camera on current CS2.

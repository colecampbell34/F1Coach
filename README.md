# F1Coach

F1Coach is a small Python app that listens to live UDP telemetry from F1 24 and turns lap data into coaching feedback. The first version targets F1 24's `2024` UDP format and is structured so other Codemasters F1 formats or racing games can be added as adapters later.

## Current Scope

- Listens for UDP telemetry on port `20777`.
- Decodes F1 24 `2024` packet headers plus session, lap, car telemetry, car status, motion, and motion-ex packets.
- Runs a local review dashboard focused on completed laps, delta maps, input traces, priority fixes, and packet status.
- Adds a separate Race Overview dashboard for full-race tracking, stint phase review, sector trends, race risks, and a power ranking out of 10.
- Plots a 2D track map from live motion packet world positions.
- Highlights areas of the track where the last lap lost time.
- Calculates a simple theoretical best from your three best clean sectors.
- Supports imported reference lap JSON when you can get telemetry from another driver or tool.
- Prints completed-lap summaries with sector deltas and likely time-loss areas.

## PS4 F1 24 Setup

In F1 24:

1. Open `Settings -> Telemetry Settings`.
2. Set `UDP Telemetry` to `ON`.
3. Set `UDP Broadcast Mode` to `ON`.
4. Set `UDP Port` to `20777`.
5. Set `UDP Send Rate` to `60Hz`.
6. Set `UDP Format` to `2024`.

Your console and laptop must be on the same Wi-Fi/router.

## Easiest Download And Run

1. Install Python 3.10 or newer from https://www.python.org/downloads/.
2. Download this project from GitHub as a ZIP and unzip it.
3. Start F1Coach from the unzipped folder:
   - macOS: double-click `run_f1coach.command`.
   - Windows: double-click `run_f1coach.bat`.
   - Linux: run `./run_f1coach.sh`.
4. The dashboard opens at `http://127.0.0.1:8765`.

Keep the launcher window open while using the dashboard. Close it with `Ctrl+C` when you are done.

If macOS blocks the launcher, right-click `run_f1coach.command`, choose `Open`, then approve it. If Windows asks about firewall access for Python, allow it on private networks so the game packets can reach the app.

## Run From Terminal

Use Python 3.10 or newer.

```bash
python3 -m f1coach dashboard --port 20777 --open-browser
```

Open:

```text
http://127.0.0.1:8765
```

Or install it in editable mode:

```bash
python3 -m pip install -e .
f1coach dashboard --port 20777 --open-browser
```

When you start driving, the app prints packet status, lap completions, and coaching messages. The dashboard is designed for after-lap review rather than live driving inputs.

## Deployment Model

This app cannot run as a telemetry receiver on Vercel by itself. F1 games send UDP broadcast packets on your local network, and Vercel runs in the cloud, outside the user's router/LAN. A browser tab opened from Vercel also cannot bind to UDP port `20777`.

The practical options are:

- Run F1Coach locally on the same Wi-Fi/router as the console or PC running the game.
- Later, split the project into a local telemetry bridge plus a hosted dashboard. The local bridge would receive UDP and stream normalized telemetry to the web app.

## Review Dashboard Controls

- `Pause Capture` stops recording/analyzing incoming UDP packets without clearing the current session. Existing laps, the track map, the improvement target, and analysis stay visible.
- `Resume Capture` continues recording new packets into the same session.
- `Lap Review` / `Race Overview` switches between single-lap coaching and the full-race review area. Race Overview keeps the race log, phase breakdown, execution risks, sector trends, next-run plan, and F1-style power ranking separate from the map/trace workspace.
- The power ranking uses only representative green-flag racing laps. Lap 1, pit lane laps, safety car/VSC laps, non-green-flag laps, invalid laps, missing-telemetry laps, and obvious non-representative pace outliers stay visible in the race log but do not distort the score.
- `Qualifying` / `Race Pace` changes the coaching goal. Qualifying advice is more aggressive about peak lap time and ERS spend; race-pace advice favors repeatable braking, tyre life, and battery use for attack/defense.
- `New Session` clears the current session when you switch tracks or want a fresh reference.

F1Coach also resets automatically when a session packet reports a different track ID or track length.

The coach dynamically detects corner entry, mid-corner, exit, and straight zones from the lap/reference telemetry instead of relying on a hard-coded corner list. Loss reports can call out patterns such as downhill braking, steering overlap, low-grip exits, high-speed scrub, brake-bias direction, throttle shape, and ERS deployment priorities at any point on any track.

If FastF1 is installed, the dashboard runtime also tries to load FastF1 circuit markers in the background and use official turn labels such as `T5 entry` or `T8 exit` when they line up with the live lap distance. The driving diagnosis still comes from your telemetry, so missing or unavailable FastF1 data simply falls back to dynamic `Corner 5 entry` labels.

## Terminal-Only Mode

```bash
python3 -m f1coach listen --port 20777 --show-packets
```

## Reference Lap Strategy

F1Coach uses the best available reference in this order:

1. Your current-session theoretical best: best sector 1 + best sector 2 + best sector 3 from clean laps.
2. Imported reference lap JSON passed with `--reference`.
3. Your personal best lap from the current session.

The dashboard calculates theoretical best with a simple sector sum from your clean completed laps. Because this target has no speed, input, ERS, or position trace, corner-level tips still compare your selected lap against your personal-best trace or an imported reference.

External laps are useful, but assists matter. A no-assist wheel lap is often a poor direct target for a controller lap with medium traction control. If you import another driver's reference, include an `assistProfile` in the JSON so the dashboard can make the source clear.

Potential reference sources:

- EA Racenet Time Trial Analysis can compare your telemetry with friends or leaderboard positions.
- F1Laps publishes F1 24 leaderboards and marks laps that include telemetry data.
- F1Laps companion apps can sync your own F1 20 through F1 25 laps, which is a likely path for exporting or manually recreating reference data later.

Example reference file shape:

```json
{
  "name": "Imported Austria TT Reference",
  "source": "manual-export",
  "lapTimeMs": 64123,
  "sector1Ms": 15800,
  "sector2Ms": 29600,
  "assistProfile": {
    "input": "controller",
    "tractionControl": "medium",
    "abs": "on",
    "racingLine": "corners"
  },
  "samples": [
    {
      "lapTimeMs": 1234,
      "lapDistanceM": 84.2,
      "normalizedDistance": 0.02,
      "speedKmh": 281,
      "throttle": 1.0,
      "brake": 0.0,
      "steer": 0.03,
      "gear": 8,
      "engineRpm": 11800,
      "worldPosition": [120.0, 3.5, -42.0]
    }
  ]
}
```

Run with:

```bash
python3 -m f1coach dashboard --reference path/to/reference.json
```

## Troubleshooting

- If no packets arrive, confirm PS4 and laptop are on the same network.
- Keep `UDP Broadcast Mode` enabled for the simplest setup.
- Make sure no other app is already bound to port `20777`.
- On macOS or Windows, allow Python through the firewall if prompted.
- If `python` does not work, use `python3 -m f1coach dashboard --port 20777 --open-browser`.
- If the dashboard has gauges but no track map, confirm F1 24 is sending motion packets and you are on track.
- If the dashboard loads but packet count stays at zero, use `--show-packets` and check firewall permissions.

## Design Notes

The code is split into three layers:

- `f1coach.adapters`: converts game-specific binary packets into normalized events.
- `f1coach.coach`: turns normalized lap samples into coaching feedback.
- `f1coach.cli`: handles UDP socket IO and terminal output.

That separation is deliberate. Future support for F1 23, F1 25, Assetto Corsa Competizione, iRacing, or Gran Turismo should live behind new adapters instead of leaking game-specific binary layouts into the coaching engine.

## Tests

```bash
python -m unittest
```

## Reference

Packet layouts are based on EA's F1 24 UDP specification:

https://forums.ea.com/discussions/f1-24-general-discussion-en/f1-24-udp-specification/8369125

# F1Coach

F1Coach is a small Python app that listens to live UDP telemetry from F1 24 and turns lap data into coaching feedback. The first version targets F1 24's `2024` UDP format and is structured so other Codemasters F1 formats or racing games can be added as adapters later.

## Current Scope

- Listens for UDP telemetry on port `20777`.
- Decodes F1 24 `2024` packet headers plus session, lap, car telemetry, car status, motion, and motion-ex packets.
- Runs a local dashboard with live speed, gear, throttle, brake, ERS, lap delta, and packet status.
- Plots a 2D track map from live motion packet world positions.
- Highlights areas of the track where the last lap lost time.
- Builds an assist-matched `Ideal lap` reference from your own best clean micro-sectors.
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

When you start driving, the app prints packet status, lap completions, and coaching messages. The dashboard updates every half second.

## Deployment Model

This app cannot run as a telemetry receiver on Vercel by itself. F1 games send UDP broadcast packets on your local network, and Vercel runs in the cloud, outside the user's router/LAN. A browser tab opened from Vercel also cannot bind to UDP port `20777`.

The practical options are:

- Run F1Coach locally on the same Wi-Fi/router as the console or PC running the game.
- Later, split the project into a local telemetry bridge plus a hosted dashboard. The local bridge would receive UDP and stream normalized telemetry to the web app.

## Dashboard Controls

- `Pause` stops recording/analyzing incoming UDP packets without clearing the current session. Existing laps, the track map, the ideal lap, and analysis stay visible.
- `Resume` continues recording new packets into the same session.
- `New Session` clears the current session when you switch tracks or want a fresh reference.

F1Coach also resets automatically when a session packet reports a different track ID or track length.

## Terminal-Only Mode

```bash
python3 -m f1coach listen --port 20777 --show-packets
```

## Reference Lap Strategy

F1Coach uses the best available reference in this order:

1. Imported reference lap JSON passed with `--reference`.
2. Generated `Ideal lap` from your own clean micro-sectors.
3. Your personal best lap from the current session.

The generated `Ideal lap` is the default target because it can be faster than your best single lap while still matching your exact assists, input device, setup habits, and driving level. It follows the same theoretical-best method used by F1 games: split the lap into timing loops, take the fastest clean loop from any lap in the session, then add those loops together.

The public F1 24 UDP spec exposes lap history and best sector lap numbers, but not the game's private mini-sector loop boundaries or its already-calculated theoretical-best value. F1Coach therefore uses the same algorithm on its own fixed telemetry loops and preserves the source lap for each loop so analysis can explain which previous lap set the reference.

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

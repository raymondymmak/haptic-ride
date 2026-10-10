# Haptic Ride

Navigate by feel. A starter for the INTERFACE 24h hackathon: haptic rings that tell a cyclist when to turn and when a car is closing from behind, plus a Mario Kart-style score for actually looking.

The Infineon 60 GHz radar kit and the Genki Wave / DataFeel rings are not required to demo. One laptop runs a bridge. Every other machine is just a WebSocket client. Until the hardware arrives, the bridge is fake and plays a one-minute route on a loop.

## Run

```bash
./run.sh
```

Open [http://127.0.0.1:8641](http://127.0.0.1:8641).

| Key | Action |
| --- | --- |
| `A` / `D` | Shoulder check left / right |
| `←` / `→` | Turn left / right |
| `M` | Tick sounds for the screen recording |

Shoulder-check the ring that says CHECK. Turn when the arrow says NOW. The loop is 56 seconds and then repeats.

Two phones on the same Wi-Fi can stand in for the rings. Android Chrome will vibrate; iPhone Safari does not implement the vibration API, so the phone still shows the pattern.

```text
http://<laptop-ip>:8641/?ring=left
http://<laptop-ip>:8641/?ring=right
```

`./run.sh` prints the LAN address. `WS_PORT` and `HTTP_PORT` change the ports (defaults `8765` and `8641`). It makes a virtualenv when Python allows it, and otherwise installs the dependency into `.vendor/`.

Without the script, from the repo root:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python bridge/fake_bridge.py
python engine/game.py
```

Headless check (scoring rules, replay, live sim, record, hardware stub, radar filter):

```bash
python tests/test_e2e.py
python tests/test_radar_filter.py
```

## Architecture

```text
 overlay (browser)  ←——ws——→  bridge  ←——ws——→  game engine
 rings, radar, keys          the bus            score + which buzz
                             fake now,
                             radar + rings later
```

The bridge is a dumb hub. It owns the hardware, rebroadcasts every JSON message to the other clients, and prints buzz commands. The game engine is the only place that scores or picks a pattern. The overlay draws and, on a phone, vibrates. Run **one** game engine on the LAN or two of them will both buzz.

```text
car, nav                  bridge  →  game, overlay
shoulder_check, turn      overlay →  game          (later: ring IMU)
buzz                      game    →  bridge, overlay
state                     game    →  overlay
hello                     bridge  →  whoever just connected
```

## Protocol

One JSON object per WebSocket text frame. Fields the engine adds (`id`, and the `state` / `turn` / `hello` messages) are how a car stays a car and how the HUD gets a score. Stick to these names.

**Sensors** (bridge, or the overlay for rider input):

```json
{"type": "car", "side": "left", "dist": 14.2, "speed": 4.0, "id": "c0-1"}
{"type": "nav", "turn": "right", "in_m": 18.0, "id": "n0-1"}
{"type": "shoulder_check", "side": "left"}
{"type": "turn", "turn": "right"}
```

`speed` is closing speed in m/s, positive when the car is approaching. `dist` and `in_m` are meters. `id` lets two cars exist at once; without it the engine treats the side as the id.

**Buzz** (game → rings). `intensity` is 0 to 1 and rises as distance falls.

```json
{"type": "buzz", "side": "right", "pattern": "car_approach", "intensity": 0.64}
```

`side` is `left`, `right`, or `both`. `pattern` is one of:

| Pattern | When |
| --- | --- |
| `turn_soon` | Next turn is inside 45 m |
| `turn_now` | Next turn is inside 12 m |
| `car_approach` | Nearest car on that side, closing under 8 m/s |
| `blue_shell` | Nearest car on that side, closing at 8 m/s or more |
| `combo` | A scored item, dodge, or drift. Usually `both` |

A car on a ring wins over a turn cue on the same ring.

**State** (game → overlay), about 10 Hz:

```json
{
  "type": "state",
  "score": 250,
  "combo": 2,
  "flash": {"text": "ITEM BOX", "pts": 150, "kind": "item", "n": 4},
  "nav": {"id": "n0-0", "turn": "left", "in_m": 11.2, "phase": "turn_now"},
  "cars": [{"id": "c0-1", "side": "right", "dist": 14.2, "speed": 3.2, "blue": false}],
  "rings": {
    "left": {"pattern": "turn_now", "intensity": 0.86},
    "right": {"pattern": "car_approach", "intensity": 0.54}
  },
  "check": ["right"],
  "need_turn": true,
  "coach": "CAR ON THE RIGHT — shoulder check for an item box",
  "stats": {"items": 1, "dodges": 0, "drifts": 0, "misses": 0}
}
```

`phase` is `far`, `turn_soon`, or `turn_now`.

## Scoring

Pay multiplier is `1 + 0.5 × combo`. The points land, then combo increases by one. A miss (warned car passes with no check, or a TURN NOW leg ends with no turn) sets combo back to 0.

| Action | Points |
| --- | --- |
| Item box — shoulder check on a side that currently has a car inside 25 m | 100 × pay |
| Blue shell dodge — that car is closing at ≥ 8 m/s | 250 × pay |
| Drift boost — turn input matches the cue while it is NOW | 150 × pay |

Wrong-way during NOW drops the combo. Turning before NOW does not. The constants sit at the top of `engine/game.py`.

## Record and replay

Sensor messages only (`car`, `nav`), one JSON object per line:

```json
{"t": 1.40, "msg": {"type": "car", "side": "left", "dist": 12.0, "speed": 4.0, "id": "demo-car"}}
```

`t` is seconds from the start of that recording. Replay loops the file.

```bash
python bridge/fake_bridge.py --record logs/session.jsonl
python bridge/fake_bridge.py --replay examples/short.jsonl
```

Buzz commands are printed by the bridge (`buzz  left  car_approach  0.62`), not written into the sensor log.

## Team workflow

Until the kit is on a bike, everyone uses the fake bridge.

1. Demo laptop runs `./run.sh` (bridge + the one game engine + the overlay).
2. Other laptops and phones open `http://<demo-laptop>:8641`. They do not run a second engine.
3. Someone hacking the engine runs **only** `python engine/game.py --url ws://<bridge>:8765` and stops the engine on the bridge machine first.
4. When the kit is on the Windows VM, that VM runs the real bridge (below) and the Mac runs the one game engine. Do not also run `./run.sh` or a second engine.

There is no auth. Keep it on the hackathon LAN.

## Radar on the Windows VM

The distance example (`distance_fft.py`) reports the strongest range bin. With nothing inside 6 m that bin is often a static reflection around 4–5 m, with an occasional 6 m spike. `read_radar()` keeps the working USB-passthrough setup (0.15 m resolution, 9.6 m max range, max speed about 3 m/s, 60.75 GHz, 5 Hz, one RX antenna, IF gain 33) and then:

1. Averages 20 empty-scene range-FFT frames at first launch and subtracts that profile. Later launches reuse `logs/radar_background.json`. `--recalibrate` measures it again — point the radar at an empty scene when you do.
2. Ignores bins under 0.3 m, and ignores a peak that is not above `k` times the median of the remaining bins (`--threshold-k`, default 6).
3. Emits a car only after the same target shows up in 3 consecutive frames. Distance is the median of the last 5 peaks. Speed is the closing rate of that trend, positive when the target is approaching.

This kit does not resolve left and right. `--side` chooses the ring (default `right`).

The VM needs Python 3.14, `ifxradarsdk`, and the SDK examples folder that contains `helpers\DistanceAlgo.py` (it sits next to `distance_fft.py` and is not installed as a package). Allow inbound TCP 8765 and 8641 through the VM firewall. USB-passthrough the DEMO-BGT60TR13C.

On the VM, from that examples folder:

```bat
set PYTHONPATH=%CD%
py -3.14 C:\path\to\haptic-ride\bridge\real_bridge_stub.py --host 0.0.0.0 --port 8765 --http-port 8641 --if-gain 33
```

The first 20 frames (about 4 seconds) are the empty scene unless a background file is already there. The console prints `radar  right   4.72 m  +0.80 m/s` when a target is actually present, and `radar  below threshold` or `radar  wait 2/3` when it is not.

On the Mac, use the VM's IP (`ipconfig` on the VM):

```bash
python3 engine/game.py --url ws://VM_IP:8765
```

Open `http://VM_IP:8641` in the Mac browser. That page's websocket goes back to the VM, which is the only bridge. One game engine, on the Mac.

To try the filter with no kit, on either machine:

```bash
python bridge/real_bridge_stub.py --synthetic
```

IF gain 33–45 is the range that worked. Raising max range, resolution, speed, or the RX count makes a bigger frame and the kit returns `IFX_ERROR_FRAME_ACQUISITION_FAILED` over USB passthrough.

`play_buzz()` is still the open hook for the Genki Wave rings or a DataFeel devkit. The hub already validates and prints every buzz.

## Demo loop

`bridge/fake_bridge.py` repeats this every 56 seconds. Turns and cars are on opposite sides so both rings are readable in a picture-in-picture crop.

| Time | What to show |
| --- | --- |
| 0s | Left ring eases into the turn. Radar is clear. |
| 1s | A car closes on the right. Press `D`. Item box. |
| 4–6s | Two cars behind you on the right. |
| 10s | Left cue snaps to NOW. Press `←`. Drift boost. |
| 16s | Fast car on the left, blue-shell pattern. Press `A` before it arrives. |
| 23s | Right ring is SOON, a slower car is still on the left. |
| 31s | Right NOW. Press `→`. |
| 37s | Second blue shell, on the right. |
| 44s | Right car under the final left-turn cue. |
| 52s | Left NOW again. |
| 56s | Loop. |

Crop the browser over the bike shot. The edge of the screen glows on the buzzing side, the score is the loudest type on the page, and the bus ticker at the bottom right shows the JSON the judges cannot see in the rings.

## Layout

```text
bridge/fake_bridge.py        sim, record, replay, overlay HTTP
bridge/real_bridge_stub.py   BGT60TR13C distance FFT, ring TODO
bridge/radar_filter.py      background, threshold, persistence
bridge/hub.py                WebSocket fan-out
engine/game.py               haptics + score
overlay/index.html           the HUD, one file
examples/short.jsonl         tiny replay
run.sh                       venv, bridge, engine
tests/test_e2e.py            headless end-to-end check
```

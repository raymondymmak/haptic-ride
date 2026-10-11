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
| `A` / `D` | Shoulder check left / right. Either one also counts for a car directly behind |
| `S` or `W` | Look back. Scores a car in the behind zone |
| `←` / `→` | Turn left / right |
| `M` | Tick sounds for the screen recording |

The overlay header has a **Simulated / Real** control. On the hardware bridge it swaps the car source without restarting the process. Turn cues keep running in both.

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

Headless check (scoring rules, replay, live sim, record, hardware stub, radar filter, DataFeel dry-run, camera boxes):

```bash
python tests/test_e2e.py
python tests/test_radar_filter.py
python tests/test_datafeel_rings.py
python tests/test_camera_bridge.py
```

## Architecture

```text
 overlay (browser)  ←——ws——→  bridge  ←——ws——→  game engine
 phone rings                 the bus            score + which buzz
 DataFeel ring client        fake, radar, or camera
```

The bridge is a dumb hub. It rebroadcasts every JSON message and prints buzz commands. The game engine is the only place that scores or picks a pattern. The DataFeel ring client is the only place that drives the dots. The overlay draws and, on a phone, vibrates. Run **one** game engine on the LAN or two of them will both buzz.

```text
car, nav                  bridge  →  game, overlay, ring client
shoulder_check, turn      overlay or Wave → game
dismiss                   Wave → game, ring client
buzz                      game    →  bridge, overlay, ring client
state                     game    →  overlay
hello                     bridge  →  whoever just connected
mode                      overlay →  bridge → everyone
```

## Protocol

One JSON object per WebSocket text frame. Fields the engine adds (`id`, and the `state` / `turn` / `hello` messages) are how a car stays a car and how the HUD gets a score. Stick to these names.

**Sensors** (bridge, or the overlay for rider input):

```json
{"type": "car", "side": "left", "dist": 14.2, "speed": 4.0, "id": "c0-1"}
{"type": "car", "side": "behind", "dist": 8.0, "speed": 3.0, "id": "cam-behind"}
{"type": "nav", "turn": "right", "in_m": 18.0, "id": "n0-1"}
{"type": "shoulder_check", "side": "left"}
{"type": "look_back"}
{"type": "turn", "turn": "right", "side": "right"}
{"type": "dismiss"}
{"type": "dismiss", "side": "left"}
{"type": "mode", "value": "sim"}
```

`speed` is closing speed in m/s, positive when the car is approaching. `dist` and `in_m` are meters. `id` lets two cars exist at once; without it the engine treats the side as the id. `side` on a car is `left`, `right`, or `behind` (`center` is accepted as `behind`). A shoulder check on either side scores a behind car. `look_back` scores only that car.

`turn` takes `turn` or `side` (`left` or `right`). The overlay sends `turn`. The Wave sends both. `dismiss` is a wrist flick: it scores like a shoulder check and silences that car warning so the buzz stops. With `side`, it answers that wrist and a car behind. Without `side`, it answers every car that is currently warning and scores the nearest (a blue shell first).

`mode` asks the bridge to change car source. `value` is `sim` or `real`. The bridge answers with the same message, to every client, using the value it actually applied. `hello` also carries `source` (`sim` or `real`) plus `mode` (`fake`, `real`, or `synthetic`), which is which process you joined.

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
| `car_behind` | Car directly behind. One buzz with `side: "both"`, both wrists on the same pulse. Intensity rises as it gets closer |
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
| Item box — shoulder check on a side that currently has a car inside 25 m, or a look-back (`S` / `W`) on a car directly behind. A left or right check also scores the behind car | 100 × pay |
| Blue shell dodge — that car is closing at ≥ 8 m/s | 250 × pay |
| Drift boost — turn input matches the cue while it is NOW | 150 × pay |
| Dismiss — Wave flick, middle-button click, or `{"type":"dismiss"}`. Same pay as the matching check, and the car buzz stops | 100 × pay, or 250 × pay for a blue shell |

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
4. When the kit is on the Windows VM, that VM runs the real bridge (below) and the Mac runs the one game engine. Do not also run `./run.sh` or a second engine. The Mac with the El Jefe modules also runs `rings/datafeel_rings.py` against the VM's websocket. The bridge only logs buzzes.

There is no auth. Keep it on the hackathon LAN.

## Radar on the Windows VM

The distance example (`distance_fft.py`) reports the strongest range bin. With nothing inside 6 m that bin is often a static reflection around 4–5 m, with an occasional 6 m spike. `read_radar()` keeps the working USB-passthrough setup (0.15 m resolution, 9.6 m max range, max speed about 3 m/s, 60.75 GHz, 5 Hz, one RX antenna, IF gain 33) and then:

1. Averages 20 empty-scene range-FFT frames at first launch and subtracts that profile. Later launches reuse `logs/radar_background.json`. `--recalibrate` measures it again — point the radar at an empty scene when you do.
2. Ignores bins under 0.3 m, and ignores a peak that is not above `k` times the median of the remaining bins (`--threshold-k`, default 6).
3. Follows every peak that clears the threshold. A car is published only when that peak has filled the smoothing window (default 5 frames), the distance is net-decreasing across the window, and the closing speed is at least `--min-approach` (default 0.4 m/s). A roof return that sits still, or jitters between about −0.1 and +0.1 m/s, stays silent. So does anything moving away.
4. When more than one peak is approaching, the nearest one is the car. Two static peaks no longer take turns (the live failure was 1.74 m, then 4.9 m, then 1.74 m again).
5. Bins that are not an approaching target keep updating the background with an exponential moving average (`--bg-alpha`, default 0.02). A reflection that appears after calibration slowly fades. `--bg-alpha 0` freezes the background at the saved average.

This kit does not resolve left and right. `--side` chooses the ring (default `right`).

The VM needs Python 3.14, `ifxradarsdk`, and the SDK examples folder that contains `helpers\DistanceAlgo.py` (it sits next to `distance_fft.py` and is not installed as a package). Allow inbound TCP 8765 and 8641 through the VM firewall. USB-passthrough the DEMO-BGT60TR13C.

On the VM, from that examples folder:

```bat
set PYTHONPATH=%CD%
py -3.14 C:\path\to\haptic-ride\bridge\real_bridge_stub.py --host 0.0.0.0 --port 8765 --http-port 8641 --if-gain 33
```

The first 20 frames (about 4 seconds) are the empty scene unless a background file is already there. The console prints `radar  right   4.72 m  +0.80 m/s` when a target is closing, `radar  static 1.74 m +0.02 m/s` when a reflection is just sitting there, and `radar  below threshold` or `radar  wait 2/5` while a peak is still unconfirmed.

On the Mac, use the VM's IP (`ipconfig` on the VM):

```bash
python3 engine/game.py --url ws://VM_IP:8765
```

Open `http://VM_IP:8641` in the Mac browser. That page's websocket goes back to the VM, which is the only bridge. One game engine, on the Mac.

The page starts on **Real** (the radar). **Simulated** sends `{"type":"mode","value":"sim"}`. The process stays up: radar frames keep being read, but cars on the bus switch to the scripted route from `fake_bridge.py` (`nav_at` / `cars_at`). **Real** sends `value: "real"` and radar cars publish again. Turn cues from that same route keep streaming in both modes, so the rings still get SOON and NOW while you are on the live radar. The header shows the source that the bridge confirmed (`HARDWARE · REAL` or `HARDWARE · SIMULATED`). The fake bridge has no radar, so it answers every mode request with `sim` and the scripted demo does not stop.

To try the filter with no kit, on either machine:

```bash
python bridge/real_bridge_stub.py --synthetic
```

IF gain 33–45 is the range that worked. Raising max range, resolution, speed, or the RX count makes a bigger frame and the kit returns `IFX_ERROR_FRAME_ACQUISITION_FAILED` over USB passthrough.

`play_buzz()` logs the buzz and does not open a serial port. Output is the ring client in the next section. The hub already validates and prints every buzz.

## Camera bridge (Mac)

Indoors the radar invents targets that jump between about 1 m, 5 m, and 7 m. `bridge/camera_bridge.py` is the fallback. It runs on the demo Mac (Apple Silicon, Python 3.13), opens a webcam or Continuity Camera, and publishes the same `car` events on the same ports (`8765` / `8641`). The engine, the overlay, and the ring client stay as they are. **Real** is the camera. **Simulated** swaps in the scripted cars from `fake_bridge.py`. Turn cues keep running in both.

Stop `./run.sh` first. It uses ports 8765 and 8641.

The detector is ultralytics YOLO (`yolo11n.pt` by default, `yolov8n.pt` if you pass `--model`). The device is MPS when PyTorch reports it, and CPU if MPS is missing or a call fails. Classes are person, bicycle, car, motorcycle, bus, and truck. The first launch downloads the weights.

Side comes from the box center. `--center-width` (default `0.34`) is the behind band, centered in the frame, so the edges sit at 0.33 and 0.67. The camera faces backward, so the picture is mirrored by default (`--mirror` / `--no-mirror`) and image-left is the rider's left. Left of the band is `left`, right of it is `right`, and the band itself is published as `side: "behind"`. The overlay draws that car on the center line directly behind the rider arrow and labels the zone BEHIND. Each of the three sides publishes its nearest qualifying target. If two on the same side are within 0.5 m, the one closing faster wins.

Distance is `cal / (bbox height / frame height)`, in meters. The default `cal` is the distance at which that class would fill the frame vertically (a rough 50° webcam): car 3.2, person 3.6, bicycle 2.4, motorcycle 2.6, bus 6.4, truck 6.0. Measure one object at a known distance and set `cal = distance × height_fraction`. Speed is `(previous distance − distance) / frame time`, positive when the box is growing. A car is published only after 3 consecutive frames in which it is closing at `--min-approach` (0.4 m/s) or already inside `--near` (6 m).

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 bridge/camera_bridge.py --list-cameras
python3 bridge/camera_bridge.py --camera 0 --preview
```

Continuity Camera shows up as one of the indexes from `--list-cameras`. macOS will ask the terminal for camera permission. `q` closes the preview window and stops the bridge.

Two more terminals, same venv, once the bridge is up:

```bash
source .venv/bin/activate
python3 engine/game.py --url ws://127.0.0.1:8765
```

```bash
source .venv/bin/activate
python3 rings/datafeel_rings.py --url ws://127.0.0.1:8765
```

Open [http://127.0.0.1:8641](http://127.0.0.1:8641). Phones use `http://<mac-ip>:8641`.

```bash
python3 bridge/camera_bridge.py --camera 1 --preview
python3 bridge/camera_bridge.py --camera 0 --preview --no-mirror
python3 bridge/camera_bridge.py --camera 0 --preview --center-width 0.34 --cal car=4.0,person=3.5
python3 bridge/camera_bridge.py --synthetic
```

`--synthetic` needs no camera and no model. It drives the tracker with boxes and is what `python3 tests/test_camera_bridge.py` runs.

## DataFeel rings

The dots hang off El Jefe on the 5-pin cable. El Jefe talks to the laptop over USB-C serial (Silicon Labs CP210x). The client uses the same Python entry as `samples/read_write.py`:

```python
from datafeel.device import VibrationMode, discover_devices, LedMode, ThermalMode, VibrationWaveforms
devices = discover_devices(4)
```

Addresses are whatever answers that scan. A kit at the event showed one El Jefe on `/dev/cu.usbserial-210`, nothing at address 1, and a dot at address 2 (the other dot on that chain is the other address that answers, often 4). The client does not assume 1 and 2. On one port the lowest address that answers is LEFT and the next is RIGHT. `--left-addr` and `--right-addr` override that pair. `--swap` exchanges the wrists. Startup prints the discovered map.

A second CP210x port, if one is plugged in, is the other wrist. Port names are sorted (`usbserial-20` before `usbserial-110`, `COM3` before `COM10`) and every dot discovered on that chain plays for that wrist. `discover_devices()` stops after the first port, so the second port is walked with the same address loop. `--port` forces a single chain even if another adapter is present.

Playback calls are the ones in that sample: `registers.set_vibration_mode(VibrationMode.MANUAL)`, `play_frequency(hz, intensity)`, `stop_vibration()`, `set_led(r, g, b)`, `set_led_off()`, `activate_thermal_intensity_control(heat)`, `disable_all_thermal()`. BLE is the C# stack and this client does not use it.

Patterns are asyncio tasks. A newer buzz for a wrist cancels the one already playing there. `both` drives both wrists. When a pattern finishes, and it is still the latest command for that wrist, the dot goes idle: `stop_vibration()` (intensity 0), `set_led_off()`, thermal off.

Every LED is dim. `--led-brightness` (0 to 1, default `0.15`) scales the RGB channels, so the brightest channel is about 38/255. `turn_soon` and `turn_now` use the same amber, `(255, 230, 0)` at that cap. Timing is what separates them. Soon is a steady glow held for about 1.2 s. Now blinks that amber five times at 6 Hz.

| Pattern | Feel |
| --- | --- |
| `turn_soon` | One short pulse. Amber LED holds a steady glow for about 1.2 s, then turns off |
| `turn_now` | Rapid pulses, one per blink. Same amber, five blinks at about 6 Hz, at the `--led-brightness` cap |
| `car_approach` | Continuous vibration. Intensity and frequency (100–250 Hz) rise with `intensity`. Dim red LED, brighter as `intensity` rises |
| `car_behind` | Both dots pulse on the same beat (one task, not left-then-right). Stronger and quicker as `intensity` rises. Dim red LED on both |
| `blue_shell` | Strong rapid rumble, a blue pulse on each tick, plus heat (`ThermalMode.MANUAL`) when `device.py` exposes it |
| `combo` | Quick triple tap. No LED |

On two El Jefe modules, every dot that answered on that chain gets the wrist's pattern. On one chain, LEFT is the lowest address and RIGHT is the next.

### Setup

`requirements.txt` installs `websockets` and `pyserial`. The DataFeel package is not on PyPI.

1. Install the CP210x driver if the port does not appear.
   - macOS: `/dev/cu.SLAB_USBtoUART` or `/dev/cu.usbserial-*` (use the `cu.` node, not `tty.`)
   - Windows: a `COMx` port. Driver: https://www.silabs.com/documents/public/software/CP210x_Windows_Drivers.zip
2. Clone the SDK and install the Python package from the `Python` folder (the one with `setup.py`):

```bash
git clone https://github.com/DataFeel/devkit-samples.git
cd devkit-samples/Python
pip install .
```

Windows: `py -m pip install .` from that folder. macOS: `pip3 install .` if `pip` is not Python 3. That pulls `pyserial` and `minimalmodbus`.

3. From this repo, against a bridge that is already running (`python3` on macOS, `py` or `python` on Windows):

```bash
pip install -r requirements.txt
python3 rings/datafeel_rings.py --url ws://127.0.0.1:8765 --led-brightness 0.15
```

Ports are auto-detected (CP210x VID `10C4` / PID `EA60`, the check inside `discover_devices`, or a `SLAB_USBtoUART` / `usbserial` name). The scan is addresses 1 through `--max-address` (default 4, same as the sample). The process prints each miss (`No device at address 1`) and then the map:

```text
found DataFeel Device on port /dev/cu.usbserial-210
found 2 devices
Dot 2 (Name = DataFeel Dot V6, ...)
  one El Jefe /dev/cu.usbserial-210; discovered addresses 2, 4; lowest is left, next is right
  LEFT   /dev/cu.usbserial-210  dots=2
  RIGHT  /dev/cu.usbserial-210  dots=4
```

```bash
python3 rings/datafeel_rings.py --port /dev/cu.usbserial-210
python3 rings/datafeel_rings.py --left-addr 2 --right-addr 4
python3 rings/datafeel_rings.py --swap
python3 rings/datafeel_rings.py --left-port /dev/cu.usbserial-110 --right-port /dev/cu.usbserial-210
python3 rings/datafeel_rings.py --port COM5
```

Without the dots, print the frames the hardware would get:

```bash
python3 rings/datafeel_rings.py --dry-run --url ws://127.0.0.1:8765
```

Cycle every pattern on the LEFT wrist, then the RIGHT, with no bridge. The first two buzzes are a mapping check (one long amber pulse, then two blue pulses). Combo taps with the LED off:

```bash
python3 rings/datafeel_rings.py --test
python3 rings/datafeel_rings.py --test --dry-run
python3 rings/datafeel_rings.py --test --led-brightness 0.2
```

On the demo laptop the fake bridge and the ring client can share a machine:

```bash
./run.sh
python3 rings/datafeel_rings.py --url ws://127.0.0.1:8765
```

With the radar on the Windows VM, point the client at the VM (`play_buzz()` there only logs):

```bash
python3 rings/datafeel_rings.py --url ws://VM_IP:8765
```

## Genki Wave

The Wave is input only. It does not buzz. `rings/datafeel_rings.py` still plays every cue. The Wave publishes rider gestures on the same bridge websocket.

A turn is about 45 degrees of world yaw over 0.7–3 seconds. The tracker integrates the gyro axis about gravity: the Wave pose quaternion rotates the bias-corrected gyro into the world and the vertical component is yaw; with no pose, that axis is the accelerometer gravity vector. Euler yaw is not the heading. The ring's euler can sit at 0 while this integral moves, which is what `--print` shows as `int`. The cue is published on the sample where that integral crosses the turn angle, while the wrist is still turning. One turn publishes one cue. The hand coming back to straight is not the opposite turn: after a cue, rotation is ignored until the ring has been still for about a second, and only then does the baseline recenter. A later turn is measured from that new baseline.

```json
{"type": "turn", "turn": "left", "side": "left"}
```

The engine scores that the same way as the overlay arrow keys: it has to match the nav cue during TURN NOW.

A flick is a short sharp spike, distinct from that turn: gyro magnitude over about 180 °/s, or linear acceleration over about 1.6 g, lasting under about 0.35 s. The Wave's own `peak` flag counts too. A flick publishes `{"type":"dismiss"}`. The engine scores the buzzing car like a shoulder check and marks it silenced, so it drops off the wrists and does not become a miss. The ring client cancels `car_approach`, `car_behind`, and `blue_shell` immediately. A turn cue that is already playing stays.

One Wave: the sign of the heading change picks left or right. `--invert` swaps them if a turn comes out mirrored. Two Waves (`--left-addr` and `--right-addr`): each flick includes that hand's `side`, so a left-hand flick prefers the left car (and a car behind). Heading still decides the turn direction.

The middle button click is the same dismiss. The bottom button click is `look_back`. The top button is the hardware reset, not a gesture.

```bash
python3 -m pip install -r requirements.txt
python3 rings/wave_input.py --discover
python3 rings/wave_input.py --addr EAED7675-7521-9A23-1011-6090ADC792C9 --print
python3 rings/wave_input.py --addr EAED7675-7521-9A23-1011-6090ADC792C9 --calibrate
python3 rings/wave_input.py --addr EAED7675-7521-9A23-1011-6090ADC792C9 --url ws://127.0.0.1:8765
python3 rings/wave_input.py --left-addr AA:AA:AA:AA:AA:AA --right-addr BB:BB:BB:BB:BB:BB
```

On macOS, `--discover` prints a CoreBluetooth UUID, not a MAC address. Pass that UUID to `--addr`.

`run_asyncio_bluetooth` starts reading at the same moment it starts connecting, and its consumer gives up after 10 seconds with `Failed to receive valid package in 10 seconds, exiting consumer`. CoreBluetooth often takes longer than that. The consumer then sets cancel, so the later `Connected to Wave` line is followed immediately by `Recieved a cancel signal, stopping ble client`. Notifications and the `DeviceMode.API` write do run, but only after that timer has already fired, so no datastream is delivered.

The live path turns notifications on, writes the device-info request, writes `DeviceMode.API`, writes the motion-data config, and only then starts a 30 second package timer (`--packet-timeout`). Each step is logged with the seconds since connect started. If nothing arrives, it says whether any GATT notifies showed up and reconnects (`--reconnect-s`, default 2). `--connect-timeout` (default 40) is how long Bleak may spend connecting.

`--raw-test` runs the official example with no extra logic, so a failure there is the library timer and a success on `--print` is this client:

```bash
python3 rings/wave_input.py --addr EAED7675-7521-9A23-1011-6090ADC792C9 --raw-test
python3 rings/wave_input.py --addr EAED7675-7521-9A23-1011-6090ADC792C9 --print
```

`--print` streams `int` (integrated heading, degrees), `turn` (degrees since the last straight pose), `rate`, `gyro`, and `yaw_gyro` so the heading can be watched while the wrist turns. `--calibrate` records about two seconds of stillness (neutral yaw and gyro bias) into `logs/wave_cal.json` once packets are flowing, and exits. The bias is subtracted before the yaw rate is integrated. Flags: `--turn-deg` (default 45), `--turn-min-s` (0.7), `--turn-max-s` (3), `--flick-dps` (180), `--flick-g` (1.6), `--flick-max-s` (0.35), `--straight-dps` (8), `--straight-s` (0.5).

Turn the ring on with the middle button. It must not already be connected to Softwave or to the Mac, because bleak can only open a free device. On Linux, if the adapter grabs it, `bluetoothctl disconnect` first. If you Ctrl-C and the ring sticks in a bad session, hold the top button for a few seconds. macOS Apple Silicon uses the same virtualenv as the rest of the repo. `genki-wave` is on PyPI and talks BLE through bleak.

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
bridge/real_bridge_stub.py   radar or scripted cars, nav in both, buzz log only
bridge/camera_bridge.py      webcam fallback, same hub and mode toggle
bridge/radar_filter.py       background, threshold, persistence
bridge/hub.py                WebSocket fan-out
engine/game.py               haptics + score
rings/datafeel_rings.py      DataFeel dots, one chain or two El Jefe
rings/wave_input.py          Genki Wave turns and dismiss flicks
overlay/index.html           the HUD, one file
examples/short.jsonl         tiny replay
run.sh                       venv, bridge, engine
tests/test_e2e.py            headless end-to-end check
tests/test_datafeel_rings.py port map, patterns, dry-run against the fake bridge
tests/test_camera_bridge.py  synthetic boxes, persistence, mode toggle
tests/test_wave_input.py     heading turns, flick dismiss, calibration
```

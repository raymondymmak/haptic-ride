#!/usr/bin/env python3
"""Genki Wave as rider input. The Wave has no haptics; the DataFeel dots play.

A real turn is a sustained heading change, about 45–90 degrees over 1–3
seconds. That publishes ``{"type": "turn", "turn": "left|right", "side": ...}``.
A quick wrist flick (a short gyro or accel spike, or the Wave's own peak
flag) is not a turn. It publishes ``{"type": "dismiss"}`` so the engine
scores the car warning and the ring client cuts that buzz.

    python3 rings/wave_input.py --discover
    python3 rings/wave_input.py --addr D5:73:DB:85:B4:A1 --print
    python3 rings/wave_input.py --addr D5:73:DB:85:B4:A1 --calibrate
    python3 rings/wave_input.py --addr D5:73:DB:85:B4:A1
    python3 rings/wave_input.py --left-addr AA:AA:AA:AA:AA:AA --right-addr BB:BB:BB:BB:BB:BB

The ring must not already be connected (Softwave, system Bluetooth, or a
stuck session). After an unclean exit, hold the top button a few seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CAL = ROOT / "logs" / "wave_cal.json"

# Wave euler angles are radians. Its gyro in the public samples is degrees
# per second (a still ring reports tens, while yaw barely moves). Accel is
# in g. Linear accel is gravity removed.
TURN_DEG = 60.0
TURN_MIN_S = 1.0
TURN_MAX_S = 3.0
DEADBAND_DEG = 8.0
STRAIGHT_DPS = 12.0
STRAIGHT_S = 0.5
TAU_HEADING_S = 0.12
TAU_RATE_S = 0.08
FLICK_DPS = 180.0
FLICK_G = 1.6
FLICK_MAX_S = 0.35
FLICK_PEAK = 0.5
DISMISS_COOLDOWN_S = 0.45
# A heading change already underway is a turn, not a flick.
SUSTAINED_S = 0.4
SUSTAINED_DEG = 15.0
CAL_STILL_DPS = 25.0
CAL_HOLD_S = 2.0
PRINT_EVERY_S = 0.2


def wrap_deg(delta: float) -> float:
    return (delta + 180.0) % 360.0 - 180.0


def _hypot3(value) -> float:
    x, y, z = value
    return math.sqrt(x * x + y * y + z * z)


@dataclass
class Sample:
    """One IMU frame. `yaw_rad` is Wave euler yaw. Gyro is degrees per second."""

    t: float
    yaw_rad: float
    gyro: tuple[float, float, float]
    grav: tuple[float, float, float] = (0.0, 0.0, 1.0)
    linacc: tuple[float, float, float] = (0.0, 0.0, 0.0)
    peak: bool = False
    peak_norm: float = 0.0


def button_gesture(button: str, action: str, hand: str | None = None) -> dict | None:
    """Middle click dismisses. Bottom click looks back. Motion is separate."""
    if action != "click":
        return None
    if button == "middle":
        msg = {"type": "dismiss"}
        if hand in ("left", "right"):
            msg["side"] = hand
        return msg
    if button == "bottom":
        return {"type": "look_back"}
    return None


class GestureTracker:
    """Heading from orientation, flick from a short gyro or accel spike.

    While the rider is straight, heading is recentered so slow drift never
    grows into a fake turn. A turn arms when heading leaves a small deadband
    and fires once the change reaches `turn_deg` inside the time window.
    """

    def __init__(
        self,
        hand: str | None = None,
        invert: bool = False,
        turn_deg: float = TURN_DEG,
        turn_min_s: float = TURN_MIN_S,
        turn_max_s: float = TURN_MAX_S,
        flick_dps: float = FLICK_DPS,
        flick_g: float = FLICK_G,
        flick_max_s: float = FLICK_MAX_S,
        flick_peak: float = FLICK_PEAK,
        straight_dps: float = STRAIGHT_DPS,
        straight_s: float = STRAIGHT_S,
        tau_heading: float = TAU_HEADING_S,
        tau_rate: float = TAU_RATE_S,
        gyro_bias: tuple[float, float, float] = (0.0, 0.0, 0.0),
        neutral_yaw_rad: float | None = None,
    ) -> None:
        self.hand = hand
        self.invert = invert
        self.turn_deg = turn_deg
        self.turn_min_s = turn_min_s
        self.turn_max_s = turn_max_s
        self.flick_dps = flick_dps
        self.flick_g = flick_g
        self.flick_max_s = flick_max_s
        self.flick_peak = flick_peak
        self.straight_dps = straight_dps
        self.straight_s = straight_s
        self.tau_heading = tau_heading
        self.tau_rate = tau_rate
        self.gyro_bias = gyro_bias
        self.neutral_yaw_rad = neutral_yaw_rad
        self.heading = 0.0
        self.rate = 0.0
        self._gyro_mag = 0.0
        self._yaw_gyro = 0.0
        self._t: float | None = None
        self._raw = 0.0
        self._unwrapped = 0.0
        self.origin = 0.0
        self._straight_for = 0.0
        self.turn_t0: float | None = None
        self.turn_armed = True
        self._flick_on = False
        self._flick_t0 = 0.0
        self._peak_on = False
        self._last_dismiss = -1e9

    def preview(self) -> str:
        delta = self.heading - self.origin
        return (
            f"yaw {delta:+6.1f}°  rate {self.rate:+6.1f}°/s  "
            f"gyro {self._gyro_mag:5.0f}°/s  yaw_gyro {self._yaw_gyro:+6.1f}°/s"
        )

    def update(self, sample: Sample) -> list[dict]:
        if self._t is None:
            self._reset(sample)
            return []
        dt = sample.t - self._t
        if dt <= 0:
            return []
        if dt > 0.5:
            self._reset(sample)
            return []
        self._t = sample.t
        self._integrate(sample, dt)
        events = self._flick(sample)
        turned = self._turn(sample)
        events.extend(turned)
        self._straight(dt)
        return events

    def _reset(self, sample: Sample) -> None:
        yaw = math.degrees(sample.yaw_rad)
        self._raw = yaw
        self._unwrapped = yaw
        self.heading = yaw
        if self.neutral_yaw_rad is None:
            self.origin = yaw
        else:
            neutral = math.degrees(self.neutral_yaw_rad)
            self.origin = yaw - wrap_deg(yaw - neutral)
        self.rate = 0.0
        self._t = sample.t
        self._straight_for = 0.0
        self.turn_t0 = None
        self.turn_armed = True
        self._flick_on = False
        self._note_gyro(sample)

    def _note_gyro(self, sample: Sample) -> None:
        gyro = tuple(axis - bias for axis, bias in zip(sample.gyro, self.gyro_bias))
        self._gyro_mag = _hypot3(gyro)
        grav = _hypot3(sample.grav)
        if grav < 0.2:
            self._yaw_gyro = 0.0
        else:
            self._yaw_gyro = (
                gyro[0] * sample.grav[0] + gyro[1] * sample.grav[1] + gyro[2] * sample.grav[2]
            ) / grav

    def _integrate(self, sample: Sample, dt: float) -> None:
        raw = math.degrees(sample.yaw_rad)
        self._unwrapped += wrap_deg(raw - self._raw)
        self._raw = raw
        alpha = 1.0 - math.exp(-dt / self.tau_heading)
        previous = self.heading
        self.heading += alpha * (self._unwrapped - self.heading)
        inst = (self.heading - previous) / dt
        rate_alpha = 1.0 - math.exp(-dt / self.tau_rate)
        self.rate += rate_alpha * (inst - self.rate)
        self._note_gyro(sample)

    def _delta(self) -> float:
        return self.heading - self.origin

    def _turning_hard(self, t: float) -> bool:
        if self.turn_t0 is None:
            return False
        return (t - self.turn_t0) >= SUSTAINED_S and abs(self._delta()) >= SUSTAINED_DEG

    def _emit_dismiss(self, t: float) -> list[dict]:
        if t - self._last_dismiss < DISMISS_COOLDOWN_S or self._turning_hard(t):
            return []
        self._last_dismiss = t
        msg: dict = {"type": "dismiss"}
        if self.hand in ("left", "right"):
            msg["side"] = self.hand
        return [msg]

    def _flick(self, sample: Sample) -> list[dict]:
        events: list[dict] = []
        lin = _hypot3(sample.linacc)
        hot = self._gyro_mag >= self.flick_dps or lin >= self.flick_g
        if hot and not self._flick_on:
            self._flick_on = True
            self._flick_t0 = sample.t
        elif self._flick_on and not hot:
            duration = sample.t - self._flick_t0
            self._flick_on = False
            if duration <= self.flick_max_s:
                events.extend(self._emit_dismiss(sample.t))
        elif self._flick_on and sample.t - self._flick_t0 > self.flick_max_s:
            self._flick_on = False
        peak = bool(sample.peak) and sample.peak_norm >= self.flick_peak
        if peak and not self._peak_on:
            events.extend(self._emit_dismiss(sample.t))
        self._peak_on = peak
        return events

    def _turn(self, sample: Sample) -> list[dict]:
        delta = self._delta()
        if abs(delta) < DEADBAND_DEG:
            self.turn_t0 = None
            self.turn_armed = True
            return []
        if self.turn_t0 is None:
            self.turn_t0 = sample.t
        if not self.turn_armed or abs(delta) < self.turn_deg:
            return []
        duration = sample.t - self.turn_t0
        self.turn_armed = False
        if duration < self.turn_min_s or duration > self.turn_max_s:
            return []
        side = "right" if delta > 0 else "left"
        if self.invert:
            side = "left" if side == "right" else "right"
        return [{"type": "turn", "turn": side, "side": side}]

    def _straight(self, dt: float) -> None:
        if abs(self.rate) <= self.straight_dps:
            self._straight_for += dt
        else:
            self._straight_for = 0.0
            return
        if self._straight_for < self.straight_s:
            return
        self.origin = self.heading
        self._straight_for = 0.0
        self.turn_t0 = None
        self.turn_armed = True


def neutral_from(samples: list[Sample]) -> dict | None:
    """Mean yaw and gyro while the ring is held still. None if it moved."""
    if len(samples) < 5:
        return None
    span = samples[-1].t - samples[0].t
    if span < CAL_HOLD_S * 0.9:
        return None
    mags = []
    gyro = [0.0, 0.0, 0.0]
    sin_y = 0.0
    cos_y = 0.0
    for sample in samples:
        mag = _hypot3(sample.gyro)
        mags.append(mag)
        if mag > CAL_STILL_DPS:
            return None
        for index, axis in enumerate(sample.gyro):
            gyro[index] += axis
        sin_y += math.sin(sample.yaw_rad)
        cos_y += math.cos(sample.yaw_rad)
    count = float(len(samples))
    return {
        "yaw_rad": math.atan2(sin_y / count, cos_y / count),
        "gyro_bias": [axis / count for axis in gyro],
    }


def load_cal(path: Path, address: str) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    entry = data.get(_cal_key(address))
    if not isinstance(entry, dict):
        return None
    return entry


def save_cal(path: Path, address: str, entry: dict) -> None:
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    data[_cal_key(address)] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _cal_key(address: str) -> str:
    return address.replace(":", "").replace("-", "").upper()


def tracker_from_cal(hand: str | None, args, entry: dict | None) -> GestureTracker:
    bias = (0.0, 0.0, 0.0)
    yaw = None
    if entry:
        raw_bias = list(entry.get("gyro_bias") or [0, 0, 0])
        while len(raw_bias) < 3:
            raw_bias.append(0.0)
        bias = (float(raw_bias[0]), float(raw_bias[1]), float(raw_bias[2]))
        if entry.get("yaw_rad") is not None:
            yaw = float(entry["yaw_rad"])
    return GestureTracker(
        hand=hand,
        invert=args.invert,
        turn_deg=args.turn_deg,
        turn_min_s=args.turn_min_s,
        turn_max_s=args.turn_max_s,
        flick_dps=args.flick_dps,
        flick_g=args.flick_g,
        flick_max_s=args.flick_max_s,
        flick_peak=args.flick_peak,
        straight_dps=args.straight_dps,
        straight_s=args.straight_s,
        gyro_bias=bias,
        neutral_yaw_rad=yaw,
    )


def _component(value, name: str, index: int) -> float:
    if value is None:
        return 0.0
    if hasattr(value, name):
        return float(getattr(value, name))
    return float(value[index])


def sample_from_package(data, t_fallback: float) -> Sample | None:
    euler = getattr(data, "euler", None)
    gyro = getattr(data, "gyro", None)
    if euler is None or gyro is None:
        return None
    timestamp = getattr(data, "timestamp_us", None)
    t = float(timestamp) / 1e6 if timestamp else t_fallback
    grav = getattr(data, "grav", None)
    if grav is None:
        grav = (0.0, 0.0, 1.0)
    linacc = getattr(data, "linacc", None)
    if linacc is None:
        linacc = (0.0, 0.0, 0.0)
    return Sample(
        t=t,
        yaw_rad=float(getattr(euler, "yaw")),
        gyro=(
            _component(gyro, "x", 0),
            _component(gyro, "y", 1),
            _component(gyro, "z", 2),
        ),
        grav=(
            _component(grav, "x", 0),
            _component(grav, "y", 1),
            _component(grav, "z", 2),
        ),
        linacc=(
            _component(linacc, "x", 0),
            _component(linacc, "y", 1),
            _component(linacc, "z", 2),
        ),
        peak=bool(getattr(data, "peak", False)),
        peak_norm=float(getattr(data, "peak_norm_velocity", 0.0) or 0.0),
    )


class Bus:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self._down = False

    async def run(self) -> None:
        from websockets.asyncio.client import connect

        while True:
            try:
                async with connect(self.url, open_timeout=2) as ws:
                    self.ws = ws
                    self._down = False
                    print(f"wave  connected {self.url}", flush=True)
                    await ws.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.ws = None
                if not self._down:
                    print(f"wave  bridge {exc.__class__.__name__}: {exc} — retrying", flush=True)
                    self._down = True
            await asyncio.sleep(0.4)

    async def send(self, msg: dict) -> None:
        ws = self.ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(msg, separators=(",", ":")))
        except Exception:
            self.ws = None


def _waves(args) -> list[tuple[str, str | None]]:
    if args.left_addr and args.right_addr:
        if args.left_addr == args.right_addr:
            raise SystemExit("--left-addr and --right-addr must differ")
        return [(args.left_addr, "left"), (args.right_addr, "right")]
    if args.addr:
        return [(args.addr, None)]
    if args.left_addr:
        return [(args.left_addr, "left")]
    if args.right_addr:
        return [(args.right_addr, "right")]
    return []


def _import_wave():
    try:
        from genki_wave.asyncio_runner import consumer, producer_bluetooth
        from genki_wave.callbacks import WaveCallback
        from genki_wave.data.enums import ButtonAction, ButtonId
        from genki_wave.data.organization import ButtonEvent, DataPackage
        from genki_wave.discover import discover_bluetooth
        from genki_wave.protocols import CommunicateCancel, ProtocolAsyncio
    except ImportError as exc:
        raise SystemExit(
            "genki-wave is not installed. In this venv: python3 -m pip install genki-wave"
        ) from exc
    return {
        "consumer": consumer,
        "producer_bluetooth": producer_bluetooth,
        "WaveCallback": WaveCallback,
        "ButtonAction": ButtonAction,
        "ButtonId": ButtonId,
        "ButtonEvent": ButtonEvent,
        "DataPackage": DataPackage,
        "discover_bluetooth": discover_bluetooth,
        "CommunicateCancel": CommunicateCancel,
        "ProtocolAsyncio": ProtocolAsyncio,
    }


def _make_callback(api, tracker: GestureTracker, queue: asyncio.Queue, samples: list[Sample] | None, show: bool):
    button_id = api["ButtonId"]
    button_action = api["ButtonAction"]
    names = {
        button_id.TOP: "top",
        button_id.MIDDLE: "middle",
        button_id.BOTTOM: "bottom",
    }
    actions = {button_action.CLICK: "click"}

    class Callback(api["WaveCallback"]):
        def __init__(self) -> None:
            self._printed = 0.0

        def __call__(self, data) -> None:
            # Skip the library firmware gate so a demo ring on an older build still streams.
            if isinstance(data, api["ButtonEvent"]):
                self._button_handler(data)
            elif isinstance(data, api["DataPackage"]):
                self._data_handler(data)

        def _button_handler(self, data) -> None:
            button = names.get(data.button_id, "")
            action = actions.get(data.action, "")
            if show:
                print(f"wave  button {button or data.button_id} {action or data.action}", flush=True)
            msg = button_gesture(button, action, tracker.hand)
            if msg is not None:
                queue.put_nowait(msg)

        def _data_handler(self, data) -> None:
            sample = sample_from_package(data, time.monotonic())
            if sample is None:
                return
            if samples is not None:
                samples.append(sample)
            for msg in tracker.update(sample):
                queue.put_nowait(msg)
            if show and sample.t - self._printed >= PRINT_EVERY_S:
                self._printed = sample.t
                print("wave  " + tracker.preview(), flush=True)

    return Callback()


async def _connect_one(api, address: str, hand: str | None, args, queue: asyncio.Queue, samples: list[Sample] | None):
    entry = None if args.calibrate else load_cal(Path(args.cal), address)
    if entry:
        print(f"wave  cal {address}  bias {[round(v, 2) for v in entry.get('gyro_bias', [])]}", flush=True)
    tracker = tracker_from_cal(hand, args, entry)
    callback = _make_callback(api, tracker, queue, samples, args.print_ and not args.calibrate)
    protocol = api["ProtocolAsyncio"]()
    comm = api["CommunicateCancel"]()
    producer = api["producer_bluetooth"](
        protocol,
        comm,
        address,
        enable_spectrogram=False,
    )
    consume = api["consumer"](protocol, comm, [callback])
    return producer, consume, comm


async def _pump(queue: asyncio.Queue, bus: Bus | None) -> None:
    while True:
        msg = await queue.get()
        label = msg.get("side") or msg.get("turn") or ""
        print(f"wave  {msg['type']:8} {label}", flush=True)
        if bus is not None:
            await bus.send(msg)


async def _calibrate(address: str, samples: list[Sample], path: Path, stop: asyncio.Event) -> None:
    print(f"wave  calibrate {address}  hold still for {CAL_HOLD_S:.0f}s", flush=True)
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline and not stop.is_set():
        await asyncio.sleep(0.2)
        if len(samples) < 10:
            continue
        end = samples[-1].t
        window = [sample for sample in samples if sample.t >= end - CAL_HOLD_S]
        entry = neutral_from(window)
        if entry is None:
            continue
        save_cal(path, address, entry)
        bias = ", ".join(f"{axis:.2f}" for axis in entry["gyro_bias"])
        print(
            f"wave  saved {path}  {address}  yaw {math.degrees(entry['yaw_rad']):+.1f}°  bias {bias} °/s",
            flush=True,
        )
        stop.set()
        return
    if stop.is_set():
        return
    print("wave  calibrate timed out. Hold the ring still and run it again.", flush=True)
    stop.set()


async def _run(args) -> None:
    api = _import_wave()
    waves = _waves(args)
    if args.discover or not waves:
        print("Turn the Wave on with the middle button. It must not be connected to Softwave.", flush=True)
        await api["discover_bluetooth"]("Wave")
        if not waves:
            print("Pass --addr, or --left-addr and --right-addr, to connect.", flush=True)
        return

    queue: asyncio.Queue = asyncio.Queue()
    buckets: dict[str, list[Sample]] = {address: [] for address, _hand in waves} if args.calibrate else {}
    tasks = []
    comms = []
    for address, hand in waves:
        producer, consume, comm = await _connect_one(
            api, address, hand, args, queue, buckets.get(address)
        )
        comms.append(comm)
        tasks.append(asyncio.create_task(producer, name=f"wave-in-{hand or 'one'}"))
        tasks.append(asyncio.create_task(consume, name=f"wave-read-{hand or 'one'}"))

    bus = None
    if not args.calibrate:
        bus = Bus(args.url)
        tasks.append(asyncio.create_task(bus.run(), name="wave-bus"))
        tasks.append(asyncio.create_task(_pump(queue, bus), name="wave-pump"))

    stop = asyncio.Event()
    if args.calibrate:
        for address, _hand in waves:
            tasks.append(
                asyncio.create_task(
                    _calibrate(address, buckets[address], Path(args.cal), stop),
                    name=f"wave-cal-{address}",
                )
            )
    print("wave  input only. Ctrl-C stops. If the ring sticks, hold the top button a few seconds.", flush=True)
    try:
        if args.calibrate:
            await stop.wait()
        else:
            await asyncio.Event().wait()
    finally:
        for comm in comms:
            comm.cancel = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        print("wave  if the ring stays connected, hold the top button a few seconds to reset", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Haptic Ride Genki Wave input")
    parser.add_argument("--url", default="ws://127.0.0.1:8765", help="bridge websocket")
    parser.add_argument("--discover", action="store_true", help="print nearby Wave addresses and exit")
    parser.add_argument("--addr", default="", help="BLE address of one Wave; heading picks left or right")
    parser.add_argument("--left-addr", default="", help="left-hand Wave. With --right-addr, a flick carries that side")
    parser.add_argument("--right-addr", default="", help="right-hand Wave")
    parser.add_argument("--print", dest="print_", action="store_true", help="print live yaw, rate, and gyro")
    parser.add_argument("--calibrate", action="store_true", help="record a still pose and gyro bias, then exit")
    parser.add_argument("--cal", default=str(DEFAULT_CAL), help="calibration JSON path")
    parser.add_argument("--invert", action="store_true", help="swap left and right if the yaw sign is mirrored")
    parser.add_argument("--turn-deg", type=float, default=TURN_DEG, help="heading change that counts as a turn")
    parser.add_argument("--turn-min-s", type=float, default=TURN_MIN_S, help="fastest sustained turn")
    parser.add_argument("--turn-max-s", type=float, default=TURN_MAX_S, help="slowest sustained turn")
    parser.add_argument("--flick-dps", type=float, default=FLICK_DPS, help="gyro spike that dismisses, in deg/s")
    parser.add_argument("--flick-g", type=float, default=FLICK_G, help="linear accel spike that dismisses, in g")
    parser.add_argument("--flick-max-s", type=float, default=FLICK_MAX_S, help="a longer spike is not a flick")
    parser.add_argument("--flick-peak", type=float, default=FLICK_PEAK, help="Wave peak_norm_velocity that dismisses")
    parser.add_argument("--straight-dps", type=float, default=STRAIGHT_DPS, help="below this, heading recenters")
    parser.add_argument("--straight-s", type=float, default=STRAIGHT_S, help="how long straight lasts before recenter")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.turn_min_s <= 0 or args.turn_max_s < args.turn_min_s:
        raise SystemExit("--turn-min-s and --turn-max-s must be positive, with max >= min")
    if args.turn_deg <= 0 or args.flick_dps <= 0 or args.flick_max_s <= 0:
        raise SystemExit("thresholds must be positive")
    if args.flick_max_s >= args.turn_min_s:
        raise SystemExit("--flick-max-s must stay under --turn-min-s so a flick is not a turn")
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()

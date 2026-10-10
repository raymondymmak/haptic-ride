#!/usr/bin/env python3
"""Hardware bridge. Radar input is the BGT60TR13C distance FFT; rings are still a TODO.

The Windows VM that owns the USB kit:

    cd C:\\path\\to\\sdk\\examples\\py\\BGT60TR13C
    set PYTHONPATH=%CD%
    py -3.14 C:\\path\\to\\haptic-ride\\bridge\\real_bridge_stub.py --host 0.0.0.0

PYTHONPATH has to be the folder that contains helpers\\DistanceAlgo.py
(the same folder as Infineon's distance_fft.py). The Mac then points the
game engine at ws://<vm-ip>:8765 and opens http://<vm-ip>:8641.

Without the kit:

    python bridge/real_bridge_stub.py --synthetic
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import threading
import time
from pathlib import Path

import radar_filter
from hub import Hub, add_server_args, lan_ip, serve_overlay
from radar_filter import (
    CLOSING_MPS,
    RangeTracker,
    demo_live_frames,
    empty_scene,
    linspace,
    range_axis,
)

# Metrics that survive USB passthrough on the VM. Larger frames raise
# IFX_ERROR_FRAME_ACQUISITION_FAILED, so don't widen range or speed.
RANGE_RESOLUTION_M = 0.15
MAX_RANGE_M = 9.6
MAX_SPEED_M_S = 3.0
SPEED_RESOLUTION_M_S = 0.2
CENTER_FREQUENCY_HZ = 60_750_000_000
FRAME_RATE_HZ = 5
IF_GAIN_DB = 33
RX_MASK = 1  # one RX antenna

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BG = ROOT / "logs" / "radar_background.json"


def same_axis(saved, live) -> bool:
    if not saved or len(saved) != len(live):
        return False
    return abs(float(saved[0]) - float(live[0])) < 1e-3 and abs(float(saved[-1]) - float(live[-1])) < 1e-2


def load_background(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("background") or not data.get("ranges"):
            return None
        return data
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def save_background(path: Path, tracker: RangeTracker) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ranges": tracker.ranges, "background": tracker.background}
    path.write_text(json.dumps(payload), encoding="utf-8")
    print(f"radar  saved background → {path}", flush=True)


def car_message(side: str, reading: dict) -> dict:
    return {
        "type": "car",
        "id": "radar",
        "side": side,
        "dist": round(reading["dist"], 2),
        "speed": round(reading["speed"], 2),
    }


def report(side: str, tracker: RangeTracker, reading: dict | None) -> None:
    if reading is None:
        print(f"radar  {tracker.note}", flush=True)
        return
    print(
        f"radar  {side:5}  {reading['dist']:5.2f} m  {reading['speed']:+5.2f} m/s",
        flush=True,
    )


async def emit(hub: Hub, side: str, tracker: RangeTracker, reading: dict | None) -> None:
    report(side, tracker, reading)
    if reading is not None:
        await hub.broadcast(car_message(side, reading))


async def run_synthetic(hub: Hub, args) -> None:
    ranges = range_axis(args.max_range_m, args.range_resolution_m)
    tracker = RangeTracker(
        ranges,
        bg_frames=args.bg_frames,
        threshold_k=args.threshold_k,
        min_range_m=args.min_range_m,
        persist_frames=args.persist,
        smooth_frames=args.smooth,
        max_step_m=args.max_step_m,
    )
    dt = 1.0 / args.frame_rate
    t = 0.0
    print("radar  synthetic — calibrating on an empty scene with a static 4.7 m peak", flush=True)
    for _ in range(args.bg_frames):
        tracker.calibrate_frame(empty_scene(ranges))
        await asyncio.sleep(dt)
        t += dt
    print(f"radar  background ready  ({len(tracker.background)} bins)", flush=True)
    while True:
        for mag in demo_live_frames(ranges, dt, CLOSING_MPS):
            await emit(hub, args.side, tracker, tracker.update(mag, t))
            await asyncio.sleep(dt)
            t += dt


def configure_device(device, args):
    """Chirp setup from distance_fft.py, with the metrics that fit through the VM's USB passthrough."""
    from ifxradarsdk import get_version_full
    from ifxradarsdk.fmcw.types import FmcwMetrics, FmcwSimpleSequenceConfig
    from helpers.DistanceAlgo import DistanceAlgo

    print(f"radar  SDK {get_version_full()}  sensor {device.get_sensor_type()}", flush=True)
    metrics = FmcwMetrics(
        range_resolution_m=args.range_resolution_m,
        max_range_m=args.max_range_m,
        max_speed_m_s=args.max_speed_m_s,
        speed_resolution_m_s=args.speed_resolution_m_s,
        center_frequency_Hz=args.center_frequency_hz,
    )
    sequence = device.create_simple_sequence(FmcwSimpleSequenceConfig())
    sequence.loop.repetition_time_s = 1 / args.frame_rate
    chirp_loop = sequence.loop.sub_sequence.contents
    device.sequence_from_metrics(metrics, chirp_loop)

    chirp = chirp_loop.loop.sub_sequence.contents.chirp
    chirp.sample_rate_Hz = 1_000_000
    chirp.rx_mask = args.rx_mask
    chirp.tx_mask = 1
    chirp.tx_power_level = 31
    chirp.if_gain_dB = args.if_gain_db
    chirp.lp_cutoff_Hz = 500_000
    chirp.hp_cutoff_Hz = 80_000
    device.set_acquisition_sequence(sequence)

    algo = DistanceAlgo(chirp, chirp_loop.loop.num_repetitions)
    return algo, metrics


def read_magnitude(device, algo) -> list[float]:
    """One RX, one range profile. Same call as distance_fft.py."""
    frame_data = device.get_next_frame()[0]
    antenna_samples = frame_data[0, :, :]
    _peak_m, distance_data = algo.compute_distance(antenna_samples)
    return [float(value) for value in distance_data]


def device_thread(args, loop, queue: asyncio.Queue) -> None:
    def post(item: dict) -> None:
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:
            return

    try:
        from ifxradarsdk.fmcw import DeviceFmcw
    except ImportError as exc:
        post({
            "kind": "fatal",
            "text": (
                f"radar: {exc}. read_radar() idle. Install ifxradarsdk and put the SDK "
                "examples folder (the one with helpers/DistanceAlgo.py) on PYTHONPATH. "
                "Or run with --synthetic."
            ),
        })
        return

    try:
        with DeviceFmcw() as device:
            algo, metrics = configure_device(device, args)
            while True:
                try:
                    mag = read_magnitude(device, algo)
                except Exception as exc:
                    post({
                        "kind": "fatal",
                        "text": (
                            f"radar: {exc.__class__.__name__}: {exc}. read_radar() idle. "
                            "IFX_ERROR_FRAME_ACQUISITION_FAILED means the frame is too big "
                            "for USB passthrough — keep range resolution 0.15 m, max range "
                            "9.6 m, max speed about 3 m/s, one RX antenna."
                        ),
                    })
                    return
                post({
                    "kind": "frame",
                    "mag": mag,
                    "t": time.monotonic(),
                    "ranges": linspace(metrics.max_range_m, len(mag)),
                })
    except Exception as exc:
        post({"kind": "fatal", "text": f"radar: {exc.__class__.__name__}: {exc}. read_radar() idle."})


async def run_device(hub: Hub, args) -> None:
    saved = None if args.recalibrate else load_background(args.bg_file)
    if args.recalibrate:
        print("radar  --recalibrate: measuring a new empty scene", flush=True)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    threading.Thread(target=device_thread, args=(args, loop, queue), daemon=True).start()

    tracker: RangeTracker | None = None
    cache = saved
    while True:
        item = await queue.get()
        if item["kind"] == "fatal":
            print(item["text"], flush=True)
            while True:
                await asyncio.sleep(2)
        if tracker is None:
            tracker = RangeTracker(
                item["ranges"],
                bg_frames=args.bg_frames,
                threshold_k=args.threshold_k,
                min_range_m=args.min_range_m,
                persist_frames=args.persist,
                smooth_frames=args.smooth,
                max_step_m=args.max_step_m,
            )
            if cache and same_axis(cache["ranges"], item["ranges"]):
                tracker.background = [float(v) for v in cache["background"]]
                print(
                    f"radar  loaded background {args.bg_file}  ({len(tracker.background)} bins)",
                    flush=True,
                )
                cache = None
            else:
                if cache:
                    print("radar  saved background does not match this frame, measuring again", flush=True)
                print("radar  calibrating — keep the scene empty", flush=True)
                cache = None
        if tracker.background is None:
            count = tracker.calibrate_frame(item["mag"])
            print(f"radar  cal  {count}/{args.bg_frames}", flush=True)
            if tracker.background is not None:
                print(f"radar  background ready  ({len(tracker.background)} bins)", flush=True)
                try:
                    save_background(args.bg_file, tracker)
                except OSError as exc:
                    print(f"radar  could not save background ({exc})", flush=True)
            continue
        await emit(hub, args.side, tracker, tracker.update(item["mag"], item["t"]))


async def read_radar(hub: Hub, args) -> None:
    if args.synthetic:
        await run_synthetic(hub, args)
    else:
        await run_device(hub, args)


async def play_buzz(cmd: dict) -> None:
    """Play one buzz on the Genki Wave rings or a DataFeel devkit.

    TODO — this is the only place that should touch the actuators.
    `cmd` is already validated:

        side       "left" | "right" | "both"
        pattern    turn_soon | turn_now | car_approach | blue_shell | combo
        intensity  float 0..1

    Feel the team can aim for:

        turn_soon      soft pulses, about 2 Hz, gain = intensity
        turn_now       hard double-tap
        car_approach   steady rumble, amplitude follows intensity
        blue_shell     fast staccato at full strength
        combo          short rising burst, usually both rings

    The hub has already printed this command.
    """
    _ = cmd


def banner(args) -> None:
    ip = lan_ip()
    mode = "synthetic radar" if args.synthetic else "BGT60TR13C distance FFT"
    print("", flush=True)
    print(f"  haptic-ride  hardware bridge  ({mode})", flush=True)
    print(f"  overlay     http://127.0.0.1:{args.http_port}", flush=True)
    print(f"  websocket   ws://{ip}:{args.port}", flush=True)
    print(f"  cars        side={args.side}  read_radar()", flush=True)
    print("  rings       play_buzz() still a TODO", flush=True)
    print("", flush=True)


async def amain(args) -> None:
    hub = Hub(mode="synthetic" if args.synthetic else "real")
    hub.on_buzz = play_buzz
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    serve_overlay(args.host, args.http_port, args.port)
    banner(args)

    from websockets.asyncio.server import serve

    try:
        async with serve(hub.handler, args.host, args.port):
            task = asyncio.create_task(read_radar(hub, args))
            await stop.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    except OSError as exc:
        raise SystemExit(f"websocket port {args.port} is already in use ({exc})") from exc


def main() -> None:
    parser = argparse.ArgumentParser(description="Haptic Ride hardware bridge")
    add_server_args(parser)
    parser.add_argument("--synthetic", action="store_true", help="feed a fake range-FFT through the filter")
    parser.add_argument("--side", choices=("left", "right"), default="right", help="ring for this single-target radar")
    parser.add_argument("--recalibrate", action="store_true", help="ignore the saved empty-scene average and measure again")
    parser.add_argument("--bg-file", type=Path, default=DEFAULT_BG, help="cached empty-scene magnitude")
    parser.add_argument("--bg-frames", type=int, default=radar_filter.BG_FRAMES)
    parser.add_argument("--threshold-k", type=float, default=radar_filter.THRESHOLD_K, help="peak must exceed k × the median bin")
    parser.add_argument("--min-range-m", type=float, default=radar_filter.MIN_RANGE_M)
    parser.add_argument("--persist", type=int, default=radar_filter.PERSIST_FRAMES, help="consecutive frames before a target is real")
    parser.add_argument("--smooth", type=int, default=radar_filter.SMOOTH_FRAMES, help="median window for distance")
    parser.add_argument("--max-step-m", type=float, default=radar_filter.MAX_STEP_M, help="jump larger than this starts a new target")
    parser.add_argument("--frame-rate", type=float, default=FRAME_RATE_HZ)
    parser.add_argument("--if-gain", dest="if_gain_db", type=int, default=IF_GAIN_DB, help="33–45 worked; higher is louder")
    parser.add_argument("--rx-mask", type=int, default=RX_MASK, help="1 = one RX antenna")
    parser.add_argument("--range-resolution-m", type=float, default=RANGE_RESOLUTION_M)
    parser.add_argument("--max-range-m", type=float, default=MAX_RANGE_M)
    parser.add_argument("--max-speed-m-s", type=float, default=MAX_SPEED_M_S)
    parser.add_argument("--speed-resolution-m-s", type=float, default=SPEED_RESOLUTION_M_S)
    parser.add_argument("--center-frequency-hz", type=float, default=CENTER_FREQUENCY_HZ)
    args = parser.parse_args()
    if args.side not in ("left", "right"):
        raise SystemExit("--side must be left or right")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""The radar filter, without a kit: static clutter stays quiet, a closing car gets through.

    python tests/test_radar_filter.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_filter():
    spec = importlib.util.spec_from_file_location("radar_filter", ROOT / "bridge" / "radar_filter.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def calibrate(tracker, frame) -> None:
    for _ in range(tracker.bg_frames):
        tracker.calibrate_frame(frame)
    assert tracker.background is not None


def check_static_and_approach(mod) -> None:
    ranges = mod.range_axis()
    tracker = mod.RangeTracker(ranges)
    empty = mod.empty_scene(ranges)
    calibrate(tracker, empty)

    for i in range(6):
        assert tracker.update(empty, i * 0.2) is None, tracker.note

    outlier = mod.spectrum(ranges, [(mod.STATIC_DIST_M, mod.STATIC_AMP), (6.0, 40.0)])
    assert tracker.update(outlier, 2.0) is None
    assert len(tracker._tracks) == 1
    assert tracker._tracks[0]["streak"] == 1

    dt = 0.2
    readings = []
    t = 10.0
    for mag in mod.demo_live_frames(ranges, dt):
        readings.append(tracker.update(mag, t))
        t += dt

    # 5 quiet + 1 outlier + 2 quiet, then the approach.
    # The smoothing window is 5 frames, so nothing publishes before the 5th approach frame.
    quiet = 5 + 1 + 2
    assert all(reading is None for reading in readings[: quiet + 4]), readings[: quiet + 4]
    first = readings[quiet + 4]
    assert first is not None
    assert 6.2 < first["dist"] < 8.2
    assert 1.2 < first["speed"] < 2.8
    emitted = [reading for reading in readings[quiet:] if reading is not None]
    assert len(emitted) >= 6, emitted
    assert all(reading["speed"] >= tracker.min_approach for reading in emitted)
    assert emitted[0]["dist"] > 6.0
    assert emitted[-1]["dist"] < emitted[0]["dist"]
    assert all(reading["dist"] > 2.4 for reading in emitted)
    print("ok  static clutter is silent and a closing car is emitted")


def check_roof_peaks_and_adaptive_background(mod) -> None:
    """The live failure: 1.74 m and ~4.9 m, speed jittering around zero, swapping which is louder."""
    ranges = mod.range_axis()
    tracker = mod.RangeTracker(ranges, bg_alpha=0.05, min_approach=0.4)
    calibrate(tracker, mod.spectrum(ranges, []))
    bin_174 = min(range(len(ranges)), key=lambda i: abs(ranges[i] - 1.74))
    before = tracker.background[bin_174]

    for i in range(24):
        if i % 2 == 0:
            mag = mod.spectrum(ranges, [(1.74, 50.0), (4.9, 28.0)])
        else:
            mag = mod.spectrum(ranges, [(1.74, 26.0), (4.9, 48.0)])
        assert tracker.update(mag, i * 0.2) is None, tracker.note
    assert tracker.note.startswith("static")
    assert tracker.background[bin_174] > before + 5.0

    # A car closing from 8 m is the detection. The nearer roof peaks are not.
    readings = []
    dist = 8.0
    t = 20.0
    for _ in range(10):
        mag = mod.spectrum(ranges, [(1.74, 40.0), (4.9, 36.0), (dist, 30.0)])
        readings.append(tracker.update(mag, t))
        dist -= 0.35
        t += 0.2
    emitted = [reading for reading in readings if reading is not None]
    assert len(emitted) >= 4, readings
    assert all(reading["dist"] > 5.5 for reading in emitted), emitted
    assert all(reading["speed"] >= 0.4 for reading in emitted)
    assert emitted[-1]["dist"] < emitted[0]["dist"]

    # Moving away is silence, even when it is loud and persistent.
    receding = mod.RangeTracker(ranges, min_approach=0.4)
    calibrate(receding, mod.spectrum(ranges, []))
    dist = 3.0
    for i in range(10):
        assert receding.update(mod.spectrum(ranges, [(dist, 40.0)]), i * 0.2) is None, receding.note
        dist += 0.4
    print("ok  roof peaks at 1.74 m and 4.9 m stay silent; a closing car publishes")


def check_threshold_and_near_range(mod) -> None:
    ranges = mod.range_axis()
    zeros = [0.0] * len(ranges)
    tracker = mod.RangeTracker(ranges, threshold_k=6)
    calibrate(tracker, zeros)

    floor = [1.0] * len(ranges)
    weak_i = min(range(len(ranges)), key=lambda i: abs(ranges[i] - 2.0))
    weak = floor[:]
    weak[weak_i] = 3.0  # under 6 × the median floor
    for i in range(5):
        assert tracker.update(weak, i) is None
    assert tracker.note.startswith("below threshold")

    strong = floor[:]
    strong[weak_i] = 30.0
    for i in range(8):
        assert tracker.update(strong, 10 + i * 0.2) is None
    assert tracker.note.startswith("static")

    near_i = min(range(len(ranges)), key=lambda i: abs(ranges[i] - 0.15))
    assert ranges[near_i] < 0.3
    fresh = mod.RangeTracker(ranges, threshold_k=6, min_approach=0.4)
    calibrate(fresh, zeros)
    dist = 3.0
    emitted = []
    for i in range(6):
        mag = [1.0] * len(ranges)
        mag[near_i] = 100.0
        far_i = min(range(len(ranges)), key=lambda n: abs(ranges[n] - dist))
        mag[far_i] = 15.0
        reading = fresh.update(mag, i * 0.2)
        if reading is not None:
            emitted.append(reading)
        dist -= 0.4
    assert emitted
    assert all(reading["dist"] > 1.0 for reading in emitted)
    assert all(reading["speed"] >= 0.4 for reading in emitted)
    print("ok  weak peaks, static peaks, and bins under 0.3 m are ignored")


def check_median_smooth(mod) -> None:
    ranges = mod.range_axis()
    tracker = mod.RangeTracker(ranges, threshold_k=6, max_step_m=20, smooth_frames=5, min_approach=0.4)
    calibrate(tracker, [0.0] * len(ranges))
    # Four samples closing, then one frame that leaps much closer. The median stays with the trend.
    series = (8.0, 7.4, 6.8, 6.2, 3.0)
    reading = None
    for n, dist in enumerate(series):
        index = min(range(len(ranges)), key=lambda i: abs(ranges[i] - dist))
        mag = [1.0] * len(ranges)
        mag[index] = 40.0
        reading = tracker.update(mag, n * 0.2)
    assert reading is not None
    assert reading["dist"] > 6.0
    assert reading["speed"] >= 0.4
    print("ok  median of five frames rejects a one-frame jump")


class Proc:
    def __init__(self, args: list[str]):
        self.lines: list[str] = []
        self.proc = subprocess.Popen(
            args,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.append(line)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()


async def check_socket() -> None:
    from websockets.asyncio.client import connect

    url = "ws://127.0.0.1:8781"
    cars = []
    deadline = time.monotonic() + 8
    async with connect(url) as sock:
        while time.monotonic() < deadline and len(cars) < 4:
            raw = await asyncio.wait_for(sock.recv(), timeout=8)
            msg = json.loads(raw)
            if msg.get("type") == "car":
                cars.append(msg)
    assert len(cars) >= 4
    assert all(car["id"] == "radar" for car in cars)
    assert all(car["side"] == "right" for car in cars)
    assert all(car["type"] == "car" and "dist" in car and "speed" in car for car in cars)
    assert all(car["speed"] > 0.5 for car in cars)
    closing = any(
        cars[j]["dist"] < cars[i]["dist"] - 0.05
        for i in range(len(cars))
        for j in range(i + 1, len(cars))
    )
    assert closing

    async with connect(url) as sock:
        hello = json.loads(await asyncio.wait_for(sock.recv(), timeout=3))
        assert hello["type"] == "hello" and hello["source"] == "real"
        await sock.send(json.dumps({"type": "mode", "value": "sim"}))
        saw_mode = False
        saw_nav = False
        sim_cars = []
        settle = 0.0
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=4))
            if msg.get("type") == "mode" and msg.get("value") == "sim":
                saw_mode = True
                settle = time.monotonic() + 0.4
                continue
            if not saw_mode:
                continue
            if msg.get("type") == "nav":
                saw_nav = True
            if msg.get("type") == "car" and str(msg.get("id", "")).startswith("c"):
                sim_cars.append(msg)
            if msg.get("type") == "car" and msg.get("id") == "radar" and time.monotonic() >= settle:
                raise AssertionError(msg)
            if saw_nav and sim_cars and time.monotonic() >= settle:
                break
        assert saw_mode and saw_nav and sim_cars

        await sock.send(json.dumps({"type": "mode", "value": "real"}))
        saw_real = False
        saw_nav = False
        radar_back = False
        settle = 0.0
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not (saw_real and saw_nav and radar_back and time.monotonic() >= settle):
            msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=4))
            if msg.get("type") == "mode" and msg.get("value") == "real":
                saw_real = True
                settle = time.monotonic() + 0.4
                continue
            if not saw_real or time.monotonic() < settle:
                continue
            if msg.get("type") == "nav":
                saw_nav = True
            if msg.get("type") == "car" and str(msg.get("id", "")).startswith("c"):
                raise AssertionError(msg)
            if msg.get("type") == "car" and msg.get("id") == "radar":
                radar_back = True
        assert saw_real and saw_nav and radar_back
    print("ok  --synthetic publishes a closing car on the websocket")


def main() -> None:
    mod = load_filter()
    check_static_and_approach(mod)
    check_roof_peaks_and_adaptive_background(mod)
    check_threshold_and_near_range(mod)
    check_median_smooth(mod)

    proc = Proc(
        [
            sys.executable,
            "bridge/real_bridge_stub.py",
            "--host", "127.0.0.1",
            "--port", "8781",
            "--http-port", "8661",
            "--synthetic",
            "--frame-rate", "30",
        ]
    )
    try:
        time.sleep(0.4)
        asyncio.run(check_socket())
    except Exception:
        print("".join(proc.lines[-40:]))
        raise
    finally:
        proc.stop()
    text = "".join(proc.lines)
    assert "background ready" in text
    assert any(line.startswith("radar  right") for line in text.splitlines())
    print("ok  radar filter")


if __name__ == "__main__":
    main()

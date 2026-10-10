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
    assert tracker._streak == 1

    dt = 0.2
    readings = []
    t = 10.0
    for mag in mod.demo_live_frames(ranges, dt):
        readings.append(tracker.update(mag, t))
        t += dt

    # 5 quiet + 1 outlier + 2 quiet, then the approach. Nothing before the 3rd approach frame.
    quiet = 5 + 1 + 2
    assert all(reading is None for reading in readings[: quiet + 2]), readings[: quiet + 2]
    first = readings[quiet + 2]
    assert first is not None
    assert 7.0 < first["dist"] < 8.2
    assert 1.2 < first["speed"] < 2.8
    emitted = [reading for reading in readings[quiet:] if reading is not None]
    assert len(emitted) >= 8
    assert all(reading["speed"] > 0.8 for reading in emitted)
    assert emitted[-1]["dist"] < emitted[0]["dist"]
    print("ok  static clutter is silent and a closing car is emitted")


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
    got = None
    for i in range(3):
        got = tracker.update(strong, 10 + i)
    assert got is not None and abs(got["dist"] - ranges[weak_i]) < 0.2

    near_i = min(range(len(ranges)), key=lambda i: abs(ranges[i] - 0.15))
    assert ranges[near_i] < 0.3
    far_i = weak_i
    leaked = [0.0] * len(ranges)
    leaked[near_i] = 100.0
    leaked[far_i] = 15.0
    fresh = mod.RangeTracker(ranges, threshold_k=6)
    calibrate(fresh, zeros)
    got = None
    for i in range(3):
        got = fresh.update(leaked, i)
    assert got is not None
    assert got["dist"] > 1.0
    print("ok  weak peaks and bins under 0.3 m are ignored")


def check_median_smooth(mod) -> None:
    ranges = mod.range_axis()
    tracker = mod.RangeTracker(ranges, threshold_k=6, max_step_m=20, smooth_frames=5)
    calibrate(tracker, [0.0] * len(ranges))
    indexes = []
    for dist in (5.0, 5.0, 5.0, 5.0, 1.0):
        indexes.append(min(range(len(ranges)), key=lambda i: abs(ranges[i] - dist)))
    reading = None
    for n, index in enumerate(indexes):
        mag = [1.0] * len(ranges)
        mag[index] = 40.0
        reading = tracker.update(mag, n)
    assert reading is not None
    assert reading["dist"] > 4.0  # the 1 m sample does not win the median
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
    assert all(car["side"] == "right" for car in cars)
    assert all(car["type"] == "car" and "dist" in car and "speed" in car for car in cars)
    assert all(car["speed"] > 0.5 for car in cars)
    closing = any(
        cars[j]["dist"] < cars[i]["dist"] - 0.05
        for i in range(len(cars))
        for j in range(i + 1, len(cars))
    )
    assert closing
    print("ok  --synthetic publishes a closing car on the websocket")


def main() -> None:
    mod = load_filter()
    check_static_and_approach(mod)
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

#!/usr/bin/env python3
"""Wave gesture rules, without a ring or Bluetooth.

    python tests/test_wave_input.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_wave():
    spec = importlib.util.spec_from_file_location("wave_input", ROOT / "rings" / "wave_input.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wave_input"] = mod
    spec.loader.exec_module(mod)
    return mod


def feed(tracker, wave, yaw0, yaw1, t0, t1, hz=50, gyro=None, linacc=(0.0, 0.0, 0.0), peak=False, peak_norm=0.0):
    span = t1 - t0
    n = max(2, int(round(span * hz)) + 1)
    rate = (yaw1 - yaw0) / span if span else 0.0
    events = []
    for index in range(n):
        mix = index / (n - 1)
        yaw = yaw0 + (yaw1 - yaw0) * mix
        gyro_value = gyro if gyro is not None else (0.0, 0.0, rate)
        events.extend(
            tracker.update(
                wave.Sample(
                    t=t0 + span * mix,
                    yaw_rad=math.radians(yaw),
                    gyro=gyro_value,
                    grav=(0.0, 0.0, 1.0),
                    linacc=linacc,
                    peak=peak,
                    peak_norm=peak_norm,
                )
            )
        )
    return events


def kinds(events):
    return [event["type"] for event in events]


def check_gestures(wave) -> None:
    tracker = wave.GestureTracker()
    tracker.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    turn = feed(tracker, wave, 0.0, 90.0, 0.05, 2.05)
    assert kinds(turn) == ["turn"], turn
    assert turn[0]["turn"] == "right" and turn[0]["side"] == "right"
    assert "dismiss" not in kinds(turn)

    quiet = feed(tracker, wave, 90.0, 90.0, 2.05, 3.2, gyro=(0.0, 0.0, 0.0))
    assert quiet == []
    again = feed(tracker, wave, 90.0, 180.0, 3.3, 5.3)
    assert kinds(again) == ["turn"] and again[0]["turn"] == "right"

    left = wave.GestureTracker()
    left.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    left_turn = feed(left, wave, 0.0, -90.0, 0.05, 2.05)
    assert kinds(left_turn) == ["turn"] and left_turn[0]["turn"] == "left"

    mirrored = wave.GestureTracker(invert=True)
    mirrored.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    flipped = feed(mirrored, wave, 0.0, 90.0, 0.05, 2.05)
    assert flipped[0]["turn"] == "left"

    drift = wave.GestureTracker()
    drift.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    assert feed(drift, wave, 0.0, 40.0, 0.05, 8.05) == []

    fast = wave.GestureTracker()
    fast.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    jerked = feed(fast, wave, 0.0, 90.0, 0.05, 0.30)
    jerked += feed(fast, wave, 90.0, 90.0, 0.30, 0.55, gyro=(0.0, 0.0, 0.0))
    assert "turn" not in kinds(jerked), jerked
    assert "dismiss" in kinds(jerked)

    flick = wave.GestureTracker(hand="left")
    flick.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    spike = feed(flick, wave, 0.0, 30.0, 0.05, 0.17, gyro=(0.0, 0.0, 250.0))
    spike += feed(flick, wave, 30.0, 30.0, 0.17, 0.40, gyro=(0.0, 0.0, 0.0))
    assert kinds(spike) == ["dismiss"], spike
    assert spike[0]["side"] == "left"

    tap = wave.GestureTracker()
    tap.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    bumped = feed(tap, wave, 0.0, 0.0, 0.05, 0.15, gyro=(0.0, 0.0, 0.0), linacc=(2.2, 0.0, 0.0))
    bumped += feed(tap, wave, 0.0, 0.0, 0.15, 0.40, gyro=(0.0, 0.0, 0.0))
    assert kinds(bumped) == ["dismiss"]

    peaked = wave.GestureTracker()
    peaked.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    peak_events = peaked.update(
        wave.Sample(0.1, 0.0, (0.0, 0.0, 0.0), peak=True, peak_norm=2.0)
    )
    peak_events += peaked.update(wave.Sample(0.12, 0.0, (0.0, 0.0, 0.0), peak=True, peak_norm=2.0))
    assert kinds(peak_events) == ["dismiss"]

    busy = wave.GestureTracker()
    busy.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    # 90 degrees over 2s. A peak once the turn is already underway is not a flick.
    mid = []
    span = 2.0
    for index in range(1, 101):
        mix = index / 100
        yaw = 90.0 * mix
        t = 0.05 + span * mix
        mid.extend(
            busy.update(
                wave.Sample(
                    t=t,
                    yaw_rad=math.radians(yaw),
                    gyro=(0.0, 0.0, 45.0),
                    peak=1.15 < t < 1.30,
                    peak_norm=3.0 if 1.15 < t < 1.30 else 0.0,
                )
            )
        )
    assert kinds(mid) == ["turn"], mid

    # Euler yaw stuck at 0. The ring still reports a world-yaw gyro, which is what we integrate.
    stuck = wave.GestureTracker()
    stuck.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    gyro_turn = []
    for index in range(1, 71):
        gyro_turn.extend(
            stuck.update(wave.Sample(index * 0.02, 0.0, (0.0, 0.0, 40.0), grav=(0.0, 0.0, 1.0)))
        )
    assert kinds(gyro_turn) == ["turn"] and gyro_turn[0]["turn"] == "right", gyro_turn
    assert abs(stuck.heading) > 40.0
    line = stuck.preview()
    assert "int" in line and "turn" in line
    assert "+0.0" not in line.split("int", 1)[1][:12]

    # Pose quaternion maps body X onto world up. Gravity is empty, so the axis has to come from the pose.
    half = math.sqrt(0.5)
    tilted = wave.GestureTracker()
    tilted.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0), grav=(0.0, 0.0, 0.0), quat=(1.0, 0.0, 0.0, 0.0)))
    pose_turn = []
    for index in range(1, 71):
        pose_turn.extend(
            tilted.update(
                wave.Sample(
                    index * 0.02,
                    0.0,
                    (40.0, 0.0, 0.0),
                    grav=(0.0, 0.0, 0.0),
                    quat=(half, 0.0, -half, 0.0),
                )
            )
        )
    assert kinds(pose_turn) == ["turn"], pose_turn

    # A pause in the middle of a turn must not snap the integral back to zero.
    paused = wave.GestureTracker()
    paused.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 0.0)))
    feed(paused, wave, 0.0, 22.0, 0.05, 0.55)
    held = feed(paused, wave, 22.0, 22.0, 0.55, 1.8, gyro=(0.0, 0.0, 0.0))
    assert held == []
    assert abs(paused.heading) > 12.0, paused.heading
    rest = feed(paused, wave, 22.0, 55.0, 1.85, 2.6)
    assert kinds(rest) == ["turn"], rest

    biased = wave.GestureTracker(gyro_bias=(0.0, 0.0, 10.0))
    biased.update(wave.Sample(0.0, 0.0, (0.0, 0.0, 10.0)))
    assert feed(biased, wave, 0.0, 0.0, 0.05, 2.5, gyro=(0.0, 0.0, 10.0)) == []
    assert abs(biased.heading) < 8.0
    real = feed(biased, wave, 0.0, 0.0, 2.6, 4.0, gyro=(0.0, 0.0, 50.0))
    assert kinds(real) == ["turn"], real

    assert wave.TURN_DEG == 45.0 and wave.TURN_MIN_S == 0.7

    class _Vec:
        def __init__(self, x, y, z):
            self.x, self.y, self.z = x, y, z

    class _Quat:
        def __init__(self, w, x, y, z):
            self.w, self.x, self.y, self.z = w, x, y, z

    class _Pack:
        gyro = _Vec(30.0, 0.0, 0.0)
        grav = _Vec(0.0, 0.0, 0.0)
        current_pose = _Quat(half, 0.0, -half, 0.0)
        timestamp_us = 1_000_000

    packed = wave.sample_from_package(_Pack(), 0.0)
    assert packed is not None and packed.quat[0] == half
    assert abs(wave.world_yaw_dps(packed) - 30.0) < 1e-6
    print("ok  sustained turn, flick dismiss, gyro heading")


def check_buttons_and_cal(wave) -> None:
    assert wave.button_gesture("middle", "click") == {"type": "dismiss"}
    assert wave.button_gesture("middle", "click", "right") == {"type": "dismiss", "side": "right"}
    assert wave.button_gesture("bottom", "click") == {"type": "look_back"}
    assert wave.button_gesture("top", "click") is None
    assert wave.button_gesture("middle", "down") is None

    still = []
    for index in range(40):
        still.append(wave.Sample(index * 0.05, math.radians(12.0), (0.4, -0.2, 0.1)))
    entry = wave.neutral_from(still)
    assert entry is not None
    assert abs(math.degrees(entry["yaw_rad"]) - 12.0) < 0.1
    assert abs(entry["gyro_bias"][0] - 0.4) < 1e-6

    moving = list(still)
    moving.append(wave.Sample(2.1, 0.0, (0.0, 0.0, 80.0)))
    assert wave.neutral_from(moving) is None

    path = ROOT / "logs" / "test-wave-cal.json"
    path.unlink(missing_ok=True)
    wave.save_cal(path, "aa:bb:cc:dd:ee:ff", entry)
    loaded = wave.load_cal(path, "AA:BB:CC:DD:EE:FF")
    assert loaded["gyro_bias"][0] == entry["gyro_bias"][0]
    path.unlink()

    parser = wave.build_parser()
    help_text = parser.format_help()
    assert "--print" in help_text and "--calibrate" in help_text and "--discover" in help_text
    assert "--raw-test" in help_text and "--packet-timeout" in help_text
    try:
        wave.main(["--turn-min-s", "0.2", "--flick-max-s", "0.3", "--addr", "00"])
    except SystemExit as exc:
        assert "flick" in str(exc)
    else:
        raise AssertionError("overlapping flick and turn windows should be rejected")
    try:
        wave.main(["--raw-test"])
    except SystemExit as exc:
        assert "raw-test" in str(exc)
    else:
        raise AssertionError("--raw-test without an address should exit")
    assert "genki_wave" not in sys.modules
    print("ok  buttons, calibration, and tuning flags")


async def check_arm_before_timeout(wave) -> None:
    """A slow connect must finish the API writes before the receive timer can fire."""
    logs: list[str] = []
    queue: asyncio.Queue = asyncio.Queue()

    class Slow:
        async def start_notify(self, _uuid, _cb):
            logs.append("inside-notify")
            await asyncio.sleep(0.2)
            await queue.put("pkt")

        async def write_gatt_char(self, _uuid, _data, _response=False):
            logs.append("write")

    got: list[str] = []

    def on_package(package) -> bool:
        got.append(package)
        logs.append("delivered")
        return True

    result = await wave.arm_and_read(
        Slow(),
        "char",
        None,
        [("device-info request", b"a"), ("DeviceMode.API", b"b"), ("MOTION_DATA config", b"c")],
        queue,
        on_package,
        0.05,
        logs.append,
        asyncio.Event(),
    )
    assert result == "package", result
    assert got == ["pkt"]
    assert logs.index("inside-notify") < logs.index("sent DeviceMode.API")
    assert logs.index("sent MOTION_DATA config") < logs.index("delivered")
    assert "timeout" not in logs

    empty: asyncio.Queue = asyncio.Queue()
    quiet: list[str] = []

    class SlowQuiet:
        async def start_notify(self, _uuid, _cb):
            quiet.append("inside-notify")
            await asyncio.sleep(0.2)

        async def write_gatt_char(self, _uuid, _data, _response=False):
            quiet.append("write")

    result = await wave.arm_and_read(
        SlowQuiet(),
        "char",
        None,
        [("device-info request", b"a"), ("DeviceMode.API", b"b"), ("MOTION_DATA config", b"c")],
        empty,
        lambda _package: False,
        0.05,
        quiet.append,
        asyncio.Event(),
    )
    assert result == "timeout"
    assert quiet.count("write") == 3
    assert quiet.index("sent DeviceMode.API") < quiet.index("timeout")
    assert quiet.index("inside-notify") < quiet.index("timeout")
    print("ok  package timer starts after DeviceMode.API")


def main() -> None:
    wave = load_wave()
    check_gestures(wave)
    check_buttons_and_cal(wave)
    asyncio.run(check_arm_before_timeout(wave))
    print("ok  wave input")


if __name__ == "__main__":
    main()

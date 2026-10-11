#!/usr/bin/env python3
"""DataFeel ring client: discovery map, pattern override, dry-run on the fake bridge.

    python tests/test_datafeel_rings.py
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
os.chdir(ROOT)


def load_rings():
    spec = importlib.util.spec_from_file_location("datafeel_rings", ROOT / "rings" / "datafeel_rings.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Port:
    def __init__(self, device, vid=None, pid=None, description="", manufacturer="", hwid="", product="", name=""):
        self.device = device
        self.vid = vid
        self.pid = pid
        self.description = description
        self.manufacturer = manufacturer
        self.hwid = hwid
        self.product = product
        self.name = name


def check_ports(rings) -> None:
    cu = "/dev/cu.SLAB_USBtoUART"
    tty = "/dev/tty.SLAB_USBtoUART"
    found = rings.matching_ports([
        Port(tty, description="SLAB_USBtoUART"),
        Port(cu, description="SLAB_USBtoUART"),
        Port("/dev/cu.Bluetooth-Incoming-Port", description="Bluetooth"),
    ])
    assert found == [cu], found

    com = rings.matching_ports([
        Port("COM10", vid=0x10C4, pid=0xEA60),
        Port("COM3", vid=0x10C4, pid=0xEA60),
    ])
    assert com == ["COM3", "COM10"], com

    serials = rings.matching_ports([
        Port("/dev/cu.usbserial-110"),
        Port("/dev/cu.usbserial-20"),
    ])
    assert serials == ["/dev/cu.usbserial-20", "/dev/cu.usbserial-110"], serials

    hwid = rings.matching_ports([
        Port("COM4", hwid="USB VID:PID=10C4:EA60 SER=ABC"),
    ])
    assert hwid == ["COM4"], hwid
    print("ok  port scan prefers cu nodes and sorts names")


def check_map(rings) -> None:
    discovered = rings.Discovered
    port = "/dev/cu.usbserial-210"
    # The live kit: address 1 empty, address 2 present, address 3 empty, address 4 present.
    dots = [discovered(port, 4), discovered(port, 2)]
    left, right, note = rings.assign_discovered(dots, ports=[port])
    assert [dot.address for dot in left] == [2], note
    assert [dot.address for dot in right] == [4], note
    assert "discovered addresses 2, 4" in note

    left, right, _note = rings.assign_discovered(dots, ports=[port], swap=True)
    assert [dot.address for dot in left] == [4]
    assert [dot.address for dot in right] == [2]

    left, right, _note = rings.assign_discovered(dots, ports=[port], left_addr=4, right_addr=2)
    assert [dot.address for dot in left] == [4]
    assert [dot.address for dot in right] == [2]

    try:
        rings.assign_discovered(dots, ports=[port], left_addr=1)
    except SystemExit as exc:
        assert "1" in str(exc)
    else:
        raise AssertionError("address 1 was not discovered and should be rejected")

    first, second = "/dev/cu.usbserial-110", "/dev/cu.usbserial-210"
    both = [discovered(first, 2), discovered(second, 2), discovered(second, 4)]
    left, right, note = rings.assign_discovered(both, ports=[first, second])
    assert left[0].port == first and [dot.address for dot in left] == [2]
    assert right[0].port == second and [dot.address for dot in right] == [2, 4]
    assert "two El Jefe" in note
    left, right, _note = rings.assign_discovered(both, ports=[first, second], swap=True)
    assert left[0].port == second and right[0].port == first

    ports = rings.choose_ports(
        ["/dev/cu.usbserial-20", "/dev/cu.usbserial-110", "/dev/cu.usbserial-300"]
    )
    assert ports == ["/dev/cu.usbserial-20", "/dev/cu.usbserial-110"], ports
    forced = rings.choose_ports(
        ["/dev/cu.usbserial-20", "/dev/cu.usbserial-110"],
        port="/dev/cu.usbserial-210",
    )
    assert forced == ["/dev/cu.usbserial-210"]
    try:
        rings.choose_ports([], port="/dev/cu.x", left_port="/dev/cu.y")
    except SystemExit:
        pass
    else:
        raise AssertionError("mixed --port and --left-port should fail")

    assert rings.approach_frequency(0) == 100
    assert rings.approach_frequency(1) == 250
    assert rings.approach_frequency(0.64) == 196
    assert rings.dim_channel(255, 0.15) == 38
    assert rings.dim_rgb(255, 0, 0, 0.15, 0.64) == (24, 0, 0)
    assert rings.AMBER_SOFT == (255, 230, 0) and rings.AMBER_SOFT_GAIN == 1.0
    assert rings.AMBER == rings.AMBER_SOFT
    soon_color = rings.dim_rgb(*rings.AMBER_SOFT, 0.15, rings.AMBER_SOFT_GAIN)
    assert rings.dim_rgb(*rings.AMBER, 0.15) == soon_color
    assert rings.TURN_SOON_GLOW_S >= 1.0
    assert rings.TURN_NOW_BLINKS >= 4 and rings.TURN_NOW_BLINK_HZ == 6.0
    assert rings.shell_heat(1) == 1
    print("ok  discovered addresses 2 and 4 map to left and right")


class Rec:
    def __init__(self, side: str, events: list):
        self.side = side
        self.port = "rec"
        self.addresses = [1]
        self.events = events
        self.led_brightness = 0.15

    def available(self) -> bool:
        return True

    def has_thermal(self) -> bool:
        return True

    def describe(self) -> str:
        return "1"

    async def manual(self, intensity, frequency):
        self.events.append((self.side, "vib", round(float(intensity), 2)))

    async def led(self, red, green, blue):
        self.events.append((self.side, "led", red, green, blue))

    async def led_off(self):
        self.events.append((self.side, "led_off"))

    async def heat(self, intensity):
        self.events.append((self.side, "heat", round(float(intensity), 2)))

    async def thermal_off(self):
        self.events.append((self.side, "thermal_off"))

    async def idle(self):
        self.events.append((self.side, "idle"))


async def check_override(rings) -> None:
    events: list = []
    player = rings.Player({"left": Rec("left", events), "right": Rec("right", events)})
    await player.play("left", "car_approach", 0.5)
    await asyncio.sleep(0.05)
    await player.play("left", "combo", 1.0)
    await player.wait("left")
    left = [event for event in events if event[0] == "left"]
    assert [event for event in left if event[1] == "idle"] == [("left", "idle")]
    assert ("left", "led", 19, 0, 0) in left
    assert not any(event[1] == "led" and event[3] > 0 for event in left)

    events.clear()
    await player.play("both", "turn_soon", 0.4)
    await asyncio.sleep(0.02)
    await player.play("left", "turn_now", 0.9)
    await player.wait()
    assert [event for event in events if event[0] == "right" and event[1] == "idle"] == [("right", "idle")]
    assert [event for event in events if event[0] == "left" and event[1] == "idle"] == [("left", "idle")]
    print("ok  a newer buzz overrides one wrist and still idles")


async def check_turn_cues(rings) -> None:
    """Same amber. Soon is one pulse and a long glow. Now blinks and pulses rapidly."""
    amber = rings.dim_rgb(*rings.AMBER, 0.15)
    events: list = []
    player = rings.Player({"left": Rec("left", events), "right": Rec("right", events)})

    started = time.monotonic()
    await player.play("left", "turn_soon", 0.45)
    await player.wait("left")
    soon_s = time.monotonic() - started
    assert soon_s >= rings.TURN_SOON_GLOW_S - 0.05, soon_s
    leds = [event for event in events if event[1] == "led"]
    offs = [event for event in events if event[1] == "led_off"]
    pulses = [event for event in events if event[1] == "vib" and event[2] > 0]
    assert leds == [("left", "led", *amber)], leds
    assert len(offs) == 1
    assert len(pulses) == 1

    events.clear()
    started = time.monotonic()
    await player.play("left", "turn_now", 0.9)
    await player.wait("left")
    now_s = time.monotonic() - started
    assert now_s < soon_s
    leds = [event for event in events if event[1] == "led"]
    offs = [event for event in events if event[1] == "led_off"]
    pulses = [event for event in events if event[1] == "vib" and event[2] > 0]
    assert len(leds) == rings.TURN_NOW_BLINKS
    assert len(offs) == rings.TURN_NOW_BLINKS
    assert all(event[2:] == amber for event in leds)
    assert len(pulses) == rings.TURN_NOW_BLINKS
    print("ok  turn_soon glows and turn_now blinks the same amber")


async def check_behind_sync(rings) -> None:
    events: list = []
    player = rings.Player({"left": Rec("left", events), "right": Rec("right", events)})
    await player.play("both", "car_behind", 0.8)
    await player.wait()
    pulses = [event for event in events if event[1] == "vib" and event[2] > 0]
    sides = [event[0] for event in pulses]
    assert sides.count("left") == rings.BEHIND_PULSES
    assert sides.count("right") == rings.BEHIND_PULSES
    assert sides[:2] == ["left", "right"]
    assert sides != sorted(sides)
    leds = [event for event in events if event[1] == "led"]
    assert len(leds) == rings.BEHIND_PULSES * 2
    assert all(event[3] == 0 and event[4] == 0 and 0 < event[2] <= 40 for event in leds)
    assert leds[0][2:] == leds[1][2:]
    print("ok  car_behind pulses both wrists on the same beat")


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
                self.proc.wait(timeout=2)

    def text(self) -> str:
        return "".join(self.lines)


def check_test_flag(rings) -> None:
    proc = Proc([sys.executable, "rings/datafeel_rings.py", "--test", "--dry-run"])
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if "TEST done" in proc.text():
                break
            if proc.proc.poll() is not None:
                break
            time.sleep(0.1)
        text = proc.text()
    finally:
        proc.stop()
    assert proc.proc.returncode in (0, None, -15), text[-500:]
    markers = [
        "TEST LEFT identify",
        "TEST RIGHT identify",
        "TEST LEFT turn_soon",
        "TEST RIGHT turn_soon",
        "TEST LEFT turn_now",
        "TEST RIGHT turn_now",
        "TEST LEFT car_approach",
        "TEST RIGHT car_approach",
        "TEST LEFT blue_shell",
        "TEST RIGHT blue_shell",
        "TEST LEFT combo",
        "TEST RIGHT combo",
    ]
    pos = 0
    for marker in markers:
        idx = text.find(marker, pos)
        assert idx >= 0, marker + "\n" + text
        pos = idx + len(marker)
    assert "dots=lowest" in text and "dots=next" in text
    assert "play_frequency  MANUAL" in text
    assert "frequency=196" in text
    assert "triple tap, no LED" in text
    assert "slow amber glow" in text and "rapid amber blink" in text and "pulsing blue" in text
    assert "TEST BOTH car_behind" in text and "synced red pulses" in text
    amber = rings.dim_rgb(*rings.AMBER, rings.DEFAULT_LED_BRIGHTNESS)
    soon = rings.dim_rgb(*rings.AMBER_SOFT, rings.DEFAULT_LED_BRIGHTNESS, rings.AMBER_SOFT_GAIN)
    assert soon == amber
    blue = rings.dim_rgb(*rings.BLUE_PULSE, rings.DEFAULT_LED_BRIGHTNESS)
    red = rings.dim_rgb(255, 0, 0, rings.DEFAULT_LED_BRIGHTNESS, 0.64)
    amber_line = f"set_led  {amber[0]},{amber[1]},{amber[2]}"
    for color in (amber, blue, red):
        assert f"set_led  {color[0]},{color[1]},{color[2]}" in text
        assert max(color) <= 40
    soon_left = text[text.find("TEST LEFT turn_soon"):text.find("TEST RIGHT turn_soon")]
    now_left = text[text.find("TEST LEFT turn_now"):text.find("TEST RIGHT turn_now")]
    assert soon_left.count(amber_line) == 1
    assert now_left.count(amber_line) == rings.TURN_NOW_BLINKS
    assert "set_led  0,255,0" not in text
    assert "set_led  0,38,0" not in text
    assert "activate_thermal_intensity_control  1.00" in text
    assert text.count("stop_vibration") >= 12
    print("ok  --test --dry-run buzzes LEFT then RIGHT")


async def inject(port: int) -> None:
    from websockets.asyncio.client import connect

    async with connect(f"ws://127.0.0.1:{port}") as sock:
        await asyncio.wait_for(sock.recv(), timeout=3)
        samples = [
            ("left", "turn_soon", 0.4),
            ("right", "turn_now", 0.9),
            ("right", "car_approach", 0.64),
            ("left", "blue_shell", 1),
            ("both", "combo", 1),
        ]
        for side, pattern, intensity in samples:
            await sock.send(json.dumps({
                "type": "buzz",
                "side": side,
                "pattern": pattern,
                "intensity": intensity,
            }))
            await asyncio.sleep(0.05)
        await sock.send(json.dumps({"type": "nav", "turn": "left", "in_m": 30}))


def check_bridge() -> None:
    py = sys.executable
    bridge = Proc([
        py, "bridge/fake_bridge.py",
        "--host", "127.0.0.1", "--port", "8791", "--http-port", "8671",
    ])
    rings = None
    engine = None
    try:
        time.sleep(0.4)
        rings = Proc([
            py, "rings/datafeel_rings.py",
            "--dry-run",
            "--url", "ws://127.0.0.1:8791",
            "--port", "/dev/cu.usbserial-210",
            "--left-addr", "2",
            "--right-addr", "4",
        ])
        deadline = time.monotonic() + 5
        while "rings  connected" not in rings.text():
            if time.monotonic() > deadline:
                raise AssertionError(rings.text())
            time.sleep(0.05)
        asyncio.run(inject(8791))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            text = rings.text()
            if (
                "dots=2" in text
                and "dots=4" in text
                and "play_frequency  MANUAL" in text
                and "activate_thermal_intensity_control  1.00" in text
                and "set_led  24,0,0" in text
                and "stop_vibration" in text
            ):
                break
            time.sleep(0.05)
        else:
            raise AssertionError(rings.text())
        assert "usbserial-210" in rings.text()
        assert "turn_soon" in rings.text() and "blue_shell" in rings.text()
        assert "set_led  0,255,0" not in rings.text()

        engine = Proc([py, "engine/game.py", "--url", "ws://127.0.0.1:8791"])
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if "buzz  left   turn_soon" in rings.text() or "buzz  left   car_approach" in rings.text():
                break
            time.sleep(0.05)
        else:
            raise AssertionError("engine never reached the ring client\n" + rings.text()[-1500:])
    finally:
        if engine is not None:
            engine.stop()
        if rings is not None:
            rings.stop()
        bridge.stop()
    print("ok  dry-run client plays bridge buzzes on the discovered addresses")


def main() -> None:
    rings = load_rings()
    check_ports(rings)
    check_map(rings)
    asyncio.run(check_override(rings))
    asyncio.run(check_turn_cues(rings))
    asyncio.run(check_behind_sync(rings))
    check_test_flag(rings)
    check_bridge()
    print("ok  datafeel rings")


if __name__ == "__main__":
    main()

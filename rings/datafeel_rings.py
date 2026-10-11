#!/usr/bin/env python3
"""Play Haptic Ride buzz commands on DataFeel dots.

The game engine publishes {"type": "buzz", "side", "pattern", "intensity"}
on the bridge websocket. This process is the only one that opens the El Jefe
serial port. bridge/real_bridge_stub.py logs the same messages and does not
touch the actuators.

Connection matches samples/read_write.py:

    from datafeel.device import VibrationMode, discover_devices, LedMode, ThermalMode, VibrationWaveforms
    devices = discover_devices(4)

Addresses are not fixed. The scan walks 1..N (default 4, same as that
sample). On one El Jefe the lowest address that answers is the left dot
and the next address is the right dot. A live kit answered on
/dev/cu.usbserial-210 at address 2 (address 1 was empty). --left-addr and
--right-addr override the map. --swap exchanges the wrists.

A second CP210x port, if one is present, is the other wrist: ports are
sorted by name and each side drives the dots discovered on that chain.
discover_devices() itself stops after the first port, so a second port is
probed with the same address loop.

        python rings/datafeel_rings.py --url ws://127.0.0.1:8765
        python rings/datafeel_rings.py --dry-run --url ws://127.0.0.1:8765
        python rings/datafeel_rings.py --test --dry-run
        python rings/datafeel_rings.py --port /dev/cu.usbserial-210
        python rings/datafeel_rings.py --left-addr 2 --right-addr 4
        python rings/datafeel_rings.py --swap

Playback uses the sample's calls: VibrationMode.MANUAL, play_frequency,
stop_vibration, set_led, set_led_off, activate_thermal_intensity_control,
disable_all_thermal. LEDs are scaled by --led-brightness (default 0.15).
turn_soon and turn_now share one amber. Soon holds that color as a steady
glow for about 1.2 s with one pulse. Now blinks it about five times at 6 Hz
with a pulse on each blink. car_approach is red, blue_shell pulsing blue.
car_behind pulses both dots together with a dim red LED. combo is a triple
tap with no LED. The LED goes off when the pattern ends. A dismiss message
from the Wave cuts car_approach, car_behind, and blue_shell immediately.
Turn cues keep playing.
BLE is the C# stack and is not used here.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
from typing import Callable, Iterable

# CP210x USB-to-UART, the bridge inside El Jefe. macOS names the call-out
# node cu.SLAB_USBtoUART (Silicon Labs VCP) or cu.usbserial-* (generic).
CP210X_VID = 0x10C4
CP210X_PID = 0xEA60

PATTERNS = ("turn_soon", "turn_now", "car_approach", "car_behind", "blue_shell", "combo")
SIDES = ("left", "right", "both")
CAR_PATTERNS = {"car_approach", "car_behind", "blue_shell"}

# The engine resends a held cue about every 0.45 s. Continuous feels hold
# a little longer than that so a refresh restarts the task before idle.
APPROACH_HOLD_S = 0.70
APPROACH_HZ_LO = 100.0
APPROACH_HZ_HI = 250.0
TURN_SOON_HZ = 130.0
TURN_NOW_HZ = 180.0
TURN_SOON_S = 0.12
# Longer than the engine's 0.45 s resend, so a held turn_soon stays lit.
TURN_SOON_GLOW_S = 1.2
# 5 blinks at 6 Hz, on and off equal, at the --led-brightness cap.
TURN_NOW_BLINKS = 5
TURN_NOW_BLINK_HZ = 6.0
BLUE_SHELL_HZ = 240.0
BLUE_SHELL_PULSES = 8
COMBO_HZ = 200.0
COMBO_TAPS = 3
BEHIND_PULSES = 4
BEHIND_HZ = 160.0

TEST_INTENSITY = {
    "turn_soon": 0.45,
    "turn_now": 0.90,
    "car_approach": 0.64,
    "car_behind": 0.80,
    "blue_shell": 1.0,
    "combo": 1.0,
}

# Full-scale colors. dim_rgb multiplies by --led-brightness (default 0.15),
# so the brightest channel is about 38/255.
DEFAULT_LED_BRIGHTNESS = 0.15
AMBER_SOFT = (255, 230, 0)
AMBER_SOFT_GAIN = 1.0
AMBER = (255, 230, 0)
BLUE_PULSE = (0, 72, 255)
LED_NOTE = {
    "turn_soon": "slow amber glow",
    "turn_now": "rapid amber blink",
    "car_approach": "red",
    "car_behind": "synced red pulses",
    "blue_shell": "pulsing blue",
    "combo": "triple tap, no LED",
}

PatternFn = Callable[["Output", float], "asyncio.Future"]


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def approach_frequency(intensity: float) -> float:
    """Hz rises with the buzz intensity. 100 Hz at 0, 250 Hz at 1."""
    return APPROACH_HZ_LO + clamp(intensity) * (APPROACH_HZ_HI - APPROACH_HZ_LO)


def dim_channel(channel: int, brightness: float, gain: float = 1.0) -> int:
    """Scale one 0..255 channel by the global cap and an optional cue gain."""
    scale = clamp(brightness) * clamp(gain)
    return max(0, min(255, int(round(int(channel) * scale))))


def dim_rgb(
    red: int,
    green: int,
    blue: int,
    brightness: float,
    gain: float = 1.0,
) -> tuple[int, int, int]:
    return (
        dim_channel(red, brightness, gain),
        dim_channel(green, brightness, gain),
        dim_channel(blue, brightness, gain),
    )


def shell_heat(intensity: float) -> float:
    """ThermalMode.MANUAL intensity. Positive is heat, 1.0 is full."""
    return min(1.0, 0.65 + 0.35 * clamp(intensity))


def natural_key(text: str) -> list:
    """Sort COM3 before COM10, and usbserial-20 before usbserial-110."""
    parts = re.split(r"(\d+)", text)
    return [int(part) if part.isdigit() else part.lower() for part in parts]


def _vid_pid(port) -> tuple[int | None, int | None]:
    vid = getattr(port, "vid", None)
    pid = getattr(port, "pid", None)
    if vid is not None and pid is not None:
        return vid, pid
    hwid = str(getattr(port, "hwid", "") or "")
    match = re.search(r"VID:PID=([0-9A-Fa-f]{4}):([0-9A-Fa-f]{4})", hwid)
    if not match:
        return vid, pid
    return int(match.group(1), 16), int(match.group(2), 16)


def _port_blob(port) -> str:
    bits = [getattr(port, "device", "") or ""]
    for attr in ("description", "manufacturer", "product", "hwid", "name"):
        bits.append(str(getattr(port, attr, "") or ""))
    return " ".join(bits)


def _callout_name(device: str, available: set[str]) -> str:
    """Prefer /dev/cu.* over /dev/tty.* on macOS. Opening tty can block."""
    if device.startswith("/dev/tty."):
        callout = "/dev/cu." + device[len("/dev/tty.") :]
        if callout in available:
            return callout
    return device


def is_datafeel_port(port, available: set[str] | None = None) -> str | None:
    """Return the device path when this looks like an El Jefe, else None."""
    device = getattr(port, "device", "") or ""
    if not device:
        return None
    blob = _port_blob(port)
    vid, pid = _vid_pid(port)
    cp210 = vid == CP210X_VID and pid == CP210X_PID
    slab = "SLAB_USBtoUART" in blob
    usbserial = "usbserial" in blob.lower()
    named = "CP210" in blob.upper() or "SILICON LABS" in blob.upper() or "SILICONLABS" in blob.upper()
    if not (cp210 or slab or usbserial or named):
        return None
    return _callout_name(device, available or set())


def matching_ports(ports: Iterable) -> list[str]:
    """Unique El Jefe candidates, sorted by port name."""
    available = {getattr(port, "device", "") or "" for port in ports}
    found: list[str] = []
    for port in ports:
        device = is_datafeel_port(port, available)
        if device and device not in found:
            found.append(device)
    found.sort(key=natural_key)
    return found


def glob_serial_ports() -> list[str]:
    """macOS fallback when pyserial does not list the call-out node."""
    if os.name == "nt":
        return []
    found: list[str] = []
    for pattern in ("/dev/cu.SLAB_USBtoUART", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.usbserial-*"):
        found.extend(glob.glob(pattern))
    return sorted(set(found), key=natural_key)


def detect_ports() -> list[str]:
    """List El Jefe serial ports. Empty when nothing is plugged in.

    A missing pyserial install does not raise: --dry-run and an explicit
    --port still work. Windows needs pyserial to see COMx; the SDK provides it.
    """
    found: list[str] = []
    try:
        from serial.tools import list_ports
    except ImportError:
        list_ports = None
    if list_ports is not None:
        try:
            found = matching_ports(list(list_ports.comports()))
        except OSError as exc:
            print(f"rings  port scan failed ({exc})", flush=True)
            found = []
    if found:
        return found
    return glob_serial_ports()


class Discovered:
    """One dot that answered during the address scan."""

    def __init__(self, port: str, address: int, dot=None):
        self.port = port
        self.address = int(address)
        self.dot = dot

    def __str__(self) -> str:
        if self.dot is not None:
            return str(self.dot)
        return f"Dot {self.address}"


def _address_of(dot) -> int:
    if getattr(dot, "address", None) is not None:
        return int(dot.address)
    return int(dot.id)


def _port_of(dot) -> str:
    return str(getattr(dot, "port"))


def choose_ports(found: list[str], *, port: str = "", left_port: str = "", right_port: str = "") -> list[str]:
    """Which El Jefe ports to scan. Two ports means one wrist each."""
    if port and (left_port or right_port):
        raise SystemExit("--port is one El Jefe. For two modules use --left-port and --right-port.")
    if left_port or right_port:
        return _explicit_pair(found, left_port, right_port)
    if port:
        return [port]
    if len(found) >= 2:
        return list(found[:2])
    if len(found) == 1:
        return list(found)
    raise SystemExit(
        "no DataFeel serial port found. Plug in El Jefe (CP210x) or pass "
        "--port / --left-port / --right-port. macOS: /dev/cu.SLAB_USBtoUART "
        "or /dev/cu.usbserial-*. Windows: COMx (install the Silicon Labs "
        "CP210x driver). The SDK provides pyserial: pip install . from "
        "devkit-samples/Python."
    )


def _explicit_pair(found: list[str], left_port: str, right_port: str) -> list[str]:
    if left_port and right_port:
        pair = [left_port, right_port]
    elif left_port:
        rest = [item for item in found if item != left_port]
        if not rest:
            raise SystemExit(
                f"--left-port {left_port} is set, but no other DataFeel port was found for the right wrist"
            )
        pair = [left_port, rest[0]]
    else:
        rest = [item for item in found if item != right_port]
        if not rest:
            raise SystemExit(
                f"--right-port {right_port} is set, but no other DataFeel port was found for the left wrist"
            )
        pair = [rest[0], right_port]
    if pair[0] == pair[1]:
        raise SystemExit("left and right serial ports must differ. For one El Jefe, use --port.")
    return pair


def _missing_addr(flag: str, addr: int, dots: list) -> SystemExit:
    found = ", ".join(str(_address_of(dot)) for dot in dots) or "none"
    port = _port_of(dots[0]) if dots else "?"
    return SystemExit(f"no dot at {flag} {addr} on {port}. discovered addresses: {found}")


def _pair_from_chain(ordered: list, left_addr: int | None, right_addr: int | None):
    """Lowest responding address is left, the next is right, unless overridden."""
    by_addr = {_address_of(dot): dot for dot in ordered}
    if left_addr is not None and right_addr is not None and left_addr == right_addr:
        raise SystemExit("--left-addr and --right-addr must differ")
    if left_addr is not None and left_addr not in by_addr:
        raise _missing_addr("--left-addr", left_addr, ordered)
    if right_addr is not None and right_addr not in by_addr:
        raise _missing_addr("--right-addr", right_addr, ordered)
    if left_addr is None and right_addr is None:
        left = ordered[0] if ordered else None
        right = ordered[1] if len(ordered) > 1 else None
        return left, right, list(ordered[2:])
    left = by_addr[left_addr] if left_addr is not None else None
    right = by_addr[right_addr] if right_addr is not None else None
    used = {id(left), id(right)}
    remaining = [dot for dot in ordered if id(dot) not in used]
    if left is None and remaining:
        left = remaining.pop(0)
    if right is None and remaining:
        right = remaining.pop(0)
    return left, right, remaining


def _select_port_dots(dots: list, addr: int | None, flag: str) -> list:
    ordered = sorted(dots, key=_address_of)
    if addr is None:
        return ordered
    match = [dot for dot in ordered if _address_of(dot) == addr]
    if not match:
        raise _missing_addr(flag, addr, ordered)
    return match


def assign_discovered(
    dots: list,
    *,
    ports: list[str],
    left_addr: int | None = None,
    right_addr: int | None = None,
    swap: bool = False,
) -> tuple[list, list, str]:
    """Map a discover_devices-style scan onto the two wrists.

    One port: sort by address, lowest = left, next = right.
    Two ports: each El Jefe is a wrist (sorted order unless --swap), and
    every dot that answered on that port plays for that wrist.
    """
    if left_addr is not None and right_addr is not None and left_addr == right_addr:
        raise SystemExit("--left-addr and --right-addr must differ")
    grouped: dict[str, list] = {}
    for dot in dots:
        grouped.setdefault(_port_of(dot), []).append(dot)
    for port, group in grouped.items():
        group.sort(key=_address_of)

    if len(ports) >= 2:
        left_port, right_port = ports[0], ports[1]
        if swap:
            left_port, right_port = right_port, left_port
        left = _select_port_dots(grouped.get(left_port, []), left_addr, "--left-addr")
        right = _select_port_dots(grouped.get(right_port, []), right_addr, "--right-addr")
        note = f"two El Jefe ports; {left_port} is left, {right_port} is right"
        if swap:
            note += " (--swap)"
        extra = ports[2:]
        if extra:
            note += "; ignoring " + ", ".join(extra)
        return left, right, note

    port = ports[0] if ports else (next(iter(grouped)) if grouped else "")
    ordered = grouped.get(port, [])
    if not ordered and grouped:
        ordered = next(iter(grouped.values()))
        port = _port_of(ordered[0])
    left, right, unused = _pair_from_chain(ordered, left_addr, right_addr)
    addrs = ", ".join(str(_address_of(dot)) for dot in ordered) or "none"
    note = f"one El Jefe {port}; discovered addresses {addrs}; lowest is left, next is right"
    if left_addr is not None or right_addr is not None:
        note += " (address override)"
    if swap:
        left, right = right, left
        note += " (--swap)"
    if unused:
        skipped = ", ".join(str(_address_of(dot)) for dot in unused)
        note += f"; not mapped: {skipped}"
    return [left] if left is not None else [], [right] if right is not None else [], note


class Output:
    """One wrist. A dual setup has one of these per El Jefe."""

    def __init__(self, side: str, port: str, addresses: list[int] | None, label: str = ""):
        self.side = side
        self.port = port
        self.addresses = list(addresses or [])
        self.label = label
        self.led_brightness = DEFAULT_LED_BRIGHTNESS

    def available(self) -> bool:
        return True

    def has_thermal(self) -> bool:
        return False

    def describe(self) -> str:
        if self.addresses:
            return ",".join(str(addr) for addr in self.addresses)
        return self.label or "none"

    async def manual(self, intensity: float, frequency: float) -> None:
        raise NotImplementedError

    async def led(self, red: int, green: int, blue: int) -> None:
        raise NotImplementedError

    async def led_off(self) -> None:
        raise NotImplementedError

    async def heat(self, intensity: float) -> None:
        raise NotImplementedError

    async def thermal_off(self) -> None:
        raise NotImplementedError

    async def idle(self) -> None:
        raise NotImplementedError


class PrintOutput(Output):
    """--dry-run. Same pattern timing, commands go to stdout."""

    def __init__(self, side: str, port: str, addresses: list[int] | None, label: str = "", thermal: bool = True):
        super().__init__(side, port, addresses, label)
        self._thermal = thermal
        self._said_no_thermal = False

    def has_thermal(self) -> bool:
        return self._thermal

    def _line(self, text: str) -> None:
        print(f"  {self.side:5}  {self.port}  dots={self.describe()}  {text}", flush=True)

    async def manual(self, intensity: float, frequency: float) -> None:
        # play_frequency(hz, intensity) sets VibrationMode.MANUAL.
        self._line(f"play_frequency  MANUAL  intensity={intensity:.2f}  frequency={frequency:.0f}")

    async def led(self, red: int, green: int, blue: int) -> None:
        self._line(f"set_led  {red},{green},{blue}")

    async def led_off(self) -> None:
        self._line("set_led_off")

    async def heat(self, intensity: float) -> None:
        self._line(f"activate_thermal_intensity_control  {intensity:.2f}")

    async def thermal_off(self) -> None:
        if not self._thermal:
            if not self._said_no_thermal:
                self._said_no_thermal = True
                self._line("thermal unavailable")
            return
        self._line("disable_all_thermal")

    async def idle(self) -> None:
        thermal = "disable_all_thermal" if self._thermal else "thermal unavailable"
        self._line(f"stop_vibration  set_led_off  {thermal}")


def dot_has_thermal(dot) -> bool:
    """samples/read_write.py heats with activate_thermal_intensity_control."""
    return hasattr(dot, "activate_thermal_intensity_control") and hasattr(dot, "disable_all_thermal")


class HardwareOutput(Output):
    """Real dots. Writes on one serial port are serialized."""

    def __init__(self, side: str, port: str, dots: list, io_lock: asyncio.Lock, vibration_mode):
        addresses = [_address_of(dot) for dot in dots]
        super().__init__(side, port, addresses)
        self.dots = dots
        self.io = io_lock
        self.vibration_mode = vibration_mode
        self._thermal = any(dot_has_thermal(getattr(dot, "dot", dot)) for dot in dots)
        self._said_no_thermal = False

    def available(self) -> bool:
        return bool(self.dots)

    def has_thermal(self) -> bool:
        return self._thermal

    def _device(self, dot):
        return dot.dot if getattr(dot, "dot", None) is not None else dot

    def _each(self, action: str, fn) -> None:
        for dot in self.dots:
            device = self._device(dot)
            try:
                fn(device)
            except Exception as exc:
                print(f"rings  {self.side} addr {_address_of(dot)} {action} failed: {exc}", flush=True)

    async def _write(self, action: str, fn) -> None:
        async with self.io:
            await asyncio.to_thread(self._each, action, fn)

    async def manual(self, intensity: float, frequency: float) -> None:
        """Same calls as the scale loop in samples/read_write.py."""
        gain = clamp(intensity)
        hz = float(frequency)
        mode = self.vibration_mode.MANUAL

        def write(device) -> None:
            device.registers.set_vibration_mode(mode)
            device.play_frequency(hz, gain)

        await self._write("play_frequency", write)

    async def led(self, red: int, green: int, blue: int) -> None:
        red_i = max(0, min(255, int(red)))
        green_i = max(0, min(255, int(green)))
        blue_i = max(0, min(255, int(blue)))

        def write(device) -> None:
            # set_led(r, g, b) is LedMode.GLOBAL_MANUAL + the global color.
            device.set_led(red_i, green_i, blue_i)

        await self._write("set_led", write)

    async def led_off(self) -> None:
        def write(device) -> None:
            device.set_led_off()

        await self._write("set_led_off", write)

    async def heat(self, intensity: float) -> None:
        if not self._thermal:
            await self.thermal_off()
            return
        gain = max(-1.0, min(1.0, float(intensity)))

        def write(device) -> None:
            if dot_has_thermal(device):
                device.activate_thermal_intensity_control(gain)

        await self._write("activate_thermal_intensity_control", write)

    async def thermal_off(self) -> None:
        if not self._thermal:
            if not self._said_no_thermal:
                self._said_no_thermal = True
                print(f"rings  {self.side}  thermal API not on this dot, rumble only", flush=True)
            return

        def write(device) -> None:
            if hasattr(device, "disable_all_thermal"):
                device.disable_all_thermal()

        await self._write("disable_all_thermal", write)

    async def idle(self) -> None:
        """stop_vibration, set_led_off, disable_all_thermal. Vibration ends at intensity 0."""

        def write(device) -> None:
            steps = (
                ("stop_vibration", device.stop_vibration),
                ("set_led_off", device.set_led_off),
            )
            if hasattr(device, "disable_all_thermal"):
                steps = steps + (("disable_all_thermal", device.disable_all_thermal),)
            for label, action in steps:
                try:
                    action()
                except Exception as exc:
                    print(f"rings  {self.side} idle {label} failed: {exc}", flush=True)

        await self._write("idle", write)


_SDK = None


def load_sdk():
    """The import line from samples/read_write.py."""
    global _SDK
    if _SDK is not None:
        return _SDK
    try:
        from datafeel.device import (
            LedMode,
            ThermalMode,
            VibrationMode,
            VibrationWaveforms,
            discover_devices,
        )
    except ImportError as exc:
        raise SystemExit(
            "DataFeel SDK is not installed. Clone https://github.com/DataFeel/devkit-samples, "
            "cd into its Python folder (the one with setup.py), and run: pip install .\n"
            "macOS: pip3 install .    Windows: py -m pip install .\n"
            f"({exc})"
        ) from exc
    print(
        "rings  sdk  samples/read_write.py  "
        f"VibrationMode.MANUAL={int(VibrationMode.MANUAL)}  "
        f"LedMode.GLOBAL_MANUAL={int(LedMode.GLOBAL_MANUAL)}  "
        f"ThermalMode.MANUAL={int(ThermalMode.MANUAL)}  "
        f"VibrationWaveforms.STRONG_BUZZ_P100={int(VibrationWaveforms.STRONG_BUZZ_P100)}",
        flush=True,
    )
    _SDK = (discover_devices, VibrationMode)
    return _SDK


def scan_port(port_name: str, max_address: int) -> list[Discovered]:
    """Address walk from discover_devices, on one named port.

    discover_devices() itself returns after the first CP210x node. A second
    El Jefe needs this same loop: Dot(port, 1..max), print a miss, keep going.
    """
    from datafeel.device import Dot

    print("found DataFeel Device on port", port_name, flush=True)
    found: list[Discovered] = []
    for address in range(1, max_address + 1):
        try:
            dot = Dot(port_name, address)
        except Exception as exc:
            print(f"No device at address {address}", flush=True)
            text = str(exc).lower()
            if "could not open" in text or "no such file" in text or "filenotfound" in text or "file not found" in text:
                break
            continue
        found.append(Discovered(getattr(dot, "port", None) or port_name, dot.id, dot))
    return found


def scan_kit(ports: list[str], max_address: int, *, use_discover_devices: bool) -> list[Discovered]:
    """Prefer discover_devices() for the single-port case the sample uses."""
    if use_discover_devices:
        discover_devices, _mode = load_sdk()
        devices = discover_devices(max_address)
        if devices:
            return [Discovered(device.port, device.id, device) for device in devices]
        if not ports:
            return []
        print("rings  discover_devices found nothing, probing the matched port", flush=True)
    else:
        load_sdk()
    found: list[Discovered] = []
    for port in ports:
        found.extend(scan_port(port, max_address))
    return found


def outputs_from(left: list, right: list, vibration_mode) -> dict[str, Output]:
    locks: dict[str, asyncio.Lock] = {}

    def lock_for(port: str) -> asyncio.Lock:
        if port not in locks:
            locks[port] = asyncio.Lock()
        return locks[port]

    def one(side: str, group: list) -> HardwareOutput:
        port = _port_of(group[0]) if group else ""
        return HardwareOutput(side, port, group, lock_for(port or side), vibration_mode)

    return {"left": one("left", left), "right": one("right", right)}


def print_devices(dots: list[Discovered]) -> None:
    print("found", len(dots), "devices", flush=True)
    for dot in dots:
        print(dot, flush=True)


def print_mapping(note: str, outputs: dict[str, Output]) -> None:
    print("", flush=True)
    print(f"  {note}", flush=True)
    for side in ("left", "right"):
        chain = outputs[side]
        print(f"  {side.upper():5}  {chain.port or '(none)'}  dots={chain.describe()}", flush=True)
    print("  --swap exchanges LEFT and RIGHT. --left-addr / --right-addr pick addresses.", flush=True)
    print("", flush=True)


def connect_hardware(args, found_ports: list[str]) -> tuple[dict[str, Output], str]:
    ports = choose_ports(
        found_ports,
        port=args.port,
        left_port=args.left_port,
        right_port=args.right_port,
    )
    if len(found_ports) > 2 and not (args.port or args.left_port or args.right_port):
        print("rings  ignoring extra ports " + ", ".join(found_ports[2:]), flush=True)
    # One auto-detected port: call discover_devices() exactly like read_write.py.
    # An explicit --port, or a second El Jefe, is scanned with the same address loop.
    use_sdk = not (args.port or args.left_port or args.right_port) and len(ports) <= 1
    dots = scan_kit(ports, args.max_address, use_discover_devices=use_sdk)
    print_devices(dots)
    if not dots:
        raise SystemExit("discover_devices found no dots. Power on El Jefe and the dots, then try again.")
    left, right, note = assign_discovered(
        dots,
        ports=ports,
        left_addr=args.left_addr,
        right_addr=args.right_addr,
        swap=args.swap,
    )
    _discover, vibration_mode = load_sdk()
    return outputs_from(left, right, vibration_mode), note


def dry_run_outputs(args, found_ports: list[str]) -> tuple[dict[str, Output], str]:
    """Print the map without opening serial or calling discover_devices."""
    if args.port and (args.left_port or args.right_port):
        raise SystemExit("--port is one El Jefe. For two modules use --left-port and --right-port.")
    if args.left_addr is not None and args.right_addr is not None and args.left_addr == args.right_addr:
        raise SystemExit("--left-addr and --right-addr must differ")

    two_ports = bool(args.left_port or args.right_port) or (len(found_ports) >= 2 and not args.port)
    if two_ports:
        if args.left_port or args.right_port:
            ports = _explicit_pair(found_ports, args.left_port, args.right_port)
        else:
            ports = found_ports[:2]
        if args.swap:
            ports = [ports[1], ports[0]]
        note = f"dry-run, two ports, serial not opened; {ports[0]} is left, {ports[1]} is right"
        left_addrs = [args.left_addr] if args.left_addr is not None else []
        right_addrs = [args.right_addr] if args.right_addr is not None else []
        outputs = {
            "left": PrintOutput("left", ports[0], left_addrs, label="all"),
            "right": PrintOutput("right", ports[1], right_addrs, label="all"),
        }
        return outputs, note

    port = args.port or (found_ports[0] if found_ports else "(dry-run)")
    left_addrs = [args.left_addr] if args.left_addr is not None else []
    right_addrs = [args.right_addr] if args.right_addr is not None else []
    if args.swap and (left_addrs or right_addrs):
        left_addrs, right_addrs = right_addrs, left_addrs
    if left_addrs or right_addrs:
        note = f"dry-run address override on {port}, serial not opened"
        left_label, right_label = "none", "none"
    else:
        note = (
            f"dry-run, serial not opened. On hardware, discover_devices scans {port} "
            "and the lowest address that answers is LEFT, the next is RIGHT"
        )
        left_label, right_label = "lowest", "next"
        if args.swap:
            left_label, right_label = right_label, left_label
            note += " (--swap)"
    outputs = {
        "left": PrintOutput("left", port, left_addrs, label=left_label),
        "right": PrintOutput("right", port, right_addrs, label=right_label),
    }
    return outputs, note


async def show_led(chain: Output, red: int, green: int, blue: int, gain: float = 1.0) -> None:
    """Global LED at the capped brightness. All-zero is off."""
    color = dim_rgb(red, green, blue, chain.led_brightness, gain)
    if color == (0, 0, 0):
        await chain.led_off()
    else:
        await chain.led(*color)


async def pattern_turn_soon(chain: Output, intensity: float) -> None:
    """One short pulse. The same amber stays on as a slow steady glow."""
    await chain.thermal_off()
    await show_led(chain, *AMBER_SOFT, AMBER_SOFT_GAIN)
    await chain.manual(clamp(intensity), TURN_SOON_HZ)
    await asyncio.sleep(TURN_SOON_S)
    await chain.manual(0.0, TURN_SOON_HZ)
    await asyncio.sleep(max(0.0, TURN_SOON_GLOW_S - TURN_SOON_S))
    await chain.led_off()


async def pattern_turn_now(chain: Output, intensity: float) -> None:
    """Rapid pulses, one per amber blink, at the --led-brightness cap."""
    await chain.thermal_off()
    gain = clamp(intensity)
    half = 0.5 / TURN_NOW_BLINK_HZ
    for _ in range(TURN_NOW_BLINKS):
        await show_led(chain, *AMBER)
        await chain.manual(gain, TURN_NOW_HZ)
        await asyncio.sleep(half)
        await chain.led_off()
        await chain.manual(0.0, TURN_NOW_HZ)
        await asyncio.sleep(half)


async def pattern_car_behind(chains: list[Output], intensity: float) -> None:
    """Both wrists pulse on the same beat. Closer means a stronger, quicker pulse and a brighter dim red."""
    gain = clamp(intensity)
    active = [chain for chain in chains if chain is not None and chain.available()]
    if not active:
        return
    hz = BEHIND_HZ + gain * 40.0
    on = 0.06 + 0.08 * gain
    off = 0.12 - 0.06 * gain
    await asyncio.gather(*(chain.thermal_off() for chain in active))
    for _ in range(BEHIND_PULSES):
        await asyncio.gather(*(show_led(chain, 255, 0, 0, gain) for chain in active))
        await asyncio.gather(*(chain.manual(gain, hz) for chain in active))
        await asyncio.sleep(on)
        await asyncio.gather(*(chain.led_off() for chain in active))
        await asyncio.gather(*(chain.manual(0.0, hz) for chain in active))
        await asyncio.sleep(off)


async def pattern_car_approach(chain: Output, intensity: float) -> None:
    """Continuous rumble. Intensity and frequency follow `intensity`. Dim red LED."""
    gain = clamp(intensity)
    await chain.thermal_off()
    await show_led(chain, 255, 0, 0, gain)
    await chain.manual(gain, approach_frequency(gain))
    await asyncio.sleep(APPROACH_HOLD_S)
    await chain.led_off()


async def pattern_blue_shell(chain: Output, intensity: float) -> None:
    """Strong rapid rumble, a blue pulse on each tick, plus heat when the API exists."""
    gain = max(0.85, clamp(intensity))
    if chain.has_thermal():
        await chain.heat(shell_heat(intensity))
    else:
        await chain.thermal_off()
    for pulse in range(BLUE_SHELL_PULSES):
        hz = BLUE_SHELL_HZ if pulse % 2 == 0 else BLUE_SHELL_HZ + 10.0
        await show_led(chain, *BLUE_PULSE)
        await chain.manual(gain, hz)
        await asyncio.sleep(0.04)
        await chain.led_off()
        await chain.manual(0.0, BLUE_SHELL_HZ)
        await asyncio.sleep(0.03)


async def pattern_combo(chain: Output, intensity: float) -> None:
    """Quick triple tap. No LED — a success must not flash the dot."""
    gain = clamp(intensity)
    await chain.thermal_off()
    await chain.led_off()
    for _ in range(COMBO_TAPS):
        await chain.manual(gain, COMBO_HZ)
        await asyncio.sleep(0.045)
        await chain.manual(0.0, COMBO_HZ)
        await asyncio.sleep(0.035)


async def pattern_identify_left(chain: Output, _intensity: float) -> None:
    """Mapping check: one long pulse and a dim amber light on the left wrist."""
    await chain.thermal_off()
    await show_led(chain, *AMBER)
    await chain.manual(1.0, 140)
    await asyncio.sleep(0.40)
    await chain.led_off()


async def pattern_identify_right(chain: Output, _intensity: float) -> None:
    """Mapping check: two pulses and a dim blue flash on each, on the right wrist."""
    await chain.thermal_off()
    for _ in range(2):
        await show_led(chain, *BLUE_PULSE)
        await chain.manual(1.0, 180)
        await asyncio.sleep(0.16)
        await chain.led_off()
        await chain.manual(0.0, 180)
        await asyncio.sleep(0.10)


async def pattern_car_behind_one(chain: Output, intensity: float) -> None:
    """One wrist, same pulse as the paired cue, when the other wrist is busy."""
    await pattern_car_behind([chain], intensity)


PATTERN_FNS: dict[str, PatternFn] = {
    "turn_soon": pattern_turn_soon,
    "turn_now": pattern_turn_now,
    "car_approach": pattern_car_approach,
    "car_behind": pattern_car_behind_one,
    "blue_shell": pattern_blue_shell,
    "combo": pattern_combo,
}


class Player:
    """One task per wrist. A newer buzz cancels the older one on that wrist.

    Dots on the same El Jefe share the task, so a chain stays in sync.
    The finally block idles the wrist only when this command is still the
    latest one. An override leaves the motor where it is so the next
    command can take over without a gap, then that command idles at the end.
    """

    def __init__(self, outputs: dict[str, Output]):
        self.outputs = outputs
        self._locks = {"left": asyncio.Lock(), "right": asyncio.Lock()}
        self._tasks: dict[str, asyncio.Task | None] = {"left": None, "right": None}
        self._gen = {"left": 0, "right": 0}
        self._pattern = {"left": "", "right": ""}
        self._sync_task: asyncio.Task | None = None

    async def play(self, side: str, pattern: str, intensity: float) -> None:
        if pattern == "car_behind" and side == "both":
            await self._play_synced(pattern, intensity)
            return
        await self.start(side, pattern, PATTERN_FNS[pattern], intensity)

    async def _cancel_open(self) -> None:
        tasks = []
        seen: set[int] = set()
        for task in list(self._tasks.values()) + [self._sync_task]:
            if task is None or task.done() or id(task) in seen:
                continue
            seen.add(id(task))
            task.cancel()
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _play_synced(self, name: str, intensity: float) -> None:
        """One task drives both wrists so the pulses share a clock."""
        async with self._locks["left"]:
            async with self._locks["right"]:
                for side in ("left", "right"):
                    self._gen[side] += 1
                    self._pattern[side] = name
                gens = {side: self._gen[side] for side in ("left", "right")}
                await self._cancel_open()
                task = asyncio.create_task(
                    self._run_synced(name, intensity, gens),
                    name=f"buzz-both-{name}",
                )
                self._sync_task = task
                self._tasks["left"] = task
                self._tasks["right"] = task

    async def _run_synced(self, name: str, intensity: float, gens: dict[str, int]) -> None:
        print(f"buzz  both   {name:13}  {intensity:.2f}  synced", flush=True)
        chains = [self.outputs[side] for side in ("left", "right")]
        try:
            await pattern_car_behind(chains, intensity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"rings  both {name} failed: {exc.__class__.__name__}: {exc}", flush=True)
        finally:
            for side, gen in gens.items():
                if self._gen.get(side) != gen:
                    continue
                self._pattern[side] = ""
                chain = self.outputs.get(side)
                if chain is None or not chain.available():
                    continue
                try:
                    await chain.idle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"rings  {side} idle failed: {exc}", flush=True)

    async def start(self, side: str, name: str, fn: PatternFn, intensity: float) -> None:
        sides = ("left", "right") if side == "both" else (side,)
        await asyncio.gather(*(self._start_one(item, name, fn, intensity) for item in sides))

    async def _start_one(self, side: str, name: str, fn: PatternFn, intensity: float) -> None:
        chain = self.outputs.get(side)
        if chain is None or not chain.available():
            port = chain.port if chain is not None else "?"
            print(f"rings  {side}  no dots on {port}", flush=True)
            return
        async with self._locks[side]:
            self._gen[side] += 1
            gen = self._gen[side]
            previous = self._pattern.get(side) or ""
            if previous and previous != name:
                print(f"override {side:5}  {previous} -> {name}", flush=True)
            self._pattern[side] = name
            running = self._tasks.get(side)
            if running is not None and not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            self._tasks[side] = asyncio.create_task(
                self._run(chain, side, name, fn, intensity, gen),
                name=f"buzz-{side}-{name}",
            )

    async def _run(self, chain: Output, side: str, name: str, fn: PatternFn, intensity: float, gen: int) -> None:
        print(
            f"buzz  {side:5}  {name:13}  {intensity:.2f}  {chain.port}  dots={chain.describe()}",
            flush=True,
        )
        try:
            await fn(chain, intensity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"rings  {side} {name} failed: {exc.__class__.__name__}: {exc}", flush=True)
        finally:
            if self._gen.get(side) == gen:
                self._pattern[side] = ""
                try:
                    await chain.idle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"rings  {side} idle failed: {exc}", flush=True)

    async def wait(self, side: str | None = None) -> None:
        if side is None:
            tasks = list(self._tasks.values())
        else:
            tasks = [self._tasks.get(side)]
        pending = [task for task in tasks if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def stop_cars(self) -> None:
        """Cut a car warning now. Turn cues and the combo tap keep playing."""
        async with self._locks["left"]:
            async with self._locks["right"]:
                sides = [side for side in ("left", "right") if self._pattern.get(side) in CAR_PATTERNS]
                if not sides:
                    return
                tasks = []
                seen: set[int] = set()
                for side in sides:
                    self._gen[side] += 1
                    self._pattern[side] = ""
                    task = self._tasks.get(side)
                    if task is not None and not task.done() and id(task) not in seen:
                        seen.add(id(task))
                        task.cancel()
                        tasks.append(task)
                    self._tasks[side] = None
                if self._sync_task is not None and id(self._sync_task) in seen:
                    self._sync_task = None
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                for side in sides:
                    chain = self.outputs.get(side)
                    if chain is None or not chain.available():
                        continue
                    try:
                        await chain.idle()
                    except Exception as exc:
                        print(f"rings  {side} idle failed: {exc}", flush=True)
                print("rings  dismiss  car buzz off", flush=True)

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks.values() if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for chain in self.outputs.values():
            if not chain.available():
                continue
            try:
                await chain.idle()
            except Exception as exc:
                print(f"rings  {chain.side} idle failed: {exc}", flush=True)


def parse_buzz(msg: dict) -> tuple[str, str, float] | None:
    if not isinstance(msg, dict) or msg.get("type") != "buzz":
        return None
    side = msg.get("side")
    pattern = msg.get("pattern")
    if side not in SIDES or pattern not in PATTERNS:
        print(f"rings  ignore buzz {msg!r}", flush=True)
        return None
    try:
        intensity = round(clamp(float(msg.get("intensity", 0))), 2)
    except (TypeError, ValueError):
        print(f"rings  ignore buzz {msg!r}", flush=True)
        return None
    return side, pattern, intensity


async def run_socket(url: str, player: Player) -> None:
    from websockets.asyncio.client import connect

    while True:
        try:
            async with connect(url, open_timeout=2) as ws:
                print(f"rings  connected {url}", flush=True)
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(msg, dict) and msg.get("type") == "dismiss":
                        print("rings  dismiss", flush=True)
                        await player.stop_cars()
                        continue
                    parsed = parse_buzz(msg)
                    if parsed is None:
                        continue
                    side, pattern, intensity = parsed
                    await player.play(side, pattern, intensity)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"rings  {exc.__class__.__name__}: {exc} — retrying", flush=True)
            await asyncio.sleep(0.4)


async def run_test(player: Player) -> None:
    """Buzz LEFT, then RIGHT, so a wrong --swap is obvious before the catalog."""
    brightness = player.outputs["left"].led_brightness
    cap = dim_channel(255, brightness)
    print("TEST mapping: LEFT wrist first (one long pulse, dim amber), then RIGHT (two pulses, dim blue).", flush=True)
    print("TEST if those wrists are backwards, stop and rerun with --swap.", flush=True)
    print(
        f"TEST leds  brightness {brightness:.2f}  max channel {cap}/255. combo is a triple tap with no LED.",
        flush=True,
    )
    checks = (
        ("left", "identify one long pulse", pattern_identify_left),
        ("right", "identify two pulses", pattern_identify_right),
    )
    for side, label, fn in checks:
        chain = player.outputs[side]
        if not chain.available():
            print(f"TEST {side.upper()} no dot discovered", flush=True)
            continue
        print(f"TEST {side.upper()} {label}  {chain.port}  dots={chain.describe()}", flush=True)
        await player.start(side, "identify", fn, 1.0)
        await player.wait(side)
        await asyncio.sleep(0.35)
    for pattern in PATTERNS:
        intensity = TEST_INTENSITY[pattern]
        if pattern == "car_behind":
            print(
                f"TEST BOTH car_behind  {LED_NOTE[pattern]}  synced",
                flush=True,
            )
            await player.play("both", pattern, intensity)
            await player.wait()
            await asyncio.sleep(0.2)
            continue
        for side in ("left", "right"):
            chain = player.outputs[side]
            if not chain.available():
                print(f"TEST {side.upper()} {pattern}  no dot discovered", flush=True)
                continue
            print(
                f"TEST {side.upper()} {pattern}  {LED_NOTE[pattern]}  {chain.port}  dots={chain.describe()}",
                flush=True,
            )
            await player.play(side, pattern, intensity)
            await player.wait(side)
            await asyncio.sleep(0.2)
    left = player.outputs["left"]
    right = player.outputs["right"]
    print(f"TEST done. LEFT was {left.port}. RIGHT was {right.port}.", flush=True)


async def amain(args) -> None:
    found = detect_ports()
    if found:
        print("rings  ports  " + ", ".join(found), flush=True)
    if args.dry_run:
        outputs, note = dry_run_outputs(args, found)
        print("rings  dry-run: printing actuator commands, serial port stays closed", flush=True)
    else:
        outputs, note = connect_hardware(args, found)
    for chain in outputs.values():
        chain.led_brightness = args.led_brightness
    cap = dim_channel(255, args.led_brightness)
    print(f"rings  leds  brightness {args.led_brightness:.2f}  max channel {cap}/255", flush=True)
    print_mapping(note, outputs)
    player = Player(outputs)
    try:
        if args.test:
            print("rings  --test does not connect to the bridge", flush=True)
            await run_test(player)
        else:
            await run_socket(args.url, player)
    finally:
        await player.shutdown()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Haptic Ride DataFeel ring client")
    parser.add_argument("--url", default="ws://127.0.0.1:8765", help="bridge websocket")
    parser.add_argument("--dry-run", action="store_true", help="print actuator commands instead of using the dots")
    parser.add_argument("--test", action="store_true", help="buzz LEFT then RIGHT for each pattern, no bridge")
    parser.add_argument(
        "--led-brightness",
        type=float,
        default=DEFAULT_LED_BRIGHTNESS,
        help="scale every LED channel by this (0..1, default 0.15, about 40/255)",
    )
    parser.add_argument("--port", default="", help="scan this one El Jefe instead of every CP210x node")
    parser.add_argument("--left-port", default="", help="left-wrist El Jefe serial port")
    parser.add_argument("--right-port", default="", help="right-wrist El Jefe serial port")
    parser.add_argument("--swap", action="store_true", help="exchange LEFT and RIGHT after discovery")
    parser.add_argument("--left-addr", type=int, default=None, help="left dot address (overrides lowest-wins)")
    parser.add_argument("--right-addr", type=int, default=None, help="right dot address (overrides next-address)")
    parser.add_argument(
        "--max-address",
        type=int,
        default=4,
        help="discover_devices scan, addresses 1..N (samples/read_write.py uses 4)",
    )
    args = parser.parse_args(argv)
    if args.max_address < 1 or args.max_address > 247:
        raise SystemExit("--max-address must be 1..247")
    if not 0.0 <= args.led_brightness <= 1.0:
        raise SystemExit("--led-brightness must be 0..1")
    for flag, value in (("--left-addr", args.left_addr), ("--right-addr", args.right_addr)):
        if value is not None and not 1 <= value <= 247:
            raise SystemExit(f"{flag} must be 1..247")
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()

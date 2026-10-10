#!/usr/bin/env python3
"""Simulated radar + route. Same sockets the hardware bridge will use.

Edit NAV and CARS to restage the one-minute demo. Cars are kept on the
opposite side from the turn cue so one ring is never asked to be both a
navigation cue and a blind-spot warning.

  python bridge/fake_bridge.py
  python bridge/fake_bridge.py --record logs/session.jsonl
  python bridge/fake_bridge.py --replay examples/short.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import time

from hub import Hub, add_server_args, lan_ip, serve_overlay

LOOP_S = 56.0
TICK_S = 0.1

# (start_s, end_s, turn, in_m at start, in_m at end)
#   0–14s  left turn,  NOW at 10s
#  14–34s  right turn, SOON at 22.8s, NOW at 31s
#  34–56s  left turn,  SOON at 41s,   NOW at 52s
NAV = [
    (0.0, 14.0, "left", 42.0, 0.0),
    (14.0, 34.0, "right", 80.0, 0.0),
    (34.0, 56.0, "left", 66.0, 0.0),
]

# (spawn_s, side, dist_m, closing speed m/s). Speed >= 8 is a blue shell.
#   1.0s  right gentle, passes ~6.4s
#   4.0s  right gentle, passes ~12.6s (two dots overlap the first)
#  16.0s  left  blue shell, passes ~17.8s
#  20.0s  left  gentle, passes ~27.5s
#  36.5s  right blue shell, passes ~38.6s
#  44.0s  right gentle, passes ~52.5s
CARS = [
    (1.0, "right", 20.0, 3.6),
    (4.0, "right", 28.0, 3.2),
    (16.0, "left", 17.0, 9.2),
    (20.0, "left", 26.0, 3.4),
    (36.5, "right", 18.0, 8.6),
    (44.0, "right", 30.0, 3.5),
]


class Recorder:
    def __init__(self, path: str):
        self.f = open(path, "a", encoding="utf-8")
        self.t0 = time.monotonic()
        print(f"recording sensors → {path}", flush=True)

    def write(self, msg: dict) -> None:
        rec = {"t": round(time.monotonic() - self.t0, 3), "msg": msg}
        self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.f.flush()

    def close(self) -> None:
        self.f.close()


def nav_at(t: float, lap: int) -> dict | None:
    for index, (start, end, turn, m0, m1) in enumerate(NAV):
        if start <= t < end:
            span = end - start
            in_m = m0 + (m1 - m0) * ((t - start) / span)
            return {
                "type": "nav",
                "turn": turn,
                "in_m": round(max(0.0, in_m), 1),
                "id": f"n{lap}-{index}",
            }
    return None


def cars_at(t: float, lap: int) -> list[dict]:
    found = []
    for index, (spawn, side, dist0, speed) in enumerate(CARS):
        age = t - spawn
        if age < 0:
            continue
        dist = dist0 - speed * age
        if dist <= 0.4:
            continue
        found.append(
            {
                "type": "car",
                "side": side,
                "dist": round(dist, 1),
                "speed": speed,
                "id": f"c{lap}-{index}",
            }
        )
    return found


async def emit(hub: Hub, recorder: Recorder | None, msg: dict) -> None:
    await hub.broadcast(msg)
    if recorder is not None:
        recorder.write(msg)


def keep_simulated(value: str) -> str:
    """The scripted bridge has no radar. A Real request stays on the demo."""
    if value == "real":
        print("mode   sim  (this bridge stays on the scripted route)", flush=True)
    return "sim"


async def simulate(hub: Hub, recorder: Recorder | None, tick: float, emit_cars=None) -> None:
    """Nav cues every tick. Scripted cars too, unless emit_cars() is false.

    The hardware bridge reuses this so the demo route can replace radar
    cars without a restart, while the turn cues keep running either way.
    """
    t0 = time.monotonic()
    lap_seen = -1
    announced: set[str] = set()
    while True:
        elapsed = time.monotonic() - t0
        lap = int(elapsed // LOOP_S)
        t = elapsed % LOOP_S
        if lap != lap_seen:
            lap_seen = lap
            announced = set()
            if lap:
                print(f"loop  {lap}", flush=True)
        nav = nav_at(t, lap)
        if nav is not None:
            if nav["id"] not in announced:
                announced.add(nav["id"])
                print(f"nav   {nav['turn']:5}  in {nav['in_m']:.0f} m", flush=True)
            await emit(hub, recorder, nav)
        if emit_cars is None or emit_cars():
            for car in cars_at(t, lap):
                if car["id"] not in announced:
                    announced.add(car["id"])
                    kind = "BLUE" if car["speed"] >= 8 else "car"
                    print(
                        f"{kind:5} {car['side']:5}  {car['dist']:.0f} m   {car['speed']} m/s",
                        flush=True,
                    )
                await emit(hub, recorder, car)
        await asyncio.sleep(tick)


def load_replay(path: str) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                events.append({"t": float(rec["t"]), "msg": rec["msg"]})
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                print(f"replay skip line {line_no}: {exc}", flush=True)
    if not events:
        raise SystemExit(f"nothing to replay in {path}")
    events.sort(key=lambda rec: rec["t"])
    return events


async def replay(path: str, hub: Hub) -> None:
    events = load_replay(path)
    print(f"replaying {len(events)} sensor events from {path}", flush=True)
    while True:
        start = time.monotonic()
        for rec in events:
            delay = rec["t"] - (time.monotonic() - start)
            if delay > 0:
                await asyncio.sleep(delay)
            await hub.broadcast(rec["msg"])
        await asyncio.sleep(1.0)


def banner(args) -> None:
    ip = lan_ip()
    print("", flush=True)
    print("  haptic-ride  fake bridge", flush=True)
    print(f"  overlay     http://127.0.0.1:{args.http_port}", flush=True)
    if ip != "127.0.0.1":
        print(f"  phones      http://{ip}:{args.http_port}/?ring=left", flush=True)
        print(f"              http://{ip}:{args.http_port}/?ring=right", flush=True)
    print(f"  websocket   ws://{ip}:{args.port}", flush=True)
    print("  keys        A / D shoulder check     ← / → turn", flush=True)
    print("", flush=True)


async def amain(args) -> None:
    hub = Hub(mode="fake", source="sim")
    hub.on_mode = keep_simulated
    recorder = Recorder(args.record) if args.record else None
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
            task = asyncio.create_task(
                replay(args.replay, hub) if args.replay else simulate(hub, recorder, args.tick)
            )
            await stop.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    except OSError as exc:
        raise SystemExit(f"websocket port {args.port} is already in use ({exc})") from exc
    finally:
        if recorder is not None:
            recorder.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Haptic Ride fake sensor bridge")
    add_server_args(parser)
    parser.add_argument("--record", default="", help="append sensor events as JSONL")
    parser.add_argument("--replay", default="", help="play a JSONL sensor log instead of the sim")
    parser.add_argument("--tick", type=float, default=TICK_S, help="sim step, seconds")
    args = parser.parse_args()
    if args.record and args.replay:
        raise SystemExit("pick one of --record or --replay")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Headless check: scoring rules, then bridge → engine → buzz → points.

    python tests/test_e2e.py
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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)


def load_game():
    spec = importlib.util.spec_from_file_location("haptic_game", ROOT / "engine" / "game.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_rules(game_mod) -> None:
    assert game_mod.car_intensity(8) > game_mod.car_intensity(18) > 0
    assert game_mod.car_intensity(40) == 0
    assert 0 < game_mod.car_intensity(25) <= 1
    assert game_mod.nav_cue(60) is None
    assert game_mod.nav_cue(30)[0] == "turn_soon"
    assert game_mod.nav_cue(10)[0] == "turn_now"
    assert game_mod.nav_cue(10)[1] < game_mod.nav_cue(2)[1]

    game = game_mod.Game()
    game.now = 10
    game.on_nav({"type": "nav", "turn": "left", "in_m": 30, "id": "n"})
    assert game.on_turn("left") is None  # too early
    assert game.score == 0

    game.on_car({"type": "car", "side": "right", "dist": 18, "speed": 3, "id": "a"})
    game.on_car({"type": "car", "side": "right", "dist": 24, "speed": 9, "id": "b"})
    game.on_car({"type": "car", "side": "left", "dist": 10, "speed": 3, "id": "c"})
    rings = game.rings()
    assert rings["right"]["pattern"] == "car_approach"  # nearer car owns the ring
    assert rings["left"]["pattern"] == "car_approach"  # car outranks the left turn
    pts = game.on_check("right")
    assert pts == game_mod.BASE_DODGE  # blue shell on that side pays more
    assert game.combo == 1
    second = game.on_check("right")  # slower car, now at 1.5×
    assert second == int(game_mod.BASE_ITEM * 1.5)
    assert game.score == game_mod.BASE_DODGE + second

    game.nav = {"id": "n", "turn": "left", "in_m": 8}
    assert game.on_turn("right") is None
    assert game.combo == 0  # wrong way breaks the chain
    drift = game.on_turn("left")
    assert drift == game_mod.BASE_DRIFT
    assert game.drifts == 1

    missed = game_mod.Game()
    missed.now = 10
    missed.combo = 4
    missed.on_car({"type": "car", "side": "left", "dist": 12, "speed": 9, "id": "shell"})
    missed.now = 10 + game_mod.CAR_GONE_S + 0.2
    missed.sweep()
    assert missed.misses == 1
    assert missed.combo == 0
    assert missed.flash["text"] == "SHELL HIT"

    missed.combo = 3
    missed.now = 20
    missed.nav = {"id": "open-turn", "turn": "left", "in_m": 4}
    missed.on_nav({"type": "nav", "turn": "right", "in_m": 40, "id": "next"})
    assert missed.misses == 2
    assert missed.combo == 0

    behind = game_mod.Game()
    behind.now = 5
    behind.on_nav({"type": "nav", "turn": "left", "in_m": 20, "id": "n"})
    behind.on_car({"type": "car", "side": "behind", "dist": 8, "speed": 3, "id": "back"})
    rings = behind.rings()
    assert rings["left"]["pattern"] == "car_behind" and rings["right"]["pattern"] == "car_behind"
    assert rings["left"]["intensity"] > behind_far_intensity(game_mod)
    assert game_mod.buzz_plan(rings) == [("both", rings["left"])]
    assert behind.on_check("left") == game_mod.BASE_ITEM

    other = game_mod.Game()
    other.now = 1
    other.on_car({"type": "car", "side": "behind", "dist": 9, "speed": 3, "id": "b"})
    assert other.on_check("right") == game_mod.BASE_ITEM

    look = game_mod.Game()
    look.now = 1
    look.on_car({"type": "car", "side": "center", "dist": 6, "speed": 9, "id": "b"})
    assert look.cars["b"]["side"] == "behind"
    assert game_mod.handle(look, {"type": "look_back"}) == game_mod.BASE_DODGE

    mixed = game_mod.Game()
    mixed.now = 1
    mixed.on_car({"type": "car", "side": "left", "dist": 12, "speed": 3, "id": "L"})
    mixed.on_car({"type": "car", "side": "behind", "dist": 8, "speed": 3, "id": "B"})
    mixed_rings = mixed.rings()
    assert mixed_rings["left"]["pattern"] == "car_approach"
    assert mixed_rings["right"]["pattern"] == "car_behind"
    assert [side for side, _cmd in game_mod.buzz_plan(mixed_rings)] == ["left", "right"]
    assert mixed.on_check("left") == game_mod.BASE_ITEM
    assert mixed.cars["B"]["scored"] is False
    assert mixed.on_check("right") == int(game_mod.BASE_ITEM * 1.5)
    print("ok  scoring rules")


def behind_far_intensity(game_mod) -> float:
    far = game_mod.Game()
    far.on_car({"type": "car", "side": "behind", "dist": 20, "speed": 3, "id": "back"})
    return far.rings()["left"]["intensity"]


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


async def wait_for(ws, predicate, timeout: float):
    from websockets.asyncio.client import connect

    deadline = time.monotonic() + timeout
    seen = []
    async with connect(ws) as sock:
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(sock.recv(), timeout=timeout)
            msg = json.loads(raw)
            seen.append(msg)
            got = predicate(msg, seen, sock)
            if asyncio.iscoroutine(got):
                got = await got
            if got:
                return seen
    raise AssertionError("timed out, last messages: " + json.dumps(seen[-4:]))


async def score_against_replay(port: int) -> None:
    url = f"ws://127.0.0.1:{port}"

    async def drive(msg, seen, sock) -> bool:
        if msg.get("type") != "state":
            return False
        for car in msg.get("cars") or []:
            key = ("check", car["id"])
            if car["dist"] <= 25 and key not in drive.done:
                drive.done.add(key)
                await sock.send(json.dumps({"type": "shoulder_check", "side": car["side"]}))
        if msg.get("need_turn") and "turn" not in drive.done:
            drive.done.add("turn")
            await sock.send(json.dumps({"type": "turn", "turn": msg["nav"]["turn"]}))
        stats = msg.get("stats") or {}
        return stats.get("items", 0) >= 1 and stats.get("dodges", 0) >= 1 and stats.get("drifts", 0) >= 1

    drive.done = set()
    seen = await wait_for(url, drive, 12)
    buzzes = [msg for msg in seen if msg.get("type") == "buzz"]
    assert buzzes, "game never sent a buzz"
    # Unbroken chain: item 100, drift 225, dodge 500.
    assert seen[-1]["type"] == "state"
    assert seen[-1]["score"] == 825
    for buzz in buzzes:
        assert buzz["pattern"] in {"turn_soon", "turn_now", "car_approach", "car_behind", "blue_shell", "combo"}
        assert 0 <= float(buzz["intensity"]) <= 1
    print("ok  replay scores item, dodge, and drift")


async def live_sim(ws_port: int, http_port: int) -> None:
    page = urllib.request.urlopen(f"http://127.0.0.1:{http_port}/", timeout=2).read().decode()
    assert "HAPTIC RIDE" in page
    assert "navigator.vibrate" in page
    assert "mode-sim" in page and "Simulated" in page and "Real" in page
    assert "look back" in page and "k-look-back" in page
    assert "BEHIND" in page and "car_behind" in page
    assert "__WS_PORT__" not in page
    assert f'"{ws_port}"' in page

    def ready(msg, seen, sock) -> bool:
        return msg.get("type") == "car" and msg.get("side") == "right"

    seen = await wait_for(f"ws://127.0.0.1:{ws_port}", ready, 8)
    assert any(msg.get("type") == "hello" and msg.get("mode") == "fake" and msg.get("source") == "sim" for msg in seen)
    assert any(msg.get("type") == "nav" for msg in seen)

    from websockets.asyncio.client import connect

    async with connect(f"ws://127.0.0.1:{ws_port}") as sock:
        hello = json.loads(await asyncio.wait_for(sock.recv(), timeout=3))
        assert hello["source"] == "sim"
        await sock.send(json.dumps({"type": "mode", "value": "real"}))
        deadline = time.monotonic() + 3
        echoed = None
        while time.monotonic() < deadline and echoed is None:
            msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=3))
            if msg.get("type") == "mode":
                echoed = msg
        assert echoed == {"type": "mode", "value": "sim"}
    print("ok  live sim broadcasts cars and serves the overlay")


async def _until(sock, predicate, timeout: float):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=timeout))
        last = msg
        if predicate(msg):
            return msg
    raise AssertionError("timed out, last message: " + json.dumps(last))


async def hardware_stub(port: int) -> None:
    from websockets.asyncio.client import connect

    async with connect(f"ws://127.0.0.1:{port}") as sock:
        hello = json.loads(await asyncio.wait_for(sock.recv(), timeout=3))
        assert hello["mode"] == "real"
        assert hello["source"] == "real"
        await _until(sock, lambda msg: msg.get("type") == "nav", 2)
        try:
            raw = await asyncio.wait_for(sock.recv(), timeout=0.4)
        except asyncio.TimeoutError:
            raw = None
        assert raw is None or json.loads(raw).get("type") != "car"

        await sock.send(json.dumps({"type": "mode", "value": "sim"}))
        await _until(sock, lambda msg: msg.get("type") == "mode" and msg.get("value") == "sim", 2)
        car = await _until(sock, lambda msg: msg.get("type") == "car", 8)
        assert str(car["id"]).startswith("c")
        await _until(sock, lambda msg: msg.get("type") == "nav", 2)

        await sock.send(json.dumps({"type": "mode", "value": "real"}))
        await _until(sock, lambda msg: msg.get("type") == "mode" and msg.get("value") == "real", 2)
        drain_until = time.monotonic() + 0.35
        while time.monotonic() < drain_until:
            try:
                await asyncio.wait_for(sock.recv(), timeout=drain_until - time.monotonic())
            except asyncio.TimeoutError:
                break
        saw_nav = False
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=1.2))
            assert msg.get("type") != "car", msg
            if msg.get("type") == "nav":
                saw_nav = True
        assert saw_nav
    print("ok  hardware stub stays quiet when the radar SDK is absent")


def main() -> None:
    game_mod = load_game()
    check_rules(game_mod)
    py = sys.executable
    procs: list[Proc] = []
    try:
        replay = Proc(
            [
                py, "bridge/fake_bridge.py",
                "--host", "127.0.0.1", "--port", "8771", "--http-port", "8651",
                "--replay", "examples/short.jsonl",
            ]
        )
        procs.append(replay)
        engine = Proc([py, "engine/game.py", "--url", "ws://127.0.0.1:8771"])
        procs.append(engine)
        time.sleep(0.6)
        asyncio.run(score_against_replay(8771))

        live = Proc(
            [
                py, "bridge/fake_bridge.py",
                "--host", "127.0.0.1", "--port", "8772", "--http-port", "8652",
            ]
        )
        procs.append(live)
        time.sleep(0.5)
        asyncio.run(live_sim(8772, 8652))

        record = ROOT / "logs" / "test-record.jsonl"
        record.unlink(missing_ok=True)
        rec = Proc(
            [
                py, "bridge/fake_bridge.py",
                "--host", "127.0.0.1", "--port", "8774", "--http-port", "8654",
                "--record", str(record),
            ]
        )
        procs.append(rec)
        time.sleep(1.3)
        rec.stop()
        lines = [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
        assert any(item["msg"]["type"] == "nav" for item in lines)
        assert all("t" in item and "msg" in item for item in lines)
        record.unlink()
        print("ok  sensor log records JSONL")

        stub = Proc(
            [
                py, "bridge/real_bridge_stub.py",
                "--host", "127.0.0.1", "--port", "8773", "--http-port", "8653",
            ]
        )
        procs.append(stub)
        time.sleep(0.4)
        asyncio.run(hardware_stub(8773))
        text = "".join(stub.lines)
        assert "read_radar" in text
        assert "datafeel_rings" in text
    except Exception:
        for proc in procs:
            print("--- process output ---")
            print("".join(proc.lines[-40:]))
        raise
    finally:
        for proc in procs:
            proc.stop()
    print("ok  end to end")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Camera bridge geometry and hub, with no webcam and no YOLO weights.

    python tests/test_camera_bridge.py
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


def load_camera():
    bridge = ROOT / "bridge"
    if str(bridge) not in sys.path:
        sys.path.insert(0, str(bridge))
    spec = importlib.util.spec_from_file_location("camera_bridge", bridge / "camera_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_geometry(cam) -> None:
    assert abs(cam.distance_m(0.2, 3.2) - 16.0) < 1e-9
    assert cam.distance_m(0.0, 3.2) == float("inf")
    # A taller box is closer, so the closing speed is positive.
    assert abs(cam.closing_mps(16.0, 12.8, 0.5) - 6.4) < 1e-9
    left_edge, right_edge = cam.zone_edges(cam.CENTER_WIDTH)
    assert cam.CENTER_WIDTH == 0.34
    assert abs(left_edge - 0.33) < 1e-9 and abs(right_edge - 0.67) < 1e-9
    assert cam.side_of(0.1, left_edge, right_edge) == "left"
    assert cam.side_of(0.5, left_edge, right_edge) == "behind"
    assert cam.side_of(0.9, left_edge, right_edge) == "right"
    narrow_l, narrow_r = cam.zone_edges(0.10)
    assert cam.side_of(0.50, narrow_l, narrow_r) == "behind"
    assert cam.side_of(0.40, narrow_l, narrow_r) == "left"
    cal = cam.parse_cal("car=4,person=3.5")
    assert cal["car"] == 4 and cal["person"] == 3.5 and cal["bus"] == cam.DEFAULT_CAL["bus"]
    try:
        cam.parse_cal("plane=1")
    except SystemExit:
        pass
    else:
        raise AssertionError("unknown class should fail")
    args = cam.build_parser().parse_args([])
    assert args.camera == 0 and args.port == 8765 and args.http_port == 8641
    assert args.mirror is True and args.persist == 3 and args.center_width == 0.34
    assert args.preview is False
    assert cam.build_parser().parse_args(["--no-mirror"]).mirror is False
    print("ok  distance, thirds, and --mirror default")


def check_tracks(cam) -> None:
    cal = dict(cam.DEFAULT_CAL)
    tracker = cam.SceneTracker(cal=cal, persist=3, min_approach=0.4, near_m=6.0, max_dist=30.0)
    dt = 0.1
    right = []
    left = []
    behind = []
    for frame in range(30):
        messages = tracker.update(cam.synthetic_boxes(frame, cal), frame * dt)
        assert all(msg["side"] in ("left", "behind", "right") for msg in messages)
        assert all(msg["type"] == "car" and msg["id"] == f"cam-{msg['side']}" for msg in messages)
        assert all(msg["class"] == "car" for msg in messages)
        for msg in messages:
            if msg["side"] == "right":
                right.append((frame, msg))
            elif msg["side"] == "behind":
                behind.append((frame, msg))
            else:
                left.append((frame, msg))
        if frame < 3:
            assert messages == []
    assert behind and behind[0][0] == 3
    assert all(msg["speed"] >= 2.5 for _frame, msg in behind)
    assert all(behind[i][1]["dist"] > behind[i + 1][1]["dist"] for i in range(len(behind) - 1))
    assert all(item[0] < 20 for item in behind)
    assert right and right[0][0] == 3
    assert all(msg["speed"] >= 3.5 for _frame, msg in right)
    assert all(right[i][1]["dist"] > right[i + 1][1]["dist"] for i in range(len(right) - 1))
    assert all(item[0] < 20 for item in right)
    assert left and left[0][0] == 22
    assert all(msg["dist"] <= 6.0 for _frame, msg in left)
    assert all(msg["dist"] < 20 for _frame, msg in left + right + behind)

    # One-frame growth is not a car.
    spike = cam.SceneTracker(cal=cal, persist=3)
    quiet = [cam.box_at("car", 0.8, cal["car"] / 18.0)]
    burst = [cam.box_at("car", 0.8, cal["car"] / 8.0)]
    assert spike.update(quiet, 0.0) == []
    assert spike.update(burst, 0.1) == []
    assert spike.update(quiet, 0.2) == []
    assert spike.update(quiet, 0.3) == []

    # Two cars on the right: the nearer one wins. Within 0.5 m, the faster one wins.
    near = cam.SceneTracker(cal=cal, persist=1, near_m=30.0, min_approach=0.0)
    far_box = cam.box_at("car", 0.78, cal["car"] / 12.0)
    close_box = cam.box_at("person", 0.9, cal["person"] / 5.0)
    messages = near.update([far_box, close_box], 0.0)
    assert [msg["dist"] for msg in messages] == [5.0]
    assert messages[0]["class"] == "person"

    race = cam.SceneTracker(cal=cal, persist=1, near_m=30.0, min_approach=0.0)
    race.update(
        [cam.box_at("car", 0.75, cal["car"] / 10.0), cam.box_at("bus", 0.9, cal["bus"] / 10.2)],
        0.0,
    )
    messages = race.update(
        [cam.box_at("car", 0.75, cal["car"] / 9.7), cam.box_at("bus", 0.9, cal["bus"] / 10.0)],
        0.1,
    )
    assert len(messages) == 1 and messages[0]["side"] == "right"
    assert messages[0]["class"] in ("car", "bus")
    assert messages[0]["speed"] > 1.0
    print("ok  persistence, nearest side, and a one-frame spike stays quiet")


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


def _scripted(msg: dict) -> bool:
    cid = str(msg.get("id", ""))
    return cid.startswith("c") and not cid.startswith("cam-")


async def check_socket() -> None:
    from websockets.asyncio.client import connect

    url = "ws://127.0.0.1:8783"
    cars = []
    saw_nav = False
    deadline = time.monotonic() + 4
    async with connect(url) as sock:
        hello = json.loads(await asyncio.wait_for(sock.recv(), timeout=3))
        assert hello["type"] == "hello" and hello["source"] == "real" and hello["mode"] == "synthetic"
        while time.monotonic() < deadline and (len(cars) < 4 or not saw_nav):
            msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=4))
            if msg.get("type") == "nav":
                saw_nav = True
            if msg.get("type") == "car" and str(msg.get("id", "")).startswith("cam-"):
                cars.append(msg)
    assert saw_nav
    assert len(cars) >= 3
    assert all(car["side"] in ("left", "behind", "right") for car in cars)
    assert any(car["side"] == "behind" and car["speed"] >= 0.4 for car in cars)
    assert all(car["speed"] >= 0 for car in cars)
    rights = [car for car in cars if car["side"] == "right"]
    assert any(car["speed"] >= 0.4 for car in rights)
    closing = any(
        rights[j]["dist"] < rights[i]["dist"] - 0.05
        for i in range(len(rights))
        for j in range(i + 1, len(rights))
    )
    assert closing

    async with connect(url) as sock:
        hello = json.loads(await asyncio.wait_for(sock.recv(), timeout=3))
        assert hello["source"] == "real"
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
            if not saw_mode or time.monotonic() < settle:
                continue
            if msg.get("type") == "nav":
                saw_nav = True
            if msg.get("type") == "car" and _scripted(msg):
                sim_cars.append(msg)
            if msg.get("type") == "car" and str(msg.get("id", "")).startswith("cam-"):
                raise AssertionError(msg)
            if saw_nav and sim_cars:
                break
        assert saw_mode and saw_nav and sim_cars

        await sock.send(json.dumps({"type": "mode", "value": "real"}))
        saw_real = False
        saw_nav = False
        camera_back = False
        settle = 0.0
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not (saw_real and saw_nav and camera_back and time.monotonic() >= settle):
            msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=4))
            if msg.get("type") == "mode" and msg.get("value") == "real":
                saw_real = True
                settle = time.monotonic() + 0.4
                continue
            if not saw_real or time.monotonic() < settle:
                continue
            if msg.get("type") == "nav":
                saw_nav = True
            if msg.get("type") == "car" and _scripted(msg):
                raise AssertionError(msg)
            if msg.get("type") == "car" and str(msg.get("id", "")).startswith("cam-"):
                camera_back = True
        assert saw_real and saw_nav and camera_back
    print("ok  --synthetic publishes cars, nav, and the Simulated/Real toggle")


def check_stream() -> None:
    import socket
    import urllib.request

    import hub

    hub.FEED.publish(b"\xff\xd8\xff\xd9")
    hub.serve_overlay("127.0.0.1", 8664, 8799)
    time.sleep(0.15)
    sock = socket.create_connection(("127.0.0.1", 8664), 2)
    sock.settimeout(2)
    sock.sendall(b"GET /camera.mjpg HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
    buf = b""
    while b"\xff\xd8" not in buf and len(buf) < 8192:
        chunk = sock.recv(1024)
        if not chunk:
            break
        buf += chunk
    sock.close()
    assert b"multipart/x-mixed-replace" in buf, buf[:200]
    assert b"\xff\xd8" in buf
    page = urllib.request.urlopen("http://127.0.0.1:8664/", timeout=2).read()
    assert b"hide camera" in page and b"REAR VIEW" in page and b"dismiss" in page
    print("ok  /camera.mjpg and the rear-view page")


def main() -> None:
    cam = load_camera()
    check_geometry(cam)
    check_tracks(cam)
    check_stream()
    proc = Proc([
        sys.executable,
        "bridge/camera_bridge.py",
        "--host", "127.0.0.1",
        "--port", "8783",
        "--http-port", "8663",
        "--synthetic",
        "--fps", "30",
    ])
    try:
        time.sleep(0.4)
        asyncio.run(check_socket())
    except Exception:
        print("".join(proc.lines[-40:]))
        raise
    finally:
        proc.stop()
    print("ok  camera bridge")


if __name__ == "__main__":
    main()

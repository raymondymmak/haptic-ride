#!/usr/bin/env python3
"""Camera bridge for the Mac when the radar is unusable indoors.

A backward-facing webcam or Continuity Camera feeds a small YOLO model.
The nearest closing object on each side becomes the same car event the
radar publishes, and this process hosts the same hub (websocket 8765,
overlay 8641). Nav cues and the Simulated / Real toggle behave like
bridge/real_bridge_stub.py: Real is the camera, Simulated is the scripted
route, and turn cues run in both.

    python3 bridge/camera_bridge.py --camera 0 --preview
    python3 bridge/camera_bridge.py --list-cameras
    python3 bridge/camera_bridge.py --synthetic

Distance is cal[class] / (bbox height / frame height). --cal replaces any
class constant. Speed is how fast that distance is falling, in m/s.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

BRIDGE_DIR = Path(__file__).resolve().parent
if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

from fake_bridge import TICK_S, simulate
from hub import Hub, add_server_args, lan_ip, serve_overlay

# COCO ids on yolov8n / yolo11n. Anything else is ignored.
CLASS_IDS = {
    "person": 0,
    "bicycle": 1,
    "car": 2,
    "motorcycle": 3,
    "bus": 5,
    "truck": 7,
}

# Meters when the box is the full frame height. Tuned for a ~50° vertical
# webcam: real object height × 1/(2·tan(fov/2)).
DEFAULT_CAL = {
    "person": 3.6,
    "bicycle": 2.4,
    "motorcycle": 2.6,
    "car": 3.2,
    "bus": 6.4,
    "truck": 6.0,
}

PERSIST_FRAMES = 3
MIN_APPROACH_MPS = 0.4
NEAR_M = 6.0
MAX_DIST_M = 30.0
SPEED_CAP = 20.0
MIN_IOU = 0.15
MAX_MISSED = 2
DEFAULT_FPS = 10
# Share of the frame that counts as directly behind the rider, centered.
CENTER_WIDTH = 0.34
WINDOW = "Haptic Ride camera"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def distance_m(height: float, cal: float) -> float:
    """Meters from a normalized bbox height. Taller box, closer object."""
    if height <= 1e-4 or cal <= 0:
        return float("inf")
    return cal / height


def closing_mps(prev_dist: float, dist: float, dt: float) -> float:
    """Positive when the box grew and the object got closer."""
    if dt <= 1e-4:
        return 0.0
    return clamp((prev_dist - dist) / dt, -SPEED_CAP, SPEED_CAP)


def zone_edges(center_width: float) -> tuple[float, float]:
    """Left and right edges of the behind band. The band is centered in the frame."""
    half = clamp(center_width, 0.05, 0.9) / 2.0
    return 0.5 - half, 0.5 + half


def side_of(cx: float, left_max: float, right_min: float) -> str:
    """Rider space. cx is 0 on the rider's left after --mirror. The middle band is behind."""
    if cx < left_max:
        return "left"
    if cx > right_min:
        return "right"
    return "behind"


def iou(a, b) -> float:
    x1 = max(a.x1, b.x1)
    y1 = max(a.y1, b.y1)
    x2 = min(a.x2, b.x2)
    y2 = min(a.y2, b.y2)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    area_a = max(0.0, a.x2 - a.x1) * max(0.0, a.y2 - a.y1)
    area_b = max(0.0, b.x2 - b.x1) * max(0.0, b.y2 - b.y1)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def parse_cal(text: str) -> dict[str, float]:
    """`--cal car=4,person=3.5` overrides those classes and keeps the rest."""
    cal = dict(DEFAULT_CAL)
    if not text or not str(text).strip():
        return cal
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise SystemExit(f"--cal expects class=meters, got {part!r}")
        name, raw = part.split("=", 1)
        name = name.strip().lower()
        if name not in DEFAULT_CAL:
            known = ", ".join(DEFAULT_CAL)
            raise SystemExit(f"--cal unknown class {name!r} (use {known})")
        try:
            value = float(raw)
        except ValueError as exc:
            raise SystemExit(f"--cal {name} is not a number") from exc
        if value <= 0:
            raise SystemExit("--cal values must be positive")
        cal[name] = value
    return cal


class Box:
    def __init__(self, cls: str, x1: float, y1: float, x2: float, y2: float, conf: float = 1.0):
        self.cls = cls
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.conf = conf

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2.0

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)


def box_at(cls: str, cx: float, height: float) -> Box:
    """A box centered at cx with a normalized height. Used by --synthetic and tests."""
    height = clamp(height, 0.02, 0.95)
    width = clamp(height * 0.55, 0.04, 0.55)
    x1 = clamp(cx - width / 2.0, 0.0, 0.98)
    x2 = clamp(cx + width / 2.0, x1 + 0.01, 1.0)
    y1 = clamp(0.5 - height / 2.0, 0.0, 0.98)
    y2 = clamp(0.5 + height / 2.0, y1 + 0.01, 1.0)
    return Box(cls, x1, y1, x2, y2)


class Track:
    def __init__(self, tid: int, box: Box, dist: float, t: float, side: str):
        self.tid = tid
        self.cls = box.cls
        self.x1 = box.x1
        self.y1 = box.y1
        self.x2 = box.x2
        self.y2 = box.y2
        self.dist = dist
        self.speed = 0.0
        self.t = t
        self.side = side
        self.hits = 1
        self.approach_hits = 0
        self.missed = 0
        self.emitting = False

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2.0

    @property
    def height(self) -> float:
        return max(0.0, self.y2 - self.y1)

    def follow(self, box: Box) -> None:
        self.x1 = box.x1
        self.y1 = box.y1
        self.x2 = box.x2
        self.y2 = box.y2
        self.cls = box.cls


class SceneTracker:
    """Keep tracks, then publish the nearest car on the left, behind, and right.

    A track is published after `persist` consecutive frames in which it is
    closing at `min_approach` or already inside `near_m`. The middle band is
    `side: "behind"`.
    """

    def __init__(
        self,
        cal: dict[str, float] | None = None,
        left_max: float | None = None,
        right_min: float | None = None,
        persist: int = PERSIST_FRAMES,
        min_approach: float = MIN_APPROACH_MPS,
        near_m: float = NEAR_M,
        max_dist: float = MAX_DIST_M,
        min_iou: float = MIN_IOU,
    ):
        self.cal = dict(DEFAULT_CAL if cal is None else cal)
        if left_max is None or right_min is None:
            left_max, right_min = zone_edges(CENTER_WIDTH)
        self.left_max = left_max
        self.right_min = right_min
        self.persist = max(1, int(persist))
        self.min_approach = min_approach
        self.near_m = near_m
        self.max_dist = max_dist
        self.min_iou = min_iou
        self.tracks: list[Track] = []
        self._next = 1

    def update(self, boxes: list[Box], t: float) -> list[dict]:
        matched, used_t, used_b = self._match(boxes)
        for index, track in enumerate(self.tracks):
            if index in used_t:
                continue
            track.missed += 1
            track.approach_hits = 0
            track.emitting = False
        for ti, bi in matched:
            self._advance(self.tracks[ti], boxes[bi], t)
        for bi, box in enumerate(boxes):
            if bi in used_b:
                continue
            self._spawn(box, t)
        self.tracks = [track for track in self.tracks if track.missed <= MAX_MISSED]
        return self._choose()

    def _match(self, boxes: list[Box]) -> tuple[list[tuple[int, int]], set[int], set[int]]:
        pairs = []
        for ti, track in enumerate(self.tracks):
            for bi, box in enumerate(boxes):
                if track.cls != box.cls:
                    continue
                score = iou(track, box)
                if score >= self.min_iou:
                    pairs.append((score, ti, bi))
        pairs.sort(reverse=True)
        used_t: set[int] = set()
        used_b: set[int] = set()
        matched = []
        for _score, ti, bi in pairs:
            if ti in used_t or bi in used_b:
                continue
            used_t.add(ti)
            used_b.add(bi)
            matched.append((ti, bi))
        return matched, used_t, used_b

    def _spawn(self, box: Box, t: float) -> None:
        cal = self.cal.get(box.cls)
        if cal is None:
            return
        dist = distance_m(box.height, cal)
        if dist > self.max_dist:
            return
        side = side_of(box.cx, self.left_max, self.right_min)
        track = Track(self._next, box, dist, t, side)
        self._next += 1
        self._qualify(track, dist, 0.0)
        self.tracks.append(track)

    def _advance(self, track: Track, box: Box, t: float) -> None:
        cal = self.cal.get(box.cls, 0.0)
        dist = distance_m(box.height, cal)
        speed = closing_mps(track.dist, dist, t - track.t)
        track.follow(box)
        track.dist = dist
        track.speed = speed
        track.t = t
        track.side = side_of(box.cx, self.left_max, self.right_min)
        track.hits += 1
        track.missed = 0
        self._qualify(track, dist, speed)

    def _qualify(self, track: Track, dist: float, speed: float) -> None:
        on_a_side = track.side in ("left", "behind", "right")
        closing = speed >= self.min_approach
        near = dist <= self.near_m
        if on_a_side and dist <= self.max_dist and (closing or near):
            track.approach_hits += 1
        else:
            track.approach_hits = 0
        track.emitting = track.approach_hits >= self.persist and track.missed == 0

    def _choose(self) -> list[dict]:
        """Nearest qualifying track on each side. Within 0.5 m, the faster one."""
        chosen: dict[str, Track] = {}
        for track in self.tracks:
            if not track.emitting or track.side not in ("left", "behind", "right"):
                continue
            current = chosen.get(track.side)
            if current is None:
                chosen[track.side] = track
                continue
            if track.dist < current.dist - 0.5:
                chosen[track.side] = track
            elif abs(track.dist - current.dist) <= 0.5 and track.speed > current.speed:
                chosen[track.side] = track
        messages = []
        for side in ("left", "behind", "right"):
            track = chosen.get(side)
            if track is None:
                continue
            speed = track.speed if track.speed >= self.min_approach else max(0.0, track.speed)
            messages.append({
                "type": "car",
                "id": f"cam-{side}",
                "side": side,
                "dist": round(track.dist, 2),
                "speed": round(speed, 2),
            })
        return messages


def synthetic_boxes(frame: int, cal: dict[str, float] | None = None) -> list[Box]:
    """A 30-frame loop: a car closing on the right, a car closing in the behind band, then a near car on the left.

    A far person stands on the left the whole time and stays off the bus.
    """
    table = DEFAULT_CAL if cal is None else cal
    i = frame % 30
    boxes = [box_at("person", 0.15, table["person"] / 22.0)]
    if i < 20:
        dist = 18.0 - 0.4 * i
        boxes.append(box_at("car", 0.82, table["car"] / dist))
        boxes.append(box_at("car", 0.50, table["car"] / (16.0 - 0.3 * i)))
    else:
        boxes.append(box_at("car", 0.18, table["car"] / 4.0))
    return boxes


def resolve_device(choice: str) -> str:
    """MPS on Apple Silicon when PyTorch reports it, otherwise CPU."""
    if choice and choice != "auto":
        return choice
    try:
        import torch
    except ImportError:
        return "cpu"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def load_model(weights: str):
    from ultralytics import YOLO

    try:
        return YOLO(weights)
    except Exception as exc:
        if weights == "yolov8n.pt":
            raise
        print(f"camera  {weights} unavailable ({exc}); trying yolov8n.pt", flush=True)
        return YOLO("yolov8n.pt")


def boxes_from_result(result) -> list[Box]:
    raw = getattr(result, "boxes", None)
    if raw is None or len(raw) == 0:
        return []
    names = getattr(result, "names", {}) or {}
    xyxy = raw.xyxyn.tolist()
    classes = raw.cls.tolist()
    confs = raw.conf.tolist()
    found = []
    allowed = set(CLASS_IDS)
    for (x1, y1, x2, y2), cls_id, conf in zip(xyxy, classes, confs):
        name = names.get(int(cls_id), "")
        if name not in allowed:
            continue
        found.append(Box(name, float(x1), float(y1), float(x2), float(y2), float(conf)))
    return found


def infer(model, frame, device_box: dict, args):
    class_ids = [CLASS_IDS[name] for name in CLASS_IDS]
    try:
        results = model.predict(
            frame,
            device=device_box["device"],
            conf=args.conf,
            classes=class_ids,
            imgsz=args.imgsz,
            verbose=False,
        )
    except Exception as exc:
        if device_box["device"] == "cpu":
            raise
        print(f"camera  {device_box['device']} failed ({exc}); falling back to cpu", flush=True)
        device_box["device"] = "cpu"
        results = model.predict(
            frame,
            device="cpu",
            conf=args.conf,
            classes=class_ids,
            imgsz=args.imgsz,
            verbose=False,
        )
    if not results:
        return []
    return boxes_from_result(results[0])


def open_capture(cv2, index: int):
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(
            f"camera {index} did not open. Run --list-cameras. "
            "Continuity Camera has to be connected, and macOS has to allow the terminal to use the camera."
        )
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    return cap


def list_cameras(limit: int = 6) -> list[int]:
    import cv2

    found = []
    for index in range(limit):
        cap = cv2.VideoCapture(index)
        opened = cap.isOpened()
        cap.release()
        if opened:
            found.append(index)
    return found


def draw_preview(cv2, frame, tracker: SceneTracker, mirror: bool):
    height, width = frame.shape[:2]
    x_left = int(tracker.left_max * width)
    x_right = int(tracker.right_min * width)
    cv2.line(frame, (x_left, 0), (x_left, height), (255, 255, 255), 1)
    cv2.line(frame, (x_right, 0), (x_right, height), (255, 255, 255), 1)
    cv2.putText(frame, "LEFT", (8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
    cv2.putText(frame, "BEHIND", (x_left + 8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 80, 255), 2)
    cv2.putText(frame, "RIGHT", (x_right + 8, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 180, 40), 2)
    note = "MIRRORED" if mirror else "NOT MIRRORED"
    cv2.putText(frame, note, (8, height - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 240, 240), 1)
    for track in tracker.tracks:
        if track.missed:
            continue
        x1 = int(clamp(track.x1, 0, 1) * width)
        y1 = int(clamp(track.y1, 0, 1) * height)
        x2 = int(clamp(track.x2, 0, 1) * width)
        y2 = int(clamp(track.y2, 0, 1) * height)
        if track.emitting and track.side == "left":
            color = (0, 180, 255)
        elif track.emitting and track.side == "behind":
            color = (40, 40, 255)
        elif track.emitting and track.side == "right":
            color = (255, 160, 40)
        else:
            color = (140, 140, 140)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        label = f"{track.cls} {track.dist:.1f}m {track.speed:+.1f}"
        cv2.putText(frame, label, (x1, max(16, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    return frame


def show_preview(cv2, frame) -> str:
    """'ok', 'quit', or 'fail'. Fail leaves the bridge running without a window."""
    try:
        cv2.imshow(WINDOW, frame)
        key = cv2.waitKey(1) & 0xFF
    except Exception as exc:
        print(f"camera  preview unavailable ({exc})", flush=True)
        return "fail"
    if key in (ord("q"), 27):
        return "quit"
    return "ok"


def car_message(track_msg: dict) -> dict:
    return {
        "type": "car",
        "id": track_msg["id"],
        "side": track_msg["side"],
        "dist": track_msg["dist"],
        "speed": track_msg["speed"],
    }


class Publisher:
    def __init__(self, hub: Hub):
        self.hub = hub
        self._held_at = {"left": 0.0, "behind": 0.0, "right": 0.0}

    async def send(self, msg: dict) -> None:
        side = msg["side"]
        if self.hub.source != "real":
            now = time.monotonic()
            if now - self._held_at.get(side, 0.0) >= 1.0:
                self._held_at[side] = now
                print(
                    f"camera held  {side:5}  {msg['dist']:5.2f} m  {msg['speed']:+5.2f} m/s",
                    flush=True,
                )
            return
        print(
            f"camera {side:5}  {msg['dist']:5.2f} m  {msg['speed']:+5.2f} m/s",
            flush=True,
        )
        await self.hub.broadcast(car_message(msg))


def apply_source(hub: Hub, value: str) -> str:
    """Overlay control. sim plays the scripted cars; real returns to the camera."""
    if value not in ("sim", "real"):
        return hub.source
    if value != hub.source:
        label = "simulated cars" if value == "sim" else "camera"
        print(f"mode   {value}  ({label})", flush=True)
    return value


def make_tracker(args) -> SceneTracker:
    left_max, right_min = zone_edges(args.center_width)
    return SceneTracker(
        cal=args.cal,
        left_max=left_max,
        right_min=right_min,
        persist=args.persist,
        min_approach=args.min_approach,
        near_m=args.near,
        max_dist=args.max_dist,
    )


async def run_synthetic(hub: Hub, args, stop: asyncio.Event) -> None:
    tracker = make_tracker(args)
    publisher = Publisher(hub)
    dt = 1.0 / args.fps
    frame = 0
    t = 0.0
    print("camera  synthetic boxes — right car closes, then a near car on the left", flush=True)
    while not stop.is_set():
        for msg in tracker.update(synthetic_boxes(frame, args.cal), t):
            await publisher.send(msg)
        frame += 1
        t += dt
        await asyncio.sleep(dt)


async def _idle(stop: asyncio.Event, text: str) -> None:
    print(text, flush=True)
    while not stop.is_set():
        await asyncio.sleep(2)


async def run_camera(hub: Hub, args, stop: asyncio.Event) -> None:
    try:
        import cv2
    except ImportError as exc:
        await _idle(stop, f"camera: {exc}. pip install opencv-python ultralytics, or run --synthetic.")
        return
    try:
        model = await asyncio.to_thread(load_model, args.model)
    except Exception as exc:
        await _idle(stop, f"camera: {exc.__class__.__name__}: {exc}. pip install ultralytics, or run --synthetic.")
        return
    device_box = {"device": resolve_device(args.device)}
    print(f"camera  detector {args.model}  device {device_box['device']}", flush=True)
    try:
        cap = open_capture(cv2, args.camera)
    except Exception as exc:
        await _idle(stop, f"camera: {exc}")
        return

    tracker = make_tracker(args)
    publisher = Publisher(hub)
    period = 1.0 / args.fps
    preview = args.preview
    try:
        while not stop.is_set():
            started = time.monotonic()
            ok, frame = cap.read()
            if not ok or frame is None:
                print("camera  no frame", flush=True)
                await asyncio.sleep(0.2)
                continue
            if args.mirror:
                frame = cv2.flip(frame, 1)
            try:
                boxes = await asyncio.to_thread(infer, model, frame, device_box, args)
            except Exception as exc:
                print(f"camera  detect failed: {exc.__class__.__name__}: {exc}", flush=True)
                await asyncio.sleep(0.2)
                continue
            for msg in tracker.update(boxes, time.monotonic()):
                await publisher.send(msg)
            if preview:
                draw_preview(cv2, frame, tracker, args.mirror)
                state = show_preview(cv2, frame)
                if state == "fail":
                    preview = False
                elif state == "quit":
                    stop.set()
                    break
            leftover = period - (time.monotonic() - started)
            if leftover > 0:
                await asyncio.sleep(leftover)
    finally:
        cap.release()
        if args.preview:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass


async def play_buzz(cmd: dict) -> None:
    """Log one buzz. rings/datafeel_rings.py is what actually plays it."""
    side = str(cmd.get("side", "?"))
    pattern = str(cmd.get("pattern", "?"))
    try:
        intensity = float(cmd.get("intensity", 0))
    except (TypeError, ValueError):
        intensity = 0.0
    print(f"rings  client plays  {side:5}  {pattern:13}  {intensity:.2f}", flush=True)


def banner(args) -> None:
    ip = lan_ip()
    mode = "synthetic boxes" if args.synthetic else f"camera {args.camera}"
    mirror = "on" if args.mirror else "off"
    print("", flush=True)
    print(f"  haptic-ride  camera bridge  ({mode})", flush=True)
    print(f"  overlay     http://127.0.0.1:{args.http_port}", flush=True)
    print(f"  websocket   ws://{ip}:{args.port}", flush=True)
    print(f"  mirror      {mirror}  (camera faces backward)", flush=True)
    print("  route       nav cues stay on; overlay can switch cars to Simulated", flush=True)
    print("  rings       log only — run rings/datafeel_rings.py on this Mac", flush=True)
    print("", flush=True)


async def amain(args) -> None:
    # "real" is what the overlay already shows as the live sensor.
    hub = Hub(mode="synthetic" if args.synthetic else "real", source="real")
    hub.on_buzz = play_buzz
    hub.on_mode = lambda value: apply_source(hub, value)
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
            sensor = asyncio.create_task(
                run_synthetic(hub, args, stop) if args.synthetic else run_camera(hub, args, stop)
            )
            route = asyncio.create_task(
                simulate(hub, None, TICK_S, emit_cars=lambda: hub.source == "sim")
            )
            await stop.wait()
            sensor.cancel()
            route.cancel()
            await asyncio.gather(sensor, route, return_exceptions=True)
    except OSError as exc:
        raise SystemExit(f"websocket port {args.port} is already in use ({exc})") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Haptic Ride camera bridge")
    add_server_args(parser)
    parser.add_argument("--camera", type=int, default=0, help="webcam index; Continuity Camera is one of these")
    parser.add_argument("--list-cameras", action="store_true", help="print indexes that open, then exit")
    parser.add_argument("--model", default="yolo11n.pt", help="ultralytics weights; yolov8n.pt also works")
    parser.add_argument("--device", default="auto", help="auto, mps, or cpu")
    parser.add_argument("--conf", type=float, default=0.35, help="detector confidence")
    parser.add_argument("--imgsz", type=int, default=320, help="inference size; 320 keeps a laptop demo responsive")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS)
    parser.add_argument(
        "--mirror",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="flip the frame horizontally (default on: the camera faces backward)",
    )
    parser.add_argument("--preview", action="store_true", help="window with boxes and left/behind/right zones")
    parser.add_argument("--synthetic", action="store_true", help="publish a scripted closing car, no camera or model")
    parser.add_argument(
        "--cal",
        default="",
        help="per-class meters at full frame height, e.g. car=3.2,person=3.6",
    )
    parser.add_argument(
        "--center-width",
        type=float,
        default=CENTER_WIDTH,
        help="width of the behind band, as a fraction of the frame (default 0.34)",
    )
    parser.add_argument("--persist", type=int, default=PERSIST_FRAMES, help="consecutive frames before a car is published")
    parser.add_argument("--min-approach", type=float, default=MIN_APPROACH_MPS, help="closing m/s required when the object is not near")
    parser.add_argument("--near", type=float, default=NEAR_M, help="meters; inside this, publish even if the box is not growing")
    parser.add_argument("--max-dist", type=float, default=MAX_DIST_M, help="ignore boxes farther than this")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.list_cameras:
        try:
            found = list_cameras()
        except ImportError as exc:
            raise SystemExit(f"{exc}. pip install opencv-python") from exc
        if not found:
            print("camera  none found", flush=True)
        else:
            print("camera  " + ", ".join(str(index) for index in found), flush=True)
        return
    if args.fps <= 0:
        raise SystemExit("--fps must be positive")
    if args.persist < 1:
        raise SystemExit("--persist must be at least 1")
    if not 0.05 <= args.center_width < 1.0:
        raise SystemExit("--center-width must be from 0.05 up to, but not including, 1")
    if args.near <= 0 or args.max_dist <= 0:
        raise SystemExit("--near and --max-dist must be positive")
    if not 0.0 < args.conf <= 1.0:
        raise SystemExit("--conf must be between 0 and 1")
    args.cal = parse_cal(args.cal)
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()

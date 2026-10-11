"""WebSocket hub shared by the fake bridge and the hardware stub.

Every client speaks the same JSON protocol. A message from one client is
forwarded to the others. The bridge publishes radar (or the scripted route)
and logs buzz commands. The game engine is the only process that scores.
rings/datafeel_rings.py is the process that drives the DataFeel dots.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "overlay" / "index.html"


class JpegFeed:
    """Latest annotated camera frame. The overlay reads it as MJPEG."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._seq = 0

    def publish(self, jpeg: bytes) -> None:
        if not jpeg:
            return
        with self._lock:
            self._jpeg = jpeg
            self._seq += 1

    def latest(self) -> tuple[int, bytes | None]:
        with self._lock:
            return self._seq, self._jpeg


FEED = JpegFeed()

PATTERNS = {"turn_soon", "turn_now", "car_approach", "car_behind", "blue_shell", "combo"}
BUZZ_SIDES = {"left", "right", "both"}


def lan_ip() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


class Hub:
    def __init__(self, mode: str, source: str | None = None):
        self.mode = mode
        if source in ("sim", "real"):
            self.source = source
        else:
            self.source = "sim" if mode == "fake" else "real"
        self.clients: set = set()
        self.on_buzz = None
        # (value) -> "sim" | "real" | None. None leaves the source unchanged.
        self.on_mode = None
        # scripted | spotter | phone | off. Empty on bridges that always script the route.
        self.nav_mode = ""
        self._lock = asyncio.Lock()
        self._buzz_log: dict[str, tuple] = {}

    async def broadcast(self, msg: dict, exclude=None) -> None:
        raw = json.dumps(msg, separators=(",", ":"))
        async with self._lock:
            dead = []
            for ws in list(self.clients):
                if ws is exclude:
                    continue
                try:
                    await ws.send(raw)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)

    def log_buzz(self, msg: dict) -> None:
        side = str(msg.get("side", "?"))
        pattern = str(msg.get("pattern", "?"))
        try:
            intensity = round(float(msg.get("intensity", 0)), 2)
        except (TypeError, ValueError):
            intensity = 0.0
        now = time.monotonic()
        prev = self._buzz_log.get(side)
        if prev and prev[0] == pattern and abs(prev[1] - intensity) < 0.08 and now - prev[2] < 0.5:
            return
        self._buzz_log[side] = (pattern, intensity, now)
        print(f"buzz  {side:5}  {pattern:13}  {intensity:.2f}", flush=True)

    def _accept_buzz(self, msg: dict) -> bool:
        if msg.get("pattern") not in PATTERNS or msg.get("side") not in BUZZ_SIDES:
            return False
        try:
            msg["intensity"] = round(max(0.0, min(1.0, float(msg.get("intensity", 0)))), 2)
        except (TypeError, ValueError):
            return False
        return True

    async def handler(self, ws) -> None:
        self.clients.add(ws)
        who = getattr(ws, "remote_address", "?")
        print(f"join   {who}  ({len(self.clients)} on the bus)", flush=True)
        try:
            hello = {
                "type": "hello",
                "role": "bridge",
                "mode": self.mode,
                "source": self.source,
            }
            if self.nav_mode:
                hello["nav"] = self.nav_mode
            await ws.send(json.dumps(hello))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict) or "type" not in msg:
                    continue
                if msg["type"] == "mode":
                    value = msg.get("value")
                    if value not in ("sim", "real") or self.on_mode is None:
                        continue
                    accepted = self.on_mode(value)
                    if asyncio.iscoroutine(accepted):
                        accepted = await accepted
                    if accepted not in ("sim", "real"):
                        continue
                    self.source = accepted
                    await self.broadcast({"type": "mode", "value": accepted})
                    continue
                if msg["type"] == "buzz":
                    if not self._accept_buzz(msg):
                        continue
                    self.log_buzz(msg)
                    if self.on_buzz is not None:
                        await self.on_buzz(msg)
                await self.broadcast(msg, exclude=ws)
        finally:
            self.clients.discard(ws)
            print(f"leave  {who}  ({len(self.clients)} on the bus)", flush=True)


def serve_overlay(host: str, http_port: int, ws_port: int, ssl_context=None) -> None:
    pages = {
        "/": OVERLAY,
        "/index.html": OVERLAY,
        "/spotter": ROOT / "overlay" / "spotter.html",
        "/nav": ROOT / "overlay" / "nav.html",
    }
    raw_pages = {route: path.read_bytes() for route, path in pages.items()}
    token = str(ws_port).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path == "/camera.mjpg":
                self._stream_camera()
                return
            if path not in raw_pages:
                self.send_error(404)
                return
            body = raw_pages[path].replace(b"__WS_PORT__", token)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _stream_camera(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            last = -1
            try:
                while True:
                    seq, jpeg = FEED.latest()
                    if jpeg is None or seq == last:
                        time.sleep(0.04)
                        continue
                    last = seq
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpeg)).encode("ascii")
                        + b"\r\n\r\n"
                    )
                    self.wfile.write(jpeg)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return
            except Exception:
                return

        def log_message(self, fmt: str, *args) -> None:
            return

    class Server(ThreadingHTTPServer):
        allow_reuse_address = True

    try:
        httpd = Server((host, http_port), Handler)
    except OSError as exc:
        raise SystemExit(f"overlay port {http_port} is already in use ({exc})") from exc
    if ssl_context is not None:
        httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


def add_server_args(parser) -> None:
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket port")
    parser.add_argument("--http-port", type=int, default=8641, help="overlay HTTP port")

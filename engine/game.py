#!/usr/bin/env python3
"""Score the ride and decide what the rings should do.

Connects to the bridge as a client. Sensor events come in, buzz commands and
game state go out. Run exactly one of these on the LAN.

Scoring (pay = 1 + 0.5 × combo, then combo grows by 1):

    item box     100 × pay   shoulder check while that side has a car warning
    blue shell   250 × pay   same check, but the car is closing at ≥ 8 m/s
    drift boost  150 × pay   turn input matches the cue during TURN NOW
    miss                   a warned car passes unseen, or TURN NOW expires

The nearest car owns the ring. A car directly behind owns both rings and
buzzes them together. A turn cue uses a ring only when no car does.
Safety wins over navigation when they land on the same side.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

ALERT_M = 25.0
BLUE_MPS = 8.0
TURN_NOW_M = 12.0
TURN_SOON_M = 45.0
# A car that stops updating is treated as passed. Keep this longer than a
# sparse replay gap (~1s) and short enough that a miss still reads as one.
CAR_GONE_S = 1.5
BASE_ITEM = 100
BASE_DODGE = 250
BASE_DRIFT = 150
CELEBRATE = {"item", "dodge", "drift"}


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def car_intensity(dist: float) -> float:
    """0.18 at the edge of the warning zone, 1.0 once the car is on you."""
    if dist > ALERT_M:
        return 0.0
    return round(clamp((ALERT_M - dist) / 20.0, 0.18, 1.0), 2)


def nav_cue(in_m: float) -> tuple[str, float] | None:
    if in_m > TURN_SOON_M:
        return None
    if in_m > TURN_NOW_M:
        span = TURN_SOON_M - TURN_NOW_M
        intensity = 0.28 + (TURN_SOON_M - in_m) / span * 0.42
        return "turn_soon", round(intensity, 2)
    intensity = 0.82 + (TURN_NOW_M - max(in_m, 0.0)) / TURN_NOW_M * 0.18
    return "turn_now", round(min(1.0, intensity), 2)


class Game:
    def __init__(self) -> None:
        self.now = 0.0
        self.cars: dict[str, dict] = {}
        self.nav: dict | None = None
        self.scored_nav: set[str] = set()
        self.score = 0
        self.combo = 0
        self.flash: dict | None = None
        self.flash_n = 0
        self.items = self.dodges = self.drifts = self.misses = 0

    def pay_mult(self) -> float:
        return 1.0 + 0.5 * self.combo

    def set_flash(self, text: str, pts: int, kind: str, force: bool = False) -> None:
        hot = (
            self.flash is not None
            and self.now < self.flash["until"]
            and self.flash["kind"] in CELEBRATE
        )
        if hot and not force and kind not in CELEBRATE:
            return
        hold = 1.5 if kind in CELEBRATE else 1.0
        self.flash_n += 1
        self.flash = {
            "text": text,
            "pts": pts,
            "kind": kind,
            "n": self.flash_n,
            "until": self.now + hold,
        }

    def award(self, base: int, kind: str, text: str) -> int:
        pts = int(round(base * self.pay_mult()))
        self.score += pts
        self.combo += 1
        self.set_flash(text, pts, kind, force=True)
        print(f"+{pts:<4} {text:<18} score={self.score:<5} combo={self.combo}", flush=True)
        return pts

    def on_car(self, msg: dict) -> None:
        try:
            side = msg["side"]
            dist = float(msg["dist"])
            speed = float(msg["speed"])
        except (KeyError, TypeError, ValueError):
            return
        if side == "center":
            side = "behind"
        if side not in ("left", "right", "behind"):
            return
        cid = str(msg.get("id") or side)
        prev = self.cars.get(cid)
        self.cars[cid] = {
            "id": cid,
            "side": side,
            "dist": dist,
            "speed": speed,
            "blue": speed >= BLUE_MPS,
            "warned": bool(prev and prev["warned"]) or dist <= ALERT_M,
            "scored": bool(prev and prev["scored"]),
            "seen": self.now,
        }

    def on_nav(self, msg: dict) -> None:
        try:
            turn = msg["turn"]
            in_m = float(msg["in_m"])
        except (KeyError, TypeError, ValueError):
            return
        if turn not in ("left", "right"):
            return
        nid = str(msg.get("id") or turn)
        prev = self.nav
        if (
            prev is not None
            and prev["id"] != nid
            and prev["id"] not in self.scored_nav
            and prev["in_m"] <= TURN_NOW_M
        ):
            self.combo = 0
            self.misses += 1
            self.set_flash("MISSED TURN", 0, "miss")
        self.nav = {"id": nid, "turn": turn, "in_m": in_m}

    def on_check(self, side: str) -> int | None:
        """Score a shoulder check, or a look-back (`side` "behind").

        A check on the left or the right also counts for a car directly
        behind. The same-side car is scored first when both are there.
        """
        if side == "center":
            side = "behind"
        if side not in ("left", "right", "behind"):
            return None
        if side == "behind":
            wanted = ("behind",)
        else:
            wanted = (side, "behind")
        live = [
            car
            for car in self.cars.values()
            if car["side"] in wanted and not car["scored"] and car["dist"] <= ALERT_M
        ]
        if not live:
            self.set_flash("NO CAR", 0, "whiff")
            return None
        live.sort(key=lambda car: (car["side"] != side, not car["blue"], car["dist"]))
        car = live[0]
        car["scored"] = True
        if car["blue"]:
            self.dodges += 1
            return self.award(BASE_DODGE, "dodge", "BLUE SHELL DODGED")
        self.items += 1
        return self.award(BASE_ITEM, "item", "ITEM BOX")

    def on_turn(self, turn: str) -> int | None:
        nav = self.nav
        if nav is None or turn not in ("left", "right"):
            return None
        if nav["id"] in self.scored_nav:
            return None
        if nav["in_m"] > TURN_NOW_M:
            self.set_flash("TOO EARLY", 0, "early")
            return None
        if turn != nav["turn"]:
            self.combo = 0
            self.set_flash("WRONG WAY", 0, "wrong")
            return None
        self.scored_nav.add(nav["id"])
        self.drifts += 1
        return self.award(BASE_DRIFT, "drift", "DRIFT BOOST")

    def sweep(self) -> None:
        stale = [cid for cid, car in self.cars.items() if self.now - car["seen"] > CAR_GONE_S]
        for cid in stale:
            car = self.cars.pop(cid)
            if car["scored"] or not car["warned"]:
                continue
            self.combo = 0
            self.misses += 1
            if car["blue"]:
                self.set_flash("SHELL HIT", 0, "shell")
            else:
                self.set_flash("MISSED TRAFFIC", 0, "miss")

    def rings(self) -> dict[str, dict | None]:
        out: dict[str, dict | None] = {"left": None, "right": None}
        nearest: dict[str, dict] = {}
        for car in self.cars.values():
            if car["dist"] > ALERT_M or car["side"] not in ("left", "right", "behind"):
                continue
            current = nearest.get(car["side"])
            if current is None or car["dist"] < current["dist"]:
                nearest[car["side"]] = car
        for side in ("left", "right"):
            car = nearest.get(side)
            if car is None:
                continue
            if car["blue"]:
                pattern, intensity = "blue_shell", round(max(0.85, car_intensity(car["dist"])), 2)
            else:
                pattern, intensity = "car_approach", car_intensity(car["dist"])
            out[side] = {"pattern": pattern, "intensity": intensity}
        behind = nearest.get("behind")
        if behind is not None:
            # One shared command so both wrists show the same pulse.
            cmd = {"pattern": "car_behind", "intensity": car_intensity(behind["dist"])}
            for side in ("left", "right"):
                if out[side] is None:
                    out[side] = cmd
        if self.nav is not None:
            cue = nav_cue(self.nav["in_m"])
            if cue is not None and out[self.nav["turn"]] is None:
                pattern, intensity = cue
                out[self.nav["turn"]] = {"pattern": pattern, "intensity": intensity}
        return out

    def _phase(self) -> str | None:
        if self.nav is None:
            return None
        cue = nav_cue(self.nav["in_m"])
        return cue[0] if cue else "far"

    def _coach(self, checks: list[str], need_turn: bool) -> str:
        blues = [car for car in self.cars.values() if car["blue"] and not car["scored"]]
        if blues:
            blues.sort(key=lambda car: car["dist"])
            return f"BLUE SHELL {self._place(blues[0]['side'])} — shoulder-check to dodge"
        if need_turn and self.nav is not None:
            return f"TURN {self.nav['turn'].upper()} NOW — drift boost"
        if checks:
            if checks[0] == "behind":
                return "CAR BEHIND — look back or shoulder-check for an item box"
            return f"CAR ON THE {checks[0].upper()} — shoulder check for an item box"
        return "Rings buzz the turn. Radar shows what is behind you."

    @staticmethod
    def _place(side: str) -> str:
        if side == "behind":
            return "BEHIND"
        return f"ON THE {side.upper()}"

    def snapshot(self) -> dict:
        rings = self.rings()
        cars = sorted(
            (
                {
                    "id": car["id"],
                    "side": car["side"],
                    "dist": round(car["dist"], 1),
                    "speed": car["speed"],
                    "blue": car["blue"],
                }
                for car in self.cars.values()
            ),
            key=lambda car: car["dist"],
        )
        flash = None
        if self.flash is not None and self.now < self.flash["until"]:
            flash = {
                "text": self.flash["text"],
                "pts": self.flash["pts"],
                "kind": self.flash["kind"],
                "n": self.flash["n"],
            }
        nav = None
        phase = self._phase()
        need_turn = False
        if self.nav is not None and phase is not None:
            need_turn = phase == "turn_now" and self.nav["id"] not in self.scored_nav
            nav = {
                "id": self.nav["id"],
                "turn": self.nav["turn"],
                "in_m": round(self.nav["in_m"], 1),
                "phase": phase,
            }
        checks = sorted(
            {
                car["side"]
                for car in self.cars.values()
                if not car["scored"] and car["dist"] <= ALERT_M
            }
        )
        return {
            "type": "state",
            "score": self.score,
            "combo": self.combo,
            "flash": flash,
            "nav": nav,
            "cars": cars,
            "rings": rings,
            "check": checks,
            "need_turn": need_turn,
            "coach": self._coach(checks, need_turn),
            "stats": {
                "items": self.items,
                "dodges": self.dodges,
                "drifts": self.drifts,
                "misses": self.misses,
            },
        }


def handle(game: Game, msg: dict) -> int | None:
    kind = msg.get("type")
    if kind == "car":
        game.on_car(msg)
    elif kind == "nav":
        game.on_nav(msg)
    elif kind == "shoulder_check":
        return game.on_check(msg.get("side"))
    elif kind == "look_back":
        return game.on_check("behind")
    elif kind == "turn":
        return game.on_turn(msg.get("turn"))
    return None


def buzz_plan(rings: dict[str, dict | None]) -> list[tuple[str, dict]]:
    """Buzz messages for this frame. A behind car on both wrists is one `both` buzz."""
    left = rings.get("left")
    right = rings.get("right")
    if (
        left
        and right
        and left.get("pattern") == "car_behind"
        and right.get("pattern") == "car_behind"
        and left.get("intensity") == right.get("intensity")
    ):
        return [("both", left)]
    planned = []
    for side in ("left", "right"):
        cmd = rings.get(side)
        if cmd:
            planned.append((side, cmd))
    return planned


async def session(url: str) -> None:
    from websockets.asyncio.client import connect

    game = Game()
    async with connect(url, open_timeout=2) as ws:
        print(f"game: connected {url}", flush=True)
        last_buzz: dict[str, dict | None] = {"left": None, "right": None, "both": None}
        last_at = {"left": 0.0, "right": 0.0, "both": 0.0}
        last_state = 0.0
        while True:
            raw = None
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.1)
            except asyncio.TimeoutError:
                raw = None
            game.now = time.monotonic()
            award = None
            if raw is not None:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    msg = None
                if isinstance(msg, dict):
                    award = handle(game, msg)
                    if award:
                        await ws.send(
                            json.dumps(
                                {"type": "buzz", "side": "both", "pattern": "combo", "intensity": 1}
                            )
                        )
            game.sweep()
            now = time.monotonic()
            if award or now - last_state >= 0.1:
                await ws.send(json.dumps(game.snapshot(), separators=(",", ":")))
                last_state = now
            for side, cmd in buzz_plan(game.rings()):
                due = now - last_at[side] >= 0.45
                if cmd != last_buzz[side] or due:
                    last_buzz[side] = cmd
                    last_at[side] = now
                    if cmd is not None:
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "buzz",
                                    "side": side,
                                    "pattern": cmd["pattern"],
                                    "intensity": cmd["intensity"],
                                }
                            )
                        )


async def run(url: str) -> None:
    while True:
        try:
            await session(url)
        except Exception as exc:
            print(f"game: {exc.__class__.__name__}: {exc} — retrying", flush=True)
        await asyncio.sleep(0.4)


def main() -> None:
    parser = argparse.ArgumentParser(description="Haptic Ride game engine")
    parser.add_argument("--url", default="ws://127.0.0.1:8765")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.url))
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()

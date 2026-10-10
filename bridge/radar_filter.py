"""Turn a range-FFT magnitude into a single car, or into silence.

The BGT60TR13C distance example reports the strongest bin. A roof or a
wall leaves several of those, and they jitter by a bin from frame to
frame. This filter subtracts an empty-scene average, then keeps folding
static bins back into that average. It follows every peak that clears
the threshold, and publishes only the nearest one that is actually
approaching: closing speed at least `min_approach`, and the distance
net-decreasing across the smoothing window.
"""

from __future__ import annotations

import statistics

# Tunables. The bridge CLI overrides these.
BG_FRAMES = 20
BG_ALPHA = 0.02
THRESHOLD_K = 6.0
MIN_RANGE_M = 0.3
PERSIST_FRAMES = 3
SMOOTH_FRAMES = 5
MAX_STEP_M = 1.5
MIN_APPROACH_MPS = 0.4

# The false peak Raymond sees on an empty scene, used by --synthetic.
STATIC_DIST_M = 4.7
STATIC_AMP = 12.0
# Roof returns from the live kit: nothing moving, still two strong bins.
ROOF_DIST_M = (1.74, 4.9)
ROOF_AMP = (40.0, 34.0)
CLOSING_MPS = 2.0


def linspace(stop: float, count: int) -> list[float]:
    """Match numpy.linspace(0, stop, count), which is what distance_fft.py plots."""
    if count <= 1:
        return [0.0]
    step = stop / (count - 1)
    return [i * step for i in range(count)]


def range_axis(max_range_m: float = 9.6, resolution_m: float = 0.15) -> list[float]:
    count = int(round(max_range_m / resolution_m)) + 1
    return linspace(max_range_m, count)


def spectrum(ranges: list[float], peaks: list[tuple[float, float]], floor: float = 1.0) -> list[float]:
    """A flat floor plus a narrow bump at each (distance, amplitude)."""
    mag = [floor] * len(ranges)
    for dist, amp in peaks:
        index = min(range(len(ranges)), key=lambda i: abs(ranges[i] - dist))
        mag[index] += amp
        if index > 0:
            mag[index - 1] += amp * 0.45
        if index + 1 < len(mag):
            mag[index + 1] += amp * 0.45
    return mag


def empty_scene(ranges: list[float]) -> list[float]:
    """What the kit reports with nothing moving: noise floor plus the static 4.7 m peak."""
    return spectrum(ranges, [(STATIC_DIST_M, STATIC_AMP)])


def roof_clutter(ranges: list[float]) -> list[tuple[float, float]]:
    return list(zip(ROOF_DIST_M, ROOF_AMP))


def demo_live_frames(ranges: list[float], dt: float, closing_mps: float = CLOSING_MPS) -> list[list[float]]:
    """Quiet scene, one 6 m outlier, then a car closing from 8 m.

    The roof peaks at 1.74 m and 4.9 m are not in the calibration, so
    they show up as new static energy. They must stay silent. The
    outlier and the original 4.7 m peak must stay silent too.
    """
    clutter = [(STATIC_DIST_M, STATIC_AMP), *roof_clutter(ranges)]
    frames = [spectrum(ranges, clutter) for _ in range(5)]
    frames.append(spectrum(ranges, [*clutter, (6.0, 40.0)]))
    frames.extend(spectrum(ranges, clutter) for _ in range(2))
    dist = 8.0
    step = closing_mps * dt
    for _ in range(16):
        frames.append(spectrum(ranges, [*clutter, (dist, 30.0)]))
        dist -= step
    return frames


def closing_speed(history: list[tuple[float, float]]) -> float:
    """Least-squares slope of distance vs time, negated so approach is positive."""
    if len(history) < 2:
        return 0.0
    t0 = history[0][0]
    xs = [t - t0 for t, _ in history]
    ys = [dist for _, dist in history]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 1e-12:
        return 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var_x
    return -slope


def approach_state(history: list[tuple[float, float]], min_approach: float) -> tuple[bool, float, float]:
    """Whether this window is a sustained approach, plus speed and median distance.

    Sustained means the smoothing window itself moved closer: the last
    sample is nearer than the first, and the median is nearer than the
    first. A one-frame dip does not move the median, so it does not count.
    """
    speed = closing_speed(history)
    dists = [dist for _, dist in history]
    smoothed = statistics.median(dists)
    start = dists[0]
    closing = dists[-1] < start and smoothed < start and speed >= min_approach
    return closing, speed, smoothed


class RangeTracker:
    def __init__(
        self,
        ranges_m: list[float],
        *,
        bg_frames: int = BG_FRAMES,
        bg_alpha: float = BG_ALPHA,
        threshold_k: float = THRESHOLD_K,
        min_range_m: float = MIN_RANGE_M,
        persist_frames: int = PERSIST_FRAMES,
        smooth_frames: int = SMOOTH_FRAMES,
        max_step_m: float = MAX_STEP_M,
        min_approach: float = MIN_APPROACH_MPS,
    ) -> None:
        self.ranges = [float(r) for r in ranges_m]
        self.bg_frames = bg_frames
        self.bg_alpha = bg_alpha
        self.threshold_k = threshold_k
        self.min_range_m = min_range_m
        self.persist_frames = persist_frames
        self.smooth_frames = smooth_frames
        self.max_step_m = max_step_m
        self.min_approach = min_approach
        self.background: list[float] | None = None
        self.note = "not calibrated"
        self._cal: list[list[float]] = []
        self._tracks: list[dict] = []

    def start_calibration(self) -> None:
        self._cal = []
        self.background = None
        self.note = "calibrating"
        self._tracks = []

    def calibrate_frame(self, magnitudes) -> int:
        """Add one empty-scene magnitude. Returns how many frames are in the average."""
        self._cal.append([float(v) for v in magnitudes])
        count = len(self._cal)
        if count >= self.bg_frames:
            width = len(self._cal[0])
            self.background = [
                sum(frame[i] for frame in self._cal) / count for i in range(width)
            ]
            self.note = "background ready"
        return count

    def _peaks(self, clean: list[float]) -> list[tuple[int, float]]:
        """Local maxima at or beyond min range that clear k × the median."""
        usable = [
            (i, clean[i]) for i, dist in enumerate(self.ranges) if dist >= self.min_range_m
        ]
        if not usable:
            self.note = "no bins past min range"
            return []
        floor = statistics.median([value for _, value in usable])
        thresh = self.threshold_k * floor
        found: list[tuple[int, float]] = []
        last = len(clean) - 1
        for i, dist in enumerate(self.ranges):
            if dist < self.min_range_m or clean[i] <= thresh:
                continue
            left = clean[i - 1] if i > 0 else -1.0
            right = clean[i + 1] if i < last else -1.0
            if clean[i] >= left and clean[i] >= right:
                found.append((i, dist))
        if not found:
            index = max(usable, key=lambda item: item[1])[0]
            self.note = f"below threshold  {self.ranges[index]:.2f} m"
        return found

    def _associate(self, peaks: list[tuple[int, float]], t: float) -> None:
        """Keep each peak on the track it already belongs to. Far jumps start a new track."""
        pairs: list[tuple[float, int, int]] = []
        for ti, track in enumerate(self._tracks):
            for pi, (_index, dist) in enumerate(peaks):
                err = abs(dist - track["last"])
                if err <= self.max_step_m:
                    pairs.append((err, ti, pi))
        pairs.sort()
        used_tracks: set[int] = set()
        used_peaks: set[int] = set()
        for _err, ti, pi in pairs:
            if ti in used_tracks or pi in used_peaks:
                continue
            used_tracks.add(ti)
            used_peaks.add(pi)
            index, dist = peaks[pi]
            track = self._tracks[ti]
            track["index"] = index
            track["last"] = dist
            track["streak"] += 1
            track["history"].append((float(t), dist))
            if len(track["history"]) > self.smooth_frames:
                track["history"] = track["history"][-self.smooth_frames :]
        self._tracks = [track for i, track in enumerate(self._tracks) if i in used_tracks]
        for pi, (index, dist) in enumerate(peaks):
            if pi in used_peaks:
                continue
            self._tracks.append(
                {"index": index, "last": dist, "streak": 1, "history": [(float(t), dist)]}
            )

    def _ready(self, track: dict) -> bool:
        return track["streak"] >= self.persist_frames and len(track["history"]) >= self.smooth_frames

    def _guard(self, index: int) -> set[int]:
        return {i for i in (index - 1, index, index + 1) if 0 <= i < len(self.ranges)}

    def _adapt(self, magnitudes, frozen: set[int]) -> None:
        """Fold bins that are not an approaching target into the background."""
        if self.background is None or self.bg_alpha <= 0:
            return
        keep = 1.0 - self.bg_alpha
        alpha = self.bg_alpha
        bg = self.background
        for i, mag in enumerate(magnitudes):
            if i in frozen:
                continue
            bg[i] = keep * bg[i] + alpha * float(mag)

    def _hold_note(self) -> None:
        if not self._tracks:
            return
        nearest = min(self._tracks, key=lambda track: track["last"])
        need = max(self.persist_frames, self.smooth_frames)
        if not self._ready(nearest):
            self.note = f"wait {nearest['streak']}/{need}  {nearest['last']:.2f} m"
            return
        _closing, speed, _dist = approach_state(nearest["history"], self.min_approach)
        self.note = f"static {nearest['last']:.2f} m {speed:+.2f} m/s"

    def update(self, magnitudes, t: float) -> dict | None:
        """Return {dist, speed} for the nearest approaching target, else None."""
        if self.background is None:
            self.note = "not calibrated"
            return None
        if len(magnitudes) != len(self.background):
            self.note = "frame length changed"
            return None
        clean = [max(0.0, float(m) - b) for m, b in zip(magnitudes, self.background)]
        peaks = self._peaks(clean)
        if not peaks:
            self._tracks = []
            self._adapt(magnitudes, set())
            return None
        self._associate(peaks, t)
        approaching: list[tuple[float, float, dict]] = []
        frozen: set[int] = set()
        for track in self._tracks:
            if not self._ready(track):
                continue
            closing, speed, dist = approach_state(track["history"], self.min_approach)
            if not closing:
                continue
            approaching.append((dist, speed, track))
            frozen |= self._guard(track["index"])
        self._adapt(magnitudes, frozen)
        if not approaching:
            self._hold_note()
            return None
        dist, speed, _track = min(approaching, key=lambda item: item[0])
        self.note = "emit"
        return {"dist": dist, "speed": speed}

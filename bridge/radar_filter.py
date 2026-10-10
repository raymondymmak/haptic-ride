"""Turn a range-FFT magnitude into a single car, or into silence.

The BGT60TR13C distance example reports the strongest bin. With nothing
inside 6 m that bin is often a static reflection around 4 m. This filter
subtracts an empty-scene average, keeps a peak only when it clears
k times the median of the bins past the minimum range, and emits only
after the same target has held for several frames.

Speed is the closing rate of that distance trend (positive when the
target is approaching). Distance is the median of the last few peaks.
"""

from __future__ import annotations

import statistics

# Tunables. The bridge CLI overrides these.
BG_FRAMES = 20
THRESHOLD_K = 6.0
MIN_RANGE_M = 0.3
PERSIST_FRAMES = 3
SMOOTH_FRAMES = 5
MAX_STEP_M = 1.5

# The false peak Raymond sees on an empty scene, used by --synthetic.
STATIC_DIST_M = 4.7
STATIC_AMP = 12.0
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


def demo_live_frames(ranges: list[float], dt: float, closing_mps: float = CLOSING_MPS) -> list[list[float]]:
    """Quiet scene, one 6 m outlier, then a car closing from 8 m.

    The outlier and the static peak must not produce a car. The closing
    target does, once it has lasted long enough.
    """
    frames = [empty_scene(ranges) for _ in range(5)]
    frames.append(spectrum(ranges, [(STATIC_DIST_M, STATIC_AMP), (6.0, 40.0)]))
    frames.extend(empty_scene(ranges) for _ in range(2))
    dist = 8.0
    step = closing_mps * dt
    for _ in range(12):
        frames.append(spectrum(ranges, [(STATIC_DIST_M, STATIC_AMP), (dist, 30.0)]))
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


class RangeTracker:
    def __init__(
        self,
        ranges_m: list[float],
        *,
        bg_frames: int = BG_FRAMES,
        threshold_k: float = THRESHOLD_K,
        min_range_m: float = MIN_RANGE_M,
        persist_frames: int = PERSIST_FRAMES,
        smooth_frames: int = SMOOTH_FRAMES,
        max_step_m: float = MAX_STEP_M,
    ) -> None:
        self.ranges = [float(r) for r in ranges_m]
        self.bg_frames = bg_frames
        self.threshold_k = threshold_k
        self.min_range_m = min_range_m
        self.persist_frames = persist_frames
        self.smooth_frames = smooth_frames
        self.max_step_m = max_step_m
        self.background: list[float] | None = None
        self.note = "not calibrated"
        self._cal: list[list[float]] = []
        self._streak = 0
        self._last_dist: float | None = None
        self._history: list[tuple[float, float]] = []

    def start_calibration(self) -> None:
        self._cal = []
        self.background = None
        self.note = "calibrating"
        self._reset_track()

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

    def _reset_track(self) -> None:
        self._streak = 0
        self._last_dist = None
        self._history = []

    def _peak(self, clean: list[float]) -> tuple[int, float] | None:
        usable = [
            (i, clean[i]) for i, dist in enumerate(self.ranges) if dist >= self.min_range_m
        ]
        if not usable:
            self.note = "no bins past min range"
            return None
        floor = statistics.median([value for _, value in usable])
        thresh = self.threshold_k * floor
        index, value = max(usable, key=lambda item: item[1])
        if value <= thresh:
            self.note = f"below threshold  {self.ranges[index]:.2f} m"
            return None
        return index, self.ranges[index]

    def update(self, magnitudes, t: float) -> dict | None:
        """Return {dist, speed} when a target is present, else None."""
        if self.background is None:
            self.note = "not calibrated"
            return None
        if len(magnitudes) != len(self.background):
            self.note = "frame length changed"
            return None
        clean = [max(0.0, float(m) - b) for m, b in zip(magnitudes, self.background)]
        found = self._peak(clean)
        if found is None:
            self._reset_track()
            return None
        _index, dist = found
        if self._last_dist is not None and abs(dist - self._last_dist) > self.max_step_m:
            self._reset_track()
        self._streak += 1
        self._last_dist = dist
        self._history.append((float(t), dist))
        if len(self._history) > self.smooth_frames:
            self._history = self._history[-self.smooth_frames :]
        if self._streak < self.persist_frames:
            self.note = f"wait {self._streak}/{self.persist_frames}  {dist:.2f} m"
            return None
        smoothed = statistics.median([d for _, d in self._history])
        speed = closing_speed(self._history)
        self.note = "emit"
        return {"dist": smoothed, "speed": speed}

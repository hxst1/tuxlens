"""Latency bookkeeping: clock sync with the phone and per-stage percentiles."""
from __future__ import annotations

import threading
import time
from collections import deque


def now_ms() -> float:
    return time.time() * 1000.0


class ClockSync:
    """NTP-style offset estimate (phone_clock - pc_clock), keeping the lowest-RTT sample."""

    def __init__(self) -> None:
        self.samples: deque[tuple[float, float]] = deque(maxlen=16)  # (rtt, offset)
        self.offset_ms: float | None = None
        self.rtt_ms: float | None = None

    def add(self, t_sent: float, t_phone: float, t_back: float) -> None:
        rtt = t_back - t_sent
        if rtt < 0:
            return
        self.samples.append((rtt, t_phone - (t_sent + t_back) / 2))
        best = min(self.samples)
        self.rtt_ms, self.offset_ms = best

    def to_pc(self, t_phone: float) -> float | None:
        return None if self.offset_ms is None else t_phone - self.offset_ms

    def reset(self) -> None:
        self.samples.clear()
        self.offset_ms = self.rtt_ms = None


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


class StageStats:
    """Rolling window of per-frame timings, in milliseconds."""

    STAGES = ("network", "decode", "process", "total")

    def __init__(self, window: int = 300) -> None:
        self.lock = threading.Lock()
        self.rows: dict[str, deque[float]] = {s: deque(maxlen=window) for s in self.STAGES}
        self.out_times: deque[float] = deque(maxlen=window)
        self.counters = {"frames_in": 0, "frames_out": 0, "decode_errors": 0, "keyframe_requests": 0,
                         "skipped_stale": 0}
        self.phone: dict = {}

    def add(self, stage: str, value: float | None) -> None:
        if value is None:
            return
        with self.lock:
            self.rows[stage].append(value)

    def count(self, name: str, n: int = 1) -> None:
        with self.lock:
            self.counters[name] = self.counters.get(name, 0) + n

    def mark_out(self) -> None:
        with self.lock:
            self.out_times.append(time.monotonic())
            self.counters["frames_out"] += 1

    def snapshot(self) -> dict:
        with self.lock:
            out = {s: {"p50": round(_pct(list(v), 0.5), 1), "p95": round(_pct(list(v), 0.95), 1)}
                   for s, v in self.rows.items()}
            times = list(self.out_times)
            fps = 0.0
            if len(times) > 1:
                recent = [t for t in times if t > times[-1] - 2.0]
                if len(recent) > 1:
                    fps = (len(recent) - 1) / (recent[-1] - recent[0])
            return {"stages_ms": out, "fps_out": round(fps, 1), "counters": dict(self.counters),
                    "phone": dict(self.phone)}

    def reset(self) -> None:
        with self.lock:
            for v in self.rows.values():
                v.clear()
            self.out_times.clear()

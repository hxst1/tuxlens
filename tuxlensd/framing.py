"""Auto-framing in the spirit of Apple's Center Stage.

The camera frame (after rotation) is larger than what is shown: a 16:9 window slides and zooms
inside it to keep the person framed. The person's extent comes from the segmentation mask of the
*displayed* window, so no extra model is needed. Movement is deliberately calm: a dead zone
ignores small shifts and the window eases toward its target (critically damped), so it never
jitters with breathing or gestures.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass


@dataclass
class Window:
    cx: float = 0.5      # center, normalized to the oriented camera frame
    cy: float = 0.5
    zoom: float = 1.0    # 1 = largest 16:9 window that fits; 2 = half its size


class Framer:
    HEAD_ROOM = 0.10       # space above the head, as a fraction of the window height
    PERSON_WIDTH = 0.55    # person (head + shoulders) width as a fraction of the window width
    DEAD_ZONE = 0.035      # ignore target moves smaller than this (normalized)
    LOST_AFTER_S = 2.0     # nobody seen for this long → ease back to the full view

    def __init__(self) -> None:
        self.window = Window()
        self.target = Window()
        self._vel = [0.0, 0.0, 0.0]
        self._last = time.monotonic()
        self._seen_at = 0.0

    def reset(self) -> None:
        self.window = Window()
        self.target = Window()
        self._vel = [0.0, 0.0, 0.0]

    @staticmethod
    def base_size(frame_w: int, frame_h: int, aspect: float) -> tuple[float, float]:
        """Size of the zoom-1 window as fractions of the frame."""
        if frame_w / frame_h > aspect:
            return frame_h * aspect / frame_w, 1.0
        return 1.0, frame_w / aspect / frame_h

    def update(self, bbox, frame_w: int, frame_h: int, aspect: float, max_zoom: float, speed: float) -> None:
        """bbox: person extent in the *current window* (normalized), or None."""
        now = time.monotonic()
        dt = min(0.1, now - self._last)
        self._last = now
        bw, bh = self.base_size(frame_w, frame_h, aspect)
        win = self.window
        ww, wh = bw / win.zoom, bh / win.zoom

        if bbox is not None:
            self._seen_at = now
            # Person box in frame coordinates.
            x0 = win.cx - ww / 2 + bbox[0] * ww
            x1 = win.cx - ww / 2 + bbox[2] * ww
            y0 = win.cy - wh / 2 + bbox[1] * wh
            person_w = max(1e-3, x1 - x0)
            zoom = bw * self.PERSON_WIDTH / person_w
            zoom = max(1.0, min(max_zoom, zoom))
            tw, th = bw / zoom, bh / zoom
            cx = (x0 + x1) / 2
            cy = y0 - self.HEAD_ROOM * th + th / 2
            target = Window(cx, cy, zoom)
            # Dead zone: keep the current target unless the new one is meaningfully different.
            t = self.target
            if (abs(target.cx - t.cx) > self.DEAD_ZONE or abs(target.cy - t.cy) > self.DEAD_ZONE
                    or abs(target.zoom - t.zoom) / t.zoom > self.DEAD_ZONE * 2):
                self.target = target
        elif now - self._seen_at > self.LOST_AFTER_S:
            self.target = Window()

        self._clamp(self.target, bw, bh)
        # Critically damped spring toward the target; speed 0..1 → time constant 1.6 s .. 0.35 s.
        tau = 1.6 - 1.25 * max(0.0, min(1.0, speed))
        omega = 2.0 / tau
        for i, name in enumerate(("cx", "cy", "zoom")):
            x = getattr(win, name)
            goal = getattr(self.target, name)
            acc = omega * omega * (goal - x) - 2 * omega * self._vel[i]
            self._vel[i] += acc * dt
            setattr(win, name, x + self._vel[i] * dt)
        self._clamp(win, bw, bh)

    @staticmethod
    def _clamp(w: Window, bw: float, bh: float) -> None:
        w.zoom = max(1.0, w.zoom) if math.isfinite(w.zoom) else 1.0
        hw, hh = bw / w.zoom / 2, bh / w.zoom / 2
        w.cx = min(1 - hw, max(hw, w.cx))
        w.cy = min(1 - hh, max(hh, w.cy))

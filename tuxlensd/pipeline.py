"""Decode → (effects) → v4l2 output, with no queues in between.

The network side drops each decoded frame into a one-slot mailbox that is overwritten; the output
thread always takes the newest frame, so a slow step skips frames instead of accumulating delay.
When the phone goes away the output keeps the device alive: it repeats the last frame briefly
(covers camera switches) and then shows a themed "camera paused" frame.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import av
import numpy as np
from av.video.reformatter import VideoReformatter

from . import placeholder
from .effects import Effects
from .framing import Framer, Window
from .stats import StageStats, now_ms
from .v4l2out import LoopbackWriter

log = logging.getLogger("tuxlens.pipeline")

HOLD_LAST_S = 1.5        # keep showing the last frame this long after the stream stops
IDLE_FPS = 10            # placeholder refresh rate (Chrome restarts capture after 1 s without frames)


@dataclass
class DecodedFrame:
    frame: av.VideoFrame
    seq: int
    t_capture_pc: float | None   # phone capture time mapped to the PC clock (ms)
    t_recv: float
    t_decoded: float


class Mailbox:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._item: DecodedFrame | None = None
        self.overwritten = 0

    def put(self, item: DecodedFrame) -> None:
        with self._cond:
            if self._item is not None:
                self.overwritten += 1
            self._item = item
            self._cond.notify()

    def take(self, timeout: float) -> DecodedFrame | None:
        with self._cond:
            if self._item is None:
                self._cond.wait(timeout)
            item, self._item = self._item, None
            return item


class Geometry:
    """Crop a window / rotate / mirror / fix range / scale, all in I420.

    The window (from auto-framing, or centered) is expressed in the *displayed* orientation and
    mapped back to sensor coordinates, so only the kept pixels are rotated and converted.
    """

    # Full-range (JPEG) → limited-range (video) lookup tables: the device advertises limited range.
    LUT_Y = np.round(16 + np.arange(256) * 219 / 255).astype(np.uint8)
    LUT_C = np.round(128 + (np.arange(256) - 128) * 224 / 255).clip(0, 255).astype(np.uint8)

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.source = ""
        self.oriented = (width, height)     # size of the rotated camera frame (for the framer)
        self.scaler = VideoReformatter()    # persistent swscale context (~10x cheaper than per call)
        self.converter = VideoReformatter()

    def to_i420(self, frame: av.VideoFrame, rotate: int, mirror: bool, window: Window | None = None) -> np.ndarray:
        w, h = self.width, self.height
        fmt = frame.format.name
        full_range = fmt == "yuvj420p" or getattr(frame, "color_range", 0) == 2
        self.source = f"{frame.width}x{frame.height} {fmt}{' full-range' if full_range else ''}"
        if fmt not in ("yuv420p", "yuvj420p"):
            # e.g. yuvj422p from MJPEG webcams; swscale also maps full range to limited here.
            frame = self.converter.reformat(frame, format="yuv420p")
            full_range = False
        fw, fh = frame.width, frame.height
        rotate %= 360
        sideways = rotate in (90, 270)
        ow, oh = (fh, fw) if sideways else (fw, fh)
        self.oriented = (ow, oh)
        win = window or Window()
        bw, bh = Framer.base_size(ow, oh, w / h)
        rw, rh = bw / win.zoom * ow, bh / win.zoom * oh             # window size in oriented px
        x0 = (win.cx * ow - rw / 2)
        y0 = (win.cy * oh - rh / 2)
        x1, y1 = x0 + rw, y0 + rh
        if mirror:                                                  # window is post-mirror
            x0, x1 = ow - x1, ow - x0
        # Oriented rect → source rect (see np.rot90 conventions below).
        if rotate == 90:
            c0, c1, r0, r1 = y0, y1, fh - x1, fh - x0
        elif rotate == 270:
            c0, c1, r0, r1 = fw - y1, fw - y0, x0, x1
        elif rotate == 180:
            c0, c1, r0, r1 = fw - x1, fw - x0, fh - y1, fh - y0
        else:
            c0, c1, r0, r1 = x0, x1, y0, y1
        c0 = max(0, int(c0) & ~1); r0 = max(0, int(r0) & ~1)
        c1 = min(fw, max(c0 + 2, int(round(c1)) & ~1)); r1 = min(fh, max(r0 + 2, int(round(r1)) & ~1))

        identity = (rotate == 0 and not mirror and not full_range and (c0, r0, c1, r1) == (0, 0, fw, fh))
        if identity:
            if (fw, fh) != (w, h):
                frame = self.scaler.reformat(frame, width=w, height=h, interpolation="BILINEAR")
            return frame.to_ndarray()

        packed = frame.to_ndarray()
        y = packed[:fh]
        u = packed[fh:fh + fh // 4].reshape(fh // 2, fw // 2)
        v = packed[fh + fh // 4:].reshape(fh // 2, fw // 2)
        planes = [y[r0:r1, c0:c1], u[r0 // 2:r1 // 2, c0 // 2:c1 // 2], v[r0 // 2:r1 // 2, c0 // 2:c1 // 2]]
        if rotate:
            # np.rot90 k=-1 is clockwise: source (r, c) → oriented (row c, col H-1-r).
            k = {90: -1, 180: 2, 270: 1}[rotate]
            planes = [np.rot90(p, k) for p in planes]
        if mirror:
            planes = [p[:, ::-1] for p in planes]
        if full_range:
            planes = [np.take(self.LUT_Y, planes[0]), np.take(self.LUT_C, planes[1]), np.take(self.LUT_C, planes[2])]
        ph, pw = planes[0].shape
        flat = np.concatenate([np.ascontiguousarray(p).reshape(-1) for p in planes]).reshape(ph * 3 // 2, pw)
        if (pw, ph) != (w, h):
            src = av.VideoFrame.from_ndarray(flat, format="yuv420p")
            flat = self.scaler.reformat(src, width=w, height=h, interpolation="BILINEAR").to_ndarray()
        return flat


class OutputThread(threading.Thread):
    def __init__(self, settings, stats: StageStats, theme_getter) -> None:
        super().__init__(name="tuxlens-output", daemon=True)
        self.settings = settings
        self.stats = stats
        self.theme_getter = theme_getter
        self.effects = Effects()
        self.framer = Framer()
        self.mailbox = Mailbox()
        self.stop_event = threading.Event()
        self.reconfigure_event = threading.Event()
        self.last_frame_at = 0.0
        self.last_output: np.ndarray | None = None
        self.preview_lock = threading.Lock()
        self.preview: np.ndarray | None = None   # latest output frame, for the panel
        self.state = "idle"
        self.phone_rotation = 0          # display rotation the phone attaches to its frames
        self.phone_flip = False
        self._writer: LoopbackWriter | None = None
        self._placeholder: bytes | None = None
        self.error: str | None = None

    # -- device -------------------------------------------------------------------------------
    def _open_device(self) -> None:
        s = self.settings
        if self._writer:
            self._writer.close()
        self._writer = LoopbackWriter(s.device, s.width, s.height, s.fps)
        self.geometry = Geometry(self._writer.width, self._writer.height)
        self._placeholder = None
        self.last_output = None
        log.info("virtual camera %s: %dx%d@%d I420", s.device, self._writer.width, self._writer.height, s.fps)

    def request_reconfigure(self) -> None:
        self.reconfigure_event.set()

    def refresh_placeholder(self) -> None:
        self._placeholder = None

    def _placeholder_bytes(self) -> bytes:
        if self._placeholder is None:
            w = self._writer
            self._placeholder = placeholder.render_i420(w.width, w.height, self.theme_getter(),
                                                        self.settings.extra.get("placeholder_text"),
                                                        self.settings.language)
        return self._placeholder

    # -- loop ---------------------------------------------------------------------------------
    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._open_device()
                self.error = None
                self._loop()
            except OSError as exc:
                self.error = f"{self.settings.device}: {exc.strerror or exc}"
                log.error("virtual camera unavailable: %s (retrying)", self.error)
                self.state = "error"
                self.stop_event.wait(3)

    def _loop(self) -> None:
        idle_period = 1.0 / IDLE_FPS
        next_idle = 0.0
        while not self.stop_event.is_set():
            if self.reconfigure_event.is_set():
                self.reconfigure_event.clear()
                self._open_device()
            item = self.mailbox.take(timeout=idle_period)
            now = time.monotonic()
            if item is not None:
                try:
                    self._emit(item)
                except OSError:
                    raise
                except Exception:  # a bad frame must never kill the virtual camera
                    log.exception("dropping frame %d", item.seq)
                    continue
                self.last_frame_at = now
                self.state = "live"
                continue
            if now < next_idle:
                continue
            next_idle = now + idle_period
            if self.last_output is not None and now - self.last_frame_at < HOLD_LAST_S:
                self._writer.write(self.last_output.data)
                self.state = "holding"
            else:
                self._writer.write(self._placeholder_bytes())
                self.last_output = None
                self.state = "idle"

    def _emit(self, item: DecodedFrame) -> None:
        s = self.settings
        t0 = now_ms()
        rotate = (s.rotate + self.phone_rotation) % 360
        mirror = s.mirror != self.phone_flip
        window = self.framer.window if s.framing else Window(zoom=max(1.0, s.digital_zoom))
        if not s.framing:
            self.framer.reset()
        w, h = self._writer.width, self._writer.height
        i420 = self.geometry.to_i420(item.frame, rotate, mirror, window)
        if self.effects.wants_mask(s) or s.filter != "none" or s.brightness or s.contrast:
            i420 = self.effects.process(i420, w, h, s)
        if s.framing:
            ow, oh = self.geometry.oriented
            self.framer.update(self.effects.last_mask.bbox, ow, oh, w / h, s.framing_zoom, s.framing_speed)
        i420 = np.ascontiguousarray(i420)
        self._writer.write(i420)
        t_out = now_ms()
        self.last_output = i420
        with self.preview_lock:
            self.preview = self.last_output
        st = self.stats
        st.mark_out()
        st.add("decode", item.t_decoded - item.t_recv)
        st.add("process", t_out - t0)
        if item.t_capture_pc is not None:
            st.add("network", item.t_recv - item.t_capture_pc)
            st.add("total", t_out - item.t_capture_pc)

    def stop(self) -> None:
        self.stop_event.set()

"""Background effects (blur / picture), color filters and the person mask used for auto-framing.

Everything runs on the CPU in I420 with numpy and swscale (through PyAV), no extra dependencies:
at 720p the whole stage costs about 4 ms (segmentation ~1.6 ms with 2 threads, blur ~0.3 ms,
blend ~1.5 ms). Key tricks:

* segmentation on a 256x144 thumbnail, every frame (reusing an old mask leaves a halo when moving);
* temporal smoothing that follows fast changes immediately and only damps small flicker;
* the blurred background is built at 1/8 size from the frame with the person *removed*
  (normalized convolution), so the person never smears into the blur;
* persistent swscale contexts: creating one per call costs ~4 ms, reusing it ~0.3 ms;
* int16 blending with a 7-bit mask.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import av
import numpy as np
from av.video.reformatter import VideoReformatter

from .config import data_path

log = logging.getLogger("tuxlens.effects")

MODEL_W, MODEL_H = 256, 144
MODEL_FILE = "selfie_segmenter_landscape.onnx"


@dataclass
class MaskInfo:
    """Person extent inside the processed frame, normalized 0..1 (None when nobody is seen)."""
    bbox: tuple[float, float, float, float] | None   # x0, y0, x1, y1
    coverage: float


def _resize_gray(ref: VideoReformatter, plane: np.ndarray, w: int, h: int, interp: str) -> np.ndarray:
    frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(plane), format="gray")
    return ref.reformat(frame, width=w, height=h, interpolation=interp).to_ndarray()


def _box_blur(img: np.ndarray, radius: int) -> np.ndarray:
    """Separable box blur over the first two axes via cumulative sums (edges clamped).
    Used on 1/8-size images only; extra trailing axes (channels) are blurred independently."""
    if radius < 1:
        return img
    k = 2 * radius + 1
    rest = [(0, 0)] * (img.ndim - 2)
    pad = np.pad(img, [(radius + 1, radius), (0, 0), *rest], mode="edge")
    c = np.cumsum(pad, axis=0)
    img = (c[k:] - c[:-k]) / k
    pad = np.pad(img, [(0, 0), (radius + 1, radius), *rest], mode="edge")
    c = np.cumsum(pad, axis=1)
    return (c[:, k:] - c[:, :-k]) / k


class Segmenter:
    def __init__(self) -> None:
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2      # measured best on a 6-core CPU (1.6 ms vs 2.5 ms single)
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Idle threads would otherwise busy-wait between frames and burn a core for nothing.
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        path = data_path("assets", "models", MODEL_FILE)
        self.session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.thumb = VideoReformatter()
        self.prev: np.ndarray | None = None
        log.info("segmentation model loaded (%s)", path.name)

    def run(self, i420: np.ndarray) -> np.ndarray:
        """Person probability at MODEL_H x MODEL_W, float32 0..1, temporally smoothed."""
        frame = av.VideoFrame.from_ndarray(i420, format="yuv420p")
        rgb = self.thumb.reformat(frame, width=MODEL_W, height=MODEL_H, format="rgb24",
                                  interpolation="BILINEAR").to_ndarray()
        x = rgb.astype(np.float32).transpose(2, 0, 1)[None] * (1 / 255)
        alpha = self.session.run(None, {self.input: x})[0][0, 0]
        if self.prev is not None and self.prev.shape == alpha.shape:
            # Follow big changes at once (movement), damp small ones (edge flicker).
            diff = np.abs(alpha - self.prev)
            a = np.clip(0.45 + diff * 2.5, 0.45, 1.0)
            alpha = self.prev + (alpha - self.prev) * a
        self.prev = alpha
        return alpha

    def reset(self) -> None:
        self.prev = None


class Effects:
    def __init__(self) -> None:
        self._segmenter: Segmenter | None = None
        self._segmenter_failed = False
        self.mask_up = VideoReformatter()
        self.mask_up_c = VideoReformatter()
        self.blur_down = [VideoReformatter() for _ in range(3)]
        self.blur_up = [VideoReformatter() for _ in range(3)]
        self.mask_down = [VideoReformatter() for _ in range(3)]   # one context per size: no re-init
        self._bg_key = None
        self._bg_planes: list[np.ndarray] | None = None
        self._lut_key = None
        self._lut_y = self._lut_c = None
        self._bufs: dict = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tuxlens-seg")
        self._job = None
        self._alpha: np.ndarray | None = None
        self.lock = threading.Lock()
        self.last_mask: MaskInfo = MaskInfo(None, 0.0)
        self.error: str | None = None

    # -- helpers ------------------------------------------------------------------------------
    @property
    def segmenter(self) -> Segmenter | None:
        if self._segmenter is None and not self._segmenter_failed:
            try:
                self._segmenter = Segmenter()
            except Exception as exc:  # missing onnxruntime or model: effects degrade to none
                self._segmenter_failed = True
                self.error = f"segmentation unavailable: {exc}"
                log.error(self.error)
        return self._segmenter

    def _background_image(self, path: str, w: int, h: int) -> list[np.ndarray] | None:
        key = (path, w, h)
        if key == self._bg_key:
            return self._bg_planes
        self._bg_key, self._bg_planes = key, None
        try:
            with av.open(path) as container:
                img = next(container.decode(video=0))
        except Exception as exc:
            self.error = f"background image: {exc}"
            log.warning(self.error)
            return None
        # Cover: scale so the image fills the frame, then center crop.
        scale = max(w / img.width, h / img.height)
        sw, sh = max(w, round(img.width * scale)) & ~1, max(h, round(img.height * scale)) & ~1
        packed = img.reformat(width=sw, height=sh, format="yuv420p", interpolation="BICUBIC").to_ndarray()
        y = packed[:sh]
        u = packed[sh:sh + sh // 4].reshape(sh // 2, sw // 2)
        v = packed[sh + sh // 4:].reshape(sh // 2, sw // 2)
        x0, y0 = ((sw - w) // 2) & ~1, ((sh - h) // 2) & ~1
        self._bg_planes = [np.ascontiguousarray(y[y0:y0 + h, x0:x0 + w]),
                           np.ascontiguousarray(u[y0 // 2:(y0 + h) // 2, x0 // 2:(x0 + w) // 2]),
                           np.ascontiguousarray(v[y0 // 2:(y0 + h) // 2, x0 // 2:(x0 + w) // 2])]
        self.error = None
        return self._bg_planes

    def _blurred(self, planes: list[np.ndarray], alpha: np.ndarray, strength: float) -> list[np.ndarray]:
        """Background blur from the frame with the person removed (no halo around the subject).

        Built at 1/8 size with a normalized box blur: blur(img * w) / blur(w), w = 1 - person.
        U and V are processed together as one two-channel array to halve the per-call overhead.
        """
        radius = 1 + int(round(strength * 5))
        y, u, v = planes
        h, w = y.shape
        out = []
        for idx, (stack, (ph, pw)) in enumerate((([y], (h, w)), ([u, v], (h // 2, w // 2)))):
            sw, sh = max(8, pw // 8) & ~1, max(8, ph // 8) & ~1
            small = np.stack([_resize_gray(self.blur_down[idx + i], p, sw, sh, "AREA") for i, p in enumerate(stack)],
                             axis=-1).astype(np.float32)
            weight = 1.0 - _resize_gray(self.mask_down[idx], (alpha * 255).astype(np.uint8), sw, sh,
                                        "AREA").astype(np.float32)[..., None] * (1 / 255)
            weight *= weight
            num = _box_blur(small * weight, radius)
            den = _box_blur(weight, radius)
            blurred = num / np.maximum(den, 1e-3)
            low = den < 0.02            # surrounded by the person: fall back to the plain blur
            if low.any():
                plain = _box_blur(small, radius)
                blurred = np.where(low, plain, blurred)
            blurred = np.clip(blurred, 0, 255).astype(np.uint8)
            for i in range(len(stack)):
                out.append(_resize_gray(self.blur_up[idx + i], blurred[..., i], pw, ph, "BILINEAR"))
        return out

    def _luts(self, s) -> tuple[np.ndarray | None, np.ndarray | None]:
        key = (s.filter, round(s.brightness, 3), round(s.contrast, 3))
        if key == self._lut_key:
            return self._lut_y, self._lut_c
        self._lut_key = key
        lut_y = lut_c = None
        if s.brightness or s.contrast:
            v = np.arange(256, dtype=np.float32)
            v = (v - 128) * (1 + s.contrast) + 128 + s.brightness * 40
            lut_y = np.clip(v, 16, 235).astype(np.uint8)
        if s.filter == "vivid":
            lut_c = np.clip((np.arange(256) - 128) * 1.35 + 128, 16, 240).astype(np.uint8)
        self._lut_y, self._lut_c = lut_y, lut_c
        return lut_y, lut_c

    # -- main entry ---------------------------------------------------------------------------
    def wants_mask(self, s) -> bool:
        return s.effect in ("blur", "image") or s.framing

    def process(self, i420: np.ndarray, width: int, height: int, s) -> np.ndarray:
        h, w = height, width
        y = i420[:h]
        u = i420[h:h + h // 4].reshape(h // 2, w // 2)
        v = i420[h + h // 4:].reshape(h // 2, w // 2)
        planes = [y, u, v]
        changed = False

        if self.wants_mask(s) and self.segmenter is not None:
            alpha = self._latest_mask(i420)
            bg = None
            if s.effect == "blur":
                bg = self._blurred(planes, alpha, s.blur_strength)
            if s.effect == "image" and s.background_image:
                bg = self._background_image(s.background_image, w, h)
            if bg is not None:
                planes = self._composite(planes, bg, alpha, w, h)
                changed = True
        elif self._segmenter is not None and self._alpha is not None:
            self._segmenter.reset()
            self._alpha = None

        if s.filter in ("mono", "warm", "cool") or s.filter == "vivid" or s.brightness or s.contrast:
            planes = self._filters(planes, s)
            changed = True

        if not changed:
            return i420
        return np.concatenate([p.reshape(-1) for p in planes]).reshape(h * 3 // 2, w)

    def _buffers(self, plane: int, shape) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        # Keyed by plane *and* shape: U and V have the same size and must not share a buffer.
        key = (plane, shape)
        bufs = self._bufs.get(key)
        if bufs is None:
            bufs = (np.empty(shape, np.int16), np.empty(shape, np.int16), np.empty(shape, np.uint8))
            self._bufs[key] = bufs
        return bufs

    def _latest_mask(self, i420: np.ndarray) -> np.ndarray:
        """Segmentation runs in the background on the newest frame; compositing uses the most
        recent finished mask (at most one frame old). Keeps the model off the critical path."""
        job = self._job
        if job is not None and job.done():
            try:
                self._alpha = job.result()
                self.last_mask = self._mask_info(self._alpha)
            except Exception as exc:
                log.warning("segmentation failed: %s", exc)
            job = None
        if job is None:
            self._job = self._pool.submit(self.segmenter.run, i420)
        if self._alpha is None:                     # first frame: wait for a mask
            self._alpha = self._job.result()
            self.last_mask = self._mask_info(self._alpha)
            self._job = None
        return self._alpha

    def _composite(self, planes, bg, alpha, w, h):
        # Soften the matte edge: smoothstep around 0.5 keeps hair soft and the inside solid.
        a = np.clip((alpha - 0.25) * 2.0, 0.0, 1.0)
        a = a * a * (3 - 2 * a)
        a7 = (a * 128).astype(np.uint8)
        m_full = _resize_gray(self.mask_up, a7, w, h, "BILINEAR")
        m_half = _resize_gray(self.mask_up_c, a7, w // 2, h // 2, "BILINEAR")
        out = []
        # out = back + ((fg - back) * m) >> 7, in place on preallocated int16 buffers (7-bit mask
        # keeps |(fg - back) * m| < 2^15).
        for i, (fg, back, m) in enumerate(zip(planes, bg, (m_full, m_half, m_half))):
            d, mm, result = self._buffers(i, fg.shape)
            np.subtract(fg, back, out=d, dtype=np.int16)
            np.copyto(mm, m, casting="unsafe")
            np.multiply(d, mm, out=d)
            np.right_shift(d, 7, out=d)
            np.add(d, back, out=d, casting="unsafe")
            np.copyto(result, d, casting="unsafe")
            out.append(result)
        return out

    def _filters(self, planes, s):
        y, u, v = planes
        lut_y, lut_c = self._luts(s)
        if lut_y is not None:
            y = np.take(lut_y, y)
        if s.filter == "mono":
            u = np.full_like(u, 128)
            v = np.full_like(v, 128)
        elif s.filter in ("warm", "cool"):
            shift = 9 if s.filter == "warm" else -9
            u = np.clip(u.astype(np.int16) - shift, 16, 240).astype(np.uint8)
            v = np.clip(v.astype(np.int16) + shift, 16, 240).astype(np.uint8)
        elif lut_c is not None:
            u = np.take(lut_c, u)
            v = np.take(lut_c, v)
        return [y, u, v]

    @staticmethod
    def _mask_info(alpha: np.ndarray) -> MaskInfo:
        solid = alpha > 0.5
        coverage = float(solid.mean())
        if coverage < 0.01:
            return MaskInfo(None, coverage)
        rows = np.flatnonzero(solid.any(axis=1))
        # Columns weighted to the upper body: the head/shoulders define the framing, not arms.
        upper = solid[: max(1, rows[0] + (rows[-1] - rows[0]) // 2 + 1)]
        cols = np.flatnonzero(upper.any(axis=0))
        if cols.size == 0:
            cols = np.flatnonzero(solid.any(axis=0))
        hh, ww = alpha.shape
        return MaskInfo((cols[0] / ww, rows[0] / hh, (cols[-1] + 1) / ww, (rows[-1] + 1) / hh), coverage)

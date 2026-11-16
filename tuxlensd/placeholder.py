"""The frame meeting participants see while the phone is not streaming, drawn from the theme."""
from __future__ import annotations

import locale
import math
import os

import cairo
import numpy as np

from .themes import hex_to_rgb

TEXTS = {"es": "Cámara en pausa", "en": "Camera paused", "fr": "Caméra en pause",
         "de": "Kamera pausiert", "it": "Fotocamera in pausa", "pt": "Câmera em pausa"}


OFF_TEXTS = {"es": "Cámara apagada", "en": "Camera off", "fr": "Caméra éteinte", "de": "Kamera aus",
             "it": "Fotocamera spenta", "pt": "Câmera desligada"}


def off_text(language: str | None = None) -> str:
    code = system_language() if not language or language == "auto" else language[:2]
    return OFF_TEXTS.get(code, OFF_TEXTS["en"])


def system_language() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value and value not in ("C", "POSIX"):
            return value[:2].lower()
    return (locale.getlocale()[0] or "en")[:2]


def default_text(language: str | None = None) -> str:
    code = system_language() if not language or language == "auto" else language[:2]
    return TEXTS.get(code, TEXTS["en"])


def render_rgb(width: int, height: int, theme: dict, text: str | None = None,
               language: str | None = None) -> np.ndarray:
    c = theme["colors"]
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    cr = cairo.Context(surface)
    scale = height / 720

    bg = hex_to_rgb(c["background"])
    grad = cairo.RadialGradient(width / 2, height / 2, 0, width / 2, height / 2, width * 0.7)
    lift = 0.04 if theme.get("dark", True) else -0.03
    grad.add_color_stop_rgb(0, *(min(1, max(0, v + lift)) for v in bg))
    grad.add_color_stop_rgb(1, *bg)
    cr.set_source(grad)
    cr.paint()

    # Camera-off glyph: rounded body + lens, drawn with strokes so it follows the theme.
    cx, cy = width / 2, height / 2 - 34 * scale
    w, h, r = 120 * scale, 82 * scale, 18 * scale
    muted = hex_to_rgb(c["muted"])
    cr.set_line_width(5 * scale)
    cr.set_source_rgb(*muted)
    x0, y0 = cx - w / 2 - 14 * scale, cy - h / 2
    cr.new_sub_path()
    cr.arc(x0 + w - r, y0 + r, r, -math.pi / 2, 0)
    cr.arc(x0 + w - r, y0 + h - r, r, 0, math.pi / 2)
    cr.arc(x0 + r, y0 + h - r, r, math.pi / 2, math.pi)
    cr.arc(x0 + r, y0 + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.stroke()
    cr.move_to(x0 + w + 8 * scale, cy - 14 * scale)
    cr.line_to(x0 + w + 34 * scale, cy - 28 * scale)
    cr.line_to(x0 + w + 34 * scale, cy + 28 * scale)
    cr.line_to(x0 + w + 8 * scale, cy + 14 * scale)
    cr.close_path()
    cr.stroke()
    cr.set_source_rgb(*hex_to_rgb(c["danger"]))
    cr.move_to(cx - 80 * scale, cy - 62 * scale)
    cr.line_to(cx + 80 * scale, cy + 62 * scale)
    cr.stroke()

    cr.set_source_rgb(*hex_to_rgb(c["text_secondary"]))
    cr.select_font_face(theme.get("font", "Inter"), cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    cr.set_font_size(30 * scale)
    label = text or default_text(language)
    ext = cr.text_extents(label)
    cr.move_to(width / 2 - ext.width / 2 - ext.x_bearing, height / 2 + 100 * scale)
    cr.show_text(label)

    surface.flush()
    buf = np.ndarray((height, surface.get_stride() // 4, 4), np.uint8, surface.get_data())[:, :width]
    return buf[:, :, [2, 1, 0]].copy()   # BGRx → RGB


def rgb_to_i420(rgb: np.ndarray) -> bytes:
    """BT.709 limited range, matching what the device advertises."""
    f = rgb.astype(np.float32)
    r, g, b = f[..., 0], f[..., 1], f[..., 2]
    y = 16 + 0.1826 * r + 0.6142 * g + 0.0620 * b
    u = 128 - 0.1006 * r - 0.3386 * g + 0.4392 * b
    v = 128 + 0.4392 * r - 0.3989 * g - 0.0403 * b
    h, w = y.shape
    u = u.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    v = v.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
    planes = [np.clip(p, 0, 255).astype(np.uint8) for p in (y, u, v)]
    return b"".join(p.tobytes() for p in planes)


def render_i420(width: int, height: int, theme: dict, text: str | None = None,
                language: str | None = None) -> bytes:
    return rgb_to_i420(render_rgb(width, height, theme, text, language))

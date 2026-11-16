"""Core sanity tests: run with `python3 -m unittest discover tests`."""
import sys
import unittest
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuxlensd import themes  # noqa: E402
from tuxlensd.config import Settings  # noqa: E402
from tuxlensd.framing import Framer, Window  # noqa: E402
from tuxlensd.pipeline import Geometry  # noqa: E402


def sensor_frame(w=1280, h=720):
    packed = np.full((h * 3 // 2, w), 128, np.uint8)
    packed[:h] = 40
    packed[0:100, 0:200] = 235          # bright marker in the sensor's top-left corner
    return av.VideoFrame.from_ndarray(packed, format="yuv420p")


class GeometryTest(unittest.TestCase):
    def test_every_rotation_and_window_gives_output_size(self):
        g = Geometry(1280, 720)
        for rot in (0, 90, 180, 270):
            for mirror in (False, True):
                for win in (Window(), Window(0.3, 0.2, 1.5)):
                    self.assertEqual(g.to_i420(sensor_frame(), rot, mirror, win).shape, (1080, 1280))

    def test_clockwise_rotation_moves_top_left_to_top_right(self):
        y = Geometry(1280, 720).to_i420(sensor_frame(), 90, False, Window(0.5, 0.0, 1.0))[:720]
        self.assertGreater(y[:40, -40:].mean(), 200)
        self.assertLess(y[:40, :40].mean(), 100)


class EffectsTest(unittest.TestCase):
    def test_chroma_planes_stay_independent(self):
        """Regression: U and V once shared a buffer, turning skin purple."""
        from tuxlensd.effects import Effects
        i420 = np.empty((1080, 1280), np.uint8)
        i420[:720], i420[720:900], i420[900:] = 120, 100, 160
        s = Settings()
        s.effect = "blur"
        out = Effects().process(i420, 1280, 720, s)
        self.assertAlmostEqual(out[720:900].mean(), 100, delta=1)
        self.assertAlmostEqual(out[900:].mean(), 160, delta=1)


class FramerTest(unittest.TestCase):
    def test_window_stays_inside_frame(self):
        f = Framer()
        for _ in range(200):
            f.update((0.0, 0.0, 0.1, 0.4), 720, 1280, 16 / 9, 2.0, 1.0)
        bw, bh = Framer.base_size(720, 1280, 16 / 9)
        self.assertGreaterEqual(f.window.cx - bw / f.window.zoom / 2, -1e-6)
        self.assertGreaterEqual(f.window.cy - bh / f.window.zoom / 2, -1e-6)


class ThemeTest(unittest.TestCase):
    def test_bundled_themes_load_with_all_tokens(self):
        for name in themes.available():
            theme = themes.load(name)
            for key in themes.BASE_TOKENS["colors"]:
                self.assertIn(key, theme["colors"], f"{name} lacks {key}")


if __name__ == "__main__":
    unittest.main()

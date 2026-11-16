"""The phone microphone as a PipeWire/PulseAudio source.

The phone sends raw 48 kHz mono s16le PCM in 10 ms chunks (no codec: zero encoding delay, and
768 kbit/s is nothing on a LAN). A `module-pipe-source` exposes a virtual microphone fed by a FIFO.

The FIFO doubles as the jitter buffer. Phone and PC clocks drift slightly, so the backlog is
checked before every write and chunks are dropped above MAX_BACKLOG_MS: latency can never creep
up during a long call.
"""
from __future__ import annotations

import fcntl
import logging
import os
import struct
import subprocess
import termios

import numpy as np

from .config import RUNTIME_DIR

log = logging.getLogger("tuxlens.audio")

RATE = 48000
BYTES_PER_MS = RATE * 2 // 1000
MAX_BACKLOG_MS = 60
SOURCE_NAME = "tuxlens_mic"
HEADER = struct.Struct("<Id")        # u32 seq, f64 capture time (phone ms)
DESCRIPTIONS = {"es": "TuxLens (micrófono del iPhone)", "en": "TuxLens (phone microphone)"}


def _pactl(*args: str) -> str:
    return subprocess.run(["pactl", *args], capture_output=True, text=True, check=True).stdout.strip()


class PhoneMic:
    def __init__(self) -> None:
        self.module: int | None = None
        self.fd = -1
        self.fifo = RUNTIME_DIR / "mic.fifo"
        self.chunks = 0
        self.dropped = 0
        self.last_seq: int | None = None
        self.lost = 0
        self.level_db = -90.0          # peak level after gain, smoothed, for the panel meter
        self.clipping = 0
        self.error: str | None = None

    @property
    def active(self) -> bool:
        return self.module is not None

    def start(self, language: str = "en") -> None:
        if self.module is not None:
            return
        self._unload_stale()
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fifo.unlink(missing_ok=True)
        desc = DESCRIPTIONS.get(language, DESCRIPTIONS["en"]).replace('"', "")
        try:
            out = _pactl("load-module", "module-pipe-source", f"source_name={SOURCE_NAME}", f"file={self.fifo}",
                         "format=s16le", f"rate={RATE}", "channels=1",
                         f'source_properties=device.description="{desc}" device.icon_name="audio-input-microphone"')
            self.module = int(out.splitlines()[-1])
        except (OSError, subprocess.CalledProcessError, ValueError) as exc:
            self.error = f"virtual microphone: {exc}"
            log.error(self.error)
            return
        try:
            self.fd = os.open(self.fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            self.error = f"virtual microphone fifo: {exc}"
            log.error(self.error)
            self.stop()
            return
        self.error = None
        log.info("virtual microphone ready (%s)", SOURCE_NAME)

    def stop(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if self.module is not None:
            try:
                _pactl("unload-module", str(self.module))
            except (OSError, subprocess.CalledProcessError):
                pass
            self.module = None
            log.info("virtual microphone removed")
        self.fifo.unlink(missing_ok=True)

    def _unload_stale(self) -> None:
        """A crashed daemon may have left its module loaded."""
        try:
            for line in _pactl("list", "modules", "short").splitlines():
                parts = line.split("\t")
                if len(parts) > 2 and parts[1] == "module-pipe-source" and f"source_name={SOURCE_NAME}" in parts[2]:
                    _pactl("unload-module", parts[0])
        except (OSError, subprocess.CalledProcessError):
            pass

    def backlog_ms(self) -> float:
        if self.fd < 0:
            return 0.0
        try:
            buf = fcntl.ioctl(self.fd, termios.FIONREAD, b"\0\0\0\0")
            return struct.unpack("i", buf)[0] / BYTES_PER_MS
        except OSError:
            return 0.0

    def feed(self, message: bytes, gain_db: float = 0.0, mute: bool = False) -> None:
        if self.fd < 0 or len(message) <= HEADER.size:
            return
        seq, _t = HEADER.unpack_from(message)
        if self.last_seq is not None and seq != self.last_seq + 1:
            self.lost += max(0, seq - self.last_seq - 1)
        self.last_seq = seq
        samples = np.frombuffer(message, dtype="<i2", offset=HEADER.size)
        if mute:
            samples = np.zeros_like(samples)
        elif gain_db:
            boosted = samples.astype(np.float32) * (10 ** (max(-12.0, min(18.0, gain_db)) / 20))
            if np.abs(boosted).max() > 32767:
                self.clipping += 1
            samples = np.clip(boosted, -32768, 32767).astype("<i2")
        peak = int(np.abs(samples.astype(np.int32)).max()) if samples.size else 0
        db = 20 * np.log10(max(peak, 1) / 32768)
        # fast attack, slow release, like a hardware meter
        self.level_db = db if db > self.level_db else self.level_db * 0.9 + db * 0.1
        pcm = samples.tobytes()
        if self.backlog_ms() > MAX_BACKLOG_MS:
            self.dropped += 1          # clock drift / burst: keep latency bounded
            return
        try:
            os.write(self.fd, pcm)
            self.chunks += 1
        except BlockingIOError:
            self.dropped += 1
        except OSError as exc:
            self.error = f"virtual microphone: {exc}"

    def stats(self) -> dict:
        return {"active": self.active, "chunks": self.chunks, "dropped": self.dropped, "lost": self.lost,
                "backlog_ms": round(self.backlog_ms(), 1), "level_db": round(self.level_db, 1),
                "clipping": self.clipping, "error": self.error}

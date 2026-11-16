"""Local cameras (USB webcams, laptop cameras, capture cards) as an alternative to the phone.

Frames from a V4L2 device go into the same one-slot mailbox as the phone's, so every effect
(background blur/picture, filters, auto-framing, digital zoom) works the same. Phone-only
controls (lenses, torch) are disabled in the panel for these sources.
"""
from __future__ import annotations

import fcntl
import logging
import os
import struct
import threading
import time
from pathlib import Path

import av
from av.codec.context import Flags

from .stats import now_ms

log = logging.getLogger("tuxlens.sources")

PHONE = "phone"
VIDIOC_QUERYCAP = (2 << 30) | (104 << 16) | (ord("V") << 8) | 0
V4L2_CAP_VIDEO_CAPTURE = 0x00000001
V4L2_CAP_DEVICE_CAPS = 0x80000000
VIRTUAL_DRIVERS = {"v4l2 loopback", "v4l2loopback"}


def _querycap(path: str) -> dict | None:
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return None
    buf = bytearray(104)
    try:
        fcntl.ioctl(fd, VIDIOC_QUERYCAP, buf)      # fills buf in place
    except OSError:
        return None
    finally:
        os.close(fd)
    driver, card, bus = (bytes(buf[o:o + n]).split(b"\0", 1)[0].decode(errors="replace")
                         for o, n in ((0, 16), (16, 32), (48, 32)))
    caps, device_caps = struct.unpack_from("<II", buf, 84)
    effective = device_caps if caps & V4L2_CAP_DEVICE_CAPS else caps
    return {"driver": driver, "card": card, "bus": bus, "capture": bool(effective & V4L2_CAP_VIDEO_CAPTURE)}


def _stable_ids() -> dict[str, str]:
    """/dev/videoN → /dev/v4l/by-id/… link (survives replugging and renumbering)."""
    ids = {}
    by_id = Path("/dev/v4l/by-id")
    if by_id.is_dir():
        for link in sorted(by_id.iterdir()):
            if link.name.endswith("-index0"):
                ids[os.path.realpath(link)] = str(link)
    return ids


def list_cameras(exclude: str | None = None) -> list[dict]:
    """Real capture devices, without virtual loopback cameras (TuxLens itself, OBS, Iriun…)."""
    cams = []
    stable = _stable_ids()
    skip = os.path.realpath(exclude) if exclude else None
    for dev in sorted(Path("/dev").glob("video*"), key=lambda p: int(p.name[5:] or 0) if p.name[5:].isdigit() else 999):
        path = str(dev)
        if os.path.realpath(path) == skip:
            continue
        info = _querycap(path)
        if not info or not info["capture"] or info["driver"].lower() in VIRTUAL_DRIVERS:
            continue
        ident = stable.get(os.path.realpath(path), path)
        if any(c["id"] == ident for c in cams):
            continue                      # second node of the same camera (metadata)
        cams.append({"id": ident, "name": info["card"].split(":")[0].strip() or path, "device": path, "kind": "v4l2"})
    return cams


class LocalCamera(threading.Thread):
    """Reads a V4L2 camera and feeds decoded frames to a callback, never queueing."""

    def __init__(self, camera_id: str, width: int, height: int, fps: int, on_frame) -> None:
        super().__init__(name="tuxlens-local-camera", daemon=True)
        self.camera_id = camera_id
        self.width, self.height, self.fps = width, height, fps
        self.on_frame = on_frame
        self.stop_event = threading.Event()
        self.error: str | None = None
        self.mode = ""
        self.frames = 0

    def _open(self):
        attempts = [
            {"input_format": "mjpeg", "video_size": f"{self.width}x{self.height}", "framerate": str(self.fps)},
            {"input_format": "mjpeg", "video_size": f"{self.width}x{self.height}"},
            {"video_size": f"{self.width}x{self.height}"},
            {},
        ]
        last = None
        for opts in attempts:
            try:
                container = av.open(self.camera_id, format="v4l2", options=opts | {"fflags": "nobuffer"},
                                    timeout=3)
                return container
            except (av.error.FFmpegError, OSError) as exc:
                last = exc
        raise last or OSError("cannot open camera")

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                container = self._open()
            except Exception as exc:
                self.error = f"{self.camera_id}: {exc}"
                log.error("local camera: %s (retrying)", self.error)
                self.stop_event.wait(2)
                continue
            self.error = None
            try:
                stream = container.streams.video[0]
                ctx = stream.codec_context
                ctx.flags |= Flags.low_delay
                ctx.thread_type = "SLICE"      # frame threading would hold frames back
                self.mode = f"{ctx.name} {stream.width}x{stream.height}@{float(stream.average_rate or 0):.0f}"
                log.info("local camera %s: %s", self.camera_id, self.mode)
                for packet in container.demux(stream):
                    if self.stop_event.is_set():
                        break
                    t_recv = now_ms()
                    for frame in packet.decode():
                        self.frames += 1
                        self.on_frame(frame, self.frames, t_recv, now_ms())
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.error = f"{self.camera_id}: {exc}"
                    log.warning("local camera stopped: %s", exc)
                    time.sleep(1)
            finally:
                container.close()

    def stop(self) -> None:
        self.stop_event.set()


# ------------------------------------------------------------------------------- camera controls
# A curated subset of V4L2 controls, shown only when the camera has them.
CONTROL_ORDER = [
    "zoom_absolute", "focus_automatic_continuous", "focus_absolute", "auto_exposure",
    "exposure_time_absolute", "brightness", "contrast", "saturation", "sharpness",
    "white_balance_automatic", "white_balance_temperature", "backlight_compensation",
    "power_line_frequency",
]


def read_controls(device: str) -> list[dict]:
    """Parse `v4l2-ctl -L` (controls + menu entries). Empty list if v4l2-ctl is missing."""
    import re
    import shutil
    import subprocess
    if not shutil.which("v4l2-ctl"):
        return []
    try:
        out = subprocess.run(["v4l2-ctl", "-d", device, "-L"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    controls: dict[str, dict] = {}
    current = None
    head = re.compile(r"^\s*(\w+) 0x[0-9a-f]+ \((\w+)\)\s*:(.*)$")
    item = re.compile(r"^\s+(\d+): (.+)$")
    for line in out.splitlines():
        m = head.match(line)
        if m:
            name, kind, rest = m.groups()
            fields = dict(re.findall(r"(\w+)=(-?\w+)", rest))
            current = {"name": name, "type": kind, "flags": fields.get("flags", "")}
            for key in ("min", "max", "step", "default", "value"):
                if key in fields:
                    try:
                        current[key] = int(fields[key])
                    except ValueError:
                        pass
            current["inactive"] = "inactive" in rest
            if kind == "menu":
                current["options"] = []
            controls[name] = current
            continue
        m = item.match(line)
        if m and current is not None and current["type"] == "menu":
            current["options"].append({"value": int(m.group(1)), "label": m.group(2).strip()})
    return [controls[n] for n in CONTROL_ORDER if n in controls]


def set_control(device: str, name: str, value: int) -> bool:
    import subprocess
    if name not in CONTROL_ORDER:
        return False
    try:
        return subprocess.run(["v4l2-ctl", "-d", device, "-c", f"{name}={int(value)}"],
                              capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False

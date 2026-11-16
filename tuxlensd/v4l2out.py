"""Writer side of a v4l2loopback device.

Frames go out as I420 (V4L2 'YU12'): it is the first format Chromium's V4L2 capture asks for, so
Chrome/Meet consume it without any conversion. Plain write() costs one ~1.4 MB memcpy per 720p
frame, which is well under a millisecond.

The device is opened once and never closed while the daemon runs: with exclusive_caps=1 the
device stops advertising CAPTURE when there is no writer, and Chrome would drop the camera.
"""
from __future__ import annotations

import fcntl
import os
import struct

V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_FIELD_NONE = 1
V4L2_COLORSPACE_REC709 = 3
V4L2_YCBCR_ENC_709 = 2
V4L2_QUANTIZATION_LIM_RANGE = 2


def _fourcc(code: str) -> int:
    return struct.unpack("<I", code.encode())[0]


def _iowr(nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord("V") << 8) | nr


FMT_SIZE = 208     # struct v4l2_format on 64-bit (u32 type + pad + 200-byte union)
PARM_SIZE = 204    # struct v4l2_streamparm
VIDIOC_S_FMT = _iowr(5, FMT_SIZE)
VIDIOC_S_PARM = _iowr(22, PARM_SIZE)
PIX_YU12 = _fourcc("YU12")


class LoopbackWriter:
    def __init__(self, device: str, width: int, height: int, fps: int):
        self.device = device
        self.width = width & ~1
        self.height = height & ~1
        self.fps = fps
        self.frame_size = self.width * self.height * 3 // 2
        self.fd = os.open(device, os.O_RDWR)
        self._configure()

    def _configure(self) -> None:
        pix = struct.pack(
            "<12I",
            self.width, self.height, PIX_YU12, V4L2_FIELD_NONE,
            self.width, self.frame_size, V4L2_COLORSPACE_REC709, 0,
            0, V4L2_YCBCR_ENC_709, V4L2_QUANTIZATION_LIM_RANGE, 0,
        )
        fmt = bytearray(FMT_SIZE)
        struct.pack_into("<I", fmt, 0, V4L2_BUF_TYPE_VIDEO_OUTPUT)
        fmt[8:8 + len(pix)] = pix
        fcntl.ioctl(self.fd, VIDIOC_S_FMT, fmt)

        parm = bytearray(PARM_SIZE)
        # type, then v4l2_outputparm: capability, outputmode, timeperframe{num, den}
        struct.pack_into("<IIIII", parm, 0, V4L2_BUF_TYPE_VIDEO_OUTPUT, 0, 0, 1, self.fps)
        try:
            fcntl.ioctl(self.fd, VIDIOC_S_PARM, parm)
        except OSError:
            pass  # frame rate is advisory for v4l2loopback

    def write(self, i420) -> None:
        view = memoryview(i420).cast("B")
        if view.nbytes != self.frame_size:
            raise ValueError(f"frame is {view.nbytes} bytes, device expects {self.frame_size}")
        os.write(self.fd, view)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

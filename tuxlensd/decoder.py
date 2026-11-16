"""H.264 decoding with zero reordering delay.

AV_CODEC_FLAG_LOW_DELAY matters twice: it disables libavcodec frame threading (which would hold
N-1 frames back) and makes the h264 decoder output each picture as soon as it is decoded. The
phone encodes without B-frames, so this is safe.
"""
from __future__ import annotations

import av
from av.codec.context import Flags


class H264Decoder:
    def __init__(self, hardware: bool = False) -> None:
        self.hardware = hardware
        self._open()

    def _open(self) -> None:
        name = "h264_cuvid" if self.hardware else "h264"
        self.ctx = av.CodecContext.create(name, "r")
        self.ctx.flags |= Flags.low_delay
        if not self.hardware:
            self.ctx.thread_type = "SLICE"
            self.ctx.thread_count = 0
        self.packets = 0
        self.frames = 0

    def reset(self) -> None:
        self._open()

    def decode(self, annexb: bytes) -> list[av.VideoFrame]:
        self.packets += 1
        frames = self.ctx.decode(av.Packet(annexb))
        self.frames += len(frames)
        return frames

    @property
    def lag(self) -> int:
        """Packets fed minus frames produced: should stay at 0 with a low-delay stream."""
        return self.packets - self.frames

#!/usr/bin/env python3
"""Simulated phone for testing the daemon without an iPhone.

Encodes a test pattern with x264 (baseline, zerolatency, AUD markers), splits it into access
units and sends them over the same WebSocket protocol the phone page uses, answering clock pings.

    python3 tools/fake-phone.py --seconds 10 [--host localhost] [--size 1280x720] [--fps 30]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import struct
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tuxlensd.config import pairing_token  # noqa: E402

HEADER = struct.Struct("<BBHIdd")
AUD = b"\x00\x00\x00\x01\x09"


def now_ms() -> float:
    return time.time() * 1000


async def access_units(proc):
    buf = b""
    while True:
        chunk = await proc.stdout.read(65536)
        if not chunk:
            if buf:
                yield buf
            return
        buf += chunk
        while True:
            idx = buf.find(AUD, 1)
            if idx < 0:
                break
            yield buf[:idx]
            buf = buf[idx:]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--port", type=int, default=7790)
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--clock-skew-ms", type=float, default=0.0, help="pretend the phone clock is off")
    args = ap.parse_args()

    token = pairing_token()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    base = f"wss://{args.host}:{args.port}"

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-f", "lavfi",
           "-i", f"testsrc2=size={args.size}:rate={args.fps}", "-t", str(args.seconds),
           "-c:v", "libx264", "-profile:v", "baseline", "-preset", "ultrafast", "-tune", "zerolatency",
           "-x264-params", f"aud=1:keyint={args.fps * 3}:repeat-headers=1", "-b:v", "6M",
           "-f", "h264", "-"]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE)

    async with aiohttp.ClientSession() as session:
        control = await session.ws_connect(f"{base}/ws/control?t={token}", ssl=ctx)
        video = await session.ws_connect(f"{base}/ws/video?t={token}", ssl=ctx)
        await control.send_str(json.dumps({"type": "hello", "name": "fake-phone", "cameras": []}))

        async def control_loop():
            async for msg in control:
                data = json.loads(msg.data)
                if data.get("type") == "ping":
                    await control.send_str(json.dumps({"type": "pong", "t": data["t"],
                                                       "tp": now_ms() + args.clock_skew_ms}))
                elif data.get("type") == "config":
                    print("config from PC:", data)

        async def video_loop():
            async for msg in video:
                print("PC says:", msg.data)

        tasks = [asyncio.create_task(control_loop()), asyncio.create_task(video_loop())]
        await asyncio.sleep(0.5)   # let the clock sync settle
        seq = 0
        async for au in access_units(proc):
            nal_types = {au[i + 3] & 0x1F for i in range(len(au) - 3) if au[i:i + 3] == b"\x00\x00\x01"}
            key = 5 in nal_types
            t = now_ms() + args.clock_skew_ms
            await video.send_bytes(HEADER.pack(1, 1 if key else 0, 0, seq, t, t) + au)
            seq += 1
        print(f"sent {seq} frames")
        await asyncio.sleep(0.5)
        for task in tasks:
            task.cancel()
        await control.close()
        await video.close()


if __name__ == "__main__":
    asyncio.run(main())
